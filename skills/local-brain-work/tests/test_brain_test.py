from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brain_test  # noqa: E402


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_collects_skills_first_then_every_test_dir_but_operator_ones(tmp_path: Path) -> None:
    _touch(tmp_path / "skills" / "a" / "SKILL.md")
    _touch(tmp_path / "actions" / "refund" / "tests" / "test_preflight.py")
    _touch(tmp_path / "tests" / "test_phone.py")
    _touch(tmp_path / "action-authoring" / "gen" / "tests" / "test_generator.py")
    _touch(tmp_path / "_internal" / "tests" / "test_report.py")
    _touch(tmp_path / ".agents" / "skills" / "x" / "test_x.py")
    _touch(tmp_path / "knowledge" / "faq.md")
    _touch(tmp_path / "web" / "node_modules" / "pkg" / "test_vendored.py")
    assert brain_test.test_dirs(tmp_path) == ["skills", "action-authoring", "actions", "tests"]


def test_docs_only_brain_passes_and_a_non_brain_errors(tmp_path: Path, capsys) -> None:
    _touch(tmp_path / "AGENTS.md")
    assert brain_test.main(["--brain", str(tmp_path)]) == 1
    _touch(tmp_path / ".replypen.toml")
    assert brain_test.main(["--brain", str(tmp_path)]) == 0
    assert "docs-only" in capsys.readouterr().err
    assert brain_test.main(["--brain", str(tmp_path), "--require-live"]) == 1


def test_pytest_flags_pass_through_in_order(tmp_path: Path, monkeypatch) -> None:
    _touch(tmp_path / "skills" / "a" / "SKILL.md")
    seen = {}
    monkeypatch.setattr(brain_test.E, "discover_mirrors", lambda *a: {})
    monkeypatch.setattr(brain_test, "_run_uv", lambda brain, dirs, m, live, args, *rest: seen.update(dirs=dirs, args=args) or 0)
    assert brain_test.main(["--brain", str(tmp_path), "-k", "refund and not slow", "-x"]) == 0
    assert seen["dirs"] == ["skills"]
    assert seen["args"][-3:] == ["-k", "refund and not slow", "-x"]
