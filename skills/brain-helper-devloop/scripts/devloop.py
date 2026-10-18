#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""One command from a questions file to a helper-pickup table (brain-helper-devloop steps 4-5).

    devloop.py ask --tag <topic> --questions questions.tsv [--brain-ref dev/x] [--mirror-ref repo=ref]…
                   [--parallel 3] [--tenant <slug>] [--scenario email|raw] [--simulation]
                   [--only q1,q2] [--force]
    devloop.py report --tag <topic> [--helper attest_check.py] [--case skills/cases/attesten.md]
                      [--expect-mirror repo=sha]… [--json]

questions.tsv: tab-separated `id  tenant  question  [as]`, `#` comments and an `id` header row are
skipped. Empty tenant → `--tenant` or the login default. `as` is an audience label from the
scope-check roster (`_internal/scopecheck-<project>.toml`, in the brain or a `[mirrors]` checkout),
or a literal `kind:id`. Keep questions path-free: never name the helper or case file — that is the
thing under test.

ask: `rp ask --no-wait` per question, the run id is persisted at once (`runs/<id>.submit.json`), then
`rp run show` is polled until terminal (`runs/<id>.json`). Re-entrant: a submit file resumes polling,
a finished run is skipped unless `--force`. At most 3 in flight.

report: `rp run debug` per run (skipped when the JSONL exists), then one row per question:
  surfaced  case/helper path in the grounding pre-step (its commands or submit_selection picks)
  opened    case path appears in a main-loop bash command
  called    helper executed by a main-loop bash command (first argv, count, exit, Traceback)
  class     called | opened-not-called | surfaced-not-opened | not-surfaced | no-helper-given
Honesty: these are command-text matches. A path mention is a read ATTEMPT, not a proven read; an
exit code belongs to the whole bash command, not only the helper; `right` stays a human column.

Scratch: <brain>/.replypen/devloop/<tag>/ (gitignored). Stdlib only:
`uv run --no-project python devloop.py …`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scope-check" / "scripts"))
from roster import RosterError, brain_project, resolve_principal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution import helper_calls, is_main_bash, is_prestep  # noqa: F401  (shared launch parser)


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

MAX_PARALLEL = 3
PENDING = {"queued", "running", "pending", "submitted", "starting", ""}
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HEAD = 120


class DevloopError(Exception):
    """Operator-facing failure; main() prints it and exits 2."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def flat(text: Any, limit: int = HEAD) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- questions ------------------------------------------------------------------------------
def parse_questions(text: str) -> list[dict[str, str]]:
    """TSV → [{id, tenant, question, as}]. Raises DevloopError on a malformed or duplicate row."""
    rows, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        if cols[0].lower() == "id" and not rows:
            continue
        if len(cols) < 3 or not cols[2]:
            raise DevloopError(f"questions line {number}: expected id<TAB>tenant<TAB>question[<TAB>as]")
        qid = cols[0]
        if not ID_RE.match(qid):
            raise DevloopError(f"questions line {number}: id {qid!r} must match {ID_RE.pattern}")
        if qid in seen:
            raise DevloopError(f"questions line {number}: duplicate id {qid!r}")
        seen.add(qid)
        rows.append({"id": qid, "tenant": cols[1], "question": cols[2],
                     "as": cols[3] if len(cols) > 3 else ""})
    if not rows:
        raise DevloopError("questions file has no rows")
    return rows


# --- rc -------------------------------------------------------------------------------------
def rc(args: list[str], root: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([_cli(), *args], cwd=str(root), capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise DevloopError("`rp` is not on PATH (brain-dev-upgrade)") from exc
    except subprocess.TimeoutExpired as exc:
        raise DevloopError(f"rp {' '.join(args[:2])} timed out after {timeout}s") from exc


def rc_json(args: list[str], root: Path, timeout: int = 180) -> dict[str, Any]:
    done = rc([*args, "-o", "json", "--raw-output"], root, timeout)
    if done.returncode != 0:
        raise DevloopError(f"rp {args[0]} {args[1]} exited {done.returncode}: "
                           f"{(done.stderr or done.stdout).strip()[-400:]}")
    start = re.search(r"[{\[]", done.stdout or "")
    try:
        return json.loads(done.stdout[start.start():]) if start else {}
    except ValueError as exc:
        raise DevloopError(f"rp {args[0]} {args[1]} did not return JSON") from exc


def require_mirror_ref(root: Path) -> None:
    done = rc(["ask", "--help"], root, 30)
    if "--mirror-ref" not in (done.stdout + done.stderr):
        raise DevloopError("installed rp has no `rp ask --mirror-ref` yet; upgrade rp "
                           "(brain-dev-upgrade) or drop --mirror-ref and refresh the mirror instead")


def ask_command(row: dict[str, str], args: argparse.Namespace, principal: tuple[str, str] | None) -> list[str]:
    cmd = ["ask", row["question"], "--no-wait", "--scenario", args.scenario]
    if args.brain_ref:
        cmd += ["--brain-ref", args.brain_ref]
    tenant = row["tenant"] or args.tenant
    if tenant:
        cmd += ["--tenant", tenant]
    if principal:
        cmd += ["--principal-kind", principal[0], "--principal-id", principal[1]]
    for ref in args.mirror_ref or []:
        cmd += ["--mirror-ref", ref]
    if args.simulation:
        cmd.append("--simulation")
    return cmd


def changed_inputs(submit: dict[str, Any], row: dict[str, str], args: argparse.Namespace) -> list[str]:
    want = {"question": row["question"], "tenant": row["tenant"] or args.tenant or None,
            "as": row["as"] or None, "brain_ref": args.brain_ref, "mirror_ref": args.mirror_ref or [],
            "scenario": args.scenario, "simulation": args.simulation}
    return [key for key, value in want.items() if key in submit and submit[key] != value]


def run_one(row: dict[str, str], args: argparse.Namespace, root: Path, runs: Path,
            principal: tuple[str, str] | None) -> dict[str, Any]:
    submit_path = runs / f"{row['id']}.submit.json"
    if submit_path.exists():
        submit = read_json(submit_path)
    else:
        payload = rc_json(ask_command(row, args, principal), root)
        if not payload.get("run_id"):
            raise DevloopError(f"rp ask returned no run_id: {flat(payload, 300)}")
        submit = {"id": row["id"], "run_id": payload["run_id"], "submitted_at": now_iso(),
                  "tenant": row["tenant"] or args.tenant or None, "as": row["as"] or None,
                  "principal_kind": principal[0] if principal else None,
                  "brain_ref": args.brain_ref, "mirror_ref": args.mirror_ref or [],
                  "scenario": args.scenario, "simulation": args.simulation, "question": row["question"]}
        write_json(submit_path, submit)
    deadline = time.monotonic() + args.wait
    while True:
        detail = rc_json(["run", "show", submit["run_id"]], root)
        if str(detail.get("status") or "").lower() not in PENDING:
            write_json(runs / f"{row['id']}.json", detail)
            return detail
        if time.monotonic() > deadline:
            raise DevloopError(f"run {submit['run_id']} still {detail.get('status')} after "
                               f"{args.wait}s; re-run `ask` to resume polling")
        time.sleep(args.poll)


def cmd_ask(args: argparse.Namespace, root: Path, scratch: Path) -> int:
    rows = parse_questions(Path(args.questions).read_text(encoding="utf-8"))
    only = {i.strip() for arg in args.only or [] for i in arg.split(",") if i.strip()}
    if only - {r["id"] for r in rows}:
        raise DevloopError(f"--only names unknown ids: {', '.join(sorted(only - {r['id'] for r in rows}))}")
    if args.mirror_ref:
        require_mirror_ref(root)
    project = brain_project(root) or ""
    principals = {}
    for row in rows:
        if row["as"]:
            try:
                principals[row["id"]] = resolve_principal(row["as"], root, project)
            except RosterError as exc:
                raise DevloopError(f"{row['id']}: {exc}") from exc
    runs = scratch / "runs"
    forced = {row["id"] for row in rows if args.force and (not only or row["id"] in only)}
    for row in rows:  # questions.json is rewritten whole: no kept run may meet a changed row
        submit = runs / f"{row['id']}.submit.json"
        if row["id"] not in forced and submit.exists():
            changed = changed_inputs(read_json(submit), row, args)
            if changed:
                raise DevloopError(f"{row['id']}: {', '.join(changed)} changed since its run; "
                                   "use a new --tag, or --force to re-ask")
    todo = []
    for row in rows:
        if only and row["id"] not in only:
            continue
        final = runs / f"{row['id']}.json"
        if row["id"] in forced:
            final.unlink(missing_ok=True)
            (runs / f"{row['id']}.submit.json").unlink(missing_ok=True)
        if not final.exists():
            todo.append(row)
    write_json(scratch / "questions.json", rows)
    print(f"{len(todo)} to ask/poll, {len(rows) - len(todo)} done or filtered; scratch {scratch}",
          file=sys.stderr)
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.parallel, MAX_PARALLEL))) as pool:
        futures = {pool.submit(run_one, row, args, root, runs, principals.get(row["id"])): row
                   for row in todo}
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            try:
                detail = future.result()
            except DevloopError as exc:
                failed += 1
                print(f"  {row['id']} FAILED: {exc}", file=sys.stderr)
                continue
            print(f"  {row['id']} {detail.get('status')} turns={detail.get('turns')} "
                  f"{detail.get('run_url') or ''}", file=sys.stderr)
    print(f"next: devloop.py report --tag {args.tag} --helper <script> --case <case file>",
          file=sys.stderr)
    return 1 if failed else 0


# --- trace analysis -------------------------------------------------------------------------
def load_trace(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise DevloopError(f"{path} is empty")
    return lines[0], lines[1:]


def mentions(text: str, fragment: str | None) -> bool:
    return bool(fragment) and fragment in (text or "")


def analyse(header: dict[str, Any], events: list[dict[str, Any]], helper: str | None,
            case: str | None, expect: dict[str, str]) -> dict[str, Any]:
    targets = [f for f in (case, helper) if f]
    surfaced = False
    for ev in events:
        if not is_prestep(ev):
            continue
        selected = ((ev.get("args") or {}).get("selected") or []) if ev.get("tool") == "submit_selection" else []
        hay = [ev.get("command") or ""] + [str(s.get("path") or "") for s in selected if isinstance(s, dict)]
        if any(mentions(h, f) for h in hay for f in targets):
            surfaced = True
            break
    main = [ev for ev in events if is_main_bash(ev)]
    opened = any(mentions(ev.get("command"), case) for ev in main) if case else None
    calls, first = 0, None
    if helper:
        for ev in main:
            found = helper_calls(ev.get("command") or "", helper)
            if found and first is None:
                out = f"{ev.get('stdout') or ''}\n{ev.get('stderr') or ''}"
                first = {"argv": found[0], "exit": ev.get("exit_code"), "traceback": "Traceback" in out,
                         "disp": ev.get("disp")}
            calls += len(found)
    if not helper:
        klass = "no-helper-given"
    elif calls:
        klass = "called"
    elif opened:
        klass = "opened-not-called"
    elif surfaced:
        klass = "surfaced-not-opened"
    else:
        klass = "not-surfaced"
    mirrors = {s.get("name"): (s.get("commit_sha") or "")[:12]
               for s in (header.get("grounding_sources") or {}).get("sources") or []
               if s.get("kind") == "mirror"}
    mirror_check = {}
    for repo, sha in expect.items():
        got = mirrors.get(repo) or ""
        n = min(len(got), len(sha))
        mirror_check[repo] = {"expected": sha, "got": got or None,
                              "ok": bool(got) and n >= 7 and got[:n] == sha[:n]}
    answer = header.get("draft") or header.get("answer_markdown") or ""
    return {"class": klass, "surfaced": surfaced, "opened": opened, "called": calls,
            "first_call": first, "brain": header.get("brain_resolved") or header.get("brain_ref"),
            "mirrors": mirrors, "mirror_check": mirror_check, "answer_head": flat(answer),
            "status": header.get("status")}


# --- report ---------------------------------------------------------------------------------
def debug_trace(run_id: str, root: Path) -> Path:
    debug = root / ".replypen" / "debug"
    found = sorted(debug.glob(f"{run_id[:8]}-*.jsonl"))
    if found:
        return found[0]
    done = rc(["run", "debug", run_id, "--raw-output"], root, 300)
    paths = [line.strip() for line in done.stdout.splitlines() if line.strip().endswith(".jsonl")]
    if done.returncode != 0 or not paths:
        raise DevloopError(f"rp run debug {run_id} exited {done.returncode}: "
                           f"{(done.stderr or done.stdout).strip()[-300:]}")
    path = Path(paths[0])
    return path if path.is_absolute() else root / path


def parse_pairs(values: list[str] | None, flag: str) -> dict[str, str]:
    out = {}
    for value in values or []:
        repo, sep, ref = value.partition("=")
        if not sep or not repo.strip() or not ref.strip():
            raise DevloopError(f"{flag} {value!r}: expected repo=sha")
        out[repo.strip()] = ref.strip()
    return out


def yes(value: bool | None) -> str:
    return "-" if value is None else ("✓" if value else "✗")


def cell(text: Any) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|")


def render_markdown(rows: list[dict[str, Any]], expect: dict[str, str]) -> str:
    out = ["| id | class | surfaced | opened | called | args | exit | turns | brain | mirror | right | answer head |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        if row.get("error"):
            out.append(f"| {cell(row['id'])} | {cell(row['error'])} |" + " |" * 10)
            continue
        first = row.get("first_call") or {}
        args = " ".join(first.get("argv") or []) if first else ""
        exit_ = "" if not first else f"{first.get('exit')}{' Traceback' if first.get('traceback') else ''}"
        called = f"{row['called']}×" if row["called"] else ("✗" if row["class"] != "no-helper-given" else "-")
        if expect:
            mirror = " ".join(f"{repo}={c['got'] or 'absent'} {yes(c['ok'])}"
                              for repo, c in row["mirror_check"].items())
        else:
            mirror = " ".join(f"{n}={s}" for n, s in row["mirrors"].items())
        out.append("| " + " | ".join(cell(v) for v in (
            row["id"], row["class"], yes(row["surfaced"]), yes(row["opened"]), called,
            f"`{args}`" if args else "", exit_, row.get("turns"), row["brain"], mirror, " ",
            row["answer_head"])) + " |")
    return "\n".join(out)


def summary(rows: list[dict[str, Any]], expect: dict[str, str]) -> str:
    ok = [r for r in rows if not r.get("error")]
    m = len(ok)
    parts = [f"called {sum(1 for r in ok if r['called'])}/{m}",
             f"opened {sum(1 for r in ok if r['opened'])}/{m}"]
    if expect:
        parts.append(f"mirror ✓ {sum(1 for r in ok if all(c['ok'] for c in r['mirror_check'].values()))}/{m}")
    if len(rows) != m:
        parts.append(f"{len(rows) - m} not reportable")
    return ", ".join(parts)


def cmd_report(args: argparse.Namespace, root: Path, scratch: Path) -> int:
    questions_path = scratch / "questions.json"
    if not questions_path.exists():
        raise DevloopError(f"no {questions_path}; run `devloop.py ask --tag {args.tag}` first")
    expect = parse_pairs(args.expect_mirror, "--expect-mirror")
    rows = []
    for q in read_json(questions_path):
        runs = scratch / "runs"
        submit, final = runs / f"{q['id']}.submit.json", runs / f"{q['id']}.json"
        base = {"id": q["id"], "as": q.get("as") or None, "tenant": q.get("tenant") or None}
        if not submit.exists():
            rows.append({**base, "error": "not asked"})
            continue
        run_id = read_json(submit)["run_id"]
        if not final.exists():
            rows.append({**base, "run_id": run_id, "error": "still running (re-run ask)"})
            continue
        detail = read_json(final)
        try:
            header, events = load_trace(debug_trace(run_id, root))
        except DevloopError as exc:
            rows.append({**base, "run_id": run_id, "error": str(exc)})
            continue
        row = {**base, "run_id": run_id, "run_url": detail.get("run_url"),
               "turns": detail.get("turns") or (header.get("metadata") or {}).get("iterations"),
               **analyse(header, events, args.helper, args.case, expect)}
        if not row["answer_head"]:
            row["answer_head"] = flat(detail.get("draft_markdown") or detail.get("answer_markdown"))
        rows.append(row)
    if args.json:
        print(json.dumps({"rows": rows, "summary": summary(rows, expect)}, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(rows, expect))
        print(f"\n{summary(rows, expect)}")
    return 0


# --- cli ------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devloop.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brain", default=".", help="brain checkout root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="submit questions with rp ask --no-wait and poll to terminal")
    ask.add_argument("--tag", required=True)
    ask.add_argument("--questions", required=True, help="TSV: id, tenant, question[, as]")
    ask.add_argument("--brain-ref", help="dev/<branch> (default: the project's main channel)")
    ask.add_argument("--mirror-ref", action="append", metavar="REPO=REF",
                     help="passed to rp ask (needs an rp that lists --mirror-ref)")
    ask.add_argument("--parallel", type=int, default=3, help=f"in flight, capped at {MAX_PARALLEL}")
    ask.add_argument("--tenant", help="tenant for rows with an empty tenant column")
    ask.add_argument("--scenario", choices=("email", "raw"), default="email")
    ask.add_argument("--simulation", action="store_true", help="rp ask --simulation (no actions/journal)")
    ask.add_argument("--only", action="append", metavar="ID[,ID]")
    ask.add_argument("--force", action="store_true", help="re-ask finished questions")
    ask.add_argument("--wait", type=int, default=1200, help="max seconds to poll one run (default 1200)")
    ask.add_argument("--poll", type=int, default=10, help="seconds between polls (default 10)")
    report = sub.add_parser(
        "report", help="pickup table from the run traces",
        description="Pickup table from the run traces. Matches are on command text: a path mention "
                    "is a read ATTEMPT, not a proven read; `exit` belongs to the whole bash command; "
                    "`right` is left blank for the human reviewer.")
    report.add_argument("--tag", required=True)
    report.add_argument("--helper", help="helper script basename or path fragment")
    report.add_argument("--case", help="case/SKILL file path fragment")
    report.add_argument("--expect-mirror", action="append", metavar="REPO=SHA")
    report.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.brain).resolve()
    scratch = root / ".replypen" / "devloop" / args.tag
    try:
        if args.command == "ask":
            return cmd_ask(args, root, scratch)
        return cmd_report(args, root, scratch)
    except DevloopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
