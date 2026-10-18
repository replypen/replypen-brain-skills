#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""How often production runs actually EXECUTE this brain's helpers, and which SQL they hand-roll.

    usage.py --since DATE [--until DATE] [--tenant T] [--kind K …] [--staff-domain D …]
             [--mirror NAME=PATH …] [--refresh] [--input usage.json] --out DIR

Run from a brain checkout. Pages `rp run list` over the window, fetches `rp run events --stream` for
runs that ran bash, and `rp run trace --brief` for runs that launched a helper or sent SQL (tenant,
principal email). Writes DIR/usage.json + DIR/usage.md: coverage, one row per helper (inventory =
tracked .py in the brain and each [mirrors] checkout, minus tests/_internal/actions/.agents), and
repeated ad-hoc SQL grouped by table set. `--input usage.json` re-renders the markdown offline.

Counts are launch SITES from command text (execution.py), not mentions: a loop is one site, exit and
duration belong to the enclosing bash command. Cohorts: test = simulation, kind `prompt`, or a
principal email in --staff-domain; unknown = trace unavailable; else customer. Rankings use customer
runs. Cache of reduced per-run records (commands, exit, duration, traceback tail; no stdout):
<brain>/.replypen/helper-usage/<project>/ (gitignored). Stdlib only: `uv run --no-project python usage.py …`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution import is_prestep, normalise_sql, sites, sql_strings, sql_tables

SCHEMA = 3
WORKERS = 8
PENDING = {"queued", "running", "pending", "submitted", "starting", ""}
MOUNTS = ("/brain/", "/tenant/", "/mirrors/", "/skills/")
SKIP_PARTS = {".agents", ".claude", "tests", "__pycache__", "_internal", "actions", "node_modules", ".git"}
KEEP_CMD = re.compile(r"\.py\b|\bselect\b|\bpsql\b|\bimport\b", re.I)
SLOW_MS = 30_000
MOUNT_LABEL = {"/skills/": "shared /skills", "/tenant/": "tenant overlay"}
# Every rp call gets this scope: a checkout's pinned tenant (.replypen/local.toml, legacy
# .rootcause/local.toml) would otherwise silently narrow a project-wide window to one tenant.
SCOPE = ["--scope", "project"]


class UsageError(Exception):
    pass


# --- rp -------------------------------------------------------------------------------------
def rp(args: list[str], root: Path, tries: int = 3) -> str:
    cli = "rp" if shutil.which("rp") else "rc"
    for attempt in range(tries):
        try:
            done = subprocess.run([cli, *args, *SCOPE], cwd=root, capture_output=True, text=True, timeout=300)
        except FileNotFoundError as exc:
            raise UsageError("`rp` is not on PATH (brain-dev-upgrade)") from exc
        except subprocess.TimeoutExpired:
            done = None
        if done and done.returncode == 0:
            return done.stdout
        if attempt + 1 < tries:
            time.sleep(2 * (attempt + 1))
    detail = (done.stderr or done.stdout).strip()[-300:] if done else "timeout"
    raise UsageError(f"rp {' '.join(args[:3])} failed: {detail}")


def list_runs(args, root: Path) -> tuple[list[dict], int]:
    base = ["run", "list", "--since", args.since, "--limit", "100", "-o", "json", "--raw-output"]
    base += ["--until", args.until] if args.until else []
    runs, cursor, pages = {}, None, 0
    while True:
        page = json.loads(rp(base + (["--before", cursor] if cursor else []), root))
        pages += 1
        for run in page.get("runs") or []:
            runs.setdefault(run["run_id"], run)
        cursor = page.get("next_before")
        if not cursor or not page.get("runs"):
            return list(runs.values()), pages


# --- per-run records (cached) ---------------------------------------------------------------
def traceback_tail(text: str) -> list[str]:
    if "Traceback (most recent call last)" not in text:
        return []
    return [line for line in text.splitlines() if line.strip()][-5:]


def stderr_tail(text: str) -> str:
    """Last stderr lines: an argparse `usage: x.py …` / `x.py: error:` names the helper that exited."""
    return "\n".join([line for line in text.splitlines() if line.strip()][-3:])[-400:]


def foreign_traceback(tail: list[str], path: str) -> bool:
    """The agent's own heredoc (`File "<stdin>"`) raised, not this helper chained in the same bash."""
    text = "\n".join(tail)
    return ('File "<stdin>"' in text or 'File "<string>"' in text) and path.rsplit("/", 1)[-1] not in text


def bash_command(ev: dict) -> tuple[str, str]:
    """(command, provenance). After the host's 14-day scrub `command` is empty; `rp run debug` still
    carries the privacy-reduced `kept.command` (vocab words become ⟨vocab⟩, so some paths no longer match)."""
    if ev.get("command"):
        return ev["command"], "raw"
    kept = ev.get("kept")
    cmd = kept.get("command") if isinstance(kept, dict) else None
    return (cmd, "kept") if cmd else ("", "blind")


def reduce_events(lines: str, scrub: dict | None = None) -> list[dict]:
    out = []
    for line in lines.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("tool") != "bash":
            continue
        cmd, how = bash_command(ev)
        if scrub is not None and how != "raw":
            scrub[how] = scrub.get(how, 0) + 1
        if not KEEP_CMD.search(cmd):
            continue
        out.append({"seq": -1 if is_prestep(ev) else ev.get("seq"), "exit": ev.get("exit_code"),
                    "ms": ev.get("duration_ms"), "cmd": cmd,
                    "tb": traceback_tail(f"{ev.get('stdout') or ''}\n{ev.get('stderr') or ''}"),
                    "err": stderr_tail(ev.get("stderr") or "")})
    return out


def debug_events(run_id: str, root: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        rp(["run", "debug", run_id, "--out-dir", tmp], root)
        found = sorted(Path(tmp).glob("*.jsonl"))
        return found[0].read_text() if found else ""


def reduce_trace(text: str) -> dict:
    run = (json.loads(text) or {}).get("run") or {}
    claims = (((run.get("guards") or {}).get("principal_scope") or {}).get("claims") or {})
    return {"tenant": run.get("tenant"), "email": claims.get("email"),
            "brain": run.get("brain_resolved"), "question": " ".join(str(run.get("question") or "").split())[:160]}


def load_record(run: dict, cache: Path, root: Path, refresh: bool, need_trace: bool) -> dict:
    path = cache / f"{run['run_id']}.json"
    rec = None
    if path.exists() and not refresh:
        rec = json.loads(path.read_text())
        if rec.get("schema") != SCHEMA or not rec.get("terminal"):
            rec = None
    if rec is None:
        events = rp(["run", "events", run["run_id"], "--stream"], root)
        if any(e.get("tool") == "bash" and not e.get("command") for e in map(json.loads, events.splitlines())):
            events = debug_events(run["run_id"], root)  # scrubbed run: only debug keeps `kept.command`
        scrub: dict = {}
        rec = {"schema": SCHEMA, "run_id": run["run_id"],
               "terminal": str(run.get("status") or "").lower() not in PENDING,
               "bash": reduce_events(events, scrub), "scrub": scrub, "trace": None}
    if need_trace and rec["trace"] is None and any(b["seq"] is None or b["seq"] >= 0 for b in rec["bash"]):
        rec["trace"] = reduce_trace(rp(["run", "trace", run["run_id"], "--brief", "-o", "json", "--raw-output"], root))
    path.write_text(json.dumps(rec, ensure_ascii=False))
    return rec


# --- inventory ------------------------------------------------------------------------------
def tracked_py(repo: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=repo, capture_output=True, text=True, check=True).stdout
        files = out.split()
    except (OSError, subprocess.CalledProcessError):
        files = [str(p.relative_to(repo)) for p in repo.rglob("*.py")]
    return [f for f in files if not Path(f).name.startswith("test_") and Path(f).name != "conftest.py"]


SQL_REF = re.compile(r"\b(?:from|join)\s+(?:\"?\w+\"?\.)?\"?([A-Za-z_]\w*)", re.I)


def inventory(root: Path, mirrors: dict[str, Path]) -> tuple[dict[str, dict], list[str]]:
    rows, missing = {}, []
    sources = [("brain", root, "/brain/")] + [(name, p, f"/mirrors/{name}/") for name, p in mirrors.items()]
    for source, repo, mount in sources:
        if not repo.is_dir():
            missing.append(f"{source}: {repo}")
            continue
        for rel in tracked_py(repo):
            if SKIP_PARTS & set(Path(rel).parts[:-1]):
                # Not a grounding helper (actions/, .agents/, …), but it exists: a launch is not "⚠ not in checkout".
                rows[mount + rel] = {"path": mount + rel, "source": "excluded", "entry": False,
                                     "helper": False, "tables": set()}
                continue
            text = (repo / rel).read_text(errors="replace")
            rows[mount + rel] = {"path": mount + rel, "source": source,
                                 "entry": "__main__" in text or "argparse" in text,
                                 # a mirror's one-off scripts are not helpers: skills/ dir or catalog header
                                 "helper": source == "brain" or "skills" in Path(rel).parts
                                           or "# name:" in "\n".join(text.splitlines()[:40]),
                                 # tables the code queries (FROM/JOIN), not prose mentions
                                 "tables": {m.lower() for m in SQL_REF.findall(text)}}
    return rows, missing


VOCAB = "⟨vocab⟩"


def resolver(rows: dict[str, dict]):
    by_name = defaultdict(list)
    for path in rows:
        by_name[path.rsplit("/", 1)[-1]].append(path)
    # `import requests` must not land on a mirror's app module `apps/leave/requests.py`: helpers only.
    by_stem = {stem: [p for p in paths if (rows[p] or {}).get("helper", True)] for stem, paths in
               ((n[:-3], p) for n, p in by_name.items()) if stem not in sys.stdlib_module_names}

    def resolve(token: str, kind: str) -> str | None:
        if kind == "import":
            hits = by_stem.get(token.rsplit(".", 1)[-1]) or []
            return hits[0] if len(hits) == 1 else None
        token = re.sub(r"^\$\{?\w+\}?/", "", token).replace("/./", "/")
        if VOCAB in token:  # scrubbed run: re-join a privacy-reduced path when one inventory path fits
            pat = re.compile(("" if token.startswith("/") else ".*/")
                             + ".+?".join(re.escape(part) for part in token.split(VOCAB)) + "$")
            hits = [p for p in rows if pat.match(p)]
            if len(hits) == 1:
                return hits[0]
        if token.startswith(MOUNTS):
            return token
        if token.startswith("/"):
            return None  # /tmp/x.py and friends: ad-hoc, not a helper
        token = token.removeprefix("./")
        hits = [p for p in rows if p.endswith("/" + token)] or by_name.get(token.rsplit("/", 1)[-1], [])
        return hits[0] if len(hits) == 1 else (f"?/{token.rsplit('/', 1)[-1]}" if hits else None)

    return resolve


# --- aggregation ----------------------------------------------------------------------------
def cohort(run: dict, rec: dict, staff: list[str]) -> str:
    health = run.get("health") or {}
    email = ((rec.get("trace") or {}).get("email") or "").lower()
    if run.get("simulation") or health.get("simulation") or run.get("kind") == "prompt" \
            or any(email.endswith("@" + d.lower().lstrip("@")) for d in staff):
        return "test"
    return "customer" if rec.get("trace") else "unknown"


def pct(values: list[int], q: float) -> int | None:
    return sorted(values)[min(len(values) - 1, int(q * len(values)))] if values else None


def aggregate(runs: list[dict], records: dict[str, dict], rows: dict[str, dict], staff: list[str],
              tenant: str | None, base_url: str) -> tuple[list[dict], list[dict]]:
    resolve = resolver(rows)
    helpers: dict[str, dict] = {}
    groups: dict[tuple, dict] = {}

    def helper(path):
        if path not in helpers:
            inv = rows.get(path) or {}
            helpers[path] = {"path": path, "source": inv.get("source", "not in inventory"),
                             "entry": inv.get("entry", True), "helper": inv.get("helper", True),
                             "runs": defaultdict(set), "tenants": set(),
                             "launches": 0, "loop_sites": 0, "imports": 0, "help": 0, "help_chained": 0, "reads": 0,
                             "prestep_reads": 0, "exit_nonzero": 0, "exit_2": 0, "tracebacks": [],
                             "ms": [], "last_seen": None, "bash": set()}
        return helpers[path]

    for run in runs:
        rec = records.get(run["run_id"])
        if not rec:
            continue
        trace = rec.get("trace") or {}
        if tenant and trace and trace.get("tenant") != tenant:
            continue
        who = cohort(run, rec, staff)
        for b in rec["bash"]:
            pre = b["seq"] is not None and b["seq"] < 0
            found = [(site, resolve(site["path"], site["kind"])) for site in sites(b["cmd"])]
            found = [(site, path) for site, path in found if path]
            runs_something = any(s["kind"] in ("launch", "import") for s, _ in found)
            # The exit code is the whole bash command's: charge it to the helpers the stderr tail /
            # traceback names, else to the last launch site (the shell's exit) — never to every chained one.
            # Records cached before `err` existed fall back to the last site.
            ran = [p for s, p in found if s["kind"] in ("launch", "import")]
            blame = f"{b.get('err') or ''}\n" + "\n".join(b["tb"] or [])
            charged = {p for p in ran if re.search(rf"(?<![\w.-]){re.escape(p.rsplit('/', 1)[-1])}\b", blame)} \
                or set(ran[-1:])
            for site, path in found:
                h = helper(path)
                if pre:
                    h["prestep_reads"] += 1
                    continue
                if site["kind"] == "read":
                    h["reads"] += 1
                    continue
                if site["kind"] == "help":
                    # `x.py --help; x.py …` costs no turn; a bash call that only reads --help does.
                    h["help_chained" if runs_something else "help"] += 1
                    continue
                h["imports" if site["kind"] == "import" else "launches"] += 1
                h["loop_sites"] += site["loop"]
                h["runs"][who].add(run["run_id"])
                if who == "customer" and trace.get("tenant"):
                    h["tenants"].add(trace["tenant"])
                h["last_seen"] = max(h["last_seen"] or "", run.get("created_at") or "")
                key = (run["run_id"], b["seq"])
                if key not in h["bash"]:
                    h["bash"].add(key)
                    if b["exit"] not in (0, None) and path in charged:
                        h["exit_2" if b["exit"] == 2 else "exit_nonzero"] += 1
                    if b["ms"] is not None:
                        h["ms"].append(b["ms"])
                    if b["tb"] and not foreign_traceback(b["tb"], path):
                        h["tracebacks"].append({"run_id": run["run_id"], "tail": b["tb"]})
            if pre:
                continue
            for sql in sql_strings(b["cmd"]):
                norm = normalise_sql(sql)
                tables = tuple(sql_tables(norm))
                if not tables:
                    continue
                g = groups.setdefault(tables, {"tables": list(tables), "runs": defaultdict(set), "tenants": set(),
                                               "shapes": defaultdict(int), "sql": {}, "examples": [], "failed": 0,
                                               "slow": 0, "queries": 0})
                g["queries"] += 1
                g["shapes"][norm] += 1
                g["sql"].setdefault(norm, " ".join(sql.split())[:400])
                g["runs"][who].add(run["run_id"])
                if who == "customer" and trace.get("tenant"):
                    g["tenants"].add(trace["tenant"])
                g["failed"] += b["exit"] not in (0, None)
                g["slow"] += (b["ms"] or 0) > SLOW_MS
                if run["run_id"] not in {e["run_id"] for e in g["examples"]}:
                    g["examples"].append({"run_id": run["run_id"], "url": f"{base_url}/runs/{run['run_id']}",
                                          "question": trace.get("question", ""), "customer": who == "customer"})

    for path, inv in rows.items():
        if inv["source"] != "excluded":
            helper(path)
    table = []
    for h in helpers.values():
        if h["source"] in ("not in inventory", "excluded") and not (h["launches"] or h["imports"] or h["help"]
                                                                       or h["help_chained"]):
            continue  # reading some non-helper source file is not helper usage
        runs_by = {k: len(v) for k, v in h["runs"].items()}
        table.append({
            "path": h["path"], "source": h["source"], "kind": "entry" if h["entry"] else "library",
            "helper": h["helper"],
            "customer_runs": runs_by.get("customer", 0), "test_runs": runs_by.get("test", 0),
            "unknown_runs": runs_by.get("unknown", 0), "tenants": len(h["tenants"]),
            "launches": h["launches"], "loop_sites": h["loop_sites"], "imports": h["imports"],
            "exit_nonzero": h["exit_nonzero"], "exit_2": h["exit_2"], "tracebacks": h["tracebacks"][:5],
            "traceback_count": len(h["tracebacks"]), "median_ms": pct(h["ms"], .5),
            "p90_ms": pct(h["ms"], .9), "duration_n": len(h["ms"]), "help_calls": h["help"],
            "help_chained": h["help_chained"],
            "source_reads": h["reads"], "prestep_reads": h["prestep_reads"], "last_seen": h["last_seen"],
            "run_ids": sorted(set().union(*h["runs"].values()))[:20] if h["runs"] else [],
        })
    table.sort(key=lambda r: (-r["customer_runs"], -(r["launches"] + r["imports"]), -r["test_runs"],
                              -r["source_reads"], r["path"]))

    # A helper that mentions every observed table (a shared db lib) "covers" nothing: rank by specificity.
    seen_tables = {t.rsplit(".", 1)[-1] for g in groups.values() for t in g["tables"]}
    launched = {r["path"] for r in table if r["launches"] or r["imports"]}
    entries = [(p, inv["tables"], len(inv["tables"] & seen_tables)) for p, inv in rows.items()
               if inv["entry"] and (inv.get("helper", True) or p in launched)]
    discovery = []
    for g in groups.values():
        runs_by = {k: len(v) for k, v in g["runs"].items()}
        bare = [t.rsplit(".", 1)[-1] for t in g["tables"]]
        covered = sorted((width, p not in launched, p) for p, ids, width in entries if all(t in ids for t in bare))
        examples = sorted(g["examples"], key=lambda e: not e["customer"])[:2]
        discovery.append({
            "tables": g["tables"], "customer_runs": runs_by.get("customer", 0), "test_runs": runs_by.get("test", 0),
            "unknown_runs": runs_by.get("unknown", 0), "tenants": len(g["tenants"]), "queries": g["queries"],
            "shapes": len(g["shapes"]), "failed": g["failed"], "slow": g["slow"], "examples": examples,
            "sql": g["sql"][max(g["shapes"], key=g["shapes"].get)], "maybe_covered_by": [c[-1] for c in covered[:3]],
            "score": runs_by.get("customer", 0) * max(1, len(g["tenants"])),
        })
    discovery.sort(key=lambda g: (-g["score"], -g["customer_runs"], -g["queries"]))
    return table, discovery


# --- render ---------------------------------------------------------------------------------
def cell(value: Any) -> str:
    return "" if value in (None, 0, "") else str(value).replace("|", "\\|")


def secs(ms: int | None) -> str:
    return "" if ms is None else f"{ms / 1000:.1f}"


def render(report: dict) -> str:
    c = report["coverage"]
    out = [f"# Helper usage — {report['project']}", "",
           f"Window {c['since']} → {c['until'] or 'now'}{' · tenant ' + report['tenant'] if report['tenant'] else ''}"
           f" · generated {report['generated_at']}{' · ' + report['kit'] if report.get('kit') else ''}", "",
           "| coverage | |", "|---|---|"]
    out += [f"| {k.replace('_', ' ')} | {v} |" for k, v in c.items() if k not in ("since", "until")]
    out += ["", "> [!NOTE]", "> Launch sites from command text: duration belongs to the enclosing bash; its exit "
            "code only to the last launch site or a helper its stderr names. A loop is one site. `help` = bash calls "
            "that only read `--help` (+chained = `x.py --help; x.py …`, no extra turn). Rows rank by customer runs; "
            "test = simulation / prompt / staff domain."]
    for name, n in (c.get("undeclared_mirrors") or {}).items():
        line = (report.get("mirror_hints") or {}).get(name) or f'"{name}" = "<relative path to its checkout>"'
        out += ["", "> [!WARNING]", f"> Runs launched `/mirrors/{name}/…` {n}× but `.replypen.toml` declares no "
                f"such mirror, so its helpers are missing from the inventory. Add under `[mirrors]`: `{line}` "
                f"(or pass `--mirror {name}=PATH`)."]
    if c.get("scrubbed_runs") or c.get("blind_runs"):
        out += ["> Scrubbed runs (older than ~14 d) only keep a privacy-reduced command (⟨vocab⟩ can hide a helper"
                " path); blind runs keep none. Counts are a floor: cache a window while it is fresh, re-run monthly."]
    if report["mirrors"]:
        out += ["> A mirror helper is shared by every project that mounts the mirror: retire only with zero "
                "observations in each consumer's report."]
    head = ("| helper | src | cust runs | tenants | test runs | launches | exit≠0 | exit 2 | tb | "
            "median s (n) | p90 s | help | reads | last seen |")
    seen = [r for r in report["helpers"] if r["launches"] or r["imports"] or r["help_calls"] or r.get("help_chained")
            or r["source_reads"] or r["prestep_reads"]]
    out += ["", "## Helpers", "", head, "|" + "---|" * 14]
    for r in seen:
        name = r["path"].rsplit("/", 1)[-1]
        launches = " + ".join(x for x in (str(r["launches"] or ""), f"{r['imports']} imp" if r["imports"] else "") if x)
        if r["loop_sites"]:
            launches = f"{launches} ({r['loop_sites']} loop)"
        median = f"{secs(r['median_ms'])} ({r['duration_n']})" if r["median_ms"] is not None else ""
        src = "brain" if r["source"] == "brain" else "mirror"
        if r["source"] == "excluded":
            src = "non-helper dir"
        elif r["source"] == "not in inventory":
            src = next((v for k, v in MOUNT_LABEL.items() if r["path"].startswith(k)), "⚠ not in checkout")
        reads = f"{r['source_reads'] or ''} (+{r['prestep_reads']} pre)".lstrip() if r["prestep_reads"] else r["source_reads"]
        chained = r.get("help_chained")
        helps = f"{r['help_calls'] or ''} (+{chained} chained)".lstrip() if chained else r["help_calls"]
        out.append("| " + " | ".join(cell(v) for v in (
            f"`{name}`", src, r["customer_runs"], r["tenants"], r["test_runs"], launches or "",
            r["exit_nonzero"], r["exit_2"], r["traceback_count"], median, secs(r["p90_ms"]),
            helps, reads, (r["last_seen"] or "")[:10])) + " |")
    idle = [r for r in report["helpers"] if r not in seen]
    others = [r for r in idle if not r.get("helper", True)]
    idle = [r for r in idle if r.get("helper", True)]
    if idle or others:
        out += ["", f"<details><summary>Not observed in window: {len(idle)} "
                f"({sum(r['kind'] == 'entry' for r in idle)} entry points)</summary>", ""]
        for r in idle:
            pre = f" · {r['prestep_reads']} pre-step reads" if r["prestep_reads"] else ""
            out.append(f"- `{r['path']}`{' (library)' if r['kind'] == 'library' else ''}{pre}")
        if others:
            out.append(f"- + {len(others)} other mirror .py not shown: outside `skills/`, no `# name:` header")
        out += ["", "</details>"]
    tbs = [(r["path"].rsplit("/", 1)[-1], t) for r in report["helpers"] for t in r["tracebacks"]]
    if tbs:
        out += ["", "<details><summary>Tracebacks</summary>", ""]
        for name, t in tbs[:15]:
            out += [f"- `{name}` [{t['run_id'][:8]}]({report['base_url']}/runs/{t['run_id']}): "
                    f"`{cell(t['tail'][-1])[:200]}`"]
        out += ["", "</details>"]
    out += ["", "## Repeated ad-hoc SQL (new-helper candidates)", "",
            "Grouped by FROM/JOIN table set; score = customer runs × tenants.", ""]
    for g in report["discovery"][:25]:
        if g["customer_runs"] + g["test_runs"] + g["unknown_runs"] < 2:
            break
        flags = [f"{g['failed']} failed" if g["failed"] else "", f"{g['slow']} slow >30s" if g["slow"] else ""]
        covered = (" · maybe covered by " + ", ".join(f"`{p.rsplit('/', 1)[-1]}`" for p in g["maybe_covered_by"])
                   if g["maybe_covered_by"] else "")
        links = " ".join(f"[{e['run_id'][:8]}]({e['url']})" for e in g["examples"])
        out.append(f"- **{', '.join(g['tables'])}** — {g['customer_runs']} cust runs · {g['tenants']} tenants · "
                   f"{g['test_runs']} test · {g['queries']} queries / {g['shapes']} shapes"
                   f"{''.join(' · ' + f for f in flags if f)}{covered} · {links}")
        out.append(f"  `{cell(g['sql'])}`")
    return "\n".join(out) + "\n"


# --- cli ------------------------------------------------------------------------------------
def kit_version() -> str:
    """`kit v0.13.4 (latest v0.13.5 · main +2 unreleased)`: a report made with a stale kit undercounts."""
    kit = Path(__file__).resolve().parents[3]

    def git(*args):
        done = subprocess.run(["git", "-C", str(kit), *args], capture_output=True, text=True, timeout=10)
        return done.stdout.strip() if done.returncode == 0 else ""

    here = git("describe", "--tags") or "?"
    latest = (git("tag", "-l", "v*", "--sort=-v:refname").splitlines() or [""])[0]
    ahead = git("rev-list", "--count", f"{latest}..origin/main") if latest else ""
    extra = [f"latest {latest}"] if latest and latest != here else []
    extra += [f"main +{ahead} unreleased"] if ahead not in ("", "0") else []
    return f"kit {here}" + (f" ({' · '.join(extra)})" if extra else "")


def undeclared_mirrors(helpers: list[dict], mirrors: dict) -> dict[str, int]:
    """{mirror name: launch + help sites} for `/mirrors/<name>/…` paths no `[mirrors]` entry covers."""
    out: dict[str, int] = defaultdict(int)
    for h in helpers:
        parts = h["path"].split("/")
        if h["path"].startswith("/mirrors/") and len(parts) > 3 and parts[2] not in mirrors:
            out[parts[2]] += h["launches"] + h["help_calls"] + h["help_chained"]
    return {n: c for n, c in sorted(out.items()) if c}


def mirror_hint(root: Path, name: str) -> str | None:
    """The `[mirrors]` line, when exactly one sibling checkout `<code>/*/<name>` exists next to the brain's."""
    hits = [p for p in root.parent.parent.glob(f"*/{name}") if (p / ".git").exists()]
    if len(hits) != 1:
        return None
    return f'"{name}" = "{Path("..") / ".." / hits[0].relative_to(root.parent.parent)}"'


def build(args, root: Path) -> dict:
    meta = tomllib.loads((root / ".replypen.toml").read_text()) if (root / ".replypen.toml").exists() else \
        tomllib.loads((root / ".rootcause.toml").read_text()) if (root / ".rootcause.toml").exists() else {}
    project = meta.get("project")
    if not project:
        raise UsageError(f"{root} has no .replypen.toml project: run from a brain checkout")
    base_url = (meta.get("base_url") or "https://app.replypen.com").rstrip("/")
    if args.tenant:
        SCOPE[:] = ["--tenant", args.tenant]
    mirrors = {n: (root / p).resolve() for n, p in (meta.get("mirrors") or {}).items()}
    for pair in args.mirror or []:
        name, sep, path = pair.partition("=")
        if not sep:
            raise UsageError(f"--mirror {pair!r}: expected NAME=PATH")
        mirrors[name] = Path(path).expanduser().resolve()
    rows, missing = inventory(root, mirrors)

    runs, pages = list_runs(args, root)
    listed = len(runs)
    if args.kind:
        runs = [r for r in runs if r.get("kind") in args.kind]
    cache = root / ".replypen" / "helper-usage" / project
    cache.mkdir(parents=True, exist_ok=True)
    todo = [r for r in runs if (r.get("health") or {}).get("bash_total", 1) > 0]
    records, failed = {}, []

    def one(run):
        test = run.get("simulation") or run.get("kind") == "prompt"
        return run["run_id"], load_record(run, cache, root, args.refresh, need_trace=not test)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(one, run): run["run_id"] for run in todo}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                run_id, rec = future.result()
                records[run_id] = rec
            except (UsageError, ValueError) as exc:
                failed.append({"run_id": futures[future], "error": str(exc)[:200]})
            if i % 100 == 0:
                print(f"  {i}/{len(todo)} runs", file=sys.stderr)
    helpers, discovery = aggregate(runs, records, rows, args.staff_domain or [], args.tenant, base_url)
    coverage = {
        "since": args.since, "until": args.until, "pages": pages, "runs_listed": listed,
        "runs_in_kind_filter": len(runs), "simulations": sum(bool(r.get("simulation")) for r in runs),
        "runs_with_bash": len(todo), "events_fetched": len(records), "fetch_failed": len(failed),
        "traces": sum(1 for r in records.values() if r.get("trace")),
        "non_terminal": sum(1 for r in records.values() if not r.get("terminal")),
        "scrubbed_runs": sum(1 for r in records.values() if r.get("scrub", {}).get("kept")),
        "blind_runs": sum(1 for r in records.values() if r.get("scrub", {}).get("blind")),
        "inventory": f"{sum(r['source'] != 'excluded' for r in rows.values())} .py "
                     f"({sum(r['entry'] for r in rows.values())} entry points)",
    }
    if missing:
        coverage["missing_checkouts"] = "; ".join(missing)
    undeclared = undeclared_mirrors(helpers, mirrors)
    if undeclared:
        coverage["undeclared_mirrors"] = undeclared
    return {"schema": f"helper-usage/v{SCHEMA}", "project": project, "tenant": args.tenant, "kit": kit_version(),
            "base_url": base_url, "mirrors": sorted(mirrors),
            "mirror_hints": {n: hint for n in undeclared if (hint := mirror_hint(root, n))},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "staff_domains": args.staff_domain or [], "kinds": args.kind or [], "coverage": coverage,
            "fetch_failures": failed[:50], "helpers": helpers, "discovery": discovery[:200]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usage.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--brain", default=".", help="brain checkout root (default: cwd)")
    p.add_argument("--since", help="RFC3339 or YYYY-MM-DD (UTC)")
    p.add_argument("--until")
    p.add_argument("--tenant")
    p.add_argument("--kind", action="append", choices=("email", "chat", "prompt", "analysis", "mcp", "console"))
    p.add_argument("--staff-domain", action="append", metavar="DOMAIN", help="principal emails here = test cohort")
    p.add_argument("--mirror", action="append", metavar="NAME=PATH", help="add/override a [mirrors] checkout")
    p.add_argument("--refresh", action="store_true", help="ignore the per-run cache")
    p.add_argument("--input", help="re-render this usage.json offline")
    p.add_argument("--out", required=True, help="output directory")
    args = p.parse_args(argv)
    out = Path(args.out)
    try:
        if args.input:
            report = json.loads(Path(args.input).read_text())
        elif not args.since:
            p.error("--since is required (unless --input)")
        else:
            report = build(args, Path(args.brain).resolve())
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / "usage.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    (out / "usage.md").write_text(render(report))
    print(f"{out / 'usage.md'}\n{json.dumps(report['coverage'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
