"""Offline tests for brain-simulate. No network, no `rp`, no git checkout required.

    cd skills/brain-simulate && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import simulate as sim  # noqa: E402

FIXTURES = ROOT / "fixtures"
ASK_PAYLOAD = {
    "attachments": [], "bash_total": 5, "category": "ok", "created_at": "2026-03-06T08:59:00Z",
    "draft_markdown": "Dag,\n\nDat kan zeker.", "duration_ms": 21324,
    "finished_at": "2026-03-06T09:00:00Z", "has_draft": True, "has_note": False, "kind": "prompt",
    "metadata": {"iterations": 3, "outcome": "replied",
                 "run_url": "https://app.replypen.com/runs/abc?t=xyz"},
    "outcome": "answered", "run_id": "abc", "run_url": "https://app.replypen.com/runs/abc?t=xyz",
    "scenario": "email", "session_id": "s1", "simulation": True, "status": "done",
    "thread_id": "prompt-1", "turns": 5,
}


def cli(tmp_path: Path, *args: str) -> int:
    return sim.main(["--brain", str(tmp_path), "--scratch", str(tmp_path / "sc"),
                     "--tag", "demo", *args])


def scratch(tmp_path: Path) -> Path:
    return tmp_path / "sc"


def load(tmp_path: Path, name: str):
    return json.loads((scratch(tmp_path) / name).read_text(encoding="utf-8"))


def acquire_helpscout(tmp_path: Path) -> list[dict]:
    assert cli(tmp_path, "acquire", "--source", "helpscout",
               "--pages", str(FIXTURES / "helpscout_page.json")) == 0
    return load(tmp_path, "cases.json")


def seed(tmp_path: Path) -> list[str]:
    """Acquire + select the three keepable Help Scout cases."""
    cases = acquire_helpscout(tmp_path)
    picks = []
    for case, (tag, difficulty) in zip(cases, [("mail-delivery", "medium"), ("how-to", "easy"),
                                               ("booking-config", "hard")]):
        picks += ["--pick", f"{case['id']}|{tag}|{difficulty}|representatief voor {tag}"]
    assert cli(tmp_path, "select", *picks) == 0
    return [c["id"] for c in cases]


def write_artifacts(tmp_path: Path, ids: list[str], ref: str = "main", *,
                    fail_last: bool = False) -> None:
    slug = sim.ref_slug(ref)
    run = json.loads((FIXTURES / "run_result.json").read_text())
    score = json.loads((FIXTURES / "score.json").read_text())
    for index, cid in enumerate(ids):
        record = dict(run, case_id=cid, ref=ref)
        entry = dict(score, case_id=cid, ref=ref)
        if fail_last and index == len(ids) - 1:
            record.update(status="error", error="rc: brain boot failed", run_url=None,
                          draft_markdown="")
        sim.write_json(scratch(tmp_path) / "runs" / slug / f"{cid}.json", record)
        if not (fail_last and index == len(ids) - 1):
            sim.write_json(scratch(tmp_path) / "scores" / slug / f"{cid}.json", entry)
    recommendations = json.loads((FIXTURES / "recommendations.json").read_text())
    recommendations["recommendations"][0]["cases"] = [ids[0]]
    sim.write_json(scratch(tmp_path) / "recommendations.json", recommendations)
    sim.write_json(scratch(tmp_path) / "persona.json",
                   {"captured_at": "2026-03-06T08:00:00Z",
                    "persona": {"tone": {"source": "project", "value": "warm, kort"}}})


# --- acquire --------------------------------------------------------------------------------

def test_acquire_helpscout_normalizes_sorts_and_drops(tmp_path, capsys):
    cases = acquire_helpscout(tmp_path)
    record = load(tmp_path, "acquire.json")
    assert record["input_count"] == 5 and record["kept"] == 3
    assert record["dropped"] == {"inbound_too_short": 1, "no_customer_turn_before_reply": 1}

    assert [c["created_at"] for c in cases] == sorted(c["created_at"] for c in cases)
    by_ref = {c["source_ref"]: c for c in cases}
    assert set(by_ref) == {"101", "102", "103"}

    burst = by_ref["101"]
    assert len(burst["inbound"]) == 2
    assert burst["inbound"][0]["at"] < burst["inbound"][1]["at"]  # file order was newest-first
    assert burst["human_agent"] == "Sanne" and burst["customer_first"] == "Lien"
    assert burst["channel"] == "email"
    assert [t["role"] for t in burst["later_turns"]] == ["customer"]
    assert "lineitem" not in json.dumps(burst)

    assert by_ref["102"]["channel"] == "chat"
    assert by_ref["102"]["meta"]["tags"] == ["agenda"]

    notes = by_ref["103"]["meta"]["notes"]
    assert notes == ["Klant belde eerder al over hetzelfde."]  # Technical Information dropped


def test_case_ids_are_content_derived_and_stable(tmp_path):
    first = [c["id"] for c in acquire_helpscout(tmp_path)]
    second = [c["id"] for c in acquire_helpscout(tmp_path)]
    assert first == second
    assert all(cid.startswith("S") and len(cid) == 17 for cid in first)


def test_acquire_append_dedupes(tmp_path):
    acquire_helpscout(tmp_path)
    assert cli(tmp_path, "acquire", "--source", "helpscout", "--append",
               "--pages", str(FIXTURES / "helpscout_page.json")) == 0
    assert len(load(tmp_path, "cases.json")) == 3


def test_acquire_harvest_v3(tmp_path):
    assert cli(tmp_path, "acquire", "--source", "harvest",
               "--corpus", str(FIXTURES / "harvest_v3.md")) == 0
    cases = load(tmp_path, "cases.json")
    assert [c["subject"] for c in cases] == ["Openingsuren tussen kerst en nieuwjaar",
                                             "Cadeaubon verlengen"]
    assert [len(c["inbound"]) for c in cases] == [1, 2]
    assert all(c["source"] == "harvest" and c["channel"] == "email" for c in cases)
    assert cases[1]["human_reply"].startswith("Dag, cadeaubonnen verlengen")


# --- plan / select --------------------------------------------------------------------------

def test_plan_is_deterministic_for_a_seed(tmp_path):
    acquire_helpscout(tmp_path)
    assert cli(tmp_path, "plan", "--target", "3") == 0
    first = (scratch(tmp_path) / "candidates.md").read_text()
    assert cli(tmp_path, "plan", "--target", "3") == 0
    assert (scratch(tmp_path) / "candidates.md").read_text() == first
    assert "seed 7" in first and "Pick **3**" in first

    candidates = load(tmp_path, "candidates.json")
    hints = {c["id"]: c["hints"] for c in candidates["cases"]}
    multi = [cid for cid, h in hints.items() if h["multi_turn"]]
    assert len(multi) == 1
    ordered = [c["id"] for c in candidates["cases"]]
    assert cli(tmp_path, "plan", "--seed", "99", "--target", "3") == 0
    assert sorted(c["id"] for c in load(tmp_path, "candidates.json")["cases"]) == sorted(ordered)


def test_plan_validates_an_existing_selection(tmp_path, capsys):
    ids = seed(tmp_path)
    assert cli(tmp_path, "plan") == 0
    assert "type coverage" in capsys.readouterr().out
    bad = load(tmp_path, "selection.json")
    bad["cases"][0]["reason"] = ""
    bad["cases"][1]["difficulty"] = "brutal"
    sim.write_json(scratch(tmp_path) / "selection.json", bad)
    assert cli(tmp_path, "plan") == 1
    assert ids  # sanity


def test_select_rejects_unknown_id(tmp_path):
    acquire_helpscout(tmp_path)
    assert cli(tmp_path, "select", "--pick", "Sdeadbeef|how-to|easy|bestaat niet") == 1
    assert not (scratch(tmp_path) / "selection.json").exists()


# --- run ------------------------------------------------------------------------------------

def test_dry_run_builds_the_expected_command(tmp_path, capsys):
    ids = seed(tmp_path)
    assert cli(tmp_path, "run", "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.count("rp ask ") == 3
    assert out.count("--simulation") == 3
    assert "--brain-ref" not in out and "--session" not in out
    assert "--scenario email" in out

    command = sim.build_command({"id": ids[0], "subject": "Onderwerp",
                                 "inbound": [{"at": None, "text": "Hallo daar"}]},
                                "dev/x", "5m", None)
    assert command[:3] == ["rp", "ask", "Hallo daar"]
    assert command[-2:] == ["--brain-ref", "dev/x"]
    assert "--simulation" in command and "--session" not in command
    assert command[command.index("--from") + 1] == f"simulate-{ids[0][:8].lower()}@example.test"


FIXTURE_CASE = {
    "id": "fx-annulatie", "tenant": "demo", "from": "sim@example.com", "subject": "Re: annulatie",
    "inbound": "Doe maar zo, attest volgt.\n\n> Sarah: €25 kost, €125 spaarpot, €25 terug op attest",
    "human_reply": "Dag Lotte, geannuleerd: €25 kost, €125 in je spaarpot, €25 terug op attest.",
    "expect": {"draft_matches": ["€\\s?25", "attest"], "draft_forbids": ["€\\s?20"],
               "note_matches": ["attest"],
               "actions": [{"slug": "cancel_subscription", "description_matches": ["€25", "€125"]}]},
}
FIXTURE_PAYLOAD = dict(
    ASK_PAYLOAD, draft_markdown="Dag Lotte, geannuleerd: €25 kosten, €125 tegoed. Attest → €25 terug.",
    notes=[{"key": "summary", "body": "- 👀 attest opvolgen"}, {"key": "proposals", "body": ""}],
    proposed_actions=[{"id": "p1", "slug": "cancel_subscription", "label": "Inschrijving annuleren",
                       "description": "Finn · annuleren, €25 kosten, €125 tegoed",
                       "resource_url": "https://admin.example/subscriptions/1"}])


def acquire_fixture(tmp_path: Path, case: dict) -> dict:
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps([case]), encoding="utf-8")
    assert cli(tmp_path, "acquire", "--source", "manual", "--cases", str(path)) == 0
    return load(tmp_path, "cases.json")[0]


def test_fixture_case_carries_sender_tenant_and_expect(tmp_path):
    case = acquire_fixture(tmp_path, FIXTURE_CASE)
    assert case["id"] == "fx-annulatie" and case["expect"]["actions"][0]["slug"] == "cancel_subscription"
    command = sim.build_command(case, "dev/x", "5m", "global@example.test")
    assert command[command.index("--from") + 1] == "sim@example.com"
    assert command[command.index("--tenant") + 1] == "demo"


def test_fixture_checks_pass_and_fail(tmp_path):
    case = acquire_fixture(tmp_path, FIXTURE_CASE)
    record = sim.result_record(case, "main", FIXTURE_PAYLOAD, 1, ["rp", "ask", "q"])
    assert record["note_markdown"] == "- 👀 attest opvolgen"
    assert record["proposed_actions"][0]["description"].endswith("€125 tegoed")
    assert all(c["ok"] for c in record["checks"]) and sim.checks_label(record) == "✓ 6/6"

    worse = dict(FIXTURE_PAYLOAD, draft_markdown="Dag Lotte, €20 korting vervalt.",
                 proposed_actions=FIXTURE_PAYLOAD["proposed_actions"] + [
                     {"slug": "adjust_subscription_pricing", "description": "+€20"}])
    failed = {c["check"] for c in sim.result_record(case, "main", worse, 1, ["rp"])["checks"]
              if not c["ok"]}
    assert failed == {"draft has /€\\s?25/", "draft has /attest/", "draft lacks /€\\s?20/",
                      "no unlisted action proposals"}


def test_fixture_outcome_and_identity_stop(tmp_path, monkeypatch):
    case = acquire_fixture(tmp_path, dict(FIXTURE_CASE, expect={"outcome": "draft", "decline_kind": "none"}))
    monkeypatch.setattr(sim, "reply_decline", lambda run_id, tenant: ("", ""))
    assert all(c["ok"] for c in sim.result_record(case, "main", FIXTURE_PAYLOAD, 1, ["rp"])["checks"])

    stopped = dict(FIXTURE_PAYLOAD, draft_markdown="", outcome="declined")
    monkeypatch.setattr(sim, "reply_decline", lambda run_id, tenant: ("identity_unresolved", "kind niet gekoppeld"))
    failed = {c["check"]: c["detail"] for c in sim.result_record(case, "main", stopped, 1, ["rp"])["checks"]
              if not c["ok"]}
    assert failed == {"outcome draft": "got decline", "decline_kind none": "got identity_unresolved: kind niet gekoppeld"}

    case["expect"] = {"outcome": "decline", "decline_kind": "identity_unresolved"}
    assert all(c["ok"] for c in sim.result_record(case, "main", stopped, 1, ["rp"])["checks"])


def test_fixture_outcome_is_validated(tmp_path):
    bad = dict(FIXTURE_CASE, expect={"outcome": "maybe", "decline_kind": "unresolved"})
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(sim.SimError, match="outcome must be.*decline_kind must be"):
        sim.acquire_manual(str(path))


def test_pass_score_contradicted_by_failing_checks_is_rejected(tmp_path, capsys):
    case = acquire_fixture(tmp_path, FIXTURE_CASE)
    assert cli(tmp_path, "select", "--pick", f"{case['id']}|cancellation|hard|fixture") == 0
    worse = dict(FIXTURE_PAYLOAD, draft_markdown="Dag Lotte, €20 korting vervalt.")
    sim.write_json(scratch(tmp_path) / "runs" / "main" / f"{case['id']}.json",
                   sim.result_record(case, "main", worse, 1, ["rp"]))
    score = json.loads((FIXTURES / "score.json").read_text())
    sim.write_json(scratch(tmp_path) / "scores" / "main" / f"{case['id']}.json",
                   dict(score, case_id=case["id"], verdict="pass"))
    assert cli(tmp_path, "score", "--validate") != 0
    assert "fixture checks fail" in capsys.readouterr().err
    assert cli(tmp_path, "report") == 0
    assert "| unscored ✗" in (scratch(tmp_path) / "report.md").read_text()


def test_fixture_expect_is_validated(tmp_path):
    bad = dict(FIXTURE_CASE, expect={"draft_matches": ["(unclosed"], "typo": []})
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(sim.SimError, match="unknown key 'typo'.*bad regex"):
        sim.acquire_manual(str(path))


def test_multi_turn_question_joins_with_a_rule(tmp_path):
    cases = acquire_helpscout(tmp_path)
    burst = next(c for c in cases if len(c["inbound"]) == 2)
    assert "\n\n---\n\n" in sim.build_question(burst)


def test_run_skips_done_cases_unless_forced(tmp_path, capsys):
    ids = seed(tmp_path)
    sim.write_json(scratch(tmp_path) / "runs" / "main" / f"{ids[0]}.json",
                   {"case_id": ids[0], "status": "done"})
    assert cli(tmp_path, "run", "--dry-run") == 0
    assert capsys.readouterr().out.count("rp ask ") == 2
    assert cli(tmp_path, "run", "--dry-run", "--force") == 0
    assert capsys.readouterr().out.count("rp ask ") == 3


def test_run_retries_transient_failures_and_snapshots_persona(tmp_path, monkeypatch):
    ids = seed(tmp_path)
    target = ids[0]
    asks: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if command[:4] == ["rp", "project", "settings", "behavior"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"resolved": {"persona": {"tone": {"source": "project", "value": "warm"}}}}))
        asks.append(command)
        if len(asks) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="upstream timeout, try again")
        return SimpleNamespace(returncode=0, stdout=json.dumps(ASK_PAYLOAD), stderr="")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    assert cli(tmp_path, "run", "--only", target, "--parallel", "1") == 0

    record = json.loads((scratch(tmp_path) / "runs" / "main" / f"{target}.json").read_text())
    assert record["attempts"] == 2 and record["status"] == "done"
    assert record["run_id"] == "abc" and record["metadata_outcome"] == "replied"
    assert record["turns"] == 5 and record["duration_ms"] == 21324
    assert len(record["command"][2]) <= 80  # the question is elided for reproducibility only
    assert len(asks) == 2 and all("--simulation" in a for a in asks)

    persona = load(tmp_path, "persona.json")
    assert persona["persona"]["tone"]["value"] == "warm"


def test_run_records_a_permanent_failure(tmp_path, monkeypatch):
    ids = seed(tmp_path)
    target = ids[1]

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stderr="", stdout="not json at all")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    assert cli(tmp_path, "run", "--only", target, "--parallel", "1", "--no-persona") == 1
    record = json.loads((scratch(tmp_path) / "runs" / "main" / f"{target}.json").read_text())
    assert record["status"] == "error" and record["attempts"] == 1
    assert "not json" in record["error"]


# --- score ----------------------------------------------------------------------------------

def test_score_writes_bundles_and_validates(tmp_path, capsys):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids)
    assert cli(tmp_path, "score") == 0
    bundle = (scratch(tmp_path) / "judge" / "main" / f"{ids[0]}.md").read_text()
    assert "## Inbound" in bundle and "## Human answered" in bundle and "## Our draft" in bundle
    assert "Persona settings in effect" in bundle and "warm, kort" in bundle
    assert "https://app.replypen.com/runs/" in bundle
    assert cli(tmp_path, "score", "--validate") == 0


def test_score_validate_rejects_bad_range_and_enum(tmp_path):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids)
    path = scratch(tmp_path) / "scores" / "main" / f"{ids[0]}.json"
    broken = json.loads(path.read_text())
    broken["scores"]["content"] = 9
    sim.write_json(path, broken)
    assert cli(tmp_path, "score", "--validate") == 1

    broken["scores"]["content"] = 3
    broken["verdict"] = "meh"
    sim.write_json(path, broken)
    assert cli(tmp_path, "score", "--validate") == 1

    broken["verdict"] = "pass"
    broken["lever"] = "vibes"
    sim.write_json(path, broken)
    assert cli(tmp_path, "score", "--validate") == 1


def test_score_validate_rejects_bad_recommendations(tmp_path):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids)
    recommendations = load(tmp_path, "recommendations.json")
    recommendations["recommendations"][0]["cases"] = ["Snope"]
    sim.write_json(scratch(tmp_path) / "recommendations.json", recommendations)
    assert cli(tmp_path, "score", "--validate") == 1


def test_score_validate_tolerates_a_failed_run(tmp_path):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids, fail_last=True)
    assert cli(tmp_path, "score", "--validate") == 0


# --- report ---------------------------------------------------------------------------------

def test_report_renders_html_and_markdown(tmp_path):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids, fail_last=True)
    assert cli(tmp_path, "report") == 0

    html = (scratch(tmp_path) / "report.html").read_text()
    markdown = (scratch(tmp_path) / "report.md").read_text()
    verdict = load(tmp_path, "recommendations.json")["verdict_line"]
    assert verdict in html and verdict in markdown
    assert "https://app.replypen.com/runs/" in html and "https://app.replypen.com/runs/" in markdown
    assert "button class=\"copy\"" in html and "navigator.clipboard" in html
    assert "http://" not in html.replace("https://", "")  # no CDN, no external fonts
    lowered = html.lower()
    assert "token" not in lowered and "cost" not in lowered
    assert "nothing was sent" in lowered and "brain boot failed" in html
    # worst first: the failed run precedes the scored ones
    assert html.index(ids[-1]) < html.index(ids[0])


def test_report_history_dedupes_on_tag_and_ref(tmp_path):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids)
    assert cli(tmp_path, "report") == 0
    assert cli(tmp_path, "report") == 0
    lines = [json.loads(line) for line in
             (tmp_path / "history.jsonl").read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["tag"] == "sc" and lines[0]["ref"] == "main"
    assert lines[0]["n_cases"] == 3 and lines[0]["pass_rate"] == 0.0
    assert lines[0]["mean_scores"]["content"] == 3.0

    write_artifacts(tmp_path, ids, ref="dev/x")
    assert cli(tmp_path, "report", "--ref", "dev/x") == 0
    lines = [json.loads(line) for line in
             (tmp_path / "history.jsonl").read_text().splitlines() if line.strip()]
    assert {(item["tag"], item["ref"]) for item in lines} == {("sc", "main"), ("sc", "dev/x")}
    assert (scratch(tmp_path) / "report-dev--x.html").exists()


def test_report_compare_produces_a_delta_table(tmp_path):
    ids = seed(tmp_path)
    write_artifacts(tmp_path, ids)
    write_artifacts(tmp_path, ids, ref="dev/x")
    better = json.loads((scratch(tmp_path) / "scores" / "dev--x" / f"{ids[0]}.json").read_text())
    better["scores"]["format"] = 4
    better["verdict"] = "pass"
    sim.write_json(scratch(tmp_path) / "scores" / "dev--x" / f"{ids[0]}.json", better)

    assert cli(tmp_path, "report", "--compare", "dev/x") == 0
    html = (scratch(tmp_path) / "report.html").read_text()
    markdown = (scratch(tmp_path) / "report.md").read_text()
    assert "Compare" in html and "style-gap → pass" in html
    assert "+2" in html and "Compare main → dev/x" in markdown


# --- guards ---------------------------------------------------------------------------------

def test_refuses_a_stageable_scratch_dir(tmp_path, monkeypatch, capsys):
    def fake_run(command, **kwargs):
        if command[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # check-ignore: not ignored

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    assert cli(tmp_path, "plan") == 2
    assert "refusing stageable scratch root" in capsys.readouterr().err


def test_markdown_renderer_escapes_html(tmp_path):
    rendered = sim.md_to_html("<script>alert(1)</script> **vet** https://example.test/x")
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "<strong>vet</strong>" in rendered
    assert 'href="https://example.test/x"' in rendered


def test_parse_duration():
    assert sim.parse_duration("90") == 90
    assert sim.parse_duration("5m") == 300
    assert sim.parse_duration("1h") == 3600
    with pytest.raises(sim.SimError):
        sim.parse_duration("soon")
