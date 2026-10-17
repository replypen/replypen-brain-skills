#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Replay representative real inbound cases through a production brain and report on the result.

Six subcommands over one gitignored scratch dir. Real customer mail never leaves that dir.
Every `rp ask` carries `--simulation`: no action executes, no journal commit, nothing is placed
in a mailbox (see docs/side-effects.md).

    simulate.py acquire --source helpscout --pages .replypen/simulate/raw/'page*.json'
    simulate.py acquire --source harvest --corpus corpus.md [--append]
    simulate.py acquire --source manual --cases my-cases.json
    simulate.py plan [--seed 7] [--target 10] [--force]
    simulate.py select --pick 'S1234…|how-to|medium|covers the menu-path answer' [--pick …]
    simulate.py run [--ref main|dev/<branch>] [--parallel 2] [--only ID,ID] [--dry-run] [--force]
    simulate.py score [--ref main] [--force]
    simulate.py score --validate [--ref main]
    simulate.py report [--ref main] [--compare dev/<branch>]

Stdlib only: run with `uv run --no-project python simulate.py` or plain `python3`.

Determinism: case ids are content-derived (stable across re-acquires), the candidate order is a
seeded shuffle, and every subcommand is re-entrant per case — finished work is skipped unless
`--force`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import glob as globlib
import hashlib
import html
import importlib.util
import json
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


class SimError(Exception):
    """Operator-facing failure; main() prints it and exits 2."""


DIMS = ("content", "routing", "tone", "format", "safety")
VERDICTS = ("pass", "style-gap", "knowledge-gap", "blocked-on-grounding", "routing-error", "unsafe")
# Worst first: the report's reading order and the sort key of the cases table.
VERDICT_RANK = {"unsafe": 0, "routing-error": 1, "knowledge-gap": 2,
                "blocked-on-grounding": 3, "style-gap": 4, "pass": 5}
DIFFICULTIES = ("easy", "medium", "hard")
LEVERS = ("brain", "persona", "triage", "grounding", "host")
BLOCKED_ON = (None, "db", "write-path", "kb", "other")
DEFAULT_SEED = 7
DEFAULT_TARGET = 10
MAX_PARALLEL = 3

# Cheap selection hints only — never a classification. One place, easy to retune per project.
HINT_RES = {
    "url": re.compile(r"https?://|www\.", re.I),
    "misdirected_customer": re.compile(r"afspraak|reservatie|cadeaubon|boeken", re.I),
    "misdirected_reply": re.compile(r"software ?leverancier|niet bij het instituut", re.I),
}
TRANSIENT_RE = re.compile(
    r"timeout|timed out|temporar|rate.?limit|throttl|too many requests|"
    r"unavailable|connection|network|EOF|502|503|504", re.I)
DURATION_RE = re.compile(r"^(\d+)\s*([smh]?)$")


# --- small helpers --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def words(text: str | None) -> int:
    return len((text or "").split())


def read_json(path: Path, what: str) -> Any:
    if not path.exists():
        raise SimError(f"missing {what}: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SimError(f"{path} is not valid JSON: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ref_slug(ref: str) -> str:
    return ref.replace("/", "--")


def case_id(source: str, inbound_text: str, reply: str) -> str:
    digest = hashlib.sha256(f"{source}\n{norm(inbound_text)}\n{norm(reply)}".encode()).hexdigest()
    return "S" + digest[:16]


def parse_duration(value: str) -> int:
    match = DURATION_RE.match(value.strip())
    if not match:
        raise SimError(f"--timeout must look like 90, 90s, 5m or 1h (got {value!r})")
    amount, unit = int(match.group(1)), match.group(2) or "s"
    return amount * {"s": 1, "m": 60, "h": 3600}[unit]


def ensure_safe_scratch(scratch: Path, root: Path) -> None:
    """Refuse to write customer mail anywhere git could stage it (prepare_harvest.py's rule)."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         cwd=str(root), text=True, capture_output=True)
    if top.returncode != 0:
        warn(f"{root} is not a git checkout — writing to {scratch} without an ignore guard")
        return
    repo = Path(top.stdout.strip()).resolve()
    try:
        scratch.resolve().relative_to(repo)
    except ValueError:
        warn(f"{scratch} sits outside the checkout {repo} — no ignore guard applied")
        return
    probe = scratch / ".simulate-ignore-check"
    ignored = subprocess.run(["git", "check-ignore", "-q", str(probe)], cwd=str(repo)).returncode == 0
    if not ignored:
        raise SimError(
            f"refusing stageable scratch root {scratch}; real customer mail lands here. "
            "Add '.replypen/' to .gitignore first.")


def preview(text: str, limit: int) -> str:
    flat = norm(text)
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def day(value: str | None) -> str:
    return (value or "")[:10] or "?"


# --- case model -----------------------------------------------------------------------------

def make_case(*, source: str, source_ref: str, channel: str, created_at: str | None,
              subject: str | None, customer_first: str | None, human_agent: str | None,
              inbound: list[dict[str, Any]], human_reply: str,
              later_turns: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    joined = "\n\n".join(turn["text"] for turn in inbound)
    return {
        "id": case_id(source, joined, human_reply),
        "source": source,
        "source_ref": str(source_ref),
        "channel": channel,
        "created_at": created_at,
        "subject": subject,
        "customer_first": customer_first,
        "human_agent": human_agent,
        "inbound": inbound,
        "human_reply": human_reply,
        "later_turns": later_turns,
        "meta": {"tags": meta.get("tags") or [], "assignee": meta.get("assignee"),
                 "status": meta.get("status"), "notes": meta.get("notes") or [],
                 "era": None, "product_line": None, "type_tag": None, "difficulty": None},
    }


def split_conversation(turns: list[dict[str, Any]]) -> tuple[list[dict], str, list[dict], str | None]:
    """turns: chronological [{role: customer|agent, at, text}]. Returns inbound, reply, later, drop."""
    first_agent = next((i for i, t in enumerate(turns) if t["role"] == "agent"), None)
    if first_agent is None:
        return [], "", [], "no_human_reply"
    inbound = [t for t in turns[:first_agent] if t["role"] == "customer"]
    if not inbound:
        return [], "", [], "no_customer_turn_before_reply"
    block = []
    index = first_agent
    while index < len(turns) and turns[index]["role"] == "agent":
        block.append(turns[index])
        index += 1
    reply = "\n\n".join(t["text"] for t in block if t["text"].strip()).strip()
    if not reply:
        return [], "", [], "no_human_reply"
    if words(" ".join(t["text"] for t in inbound)) < 3:
        return [], "", [], "inbound_too_short"
    clean_inbound = [{"at": t["at"], "text": t["text"]} for t in inbound]
    return clean_inbound, reply, turns[index:], None


# --- acquire: help scout --------------------------------------------------------------------

def expand_pages(pattern: str) -> list[Path]:
    path = Path(pattern)
    if path.is_dir():
        found = sorted(path.glob("*.json"))
    else:
        found = sorted(Path(p) for p in globlib.glob(pattern))
        if not found and path.exists():
            found = [path]
    if not found:
        raise SimError(f"--pages matched no JSON file: {pattern}")
    return found


def load_page(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, "Help Scout page")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("conversations", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        embedded = data.get("_embedded") or {}
        if isinstance(embedded.get("conversations"), list):
            return embedded["conversations"]
    raise SimError(f"{path} is not a JSON list of Help Scout conversations")


def hs_tags(conversation: dict[str, Any]) -> list[str]:
    out = []
    for tag in conversation.get("tags") or []:
        if isinstance(tag, str):
            out.append(tag)
        elif isinstance(tag, dict) and tag.get("tag"):
            out.append(str(tag["tag"]))
    return out


def hs_person(value: Any) -> str | None:
    if isinstance(value, dict):
        name = " ".join(str(value[k]) for k in ("first", "last") if value.get(k)).strip()
        return name or value.get("email") or None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def acquire_helpscout(pages: str) -> tuple[list[dict], int, dict[str, int]]:
    cases: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    seen = 0
    for path in expand_pages(pages):
        for conversation in load_page(path):
            seen += 1
            case, reason = helpscout_case(conversation)
            if case is None:
                dropped[reason] = dropped.get(reason, 0) + 1
            else:
                cases.append(case)
    return cases, seen, dropped


def helpscout_case(conversation: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    raw = conversation.get("threads") or []
    ordered = sorted(enumerate(raw), key=lambda pair: (str(pair[1].get("at") or ""), pair[0]))
    turns: list[dict[str, Any]] = []
    notes: list[str] = []
    is_chat = False
    for _, thread in ordered:
        kind = (thread.get("type") or "").lower()
        body = (thread.get("body") or "").strip()
        if kind == "beaconchat":
            is_chat = True
        if kind == "note":
            if body and not body.startswith("Technical Information") and len(body) <= 500:
                notes.append(body)
            continue
        if kind not in ("customer", "message", "beaconchat") or not body:
            continue
        role = "customer" if (thread.get("by") or "").lower() == "customer" else "agent"
        turns.append({"role": role, "at": thread.get("at"), "text": body,
                      "first": thread.get("first")})
    inbound, reply, later, drop = split_conversation(turns)
    if drop:
        return None, drop
    source_type = ((conversation.get("source") or {}).get("type") or "").lower()
    channel = "chat" if source_type.startswith("beacon") and is_chat else "email"
    customer_first = next((t.get("first") for t in turns if t["role"] == "customer" and t.get("first")), None)
    agent_first = next((t.get("first") for t in turns if t["role"] == "agent" and t.get("first")), None)
    return make_case(
        source="helpscout",
        source_ref=conversation.get("number") or conversation.get("id") or "?",
        channel=channel,
        created_at=conversation.get("createdAt") or (inbound[0]["at"] if inbound else None),
        subject=conversation.get("subject"),
        customer_first=customer_first,
        human_agent=agent_first or hs_person(conversation.get("assignee")),
        inbound=inbound,
        human_reply=reply,
        later_turns=[{"role": t["role"], "at": t["at"], "text": t["text"]} for t in later],
        meta={"tags": hs_tags(conversation), "assignee": hs_person(conversation.get("assignee")),
              "status": conversation.get("status"), "notes": notes},
    ), ""


# --- acquire: harvest v3 corpus --------------------------------------------------------------

def load_prepare_harvest():
    path = Path(__file__).resolve().parents[2] / "brain-harvest" / "scripts" / "prepare_harvest.py"
    if not path.exists():
        raise SimError(
            f"cannot import the harvest corpus parser: {path} is missing. Install/update the "
            "brain-skills kit (brain-dev-upgrade) or use --source helpscout|manual instead.")
    spec = importlib.util.spec_from_file_location("prepare_harvest_for_simulate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolves annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def acquire_harvest(corpus: str) -> tuple[list[dict], int, dict[str, int]]:
    module = load_prepare_harvest()
    path = Path(corpus)
    files = sorted(path.glob("*.md")) if path.is_dir() else [path]
    if not files or not all(f.exists() for f in files):
        raise SimError(f"--corpus not found: {corpus}")
    try:
        threads, _harvested_at, formats, _diag = module.load_threads(files)
    except Exception as exc:  # HarvestError and friends stay operator-facing
        raise SimError(f"harvest corpus rejected by the v3 parser: {exc}") from exc
    if any(fmt != "v3" for label in formats for fmt in label.split("+")):
        raise SimError(
            f"simulate needs a harvest_format v3 corpus (got {'+'.join(formats)}); v1/v2 are "
            "mailbox-first sent-only exports without the inbound turn simulate replays.")
    cases: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for thread in threads:
        turns = [{"role": "customer" if m.role == "external" else "agent",
                  "at": m.date, "text": m.body.strip()}
                 for m in thread.messages if m.body.strip()]
        inbound, reply, later, drop = split_conversation(turns)
        if drop:
            dropped[drop] = dropped.get(drop, 0) + 1
            continue
        cases.append(make_case(
            source="harvest", source_ref=thread.section_index, channel="email",
            created_at=inbound[0]["at"], subject=thread.subject or None,
            customer_first=None, human_agent=None, inbound=inbound, human_reply=reply,
            later_turns=later, meta={"tags": []},
        ))
    return cases, len(threads), dropped


# --- acquire: manual ---------------------------------------------------------------------------

def acquire_manual(cases_file: str) -> tuple[list[dict], int, dict[str, int]]:
    data = read_json(Path(cases_file), "manual case list")
    if not isinstance(data, list):
        raise SimError("--cases must hold a JSON list of cases")
    cases: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise SimError(f"cases[{index}] must be an object")
        inbound = item.get("inbound") or []
        if isinstance(inbound, str):
            inbound = [{"at": item.get("created_at"), "text": inbound}]
        inbound = [{"at": t.get("at"), "text": (t.get("text") or "").strip()} for t in inbound
                   if (t.get("text") or "").strip()]
        reply = (item.get("human_reply") or "").strip()
        if not inbound or not reply:
            dropped["manual_incomplete"] = dropped.get("manual_incomplete", 0) + 1
            continue
        if words(" ".join(t["text"] for t in inbound)) < 3:
            dropped["inbound_too_short"] = dropped.get("inbound_too_short", 0) + 1
            continue
        case = make_case(
            source="manual", source_ref=item.get("source_ref", index),
            channel=item.get("channel") or "email", created_at=item.get("created_at"),
            subject=item.get("subject"), customer_first=item.get("customer_first"),
            human_agent=item.get("human_agent"), inbound=inbound, human_reply=reply,
            later_turns=item.get("later_turns") or [], meta=item.get("meta") or {},
        )
        if isinstance(item.get("id"), str) and item["id"].strip():
            case["id"] = item["id"].strip()
        cases.append(case)
    return cases, len(data), dropped


def sort_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(cases, key=lambda c: (c.get("created_at") or "", c["id"]))


def cmd_acquire(args: argparse.Namespace, scratch: Path) -> int:
    if args.source == "helpscout":
        if not args.pages:
            raise SimError("--source helpscout needs --pages <dir or glob>")
        fresh, seen, dropped = acquire_helpscout(args.pages)
    elif args.source == "harvest":
        if not args.corpus:
            raise SimError("--source harvest needs --corpus <file-or-dir>")
        fresh, seen, dropped = acquire_harvest(args.corpus)
    else:
        if not args.cases:
            raise SimError("--source manual needs --cases <file.json>")
        fresh, seen, dropped = acquire_manual(args.cases)

    by_id: dict[str, dict[str, Any]] = {}
    if args.append and (scratch / "cases.json").exists():
        for case in read_json(scratch / "cases.json", "cases.json"):
            by_id[case["id"]] = case
    duplicates = 0
    for case in fresh:
        if case["id"] in by_id:
            duplicates += 1
        by_id[case["id"]] = case
    cases = sort_cases(list(by_id.values()))
    write_json(scratch / "cases.json", cases)
    record = {"source": args.source, "input_count": seen, "kept": len(fresh),
              "dropped": dict(sorted(dropped.items())), "acquired_at": now_iso(),
              "appended": bool(args.append), "total_cases": len(cases)}
    write_json(scratch / "acquire.json", record)
    drops = ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())) or "none"
    print(f"acquire {args.source}: read {seen}, kept {len(fresh)}, dropped {sum(dropped.values())} "
          f"({drops}), duplicates {duplicates}")
    print(f"cases.json now holds {len(cases)} cases -> {scratch / 'cases.json'}")
    return 0


# --- plan / select -------------------------------------------------------------------------

def hints_for(case: dict[str, Any]) -> dict[str, bool]:
    inbound = " ".join(t["text"] for t in case["inbound"])
    reply = case["human_reply"]
    return {
        "multi_turn": len(case["inbound"]) > 1,
        "long_reply": words(reply) > 60,
        "question_only_reply": reply.rstrip().endswith("?") or ("?" in reply and words(reply) < 25),
        "has_url_in_reply": bool(HINT_RES["url"].search(reply)),
        "misdirected_hint": bool(HINT_RES["misdirected_customer"].search(inbound)
                                 and HINT_RES["misdirected_reply"].search(reply)),
    }


def validate_selection(selection: Any, cases: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    if not isinstance(selection, dict) or not isinstance(selection.get("cases"), list):
        return ["selection.json must be an object with a 'cases' list"]
    known = {c["id"] for c in cases}
    seen: set[str] = set()
    for index, entry in enumerate(selection["cases"]):
        where = f"selection.cases[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be an object")
            continue
        cid = entry.get("id")
        if cid not in known:
            problems.append(f"{where}.id {cid!r} is not in cases.json")
        elif cid in seen:
            problems.append(f"{where}.id {cid} is selected twice")
        else:
            seen.add(cid)
        if not str(entry.get("reason") or "").strip():
            problems.append(f"{where}.reason is empty (say why this case is representative)")
        if entry.get("difficulty") not in DIFFICULTIES:
            problems.append(f"{where}.difficulty must be one of {'/'.join(DIFFICULTIES)}")
        if not str(entry.get("type_tag") or "").strip():
            problems.append(f"{where}.type_tag is empty")
    return problems


def selection_summary(selection: dict[str, Any]) -> str:
    lines = [f"selection: {len(selection['cases'])} cases "
             f"(seed {selection.get('seed', DEFAULT_SEED)}, target {selection.get('target', DEFAULT_TARGET)})",
             f"{'id':18} {'type':16} {'diff':7} reason"]
    for entry in selection["cases"]:
        lines.append(f"{entry['id']:18} {str(entry.get('type_tag'))[:16]:16} "
                     f"{str(entry.get('difficulty')):7} {preview(entry.get('reason'), 70)}")
    tags = sorted({str(e.get("type_tag")) for e in selection["cases"]})
    lines.append(f"type coverage: {', '.join(tags)}")
    return "\n".join(lines)


def cmd_plan(args: argparse.Namespace, scratch: Path) -> int:
    cases = read_json(scratch / "cases.json", "cases.json")
    selection_path = scratch / "selection.json"
    if selection_path.exists() and not args.force:
        selection = read_json(selection_path, "selection.json")
        problems = validate_selection(selection, cases)
        if problems:
            print("selection.json has problems:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(selection_summary(selection))
        print("\n(re-plan with --force to get a fresh candidate list)")
        return 0

    order = list(cases)
    random.Random(args.seed).shuffle(order)
    rows = []
    for case in order:
        inbound = " ".join(t["text"] for t in case["inbound"])
        rows.append({
            "id": case["id"], "channel": case["channel"], "date": day(case.get("created_at")),
            "human_agent": case.get("human_agent"), "words_in": words(inbound),
            "words_out": words(case["human_reply"]), "tags": case["meta"].get("tags") or [],
            "subject": case.get("subject"),
            "inbound_preview": preview(inbound, 140),
            "reply_preview": preview(case["human_reply"], 100),
            "hints": hints_for(case),
        })
    write_json(scratch / "candidates.json",
               {"seed": args.seed, "target": args.target, "generated_at": now_iso(),
                "total": len(rows), "cases": rows})

    header = [
        f"# Simulate candidates — {len(rows)} cases, seed {args.seed}",
        "",
        f"Pick **{args.target}** representative cases: cover the ticket-type spread, deliberately",
        "include hard ones (multi-turn, needs-DB, escalation, misdirected sender, feature request,",
        "billing), skip trivial FAQ-only ones. Write the picks with `simulate.py select --pick ...`.",
        "",
        f"channels: " + ", ".join(f"{k}={v}" for k, v in sorted(
            {r["channel"]: sum(1 for x in rows if x["channel"] == r["channel"]) for r in rows}.items())),
        f"hint counts: " + ", ".join(
            f"{name}={sum(1 for r in rows if r['hints'][name])}" for name in
            ("multi_turn", "long_reply", "question_only_reply", "has_url_in_reply", "misdirected_hint")),
        "",
        "`id | channel | date | agent | words_in/out | tags | inbound → human reply`",
        "",
    ]
    body = []
    for row in rows:
        flags = ",".join(name for name, on in row["hints"].items() if on)
        tags = ",".join(row["tags"]) or "-"
        body.append(f"{row['id']} | {row['channel']} | {row['date']} | {row['human_agent'] or '-'} | "
                    f"{row['words_in']}/{row['words_out']}w | {tags}{('|' + flags) if flags else ''} | "
                    f"{row['inbound_preview']} → {row['reply_preview']}")
    (scratch / "candidates.md").write_text("\n".join(header + body) + "\n", encoding="utf-8")
    print(f"wrote {scratch / 'candidates.md'} ({len(rows)} candidates, target {args.target})")
    print(f"wrote {scratch / 'candidates.json'}")
    return 0


def cmd_select(args: argparse.Namespace, scratch: Path) -> int:
    cases = read_json(scratch / "cases.json", "cases.json")
    entries = []
    for pick in args.pick:
        parts = [p.strip() for p in pick.split("|", 3)]
        if len(parts) != 4:
            raise SimError(f"--pick needs 'ID|type_tag|difficulty|reason' (got {pick!r})")
        entries.append({"id": parts[0], "type_tag": parts[1],
                        "difficulty": parts[2], "reason": parts[3]})
    seed, target = args.seed, args.target
    if (scratch / "candidates.json").exists():
        candidates = read_json(scratch / "candidates.json", "candidates.json")
        seed = candidates.get("seed", seed)
        target = candidates.get("target", target)
    selection = {"seed": seed, "target": target, "selected_at": now_iso(), "cases": entries}
    problems = validate_selection(selection, cases)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    write_json(scratch / "selection.json", selection)
    print(selection_summary(selection))
    print(f"\nwrote {scratch / 'selection.json'}")
    return 0


def load_selection(scratch: Path) -> tuple[dict[str, Any], list[dict], dict[str, dict]]:
    cases = read_json(scratch / "cases.json", "cases.json")
    selection = read_json(scratch / "selection.json",
                          "selection.json (run `plan`, then `select --pick ...`)")
    problems = validate_selection(selection, cases)
    if problems:
        raise SimError("selection.json is invalid: " + "; ".join(problems))
    return selection, cases, {c["id"]: c for c in cases}


# --- run ------------------------------------------------------------------------------------

def build_question(case: dict[str, Any]) -> str:
    return "\n\n---\n\n".join(turn["text"].strip() for turn in case["inbound"])


def build_subject(case: dict[str, Any]) -> str:
    if case.get("subject"):
        return str(case["subject"])
    first = norm(case["inbound"][0]["text"]).split()
    return " ".join(first[:8]) or "Support request"


def build_command(case: dict[str, Any], ref: str, timeout: str, sender: str | None) -> list[str]:
    frm = sender or f"simulate-{case['id'][:8].lower()}@example.test"
    command = [_cli(), "ask", build_question(case),
               "--scenario", "email", "--simulation",
               "--subject", build_subject(case), "--from", frm,
               "--timeout", timeout, "-o", "json", "--raw-output"]
    if ref != "main":
        command += ["--brain-ref", ref]
    return command


def elide_command(command: list[str]) -> list[str]:
    return [preview(part, 80) if index == 2 else part for index, part in enumerate(command)]


def snapshot_persona(scratch: Path, root: Path) -> None:
    command = [_cli(), "project", "settings", "behavior", "get", "-o", "json", "--raw-output"]
    try:
        done = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"persona snapshot skipped: {exc}")
        return
    if done.returncode != 0:
        warn(f"persona snapshot skipped (rc exit {done.returncode}): {done.stderr.strip()[:200]}")
        return
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError:
        warn("persona snapshot skipped: rc did not return JSON")
        return
    persona = (payload.get("resolved") or {}).get("persona") or {}
    write_json(scratch / "persona.json", {"captured_at": now_iso(), "persona": persona})


def run_one(case: dict[str, Any], ref: str, args: argparse.Namespace, root: Path) -> dict[str, Any]:
    command = build_command(case, ref, args.timeout, args.sender)
    hard_timeout = parse_duration(args.timeout) + 60
    attempts = 0
    error = ""
    while attempts <= args.retries:
        attempts += 1
        transient = False
        try:
            done = subprocess.run(command, cwd=str(root), capture_output=True,
                                  text=True, timeout=hard_timeout)
        except subprocess.TimeoutExpired:
            error, transient = f"local timeout after {hard_timeout}s", True
        except OSError as exc:
            error, transient = f"cannot execute rc: {exc}", False
        else:
            if done.returncode != 0:
                error = (done.stderr or done.stdout or "").strip()[-600:]
                transient = True
            else:
                try:
                    payload = json.loads(done.stdout)
                except json.JSONDecodeError:
                    error, transient = f"rc did not return JSON: {done.stdout.strip()[-400:]}", False
                else:
                    status = payload.get("status")
                    message = str(payload.get("error") or payload.get("message") or "")
                    if status == "error" and TRANSIENT_RE.search(message):
                        error, transient = message[-600:], True
                    else:
                        return result_record(case, ref, payload, attempts, command,
                                             error=message if status == "error" else "")
        if not transient or attempts > args.retries:
            break
    return {"case_id": case["id"], "ref": ref, "run_id": None, "run_url": None,
            "status": "error", "category": None, "outcome": None, "metadata_outcome": None,
            "draft_markdown": "", "note_markdown": "", "turns": None, "bash_total": None,
            "duration_ms": None, "attempts": attempts, "error": error,
            "command": elide_command(command), "ran_at": now_iso()}


def result_record(case: dict[str, Any], ref: str, payload: dict[str, Any], attempts: int,
                  command: list[str], error: str = "") -> dict[str, Any]:
    metadata = payload.get("metadata") or {}
    return {
        "case_id": case["id"], "ref": ref,
        "run_id": payload.get("run_id"),
        "run_url": payload.get("run_url") or metadata.get("run_url"),
        "status": payload.get("status"), "category": payload.get("category"),
        "outcome": payload.get("outcome"), "metadata_outcome": metadata.get("outcome"),
        "draft_markdown": payload.get("draft_markdown") or "",
        "note_markdown": payload.get("note_markdown") or payload.get("note") or "",
        "turns": payload.get("turns"), "bash_total": payload.get("bash_total"),
        "duration_ms": payload.get("duration_ms"), "attempts": attempts,
        "error": error, "command": elide_command(command), "ran_at": now_iso(),
    }


def cmd_run(args: argparse.Namespace, scratch: Path, root: Path) -> int:
    selection, _cases, by_id = load_selection(scratch)
    only = {i.strip() for arg in (args.only or []) for i in arg.split(",") if i.strip()}
    slug = ref_slug(args.ref)
    out_dir = scratch / "runs" / slug
    picked = [by_id[e["id"]] for e in selection["cases"] if not only or e["id"] in only]
    if only:
        unknown = only - {c["id"] for c in picked}
        if unknown:
            raise SimError(f"--only names cases that are not selected: {', '.join(sorted(unknown))}")

    todo, skipped = [], []
    for case in picked:
        path = out_dir / f"{case['id']}.json"
        if path.exists() and not args.force:
            existing = read_json(path, "run result")
            if existing.get("status") == "done":
                skipped.append(case["id"])
                continue
        todo.append(case)

    if args.dry_run:
        for case in todo:
            print(" ".join(elide_command(build_command(case, args.ref, args.timeout, args.sender))))
        print(f"dry-run: {len(todo)} case(s) would run, {len(skipped)} already done", file=sys.stderr)
        return 0

    if not args.no_persona:
        snapshot_persona(scratch, root)

    results: dict[str, dict[str, Any]] = {}
    if todo:
        workers = max(1, min(args.parallel, MAX_PARALLEL))
        print(f"running {len(todo)} case(s) at ref {args.ref} with {workers} worker(s); "
              f"{len(skipped)} already done", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_one, case, args.ref, args, root): case for case in todo}
            for future in concurrent.futures.as_completed(futures):
                case = futures[future]
                record = future.result()
                results[case["id"]] = record
                write_json(out_dir / f"{case['id']}.json", record)
                print(f"  {case['id']} {record['status']} "
                      f"turns={record.get('turns')} attempts={record['attempts']}", file=sys.stderr)

    print(f"{'case':18} {'status':7} {'turns':>5} {'dur':>7}  run")
    failed = 0
    for case in picked:
        path = out_dir / f"{case['id']}.json"
        record = results.get(case["id"]) or (read_json(path, "run result") if path.exists() else None)
        if record is None:
            continue
        if record["status"] != "done":
            failed += 1
        duration = record.get("duration_ms")
        print(f"{case['id']:18} {str(record['status']):7} {str(record.get('turns') or '-'):>5} "
              f"{(str(round(duration / 1000)) + 's') if duration else '-':>7}  "
              f"{record.get('run_url') or record.get('error', '')[:80]}")
    print(f"\nsimulation runs only — nothing was sent, no action executed. Results in {out_dir}")
    return 1 if failed else 0


# --- score ----------------------------------------------------------------------------------

def persona_lines(scratch: Path) -> list[str]:
    path = scratch / "persona.json"
    if not path.exists():
        return ["(no persona snapshot; run `simulate.py run` or `rp project settings behavior get`)"]
    persona = read_json(path, "persona.json").get("persona") or {}
    lines = []
    for key in sorted(persona):
        entry = persona[key]
        value = entry.get("value") if isinstance(entry, dict) else entry
        lines.append(f"- **{key}**: {norm(str(value))[:400]}")
    return lines or ["(persona snapshot is empty)"]


def judge_bundle(case: dict[str, Any], entry: dict[str, Any], record: dict[str, Any],
                 scratch: Path) -> str:
    lines = [f"# Judge bundle — {case['id']} ({entry.get('type_tag')} / {entry.get('difficulty')})",
             "",
             "Read `templates/judge-prompt.md` for the rubric. Write your verdict to "
             f"`scores/{ref_slug(record['ref'])}/{case['id']}.json`.",
             "",
             "## Case",
             f"- channel: {case['channel']}",
             f"- date: {day(case.get('created_at'))}",
             f"- subject: {case.get('subject') or '(none)'}",
             f"- selected because: {entry.get('reason')}",
             f"- tags: {', '.join(case['meta'].get('tags') or []) or '-'}",
             f"- run: {record.get('run_url') or '(no run url)'} (status {record.get('status')}, "
             f"turns {record.get('turns')})",
             "",
             "## Persona settings in effect (values only)",
             *persona_lines(scratch),
             "",
             "## Inbound (what the customer wrote)",
             ""]
    for turn in case["inbound"]:
        lines += [f"**contact ({day(turn.get('at'))}):**", turn["text"].strip(), ""]
    lines += ["## Human answered", "", case["human_reply"].strip(), ""]
    later = case.get("later_turns") or []
    lines += ["## Later turns (context only, collapsed)", ""]
    if later:
        for turn in later:
            lines.append(f"- {turn.get('role')} ({day(turn.get('at'))}): "
                         f"{preview(turn.get('text', ''), 220)}")
    else:
        lines.append("- (none)")
    lines += ["", "## Our draft", "", (record.get("draft_markdown") or "(no draft)").strip(), ""]
    if record.get("note_markdown"):
        lines += ["## Our note", "", record["note_markdown"].strip(), ""]
    if record.get("status") != "done":
        lines += ["## Run error", "", "```", (record.get("error") or "")[-800:], "```", ""]
    return "\n".join(lines) + "\n"


def score_problems(score: Any, case_id_expected: str, ref: str) -> list[str]:
    problems: list[str] = []
    if not isinstance(score, dict):
        return [f"{case_id_expected}: score file must be an object"]
    if score.get("case_id") != case_id_expected:
        problems.append(f"{case_id_expected}: case_id mismatch ({score.get('case_id')!r})")
    if score.get("ref") not in (None, ref):
        problems.append(f"{case_id_expected}: ref mismatch ({score.get('ref')!r} != {ref})")
    scores = score.get("scores")
    if not isinstance(scores, dict):
        problems.append(f"{case_id_expected}: 'scores' must be an object")
    else:
        for dim in DIMS:
            value = scores.get(dim)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 4:
                problems.append(f"{case_id_expected}: scores.{dim} must be an integer 0-4")
        extra = sorted(set(scores) - set(DIMS))
        if extra:
            problems.append(f"{case_id_expected}: unknown score dimensions {extra}")
    if score.get("verdict") not in VERDICTS:
        problems.append(f"{case_id_expected}: verdict must be one of {'/'.join(VERDICTS)}")
    if score.get("blocked_on") not in BLOCKED_ON:
        problems.append(f"{case_id_expected}: blocked_on must be null|db|write-path|kb|other")
    if score.get("lever") not in tuple(LEVERS) + ("none",):
        problems.append(f"{case_id_expected}: lever must be one of {'/'.join(LEVERS)}/none")
    for field in ("rationale", "gap"):
        if not str(score.get(field) or "").strip():
            problems.append(f"{case_id_expected}: {field} is empty")
    return problems


def recommendations_problems(data: Any, known_ids: set[str]) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["recommendations.json must be an object"]
    if not str(data.get("verdict_line") or "").strip():
        problems.append("recommendations.verdict_line is empty")
    summary = data.get("summary") or []
    if not isinstance(summary, list) or len(summary) > 5:
        problems.append("recommendations.summary must be a list of at most 5 lines")
    for index, rec in enumerate(data.get("recommendations") or []):
        where = f"recommendations[{index}]"
        if not isinstance(rec, dict):
            problems.append(f"{where} must be an object")
            continue
        if rec.get("lever") not in LEVERS:
            problems.append(f"{where}.lever must be one of {'/'.join(LEVERS)}")
        for field in ("title", "why", "prompt"):
            if not str(rec.get(field) or "").strip():
                problems.append(f"{where}.{field} is empty")
        for cid in rec.get("cases") or []:
            if cid not in known_ids:
                problems.append(f"{where}.cases has unknown id {cid!r}")
    return problems


def load_scores(scratch: Path, ref: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for cid in ids:
        path = scratch / "scores" / ref_slug(ref) / f"{cid}.json"
        if path.exists():
            out[cid] = read_json(path, "score file")
    return out


def load_runs(scratch: Path, ref: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for cid in ids:
        path = scratch / "runs" / ref_slug(ref) / f"{cid}.json"
        if path.exists():
            out[cid] = read_json(path, "run result")
    return out


def cmd_score(args: argparse.Namespace, scratch: Path) -> int:
    selection, _cases, by_id = load_selection(scratch)
    ids = [e["id"] for e in selection["cases"]]
    entries = {e["id"]: e for e in selection["cases"]}
    runs = load_runs(scratch, args.ref, ids)

    if args.validate:
        problems: list[str] = []
        scores = load_scores(scratch, args.ref, ids)
        print(f"{'case':18} {'verdict':22} {'c r t f s':11} lever")
        for cid in ids:
            score = scores.get(cid)
            if score is None:
                record = runs.get(cid)
                if record is not None and record.get("status") != "done":
                    # A failed run has nothing to judge; `report` renders it as a red row.
                    print(f"{cid:18} {'(run failed, no score)':22}")
                    continue
                problems.append(f"{cid}: no score file in scores/{ref_slug(args.ref)}/")
                print(f"{cid:18} {'MISSING':22}")
                continue
            problems += score_problems(score, cid, args.ref)
            values = score.get("scores") or {}
            grid = " ".join(str(values.get(dim, "?")) for dim in DIMS)
            print(f"{cid:18} {str(score.get('verdict')):22} {grid:11} {score.get('lever')}")
        rec_path = scratch / "recommendations.json"
        if rec_path.exists():
            problems += recommendations_problems(read_json(rec_path, "recommendations.json"), set(ids))
        else:
            print("note: no recommendations.json yet (the agent writes it before `report`)")
        if problems:
            print("\nproblems:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print("\nall scores valid")
        return 0

    missing = [cid for cid in ids if cid not in runs]
    if missing:
        warn(f"no run result yet for {len(missing)} case(s): {', '.join(missing)}")
    written = []
    out_dir = scratch / "judge" / ref_slug(args.ref)
    out_dir.mkdir(parents=True, exist_ok=True)
    for cid in ids:
        if cid not in runs:
            continue
        path = out_dir / f"{cid}.md"
        if path.exists() and not args.force:
            continue
        path.write_text(judge_bundle(by_id[cid], entries[cid], runs[cid], scratch), encoding="utf-8")
        written.append(path)
    for path in written:
        print(path)
    print(f"\n{len(written)} bundle(s) written, {len(runs) - len(written)} kept. "
          f"Judge each one, then `simulate.py score --validate`.", file=sys.stderr)
    return 0


# --- report ---------------------------------------------------------------------------------

def aggregate(scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    means = {}
    for dim in DIMS:
        values = [s["scores"][dim] for s in scores.values()
                  if isinstance(s.get("scores"), dict) and isinstance(s["scores"].get(dim), int)]
        means[dim] = round(sum(values) / len(values), 2) if values else None
    counts = {verdict: sum(1 for s in scores.values() if s.get("verdict") == verdict)
              for verdict in VERDICTS}
    total = len(scores)
    return {"means": means, "verdict_counts": counts, "n": total,
            "pass_rate": round(counts["pass"] / total, 2) if total else None}


def score_color(value: float | None) -> str:
    if value is None:
        return "#98a2b3"
    return ["#b42318", "#c2410c", "#b58105", "#4d7c26", "#177245"][min(4, max(0, int(round(value))))]


VERDICT_COLOR = {"pass": "#177245", "style-gap": "#b58105", "knowledge-gap": "#c2410c",
                 "blocked-on-grounding": "#33518c", "routing-error": "#b42318",
                 "unsafe": "#7a1020", "error": "#b42318", "unscored": "#98a2b3"}


def e(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def md_to_html(text: str) -> str:
    """Deliberately tiny: escape first, then paragraphs, **bold**, links, line breaks."""
    escaped = e(text or "")
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped, flags=re.S)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                     r'<a href="\2" target="_blank" rel="noopener">\1</a>', escaped)
    escaped = re.sub(r"(?<!\")(?<!>)(https?://[^\s<]+)",
                     r'<a href="\1" target="_blank" rel="noopener">\1</a>', escaped)
    paragraphs = [p.strip().replace("\n", "<br>") for p in re.split(r"\n\s*\n", escaped) if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paragraphs) or "<p class='muted'>(empty)</p>"


CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#101828;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 font-size:15px;line-height:1.55}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:#667085;margin:34px 0 10px}
.verdict{font-size:19px;font-weight:600;background:#fff;border:1px solid #e4e7ec;border-radius:12px;
 padding:18px 20px;margin:14px 0 6px}
.summary{margin:0;padding-left:18px;color:#344054;font-size:14.5px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.tile{flex:1 1 150px;background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:14px 16px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#667085;font-weight:700}
.tile .v{font-size:28px;font-weight:700;line-height:1.2}
.tile .b{height:5px;border-radius:3px;background:#eef0f4;margin-top:8px;overflow:hidden}
.tile .b i{display:block;height:100%}
.hist{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:14px 16px}
.hrow{display:flex;align-items:center;gap:10px;font-size:13px;padding:3px 0}
.hrow .lab{width:170px;color:#344054}
.hrow .bar{flex:1;background:#eef0f4;border-radius:3px;height:10px;overflow:hidden}
.hrow .bar i{display:block;height:100%}
.hrow .n{width:26px;text-align:right;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:#fff;
 border:1px solid #e4e7ec;border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:9px 10px;border-top:1px solid #eef0f4;vertical-align:top}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#667085;border-top:none}
td.num,th.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:700;
 color:#fff;white-space:nowrap}
.grid{font-variant-numeric:tabular-nums;letter-spacing:.08em}
.case{background:#fff;border:1px solid #e4e7ec;border-radius:12px;margin-top:10px;padding:0}
.case>summary{cursor:pointer;padding:12px 14px;display:flex;flex-wrap:wrap;gap:10px;
 align-items:center;font-size:14px}
.case>summary::-webkit-details-marker{display:none}
.case>summary .t{font-weight:600}
.case[open]>summary{border-bottom:1px solid #eef0f4}
.cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-bottom:1px solid #eef0f4}
.col{padding:14px;border-left:1px solid #eef0f4;min-width:0}
.col:first-child{border-left:none}
.col h3{margin:0 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#667085}
.col p{margin:0 0 9px;font-size:13.5px;word-wrap:break-word;overflow-wrap:anywhere}
.turn{border-left:2px solid #e4e7ec;padding-left:10px;margin-bottom:10px}
.turn .when{font-size:11px;color:#98a2b3;font-weight:700}
.judge{padding:14px}
.judge dt{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#667085;font-weight:700;
 margin-top:8px}
.judge dd{margin:2px 0 0;font-size:13.5px;color:#344054}
.rec{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:16px;margin-top:10px}
.rec h3{margin:0 0 4px;font-size:16px}
.chip{display:inline-block;background:#eef2fa;color:#33518c;border-radius:6px;padding:1px 7px;
 font-size:11.5px;font-weight:700;margin:0 4px 4px 0;text-decoration:none}
.lever{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:#33518c;font-weight:700}
.prompt-head{display:flex;align-items:center;gap:8px;margin-top:10px}
.copy{font:inherit;font-size:12px;font-weight:700;border:1px solid #e4e7ec;background:#fff;
 color:#33518c;border-radius:8px;padding:5px 11px;cursor:pointer;margin-left:auto}
pre{margin:8px 0 0;background:#11141c;color:#e6e9f2;border-radius:10px;padding:13px 15px;
 font-size:12.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-height:420px;overflow:auto}
.err{background:#fef3f2;border-color:#fda29b}
.muted{color:#98a2b3}
.footer{margin-top:40px;font-size:12px;color:#98a2b3;border-top:1px solid #e4e7ec;padding-top:14px}
a{color:#33518c}
@media (max-width:820px){.cols{grid-template-columns:1fr}
 .col{border-left:none;border-top:1px solid #eef0f4}
 .hrow .lab{width:120px}}
"""

JS = """
document.querySelectorAll('button.copy').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var pre = document.getElementById(btn.dataset.target);
    if (!pre) return;
    var done = function () {
      var old = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = old; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(pre.textContent).then(done);
    } else {
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(pre);
      sel.removeAllRanges(); sel.addRange(range);
      try { document.execCommand('copy'); done(); } catch (err) { /* selection stays */ }
    }
  });
});
"""


def row_sort_key(cid: str, scores: dict, runs: dict) -> tuple[int, str]:
    record = runs.get(cid)
    if record is not None and record.get("status") != "done":
        return (-1, cid)
    score = scores.get(cid)
    if not score:
        return (98, cid)
    return (VERDICT_RANK.get(score.get("verdict"), 97), cid)


def verdict_of(cid: str, scores: dict, runs: dict) -> str:
    record = runs.get(cid)
    if record is None:
        return "unscored"
    if record.get("status") != "done":
        return "error"
    score = scores.get(cid)
    return score.get("verdict", "unscored") if score else "unscored"


def render_case_row(case: dict, entry: dict, record: dict | None, score: dict | None) -> str:
    cid = case["id"]
    verdict = "error" if (record and record.get("status") != "done") else (
        (score or {}).get("verdict") or "unscored")
    values = (score or {}).get("scores") or {}
    grid = " ".join(str(values.get(dim, "·")) for dim in DIMS)
    pill = (f'<span class="pill" style="background:{VERDICT_COLOR.get(verdict, "#98a2b3")}">'
            f"{e(verdict)}</span>")
    link = (f'<a href="{e(record["run_url"])}" target="_blank" rel="noopener">trace ↗</a>'
            if record and record.get("run_url") else '<span class="muted">-</span>')
    failed = bool(record) and record.get("status") != "done"
    detail = "" if failed else (
        f'<span class="grid muted">{e(grid)}</span>'
        f'<span class="muted">{e(record.get("turns") if record else "-")} turns</span>')
    head = (f'<summary><span class="t">{e(entry.get("type_tag"))}</span>'
            f'<span class="muted">{e(entry.get("difficulty"))}</span>{pill}{detail}'
            f'<span style="margin-left:auto">{link}</span></summary>')

    if failed:
        body = ('<div class="judge"><dt>Run failed</dt>'
                f'<pre>{e((record.get("error") or "")[-1200:])}</pre></div>')
        return f'<details id="{e(cid)}" class="case err">{head}{body}</details>'

    inbound = "".join(
        f'<div class="turn"><div class="when">{e(day(t.get("at")))}</div>{md_to_html(t["text"])}</div>'
        for t in case["inbound"])
    draft = md_to_html((record or {}).get("draft_markdown") or "(no draft)")
    note = ((record or {}).get("note_markdown") or "").strip()
    if note:
        draft += f'<h3 style="margin-top:12px">Note</h3>{md_to_html(note)}'
    cols = (f'<div class="cols"><div class="col"><h3>Inbound</h3>{inbound}</div>'
            f'<div class="col"><h3>Human answered</h3>{md_to_html(case["human_reply"])}</div>'
            f'<div class="col"><h3>Our draft</h3>{draft}</div></div>')

    items = [("Selected because", entry.get("reason"))]
    if score:
        items += [("Rationale", score.get("rationale")), ("Gap", score.get("gap")),
                  ("Lever", score.get("lever")), ("Blocked on", score.get("blocked_on") or "-")]
    else:
        items.append(("Judge", "not scored yet"))
    judge = "".join(f"<dt>{e(k)}</dt><dd>{md_to_html(str(v))}</dd>" for k, v in items)
    meta = (f'<dt>Case</dt><dd class="muted">{e(cid)} · {e(case["channel"])} · '
            f'{e(day(case.get("created_at")))} · {e(case.get("subject") or "(no subject)")}</dd>')
    return (f'<details id="{e(cid)}" class="case">{head}{cols}'
            f'<dl class="judge">{judge}{meta}</dl></details>')


def delta_label(value: int | None) -> str:
    return "·" if value is None else f"{value:+d}"


def pct(value: float | None) -> str:
    return "–" if value is None else f"{round(value * 100)}%"


def render_compare(rows: list[dict[str, Any]], ref: str, compare: str) -> str:
    if not rows:
        return ""
    head = ("<tr><th>case</th><th>verdict " + e(ref) + " → " + e(compare) + "</th>"
            + "".join(f'<th class="num">{e(d[:4])}</th>' for d in DIMS)
            + '<th class="num">changed</th></tr>')
    body = []
    for row in rows:
        deltas = "".join(f'<td class="num">{e(delta_label(row["deltas"][d]))}</td>' for d in DIMS)
        body.append(f'<tr><td><a href="#{e(row["id"])}">{e(row["id"][:9])}</a></td>'
                    f'<td>{e(row["from"])} → {e(row["to"])}</td>{deltas}'
                    f'<td class="num">{"yes" if row["changed"] else "no"}</td></tr>')
    changed = sum(1 for r in rows if r["changed"])
    return (f"<h2>Compare</h2><table>{head}{''.join(body)}</table>"
            f'<p class="muted">{changed} of {len(rows)} cases changed verdict or score.</p>')


def build_compare_rows(ids: list[str], scores: dict, runs: dict,
                       other_scores: dict, other_runs: dict) -> list[dict[str, Any]]:
    rows = []
    for cid in ids:
        if cid not in other_runs and cid not in other_scores:
            continue
        left, right = scores.get(cid) or {}, other_scores.get(cid) or {}
        deltas = {}
        for dim in DIMS:
            a, b = (left.get("scores") or {}).get(dim), (right.get("scores") or {}).get(dim)
            deltas[dim] = (b - a) if isinstance(a, int) and isinstance(b, int) else None
        from_v = verdict_of(cid, scores, runs)
        to_v = verdict_of(cid, other_scores, other_runs)
        rows.append({"id": cid, "from": from_v, "to": to_v, "deltas": deltas,
                     "changed": from_v != to_v or any(d not in (None, 0) for d in deltas.values())})
    return rows


def render_html(context: dict[str, Any]) -> str:
    agg = context["agg"]
    tiles = []
    for dim in DIMS:
        value = agg["means"][dim]
        color = score_color(value)
        width = 0 if value is None else round(value / 4 * 100)
        tiles.append(f'<div class="tile"><div class="k">{e(dim)}</div>'
                     f'<div class="v" style="color:{color}">{"–" if value is None else f"{value:.1f}"}'
                     f'<span class="muted" style="font-size:14px">/4</span></div>'
                     f'<div class="b"><i style="width:{width}%;background:{color}"></i></div></div>')
    biggest = max(agg["verdict_counts"].values() or [0]) or 1
    hist = []
    for verdict in VERDICTS:
        count = agg["verdict_counts"][verdict]
        hist.append(f'<div class="hrow"><span class="lab">{e(verdict)}</span>'
                    f'<span class="bar"><i style="width:{round(count / biggest * 100)}%;'
                    f'background:{VERDICT_COLOR[verdict]}"></i></span>'
                    f'<span class="n">{count}</span></div>')

    summary = "".join(f"<li>{e(line)}</li>" for line in context["recommendations"].get("summary") or [])
    summary_html = f'<ul class="summary">{summary}</ul>' if summary else ""

    rows = "".join(context["rows_html"])
    rec_html = []
    for lever in LEVERS:
        group = [r for r in context["recommendations"].get("recommendations") or []
                 if r.get("lever") == lever]
        for index, rec in enumerate(group):
            pid = f"p-{lever}-{index}"
            chips = "".join(f'<a class="chip" href="#{e(cid)}">{e(cid[:9])}</a>'
                            for cid in rec.get("cases") or [])
            rec_html.append(
                f'<div class="rec"><div class="lever">{e(lever)}</div>'
                f'<h3>{e(rec.get("title"))}</h3>{md_to_html(rec.get("why") or "")}'
                f"<div>{chips}</div>"
                f'<div class="prompt-head"><span class="muted">Prompt for a coding agent</span>'
                f'<button class="copy" type="button" data-target="{pid}">Copy</button></div>'
                f'<pre id="{pid}">{e(rec.get("prompt"))}</pre></div>')
    recs = "".join(rec_html) or '<p class="muted">No recommendations recorded.</p>'

    persona = context["persona"]
    persona_html = ""
    if persona:
        items = "".join(f"<dt>{e(k)}</dt><dd>{e(norm(str(v)))}</dd>" for k, v in persona.items())
        persona_html = ('<details class="case"><summary><span class="t">Persona settings in effect'
                        f'</span></summary><dl class="judge">{items}</dl></details>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brain simulation — {e(context['tag'])} @ {e(context['ref'])}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Brain simulation — {e(context['tag'])}</h1>
<p class="muted">ref <b>{e(context['ref'])}</b> · {agg['n']} scored of {context['n_cases']} cases ·
 pass rate {pct(agg['pass_rate'])}</p>
<div class="verdict">{e(context['verdict_line'])}</div>
{summary_html}
<h2>Scores</h2>
<div class="tiles">{''.join(tiles)}</div>
<h2>Verdict classes</h2>
<div class="hist">{''.join(hist)}</div>
{context['compare_html']}
<h2>Cases (worst first)</h2>
{rows}
<h2>Recommendations</h2>
{recs}
{persona_html}
<div class="footer">tag {e(context['tag'])} · ref {e(context['ref'])}
{(' · compared with ' + e(context['compare'])) if context['compare'] else ''} ·
generated {e(context['generated_at'])} · {context['n_cases']} cases ·
simulation runs — nothing was sent.</div>
</div><script>{JS}</script></body></html>
"""


def render_markdown(context: dict[str, Any]) -> str:
    agg = context["agg"]
    lines = [f"# Brain simulation — {context['tag']} @ {context['ref']}", "",
             context["verdict_line"], ""]
    for line in context["recommendations"].get("summary") or []:
        lines.append(f"- {line}")
    lines += ["", "## Scores", "",
              " · ".join(f"{dim} {'–' if agg['means'][dim] is None else agg['means'][dim]}/4"
                         for dim in DIMS),
              f"pass rate: {pct(agg['pass_rate'])}"
              f" ({agg['n']} scored of {context['n_cases']})", "", "## Verdict classes", ""]
    biggest = max(agg["verdict_counts"].values() or [0]) or 1
    for verdict in VERDICTS:
        count = agg["verdict_counts"][verdict]
        lines.append(f"{verdict:22} {'#' * round(count / biggest * 20):20} {count}")
    lines += ["", "## Cases", "",
              "| id | type | difficulty | verdict | " + " | ".join(DIMS) + " | run |",
              "|---|---|---|---|" + "---|" * len(DIMS) + "---|"]
    for cid in context["order"]:
        entry = context["entries"][cid]
        record = context["runs"].get(cid)
        score = context["scores"].get(cid)
        values = (score or {}).get("scores") or {}
        grid = " | ".join(str(values.get(dim, "·")) for dim in DIMS)
        lines.append(f"| {cid} | {entry.get('type_tag')} | {entry.get('difficulty')} | "
                     f"{verdict_of(cid, context['scores'], context['runs'])} | {grid} | "
                     f"{(record or {}).get('run_url') or (record or {}).get('status') or '-'} |")
    if context["compare_rows"]:
        lines += ["", f"## Compare {context['ref']} → {context['compare']}", "",
                  "| id | verdict | " + " | ".join(DIMS) + " |",
                  "|---|---|" + "---|" * len(DIMS)]
        for row in context["compare_rows"]:
            deltas = " | ".join("·" if row["deltas"][d] is None else f"{row['deltas'][d]:+d}"
                                for d in DIMS)
            lines.append(f"| {row['id']} | {row['from']} → {row['to']} | {deltas} |")
    lines += ["", "## Recommendations", ""]
    any_rec = False
    for lever in LEVERS:
        for rec in context["recommendations"].get("recommendations") or []:
            if rec.get("lever") != lever:
                continue
            any_rec = True
            lines += [f"### [{lever}] {rec.get('title')}", "", rec.get("why") or "",
                      f"cases: {', '.join(rec.get('cases') or []) or '-'}", "",
                      "```", (rec.get("prompt") or "").strip(), "```", ""]
    if not any_rec:
        lines += ["(none recorded)", ""]
    lines += ["---", f"simulation runs — nothing was sent. generated {context['generated_at']}."]
    return "\n".join(lines) + "\n"


def append_history(scratch: Path, entry: dict[str, Any]) -> None:
    path = scratch.parent / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (item.get("tag"), item.get("ref")) == (entry["tag"], entry["ref"]):
                continue
            kept.append(item)
    kept.append(entry)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in kept),
                    encoding="utf-8")


def cmd_report(args: argparse.Namespace, scratch: Path) -> int:
    selection, _cases, by_id = load_selection(scratch)
    ids = [entry["id"] for entry in selection["cases"]]
    entries = {entry["id"]: entry for entry in selection["cases"]}
    runs = load_runs(scratch, args.ref, ids)
    scores = load_scores(scratch, args.ref, ids)
    persona_path = scratch / "persona.json"
    persona_raw = read_json(persona_path, "persona.json").get("persona", {}) if persona_path.exists() else {}
    persona = {key: (value.get("value") if isinstance(value, dict) else value)
               for key, value in sorted(persona_raw.items())}
    rec_path = scratch / "recommendations.json"
    recommendations = read_json(rec_path, "recommendations.json") if rec_path.exists() else {}

    agg = aggregate(scores)
    order = sorted(ids, key=lambda cid: row_sort_key(cid, scores, runs))
    compare_rows: list[dict[str, Any]] = []
    compare_html = ""
    if args.compare:
        other_runs = load_runs(scratch, args.compare, ids)
        other_scores = load_scores(scratch, args.compare, ids)
        compare_rows = build_compare_rows(order, scores, runs, other_scores, other_runs)
        compare_html = render_compare(compare_rows, args.ref, args.compare)

    verdict_line = recommendations.get("verdict_line") or (
        f"{agg['verdict_counts']['pass']} of {agg['n'] or len(ids)} cases pass; "
        "no overall verdict recorded yet.")
    context = {
        "tag": scratch.name,  # the scratch dir name is the tag, even when --tag was also given
        "ref": args.ref, "compare": args.compare,
        "generated_at": now_iso(), "n_cases": len(ids), "agg": agg, "order": order,
        "entries": entries, "runs": runs, "scores": scores, "persona": persona,
        "recommendations": recommendations, "verdict_line": verdict_line,
        "compare_html": compare_html, "compare_rows": compare_rows,
        "rows_html": [render_case_row(by_id[cid], entries[cid], runs.get(cid), scores.get(cid))
                      for cid in order],
    }

    suffix = "" if args.ref == "main" else f"-{ref_slug(args.ref)}"
    html_path = scratch / f"report{suffix}.html"
    md_path = scratch / f"report{suffix}.md"
    html_path.write_text(render_html(context), encoding="utf-8")
    md_path.write_text(render_markdown(context), encoding="utf-8")
    append_history(scratch, {"at": context["generated_at"], "tag": context["tag"], "ref": args.ref,
                             "n_cases": len(ids), "mean_scores": agg["means"],
                             "verdict_counts": agg["verdict_counts"], "pass_rate": agg["pass_rate"]})
    print(f"wrote {html_path}")
    print(f"wrote {md_path}")
    print(f"history: {scratch.parent / 'history.jsonl'}")
    print(f"open with: open {html_path}")
    return 0


# --- cli ------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulate.py",
        description="Replay real inbound cases through a production brain and report on the result.")
    # Common flags are accepted before or after the subcommand (sub-level defaults are SUPPRESS so
    # they never clobber a value given before the subcommand).
    def add_common(target: argparse.ArgumentParser, top: bool) -> None:
        d = (lambda v: v) if top else (lambda v: argparse.SUPPRESS)
        target.add_argument("--brain", default=d("."), help="brain checkout root (default: cwd)")
        target.add_argument("--tag", default=d("default"), help="scratch tag (default: default)")
        target.add_argument("--scratch", default=d(None),
                            help="scratch dir (default: <brain>/.replypen/simulate/<tag>)")

    add_common(parser, top=True)
    common = argparse.ArgumentParser(add_help=False)
    add_common(common, top=False)
    sub = parser.add_subparsers(
        dest="command", required=True,
        parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    acquire = sub.add_parser("acquire", help="normalize candidate cases into cases.json")
    acquire.add_argument("--source", required=True, choices=("helpscout", "harvest", "manual"))
    acquire.add_argument("--pages", help="helpscout: dir or glob of JSON conversation pages")
    acquire.add_argument("--corpus", help="harvest: v3 corpus file or dir")
    acquire.add_argument("--cases", help="manual: JSON list of cases")
    acquire.add_argument("--append", action="store_true", help="merge into an existing cases.json")

    plan = sub.add_parser("plan", help="write the candidate list, or validate an existing selection")
    plan.add_argument("--seed", type=int, default=DEFAULT_SEED)
    plan.add_argument("--target", type=int, default=DEFAULT_TARGET)
    plan.add_argument("--force", action="store_true", help="re-plan even if selection.json exists")

    select = sub.add_parser("select", help="write selection.json from --pick args")
    select.add_argument("--pick", action="append", default=[],
                        metavar="ID|type_tag|difficulty|reason", required=True)
    select.add_argument("--seed", type=int, default=DEFAULT_SEED)
    select.add_argument("--target", type=int, default=DEFAULT_TARGET)

    run = sub.add_parser("run", help="replay the selected cases through `rp ask --simulation`")
    run.add_argument("--ref", default="main", help="main (default) or dev/<branch>")
    run.add_argument("--parallel", type=int, default=2, help=f"workers, capped at {MAX_PARALLEL}")
    run.add_argument("--retries", type=int, default=2)
    run.add_argument("--timeout", default="5m")
    run.add_argument("--from", dest="sender", help="sender address (default simulate-<id>@example.test)")
    run.add_argument("--only", action="append", default=[], metavar="ID[,ID]")
    run.add_argument("--force", action="store_true", help="re-run cases that already finished")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-persona", action="store_true", help="skip the persona settings snapshot")

    score = sub.add_parser("score", help="write judge bundles, or validate the written scores")
    score.add_argument("--ref", default="main")
    score.add_argument("--validate", action="store_true")
    score.add_argument("--force", action="store_true", help="rewrite existing bundles")

    report = sub.add_parser("report", help="render report.html + report.md")
    report.add_argument("--ref", default="main")
    report.add_argument("--compare", help="second ref to diff against")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.brain).resolve()
    scratch = Path(args.scratch).resolve() if args.scratch else (
        root / ".replypen" / "simulate" / args.tag)
    try:
        ensure_safe_scratch(scratch, root)
        scratch.mkdir(parents=True, exist_ok=True)
        if args.command == "acquire":
            return cmd_acquire(args, scratch)
        if args.command == "plan":
            return cmd_plan(args, scratch)
        if args.command == "select":
            return cmd_select(args, scratch)
        if args.command == "run":
            return cmd_run(args, scratch, root)
        if args.command == "score":
            return cmd_score(args, scratch)
        if args.command == "report":
            return cmd_report(args, scratch)
    except SimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise SimError(f"unknown command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
