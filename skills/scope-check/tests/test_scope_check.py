"""scope-check unit tests — a fake `rc` at the subprocess boundary, no network, no production."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scope_common  # noqa: E402
import scope_matrix  # noqa: E402
import scope_smoke  # noqa: E402


# --- fake rc -----------------------------------------------------------------------------------


def fake_rc(monkeypatch, handler):
    """Replace subprocess.run with `handler(args) -> (returncode, stdout, stderr)`; record calls."""
    calls = []

    def run(cmd, **kwargs):
        assert cmd[0] == "rc"
        calls.append(cmd)
        code, out, err = handler(cmd[1:])
        return subprocess.CompletedProcess(cmd, code, out, err)

    monkeypatch.setattr(scope_common.subprocess, "run", run)
    return calls


PREVIEW_NO_PRINCIPAL = {
    "tables": [
        {"name": "activities", "count": 4000, "predicate": "", "rows": []},
        {"name": "people", "count": 900, "predicate": "", "rows": []},
        {"name": "form_fields", "count": 12, "predicate": "", "rows": []},
    ],
    "unconstrained_tables": ["activities", "people", "form_fields"],
    "table_grants": {"all": True, "granted": []},
    "hidden_tables": [],
}

PREVIEW_PARENT = {
    "tables": [
        {"name": "activities", "count": 3, "predicate": "id = ANY(:my_ids)", "rows": []},
        {"name": "people", "count": 2, "predicate": "", "rows": []},
    ],
    "unconstrained_tables": ["people"],
    "table_grants": {"all": False, "granted": ["activities", "people"]},
    "hidden_tables": ["form_fields"],
}

SCHEMA_NO_PRINCIPAL = {
    "tables": [
        {"name": "people", "columns": [{"name": "id"}, {"name": "email"}, {"name": "iban"}]},
    ]
}
SCHEMA_PARENT = {"tables": [{"name": "people", "columns": [{"name": "id"}, {"name": "email"}]}]}


def matrix_handler(args):
    is_parent = "--principal-kind" in args
    if args[:3] == ["project", "database", "preview"]:
        return 0, json.dumps(PREVIEW_PARENT if is_parent else PREVIEW_NO_PRINCIPAL), ""
    if args[:4] == ["dev", "console", "database", "schema"]:
        return 0, json.dumps(SCHEMA_PARENT if is_parent else SCHEMA_NO_PRINCIPAL), ""
    raise AssertionError(f"unexpected rc call: {args}")


# --- audiences ---------------------------------------------------------------------------------


def test_parse_audience():
    aud = scope_common.parse_audience("parent=kampadmin_parent:abc-123")
    assert (aud.label, aud.kind, aud.external_id) == ("parent", "kampadmin_parent", "abc-123")
    assert aud.flags() == ["--principal-kind", "kampadmin_parent", "--principal-id", "abc-123"]


@pytest.mark.parametrize("spec", ["parent", "parent=", "=k:i", "parent=kind", "parent=:id"])
def test_parse_audience_rejects_half_specs(spec):
    # A half-spec silently becoming the no-principal view is the exact confusion this skill removes.
    with pytest.raises(ValueError):
        scope_common.parse_audience(spec)


def test_no_principal_is_always_first():
    auds = scope_common.audiences_from_specs(["parent=k:1"])
    assert [a.label for a in auds] == ["no-principal", "parent"]
    assert auds[0].flags() == []


# --- matrix ------------------------------------------------------------------------------------


def test_classify_separates_hidden_row_constrained_and_visible():
    assert scope_matrix.classify(PREVIEW_PARENT) == {
        "activities": "rows:3",
        "people": "✓",
        "form_fields": "hidden",
    }
    assert scope_matrix.classify(PREVIEW_NO_PRINCIPAL)["form_fields"] == "✓"


def test_matrix_renders_every_audience_and_the_absent_cell(monkeypatch, tmp_path, capsys):
    calls = fake_rc(monkeypatch, matrix_handler)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "matrix.md"
    rc = scope_matrix.main(
        [
            "--project", "kampadmin",
            "--dsn", "KAMPADMIN_APP_DSN",
            "--tenant", "acme",
            "--as", "parent=kampadmin_parent:abc",
            "--out", str(out),
        ]
    )
    assert rc == 0
    md = out.read_text()

    assert "| table | no-principal | parent |" in md
    assert "| `activities` | ✓ | rows:3 |" in md
    assert "| `form_fields` | ✓ | hidden |" in md
    # people is unconstrained for the parent too → plain ✓, not rows:2
    assert "| `people` | ✓ | ✓ |" in md
    # header carries the regeneration command and the scope
    assert "--as parent=kampadmin_parent:abc --out matrix.md`" in md  # repo-relative, no home path
    assert "`KAMPADMIN_APP_DSN`" in md
    assert "- **parent** — 2 table(s): activities, people" in md
    # column stripping vs the baseline
    assert "- **parent**\n  - `people`: `iban`" in md

    previews = [c for c in calls if c[1:4] == ["project", "database", "preview"]]
    assert len(previews) == 2
    assert "--raw-output" in previews[0] and "--principal-kind" not in previews[0]
    assert previews[1][previews[1].index("--principal-id") + 1] == "abc"


def test_matrix_degrades_when_rc_cannot_scope_a_schema_read(monkeypatch, capsys):
    def handler(args):
        if args[:3] == ["project", "database", "preview"]:
            is_parent = "--principal-kind" in args
            return 0, json.dumps(PREVIEW_PARENT if is_parent else PREVIEW_NO_PRINCIPAL), ""
        if "--principal-kind" in args:
            return 1, "", "Error: unknown flag: --principal-kind"
        return 0, json.dumps(SCHEMA_NO_PRINCIPAL), ""

    fake_rc(monkeypatch, handler)
    assert scope_matrix.main(
        ["--project", "p", "--dsn", "D_DSN", "--as", "parent=k:1"]
    ) == 0
    md = capsys.readouterr().out
    assert "| `form_fields` | ✓ | hidden |" in md  # the load-bearing half still renders
    assert "cannot scope a schema read by principal" in md


def test_matrix_reports_an_rc_failure(monkeypatch, capsys):
    fake_rc(monkeypatch, lambda args: (1, "", "Error: project not found"))
    assert scope_matrix.main(["--project", "p", "--dsn", "D_DSN"]) == 1
    assert "project not found" in capsys.readouterr().err


# --- smoke -------------------------------------------------------------------------------------


def bash_result(exit_code=0, stdout="", stderr="", timed_out=False):
    return json.dumps(
        {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out}
    )


def test_smoke_passes_and_fails_per_audience(monkeypatch, capsys):
    def handler(args):
        if "--principal-kind" in args:
            # the parent hits a table the access policy removed
            return 1, bash_result(1, "", 'relation "form_fields" does not exist'), ""
        return 0, bash_result(0, "Camp: Zomerkamp\n"), ""

    calls = fake_rc(monkeypatch, handler)
    rc = scope_smoke.main(
        ["--project", "p", "--tenant", "t", "--as", "parent=k:1", "--cmd", "python overview.py"]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "| no-principal | parent |" in out
    assert "✅" in out and "❌" in out
    assert "❌ parent × python overview.py: exit 1" in out
    assert 'relation "form_fields" does not exist' in out

    assert calls[0][1:5] == ["dev", "console", "bash", "run"]
    assert "--" in calls[0] and calls[0][-1] == "python overview.py"
    assert calls[0][calls[0].index("--timeout") + 1] == "120"


def test_smoke_fails_on_a_marker_even_when_the_command_exits_zero(monkeypatch, capsys):
    fake_rc(monkeypatch, lambda args: (0, bash_result(0, "Traceback (most recent call last):"), ""))
    assert scope_smoke.main(["--project", "p", "--cmd", "x"]) == 1
    assert "output contains 'Traceback'" in capsys.readouterr().out


def test_smoke_fails_on_a_missing_expectation(monkeypatch):
    fake_rc(monkeypatch, lambda args: (0, bash_result(0, "nothing useful"), ""))
    cmd = scope_smoke.Command("overview", "python overview.py", ("Camp:",))
    ok, reason, _ = scope_smoke.evaluate(
        {"exit_code": 0, "stdout": "nothing useful"}, cmd, scope_smoke.DEFAULT_FAIL_MARKERS
    )
    assert not ok and "missing expected 'Camp:'" == reason


def test_smoke_json_output_and_clean_pass(monkeypatch, capsys):
    fake_rc(monkeypatch, lambda args: (0, bash_result(0, "Camp: Zomerkamp"), ""))
    assert scope_smoke.main(["--project", "p", "--cmd", "x", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    assert payload["audiences"] == ["no-principal"]
    assert payload["results"][0]["ok"] is True


def test_smoke_flags_an_rc_without_principal_support(monkeypatch, capsys):
    fake_rc(monkeypatch, lambda args: (1, "", "Error: unknown flag: --principal-kind"))
    assert scope_smoke.main(["--project", "p", "--as", "parent=k:1", "--cmd", "x"]) == 1
    assert "upgrade rc" in capsys.readouterr().out


def test_config_parsing(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "scopecheck.toml"
    cfg.write_text(
        """
project = "kampadmin"
tenant = "acme"
timeout = 45
fail_on = ["nope"]

[[audience]]
label = "parent"
kind = "kampadmin_parent"
id = "abc"

[[command]]
name = "overview"
cmd = "python overview.py"
expect = ["Camp:"]
"""
    )
    parsed = scope_smoke.load_config(cfg)
    assert parsed["project"] == "kampadmin"
    assert parsed["timeout"] == 45
    assert parsed["fail_markers"] == ("nope",)
    assert [a.label for a in parsed["audiences"]] == ["no-principal", "parent"]
    assert parsed["commands"] == [scope_smoke.Command("overview", "python overview.py", ("Camp:",))]

    calls = fake_rc(monkeypatch, lambda args: (0, bash_result(0, "Camp: x"), ""))
    assert scope_smoke.main(["--config", str(cfg)]) == 0
    assert calls[0][calls[0].index("--project") + 1] == "kampadmin"
    assert calls[0][calls[0].index("--timeout") + 1] == "45"


def test_example_config_parses():
    assert scope_smoke.load_config(ROOT / "scripts" / "scopecheck.example.toml")["commands"]


@pytest.mark.parametrize(
    "body", ["[[audience]]\nlabel = 'x'\n", "[[command]]\nname = 'x'\n"]
)
def test_config_rejects_incomplete_entries(tmp_path, body):
    cfg = tmp_path / "bad.toml"
    cfg.write_text(body)
    with pytest.raises(ValueError):
        scope_smoke.load_config(cfg)


def test_classify_marks_unconstrained_tables_visible():
    from scope_matrix import classify, VISIBLE, HIDDEN

    cells = classify({
        "unconstrained_tables": ["activities", "people"],
        "tables": [{"name": "avo_tables", "count": 56, "predicate": "id IN (...)"}],
        "hidden_tables": ["tenants"],
    })
    assert cells == {"activities": VISIBLE, "people": VISIBLE, "avo_tables": "rows:56", "tenants": HIDDEN}


def test_classify_count_minus_one_is_too_large():
    cells = scope_matrix.classify({"tables": [{"name": "versions", "count": -1, "predicate": "CASE …"}]})
    assert cells == {"versions": "rows:too large to count"}
