"""V2 contracts: recurrence, executable tasks and owner choices and production implementation prompts."""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from report_schema import (
    compose_prompt,
    evidence_errors,
    load_report,
    owner_prompt_allowed,
    soft_warnings,
    validation_errors,
)

FIXTURES = ROOT / "fixtures"
SAMPLE = FIXTURES / "sample_report.json"
OLD = json.loads((FIXTURES / "prior_finding.json").read_text())
PRIORS = {OLD["signature"]: {"finding": OLD, "rows": [{"status": "open", "project": OLD["members"][0], "audience": OLD["audience"]}]}}


@pytest.fixture
def sample():
    return load_report(SAMPLE, prior=copy.deepcopy(PRIORS))


def write_report(tmp_path, data, name="report.json"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


def changed_sample(tmp_path, mutate):
    data = json.loads(SAMPLE.read_text())
    mutate(data)
    return write_report(tmp_path, data)


def test_fixture_and_collector_contract(sample):
    assert not validation_errors(SAMPLE, prior=copy.deepcopy(PRIORS))
    assert not validation_errors(FIXTURES / "quiet_report.json", prior={})
    assert not [
        w
        for w in soft_warnings(
            sample, FIXTURES / "sample_kpis.json", FIXTURES / "sample_manifest.json"
        )
        if w.startswith(("kpis:", "coverage:"))
    ]


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda d: d.update(schema_version=1), "schema_version"),
        (lambda d: d["findings"][0].update(oops=1), "oops"),
        (lambda d: d["findings"][0].pop("prompt"), "prompt"),
        (lambda d: d["findings"][0].pop("ask_nl"), "ask_nl"),
        (lambda d: d["findings"][-1].update(text_en=None), "unchanged forbids"),
        (lambda d: d["findings"][-1].update(prompt=None), "unchanged forbids"),
        (
            lambda d: d["findings"][0]["prompt"]["targets"][0].update(
                paths=["missing-fleet-fixture-file"]
            ),
            "path does not exist",
        ),
        (
            lambda d: d["findings"][0]["prompt"]["targets"][0].update(repo="relative"),
            "absolute checkout",
        ),
        (
            lambda d: d["findings"][0]["prompt"]["targets"][0].update(
                paths=["/etc/hosts"]
            ),
            "outside target",
        ),
        (lambda d: d["findings"][0]["prompt"].update(task_kind="decide"), "decision"),
        (
            lambda d: d["findings"][0]["prompt"].update(
                run_refs=["https://app.replypen.com/runs/a?t=x"]
            ),
            "canonical",
        ),
        (
            lambda d: d["findings"][0].update(
                scope={"level": "tenant", "tenant": "unknown"}
            ),
            "unknown tenant",
        ),
        (lambda d: d["findings"][0].update(update_en="unneeded"), "new forbids"),
    ],
)
def test_validation(tmp_path, mutate, expected):
    errors = validation_errors(changed_sample(tmp_path, mutate), prior=copy.deepcopy(PRIORS))
    assert any(expected in e for e in errors), errors


def test_unchanged_requires_prior(tmp_path):
    errors = validation_errors(changed_sample(tmp_path, lambda d: None), prior={})
    assert any("needs a prior" in e for e in errors)


def test_decide_and_investigate_composition(sample):
    f = sample.findings[0]
    assert "Seen " not in compose_prompt(f) and "Skills:" not in compose_prompt(f)
    from report_schema import Decision

    f.prompt.task_kind = "decide"
    f.prompt.decision = Decision(question="Which rule?", options=["Always", "Per case"])
    assert "Decision first" in compose_prompt(f) and "Per option" in compose_prompt(f)
    f.prompt.task_kind = "investigate"
    f.prompt.decision = None
    assert "Task / Checks" in compose_prompt(f)


def test_warnings_and_evidence(tmp_path, sample):
    sample.findings[0].prompt.repro = None
    sample.findings[
        0
    ].prompt.change = "Locate the manifest and consider either implementation."
    warnings = soft_warnings(sample)
    assert any("repro" in w for w in warnings) and any("hedge" in w for w in warnings)
    assert evidence_errors(sample, write_report(tmp_path, {"runs": []}))
    bad = write_report(tmp_path, {"invalid": True}, "manifest.json")
    assert any("cannot compare" in w for w in soft_warnings(sample, manifest_path=bad))


def test_known_signature_cannot_be_new(tmp_path):
    data = json.loads(SAMPLE.read_text())
    old = copy.deepcopy(OLD)
    data["findings"] = [old]
    assert any(
        "use changed/unchanged" in e
        for e in validation_errors(write_report(tmp_path, data), prior=copy.deepcopy(PRIORS))
    )



@pytest.mark.parametrize("options", [[], [{"label": "", "instruction": "Do it"}], [{"label": "A", "instruction": ""}], [{"label": "A", "instruction": "Do it"}] * 5])
def test_owner_options_required(tmp_path, options):
    path = changed_sample(tmp_path, lambda d: d["findings"][0].update(options=options))
    assert validation_errors(path, prior=PRIORS)


def test_unchanged_owner_requires_options(tmp_path):
    data = json.loads(SAMPLE.read_text())
    old = data["findings"][2]
    finding = {k: v for k, v in old.items() if k not in ("text_en", "text_nl", "ask_nl", "ask_for", "prompt", "impact", "root_cause")}
    finding.update(status="unchanged", update_nl="Nog geen beslissing.")
    data["findings"] = [finding]
    prior = {old["signature"]: {"finding": old, "rows": [{"status": "open", "project": old["members"][0], "audience": old["audience"], "tenant": old["scope"].get("tenant")}]}}
    path = write_report(tmp_path, data)
    report = load_report(path, prior=prior)
    assert report.effective(report.findings[0]).ask_nl == old["ask_nl"]
    finding.pop("options")
    assert validation_errors(write_report(tmp_path, data), prior=prior)


@pytest.mark.parametrize("status", ["applied"])
def test_archived_can_recur_but_requires_full_fields(tmp_path, status):
    prior = copy.deepcopy(PRIORS)
    prior[OLD["signature"]]["rows"][0]["status"] = status
    data = json.loads(SAMPLE.read_text())
    data["findings"] = [copy.deepcopy(OLD)]
    assert not validation_errors(write_report(tmp_path, data), prior=prior)
    data["findings"] = [json.loads(SAMPLE.read_text())["findings"][-1]]
    assert any("full fields" in e for e in validation_errors(write_report(tmp_path, data), prior=prior))


def test_inherited_prompt_path_still_validated(tmp_path):
    prior = copy.deepcopy(PRIORS)
    prior[OLD["signature"]]["finding"]["prompt"]["targets"][0]["paths"] = ["missing-fleet-inherited-file"]
    assert any("inherited prompt" in e and "use changed" in e for e in validation_errors(SAMPLE, prior=prior))


def test_owner_prompt_privacy_and_ship_authority(tmp_path, sample):
    brain = tmp_path / "replypen-brain-test"
    brain.mkdir()
    (brain / "policy.md").write_text("policy")
    finding = sample.findings[1]
    finding.prompt.targets[0].repo = str(brain)
    finding.prompt.targets[0].paths = ["policy.md"]
    finding.root_cause.plane = "brain_content"
    assert owner_prompt_allowed(finding)
    assert not owner_prompt_allowed(sample.findings[0])
    assert "Commit AND ship" in compose_prompt(finding)
    assert "human review decision" in compose_prompt(finding)


@pytest.mark.parametrize("last_seen,allowed", [("2026-09-04", True), ("2026-09-03", False)])
def test_new_same_day_retry(tmp_path, last_seen, allowed):
    data = json.loads(SAMPLE.read_text())
    data["findings"] = [copy.deepcopy(OLD)]
    prior = copy.deepcopy(PRIORS)
    prior[OLD["signature"]]["rows"][0]["last_seen"] = last_seen
    errors = validation_errors(write_report(tmp_path, data), prior=prior)
    assert (not errors) == allowed


def test_archive_status_uses_only_relevant_scope(tmp_path):
    data = json.loads(SAMPLE.read_text())
    finding = copy.deepcopy(OLD)
    finding["members"] = ["kampadmin"]
    data["findings"] = [finding]
    prior = {finding["signature"]: {"finding": copy.deepcopy(OLD), "rows": [
        {"project": "kampadmin", "audience": "technical", "status": "applied"},
        {"project": "kampadmin-support", "audience": "technical", "status": "open"},
        {"project": "kampadmin", "audience": "owner", "status": "open"},
    ]}}
    assert not validation_errors(write_report(tmp_path, data), prior=prior)
    unchanged = json.loads(SAMPLE.read_text())["findings"][-1]
    unchanged["members"] = ["kampadmin"]
    data["findings"] = [unchanged]
    assert any("full fields" in e for e in validation_errors(write_report(tmp_path, data), prior=prior))


def test_same_day_retry_checks_every_active_relevant_row(tmp_path):
    data = json.loads(SAMPLE.read_text())
    data["findings"] = [copy.deepcopy(OLD)]
    data["findings"][0]["members"] = ["kampadmin", "kampadmin-support"]
    prior = copy.deepcopy(PRIORS)
    prior[OLD["signature"]]["rows"] = [
        {"project": "kampadmin", "audience": "technical", "status": "open", "last_seen": data["date"]},
        {"project": "kampadmin-support", "audience": "technical", "status": "decided", "last_seen": "2026-09-03"},
    ]
    assert validation_errors(write_report(tmp_path, data), prior=prior)
    prior[OLD["signature"]]["rows"][1]["last_seen"] = data["date"]
    assert not validation_errors(write_report(tmp_path, data), prior=prior)


@pytest.mark.parametrize("disposition", ["accepted", "noise", "human_task"])
def test_closed_retest_requires_changed_update(tmp_path, disposition):
    data = json.loads(SAMPLE.read_text())
    finding = copy.deepcopy(OLD)
    data["findings"] = [finding]
    prior = copy.deepcopy(PRIORS)
    row = prior[OLD["signature"]]["rows"][0]
    row.update(status="closed", applied={"disposition": disposition})
    assert any("met retest trigger" in e for e in validation_errors(write_report(tmp_path, data), prior=prior))
    finding.update(status="changed", update_en="The previously missing source is now available; the retest trigger is met.")
    assert not validation_errors(write_report(tmp_path, data), prior=prior)


def test_later_full_new_fields_allowed_for_publisher_date_check(tmp_path):
    data = json.loads(SAMPLE.read_text())
    data["findings"] = [copy.deepcopy(OLD)]
    prior = copy.deepcopy(PRIORS)
    prior[OLD["signature"]]["rows"][0].update(status="closed", applied={"disposition": "later"})
    assert not validation_errors(write_report(tmp_path, data), prior=prior)


@pytest.mark.parametrize("widen", ["member", "audience"])
def test_unchanged_cannot_widen_prior_scope(tmp_path, widen):
    data = json.loads(SAMPLE.read_text())
    finding = data["findings"][-1]
    data["findings"] = [finding]
    prior = copy.deepcopy(PRIORS)
    if widen == "member":
        finding["members"] = ["kampadmin", "kampadmin-support"]
    else:
        finding.update(audience="both", title_nl="Controleer het beleid", update_nl="Nog open.", options=[{"label": "A", "instruction": "Kies beleid A."}])
        prior[OLD["signature"]]["finding"].update(text_nl="Oude samengevoegde tekst.", ask_nl="Welke keuze?")
    assert any("every member/audience" in e for e in validation_errors(write_report(tmp_path, data), prior=prior))


def test_owner_language_resolution_has_no_dutch_fallback(sample):
    coverage = sample.coverage
    assert coverage.lang() == "nl"                 # project persona.language
    assert coverage.lang("lbv") == "fr"            # tenant overrides the project
    assert coverage.lang("unknown") == "nl"        # unknown tenant inherits the project
    bare = coverage.model_copy(update={"owner_lang": None, "owner_lang_by_tenant": {}})
    assert bare.lang() == "en" and bare.lang("lbv") == "en"


def test_owner_language_drives_the_prose_heuristics(sample):
    english = sample.model_copy(deep=True)
    english.coverage.owner_lang = "en"
    english.coverage.owner_lang_by_tenant = {}
    dutch_titles = [w for w in soft_warnings(english) if "title_nl" in w or "text_nl" in w]
    assert not dutch_titles  # the Dutch heuristic must not fire on an English owner half
