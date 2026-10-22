from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brain_hygiene  # noqa: E402
import triage_cases  # noqa: E402
from test_brain_hygiene import make_brain  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "triage_cases"
# Same fixtures and expected messages as replypen internal/triageeval/cases_test.go: a file one parser
# accepts and the other rejects is drift. Re-copy them from the host when its parser changes.
INVALID = {
    "no-frontmatter": "frontmatter",
    "unknown-field": "colour",
    "id-mismatch": "file name",
    "inconsistent-expect": "expect.process",
    "missing-body": "no `## m1` body section",
    "duplicate-message": "duplicate message id",
    "time-travel": "back in time",
    "last-outbound": "current turn",
    "bad-status": "status",
    "bad-channel": "channel",
    "bad-follow-up": "follow_up_from",
    "text-before-section": "before the first",
    "uppercase-header": "lowercase",
    "duplicate-key": "already defined",
    "bad-bool": "bool",
    "headers-list": "map[string]string",
    "impossible-date": "RFC 3339",
}


@pytest.mark.parametrize("path", sorted((FIXTURES / "valid").glob("*.md")), ids=lambda p: p.stem)
def test_valid_fixture_parses(path: Path) -> None:
    triage_cases.parse_case(f"{triage_cases.CASES_DIR}/{path.name}", path.read_bytes())


@pytest.mark.parametrize("path", sorted((FIXTURES / "invalid").glob("*.md")), ids=lambda p: p.stem)
def test_invalid_fixture_rejected_with_host_message(path: Path) -> None:
    with pytest.raises(triage_cases.CaseError) as exc:
        triage_cases.parse_case(f"{triage_cases.CASES_DIR}/{path.name}", path.read_bytes())
    assert INVALID[path.stem] in str(exc.value)


def test_invalid_fixture_set_matches_host() -> None:
    assert {p.stem for p in (FIXTURES / "invalid").glob("*.md")} == set(INVALID)


def _corpus(extra: dict[str, str]) -> dict[str, str]:
    valid = (FIXTURES / "valid" / "akkoord-op-voorstel.md").read_text("utf-8")
    return {".replypenignore": "/_internal/\n",
            f"{triage_cases.CASES_DIR}/akkoord-op-voorstel.md": valid, **extra}


def test_hygiene_flags_a_bad_case_only_when_cases_change(tmp_path: Path) -> None:
    bad = (FIXTURES / "invalid" / "missing-body.md").read_text("utf-8")
    root = make_brain(tmp_path / "b", _corpus({f"{triage_cases.CASES_DIR}/missing-body.md": bad}))
    msgs = [f.message for f in brain_hygiene.check(root) if "triage-cases" in f.message]
    assert any("no `## m1` body section" in m for m in msgs), msgs
    assert not any("run-visible" in m for m in msgs)  # /_internal/ is ignored
    # An unrelated change does not re-judge the corpus.
    assert not [f for f in brain_hygiene.check(root, ["AGENTS.md"]) if "triage-cases" in f.message]



def test_hygiene_notices_run_visible_cases(tmp_path: Path) -> None:
    files = _corpus({})
    files.pop(".replypenignore")
    root = make_brain(tmp_path / "b", files)
    found = [f for f in brain_hygiene.check(root) if "triage-cases" in f.message]
    assert [f.severity for f in found] == ["NOTICE"] and "run-visible" in found[0].message


def test_hygiene_fallback_crash_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A uv fallback that cannot run (dependency download failed) must block, not pass silently."""
    import builtins
    import subprocess as sp

    real_import = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
        return real_import(name, *a, **k)

    root = make_brain(tmp_path / "b", _corpus({}))
    monkeypatch.setattr(builtins, "__import__", no_yaml)
    monkeypatch.setattr(brain_hygiene.shutil, "which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(brain_hygiene.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 2, stdout="", stderr="network down"))
    found = brain_hygiene._triage_case_findings(root)
    assert len(found) == 1 and found[0].severity == "ERROR" and "did not run" in found[0].message
