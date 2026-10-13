#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""One bad chat answer → case file → replay on a dev ref → compare (brain-chat-debug).

    chat_case.py open <share-url|run-url|viewer-url|session-id|run-id> [--expected TEXT | --expected-file F]
                      [--project P] [--tenant T] [--force]
    chat_case.py replay <case-dir|session8> [--turn N] [--brain-ref dev/x] [--mirror-ref repo=ref]…
                        [--mode auto|chat|raw|email] [--prompt-override TEXT] [--effort pro|max]
                        [--principal-kind K --principal-id ID] [--tenant T] [--timeout 10m]
                        [--no-simulation] [--dry-scope] [--note TEXT]
    chat_case.py compare <case-dir|session8> <replayA> <replayB>
    chat_case.py widen <case-dir|session8> --tenants a,b,c [--turn N] [--as LABEL] [--tag T]
                       [--brain-ref dev/x] [--mirror-ref repo=ref]…

open: share link → `rc run session`; run link → `rc run debug '<link>'` (session from its header);
viewer link / bare id → the logged-in path. Logged in, it also reads `rc run thread <session>
--transcript` (feedback, principal id) and `rc run sessions search` (principal kind, surface).
Then `rc run debug` per run (skipped when the JSONL exists, unless --force). Writes case.md + case.json.

replay: `--mode auto` (default) uses `rc ask --replay-chat-run <run id>` when the installed rc has it
(chat framing, real prior turns, the session's own principal, simulation); else the raw fallback:
`rc ask --scenario raw --simulation` with the prior turns embedded in the question and the case's
principal. No --brain-ref/--mirror-ref → labelled `baseline` (same brain as today: flakiness check).
`--dry-scope` previews the principal scope of the raw path, no LLM spend.
Writes replays/<ts>-<label>.{json,md}: historical vs latest baseline vs this replay.

compare: two replay JSONs side by side. widen: questions.tsv + the devloop.py commands (no spend).

Scratch: <brain>/.rootcause/chatdebug/<session8>/ (gitignored with .rootcause/; holds customer data and
capability links). Stdlib only: `uv run --no-project python chat_case.py …`. `RC` env overrides the
rc binary (tests).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SHORT_RE = re.compile(r"^[0-9a-f]{8}$", re.I)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{30,}$")
DEFAULT_BASE = "https://app.replypen.com"
ZERO_RUNS = "No answer exists in this shared chat (seed message only, 0 runs)."
CHAT_FAITHFUL = "chat-faithful (text-only prior turns, no attachments)"
NOT_CHAT = ("not chat-equivalent: a raw/email prompt-API run lacks the chat-mode prompt sections "
            "(persona, confidentiality floor, chat output contract, record links); judge grounding, "
            "helper pickup and facts, not prose shape")
# Prompt sections worth seeing when OFF (ids only); the rest is counted.
RELEVANT_SECTIONS = (
    "principal_scoped", "hidden_tables", "pii_tokens", "pii_tokens_raw", "pii_candidates",
    "record_links", "chat_output_contract", "confidentiality_floor", "persona", "first_party_voice",
    "chat_media", "chat_hidden_context", "skills_routing", "skills_catalog", "mirror_cheat_sheet",
    "db_cheat_sheet", "db_limitation", "scoped_schema", "kb_url", "kb_link", "action_chat",
    "action_catalog", "conversation_input", "context_index", "thread_coherence", "sender_history")
SKIP_LABELS = {"read file", "search files"}
CMD_HEAD, OUT_HEAD = 120, 200


class ChatCaseError(Exception):
    """Operator-facing failure: main() prints it (naming the next call) and exits 2."""


# --- small utils ------------------------------------------------------------------------------
def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def flat(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fence(text: str) -> str:
    body = (text or "").strip() or "(empty)"
    return f"````markdown\n{body}\n````"


def norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def duration_seconds(value: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", value or "")
    if not m:
        raise ChatCaseError(f"--timeout {value!r}: use e.g. 600s, 10m, 1h")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


# --- input parsing (pure) ---------------------------------------------------------------------
def parse_input(text: str) -> dict[str, str]:
    """Share `/s/<tok>`, run `/runs/<id>?t=`, viewer `/projects/<p>[/tenants/<t>]/conversations/<s>`,
    a bare uuid, an 8-hex prefix, or a bare share token."""
    s = (text or "").strip().strip("<>'\"").strip()
    if "://" in s:
        url = urllib.parse.urlsplit(s)
        parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
        token = (urllib.parse.parse_qs(url.query).get("t") or [""])[0]
        if len(parts) == 2 and parts[0] == "s":
            return {"kind": "share", "token": parts[1], "url": s}
        if len(parts) >= 2 and parts[0] == "runs" and UUID_RE.match(parts[1]):
            if token:
                return {"kind": "run_link", "run_id": parts[1].lower(), "url": s}
            return {"kind": "uuid", "id": parts[1].lower()}
        if len(parts) >= 4 and parts[0] == "projects" and "conversations" in parts:
            i = parts.index("conversations")
            session = parts[i + 1] if i + 1 < len(parts) else ""
            if UUID_RE.match(session):
                tenant = parts[3] if len(parts) > 4 and parts[2] == "tenants" else ""
                return {"kind": "viewer", "project": parts[1], "tenant": tenant,
                        "session_id": session.lower(), "url": s}
        raise ChatCaseError(f"unrecognised link {flat(s, 80)!r}: paste a chat share link (/s/<token>), "
                            "a run link (/runs/<id>?t=…), a conversation link, or a session/run id")
    if UUID_RE.match(s):
        return {"kind": "uuid", "id": s.lower()}
    if SHORT_RE.match(s):
        return {"kind": "short", "id": s.lower()}
    if TOKEN_RE.match(s):
        return {"kind": "share", "token": s, "url": f"{DEFAULT_BASE}/s/{s}"}
    raise ChatCaseError(f"cannot read {flat(s, 80)!r} as a link or id")


def parse_pairs(values: list[str] | None) -> list[str]:
    out = []
    for value in values or []:
        repo, sep, ref = value.partition("=")
        if not sep or not repo.strip() or not ref.strip():
            raise ChatCaseError(f"--mirror-ref {value!r}: expected <repo>=<dev/branch|sha>")
        out.append(f"{repo.strip()}={ref.strip()}")
    return out


# --- question composer (pure) -----------------------------------------------------------------
def compose_question(turns: list[dict[str, Any]], n: int, override: str | None = None,
                     oneline: bool = False) -> str:
    """Turn n's question with every earlier turn embedded oldest first (rc ask --session does NOT
    replay prior turns). `oneline` flattens for a TSV cell."""
    prior = [t for t in turns if t["n"] < n]
    current = next((t for t in turns if t["n"] == n), None)
    if current is None:
        raise ChatCaseError(f"no turn {n}; the case has turns {', '.join(str(t['n']) for t in turns)}")
    question = override if override is not None else current.get("question") or ""
    if not prior:
        return flat(question, 100000) if oneline else question.strip()
    if oneline:
        parts = ["Conversation so far (oldest first):"]
        for t in prior:
            parts.append(f"User: «{flat(t.get('question'), 100000)}» Assistant: «{flat(t.get('answer'), 100000)}»")
        parts.append(f"New question: {flat(question, 100000)}")
        return " ".join(parts)
    lines = ["Conversation so far (oldest first):", ""]
    for t in prior:
        for role, text in (("User", t.get("question")), ("Assistant", t.get("answer"))):
            body = (text or "").strip().splitlines() or [""]
            lines.append(f"> {role}: {body[0]}")
            lines.extend(f"> {line}" if line else ">" for line in body[1:])
            lines.append(">")
        lines[-1] = ""
    lines += ["New question:", question.strip()]
    return "\n".join(lines)


# --- trace summary (pure) ---------------------------------------------------------------------
def md_section(md_text: str | None, heading: str) -> list[str]:
    """Bullet lines under a `## <heading>` of an `rc run debug` markdown index."""
    if not md_text:
        return []
    out, inside = [], False
    for line in md_text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().startswith(heading)
            continue
        if inside and line.startswith("- "):
            out.append(line[2:].strip().strip("`"))
    return out


def is_prestep(ev: dict[str, Any]) -> bool:
    return bool(ev.get("grounding")) or str(ev.get("disp") or "").startswith("P")


def is_main_bash(ev: dict[str, Any]) -> bool:
    return ev.get("tool") == "bash" and not is_prestep(ev) and not str(ev.get("disp") or "").startswith("C")


PATH_RE = re.compile(r"(/(?:brain|mirrors|kb|tenant|skills)/[^\s'\"|;&)<>]+)")


def summarise_trace(header: dict[str, Any], events: list[dict[str, Any]],
                    md_text: str | None = None) -> dict[str, Any]:
    """Run header + events → the evidence case.md and replay md show."""
    mirrors = [{"name": s.get("name"), "ref": s.get("ref"), "sha": (s.get("commit_sha") or "")[:12],
                "override": bool(s.get("overridden"))}
               for s in (header.get("grounding_sources") or {}).get("sources") or []
               if s.get("kind") == "mirror"]
    sections = header.get("prompt_sections") or []
    on = [s.get("id") for s in sections if s.get("on")]
    off = [s.get("id") for s in sections if not s.get("on")]
    commands = []
    for ev in events:
        if not is_main_bash(ev) or ev.get("label") in SKIP_LABELS:
            continue
        commands.append({"disp": ev.get("disp"), "label": ev.get("label") or "",
                         "command": flat(ev.get("command"), CMD_HEAD), "exit": ev.get("exit_code"),
                         "stdout": flat(ev.get("stdout"), OUT_HEAD)})
    files = md_section(md_text, "Files the run read")
    if not files:
        seen: list[str] = []
        for ev in events:
            if is_main_bash(ev) and ev.get("label") == "read file":
                for path in PATH_RE.findall(ev.get("command") or ""):
                    if path not in seen:
                        seen.append(path)
        files = seen
    meta = header.get("metadata") or {}
    sources = header.get("grounding_sources") or {}
    return {
        "run_id": header.get("run_id"), "session_id": header.get("session_id"),
        "project": header.get("project"), "tenant": header.get("tenant"),
        "kind": header.get("kind"), "trigger": header.get("trigger"), "status": header.get("status"),
        "outcome": meta.get("outcome"), "iterations": meta.get("iterations"),
        "brain_ref": header.get("brain_ref"), "brain_resolved": header.get("brain_resolved"),
        "mirrors": mirrors, "sources_captured": sources.get("captured", True) is not False,
        "sources_reason": sources.get("reason"), "sections_on": on, "sections_off": off,
        "sections_off_relevant": [s for s in off if s in RELEVANT_SECTIONS],
        "commands": commands, "files_read": files, "flags": md_section(md_text, "Flags")[:8],
        "question": header.get("question") or "", "answer": header.get("draft") or "",
    }


def load_trace(jsonl: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines or lines[0].get("type") != "run":
        raise ChatCaseError(f"{jsonl} has no run header; delete it and rerun with --force")
    return lines[0], lines[1:]


# --- rc wrapper -------------------------------------------------------------------------------
class Ctx:
    def __init__(self, root: Path):
        self.root = root
        self.rc = shlex.split(os.environ.get("RC") or "rc")
        config = root / ".rootcause.toml"
        data = tomllib.loads(config.read_text()) if config.is_file() else {}
        self.project = data.get("project") or ""
        self.base_url = (data.get("base_url") or DEFAULT_BASE).rstrip("/")
        self._help: dict[str, str] = {}

    def run(self, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([*self.rc, *args], cwd=str(self.root), capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise ChatCaseError("`rc` is not on PATH: install/upgrade it (brain-dev-upgrade)") from exc
        except subprocess.TimeoutExpired as exc:
            raise ChatCaseError(f"rc {' '.join(args[:2])} timed out after {timeout}s") from exc

    def json(self, args: list[str], timeout: int = 180, tolerate_exit: bool = False) -> Any:
        done = self.run([*args, "-o", "json", "--raw-output"], timeout)
        payload = parse_json(done.stdout)
        if done.returncode != 0 and not (tolerate_exit and isinstance(payload, dict) and payload.get("run_id")):
            raise ChatCaseError(f"rc {' '.join(a for a in args[:3] if not a.startswith('http'))} exited "
                                f"{done.returncode}: {flat(done.stderr or done.stdout, 400)}")
        if payload is None:
            raise ChatCaseError(f"rc {args[0]} {args[1] if len(args) > 1 else ''} did not return JSON")
        return payload

    def scope(self, project: str | None, tenant: str | None = None) -> list[str]:
        out = []
        if project and project != self.project:
            out += ["--project", project]
        if tenant:
            out += ["--tenant", tenant]
        return out

    def logged_in(self) -> bool:
        try:
            return bool(self.json(["auth", "status"], 30).get("logged_in"))
        except ChatCaseError:
            return False

    def ask_help(self) -> str:
        if "ask" not in self._help:
            done = self.run(["ask", "--help"], 30)
            self._help["ask"] = (done.stdout or "") + (done.stderr or "")
        return self._help["ask"]


def parse_json(text: str | None) -> Any:
    start = re.search(r"[{\[]", text or "")
    if not start:
        return None
    try:
        return json.loads(text[start.start():])
    except ValueError:
        return None


def fetch_trace(ctx: Ctx, ref: str, run_id: str, project: str | None, force: bool) -> tuple[Path, Path]:
    """`rc run debug` once per run; returns (md, jsonl) paths under <brain>/.rootcause/debug/."""
    debug = ctx.root / ".rootcause" / "debug"
    found = sorted(debug.glob(f"{run_id[:8]}-{project}.jsonl" if project else f"{run_id[:8]}-*.jsonl")) \
        or sorted(debug.glob(f"{run_id[:8]}-*.jsonl"))
    if found and not force:
        return found[0].with_suffix(".md"), found[0]
    scope = [] if "://" in ref else ctx.scope(project)
    done = ctx.run([*scope, "run", "debug", ref, "--raw-output"], 300)
    paths = [line.strip() for line in done.stdout.splitlines() if line.strip().endswith(".jsonl")]
    if done.returncode != 0 or not paths:
        hint = "" if "://" in ref else " (a bare id needs a login with access to that project; the share link needs none)"
        raise ChatCaseError(f"rc run debug {run_id} exited {done.returncode}: "
                            f"{flat(done.stderr or done.stdout, 300)}{hint}")
    jsonl = Path(paths[0]) if Path(paths[0]).is_absolute() else ctx.root / paths[0]
    return jsonl.with_suffix(".md"), jsonl


def rel(ctx: Ctx, path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(ctx.root))
    except ValueError:
        return str(path)


# --- open -------------------------------------------------------------------------------------
def message_text(msg: dict[str, Any]) -> str:
    parts = [p.get("text") or "" for p in msg.get("parts") or [] if p.get("type") == "text"]
    return "\n\n".join(p for p in parts if p).strip() or (msg.get("text") or "").strip()


def turns_from_messages(messages: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Share transcript → (seed texts, [{n, question, answer}]). Seeds = assistant text before any user."""
    seeds, turns = [], []
    for msg in sorted(messages, key=lambda m: m.get("seq") or 0):
        text = message_text(msg)
        if msg.get("role") == "user":
            turns.append({"n": len(turns) + 1, "question": text, "answer": ""})
        elif not turns:
            seeds.append(text)
        else:
            turns[-1]["answer"] = f"{turns[-1]['answer']}\n\n{text}".strip()
    return seeds, turns


def find_session_row(ctx: Ctx, session_id: str, created_at: str | None, project: str | None,
                     tenant: str | None) -> dict[str, Any] | None:
    """Principal kind/surface/tenant: the session row from `rc run sessions search` around its date."""
    try:
        day = datetime.fromisoformat((created_at or "").replace("Z", "+00:00")).date()
    except ValueError:
        day = datetime.now(timezone.utc).date()
    args = [*ctx.scope(project, tenant), "run", "sessions", "search", "--limit", "100",
            "--since", (day - timedelta(days=1)).isoformat(), "--until", (day + timedelta(days=1)).isoformat()]
    before = None
    for _ in range(5):
        payload = ctx.json([*args, *(["--before", before] if before else [])], 180)
        for row in payload.get("sessions") or []:
            if row.get("session_id") == session_id:
                return row
        before = payload.get("next_before") or (payload.get("coverage") or {}).get("next_before")
        if not before:
            return None
    return None


def resolve_case_dir(ctx: Ctx, value: str) -> Path:
    path = Path(value)
    if path.is_dir() and (path / "case.json").exists():
        return path.resolve()
    candidate = ctx.root / ".rootcause" / "chatdebug" / value[:8].lower()
    if (candidate / "case.json").exists():
        return candidate
    raise ChatCaseError(f"no case at {value!r}: run `chat_case.py open <link>` first "
                        f"(cases live in {rel(ctx, ctx.root / '.rootcause' / 'chatdebug')}/<session8>/)")


def read_expected(args: argparse.Namespace) -> str | None:
    if args.expected_file:
        return Path(args.expected_file).read_text(encoding="utf-8").strip()
    return args.expected.strip() if args.expected else None


def cmd_open(args: argparse.Namespace, ctx: Ctx) -> int:
    src = parse_input(args.input)
    if src["kind"] == "short":
        raise ChatCaseError("an 8-hex prefix is ambiguous for `open`: paste the full session/run id or the link")
    authed = ctx.logged_in()
    project, tenant = args.project, args.tenant
    session_id = title = None
    seeds: list[str] = []
    share_turns: list[dict[str, Any]] = []
    share_runs: list[dict[str, Any]] = []
    traces: dict[str, tuple[Path, Path]] = {}
    notes: list[str] = []

    if src["kind"] == "share":
        payload = ctx.json(["run", "session", src["url"]])
        blob = payload.get("runs") or {}
        transcript = payload.get("transcript") or {}
        session_id = blob.get("session_id") or blob.get("id")
        title = blob.get("title") or transcript.get("title")
        seeds, share_turns = turns_from_messages(transcript.get("messages") or [])
        share_runs = sorted(blob.get("runs") or [], key=lambda r: r.get("created_at") or "")
    elif src["kind"] == "run_link":
        md, jl = fetch_trace(ctx, src["url"], src["run_id"], None, args.force)
        traces[src["run_id"]] = (md, jl)
        header, _ = load_trace(jl)
        session_id = header.get("session_id")
        project, tenant = project or header.get("project"), tenant or header.get("tenant")
        share_runs = [{"run_id": src["run_id"], "share_url": src["url"], "created_at": header.get("created_at")}]
        share_turns = [{"n": 1, "question": header.get("question") or "", "answer": header.get("draft") or ""}]
    elif src["kind"] == "viewer":
        session_id = src["session_id"]
        project, tenant = project or src["project"], tenant or src["tenant"] or None
    else:
        if not authed:
            raise ChatCaseError("a bare id needs `rc auth login`; or paste the chat share link (/s/<token>)")
        thread = ctx.json([*ctx.scope(project, tenant), "run", "thread", src["id"]])
        runs = thread.get("runs") or []
        if thread.get("resolved_by") == "none" or not runs:
            raise ChatCaseError(f"{src['id']} is no run or session this login can see. Find the chat: "
                                "`rc run sessions search --contains \"<words from the chat>\" --since <YYYY-MM-DD>`")
        session_id = next(((r.get("attribution") or {}).get("session_id") for r in runs
                           if (r.get("attribution") or {}).get("session_id")), None) or thread.get("id")

    if not session_id:
        raise ChatCaseError("could not derive a session id from the input")
    case_dir = ctx.root / ".rootcause" / "chatdebug" / session_id[:8]
    previous = read_json(case_dir / "case.json") if (case_dir / "case.json").exists() else {}
    expected = read_expected(args)
    if expected is None:
        expected = previous.get("expected")

    # Share-only fetch first: the trace header names project/tenant for the logged-in enrichment.
    for run in share_runs:
        rid = run.get("run_id") or run.get("id")
        if rid and rid not in traces:
            try:
                traces[rid] = fetch_trace(ctx, run.get("share_url") or rid, rid, project, args.force)
            except ChatCaseError as exc:
                notes.append(f"trace {rid[:8]}: {exc}")
        if rid in traces and not project:
            header, _ = load_trace(traces[rid][1])
            project, tenant = header.get("project"), tenant or header.get("tenant")

    zero = src["kind"] == "share" and not share_runs
    turns: list[dict[str, Any]] = []
    row = None
    if not zero and authed:
        try:
            transcript = ctx.json([*ctx.scope(project or ctx.project, tenant), "run", "thread", session_id,
                                   "--transcript"])
            for i, t in enumerate(transcript.get("turns") or [], 1):
                turns.append({"n": i, "run_id": t.get("run_id"), "question": t.get("question") or "",
                              "answer": t.get("answer") or "", "outcome": t.get("outcome"),
                              "feedback": t.get("feedback"), "created_at": t.get("created_at"),
                              "principal_id": t.get("principal_id")})
            row = find_session_row(ctx, session_id, turns[0]["created_at"] if turns else None,
                                   project or ctx.project, tenant)
            if row is None:
                notes.append("session row not found by `rc run sessions search`: principal kind/surface unknown")
        except ChatCaseError as exc:
            notes.append(f"logged-in transcript unavailable ({flat(exc, 200)}): feedback and principal missing")
    elif not zero:
        notes.append("not logged in: feedback and principal are withheld from share links; "
                     "replay then needs --principal-kind/--principal-id (`rc auth login` fixes both)")

    by_run = {(r.get("run_id") or r.get("id")): r for r in share_runs}
    if not turns:
        ordered = [r.get("run_id") or r.get("id") for r in share_runs]
        for turn in share_turns:
            match = next((rid for rid in ordered if rid in traces and
                          norm(load_trace(traces[rid][1])[0].get("question")) == norm(turn["question"])), None)
            turns.append({**turn, "run_id": match, "outcome": None, "feedback": None,
                          "created_at": by_run.get(match, {}).get("created_at"), "principal_id": None})
        free = [rid for rid in ordered if rid not in {t["run_id"] for t in turns}]
        for turn in turns:
            if not turn["run_id"] and free:
                turn["run_id"] = free.pop(0)
        if src["kind"] == "run_link":
            notes.append("a run link carries one run: turn position and earlier turns unknown; "
                         "paste the chat share link for the whole conversation")

    for turn in turns:
        rid = turn.get("run_id")
        turn["share_url"] = (by_run.get(rid) or {}).get("share_url")
        turn["evidence"] = None
        if not rid:
            continue
        if rid not in traces:
            try:
                traces[rid] = fetch_trace(ctx, turn["share_url"] or rid, rid, project, args.force)
            except ChatCaseError as exc:
                notes.append(f"trace {rid[:8]}: {exc}")
                continue
        md, jl = traces[rid]
        header, events = load_trace(jl)
        turn["trace_md"], turn["trace_jsonl"] = rel(ctx, md), rel(ctx, jl)
        turn["evidence"] = summarise_trace(header, events, md.read_text(encoding="utf-8") if md.exists() else None)
        project, tenant = project or header.get("project"), tenant or header.get("tenant")

    principal = None
    if row and (row.get("principal") or {}).get("id"):
        principal = {"kind": row["principal"].get("kind"), "id": row["principal"]["id"]}
    elif any(t.get("principal_id") for t in turns):
        principal = {"kind": None, "id": next(t["principal_id"] for t in turns if t.get("principal_id"))}
        notes.append("principal kind unknown: pass --principal-kind to the raw replay")
    project = project or (None if zero else ctx.project)
    tenant = tenant or (row or {}).get("tenant_slug")
    case = {
        "schema": 1, "opened_at": now_stamp(), "input_kind": src["kind"],
        "share_link": src["url"] if src["kind"] == "share" else None,
        "viewer_url": (f"{ctx.base_url}/projects/{project}" + (f"/tenants/{tenant}" if tenant else "")
                       + f"/conversations/{session_id}") if project else None,
        "session_id": session_id, "project": project, "tenant": tenant,
        "surface": (row or {}).get("surface"), "title": title or (row or {}).get("title"),
        "principal": principal, "logged_in": authed, "expected": expected,
        "seed": seeds, "zero_runs": zero, "turns": turns, "notes": notes,
        "session_feedback": (row or {}).get("feedback"),
    }
    case_dir.mkdir(parents=True, exist_ok=True)
    write_json(case_dir / "case.json", case)
    (case_dir / "case.md").write_text(render_case_md(case, rel(ctx, case_dir)), encoding="utf-8")
    warn_if_tracked(ctx)
    print("\n".join(case_summary(case, rel(ctx, case_dir / "case.md"))))
    return 0


def warn_if_tracked(ctx: Ctx) -> None:
    done = subprocess.run(["git", "-C", str(ctx.root), "check-ignore", "-q", ".rootcause/chatdebug/x"],
                          capture_output=True, check=False)
    if done.returncode == 1:
        print("warning: .rootcause/ is not gitignored in this checkout; case files hold customer data",
              file=sys.stderr)


def feedback_text(fb: dict[str, Any] | None) -> str:
    if not fb or fb.get("score") is None:
        return "-"
    return f"{fb.get('score')}★" + (f" “{fb['comment']}”" if fb.get("comment") else "")


def case_summary(case: dict[str, Any], md_path: str | None) -> list[str]:
    turns = case["turns"]
    p = case.get("principal") or {}
    rated = [f"t{t['n']}={t['feedback']['score']}★" for t in turns if (t.get("feedback") or {}).get("score") is not None]
    last = turns[-1]["n"] if turns else None
    return [
        f"case: {md_path}",
        f"project {case.get('project') or '?'} · tenant {case.get('tenant') or '?'} · surface {case.get('surface') or '?'}",
        f"principal: {p.get('kind') or '?'}:{p.get('id') or '?'}" if p else "principal: unknown (replay --mode raw needs --principal-kind/--principal-id)",
        f"turns {len(turns)} · runs {sum(1 for t in turns if t.get('run_id'))} · traced {sum(1 for t in turns if t.get('evidence'))}"
        + (f" · {ZERO_RUNS}" if case.get("zero_runs") else ""),
        f"feedback: {', '.join(rated) or 'none'} · expected: {'given' if case.get('expected') else 'not given'}",
        f"next: read case.md, then replay --turn {last}" if last else
        "next: find the chat with an answer: rc run sessions search --contains \"<words>\" --since <YYYY-MM-DD>",
    ]


# --- renderers (pure) -------------------------------------------------------------------------
def refs_line(ev: dict[str, Any] | None) -> list[str]:
    if not ev:
        return ["- (no trace)"]
    out = [f"- brain: `{ev.get('brain_resolved') or ev.get('brain_ref') or '?'}`"]
    for m in ev.get("mirrors") or []:
        out.append(f"- mirror {m['name']}: {m.get('ref')} @ {m.get('sha')}" + (" · override" if m.get("override") else ""))
    if not ev.get("mirrors") and ev.get("sources_captured") is False:
        out.append(f"- mirrors: not captured in this trace ({ev.get('sources_reason') or 'unknown'}); "
                   "see a sibling turn")
    return out


def command_lines(ev: dict[str, Any] | None) -> list[str]:
    if not ev:
        return ["- (no trace)"]
    rows = ev.get("commands") or []
    if not rows:
        return ["- (no main-loop commands besides reads/searches)"]
    out = []
    for c in rows:
        out.append(f"- [{c.get('disp')}] {c.get('label')} · exit {c.get('exit')} · `{c.get('command')}`")
        if c.get("stdout"):
            out.append(f"  → `{c['stdout']}`")
    return out


def file_lines(ev: dict[str, Any] | None) -> list[str]:
    files = (ev or {}).get("files_read") or []
    return [f"- `{f}`" for f in files] or ["- (none)"]


def render_case_md(case: dict[str, Any], case_dir: str | None = None) -> str:
    p = case.get("principal") or {}
    turns = case.get("turns") or []
    link = case.get("share_link") or case.get("viewer_url") or "-"
    out = [f"# Chat case {case['session_id'][:8]} — {case.get('title') or '(untitled)'}", "",
           "Local scratch (gitignored): customer data + capability links. Paste run ids, never these links, "
           "into tickets or chat reports.", "",
           "| field | value |", "|---|---|",
           f"| project | {case.get('project') or '-'} |", f"| tenant | {case.get('tenant') or '-'} |",
           f"| surface | {case.get('surface') or '-'} |",
           f"| principal | {p.get('kind') or '?'} / `{p.get('id') or '?'}` |" if p else "| principal | unknown |",
           f"| session | `{case['session_id']}` |", f"| link | {link} |",
           f"| turns / runs | {len(turns)} / {sum(1 for t in turns if t.get('run_id'))} |",
           "| feedback | " + (", ".join("t%s %s" % (t["n"], feedback_text(t.get("feedback")))
                                     for t in turns if t.get("feedback")) or "-") + " |",
           "", "## Expected", "", f"> {case['expected']}" if case.get("expected") else "(not given)", ""]
    if case.get("notes"):
        out += ["## Missing / notes", ""] + [f"- {n}" for n in case["notes"]] + [""]
    for seed in case.get("seed") or []:
        out += ["## Seed message (assistant, before any question)", "", fence(seed), ""]
    if case.get("zero_runs"):
        parent = next((m.group(1) for s in case.get("seed") or []
                       for m in [re.match(r"\s*Continuing from \*\*(.+?)\*\*", s)] if m), None)
        words = parent or "<words from the chat>"
        out += ["## Turns", "", ZERO_RUNS,
                "Nothing to debug here: find the chat that holds the bad answer with "
                f"`rc run sessions search --contains \"{words}\" --since <YYYY-MM-DD>` "
                "(logged in), then `open` that session id. Never derive a failure from the seed message.", ""]
        return "\n".join(out)
    for t in turns:
        ev = t.get("evidence")
        out += [f"## Turn {t['n']}", "", "**Q:**", "", fence(t.get("question")), "", "**A:**", "", fence(t.get("answer")), "",
                f"- outcome: {t.get('outcome') or (ev or {}).get('outcome') or '-'} · feedback: {feedback_text(t.get('feedback'))}",
                f"- run: `{t.get('run_id') or '-'}` · trace: `{t.get('trace_md') or '-'}`", ""]
    out += ["## Evidence per run", ""]
    for t in turns:
        ev = t.get("evidence")
        out += [f"### Turn {t['n']} · run {(t.get('run_id') or '-')[:8]}", ""]
        if not ev:
            out += ["(no trace fetched — see notes)", ""]
            continue
        off = ev.get("sections_off_relevant") or []
        extra = len(ev.get("sections_off") or []) - len(off)
        out += refs_line(ev)
        out += [f"- prompt sections off: {', '.join(off) or '-'}" + (f" (+{extra} other)" if extra > 0 else ""),
                f"- flags: {'; '.join(ev.get('flags') or []) or '-'}", "", "Commands:"] + command_lines(ev)
        out += ["", "Files read:"] + file_lines(ev) + [""]
    last = turns[-1]["n"] if turns else 1
    mirrors = sorted({m["name"] for t in turns for m in ((t.get("evidence") or {}).get("mirrors") or [])},
                     key=lambda name: -sum(f.startswith(f"/mirrors/{name}/") for t in turns
                                           for f in ((t.get("evidence") or {}).get("files_read") or [])))
    d = case_dir or f".rootcause/chatdebug/{case['session_id'][:8]}"
    cc = 'uv run --no-project python "$CC"'
    mirror_flag = " ".join(f"--mirror-ref {m}=dev/<branch>" for m in mirrors[:1]) or "--mirror-ref <repo>=dev/<branch>"
    out += ["## Next", "",
            "```bash", 'CC="$PWD/.agents/skills/brain-chat-debug/scripts/chat_case.py"',
            f"{cc} replay {d} --turn {last}                       # baseline: same brain today (flaky?)",
            f"{cc} replay {d} --turn {last} --brain-ref dev/<branch> [{mirror_flag}]",
            f"{cc} replay {d} --turn {last} --mode raw --dry-scope  # principal scope preview, no LLM",
            f"{cc} widen {d} --tenants <a,b,c> --turn {last}", "```", "",
            f"`--turn N` replays an earlier turn (1..{last}). Mirrors in these runs: {', '.join(mirrors) or '-'}. "
            "Where the fix belongs: brain-chat-debug SKILL.md step 3.", ""]
    return "\n".join(out)


def replay_column(rec: dict[str, Any] | None) -> dict[str, Any]:
    if not rec:
        return {}
    return {"answer": rec.get("answer") or "", "evidence": rec.get("evidence"), "run_id": rec.get("run_id"),
            "status": rec.get("status"), "label": rec.get("label")}


def facts_table(columns: list[tuple[str, dict[str, Any]]]) -> list[str]:
    def mirrors(ev):
        return "<br>".join(f"{m['name']} {m.get('ref')}@{m.get('sha')}{' override' if m.get('override') else ''}"
                           for m in (ev or {}).get("mirrors") or []) or "-"
    rows = [("run", lambda c: f"`{(c.get('run_id') or '-')[:8]}`"),
            ("status", lambda c: c.get("status") or (c.get("evidence") or {}).get("status") or "-"),
            ("brain", lambda c: f"`{(c.get('evidence') or {}).get('brain_resolved') or '-'}`"),
            ("mirrors", lambda c: mirrors(c.get("evidence"))),
            ("commands", lambda c: str(len((c.get("evidence") or {}).get("commands") or []))),
            ("files read", lambda c: str(len((c.get("evidence") or {}).get("files_read") or []))),
            ("answer chars", lambda c: str(len(c.get("answer") or "")))]
    out = ["| | " + " | ".join(name for name, _ in columns) + " |", "|---|" + "---|" * len(columns)]
    for label, fn in rows:
        out.append(f"| {label} | " + " | ".join(fn(col) if col else "-" for _, col in columns) + " |")
    return out


def render_replay_md(case: dict[str, Any], turn: dict[str, Any], rec: dict[str, Any],
                     baseline: dict[str, Any] | None) -> str:
    hist = {"answer": turn.get("answer") or "", "evidence": turn.get("evidence"), "run_id": turn.get("run_id"),
            "status": (turn.get("evidence") or {}).get("status")}
    cols = [("historical (chat)", hist)]
    if baseline and baseline is not rec:
        cols.append((f"baseline {baseline['stamp']}", replay_column(baseline)))
    cols.append((f"{rec['label']} {rec['stamp']}", replay_column(rec)))
    ev = rec.get("evidence") or {}
    fidelity = CHAT_FAITHFUL if rec.get("mode") == "chat" else NOT_CHAT
    out = [f"# Replay turn {turn['n']} · {rec['label']} · {rec['stamp']}", "",
           f"> [!NOTE]\n> {fidelity}." + ("" if rec.get("mode") == "chat" else
           " Privacy/rendering regressions (PII token rendering, record links) cannot be closed by it."), "",
           f"- case `{case['session_id'][:8]}` · source run `{turn.get('run_id')}` · mode {rec.get('mode')} · "
           f"tenant {rec.get('tenant') or '-'} · principal {rec.get('principal_label') or 'session own'}",
           f"- candidate refs: brain {rec.get('brain_ref') or '(live channel)'}; mirrors "
           f"{', '.join(rec.get('mirror_refs') or []) or '(live)'}",
           f"- trace: {rec.get('run_url') or '-'} · local `{rec.get('trace_md') or '-'}`"]
    if rec.get("note"):
        out.append(f"- note: {rec['note']}")
    if rec.get("mode") != "chat" and ev.get("sections_on") and (turn.get("evidence") or {}).get("sections_on"):
        gap = [s for s in turn["evidence"]["sections_on"] if s not in set(ev["sections_on"])]
        out.append(f"- on in the chat run, off in this replay: {', '.join(gap) or '-'}")
    out += ["", "## Expected check", "", f"> {case['expected']}" if case.get("expected") else "(no expected text in case.json)",
            "", "verdict: ", "", "## Facts", ""] + facts_table(cols) + [""]
    for name, col in cols:
        out += [f"## Answer — {name}", "", fence(col.get("answer")), ""]
    for title, fn in (("Commands", command_lines), ("Files read", file_lines)):
        for name, col in cols:
            out += [f"## {title} — {name}", ""] + fn(col.get("evidence")) + [""]
    out += ["## Refs", ""]
    for name, col in cols:
        out += [f"{name}:"] + refs_line(col.get("evidence")) + [""]
    out += ["Limitations: " + "; ".join(rec.get("limitations") or []), ""]
    return "\n".join(out)


def render_compare_md(case: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> str:
    cols = [(f"{a['label']} {a['stamp']}", replay_column(a)), (f"{b['label']} {b['stamp']}", replay_column(b))]
    out = [f"# Compare · case {case['session_id'][:8]} · turn {a.get('turn')} vs {b.get('turn')}", ""]
    for rec in (a, b):
        out.append(f"- {rec['label']} {rec['stamp']}: mode {rec.get('mode')} · brain {rec.get('brain_ref') or '(live)'} · "
                   f"mirrors {', '.join(rec.get('mirror_refs') or []) or '(live)'}"
                   + (f" · note: {rec['note']}" if rec.get("note") else ""))
    out += ["", "## Expected check", "", f"> {case['expected']}" if case.get("expected") else "(none)", "", "verdict: ", "",
            "## Facts", ""] + facts_table(cols) + [""]
    for name, col in cols:
        out += [f"## Answer — {name}", "", fence(col.get("answer")), ""]
    for title, fn in (("Commands", command_lines), ("Files read", file_lines)):
        for name, col in cols:
            out += [f"## {title} — {name}", ""] + fn(col.get("evidence")) + [""]
    return "\n".join(out)


# --- replay -----------------------------------------------------------------------------------
def pick_turn(case: dict[str, Any], n: int | None) -> dict[str, Any]:
    turns = case.get("turns") or []
    if case.get("zero_runs") or not turns:
        raise ChatCaseError(f"{ZERO_RUNS} Nothing to replay.")
    if n is None:
        return turns[-1]
    for t in turns:
        if t["n"] == n:
            return t
    raise ChatCaseError(f"no turn {n}; turns are 1..{len(turns)}")


def latest_baseline(case_dir: Path, turn: int, mode: str) -> dict[str, Any] | None:
    found = []
    for path in sorted((case_dir / "replays").glob("*-baseline.json")):
        rec = read_json(path)
        if rec.get("turn") == turn and rec.get("mode") == mode and rec.get("run_id"):
            found.append(rec)
    return found[-1] if found else None


def limitations(mode: str, simulation: bool) -> list[str]:
    out = []
    if mode == "chat":
        out += ["prior turns replayed as text only; attachments are not replayed"]
    else:
        out += [NOT_CHAT, "prior turns embedded as text in the question; no warm start from the session",
                "privacy/rendering regressions (PII tokens, record links) cannot be closed by this replay"]
    out.append("data drift: today's records and settings, not those of the original run's moment")
    if simulation:
        out.append("simulation: no action executes, no journal commit")
    return out


def cmd_replay(args: argparse.Namespace, ctx: Ctx) -> int:
    case_dir = resolve_case_dir(ctx, args.case)
    case = read_json(case_dir / "case.json")
    turn = pick_turn(case, args.turn)
    if not turn.get("run_id"):
        raise ChatCaseError(f"turn {turn['n']} has no run id; reopen the case with `open --force`")
    if not ctx.logged_in():
        raise ChatCaseError("not logged in: run `rc auth login`, then rerun this replay")
    mirror_refs = parse_pairs(args.mirror_ref)
    if mirror_refs and "--mirror-ref" not in ctx.ask_help():
        raise ChatCaseError("installed rc has no `rc ask --mirror-ref`; upgrade rc (brain-dev-upgrade)")
    chat_ok = "--replay-chat-run" in ctx.ask_help()
    mode = args.mode
    if args.dry_scope:
        mode = "raw" if mode in ("auto", "chat") else mode
    elif mode == "auto":
        mode = "chat" if chat_ok else "raw"
    if mode == "chat" and not chat_ok:
        raise ChatCaseError("installed rc has no `rc ask --replay-chat-run`; upgrade rc (brain-dev-upgrade) "
                            "or pass --mode raw (not chat-equivalent)")
    simulation = mode == "chat" or not args.no_simulation
    tenant = args.tenant or case.get("tenant")
    principal = None
    scope = ctx.scope(case.get("project"))
    if mode == "chat":
        question = args.prompt_override
        cmd = [*scope, "ask", *([question] if question else []), "--replay-chat-run", turn["run_id"]]
    else:
        p = case.get("principal") or {}
        kind, pid = args.principal_kind or p.get("kind"), args.principal_id or p.get("id")
        if not (kind and pid):
            raise ChatCaseError("principal unknown for the raw replay: add "
                                "`--principal-kind <kind> --principal-id <id>` (e.g. kampadmin_admin + the admin "
                                "uuid; `rc auth login` + `open --force` fills it from the session)")
        principal = (kind, pid)
        question = compose_question(case["turns"], turn["n"], args.prompt_override)
        cmd = [*scope, "ask", question, "--scenario", mode, "--principal-kind", kind, "--principal-id", pid]
        if tenant:
            cmd += ["--tenant", tenant]
        if simulation:
            cmd.append("--simulation")
        if args.dry_scope:
            cmd.append("--dry-scope")
    if args.brain_ref:
        cmd += ["--brain-ref", args.brain_ref]
    for ref in mirror_refs:
        cmd += ["--mirror-ref", ref]
    if args.effort:
        cmd += ["--effort", args.effort]
    cmd += ["--timeout", args.timeout]
    label = "dryscope" if args.dry_scope else ("candidate" if args.brain_ref or mirror_refs else "baseline")
    stamp = now_stamp()
    base = {"stamp": stamp, "label": label, "mode": mode, "note": args.note,
            "source": {"session_id": case["session_id"], "turn": turn["n"], "run_id": turn["run_id"]},
            "turn": turn["n"], "tenant": tenant if mode != "chat" else case.get("tenant"),
            "principal": {"kind": principal[0], "id": principal[1]} if principal else case.get("principal"),
            "principal_label": f"{principal[0]}:{principal[1][:8]}" if principal else None,
            "deployed_refs": {"brain": (turn.get("evidence") or {}).get("brain_resolved"),
                              "mirrors": (turn.get("evidence") or {}).get("mirrors") or []},
            "brain_ref": args.brain_ref, "mirror_refs": mirror_refs,
            "limitations": limitations(mode, simulation), "composed_question": question,
            "command": [c if c != question else "<question>" for c in cmd]}
    replays = case_dir / "replays"
    payload = ctx.json(cmd, duration_seconds(args.timeout) + 120, tolerate_exit=not args.dry_scope)
    if args.dry_scope:
        write_json(replays / f"{stamp}-dryscope.json", {**base, "rc": payload})
        scope_doc = payload.get("scope") or {}
        grants = scope_doc.get("table_grants") or {}
        print("\n".join([
            f"dry-scope: {rel(ctx, replays / f'{stamp}-dryscope.json')}",
            f"resolved {payload.get('resolved')} · kind {scope_doc.get('kind')} · resolution {scope_doc.get('resolution')}",
            f"assurance {scope_doc.get('assurance')} · table grants {'all' if grants.get('all') else len(grants)}",
            f"error: {payload.get('error') or '-'}"]))
        return 0 if payload.get("resolved") else 1
    run_id = payload.get("run_id")
    meta = payload.get("metadata") or {}
    rec = {**base, "run_id": run_id, "status": payload.get("status"),
           "run_url": payload.get("run_url") or meta.get("run_url"),
           "answer": payload.get("answer_markdown") or payload.get("draft_markdown") or payload.get("answer") or "",
           "rc": payload, "evidence": None, "trace_md": None}
    path = replays / f"{stamp}-{label}.json"
    write_json(path, rec)  # persist the run id before the trace fetch
    if run_id:
        try:
            md, jl = fetch_trace(ctx, run_id, run_id, case.get("project"), False)
            header, events = load_trace(jl)
            rec["evidence"] = summarise_trace(header, events, md.read_text(encoding="utf-8") if md.exists() else None)
            rec["trace_md"] = rel(ctx, md)
            rec["answer"] = rec["answer"] or rec["evidence"]["answer"]
        except ChatCaseError as exc:
            rec["trace_error"] = str(exc)
        write_json(path, rec)
    baseline = None if label == "baseline" else latest_baseline(case_dir, turn["n"], mode)
    md_path = path.with_suffix(".md")
    md_path.write_text(render_replay_md(case, turn, rec, baseline), encoding="utf-8")
    hist_ev, new_ev = turn.get("evidence") or {}, rec.get("evidence") or {}
    print("\n".join([
        f"replay: {rel(ctx, md_path)}",
        f"{label} · mode {mode} ({'chat-faithful' if mode == 'chat' else 'not chat-equivalent'}) · turn {turn['n']}",
        f"run {run_id or '-'} · status {rec['status']}" + (f" · trace error: {flat(rec.get('trace_error'), 120)}" if rec.get("trace_error") else ""),
        f"brain {new_ev.get('brain_resolved') or '-'} · mirrors "
        + (", ".join(f"{m['name']}@{m['sha']}{'*' if m.get('override') else ''}" for m in new_ev.get("mirrors") or []) or "-"),
        f"commands {len(hist_ev.get('commands') or [])} → {len(new_ev.get('commands') or [])} · "
        f"files read {len(hist_ev.get('files_read') or [])} → {len(new_ev.get('files_read') or [])}",
        f"answer chars {len(turn.get('answer') or '')} → {len(rec['answer'])}",
        f"baseline in md: {baseline['stamp'] if baseline else 'none'}",
        "next: read the md, fill `verdict:` (pass / still wrong / partial)"]))
    return 0 if run_id else 1


# --- compare / widen --------------------------------------------------------------------------
def load_replay(case_dir: Path, value: str) -> dict[str, Any]:
    path = Path(value)
    if path.is_file():
        return read_json(path)
    hits = sorted(p for p in (case_dir / "replays").glob(f"{Path(value).stem}*.json"))
    if len(hits) == 1:
        return read_json(hits[0])
    known = ", ".join(p.stem for p in sorted((case_dir / "replays").glob("*.json"))) or "none"
    raise ChatCaseError(f"replay {value!r} matches {len(hits)} files; known: {known}")


def cmd_compare(args: argparse.Namespace, ctx: Ctx) -> int:
    case_dir = resolve_case_dir(ctx, args.case)
    case = read_json(case_dir / "case.json")
    a, b = load_replay(case_dir, args.a), load_replay(case_dir, args.b)
    path = case_dir / "replays" / f"compare-{a['stamp']}-vs-{b['stamp']}.md"
    path.write_text(render_compare_md(case, a, b), encoding="utf-8")
    print(f"compare: {rel(ctx, path)}\n{a['label']} {a['stamp']} ({len(a.get('answer') or '')} chars) vs "
          f"{b['label']} {b['stamp']} ({len(b.get('answer') or '')} chars)")
    return 0


def cmd_widen(args: argparse.Namespace, ctx: Ctx) -> int:
    case_dir = resolve_case_dir(ctx, args.case)
    case = read_json(case_dir / "case.json")
    turn = pick_turn(case, args.turn)
    tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]
    if not tenants:
        raise ChatCaseError("--tenants needs at least one slug")
    question = compose_question(case["turns"], turn["n"], oneline=True).replace("\t", " ")
    tag = args.tag or f"chat-{case['session_id'][:8]}"
    tsv = case_dir / f"questions-{tag}.tsv"
    rows = ["id\ttenant\tquestion\tas"] + [f"{re.sub(r'[^A-Za-z0-9._-]', '-', t)}\t{t}\t{question}\t{args.as_ or ''}"
                                           for t in tenants]
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    refs = (f" --brain-ref {args.brain_ref}" if args.brain_ref else "") + "".join(
        f" --mirror-ref {r}" for r in parse_pairs(args.mirror_ref))
    print("\n".join([
        f"questions: {rel(ctx, tsv)} ({len(tenants)} tenants, turn {turn['n']}, as {args.as_ or 'login default'})",
        "check: record ids/names in the question are tenant-specific; edit rows that would not transfer.",
        'DL="$PWD/.agents/skills/brain-helper-devloop/scripts/devloop.py"',
        f'uv run --no-project python "$DL" ask --tag {tag} --questions {rel(ctx, tsv)} --scenario raw --simulation{refs}',
        f'uv run --no-project python "$DL" report --tag {tag} [--helper <helper>.py] [--case <case file>]']))
    return 0


# --- cli --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat_case.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brain", default=".", help="brain checkout root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)
    op = sub.add_parser("open", help="gather the whole chat case into case.md + case.json")
    op.add_argument("input", help="share link, run link, conversation link, session id or run id")
    ex = op.add_mutually_exclusive_group()
    ex.add_argument("--expected", help="what the user expected (verbatim)")
    ex.add_argument("--expected-file", help="file holding the expected answer")
    op.add_argument("--project", help="project for a bare id outside this checkout's project")
    op.add_argument("--tenant", help="tenant slug (default: from the trace or session row)")
    op.add_argument("--force", action="store_true", help="re-fetch traces even when the JSONL exists")
    rp = sub.add_parser("replay", help="rerun one turn (chat-faithful when rc supports it) and write replay md")
    rp.add_argument("case", help="case dir or session8")
    rp.add_argument("--turn", type=int, help="turn number (default: last)")
    rp.add_argument("--brain-ref", help="dev/<branch> of the brain (absent + no --mirror-ref = baseline)")
    rp.add_argument("--mirror-ref", action="append", metavar="REPO=REF", help="mirror override, repeatable")
    rp.add_argument("--mode", choices=("auto", "chat", "raw", "email"), default="auto",
                    help="auto: chat (rc ask --replay-chat-run) when available, else raw")
    rp.add_argument("--prompt-override", help="replacement question text for the replayed turn")
    rp.add_argument("--effort", choices=("default", "pro", "max"))
    rp.add_argument("--principal-kind", help="raw/email mode: override the case principal kind")
    rp.add_argument("--principal-id", help="raw/email mode: override the case principal id")
    rp.add_argument("--tenant", help="raw/email mode: override the case tenant")
    rp.add_argument("--timeout", default="10m", help="rc ask --timeout (default 10m)")
    rp.add_argument("--no-simulation", action="store_true", help="raw/email mode: a real (non-simulation) run")
    rp.add_argument("--dry-scope", action="store_true", help="preview the raw path's principal scope, no LLM spend")
    rp.add_argument("--note", help="free text stored with the replay")
    cp = sub.add_parser("compare", help="two replay JSONs side by side")
    cp.add_argument("case", help="case dir or session8")
    cp.add_argument("a", help="replay json path, file stem or timestamp prefix")
    cp.add_argument("b", help="replay json path, file stem or timestamp prefix")
    wd = sub.add_parser("widen", help="questions.tsv for devloop.py over other tenants (no spend)")
    wd.add_argument("case", help="case dir or session8")
    wd.add_argument("--tenants", required=True, help="comma-separated tenant slugs")
    wd.add_argument("--turn", type=int, help="turn number (default: last)")
    wd.add_argument("--as", dest="as_", metavar="LABEL", help="devloop `as` column: roster label or kind:id")
    wd.add_argument("--tag", help="devloop tag (default chat-<session8>)")
    wd.add_argument("--brain-ref", help="printed into the devloop command")
    wd.add_argument("--mirror-ref", action="append", metavar="REPO=REF", help="printed into the devloop command")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = Ctx(Path(args.brain).resolve())
    try:
        return {"open": cmd_open, "replay": cmd_replay, "compare": cmd_compare, "widen": cmd_widen}[args.command](args, ctx)
    except ChatCaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
