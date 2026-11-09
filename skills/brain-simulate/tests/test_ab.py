"""Offline tests for ab.py: trace parsing, blinding/unblinding, statistics. No network, no `rp`."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ab  # noqa: E402


def make_run(scratch: Path, run_id: str, case: str, arm: str, rep: int, answer: str = "Dag,\n\nDat kan.",
             events: list | None = None) -> dict:
    rec = {"case": case, "stratum": "sql", "arm": arm, "rep": rep, "run_id": run_id, "status": "done", "error": ""}
    with (scratch / "runs.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    (scratch / "runs").mkdir(exist_ok=True)
    (scratch / "runs" / f"{run_id}.json").write_text(json.dumps(
        {"run_id": run_id, "status": "done", "outcome": "answered", "bash_total": 3, "turns": 4,
         "answer_markdown": answer, "run_url": f"https://x/runs/{run_id}"}))
    (scratch / "traces").mkdir(exist_ok=True)
    lines = [{"type": "run", "question": "hoeveel deelnemers?", "answer_markdown": answer,
              "prior_messages": [{"is_inbound": True, "body": f"m{i}"} for i in range(8)],
              "system_prompt": "s" * 10, "bootstrap_turn": "b" * 20}] + (events or [])
    (scratch / "traces" / f"{run_id}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    return rec


def test_view_counts_real_sql_errors_only_and_shows_action_cards(tmp_path):
    events = [
        {"type": "event", "tool": "bash", "command": "cat /mirrors/x/docs/schema.md", "stdout": "UndefinedTable is a boundary"},
        {"type": "event", "tool": "bash", "command": "python - <<'PY'\nfrom lib import db\nprint(db.query('x'))\nPY",
         "stdout": "", "stderr": 'psycopg.errors.UndefinedColumn: column "foo" does not exist'},
        {"type": "event", "tool": "bash", "command": "python - <<'PY'\nfrom lib import db\nPY", "stdout": "ok"},
        {"type": "event", "tool": "preflight", "args": {"action_id": "update_record_field"},
         "stdout": json.dumps({"ok": True, "summary": "150 personen · tag toevoegen\n- id1"})},
        {"type": "event", "tool": "action", "args": {"action_id": "update_record_field", "intent": "Tag zetten"}},
    ]
    make_run(tmp_path, "r1", "c1", "old", 0, events=events)
    v = ab.view(tmp_path, ab.done_runs(tmp_path)[0])
    assert (v["sql_calls"], v["sql_errs"]) == (2, 1)
    assert v["cards"] == ["update_record_field: Tag zetten [preview: 150 personen · tag toevoegen]"]
    out = ab.render_output(v)
    assert "ACTION CARD" in out and "Dat kan." in out
    turn = ab.render_turn(v)
    assert "(2 earlier messages omitted)" in turn and turn.endswith("hoeveel deelnemers?")


def test_answers_are_never_truncated(tmp_path):
    long = "x" * 20000
    make_run(tmp_path, "r1", "c1", "old", 0, answer=long)
    v = ab.view(tmp_path, ab.done_runs(tmp_path)[0])
    assert long in ab.render_output(v)


def test_within_arm_pairs_and_cross_arm_pairs():
    runs = {"a": [{"rep": i} for i in range(3)], "b": [{"rep": i} for i in range(2)]}
    assert [(x["rep"], y["rep"]) for x, y in ab.pairs_for(runs, "a", "a", random.Random(1))] == [(0, 1)]
    assert len(ab.pairs_for(runs, "a", "b", random.Random(1))) == 2


def pack_and_judge(tmp_path, judge_fn):
    for case in ("c1", "c2", "c3", "c4"):
        for arm in ("old", "new"):
            make_run(tmp_path, f"{case}-{arm}", case, arm, 0)
    args = SimpleNamespace(scratch=str(tmp_path), a="old", b="new", seed=7, plants=0, force=False)
    assert ab.cmd_pack(args) == 0
    cmp = tmp_path / "cmp-old-new"
    key = json.loads((cmp / "key.json").read_text())["pairs"]
    for n in ("1", "2"):
        lines = []
        for pid, k in key.items():
            x_is_a = (k["pack1_x"] == "a") == (n == "1")
            lines.append(json.dumps({"pair": pid, **judge_fn(k, x_is_a, n)}))
        (cmp / f"judge-t-{n}.jsonl").write_text("\n".join(lines))
    assert ab.cmd_verdict(SimpleNamespace(**vars(args), null=None, judges=None, margin=0.15)) == 0
    return json.loads((cmp / "verdict.json").read_text())["all"]


def test_unblinding_consistent_judge_prefers_new(tmp_path):
    s = pack_and_judge(tmp_path, lambda k, x_is_a, n: {"better": "Y" if x_is_a else "X", "margin": "clear"})
    assert s["b_cases"] and len(s["b_cases"]) == 4 and not s["a_cases"]
    assert s["swap_agreement"] == 1.0 and s["ci90"][0] > 0


def test_position_biased_judge_cancels_out(tmp_path):
    s = pack_and_judge(tmp_path, lambda k, x_is_a, n: {"better": "X", "margin": "clear"})   # always picks X
    assert s["mean_net"] == 0 and s["swap_agreement"] == 0.0 and s["x_rate"] == 1.0
    assert s["case_win_rate"] == 0


def test_pack_refuses_untraced_runs(tmp_path):
    make_run(tmp_path, "r1", "c1", "old", 0)
    make_run(tmp_path, "r2", "c1", "new", 0)
    (tmp_path / "traces" / "r2.jsonl").unlink()
    with pytest.raises(ab.ABError, match="collect"):
        ab.cmd_pack(SimpleNamespace(scratch=str(tmp_path), a="old", b="new", seed=1, plants=0, force=False))


def test_sign_test():
    assert ab.sign_test(0, 0) == 1.0
    assert ab.sign_test(5, 5) == 1.0
    assert ab.sign_test(8, 0) == pytest.approx(2 / 256)


def test_plants_are_scored_apart_from_real_pairs(tmp_path):
    long = "Dag,\n\n" + "Je vindt dit onder Deelnemers. " * 20
    for case in ("c1", "c2", "c3", "c4"):
        for arm in ("old", "new"):
            make_run(tmp_path, f"{case}-{arm}", case, arm, 0, answer=long)
    args = SimpleNamespace(scratch=str(tmp_path), a="old", b="new", seed=3, plants=2, force=False)
    assert ab.cmd_pack(args) == 0
    cmp = tmp_path / "cmp-old-new"
    key = json.loads((cmp / "key.json").read_text())["pairs"]
    plants = {pid: k for pid, k in key.items() if k["plant"]}
    assert {k["plant"]["kind"] for k in plants.values()} == {"truncate", "leak"}
    assert "subscription_line_items" in (cmp / "pack-1.md").read_text()
    for n in ("1", "2"):   # judge: real pairs "same"; plants: prefers the intact side
        lines = []
        for pid, k in key.items():
            better = "same"
            if k["plant"]:
                x_is_a = (k["pack1_x"] == "a") == (n == "1")
                degraded_is_x = (k["plant"]["degraded"] == "a") == x_is_a
                better = "Y" if degraded_is_x else "X"
            lines.append(json.dumps({"pair": pid, "better": better, "margin": "clear" if k["plant"] else "none"}))
        (cmp / f"judge-t-{n}.jsonl").write_text("\n".join(lines))
    assert ab.cmd_verdict(SimpleNamespace(**vars(args), null=None, judges=None, margin=0.15)) == 0
    v = json.loads((cmp / "verdict.json").read_text())
    assert v["all"]["judgments"] == 8 and v["all"]["decisive_rate"] == 0
    assert v["by_judge"]["t"]["plants"] == {"judged": 4, "caught": 4, "clear": 4}


def test_partial_or_duplicate_judge_file_is_refused(tmp_path):
    pack_and_judge(tmp_path, lambda k, x_is_a, n: {"better": "same", "margin": "none"})
    cmp = tmp_path / "cmp-old-new"
    path = cmp / "judge-t-1.jsonl"
    lines = path.read_text().splitlines()
    args = SimpleNamespace(scratch=str(tmp_path), a="old", b="new", seed=7, null=None, judges=None, margin=0.15)
    path.write_text("\n".join(lines[:1]))
    with pytest.raises(ab.ABError, match="missing"):
        ab.cmd_verdict(args)
    path.write_text("\n".join(lines + lines[:1]))
    with pytest.raises(ab.ABError, match="judged twice"):
        ab.cmd_verdict(args)


def test_repack_refuses_to_orphan_judgments_and_detects_edited_pack(tmp_path):
    pack_and_judge(tmp_path, lambda k, x_is_a, n: {"better": "same", "margin": "none"})
    args = SimpleNamespace(scratch=str(tmp_path), a="old", b="new", seed=8, plants=0, force=False)
    with pytest.raises(ab.ABError, match="re-map"):
        ab.cmd_pack(args)
    cmp = tmp_path / "cmp-old-new"
    (cmp / "pack-1.md").write_text("edited")
    with pytest.raises(ab.ABError, match="changed"):
        ab.cmd_verdict(SimpleNamespace(**vars(args), null=None, judges=None, margin=0.15))
    assert ab.cmd_pack(SimpleNamespace(**{**vars(args), "force": True})) == 0
    assert not list(cmp.glob("judge-*.jsonl"))


def test_run_refuses_reusing_an_arm_name_for_other_flags(tmp_path):
    make_run(tmp_path, "r1", "c1", "new", 0)
    ledger = tmp_path / "runs.jsonl"
    rec = json.loads(ledger.read_text())
    ledger.write_text(json.dumps({**rec, "flags": ["--brain-ref", "dev/v1"]}) + "\n")
    (tmp_path / "ids.txt").write_text("c1\n")
    args = SimpleNamespace(scratch=str(tmp_path), cases=str(tmp_path / "ids.txt"), arm=["new=--brain-ref dev/v2"],
                           reps=1, parallel=1, retries=0, timeout="1m", project=None, seed=1)
    with pytest.raises(ab.ABError, match="already ran"):
        ab.cmd_run(args)
