"""Offline tests for chat_case.py: synthetic, scrubbed payloads only (no rc, no real ids, no PII).

Pure functions are tested directly; the open/replay wiring runs against a fake `rp` (env `RC`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import chat_case as cc

SESSION = "11111111-2222-4333-8444-555555555555"
RUN1 = "aaaaaaaa-0000-4000-8000-000000000001"
RUN2 = "bbbbbbbb-0000-4000-8000-000000000002"
NEW = "cccccccc-0000-4000-8000-000000000003"
ADMIN = "dddddddd-0000-4000-8000-000000000004"
SHARE = "https://app.example.test/s/" + "x" * 43


def header(run_id=RUN1, question="How many people are called Alex?", draft="There are **12** people.",
           override=False, ref="stable", on=("persona", "chat_output_contract", "record_links", "pii_tokens")):
    sections = [{"id": s, "on": True, "gate": "g"} for s in on]
    sections += [{"id": "principal_scoped", "on": False, "gate": "g"}, {"id": "obscure_x", "on": False, "gate": "g"}]
    return {"type": "run", "run_id": run_id, "session_id": SESSION, "project": "acme", "tenant": "tenant-a",
            "kind": "chat", "status": "done", "brain_ref": None, "brain_resolved": "channel:stable @ 0123456789ab",
            "question": question, "draft": draft, "metadata": {"outcome": "replied", "iterations": 3},
            "prompt_sections": sections,
            "grounding_sources": {"captured": True, "sources": [
                {"kind": "kb", "name": "project", "commit_sha": "ffffffffffff"},
                {"kind": "mirror", "name": "acme-common", "ref": ref, "commit_sha": "abcdef0123456789",
                 **({"overridden": True} if override else {})}]}}


def events():
    return [
        {"type": "event", "disp": "P1", "tool": "probe", "grounding": True, "label": "probe", "stdout": "requester"},
        {"type": "event", "disp": "1", "tool": "bash", "grounding": False, "label": "read file", "exit_code": 0,
         "command": "cat /mirrors/acme-common/docs/people.md", "stdout": "---\nname: people"},
        {"type": "event", "disp": "2", "tool": "bash", "grounding": False, "label": "db query", "exit_code": 0,
         "command": "python - <<'PY'\nfrom lib import db\nprint(db.query_one('SELECT count(*) FROM people'))\nPY",
         "stdout": "{'count': 12}\n"},
    ]


MD = """# Run aaaaaaaa — acme · done · chat

## Flags

- [2] slow turn 1.3s

## Files the run read

- `/mirrors/acme-common/docs/people.md`

## Drill down
"""


# --- pure -------------------------------------------------------------------------------------
def test_parse_input_variants():
    assert cc.parse_input(SHARE) == {"kind": "share", "token": "x" * 43, "url": SHARE}
    run = cc.parse_input(f"https://app.example.test/runs/{RUN1}?t=tok")
    assert run["kind"] == "run_link" and run["run_id"] == RUN1
    assert cc.parse_input(f"https://app.example.test/runs/{RUN1}") == {"kind": "uuid", "id": RUN1}
    viewer = cc.parse_input(f"https://app.example.test/projects/acme/tenants/tenant-a/conversations/{SESSION}")
    assert (viewer["kind"], viewer["project"], viewer["tenant"], viewer["session_id"]) == \
        ("viewer", "acme", "tenant-a", SESSION)
    no_tenant = cc.parse_input(f"https://app.example.test/projects/acme/conversations/{SESSION}")
    assert no_tenant["tenant"] == ""
    assert cc.parse_input(f"  {SESSION.upper()} ") == {"kind": "uuid", "id": SESSION}
    assert cc.parse_input("aaaaaaaa")["kind"] == "short"
    assert cc.parse_input("y" * 43)["kind"] == "share"
    with pytest.raises(cc.ChatCaseError):
        cc.parse_input("https://app.example.test/settings")


TURNS = [{"n": 1, "question": "How many people are called Alex?", "answer": "There are 12.\nAll active."},
         {"n": 2, "question": "Give me 3 links", "answer": "..."}]


def test_compose_question():
    assert cc.compose_question(TURNS, 1) == "How many people are called Alex?"
    two = cc.compose_question(TURNS, 2)
    assert two.startswith("Conversation so far (oldest first):")
    assert "> User: How many people are called Alex?" in two and "> Assistant: There are 12." in two
    assert "> All active." in two and two.endswith("New question:\nGive me 3 links")
    assert cc.compose_question(TURNS, 2, override="Only 2 links").endswith("Only 2 links")
    one = cc.compose_question(TURNS, 2, oneline=True)
    assert "\n" not in one and one.endswith("New question: Give me 3 links")
    with pytest.raises(cc.ChatCaseError):
        cc.compose_question(TURNS, 5)


def test_summarise_trace():
    ev = cc.summarise_trace(header(override=True, ref="dev/fix"), events(), MD)
    assert ev["brain_resolved"] == "channel:stable @ 0123456789ab"
    assert ev["mirrors"] == [{"name": "acme-common", "ref": "dev/fix", "sha": "abcdef012345", "override": True}]
    assert ev["sections_off_relevant"] == ["principal_scoped"] and "obscure_x" in ev["sections_off"]
    assert [c["label"] for c in ev["commands"]] == ["db query"]  # prestep + reads excluded
    assert ev["commands"][0]["stdout"] == "{'count': 12}"
    assert ev["files_read"] == ["/mirrors/acme-common/docs/people.md"]
    assert ev["flags"] == ["[2] slow turn 1.3s"]
    assert cc.summarise_trace(header(), events())["files_read"] == ["/mirrors/acme-common/docs/people.md"]


def case_doc(**over):
    turns = [{"n": 1, "run_id": RUN1, "question": "How many people are called Alex?", "answer": "There are 12.",
              "outcome": "replied", "feedback": {"score": 1, "comment": "wrong"}, "share_url": None,
              "trace_md": ".replypen/debug/aaaaaaaa-acme.md",
              "evidence": cc.summarise_trace(header(), events(), MD)}]
    doc = {"session_id": SESSION, "title": "Alex count", "project": "acme", "tenant": "tenant-a",
           "surface": "embed", "principal": {"kind": "acme_admin", "id": ADMIN}, "expected": "Count, no names.",
           "seed": [], "zero_runs": False, "turns": turns, "notes": [], "share_link": None,
           "viewer_url": "https://app.example.test/projects/acme/conversations/" + SESSION}
    doc.update(over)
    return doc


def test_render_case_md():
    md = cc.render_case_md(case_doc())
    assert "| principal | acme_admin / `dddddddd" in md and "| feedback | t1 1★ “wrong” |" in md
    assert "> Count, no names." in md and "## Turn 1" in md and "## Evidence per run" in md
    assert "prompt sections off: principal_scoped (+1 other)" in md
    assert "--mirror-ref acme-common=dev/<branch>" in md and "replay .replypen/chatdebug/11111111 --turn 1" in md
    assert "(not given)" in cc.render_case_md(case_doc(expected=None))


def test_render_zero_run_case():
    md = cc.render_case_md(case_doc(turns=[], zero_runs=True,
                                    seed=["Continuing from **Parent chat**:\nAsk the user first."]))
    assert cc.ZERO_RUNS in md and "## Turn 1" not in md and "## Next" not in md
    assert '--contains "Parent chat"' in md


def test_render_replay_md_three_columns():
    case = case_doc()
    turn = case["turns"][0]
    rec = {"stamp": "20260101T000001Z", "label": "candidate", "mode": "raw", "answer": "Twelve.",
           "evidence": cc.summarise_trace(header(NEW, override=True, ref="dev/fix", on=("persona",)), events(), MD),
           "run_id": NEW, "status": "done", "brain_ref": "dev/fix", "mirror_refs": ["acme-common=dev/fix"],
           "limitations": cc.limitations("raw", True)}
    base = {**rec, "stamp": "20260101T000000Z", "label": "baseline", "answer": "12 people.", "brain_ref": None}
    md = cc.render_replay_md(case, turn, rec, base)
    assert "not chat-equivalent" in md and "cannot be closed" in md
    assert "## Expected check" in md and "> Count, no names." in md and "\nverdict: \n" in md
    assert "historical (chat) | baseline 20260101T000000Z | candidate 20260101T000001Z" in md
    assert "## Answer — historical (chat)" in md and "Twelve." in md and "12 people." in md
    assert "on in the chat run, off in this replay: chat_output_contract, record_links, pii_tokens" in md
    chat = cc.render_replay_md(case, turn, {**rec, "mode": "chat", "limitations": cc.limitations("chat", True)}, None)
    assert cc.CHAT_FAITHFUL in chat and "not chat-equivalent" not in chat and "baseline 2026" not in chat


# --- wiring against a fake rc -----------------------------------------------------------------
FAKE = r'''
import json, os, sys
from pathlib import Path
data = json.loads(Path(os.environ["FAKE_RC_DATA"]).read_text())
argv = sys.argv[1:]
with open(os.environ["FAKE_RC_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
words = [a for a in argv if not a.startswith("-")]
def out(v): print(json.dumps(v)); sys.exit(0)
if argv[:2] == ["auth", "status"]: out({"logged_in": data["logged_in"]})
if argv[:2] == ["ask", "--help"]:
    print("--mirror-ref" + (" --replay-chat-run" if data["chat"] else "")); sys.exit(0)
if "ask" in argv:
    if "--dry-scope" in argv: out({"dry_scope": True, "resolved": True, "scope": {"kind": "acme_admin", "resolution": "tenant_wide", "table_grants": {"all": True}}})
    out({"run_id": data["new_run"], "status": "done", "answer_markdown": "New answer.", "run_url": "https://app.example.test/runs/x?t=y"})
if "session" in argv and "run" in argv and "sessions" not in argv: out(data["session"])
if "search" in argv:  # dates filter SESSION creation, date-only bounds inclusive (like rc)
    opt = lambda name: argv[argv.index(name) + 1][:10] if name in argv else None
    since, until = opt("--since"), opt("--until")
    out({"sessions": [r for r in data["search"]["sessions"] if (not since or r["created_at"][:10] >= since)
                      and (not until or r["created_at"][:10] <= until)]})
if "thread" in argv:
    out(data["transcript"] if "--transcript" in argv else data["thread"])
if "debug" in argv:
    ref = words[words.index("debug") + 1]
    rid = next(r for r in data["traces"] if r in ref or ref.endswith(r))
    d = Path(".replypen/debug"); d.mkdir(parents=True, exist_ok=True)
    stem = d / f"{rid[:8]}-acme"
    Path(f"{stem}.jsonl").write_text("\n".join(json.dumps(x) for x in data["traces"][rid]) + "\n")
    Path(f"{stem}.md").write_text(data["md"])
    print(f"{stem}.md\n{stem}.jsonl\nrun {rid} · status=done"); sys.exit(0)
print("unhandled " + " ".join(argv), file=sys.stderr); sys.exit(1)
'''


def fake_env(tmp_path, monkeypatch, *, logged_in=True, chat=False, zero=False,
             session_created="2026-01-01T09:59:00Z"):
    brain = tmp_path / "brain"
    brain.mkdir(exist_ok=True)
    (brain / ".replypen.toml").write_text('project = "acme"\nbase_url = "https://app.example.test"\n')
    script = tmp_path / "fake_rc.py"
    script.write_text(FAKE)
    runs = [] if zero else [{"run_id": RUN1, "created_at": "2026-01-01T10:00:00Z", "share_url": f"https://app.example.test/runs/{RUN1}?t=a"},
                            {"run_id": RUN2, "created_at": "2026-01-01T10:01:00Z", "share_url": f"https://app.example.test/runs/{RUN2}?t=b"}]
    messages = [{"role": "assistant", "seq": 1, "parts": [{"type": "text", "text": "Continuing from **Parent**:"}]}]
    if not zero:
        messages += [{"role": "user", "seq": 2, "parts": [{"type": "text", "text": "How many people are called Alex?"}]},
                     {"role": "assistant", "seq": 3, "parts": [{"type": "text", "text": "There are 12."}]},
                     {"role": "user", "seq": 4, "parts": [{"type": "text", "text": "Give me 3 links"}]},
                     {"role": "assistant", "seq": 5, "parts": [{"type": "text", "text": "Link ⟦pii:firstname:00⟧"}]}]
    data = {
        "logged_in": logged_in, "chat": chat, "new_run": NEW, "md": MD,
        "session": {"transcript": {"title": "Alex count", "messages": messages},
                    "runs": {"session_id": SESSION, "title": "Alex count", "runs": runs}},
        "thread": {"id": SESSION, "resolved_by": "thread",
                   "runs": [{"run_id": RUN2, "attribution": {"session_id": SESSION}}]},
        "transcript": {"session_id": SESSION, "turns": [
            {"seq": 1, "run_id": RUN1, "question": "How many people are called Alex?", "answer": "There are 12.",
             "outcome": "replied", "feedback": {"score": 1, "comment": "wrong"}, "created_at": "2026-01-01T10:00:00Z",
             "principal_id": ADMIN},
            {"seq": 2, "run_id": RUN2, "question": "Give me 3 links", "answer": "Link ⟦pii:firstname:00⟧",
             "outcome": "replied", "created_at": "2026-01-01T10:01:00Z", "principal_id": ADMIN}]},
        "search": {"sessions": [{"session_id": SESSION, "created_at": session_created,
                                 "tenant_slug": "tenant-a", "surface": "embed",
                                 "principal": {"kind": "acme_admin", "id": ADMIN}, "title": "Alex count"}]},
        "traces": {rid: [header(rid, q, a), *events()] for rid, q, a in (
            (RUN1, "How many people are called Alex?", "There are 12."),
            (RUN2, "Give me 3 links", "Link ⟦pii:firstname:00⟧"), (NEW, "x", "New answer."))},
    }
    (tmp_path / "data.json").write_text(json.dumps(data))
    log = tmp_path / "rc.log"
    log.write_text("")
    monkeypatch.setenv("RC", f"{sys.executable} {script}")
    monkeypatch.setenv("FAKE_RC_DATA", str(tmp_path / "data.json"))
    monkeypatch.setenv("FAKE_RC_LOG", str(log))
    return brain, log


def calls(log: Path, word: str) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines() if line and word in json.loads(line)]


def run(brain: Path, *argv: str) -> int:
    return cc.main(["--brain", str(brain), *argv])


def test_open_zero_run_share(tmp_path, monkeypatch, capsys):
    brain, _ = fake_env(tmp_path, monkeypatch, zero=True)
    assert run(brain, "open", SHARE) == 0
    md = (brain / ".replypen/chatdebug/11111111/case.md").read_text()
    assert cc.ZERO_RUNS in md and "Seed message" in md
    assert cc.ZERO_RUNS in capsys.readouterr().out
    assert run(brain, "replay", "11111111") == 2
    assert "Nothing to replay" in capsys.readouterr().err


def test_open_share_logged_out_then_raw_replay_needs_principal(tmp_path, monkeypatch, capsys):
    brain, log = fake_env(tmp_path, monkeypatch, logged_in=False)
    assert run(brain, "open", SHARE, "--expected", "Count, no names.") == 0
    case = json.loads((brain / ".replypen/chatdebug/11111111/case.json").read_text())
    assert [t["run_id"] for t in case["turns"]] == [RUN1, RUN2] and case["principal"] is None
    assert case["tenant"] == "tenant-a" and case["seed"] == ["Continuing from **Parent**:"]
    assert any("not logged in" in n for n in case["notes"])
    assert all("://" in c[c.index("debug") + 1] for c in calls(log, "debug"))  # share_url drills, no login
    assert run(brain, "replay", "11111111") == 2
    assert "rp auth login" in capsys.readouterr().err


def test_open_session_and_replay_chat_faithful(tmp_path, monkeypatch, capsys):
    brain, log = fake_env(tmp_path, monkeypatch, chat=True)
    assert run(brain, "open", SESSION, "--expected", "Count, no names.") == 0
    case = json.loads((brain / ".replypen/chatdebug/11111111/case.json").read_text())
    assert case["principal"] == {"kind": "acme_admin", "id": ADMIN} and case["surface"] == "embed"
    assert case["turns"][0]["feedback"]["score"] == 1 and case["turns"][1]["evidence"]["commands"]
    capsys.readouterr()
    assert run(brain, "replay", "11111111") == 0
    ask = [c for c in calls(log, "ask") if "--help" not in c][-1]
    assert ask[:3] == ["ask", "--replay-chat-run", RUN2]
    assert "--principal-kind" not in ask and "--scenario" not in ask
    assert ask[ask.index("--tenant") + 1] == "tenant-a"  # source tenant forwarded, never the ambient one
    md = next((brain / ".replypen/chatdebug/11111111/replays").glob("*-baseline.md")).read_text()
    assert cc.CHAT_FAITHFUL in md and "not chat-equivalent" not in md and "New answer." in md
    out = capsys.readouterr().out
    assert "baseline · mode chat" in out and "⟦pii" not in out and "New answer" not in out


def test_replay_raw_fallback_baseline_then_candidate(tmp_path, monkeypatch, capsys):
    brain, log = fake_env(tmp_path, monkeypatch, chat=False)
    assert run(brain, "open", SESSION) == 0
    assert run(brain, "replay", "11111111", "--turn", "2") == 0
    ask = [c for c in calls(log, "ask") if "--help" not in c][-1]
    assert ask[1].startswith("Conversation so far") and ask[1].endswith("New question:\nGive me 3 links")
    i = ask.index("--principal-kind")
    assert ask[i:i + 4] == ["--principal-kind", "acme_admin", "--principal-id", ADMIN]
    assert "--scenario" in ask and "raw" in ask and "--simulation" in ask and "--tenant" in ask
    assert run(brain, "replay", "11111111", "--turn", "2", "--brain-ref", "dev/fix",
               "--mirror-ref", "acme-common=dev/fix", "--note", "pii fix") == 0
    replays = brain / ".replypen/chatdebug/11111111/replays"
    cand = next(replays.glob("*-candidate.md")).read_text()
    assert "not chat-equivalent" in cand and "| historical (chat) | baseline " in cand and "| candidate " in cand
    rec = json.loads(next(replays.glob("*-candidate.json")).read_text())
    assert rec["source"] == {"session_id": SESSION, "turn": 2, "run_id": RUN2}
    assert rec["mirror_refs"] == ["acme-common=dev/fix"] and rec["deployed_refs"]["brain"]
    assert rec["command"][1] == "<question>" and rec["limitations"]
    stems = sorted(p.stem for p in replays.glob("*.json"))
    capsys.readouterr()
    assert run(brain, "compare", "11111111", stems[0], stems[1]) == 0
    assert next(replays.glob("compare-*.md")).read_text().count("## Answer —") == 2
    assert run(brain, "replay", "11111111", "--mode", "chat") == 2
    assert "--replay-chat-run" in capsys.readouterr().err


def test_dry_scope_and_widen(tmp_path, monkeypatch, capsys):
    brain, log = fake_env(tmp_path, monkeypatch, chat=True)
    assert run(brain, "open", SESSION) == 0
    capsys.readouterr()
    assert run(brain, "replay", "11111111", "--turn", "1", "--dry-scope") == 0
    ask = [c for c in calls(log, "ask") if "--help" not in c][-1]
    assert "--dry-scope" in ask and "--replay-chat-run" not in ask and "--principal-id" in ask
    assert "kind acme_admin" in capsys.readouterr().out
    assert run(brain, "widen", "11111111", "--tenants", "t1, t2", "--as", "regular-admin") == 0
    rows = (brain / ".replypen/chatdebug/11111111/questions-chat-11111111.tsv").read_text().splitlines()
    assert rows[0] == "id\ttenant\tquestion\tas" and len(rows) == 3
    assert rows[1].split("\t")[1] == "t1" and rows[2].split("\t")[3] == "regular-admin"
    assert "New question: Give me 3 links" in rows[1]
    out = capsys.readouterr().out
    assert '"$DL" ask --tag chat-11111111' in out and "--scenario raw --simulation" in out


def test_chat_replay_tenant_explicit_match_and_conflict(tmp_path, monkeypatch, capsys):
    brain, log = fake_env(tmp_path, monkeypatch, chat=True)
    assert run(brain, "open", SESSION) == 0
    assert run(brain, "replay", "11111111", "--tenant", "tenant-a") == 0
    ask = [c for c in calls(log, "ask") if "--help" not in c][-1]
    assert ask.count("--tenant") == 1 and ask[ask.index("--tenant") + 1] == "tenant-a"
    capsys.readouterr()
    submits = lambda: [c for c in calls(log, "ask") if "--help" not in c]
    before = len(submits())
    assert run(brain, "replay", "11111111", "--tenant", "tenant-b") == 2
    err = capsys.readouterr().err
    assert "tenant-b" in err and "tenant-a" in err and len(submits()) == before  # refused pre-submit
    assert run(brain, "replay", "11111111", "--mode", "raw", "--tenant", "tenant-b") == 0  # raw may retarget
    ask = [c for c in calls(log, "ask") if "--help" not in c][-1]
    assert ask[ask.index("--tenant") + 1] == "tenant-b"


def test_open_finds_session_created_days_before_first_question(tmp_path, monkeypatch, capsys):
    brain, log = fake_env(tmp_path, monkeypatch, session_created="2025-12-27T08:00:00Z")
    assert run(brain, "open", SESSION) == 0
    case = json.loads((brain / ".replypen/chatdebug/11111111/case.json").read_text())
    assert case["principal"] == {"kind": "acme_admin", "id": ADMIN} and case["surface"] == "embed"
    search = calls(log, "search")[-1]
    assert "--since" not in search and search[search.index("--until") + 1] == "2026-01-01"
    assert run(brain, "replay", "11111111", "--dry-scope") == 0


def test_inherited_context_provenance():
    warm = {**header(RUN2), "context_capture_origin": "inherited", "context_capture_origin_run_id": RUN1,
            "context_captured_at": "2026-01-01T10:00:01Z", "prompt_decomposition": "unavailable"}
    warm["grounding_sources"] = {"captured": False, "reason": "hot_no_source", "inherited_from_run_id": RUN1}
    ev = cc.summarise_trace(warm, events(), MD)
    lines = cc.refs_line(ev)
    assert "- prompt context: inherited from run aaaaaaaa (warm container), captured 2026-01-01T10:00:01Z" in lines
    assert any("warm turn without a captured origin" in line for line in lines)
    assert not any("inherited" in line for line in cc.refs_line(cc.summarise_trace(header(), events(), MD)))
    case = case_doc()
    turn = {**case["turns"][0], "evidence": ev}
    rec = {"stamp": "20260101T000001Z", "label": "baseline", "mode": "raw", "answer": "x", "run_id": NEW,
           "evidence": cc.summarise_trace(header(NEW, on=("persona",)), events(), MD), "limitations": []}
    md = cc.render_replay_md(case, turn, rec, None)
    assert "not comparable (prompt decomposition unavailable)" in md and "off in this replay: chat_" not in md
