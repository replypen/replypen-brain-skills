#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Old-vs-new comparison of real replayed runs: N reps per arm, blind swapped judge packs, null-calibrated verdict.

Method and decision rule: docs/ab-eval.md. Public `rp` only; every replay is `rp ask --replay-run` (a
simulation: no action executes, nothing is placed). Real customer content stays in one gitignored scratch dir.

    ab.py run     --scratch S --cases ids.txt --arm old= --arm 'new=--brain-ref dev/x' [--reps 2] [--parallel 4]
    ab.py collect --scratch S                     # each replay's trace (question, context, SQL errors) + cost/tokens (global admin)
    ab.py metrics --scratch S --a old --b new     # deterministic gates, within-arm vs across-arm
    ab.py pack    --scratch S --a old --b new     # S/cmp-old-new/: pack-1.md + pack-2.md (labels swapped), key.json
    ab.py verdict --scratch S --a old --b new [--null NULL_SCRATCH]

`ids.txt`: one run id per line, optional `,stratum`. A-vs-A null test: two arms with the same flags
(`--arm a1= --arm a2=`), or `--a old --b old` to pair reps inside one arm. Every step is re-entrant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from math import comb
from pathlib import Path
from typing import Any

DONE = "done"
TERMINAL = {"done", "failed", "error", "cancelled", "canceled", "blocked", "skipped"}
# A real database error in a lib.db call's output; a doc that merely *mentions* "UndefinedTable" must not count.
SQL_CALL = re.compile(r"lib\.db|from lib import db|db\.query")
SQL_ERR = re.compile(r"psycopg[\w.]*\.errors\.\w+|ProgrammingError|UndefinedColumn|syntax error at or near|"
                     r"column \"[^\"]+\" does not exist|relation \"[^\"]+\" does not exist")
CONTEXT_MESSAGES = 6          # prior messages shown to the judge (the turn itself is always shown in full)
CASE_WIN = 0.5                # |mean preference| at or above this marks a case as won by one arm
# Planted known-worse copies of real pairs: a judge that misses these is too lenient to trust its "same".
LEAK = "\n\n(Bron: tabel `subscription_line_items`, opgehaald met skills/records/query.py.)"


def plant(v: dict[str, Any], kind: str) -> dict[str, Any]:
    answer = v["answer"]
    if kind == "truncate":   # the asker loses most of the answer: a missed-question fault
        cut = answer[: max(80, int(len(answer) * 0.35))]
        answer = cut[: max(cut.rfind("\n"), cut.rfind(". ") + 1, 80)].rstrip()
    else:                    # internals leak
        answer = answer + LEAK
    return {**v, "answer": answer, "run_id": v["run_id"] + f"+{kind}"}


class ABError(Exception):
    pass


def cli() -> str:
    return "rp" if shutil.which("rp") else "rc"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def guard_scratch(scratch: Path) -> None:
    """Refuse a scratch dir git could stage: replays carry real customer content."""
    scratch.mkdir(parents=True, exist_ok=True)
    os.chmod(scratch, 0o700)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=scratch, text=True, capture_output=True)
    if top.returncode != 0:
        return
    if subprocess.run(["git", "check-ignore", "-q", str(scratch / ".ab-probe")], cwd=scratch).returncode != 0:
        raise ABError(f"refusing stageable scratch {scratch}: add it (e.g. '.replypen/') to .gitignore first")


# --- run ----------------------------------------------------------------------------------------

def parse_arms(specs: list[str]) -> dict[str, list[str]]:
    arms = {}
    for spec in specs:
        name, sep, flags = spec.partition("=")
        if not sep or not re.fullmatch(r"[\w.-]+", name):
            raise ABError(f"--arm wants NAME=FLAGS (FLAGS may be empty), got {spec!r}")
        arms[name] = shlex.split(flags)
    return arms


def load_cases(path: Path) -> list[tuple[str, str]]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            run_id, _, stratum = line.partition(",")
            cases.append((run_id.strip(), stratum.strip() or "-"))
    return cases


def rp_json(cmd: list[str]) -> tuple[Any, str]:
    done = subprocess.run([cli(), *cmd, "-o", "json", "--raw-output"], capture_output=True, text=True)
    try:
        return json.loads(done.stdout), done.stderr.strip()[-400:]
    except json.JSONDecodeError:
        return None, (done.stderr or done.stdout).strip()[-400:]


def submit(jobs: list[dict[str, Any]], flags: list[str]) -> None:
    """One `rp ask --replay-run a,b,… --no-wait` for jobs sharing an arm's flags; fills run_id or error."""
    rows, err = rp_json(["ask", "--replay-run", ",".join(j["case"] for j in jobs), *flags, "--no-wait"])
    if isinstance(rows, dict) and len(jobs) == 1:   # one id is a plain ask: its 202 body, not a batch array
        rows = [rows]
    rows = rows if isinstance(rows, list) and len(rows) == len(jobs) else [{} for _ in jobs]
    for job, row in zip(jobs, rows):
        job["attempts"] = job.get("attempts", 0) + 1
        job["run_id"], job["error"] = row.get("run_id"), row.get("error") or ("" if row.get("run_id") else err)
        job["submitted"] = time.monotonic()


def poll_status(body: dict[str, Any]) -> str:
    """A run's status from `rp run wait --once`; "" when the CLI could not read it (unknown, not failed)."""
    return "" if set(body) == {"run_id", "status", "error"} else body.get("status") or ""


def cmd_run(args: argparse.Namespace) -> int:
    scratch, arms = Path(args.scratch), parse_arms(args.arm)
    guard_scratch(scratch)
    ledger = scratch / "runs.jsonl"
    project = ["--project", args.project] if args.project else []
    done = [r for r in read_jsonl(ledger) if r.get("status") == DONE]
    for r in done:   # an arm name is bound to its flags: reusing it for another config would mix two arms
        if r["arm"] in arms and r.get("flags") != arms[r["arm"]] + project:
            raise ABError(f"arm {r['arm']!r} already ran with flags {r.get('flags')}; "
                          f"use a new arm name (or a new --scratch) for {arms[r['arm']] + project}")
    have = {(r["case"], r["arm"], r["rep"]) for r in done}
    queue = [{"case": c, "stratum": s, "arm": a, "rep": rep, "flags": arms[a] + project}
             for c, s in load_cases(Path(args.cases)) for a in arms for rep in range(args.reps) if (c, a, rep) not in have]
    random.Random(args.seed).shuffle(queue)   # interleave arms in time: host load / data drift hit both alike
    print(f"{len(queue)} replays to run ({len(have)} already done), at most {args.parallel} in flight", flush=True)
    timeout = parse_duration(args.timeout)
    inflight: list[dict[str, Any]] = []
    stray: dict[str, float] = {}   # timed out locally, maybe still running on the host: holds a slot until terminal
    last_poll = time.monotonic()
    failed = 0
    with ledger.open("a", encoding="utf-8") as out:
        def finish(job: dict[str, Any], status: str, error: str = "") -> None:
            nonlocal failed
            if status not in (DONE, "timeout") and job["attempts"] <= args.retries:
                queue.append(job)   # a redeploy or worker crash: replay the same job again
                return
            failed += status != DONE
            rec = {k: job[k] for k in ("case", "stratum", "arm", "rep", "flags", "attempts", "run_id")}
            out.write(json.dumps({**rec, "status": status, "error": error}) + "\n")
            out.flush()
            print(f"{job['arm']:>8} rep{job['rep']} {job['case'][:8]} {status} {error[:120]}", flush=True)

        while queue or inflight:
            # Fill free slots, one batch submit per arm (Replays queue behind live mail/chat on the host, but
            # the box has few workers: the in-flight cap keeps slots free for real customers.)
            free, batch = args.parallel - len(inflight) - len(stray), {}
            while free > 0 and queue:
                job = queue.pop(0)
                batch.setdefault(job["arm"], []).append(job)
                free -= 1
            for arm, jobs in batch.items():
                submit(jobs, jobs[0]["flags"])
                for job in jobs:
                    (inflight.append(job) if job["run_id"] else finish(job, "error", job["error"]))
            if not inflight and not stray:
                continue
            time.sleep(args.poll)
            bodies, err = rp_json(["run", "wait", "--once", *[j["run_id"] for j in inflight], *stray])
            if not isinstance(bodies, list):
                if time.monotonic() - last_poll > timeout:
                    raise ABError(f"no successful status poll for {args.timeout}; rerun to resume: {err}")
                print(f"poll failed (retrying): {err}", file=sys.stderr, flush=True)
                continue
            last_poll = time.monotonic()
            by_id = {b.get("run_id"): b for b in bodies if isinstance(b, dict)}
            now = time.monotonic()   # an unreadable stray frees its slot after one more --timeout
            stray = {i: t for i, t in stray.items() if poll_status(by_id.get(i) or {}) not in TERMINAL and now - t < timeout}
            for job in list(inflight):
                body = by_id.get(job["run_id"]) or {}
                status = poll_status(body)
                if status == DONE:
                    write_private(scratch / "runs" / f"{job['run_id']}.json", json.dumps(body, ensure_ascii=False))
                elif status in ("", "queued", "running", "pending"):
                    if time.monotonic() - job["submitted"] < timeout:
                        continue
                    stray[job["run_id"]] = time.monotonic()   # not retried: a second copy would only add host load
                inflight.remove(job)
                finish(job, status if status in TERMINAL else "timeout", body.get("error") or "")
    os.chmod(ledger, 0o600)
    print(f"{failed} replay(s) not done; rerun the same command to retry them" if failed else "all replays done")
    return 1 if failed else 0


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"(\d+)([smh])", text.strip())
    if not m:
        raise ABError(f"--timeout wants e.g. 15m, got {text!r}")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def done_runs(scratch: Path) -> list[dict[str, Any]]:
    latest = {}
    for r in read_jsonl(scratch / "runs.jsonl"):
        if r.get("status") == DONE:
            latest[(r["case"], r["arm"], r["rep"])] = r
    return list(latest.values())


# --- collect ------------------------------------------------------------------------------------

def cmd_collect(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch)
    project = ["--project", args.project] if args.project else []
    missing = 0
    for r in done_runs(scratch):
        path = scratch / "traces" / f"{r['run_id']}.jsonl"
        if path.exists() and not args.force:
            continue
        got = subprocess.run([cli(), "run", "trace", r["run_id"], *project, "-o", "json", "--raw-output"],
                             capture_output=True, text=True)
        if got.returncode != 0 or not got.stdout.strip():
            missing += 1
            print(f"trace {r['run_id']}: {got.stderr.strip()[-200:]}", file=sys.stderr)
            continue
        write_private(path, got.stdout)
    print(f"traces collected; {missing} missing")
    collect_usage(scratch, args.project)
    return 1 if missing else 0


def collect_usage(scratch: Path, project: str | None) -> None:
    """Cost + tokens per replay into usage.json. Global-admin only (our unit economics); others skip it."""
    path = scratch / "usage.json"
    usage = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    by_project: dict[str, list[str]] = defaultdict(list)
    for r in done_runs(scratch):
        if r["run_id"] in usage:
            continue
        run = next((x for x in read_jsonl(scratch / "traces" / f"{r['run_id']}.jsonl") if x.get("type") == "run"), {})
        if project or run.get("project"):
            by_project[project or run["project"]].append(r["run_id"])
    for proj, ids in by_project.items():
        for i in range(0, len(ids), 200):
            got, err = rp_json(["admin", "project", "run-usage", proj, *ids[i:i + 200]])
            if not isinstance(got, dict):
                print(f"cost/tokens skipped ({err[-160:]}): needs a human global-admin `rp` login", file=sys.stderr)
                return
            usage.update({u["run_id"]: u for u in got.get("items") or []})
    write_private(path, json.dumps(usage))


def view(scratch: Path, r: dict[str, Any]) -> dict[str, Any]:
    """One replay as the judge and the gates see it: ask payload + trace, nothing truncated."""
    payload = json.loads((scratch / "runs" / f"{r['run_id']}.json").read_text(encoding="utf-8"))
    lines = read_jsonl(scratch / "traces" / f"{r['run_id']}.jsonl")
    run = next((x for x in lines if x.get("type") == "run"), {})
    events = [x for x in lines if x.get("type") != "run"]
    sql = [e for e in events if e.get("tool") == "bash" and SQL_CALL.search(e.get("command") or json.dumps(e.get("args") or {}))]
    cards, preview = [], {}
    for e in events:   # a chat action card = an `action` event; its preflight preview carries counts/amounts
        a = e.get("args") or {}
        if e.get("tool") == "preflight":
            try:
                preview[a.get("action_id")] = str(json.loads(e.get("stdout") or "{}").get("summary") or "").split("\n")[0]
            except (json.JSONDecodeError, AttributeError):
                preview[a.get("action_id")] = ""
        elif e.get("tool") == "action":
            cards.append(f"{a.get('action_id')}: {a.get('intent', '')}" + (f" [preview: {preview.get(a.get('action_id'))}]"
                                                                            if preview.get(a.get("action_id")) else ""))
    actions = sorted(a.get("slug") or "?" for a in payload.get("proposed_actions") or [])
    notes = payload.get("notes") or []
    return {
        **r, "traced": bool(run),
        "question": run.get("question") or "", "prior": run.get("prior_messages") or [],
        "answer": run.get("answer_markdown") or payload.get("draft_markdown") or "",
        "outcome": payload.get("outcome") or (run.get("metadata") or {}).get("outcome"),
        "notes": [n.get("body_markdown") or "" for n in notes if isinstance(n, dict)] if isinstance(notes, list) else [],
        "actions": actions, "cards": cards,
        "action_detail": [f"{a.get('slug')}: {a.get('label') or ''} {a.get('description') or ''}".strip()
                          for a in payload.get("proposed_actions") or []],
        "bash": payload.get("bash_total"), "turns": payload.get("turns"),
        "sys_chars": len(run.get("system_prompt") or ""), "boot_chars": len(run.get("bootstrap_turn") or ""),
        "sql_calls": len(sql),
        "sql_errs": sum(bool(SQL_ERR.search((e.get("stdout") or "") + (e.get("stderr") or ""))) for e in sql),
        "run_url": payload.get("run_url") or (payload.get("metadata") or {}).get("run_url"),
        "fingerprint": f"brain {run.get('brain_resolved') or '?'} · tenant {run.get('tenant_brain_resolved') or '-'}",
    }


def by_case(scratch: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    path = scratch / "usage.json"
    usage = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for r in sorted(done_runs(scratch), key=lambda x: (x["case"], x["arm"], x["rep"])):
        out[r["case"]][r["arm"]].append({**view(scratch, r), "usage": usage.get(r["run_id"])})
    return out


# --- metrics ------------------------------------------------------------------------------------

def signature(v: dict[str, Any]) -> tuple:
    return (v["outcome"], bool(v["answer"]), tuple(v["actions"]), len(v["cards"]) > 0)


def cmd_metrics(args: argparse.Namespace) -> int:
    cases = by_case(Path(args.scratch))
    arms = (args.a, args.b)
    for a in arms:   # an arm must be ONE configuration: main moving mid-experiment silently mixes two
        prints = Counter(v["fingerprint"].split(" · ")[0] for c in cases.values() for v in c.get(a, []))
        print(f"arm {a}: {dict(prints)}" + ("   ⚠ more than one brain commit in this arm" if len(prints) > 1 else ""))
    missing = sorted(cid for cid, runs in cases.items() if not all(runs.get(a) for a in arms))
    if missing:
        print(f"excluded (an arm has no done replay): {len(missing)} case(s): {','.join(c[:8] for c in missing)}")
    print(f"{'metric':32} {args.a:>12} {args.b:>12}")

    def col(f, how="median", fmt=".0f"):
        vals = [[f(v) for c in cases.values() for v in c.get(a, []) if f(v) is not None] for a in arms]
        if how == "sum":
            return [f"{sum(v):{fmt}}" if v else "-" for v in vals]
        return [f"{statistics.median(v):{fmt}}" if v else "-" for v in vals]

    def use(key):
        return lambda v: (v.get("usage") or {}).get(key)

    for name, f, how in [("replays", lambda v: 1, "sum"), ("system prompt chars", lambda v: v["sys_chars"], "median"),
                         ("bootstrap chars", lambda v: v["boot_chars"], "median"), ("bash calls", lambda v: v["bash"], "median"),
                         ("loop turns", lambda v: v["turns"], "median"), ("answer chars", lambda v: len(v["answer"]), "median"),
                         ("sql calls", lambda v: v["sql_calls"], "sum"), ("real sql errors", lambda v: v["sql_errs"], "sum"),
                         ("action proposals", lambda v: len(v["actions"]) + len(v["cards"]), "sum"),
                         ("no answer", lambda v: int(not v["answer"]), "sum")]:
        a, b = col(f, how)
        print(f"{name + ' (' + how + ')':32} {a:>12} {b:>12}")
    # Cost/tokens only when `collect` could read them (global admin); internal unit economics, never customer copy.
    if any(v.get("usage") for c in cases.values() for runs in c.values() for v in runs):
        for name, f, how, fmt in [("cost usd", use("cost_usd"), "median", ".4f"), ("cost usd", use("cost_usd"), "sum", ".3f"),
                                  ("prompt tokens", use("prompt_tokens"), "median", ".0f"),
                                  ("completion tokens", use("completion_tokens"), "median", ".0f"),
                                  ("total tokens", use("total_tokens"), "sum", ".0f")]:
            a, b = col(f, how, fmt)
            print(f"{name + ' (' + how + ')':32} {a:>12} {b:>12}")
    else:
        print("cost/tokens: not collected (`collect` needs a human global-admin login)")
    # A gate difference only means something when it exceeds what the same arm does twice.
    within = Counter()
    across = Counter()
    flips = []
    for cid, arm_runs in cases.items():
        for a in arms:
            sigs = {signature(v) for v in arm_runs.get(a, [])}
            if len(arm_runs.get(a, [])) > 1:
                within["unstable" if len(sigs) > 1 else "stable"] += 1
        sa, sb = ({signature(v) for v in arm_runs.get(a, [])} for a in arms)
        if sa and sb:
            across["disjoint" if not sa & sb else "overlap"] += 1
            if not sa & sb:
                flips.append(f"  {cid[:8]} {args.a}: {sorted(sa)} | {args.b}: {sorted(sb)}")
    print(f"\noutcome/answer/actions signature  within-arm {dict(within) or 'n/a (1 rep)'}  across arms {dict(across)}")
    print("cases whose arms never share a signature (inspect these):" if flips else "no case flips signature across arms")
    print("\n".join(flips))
    return 0


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def cmp_dir(args: argparse.Namespace) -> Path:
    """One comparison = one folder: packs, key, judge files and verdict never collide across comparisons."""
    return Path(args.scratch) / f"cmp-{args.a}-{args.b}"


# --- pack ---------------------------------------------------------------------------------------

def pairs_for(arm_runs: dict[str, list], a: str, b: str, rnd: random.Random) -> list[tuple[dict, dict]]:
    if a == b:   # within-arm null: rep 0 vs 1, 2 vs 3, ...
        reps = arm_runs.get(a, [])
        return [(reps[i], reps[i + 1]) for i in range(0, len(reps) - 1, 2)]
    ra, rb = list(arm_runs.get(a, [])), list(arm_runs.get(b, []))
    rnd.shuffle(ra)
    rnd.shuffle(rb)   # rep numbers carry no meaning: pair at random, judge each pair in both orders
    return list(zip(ra, rb))


def render_output(v: dict[str, Any]) -> str:
    parts = []
    if v["answer"]:
        parts.append("ANSWER:\n" + v["answer"])
    else:
        parts.append(f"(no answer text; outcome: {v['outcome']})")
    parts += ["ACTION CARD (shown to the user to confirm; runs only after their click): " + c for c in v["cards"]]
    parts += ["PROPOSED ACTION (for the reviewer): " + a for a in v["action_detail"]]
    parts += ["INTERNAL NOTE (reviewer only, not shown to the customer):\n" + n for n in v["notes"] if n]
    return "\n".join(parts)


def render_turn(v: dict[str, Any]) -> str:
    prior = v["prior"][-CONTEXT_MESSAGES:]
    lines = []
    if len(v["prior"]) > len(prior):
        lines.append(f"({len(v['prior']) - len(prior)} earlier messages omitted)")
    for m in prior:
        who = "CUSTOMER/USER" if m.get("is_inbound") else "ASSISTANT/STAFF"
        lines.append(f"[{who}] {m.get('body') or ''}")
    lines.append("[THIS TURN] " + (v["question"] or "(not stored)"))
    return "\n\n".join(lines)


def cmd_pack(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch)
    rnd = random.Random(args.seed)
    cases = by_case(scratch)
    untraced = [v["run_id"] for c in cases.values() for runs in c.values() for v in runs if not v["traced"]]
    if untraced:
        raise ABError(f"{len(untraced)} replays have no trace yet: run `ab.py collect` first")
    items = []   # (pair id, case, run A, run B, plant)
    for cid, arm_runs in cases.items():
        for ra, rb in pairs_for(arm_runs, args.a, args.b, rnd):
            items.append((f"P{len(items) + 1:03d}", cid, ra, rb, None))
    eligible = [i for i in items if len(i[2]["answer"]) > 200 and len(i[3]["answer"]) > 200]
    for n, (_, cid, ra, rb, _) in enumerate(rnd.sample(eligible, min(args.plants, len(eligible)))):
        kind, side, base = ("truncate", "leak")[n % 2], rnd.choice("ab"), rnd.choice((ra, rb))
        # one output against its own degraded copy: the intact side is better by construction
        ra, rb = (plant(base, kind), base) if side == "a" else (base, plant(base, kind))
        items.append((f"P{len(items) + 1:03d}", cid, ra, rb, {"kind": kind, "degraded": side}))
    stale = sorted(cmp_dir(args).glob("judge-*.jsonl"))
    if stale and not args.force:
        raise ABError(f"{len(stale)} judge file(s) in {cmp_dir(args)} belong to the current packs; a new pack would "
                      "re-map their X/Y. Move that folder away (keep its verdict) or pass --force to delete them")
    for path in stale:
        path.unlink()
    skipped = sorted(cid for cid in cases if not any(i[1] == cid for i in items))
    if skipped:
        print(f"not packed (missing arm/rep, report them): {','.join(c[:8] for c in skipped)}")
    key, digests = {}, {}
    for pack_no in (1, 2):
        order = items[:]
        rnd.shuffle(order)
        out = [f"# Blind pair pack {pack_no}\n\nEach pair: one real turn and two outputs X and Y from two configurations. "
               "Judge every pair independently.\n"]
        for pid, cid, ra, rb, planted in order:
            if pack_no == 1:
                a_is_x = rnd.random() < 0.5
                key[pid] = {"case": cid, "stratum": ra["stratum"], "a_run": ra["run_id"], "b_run": rb["run_id"],
                            "pack1_x": "a" if a_is_x else "b", "plant": planted}
            a_is_x = (key[pid]["pack1_x"] == "a") == (pack_no == 1)   # pack 2 swaps every pair
            x, y = (ra, rb) if a_is_x else (rb, ra)
            out.append(f"\n## Pair {pid}\n\n### The turn\n{render_turn(ra)}\n\n### Output X\n{render_output(x)}\n\n"
                       f"### Output Y\n{render_output(y)}\n")
        write_private(cmp_dir(args) / f"pack-{pack_no}.md", "\n".join(out))
        digests[str(pack_no)] = digest(cmp_dir(args) / f"pack-{pack_no}.md")
    write_private(cmp_dir(args) / "key.json",
                  json.dumps({"a": args.a, "b": args.b, "packs": digests, "pairs": key}, indent=1))
    print(f"{len(items)} pairs ({min(args.plants, len(eligible))} planted) over {len(cases)} cases → {cmp_dir(args)}/pack-1.md, pack-2.md (every pair, labels swapped).\n"
          f"Judge each pack in a FRESH session with templates/ab-judge-prompt.md; write judge-<judge>-<pack>.jsonl.")
    return 0


# --- verdict ------------------------------------------------------------------------------------

def sign_test(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def bootstrap_ci(values: list[float], seed: int, n: int = 5000, level: float = 0.90) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    means = sorted(statistics.fmean(rnd.choices(values, k=len(values))) for _ in range(n))
    lo = int((1 - level) / 2 * n)
    return (means[lo], means[n - 1 - lo])


def load_judgments(cmp: Path, key: dict[str, Any], judges: list[str] | None = None) -> list[dict[str, Any]]:
    """Every judge file must cover every pair of its pack exactly once, and the pack must be the one judged."""
    for n, want in (key.get("packs") or {}).items():
        if digest(cmp / f"pack-{n}.md") != want:
            raise ABError(f"pack-{n}.md changed since key.json was written: its judgments cannot be unblinded")
    out = []
    for path in sorted(cmp.glob("judge-*-[12].jsonl")):
        judge, pack_no = path.stem[len("judge-"):].rsplit("-", 1)
        if judges and judge not in judges:
            continue
        rows = read_jsonl(path)
        ids = [str(j.get("pair")) for j in rows]
        missing, dupes = set(key["pairs"]) - set(ids), {i for i in ids if ids.count(i) > 1}
        if missing or dupes:
            raise ABError(f"{path.name}: {len(missing)} pair(s) missing, {len(dupes)} judged twice; "
                          "a partial judge file biases the verdict: rerun that judge on the whole pack")
        for j in rows:
            pid = str(j.get("pair"))
            if pid not in key["pairs"] or j.get("better") not in ("X", "Y", "same"):
                raise ABError(f"{path.name}: bad line {j}")
            k = key["pairs"][pid]
            x_is_a = (k["pack1_x"] == "a") == (pack_no == "1")
            v = 0 if j["better"] == "same" else (1 if (j["better"] == "Y") == x_is_a else -1)   # +1 = b better
            out.append({"judge": judge, "pack": pack_no, "pair": pid, "case": k["case"], "stratum": k["stratum"],
                        "v": v, "clear": j.get("margin") == "clear" and v != 0, "label": j["better"],
                        "plant": k.get("plant")})
    if not out:
        raise ABError(f"no judge-<judge>-<1|2>.jsonl files in {cmp}")
    return out


def summarize(js: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    per_case = defaultdict(list)
    for j in js:
        per_case[j["case"]].append(j["v"])
    nets = {c: statistics.fmean(v) for c, v in per_case.items()}
    b_cases = sorted(c for c, n in nets.items() if n >= CASE_WIN)
    a_cases = sorted(c for c, n in nets.items() if n <= -CASE_WIN)
    both = defaultdict(dict)
    for j in js:
        both[(j["judge"], j["pair"])][j["pack"]] = j["v"]
    swapped = [p for p in both.values() if len(p) == 2]
    lo, hi = bootstrap_ci(list(nets.values()), seed)
    return {
        "judgments": len(js), "cases": len(nets),
        "clear_rate": sum(j["clear"] for j in js) / len(js),
        "clear_b": sum(j["clear"] and j["v"] > 0 for j in js), "clear_a": sum(j["clear"] and j["v"] < 0 for j in js),
        "decisive_rate": sum(j["v"] != 0 for j in js) / len(js),
        "x_rate": sum(j["label"] == "X" for j in js) / len(js), "y_rate": sum(j["label"] == "Y" for j in js) / len(js),
        "swap_agreement": (sum(p["1"] == p["2"] for p in swapped) / len(swapped)) if swapped else None,
        "case_win_rate": (len(a_cases) + len(b_cases)) / len(nets),
        "b_cases": b_cases, "a_cases": a_cases, "mean_net": statistics.fmean(nets.values()), "ci90": [lo, hi],
        "sign_p": sign_test(sum(n > 0 for n in nets.values()), sum(n < 0 for n in nets.values())),
        "unsettled": sorted(c for c, n in nets.items() if 0 < abs(n) < CASE_WIN or (abs(n) >= CASE_WIN and len(per_case[c]) < 4)),
    }


def cmd_verdict(args: argparse.Namespace) -> int:
    cmp = cmp_dir(args)
    key = json.loads((cmp / "key.json").read_text(encoding="utf-8"))
    every = load_judgments(cmp, key, args.judges.split(",") if args.judges else None)
    js, planted = [j for j in every if not j["plant"]], [j for j in every if j["plant"]]
    s = summarize(js, args.seed)
    by_judge = {name: summarize([j for j in js if j["judge"] == name], args.seed) for name in sorted({j["judge"] for j in js})}
    for name, t in by_judge.items():   # sensitivity: share of planted known-worse outputs this judge caught
        mine = [j for j in planted if j["judge"] == name]
        caught = [j for j in mine if j["v"] == (1 if j["plant"]["degraded"] == "a" else -1)]
        t["plants"] = {"judged": len(mine), "caught": len(caught), "clear": sum(j["clear"] for j in caught)}
    null = None
    if args.null:
        null = json.loads((Path(args.null) / "verdict.json").read_text(encoding="utf-8"))["all"]
    write_private(cmp / "verdict.json", json.dumps({"a": args.a, "b": args.b, "all": s, "by_judge": by_judge, "null": null}, indent=1))
    pct = lambda x: "-" if x is None else f"{x:.0%}"
    orders = {name: {j["pack"] for j in js if j["judge"] == name} for name in by_judge}
    one_order = sorted(n for n, o in orders.items() if len(o) < 2)
    print(f"{args.b} vs {args.a}: {s['cases']} cases, {s['judgments']} judgments ({len(by_judge)} judge(s); "
          + (f"ONE order only: {','.join(one_order)}, position bias is not cancelled)" if one_order else "both orders)"))
    for name, t in by_judge.items():
        print(f"  {name:10} clear {t['clear_a']}·{t['clear_b']} ({args.a}·{args.b})  decisive {pct(t['decisive_rate'])}"
              f"  swap-agreement {pct(t['swap_agreement'])}  X/Y picked {pct(t['x_rate'])}/{pct(t['y_rate'])}"
              + (f"  planted caught {t['plants']['caught']}/{t['plants']['judged']} ({t['plants']['clear']} clear)"
                 if t["plants"]["judged"] else ""))
    print(f"cases won: {args.a} {len(s['a_cases'])} · {args.b} {len(s['b_cases'])} · tie {s['cases'] - len(s['a_cases']) - len(s['b_cases'])}"
          f"   mean preference {s['mean_net']:+.2f} (90% CI {s['ci90'][0]:+.2f}…{s['ci90'][1]:+.2f}, + = {args.b} better)"
          f"   sign test p={s['sign_p']:.2f}")
    if null:
        print(f"null floor (A-vs-A): clear rate {pct(null['clear_rate'])} · case-win rate {pct(null['case_win_rate'])} · "
              f"swap-agreement {pct(null['swap_agreement'])}   this run: {pct(s['clear_rate'])} · {pct(s['case_win_rate'])}")
    lo, hi = s["ci90"]
    for name, t in by_judge.items():
        if t["swap_agreement"] is not None and t["swap_agreement"] < 0.7:
            print(f"⚠ {name}: swap-agreement {pct(t['swap_agreement'])} < 70%: its verdicts follow position, not content")
        if t["plants"]["judged"] and t["plants"]["caught"] / t["plants"]["judged"] < 0.8:
            print(f"⚠ {name}: caught only {t['plants']['caught']}/{t['plants']['judged']} planted defects: too lenient to trust its 'same'")
    if null and args.margin < max(abs(x) for x in null["ci90"]):
        print(f"⚠ margin {args.margin} is inside the A-vs-A noise (null CI {null['ci90'][0]:+.2f}…{null['ci90'][1]:+.2f}): raise it or add cases")
    # Pre-registered rule (docs/ab-eval.md): decide on the 90% CI of the per-case mean preference.
    if lo > 0:
        call = f"{args.b} BETTER (whole CI above 0)"
    elif hi < -args.margin:
        call = f"{args.b} WORSE (whole CI below -{args.margin})"
    elif hi < 0:
        call = f"{args.b} WORSE, but maybe by less than the margin (CI below 0)"
    elif lo >= -args.margin:
        call = f"{args.b} NOT WORSE (CI lower bound {lo:+.2f} ≥ -{args.margin}); a trim/cost change may ship on this"
    else:
        call = f"INCONCLUSIVE (CI {lo:+.2f}…{hi:+.2f} crosses -{args.margin})"
    print("verdict:", call, "" if null else "  [no --null floor: run an A-vs-A on this case set once]")
    if call.startswith("INCONCLUSIVE"):
        print("extend (decided before looking, see docs/ab-eval.md): `run --reps <n+1>` adds one rep per arm on EVERY case;\n"
              f"  re-running only the {len(s['unsettled'])} unsettled case(s) ({','.join(c[:8] for c in s['unsettled'])}) is diagnosis, not evidence")
    return 0


# --- cli ----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "collect", "metrics", "pack", "verdict"):
        s = sub.add_parser(name)
        s.add_argument("--scratch", required=True)
        s.add_argument("--project", help="project name/id when not running inside its brain checkout")
        s.add_argument("--seed", type=int, default=20261109)
        if name == "run":
            s.add_argument("--cases", required=True)
            s.add_argument("--arm", action="append", required=True, help="NAME=RP_ASK_FLAGS, repeatable")
            s.add_argument("--reps", type=int, default=1, help="replays per case per arm (re-run with more to extend)")
            s.add_argument("--parallel", type=int, default=4, help="max replays in flight (the box has few workers; keep slots for live mail)")
            s.add_argument("--poll", type=float, default=15, help="seconds between status polls")
            s.add_argument("--retries", type=int, default=1)
            s.add_argument("--timeout", default="15m")
        if name == "pack":
            s.add_argument("--force", action="store_true", help="delete judge files of the previous packs")
            s.add_argument("--plants", type=int, default=0,
                           help="extra pairs with one side made known-worse (truncated / internals leak): judge sensitivity")
        if name == "collect":
            s.add_argument("--force", action="store_true")
        if name in ("metrics", "pack", "verdict"):
            s.add_argument("--a", required=True, help="baseline arm")
            s.add_argument("--b", required=True, help="candidate arm (same as --a: within-arm null)")
        if name == "verdict":
            s.add_argument("--margin", type=float, default=0.15,
                           help="largest per-case mean preference loss that still counts as 'not worse' (pre-register it)")
            s.add_argument("--judges", help="comma list: only these judges (default all judge-* files)")
            s.add_argument("--null", help="cmp-<a>-<b> dir of an A-vs-A comparison whose verdict.json sets the noise floor")
    args = p.parse_args(argv)
    try:
        return {"run": cmd_run, "collect": cmd_collect, "metrics": cmd_metrics, "pack": cmd_pack, "verdict": cmd_verdict}[args.cmd](args)
    except ABError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
