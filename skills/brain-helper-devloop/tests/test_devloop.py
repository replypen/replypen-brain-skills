"""Offline tests for devloop.py: synthetic traces only (no rc, no real ids, no PII)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
sys.path.insert(0, str(HERE.parents[2] / "scope-check" / "scripts"))
import devloop
from roster import RosterError, resolve_principal

HELPER = "widget_check.py"
CASE = "skills/cases/widgets.md"
HELPER_PATH = "/mirrors/acme-common/skills/records/scripts/widget_check.py"


def header(mirror_sha="abc123def4567890"):
    return {"type": "run", "status": "done", "brain_ref": "dev/widgets",
            "brain_resolved": "dev/widgets @ 1234567890ab", "draft": "Hello,\n\nThe widget is fine.",
            "grounding_sources": {"sources": [
                {"kind": "kb", "name": "project", "commit_sha": "ffffffffffff"},
                {"kind": "mirror", "name": "acme-common", "ref": "stable", "commit_sha": mirror_sha}]}}


def ev(disp, command="", grounding=False, tool="bash", stdout="", exit_code=0, args=None):
    out = {"type": "event", "disp": disp, "tool": tool, "command": command, "grounding": grounding,
           "stdout": stdout, "stderr": None, "exit_code": exit_code}
    if args is not None:
        out["args"] = args
    return out


def trace_file(tmp_path, events, **kw):
    path = tmp_path / "abcd1234-acme.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in [header(**kw), *events]) + "\n")
    return devloop.load_trace(path)


def run(tmp_path, events, helper=HELPER, case=CASE, expect=None, **kw):
    head, evs = trace_file(tmp_path, events, **kw)
    return devloop.analyse(head, evs, helper, case, expect or {})


SELECT = ev("P3", tool="submit_selection", grounding=True,
            args={"selected": [{"path": f"/brain/{CASE}", "start": 1, "end": 40}]})


def test_called_with_args(tmp_path):
    row = run(tmp_path, [
        SELECT,
        ev("2", f"cat /brain/{CASE}"),
        ev("3", f"python {HELPER_PATH} --widget-id 42 --json 2>&1 | head -50; "
                f"python {HELPER_PATH} --widget-id 43", stdout="Verdict: ok"),
        ev("4", f"sed -n 1,40p {HELPER_PATH}"),  # reading the source is not a call
    ])
    assert row["class"] == "called"
    assert row["surfaced"] and row["opened"]
    assert row["called"] == 2
    assert row["first_call"] == {"argv": ["--widget-id", "42", "--json"], "exit": 0,
                                 "traceback": False, "disp": "3"}
    assert row["brain"] == "dev/widgets @ 1234567890ab"
    assert row["answer_head"].startswith("Hello, The widget")


def test_opened_not_called(tmp_path):
    row = run(tmp_path, [SELECT, ev("2", f"nl -ba /brain/{CASE} | sed -n 1,80p")])
    assert (row["class"], row["opened"], row["called"]) == ("opened-not-called", True, 0)


def test_surfaced_not_opened(tmp_path):
    row = run(tmp_path, [ev("P1", f"sed -n 1,120p /brain/{CASE}", grounding=True),
                         ev("2", "rg -n widget /kb")])
    assert row["class"] == "surfaced-not-opened"
    assert row["surfaced"] and not row["opened"]


def test_not_surfaced(tmp_path):
    row = run(tmp_path, [ev("P1", "cat /tmp/rc-context/kb-tree.md", grounding=True),
                         ev("2", "rg -n widget /kb")])
    assert row["class"] == "not-surfaced"


def test_no_helper_given(tmp_path):
    row = run(tmp_path, [SELECT], helper=None)
    assert row["class"] == "no-helper-given"


def test_traceback_in_stdout(tmp_path):
    row = run(tmp_path, [ev("2", f"timeout 45 python3 {HELPER_PATH} --name 'Big Camp' > /tmp/o.txt",
                            stdout="Traceback (most recent call last):\n  ...", exit_code=1)])
    assert row["class"] == "called"
    assert row["first_call"]["argv"] == ["--name", "Big Camp"]
    assert row["first_call"]["traceback"] is True and row["first_call"]["exit"] == 1


def test_expect_mirror_mismatch(tmp_path):
    rows_ok = run(tmp_path, [], expect={"acme-common": "abc123def456"})
    assert rows_ok["mirror_check"]["acme-common"]["ok"] is True
    bad = run(tmp_path, [], expect={"acme-common": "0000000aaaaa", "other": "1111111"})
    assert bad["mirror_check"]["acme-common"]["ok"] is False
    assert bad["mirror_check"]["other"] == {"expected": "1111111", "got": None, "ok": False}
    rows = [{"id": "q1", "turns": 3, **bad}]
    table = devloop.render_markdown(rows, {"acme-common": "0000000aaaaa"})
    assert "acme-common=abc123def456 ✗" in table
    assert devloop.summary(rows, {"x": "y"}).endswith("mirror ✓ 0/1")


def test_questions_tsv_with_as_column():
    text = "id\ttenant\tquestion\tas\n# comment\nq1\tacme\tWhy is widget 42 red?\tregular-admin\n" \
           "q2\t\tHow do I add a widget?\n"
    rows = devloop.parse_questions(text)
    assert rows == [
        {"id": "q1", "tenant": "acme", "question": "Why is widget 42 red?", "as": "regular-admin"},
        {"id": "q2", "tenant": "", "question": "How do I add a widget?", "as": ""}]
    with pytest.raises(devloop.DevloopError, match="duplicate"):
        devloop.parse_questions("q1\ta\tx\nq1\ta\ty\n")
    with pytest.raises(devloop.DevloopError, match="expected id"):
        devloop.parse_questions("q1\tonly-two-columns\n")


def test_roster_resolution(tmp_path):
    brain, mirror = tmp_path / "brain", tmp_path / "mirror"
    (mirror / "_internal").mkdir(parents=True)
    brain.mkdir()
    (brain / ".replypen.toml").write_text('project = "acme-support"\n[mirrors]\n"acme-common" = "../mirror"\n')
    (mirror / "_internal" / "scopecheck-acme-support.toml").write_text(
        '[[audience]]\nlabel = "regular-admin"\nkind = "acme_admin"\nid = "a-1"\n')
    assert resolve_principal("regular-admin", brain, "acme-support") == ("acme_admin", "a-1")
    assert resolve_principal("acme_admin:a-2", brain, "acme-support") == ("acme_admin", "a-2")
    assert resolve_principal("boss=acme_admin:a-3", brain, "acme-support") == ("acme_admin", "a-3")
    with pytest.raises(RosterError, match="known labels: regular-admin"):
        resolve_principal("super-admin", brain, "acme-support")


@pytest.mark.parametrize("command", [
    f"cat <<'EXAMPLE'\npython {HELPER_PATH} --id 42\nEXAMPLE",
    f"cat <<EOF > notes.md\npython {HELPER_PATH} --id 42\nEOF",
    f"cat <<-EOF\n\tpython {HELPER_PATH} --id 42\n\tEOF",
    f"HELPER={HELPER_PATH}",
])
def test_heredoc_body_and_assignment_are_not_calls(command):
    assert devloop.helper_calls(command, HELPER) == []


def test_calls_around_heredocs_and_assignments_still_count():
    command = (f"cat <<'EOF'\npython {HELPER_PATH} --id 1\nEOF\npython {HELPER_PATH} --id 2\n"
               f"FOO=1 python {HELPER_PATH} --id 3\npython {HELPER_PATH} a=b")
    assert devloop.helper_calls(command, HELPER) == [["--id", "2"], ["--id", "3"], ["a=b"]]


def test_empty_quoted_argument_is_kept():
    assert devloop.helper_calls(f'python {HELPER} --name "" --json', HELPER) == [["--name", "", "--json"]]


def test_ask_resumes_submission_after_poll_failure(tmp_path, monkeypatch):
    args = devloop.build_parser().parse_args(["ask", "--tag", "t", "--questions", "x", "--poll", "0"])
    row = {"id": "q1", "tenant": "", "question": "A question", "as": ""}
    runs, calls = tmp_path / "runs", []

    def failing_poll(command, root, timeout=180):
        calls.append(command)
        if command[0] == "ask":
            return {"run_id": "r1"}
        raise devloop.DevloopError("temporary poll failure")
    monkeypatch.setattr(devloop, "rc_json", failing_poll)
    with pytest.raises(devloop.DevloopError):
        devloop.run_one(row, args, tmp_path, runs, None)
    assert (runs / "q1.submit.json").exists()

    def resumed(command, root, timeout=180):
        calls.append(command)
        return {"run_id": "r1", "status": "done"}
    monkeypatch.setattr(devloop, "rc_json", resumed)
    assert devloop.run_one(row, args, tmp_path, runs, None)["status"] == "done"
    assert sum(command[0] == "ask" for command in calls) == 1


def test_ask_refuses_to_resume_a_changed_question(tmp_path, monkeypatch):
    questions = tmp_path / "q.tsv"
    questions.write_text("q1\tacme\tSame question\nq2\tacme\tThe new question\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    for qid, question in (("q1", "Same question"), ("q2", "The old question")):
        devloop.write_json(scratch / "runs" / f"{qid}.submit.json",
                           {"id": qid, "run_id": f"old-{qid}", "tenant": "acme", "as": None, "brain_ref": None,
                            "mirror_ref": [], "scenario": "email", "simulation": False, "question": question})
        devloop.write_json(scratch / "runs" / f"{qid}.json", {"status": "done"})
    monkeypatch.setattr(devloop, "rc_json", lambda *a, **k: pytest.fail("must not call rc"))

    def ask(*extra):
        args = devloop.build_parser().parse_args(["ask", "--tag", "t", "--questions", str(questions), *extra])
        args.scenario = "email"
        devloop.cmd_ask(args, tmp_path, scratch)

    with pytest.raises(devloop.DevloopError, match="q2: question changed"):  # even when not selected
        ask("--only", "q1", "--force")
    assert (scratch / "runs" / "q1.json").exists()  # validated before any forced receipt is deleted
    (scratch / "runs" / "q2.submit.json").unlink()
    with pytest.raises(devloop.DevloopError, match="q1: simulation changed"):
        ask("--simulation")
    assert not (scratch / "questions.json").exists()
