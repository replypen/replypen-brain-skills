"""Run-hidden trees carry maintainer-only rp CLI guidance by design; the privacy lint skips them."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_lint.py"

HIDDEN_DOC = "Maintainer runbook\n\nRun `rp run debug <uuid>` locally to inspect the run.\n"


def _brain(tmp_path: Path) -> Path:
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    (tmp_path / ".replypenignore").write_text("/.agents/\n", "utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "intake.md").write_text("# Intake\n\nDistilled guidance.\n", "utf-8")
    hidden = tmp_path / ".agents" / "skills" / "local"
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text(HIDDEN_DOC, "utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A", "-f"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "x"], check=True, capture_output=True)
    return tmp_path


def _run(brain: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          cwd=str(brain), text=True, capture_output=True, check=False)


def test_hidden_tree_is_not_scanned_in_all_mode(tmp_path: Path) -> None:
    run = _run(_brain(tmp_path), "--all")

    assert run.returncode == 0, run.stdout + run.stderr
    assert "rc-cli-command" not in run.stdout


def test_explicit_hidden_paths_are_dropped(tmp_path: Path) -> None:
    brain = _brain(tmp_path)

    run = _run(brain, ".agents/skills/local/SKILL.md")

    assert run.returncode == 0, run.stdout + run.stderr
    assert "rc-cli-command" not in run.stdout


def test_visible_file_with_rc_guidance_still_fails(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    (brain / "notes" / "intake.md").write_text(HIDDEN_DOC, "utf-8")

    run = _run(brain, "notes/intake.md")

    assert run.returncode == 1
    assert "rc-cli-command" in run.stdout
