#!/usr/bin/env python3
"""Audit and rewrite the one-liners grounding selects files by: KB article summaries and doc descriptions.

    audit.py collect-docs  <checkout> [--mirror]        brain/mirror `description:` frontmatter
    audit.py collect-kb    [--tenant SLUG] [--from-dir DIR]   live /kb (console) or a local KB dump
    audit.py judge         --work DIR [--workers 8]     Haiku verdict + rewrite per item (cached)
    audit.py report        --work DIR [--work DIR …] --out FILE.html
    audit.py apply-docs    --work DIR [--skip KEY …]    rewrite frontmatter in place, drop description_pin
    audit.py apply-kb      --work DIR [--dry-run] [--skip KEY …]   rp project knowledge summaries apply

Each work dir holds items.jsonl (collect) and verdicts.jsonl (judge). Verdicts are cached by a hash of
the prompt version + item, so a re-run only pays for new or changed items. The judge is Haiku through
the local `claude -p` (the developer's own Claude login; no key handling here).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

import yaml

MODEL = "claude-haiku-5-5"
PROMPT_VERSION = "2026-10-20.1"
KB_SUMMARY_MAX = 200  # host kbsync.summaryMaxLen truncates past this
DOC_FIRST_SENTENCE = 150  # tree gloss cut (treeview.descMaxLen)
DOC_DESC_MAX = 1024  # kit lint FAIL / host DocDescription cap
BODY_CAP = 6000
MIRROR_ROOTS = ("skills", ".agents/skills", ".claude/skills", "doc", "docs")  # grounding.mirrorCatalogRoots
VERDICTS = ("ok", "vague", "wrong", "boilerplate")
CONSOLE_OUT = "/tmp/replypen-out"

# Openers that talk about the text instead of its content (nl/en/fr/de). A rewrite starting with one is
# rejected; the judge prompt names the same family.
BOILERPLATE = re.compile(
    r"^\s*(this|the) (article|page|guide|document|doc|section|faq|runbook|file|skill)\b"
    r"|^\s*(dit|deze|het) (artikel|pagina|handleiding|document|overzicht|bestand|gids|runbook)\b"
    r"|^\s*(cet|cette|ce) (article|page|guide|document)\b"
    r"|^\s*(dieser|diese|dieses) (artikel|seite|anleitung|dokument)\b"
    r"|^\s*(legt uit|leer|ontdek|lees( hier)?|bekijk|raadpleeg|uitleg over|stappenplan om|helpt)\b"
    r"|^\s*(learn|discover|read|explains?|describes?|helps?( users)?|provides?|overview of|guide (to|for))\b",
    re.IGNORECASE,
)


# ── shared bits ──────────────────────────────────────────────────────────────────────────────────────

def split_frontmatter(raw: str) -> tuple[list[str], str] | None:
    """(frontmatter lines, body) for a file opening with a terminated `---` fence, else None."""
    lines = raw.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") in ("---", "..."):
            return lines[1:i], "\n".join(lines[i + 1:])
    return None


def load_fm(fm: list[str]) -> dict[str, Any]:
    try:
        doc = yaml.safe_load("\n".join(line.rstrip("\r") for line in fm))
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def cap(text: str, n: int = BODY_CAP) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + "\n…[truncated]"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def first_sentence(text: str) -> str:
    m = re.search(r"[.!?](\s|$)", text)
    return text[: m.end()].strip() if m else text.strip()


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


# ── collect: docs ────────────────────────────────────────────────────────────────────────────────────

def collect_docs(root: Path, mirror: bool) -> list[dict]:
    """Every described .md the host catalogs (grounding.RenderSkillsCatalog): a brain walks all but
    dot-dirs and the root journal/; a mirror walks only MIRROR_ROOTS. Symlinks dedupe by real path."""
    root = root.resolve()
    starts = [root / r for r in MIRROR_ROOTS] if mirror else [root]
    seen: set[Path] = set()
    items = []
    for start in starts:
        if not start.exists():
            continue
        real_start = start.resolve()
        if real_start in seen:
            continue
        seen.add(real_start)
        for path in sorted(real_start.rglob("*.md")):
            rel_parts = path.relative_to(real_start).parts
            if any(p.startswith(".") for p in rel_parts):
                continue
            rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
            if not mirror and rel.split("/")[0] in ("journal", "node_modules"):
                continue
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            raw = path.read_text(encoding="utf-8", errors="replace")
            parts = split_frontmatter(raw)
            if not parts:
                continue
            fm = load_fm(parts[0])
            desc = fm.get("description")
            if not isinstance(desc, str) or not desc.strip():
                continue
            items.append({
                "key": rel, "kind": "doc", "path": rel, "title": str(fm.get("name") or first_heading(parts[1]) or rel),
                "current": " ".join(desc.split()), "body": cap(parts[1]),
                "pinned": "description_pin" in fm,
            })
    return items


# ── collect: kb ──────────────────────────────────────────────────────────────────────────────────────

def kb_item(path: str, raw: str, plane: str) -> dict | None:
    parts = split_frontmatter(raw)
    if not parts:
        return None
    fm = load_fm(parts[0])
    provider, art_id = fm.get("provider"), fm.get("id")
    if not provider or art_id in (None, ""):
        return None
    return {
        "key": f"{plane}/{provider}/{art_id}", "kind": "kb", "plane": plane, "provider": str(provider),
        "id": str(art_id), "path": path, "title": str(fm.get("title") or first_heading(parts[1])),
        "current": " ".join(str(fm.get("summary") or "").split()), "body_hash": str(fm.get("body_hash") or ""),
        "locale": str(fm.get("locale") or ""), "body": cap(parts[1]),
    }


def plane_of(rel: str) -> str:
    return "tenant" if rel.startswith("tenant/") else "project"


def collect_kb_dir(kb_root: Path, want: str) -> list[dict]:
    """A local copy of a project's kb/ (prod dump): `<provider>/…` is the project plane, `tenant/<provider>/…`
    the tenant plane."""
    items = []
    for path in sorted(kb_root.rglob("*.md")):
        rel = path.relative_to(kb_root).as_posix()
        if path.name == "INDEX.md" or "/_assets/" in rel or plane_of(rel) != want:
            continue
        item = kb_item(rel, path.read_text(encoding="utf-8", errors="replace"), want)
        if item:
            items.append(item)
    return items


def console(brain: Path, tenant: str | None, *args: str) -> subprocess.CompletedProcess:
    argv = [_cli(), "dev", "console"] + (["--tenant", tenant] if tenant else []) + list(args)
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, cwd=brain, check=False)


def collect_kb_live(brain: Path, tenant: str | None, work: Path, via: str | None = None) -> list[dict]:
    """The run's own /kb view through `rp dev console`: every article concatenated into one artifact,
    fetched with `file get` (console stdout caps at 64 KiB). `via` reads the PROJECT plane through a
    tenant console, for tenant-enabled projects that refuse a tenantless one (TENANT_REQUIRED); that view
    holds every general article plus `via`'s own restricted ones."""
    root = "/kb/tenant" if tenant else "/kb"
    prune = "" if tenant else " -path /kb/tenant -prune -o"
    tenant = tenant or via
    script = (f"mkdir -p {CONSOLE_OUT}; find {root}{prune} -name '*.md' ! -name INDEX.md ! -path '*/_assets/*' -print"
              f" | while read f; do echo \"@@ $f\"; cat \"$f\"; done > {CONSOLE_OUT}/kb-all.txt; wc -c {CONSOLE_OUT}/kb-all.txt")
    done = console(brain, tenant, "bash", "run", "-o", "json", "--raw-output", script)
    if done.returncode != 0:
        raise SystemExit(f"console bash run failed: {(done.stderr or done.stdout).strip()[:400]}")
    target = work / "raw" / "kb-all.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    got = console(brain, tenant, "file", "get", f"{CONSOLE_OUT}/kb-all.txt", "--out", str(target))
    if got.returncode != 0 or not target.exists():
        raise SystemExit(f"console file get failed: {(got.stderr or got.stdout).strip()[:400]}")
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in target.read_text(encoding="utf-8", errors="replace").split("\n"):
        if line.startswith("@@ /kb/") and line.endswith(".md"):
            current = line[3:].strip()
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    plane = "tenant" if root == "/kb/tenant" else "project"
    items = []
    for path, lines in sorted(blocks.items()):
        item = kb_item(path.removeprefix("/kb/"), "\n".join(lines), plane)
        if item:
            items.append(item)
    if not items:
        raise SystemExit(f"no articles parsed under {root}: an audit of nothing would read as all-clear")
    return items


# ── judge ────────────────────────────────────────────────────────────────────────────────────────────

KB_SYSTEM = """You audit the one-line summaries of help-centre articles. A retrieval step decides which article to open by reading ONLY each article's title and summary in a long index. So a summary must say what question the article answers, with the concrete specifics a searcher would match on.

For each item you get: key, title, current summary, article body. Judge the CURRENT summary against the body:
- "ok": accurate, content-first, specific enough to tell this article from its siblings. Small style flaws are fine. When in doubt: ok.
- "boilerplate": opens with words about the text itself instead of its content: "This article…", "Dit artikel…", "Deze handleiding…", "Legt uit…", "Leer hoe…", "Ontdek…", "Lees hier…", "Uitleg over…", "Learn how…", "Explains…", "Helps users…", "Overview of…".
- "wrong": about something the article is not mainly about, misses the main question the title asks (e.g. the title asks whether X can still be changed but the summary only states a limit), contradicts the body, or is written in a different language than the body.
- "vague": true but too generic to pick this article over a sibling: no concrete subject, numbers, deadlines, screens or terms from the body.

Rewrite ONLY when the verdict is not ok. Rewrite rules:
- ONE sentence, at most 180 characters, in the language of the article body.
- Start with the content itself: the answer, rule or task. Never mention "this article/page/guide" and never open with Learn/Discover/Read/Explains/Helps/Leer/Ontdek/Lees/Legt uit/Uitleg or similar.
- First answer the main question the title asks, then add the most distinctive specifics from the body (numbers, deadlines, menu or button names, product terms).
- Only facts stated in the body. Never invent numbers, names, prices or steps.
- Keep the good parts of the current summary.

Reply with ONLY a JSON array, one object per item, in input order:
[{"key": "...", "verdict": "ok|vague|wrong|boilerplate", "reason": "max 15 words", "rewrite": "... or empty string when ok"}]"""

DOC_SYSTEM = """You audit the `description:` frontmatter of docs in a support agent's knowledge repo (a "brain": skills, runbooks, knowledge docs). A grounding step picks which docs to open from a tree that shows each path with the first 150 characters of its description; the full description is also searched with grep. So the description must say WHEN to open the doc (the customer question, symptom or task it serves), up front, in the words customers and support agents use.

For each item you get: key (path), title, current description, body. Judge the CURRENT description against the body:
- "ok": the first ~150 characters say what the doc is for or when to open it, accurately. Long descriptions with extra detail after that are fine. When in doubt: ok.
- "boilerplate": opens with filler about the text ("This document…", "Dit bestand…", "Contains…", "Notes on…", "Guide to…", "Overview of…") so the when-to-use trigger is pushed past 150 characters or missing.
- "wrong": describes a different topic than the body, contradicts the body, or misses the body's main purpose.
- "vague": true but too generic to route on: no concrete trigger, symptom, task or domain term.

Rewrite ONLY when the verdict is not ok. Rewrite rules:
- Keep the language of the current description (Dutch stays Dutch) and keep the customer words it already has.
- First sentence at most 150 characters: the when-to-use trigger, content-first (the question, symptom or task), e.g. "Ouder vraagt waarom een fiscaal attest ontbreekt of fout is: …". Then optionally 1-2 more sentences with distinctive specifics: scope, key terms, what it does not cover.
- Preserve every correct and useful pointer from the current description (file, script or tool names, scope limits, related docs). Only facts from the body or the current description; never invent.
- No markdown, no line breaks, at most 600 characters, no longer than needed.

Reply with ONLY a JSON array, one object per item, in input order:
[{"key": "...", "verdict": "ok|vague|wrong|boilerplate", "reason": "max 15 words", "rewrite": "... or empty string when ok"}]"""


def cache_key(item: dict) -> str:
    blob = json.dumps([PROMPT_VERSION, item["kind"], item["title"], item["current"], item["body"]], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def call_model(system: str, payload: list[dict]) -> list[dict]:
    argv = ["claude", "-p", "--model", MODEL, "--output-format", "json", "--tools", "",
            "--no-session-persistence", "--system-prompt", system]
    done = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                          text=True, timeout=600, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"claude -p exit {done.returncode}: {(done.stderr or done.stdout)[:300]}")
    envelope = json.loads(done.stdout)
    text = str(envelope.get("result") or "")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError(f"no JSON array in reply: {text[:200]}")
    return json.loads(text[start:end + 1])


def check(item: dict, out: dict) -> dict:
    """Bound one model verdict. A rejected rewrite keeps the verdict but blanks the rewrite, so a bad
    rewrite never reaches apply; ok never carries a rewrite (churn guard)."""
    verdict = str(out.get("verdict") or "").strip().lower()
    rewrite = " ".join(str(out.get("rewrite") or "").split())
    res = {"key": item["key"], "verdict": verdict if verdict in VERDICTS else "ok",
           "reason": str(out.get("reason") or "")[:200], "rewrite": "", "rejected": ""}
    if res["verdict"] == "ok":
        return res
    if not rewrite or rewrite == item["current"]:
        res["rejected"] = "empty or unchanged rewrite"
    elif BOILERPLATE.search(rewrite):
        res["rejected"] = "rewrite opens with boilerplate"
    elif item["kind"] == "kb" and len(rewrite) > KB_SUMMARY_MAX:
        res["rejected"] = f"rewrite over {KB_SUMMARY_MAX} chars"
    elif item["kind"] == "doc" and len(rewrite) > DOC_DESC_MAX:
        res["rejected"] = f"rewrite over {DOC_DESC_MAX} chars"
    elif item["kind"] == "doc" and len(first_sentence(rewrite)) > DOC_FIRST_SENTENCE + 30:
        res["rejected"] = f"first sentence over {DOC_FIRST_SENTENCE} chars: the trigger would be cut"
    else:
        res["rewrite"] = rewrite
    return res


def judge(work: Path, workers: int, batch: int | None, limit: int | None) -> None:
    items = read_jsonl(work / "items.jsonl")[:limit]
    if not items:
        raise SystemExit(f"no items in {work}/items.jsonl; run a collect first")
    cache_path = work / "cache.jsonl"
    cache = {r["ck"]: r for r in read_jsonl(cache_path)}
    todo = [i for i in items if cache_key(i) not in cache]
    kind = items[0]["kind"]
    system = KB_SYSTEM if kind == "kb" else DOC_SYSTEM
    size = batch or (8 if kind == "kb" else 4)
    batches = [todo[i:i + size] for i in range(0, len(todo), size)]
    print(f"{len(items)} items, {len(items) - len(todo)} cached, {len(batches)} model calls", file=sys.stderr)

    def run(chunk: list[dict]) -> list[dict]:
        payload = [{k: i[k] for k in ("key", "title", "current", "body")} for i in chunk]
        for attempt in range(2):
            try:
                by_key = {str(o.get("key")): o for o in call_model(system, payload) if isinstance(o, dict)}
                if all(i["key"] in by_key for i in chunk):
                    return [dict(check(i, by_key[i["key"]]), ck=cache_key(i)) for i in chunk]
            except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
                if attempt:
                    print(f"batch failed ({chunk[0]['key']}…): {exc}", file=sys.stderr)
        return []

    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool, cache_path.open("a", encoding="utf-8") as fh:
        futures = [pool.submit(run, c) for c in batches]
        for n, fut in enumerate(as_completed(futures), 1):
            rows = fut.result()
            failed += 0 if rows else 1
            for r in rows:
                cache[r["ck"]] = r
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            if n % 10 == 0:
                print(f"  {n}/{len(batches)} calls", file=sys.stderr)
    verdicts = [dict(cache[cache_key(i)], key=i["key"]) for i in items if cache_key(i) in cache]
    write_jsonl(work / "verdicts.jsonl", verdicts)
    counts = {v: sum(r["verdict"] == v for r in verdicts) for v in VERDICTS}
    rewritten = sum(bool(r["rewrite"]) for r in verdicts)
    print(json.dumps({"checked": len(verdicts), **counts, "rewrites": rewritten, "failed_batches": failed}))
    if failed:
        raise SystemExit(f"{failed} batches failed; re-run judge (cached items are skipped)")


# ── report ───────────────────────────────────────────────────────────────────────────────────────────

def report(works: list[Path], out: Path) -> None:
    sections, totals = [], []
    for work in works:
        items = {i["key"]: i for i in read_jsonl(work / "items.jsonl")}
        verdicts = read_jsonl(work / "verdicts.jsonl")
        meta = json.loads((work / "meta.json").read_text()) if (work / "meta.json").exists() else {}
        label = meta.get("label") or work.name
        flagged = [v for v in verdicts if v["verdict"] != "ok"]
        counts = {v: sum(r["verdict"] == v for r in verdicts) for v in VERDICTS}
        totals.append((label, len(verdicts), len(flagged), sum(bool(v["rewrite"]) for v in flagged), counts))
        rows = []
        for v in sorted(flagged, key=lambda r: (VERDICTS.index(r["verdict"]) * -1, r["key"])):
            it = items.get(v["key"], {})
            after = html.escape(v["rewrite"]) if v["rewrite"] else f"<em>rejected: {html.escape(v['rejected'])}</em>"
            rows.append(
                f"<tr class='{v['verdict']}'><td><b>{html.escape(it.get('title', ''))}</b><br><code>{html.escape(it.get('path', v['key']))}</code></td>"
                f"<td><span class='v'>{v['verdict']}</span><br><small>{html.escape(v['reason'])}</small></td>"
                f"<td class='before'>{html.escape(it.get('current', ''))}</td><td class='after'>{after}</td></tr>")
        sections.append(f"<h2>{html.escape(label)}</h2><p>{len(verdicts)} checked · {len(flagged)} flagged · "
                        + " · ".join(f"{k} {n}" for k, n in counts.items()) + "</p>"
                        + "<table><tr><th>item</th><th>verdict</th><th>before</th><th>after</th></tr>"
                        + "".join(rows) + "</table>")
    summary = "".join(f"<tr><td>{html.escape(lbl)}</td><td>{c}</td><td>{f}</td><td>{r}</td>"
                      + "".join(f"<td>{n}</td>" for n in counts.values()) + "</tr>"
                      for lbl, c, f, r, counts in totals)
    doc = f"""<!doctype html><meta charset="utf-8"><title>Description audit {date.today()}</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1400px}}table{{border-collapse:collapse;width:100%;margin-bottom:24px}}
td,th{{border-bottom:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}}.before{{color:#8a1c1c}}.after{{color:#11632e}}
.v{{font-weight:600}}tr.wrong .v{{color:#b00}}tr.vague .v{{color:#a60}}tr.boilerplate .v{{color:#666}}code{{font-size:12px;color:#555}}</style>
<h1>Description audit · {date.today()} · {MODEL} · prompt {PROMPT_VERSION}</h1>
<table><tr><th>scope</th><th>checked</th><th>flagged</th><th>rewritten</th>{''.join(f'<th>{v}</th>' for v in VERDICTS)}</tr>{summary}</table>
{''.join(sections)}"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(out)


# ── apply ────────────────────────────────────────────────────────────────────────────────────────────

def replace_description(raw: str, new: str) -> str:
    """Swap the frontmatter `description:` (any scalar form, incl. folded/continued lines) for one
    double-quoted line and drop `description_pin:` (a human-owned line the host consolidation sweep
    never regenerates). Everything else stays byte-identical."""
    parts = split_frontmatter(raw)
    if not parts:
        raise ValueError("no frontmatter")
    fm, body = parts
    out, i, replaced = [], 0, False
    while i < len(fm):
        line = fm[i]
        key = re.match(r"^(description|description_pin)\s*:", line)
        if key:
            i += 1
            while i < len(fm) and (fm[i].startswith((" ", "\t")) or not fm[i].strip()):
                i += 1  # continuation / block-scalar lines of this key
            if key.group(1) == "description":
                out.append("description: " + json.dumps(new, ensure_ascii=False))
                replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        raise ValueError("no top-level description")
    result = "\n".join(["---", *out, "---"]) + "\n" + body
    before, after = load_fm(fm), load_fm(out)
    before.pop("description_pin", None)
    if after.get("description") != new or {k: v for k, v in after.items() if k != "description"} != \
            {k: v for k, v in before.items() if k != "description"}:
        raise ValueError("frontmatter round-trip mismatch")
    return result


def apply_docs(work: Path, skip: set[str]) -> None:
    meta = json.loads((work / "meta.json").read_text())
    root = Path(meta["root"])
    changed = 0
    for v in read_jsonl(work / "verdicts.jsonl"):
        if not v["rewrite"] or v["key"] in skip:
            continue
        path = root / v["key"]
        try:
            path.write_text(replace_description(path.read_text(encoding="utf-8"), v["rewrite"]), encoding="utf-8")
            changed += 1
        except (OSError, ValueError) as exc:
            print(f"skip {v['key']}: {exc}", file=sys.stderr)
    print(f"{changed} descriptions rewritten under {root}")


def apply_kb(work: Path, skip: set[str], dry_run: bool) -> None:
    """Send the reviewed rewrites to the host (`rp project knowledge summaries apply`). Each patch is pinned
    to the body_hash it was judged against: the host checks it against the synced KB and a scoped KB sync
    swaps the summary in; later syncs keep it until the article body changes."""
    meta = json.loads((work / "meta.json").read_text())
    items = {i["key"]: i for i in read_jsonl(work / "items.jsonl")}
    patches = [{"provider": it["provider"], "id": it["id"], "body_hash": it["body_hash"], "summary": v["rewrite"]}
               for v in read_jsonl(work / "verdicts.jsonl")
               if v["rewrite"] and v["key"] not in skip and (it := items.get(v["key"])) and it.get("body_hash")]
    if not patches:
        print("no rewrites to apply")
        return
    path = work.resolve() / "patches.json"  # rp runs from the brain dir: a relative --work would miss
    path.write_text(json.dumps({"patches": patches}, ensure_ascii=False, indent=1), encoding="utf-8")
    argv = [_cli()] + (["--tenant", meta["tenant"]] if meta.get("tenant") else ["--scope", "project"])
    argv += ["project", "knowledge", "summaries", "apply", "--from", str(path), "-o", "json"]
    if dry_run:
        argv.append("--dry-run")
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=meta.get("brain") or None, check=False)
    if done.returncode != 0:
        raise SystemExit(f"{' '.join(argv[:6])} failed: {(done.stderr or done.stdout).strip()[:600]}")
    res = json.loads(done.stdout)
    (work / ("apply-dry.json" if dry_run else "apply.json")).write_text(done.stdout, encoding="utf-8")
    counts: dict[str, int] = {}
    for r in res.get("results") or []:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(json.dumps({"sent": len(patches), **counts, "queued": res.get("queued"), "job_id": res.get("job_id"),
                      "dry_run": dry_run}))


# ── cli ──────────────────────────────────────────────────────────────────────────────────────────────

def default_work(base: Path, name: str) -> Path:
    return base / ".replypen" / "description-audit" / name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect-docs")
    p.add_argument("root", type=Path)
    p.add_argument("--mirror", action="store_true", help="catalog only the mirror skill/doc roots")
    p.add_argument("--work", type=Path)
    p = sub.add_parser("collect-kb")
    p.add_argument("--brain", type=Path, default=Path.cwd(), help="brain checkout (console scope)")
    p.add_argument("--tenant", help="audit the tenant KB plane (/kb/tenant) of this tenant")
    p.add_argument("--via-tenant", help="project plane through this tenant's console (TENANT_REQUIRED projects)")
    p.add_argument("--from-dir", type=Path, help="read a local kb/ dump instead of the live console")
    p.add_argument("--label")
    p.add_argument("--work", type=Path)
    p = sub.add_parser("judge")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--batch", type=int)
    p.add_argument("--limit", type=int)
    p = sub.add_parser("report")
    p.add_argument("--work", type=Path, action="append", required=True)
    p.add_argument("--out", type=Path, default=Path.home() / "Downloads" / f"description-audit-{date.today()}.html")
    for name in ("apply-docs", "apply-kb"):
        p = sub.add_parser(name)
        p.add_argument("--work", type=Path, required=True)
        p.add_argument("--skip", action="append", default=[], help="item key to leave alone (repeatable)")
        if name == "apply-kb":
            p.add_argument("--dry-run", action="store_true", help="host-side check only, queue nothing")
    args = ap.parse_args()

    if args.cmd == "collect-docs":
        root = args.root.resolve()
        work = args.work or default_work(root, "docs-mirror" if args.mirror else "docs")
        items = collect_docs(root, args.mirror)
        meta = {"mode": "docs", "root": str(root), "label": f"{root.name} (docs)"}
    elif args.cmd == "collect-kb":
        brain = args.brain.resolve()
        plane = "tenant" if args.tenant else "project"
        work = args.work or default_work(brain, f"kb-{args.tenant or 'project'}")
        items = (collect_kb_dir(args.from_dir, plane) if args.from_dir
                 else collect_kb_live(brain, args.tenant, work, args.via_tenant))
        meta = {"mode": "kb", "plane": plane, "tenant": args.tenant, "brain": str(brain),
                "label": args.label or f"{brain.name} KB ({args.tenant or 'project'})"}
    elif args.cmd == "judge":
        judge(args.work, args.workers, args.batch, args.limit)
        return 0
    elif args.cmd == "report":
        report(args.work, args.out)
        return 0
    elif args.cmd == "apply-docs":
        apply_docs(args.work, set(args.skip))
        return 0
    else:
        apply_kb(args.work, set(args.skip), args.dry_run)
        return 0
    write_jsonl(work / "items.jsonl", items)
    (work / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"{len(items)} items → {work}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import kit_fresh  # re-execs from origin/main when the installed kit is behind
    kit_fresh.ensure(__file__)
    sys.exit(main())
