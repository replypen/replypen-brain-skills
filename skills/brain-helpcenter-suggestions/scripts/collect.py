# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect one window of customer questions + the help-centre inventory into `evidence.json`.

    uv run skills/brain-helpcenter-suggestions/scripts/collect.py --days 60 \
        [--tenant SLUG] [--skip-tenant SLUG ...] [--kind email|chat|analysis ...] [--out DIR]

One report = one help centre, decided by the **mount**, not by the presence of tenants: a tenant KB
under `/kb/tenant/**` plus `--tenant` gives a per-tenant report, otherwise every project-level KB is
one help centre and the tenant is just a column. `--tenant` also filters the conversations.

Writes `<brain>/.replypen/helpcenter/<window end date>[-<tenant>]/`:
  conversations.tsv / articles.tsv  the read-whole tier (one short line each)
  digest.md                         the drill tier (full bodies)
  evidence.json                     the judge contract (its sha256 goes into suggestions.json)
  raw/articles/A*.md                every kb article verbatim, the anchor source for `edit:`
  raw/                              rc + console artifact cache (hdr-<run_id>.json per session)

Read-only: `rp fleet runs`, `rp run trace`, `rp project mailbox ls`, and guarded
`rp dev console bash run` reads. Never `rp ask`, never a write action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from posixpath import dirname
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain-fleet-report" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fr_common import Rc, clip, find_brain_root, guarded, json_objects, parallel, project_name
except ImportError:  # noqa: BLE001 - the sibling skill is the only hard dependency
    raise SystemExit(
        "brain-helpcenter-suggestions needs the sibling skill `brain-fleet-report` "
        "(scripts/fr_common.py) installed next to it. Reinstall the kit with brain-dev-upgrade."
    )

import corpus  # noqa: E402


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

MAX_HS_PAGES = 60
MAX_DAYS = 120
CONSOLE_TIMEOUT = 300
TRACE_TIMEOUT = 120
OUT = "/tmp/replypen-out"
MIN_RUNS_OVER_HELPSCOUT = 20  # fewer runs than this and a Help Scout box wins
CORPUS_KINDS = ("email", "chat", "analysis")
# `analysis` runs are Embassy support tickets: an admin filing one is an admin who did not find
# the article. Named in the coverage line so the reader knows which feed that is.
KIND_LABEL = {"analysis": "analysis runs (Embassy tickets)"}
# A trace header is ~200 KB, almost all of it prompt scaffolding we never read. Keep the corpus bits.
HEADER_DROP = ("bootstrap_turn", "system_prompt", "prompt_sections", "manifest_blocks",
               "tenant_settings", "tenant_settings_current", "guards", "grounding_sources", "notes")
# Verified 2026-09-08 (iBeauty, pro-backup): console stdout caps at 64 KiB, and a Help Scout page is
# ~270 KB and a 190-article INDEX overflows too, so anything bulky goes through an artifact file.
HS_PAGE_SCRIPT = (
    "import json,subprocess,os; os.makedirs('/tmp/replypen-out',exist_ok=True); "
    "r=subprocess.run(['python','-m','lib.api','get','helpscout','/conversations',"
    "'--query','status=all','--query','embed=threads','--query','page={page}',"
    "'--query','query=(createdAt:[{start} TO {end}])'],capture_output=True,text=True); "
    "p=json.loads(r.stdout); "
    "[c['_embedded'].update(threads=[t for t in c['_embedded'].get('threads',[]) if t.get('type')!='note']) "
    "for c in p.get('_embedded',{{}}).get('conversations',[]) if c.get('_embedded')]; "  # Beacon notes carry IP + browsing history
    "json.dump(p,open('/tmp/replypen-out/hc-page-{page}.json','w')); "
    "print(json.dumps(p.get('page')))"
)
FIND_INDEXES = "find /kb -maxdepth 4 -name INDEX.md 2>/dev/null"
BRAIN_KNOWLEDGE_SCRIPT = (
    "find /brain/knowledge -name '*.md' -exec sh -c 'echo \"@@ $1\"; grep -m1 \"^# \" \"$1\"' _ {} \\;"
)
TENANT_REQUIRED = "TENANT_REQUIRED"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="window length in days (default 60)")
    parser.add_argument("--tenant", help="tenant slug: filters runs and scopes /kb to that tenant")
    parser.add_argument("--skip-tenant", action="append", default=[], metavar="SLUG",
                        help="tenant whose conversations are internal traffic (repeatable)")
    parser.add_argument("--out", help="output directory (default .replypen/helpcenter/<end date>)")
    parser.add_argument("--source", choices=("auto", "runs", "helpscout"), default="auto",
                        help="corpus source; auto = runs unless fewer than %d and a Help Scout "
                             "mailbox exists" % MIN_RUNS_OVER_HELPSCOUT)
    parser.add_argument("--kind", action="append", choices=list(CORPUS_KINDS), metavar="KIND",
                        help=f"run kind to read ({'|'.join(CORPUS_KINDS)}, repeatable, default all)")
    args = parser.parse_args()
    args.kind = tuple(dict.fromkeys(args.kind or CORPUS_KINDS))
    if args.days > MAX_DAYS:
        parser.error(f"--days {args.days}: the window is capped at {MAX_DAYS} days "
                     "(one trace call per session, and older traffic answers a different product)")
    return args


def cover(feed: str, status: str, scanned: int, retained: int, reason: str | None = None) -> dict:
    return {"feed": feed, "status": status, "scanned": scanned, "retained": retained,
            "reason": clip(reason, 300) or None}


def _rc_console(brain_root: Path, tenant: str | None, *args: str) -> subprocess.CompletedProcess:
    """`--tenant` goes ONLY on console calls: RC_TENANT would also scope `rp fleet runs` to nothing."""
    argv = [_cli(), "dev", "console"] + (["--tenant", tenant] if tenant else []) + list(args)
    return subprocess.run(argv, capture_output=True, text=True, timeout=CONSOLE_TIMEOUT, cwd=brain_root)


def console(brain_root: Path, script: str, tenant: str | None = None) -> dict[str, Any]:
    """`rp dev console bash run` envelope: {stdout, stderr, exit_code, stdout_truncated}."""
    try:
        done = _rc_console(brain_root, tenant, "bash", "run", "-o", "json", "--raw-output", script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": -1, "stderr": f"{type(exc).__name__}: {exc}", "stdout": ""}
    docs = [d for d in json_objects(done.stdout) if isinstance(d, dict) and "exit_code" in d]
    if docs:
        return docs[-1]
    text = (done.stderr or "") + (done.stdout or "")
    return {"exit_code": done.returncode or -1, "stdout": "",
            "stderr": text.strip() or "no console envelope"}


def spill(brain_root: Path, target: Path, remote: str, tenant: str | None = None) -> str:
    """Fetch a workspace artifact the console wrote (stdout caps at 64 KiB, files do not)."""
    done = _rc_console(brain_root, tenant, "file", "get", remote, "--out", str(target))
    if done.returncode != 0 or not target.exists():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def at_blocks(text: str) -> dict[str, list[str]]:
    """`@@ /abs/path.md` header + its following lines. Every console reader here emits this."""
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("@@ /") and line.rstrip().endswith(".md"):
            current = line[3:].strip()
            blocks.setdefault(current, [])
        elif current:
            blocks[current].append(line)
    return blocks


def parse_frontmatter(lines: list[str]) -> dict[str, str]:
    """The leading `--- … ---` block of a mirrored article: top-level scalars only.

    `keywords`/`aliases` are inline lists and already come from the INDEX; everything the bot block
    needs (id, url, number, collection_id, section, locale, status) is a scalar.
    """
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep or not key or key[:1].isspace() or not key.strip().isidentifier():
            continue
        out[key.strip()] = value.strip().strip("'\"")
    return out


# ------------------------------------------------------------------ runs


def fetch_header(brain_root: Path, raw_dir: Path, run_id: str) -> tuple[dict[str, Any] | None, bool]:
    """First JSONL record of `rp run trace --stream`, cached as raw/hdr-<run_id>.json.

    Not `Rc.jsonl`: that buffers the whole trace stream (~200 KB per run, and the tail is turn
    transcripts we never read). Read one line, kill the process, persist the corpus keys only.
    """
    target = raw_dir / f"hdr-{run_id}.json"
    if target.exists():
        try:
            return json.loads(target.read_text(encoding="utf-8")), True
        except (OSError, json.JSONDecodeError):
            target.unlink(missing_ok=True)
    argv = [_cli(), "run", "trace", run_id, "--stream", "-o", "json", "--raw-output"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            cwd=brain_root)
    header = None
    try:
        for _ in range(20):  # the header is the first record; a banner line or two may precede it
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("run_id"):
                header = {k: v for k, v in value.items() if k not in HEADER_DROP}
                break
    finally:
        proc.kill()
        try:
            proc.wait(timeout=TRACE_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        if proc.stdout:
            proc.stdout.close()
    if header is None:
        return None, False
    target.write_text(json.dumps(header, ensure_ascii=False), encoding="utf-8")
    return header, False


def sessions(rows: list[dict]) -> list[dict[str, Any]]:
    """One conversation per session/thread: trace the LAST run (it carries the whole transcript)."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get("thread_id") or row.get("session_id") or row.get("run_id") or id(row))
        groups.setdefault(key, []).append(row)
    picked = []
    for runs in groups.values():
        ordered = sorted(runs, key=lambda r: str(r.get("created_at") or ""))
        picked.append({"run_id": str(ordered[-1].get("run_id") or ordered[-1].get("id") or ""),
                       "created_at": str(ordered[0].get("created_at") or ""),
                       "kind": str(ordered[-1].get("kind") or ""), "runs": len(runs)})
    return [p for p in picked if p["run_id"]]


def from_runs(rc: Rc, brain_root: Path, raw_dir: Path, rows: list[dict], start: datetime,
              end: datetime, tenant: str | None, skip_tenants: list[str],
              domains: frozenset[str], simulations: int,
              kinds: tuple[str, ...] = CORPUS_KINDS) -> tuple[list[dict], list[dict], Counter]:
    """Header per session, in parallel. Returns conversations, coverage feeds, tenant counts."""
    picked = sessions(rows)

    def worker(pick: dict) -> dict | None:
        header, cached = fetch_header(brain_root, raw_dir, pick["run_id"])
        return None if header is None else {**pick, "header": header, "cached": cached}

    fetched = [f for f in parallel(picked, guarded(rc, "runs", lambda p: p["run_id"], worker)) if f]
    tenants = Counter(str(f["header"].get("tenant") or "") for f in fetched)
    built: list[dict] = []
    for item in fetched:
        header = item["header"]
        if tenant and str(header.get("tenant") or "") != tenant:
            item["dropped"] = "other tenant"
            continue
        if not header.get("question") and not header.get("prior_messages"):
            # Message bodies age out of the trace (~2 weeks); the run row survives, the text does not.
            item["dropped"] = "no message payload left in the trace"
            continue
        item["dropped"] = "no inbound text"
        conv = corpus.trace_conversation(item["header"], domains)
        if not conv:
            continue
        conv["created_at"] = item["created_at"] or conv["created_at"]
        if not corpus.in_window(conv["created_at"], start, end):
            item["dropped"] = "outside the window"
            continue
        item["dropped"] = ""
        conv["_kind"] = item["kind"]
        built.append(conv)
    conversations = corpus.merge_duplicates(built)
    corpus.tag_duplicate_outreach(conversations)
    corpus.tag_noise(conversations, skip_tenants)

    feeds = []
    for kind in kinds:
        runs_of_kind = [r for r in rows if str(r.get("kind") or "") == kind]
        if not runs_of_kind:
            continue
        mine = [p for p in picked if p["kind"] == kind]
        seen = [f for f in fetched if f["kind"] == kind]
        convs = [c for c in conversations if c.get("_kind") == kind]
        drops = Counter(f["dropped"] for f in seen if f["dropped"])
        parts = [f"{len(convs)} kept", f"{len(mine)} sessions traced "
                 f"({sum(1 for f in seen if f['cached'])} cached)",
                 f"{len(mine) - len(seen)} trace unavailable"]
        parts += [f"{count} {label}" for label, count in drops.most_common()]
        parts += [f"{len([c for c in built if c.get('_kind') == kind]) - len(convs)} duplicate",
                  f"{simulations} simulation"]
        oldest = min((c["created_at"] for c in convs), default="")
        if drops.get("no message payload left in the trace") and oldest:
            parts.append(f"text only from {oldest[:10]} on")
        feeds.append(cover(f"{kind}_runs", "complete" if len(seen) == len(mine) else "partial",
                           len(runs_of_kind), len(convs),
                           f"{len(runs_of_kind)} {KIND_LABEL.get(kind, kind + ' runs')}"
                           " in window: "
                           + " · ".join(p for p in parts if p and not p.startswith("0 "))))
    for conv in conversations:
        conv.pop("_kind", None)
    return conversations, feeds, tenants


def other_runs_feed(rows: list[dict]) -> dict:
    """Kinds outside the corpus, counted so the report can say what it did not read."""
    kinds = Counter(str(r.get("kind") or "?") for r in rows)
    parts = []
    for kind, count in kinds.most_common():
        failed = sum(1 for r in rows if str(r.get("kind") or "?") == kind
                     and (r.get("outcome") == "failed" or r.get("status") == "error"))
        parts.append(f"{count} {kind} runs" + (f", {failed} failed" if failed else ""))
    return cover("other_runs", "complete", sum(kinds.values()), 0,
                 (" · ".join(parts) + ", not part of this corpus") if parts else None)


def hs_pages(brain_root: Path, raw_dir: Path, start: datetime, end: datetime) -> tuple[list[Any], dict]:
    """Page loop over the workspace connector; each page lands in raw/hs-page-<n>.json."""
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    pages: list[Any] = []
    total, number, failed = 1, 1, None
    while number <= total and number <= MAX_HS_PAGES:
        target = raw_dir / f"hs-page-{number}.json"
        if not target.exists():
            script = HS_PAGE_SCRIPT.format(page=number, start=start.strftime(stamp),
                                           end=end.strftime(stamp))
            envelope = console(brain_root, "python -c " + shlex.quote(script))
            if envelope.get("exit_code") != 0 or not str(envelope.get("stdout") or "").strip():
                failed = f"page {number}: {clip(envelope.get('stderr') or 'empty stdout', 160)}"
                break
            if not spill(brain_root, target, f"{OUT}/hc-page-{number}.json"):
                failed = f"page {number}: file get failed"
                break
        try:
            page = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failed = f"page {number}: {type(exc).__name__}"
            break
        pages.append(page)
        if number == 1:
            total = int(((page.get("page") or {}).get("totalPages")) or 1)
        number += 1
    if failed is None and total > MAX_HS_PAGES:
        failed = f"{total} pages available, stopped at MAX_HS_PAGES={MAX_HS_PAGES}"
    return pages, cover("helpscout", "partial" if failed else "complete", 0, 0, failed)


def from_helpscout(brain_root: Path, raw_dir: Path, start: datetime, end: datetime,
                   skip_tenants: list[str]):
    pages, coverage = hs_pages(brain_root, raw_dir, start, end)
    seen: dict[str, dict] = {}
    scanned = 0
    for page in pages:
        for conv in corpus.helpscout_conversations(page):
            scanned += 1
            if corpus.in_window(conv["created_at"], start, end):
                seen.setdefault(conv["id"], conv)
    conversations = corpus.merge_split_chats(list(seen.values()))
    corpus.tag_duplicate_outreach(conversations)
    corpus.tag_noise(conversations, skip_tenants)
    coverage.update(scanned=scanned, retained=len(conversations),
                    status="unavailable" if not pages else coverage["status"],
                    reason=coverage["reason"] or f"{scanned} conversations on {len(pages)} page(s): "
                    f"{len(conversations)} kept · {scanned - len(seen)} outside the window · "
                    f"{len(seen) - len(conversations)} merged chat fragments")
    return conversations, [coverage]


# ------------------------------------------------------------------ inventory


def article(aid: str, home: str, path: str, title: str, **extra: Any) -> dict[str, Any]:
    base = {"id": aid, "home": home, "path": path, "title": title, "url": None, "provider": None,
            "provider_id": None, "number": None, "collection_id": None, "parent_type": None,
            "locale": None, "status": None, "keywords": [], "aliases": [], "summary": "",
            "collection": None, "area": None, "audience": None, "updated_at": None,
            "deprecated": False}
    return {**base, **extra}


def parse_indexes(text: str) -> list[dict[str, Any]]:
    """`@@ /kb/<…>/INDEX.md` blocks -> articles. The path is the truth; the header comment is not."""
    articles: list[dict[str, Any]] = []
    for index_path, lines in at_blocks(text).items():
        root = dirname(index_path)
        for line in lines:
            parts = [p.strip() for p in line.split(" · ")]
            if line.startswith("#") or len(parts) < 2 or not parts[0].endswith(".md"):
                continue
            labelled = {p.split(":", 1)[0].strip(): p.split(":", 1)[1].strip()
                        for p in parts[4:] if ":" in p}
            articles.append(article(
                f"A{len(articles) + 1}", "kb", f"{root}/{parts[0]}", parts[1],
                keywords=[k.strip() for k in parts[2].split(",") if k.strip()] if len(parts) > 2 else [],
                aliases=[a.strip() for a in labelled.get("aliases", "").split(",") if a.strip()],
                summary=parts[3] if len(parts) > 3 else "", collection=labelled.get("collection"),
                area=labelled.get("area"), audience=labelled.get("audience"),
                deprecated="DEPRECATED" in parts))
    # Ids follow path order, not INDEX order: stable across runs while the KB is unchanged.
    articles.sort(key=lambda a: a["path"])
    for number, entry in enumerate(articles, 1):
        entry["id"] = f"A{number}"
    return articles


def kb_scope(indexes: list[str], tenant: str | None) -> tuple[str, list[str]]:
    """The mount decides, not the presence of tenants. Exits 2 when there is no single help centre."""
    tenant_indexes = [p for p in indexes if p.startswith("/kb/tenant/")]
    project_indexes = [p for p in indexes if not p.startswith("/kb/tenant/")]
    if tenant and tenant_indexes:
        return "tenant", tenant_indexes
    if project_indexes:
        return "project", project_indexes
    if tenant_indexes:
        print("this project mounts help centres per tenant (/kb/tenant/**) and none at project "
              "level. Re-run per tenant: collect.py --tenant <slug>", file=sys.stderr)
    else:
        print("no INDEX.md under /kb, nothing to hold the questions against", file=sys.stderr)
    raise SystemExit(2)


def find_indexes(brain_root: Path, tenant: str | None,
                 fallback: str | None) -> tuple[list[str], str | None, str | None]:
    """`find /kb`, retrying once with an inferred tenant when the console refuses tenantless calls.

    kampadmin-support serves a project-level KB but rejects a tenantless console (403
    TENANT_REQUIRED); any tenant sees the same mount, so the most frequent one in the runs is fine.
    """
    found = console(brain_root, FIND_INDEXES, tenant)
    used, inferred = tenant, None
    if TENANT_REQUIRED in str(found.get("stderr") or "") and not tenant and fallback:
        used = inferred = fallback
        found = console(brain_root, FIND_INDEXES, used)
    indexes = [p for p in str(found.get("stdout") or "").split() if p.endswith("INDEX.md")]
    if not indexes:
        print("no /kb INDEX.md found, no report "
              f"({clip(found.get('stderr'), 160) or 'find returned nothing'})", file=sys.stderr)
        raise SystemExit(2)
    return indexes, used, inferred


def fetch_bodies(brain_root: Path, raw_dir: Path, roots: list[str], articles: list[dict],
                 tenant: str | None) -> int:
    """Every kb article verbatim into raw/articles/<Aid>.md: the anchor source for `edit:`.

    One console call concatenates the whole KB into an artifact (~1 MB on kampadmin-support), one
    `file get` brings it back. Ids follow path order, so the directory is rewritten every collect.
    """
    listed = " ".join(shlex.quote(r) for r in roots)
    console(brain_root, f"mkdir -p {OUT}; for f in $(find {listed} -name '*.md' ! -name INDEX.md); "
                        f"do echo \"@@ $f\"; cat \"$f\"; done > {OUT}/hc-bodies.txt", tenant)
    blocks = at_blocks(spill(brain_root, raw_dir / "hc-bodies.txt", f"{OUT}/hc-bodies.txt", tenant))
    target_dir = raw_dir / "articles"
    target_dir.mkdir(parents=True, exist_ok=True)
    for stale in target_dir.glob("A*.md"):
        stale.unlink()
    written = 0
    for item in articles:
        lines = blocks.get(item["path"])
        if lines is None:
            continue
        (target_dir / f"{item['id']}.md").write_text("\n".join(lines).strip() + "\n",
                                                     encoding="utf-8")
        written += 1
        front = parse_frontmatter(lines)
        section = front.get("section")
        item.update(
            url=front.get("url") or None, provider=front.get("provider") or None,
            # KnowledgeOwl's `id` is the url_hash slug (deleted twins share it); `article_id` is its real id.
            provider_id=front.get("article_id") or front.get("id") or None, number=front.get("number") or None,
            collection_id=front.get("collection_id") or front.get("category_id") or section or None,
            parent_type=("section" if section and not front.get("collection_id") else None),
            locale=front.get("locale") or None, status=front.get("status") or None,
            updated_at=front.get("updated_at") or None,
            audience=item["audience"] or front.get("audience") or None)
    return written


def inventory(brain_root: Path, raw_dir: Path, tenant: str | None,
              fallback_tenant: str | None = None) -> tuple[list[dict], dict]:
    """One help centre: the tenant's `/kb/tenant/**` KBs, or every project-level KB. Never mixed."""
    indexes, console_tenant, inferred = find_indexes(brain_root, tenant, fallback_tenant)
    scope, indexes = kb_scope(indexes, tenant)
    roots = sorted({dirname(p) for p in indexes})
    listed = " ".join(shlex.quote(p) for p in indexes)
    count_cmd = "; ".join(f"echo \"## {r} $(find {shlex.quote(r)} -name '*.md' ! -name INDEX.md "
                          f"| wc -l)\"" for r in roots)
    read = console(brain_root, f"mkdir -p {OUT}; for f in {listed}; do echo \"@@ $f\"; cat \"$f\"; "
                               f"done > {OUT}/hc-index.txt; {count_cmd}", console_tenant)
    articles = parse_indexes(spill(brain_root, raw_dir / "hc-index.txt", f"{OUT}/hc-index.txt",
                                   console_tenant))
    on_disk = sum(int(line.split()[-1]) for line in str(read.get("stdout") or "").splitlines()
                  if line.startswith("## ") and line.split()[-1].isdigit())
    if not articles:
        print(f"no articles parsed from {len(indexes)} INDEX.md under {', '.join(roots)} "
              f"({clip(read.get('stderr'), 160) or 'empty artifact'}). A report on an empty "
              f"inventory would be confidently wrong", file=sys.stderr)
        raise SystemExit(2)
    bodies = fetch_bodies(brain_root, raw_dir, roots, articles, console_tenant)
    docs = parse_brain_docs(
        str(console(brain_root, BRAIN_KNOWLEDGE_SCRIPT, console_tenant).get("stdout") or ""),
        len(articles))
    reasons = []
    partial = (bool(on_disk) and on_disk != len(articles)) or bodies != len(articles)
    if on_disk and on_disk != len(articles):
        reasons.append(f"{len(articles)} indexed vs {on_disk} .md on disk under {', '.join(roots)}")
    if bodies != len(articles):
        reasons.append(f"{len(articles) - bodies} article bodies missing (edit anchors unavailable)")
    if inferred:
        reasons.append(f"console scoped to tenant {inferred} (the KB is project-level; this project "
                       "refuses tenantless console calls)")
    first_url = next((a["url"] for a in articles if a.get("url")), None)
    base = urlsplit(first_url) if first_url else None
    return articles + docs, {
        "status": "partial" if partial else "complete",
        "scope": scope, "provider": next((a["provider"] for a in articles if a.get("provider")), None),
        "base_url": f"{base.scheme}://{base.netloc}" if base and base.netloc else None,
        "root": ", ".join(roots), "articles": len(articles), "brain_docs": len(docs),
        "reason": " · ".join(reasons) or None}


def parse_brain_docs(text: str, offset: int) -> list[dict[str, Any]]:
    """`@@ <path>` + the file's first `# ` heading, as emitted by BRAIN_KNOWLEDGE_SCRIPT."""
    docs: list[dict[str, Any]] = []
    for path, lines in at_blocks(text).items():
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
        if title:
            docs.append(article(f"A{offset + len(docs) + 1}", "brain", path, title))
    return docs


# ------------------------------------------------------------------ main


def mailboxes(rc: Rc) -> list[dict]:
    boxes = rc.json("project", "mailbox", "ls")
    boxes = boxes.get("mailboxes") if isinstance(boxes, dict) else boxes
    return [b for b in (boxes or []) if isinstance(b, dict)]


def pick_source(rc: Rc, brain_root: Path, raw_dir: Path, args, start, end):
    """One `rp fleet runs` call: `--kind` decides the corpus, the other kinds only get counted."""
    kinds = tuple(getattr(args, "kind", None) or CORPUS_KINDS)
    payload = rc.json("fleet", "runs", "--days", str(args.days))
    rows = [r for r in ((payload.get("runs") if isinstance(payload, dict) else payload) or [])
            if isinstance(r, dict) and corpus.in_window(r.get("created_at"), start, end)]
    corpus_rows = [r for r in rows if str(r.get("kind") or "") in kinds
                   and not r.get("simulation")]
    others = other_runs_feed([r for r in rows if str(r.get("kind") or "") not in kinds])
    boxes = mailboxes(rc)
    providers = sorted({str(b.get("provider") or "") for b in boxes})
    source = getattr(args, "source", "auto")
    if source == "auto" and "helpscout" in providers and len(corpus_rows) < MIN_RUNS_OVER_HELPSCOUT:
        print(f"only {len(corpus_rows)} runs in the window: reading Help Scout instead "
              "(--source runs to force the runs)", file=sys.stderr)
        source = "helpscout"
    if corpus_rows and source != "helpscout":
        # our own addresses: an `is_inbound` turn from one of these domains is still an agent
        domains = frozenset(str(b.get("email_address") or "").lower().rpartition("@")[2]
                            for b in boxes) - {""}
        simulations = sum(1 for r in rows if str(r.get("kind") or "") in kinds
                          and r.get("simulation"))
        convs, feeds, tenants = from_runs(rc, brain_root, raw_dir, corpus_rows, start, end,
                                          args.tenant, args.skip_tenant, domains, simulations,
                                          kinds)
        return "runs", convs, feeds + [others], tenants
    if "helpscout" in providers:
        convs, feeds = from_helpscout(brain_root, raw_dir, start, end, args.skip_tenant)
        return "helpscout", convs, feeds + [others], Counter()
    print(f"no {'/'.join(kinds)} runs in the window and no helpscout mailbox (providers: "
          f"{', '.join(providers) or 'none'}). v1 reads runs or Help Scout only "
          f": a recipe for this provider still has to be written (say so in learnings).",
          file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    brain_root = find_brain_root()
    end = datetime.now(timezone.utc)
    start = (end - timedelta(days=max(1, args.days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out_dir = Path(args.out).expanduser() if args.out else (
        brain_root / ".replypen" / "helpcenter"
        / (end.date().isoformat() + (f"-{args.tenant}" if args.tenant else "")))
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rc = Rc(raw_dir=raw_dir, cwd=brain_root)

    source, conversations, feeds, tenants = pick_source(rc, brain_root, raw_dir, args, start, end)
    conversations.sort(key=lambda c: str(c.get("created_at")))
    fallback = next((name for name, _ in tenants.most_common() if name), None)
    articles, kb = inventory(brain_root, raw_dir, args.tenant, fallback)
    corpus.link_articles(conversations, articles)

    evidence = {
        "schema_version": 1, "project": project_name(brain_root), "tenant": args.tenant,
        "collected_at": end.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
        "source": source, "kb": kb, "conversations": conversations, "articles": articles,
        "coverage": feeds + [cover("kb", kb["status"], kb["articles"], kb["articles"],
                                   kb["reason"])],
    }
    payload = json.dumps(evidence, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out_dir / "evidence.json").write_text(payload, encoding="utf-8")
    (out_dir / "digest.md").write_text(corpus.write_digest(evidence), encoding="utf-8")
    convs_tsv, articles_tsv = corpus.write_tsvs(evidence)
    (out_dir / "conversations.tsv").write_text(convs_tsv, encoding="utf-8")
    (out_dir / "articles.tsv").write_text(articles_tsv, encoding="utf-8")

    noisy = sum(1 for c in conversations if c.get("noise"))
    channels = Counter(c["channel"] for c in conversations)
    print(f"{evidence['project']}{'/' + args.tenant if args.tenant else ''} "
          f"{start.date()}→{end.date()} · source {source} · "
          f"{len(conversations)} conversations ("
          f"{', '.join(f'{n} {ch}' for ch, n in channels.most_common()) or 'none'}; "
          f"{noisy} pre-tagged noise, "
          f"{sum(1 for c in conversations if (c.get('reply') or {}).get('provenance') == 'human')} "
          f"with a human reply) · "
          f"{kb['articles']} kb articles ({kb['scope']} scope) + {kb['brain_docs']} brain docs "
          f"({kb['status']}) · {rc.calls} rc calls · {len(rc.errors)} errors · "
          f"{time.monotonic() - started:.1f}s")
    for feed in evidence["coverage"]:
        if feed.get("reason"):
            print(f"coverage {feed['feed']}: {feed['reason']}")
    print("evidence_sha256 " + hashlib.sha256(payload.encode("utf-8")).hexdigest()
          + "   (first line of classification.tsv)")
    if not conversations:
        print("no conversations collected, see coverage in evidence.json", file=sys.stderr)
    print(out_dir)
    return 1 if not conversations else 0


if __name__ == "__main__":
    raise SystemExit(main())
