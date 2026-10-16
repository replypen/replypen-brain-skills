from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_lint.py"


def _brain(tmp_path: Path, script: str) -> Path:
    (tmp_path / "skills").mkdir()
    action = tmp_path / "actions" / "example"
    action.mkdir(parents=True)
    (action / "manifest.yaml").write_text("id: example\ndescription: Run example\n", "utf-8")
    (action / "script.py").write_text(script, "utf-8")
    return tmp_path


def test_entrypoint_warns_without_failing_unless_strict(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "_DEAD = 1\n")
    command = [sys.executable, str(SCRIPT), "--brain", str(brain)]

    normal = subprocess.run(command, text=True, capture_output=True, check=False)
    strict = subprocess.run([*command, "--strict"], text=True, capture_output=True, check=False)

    assert normal.returncode == 0
    assert strict.returncode == 1
    assert "brain lint: 0 FAIL, 1 WARN" in normal.stdout
    assert "WARN dead private names (1)" in normal.stdout


def test_entrypoint_fails_on_blocking_finding(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "# " + "x" * (96 * 1024) + "\n")

    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--brain", str(brain)],
        text=True, capture_output=True, check=False,
    )

    assert run.returncode == 1
    assert "brain lint: 1 FAIL" in run.stdout
    assert "FAIL script size (1)" in run.stdout


def test_entrypoint_reports_missing_pyyaml_as_dependency_error(tmp_path: Path) -> None:
    brain = _brain(tmp_path, "x = 1\n")

    # -S hides PyYAML; the flag marks the one `uv --with pyyaml` re-exec as already spent.
    run = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--brain", str(brain)],
        text=True, capture_output=True, check=False,
        env={**os.environ, "RC_BRAIN_LINT_BOOTSTRAPPED": "1"},
    )

    assert run.returncode == 1
    assert run.stdout == ""
    assert "error: PyYAML is required" in run.stderr


def test_entrypoint_accepts_all_and_explicit_paths(tmp_path: Path) -> None:
    """brain_structure.py drives this entrypoint as `--all [--strict]` and `[--strict] <path>…`."""
    brain = _brain(tmp_path, "_DEAD = 1\n")
    command = [sys.executable, str(SCRIPT), "--brain", str(brain)]

    whole = subprocess.run([*command, "--all"], text=True, capture_output=True, check=False)
    scoped = subprocess.run([*command, "actions/example/script.py"],
                            text=True, capture_output=True, check=False)
    elsewhere = subprocess.run([*command, "actions/example/manifest.yaml", "notes/thing.txt"],
                               text=True, capture_output=True, check=False)

    assert whole.returncode == 0 and "WARN dead private names (1)" in whole.stdout
    assert scoped.returncode == 0 and "WARN dead private names (1)" in scoped.stdout
    assert elsewhere.returncode == 0 and elsewhere.stdout.strip() == "brain lint: clean"


def test_entrypoint_skips_run_hidden_paths(tmp_path: Path) -> None:
    """`.agents/` & co never reach a run, so their (machine-local) symlinks are not brain findings."""
    brain = _brain(tmp_path, "x = 1\n")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(brain), *args], check=True, capture_output=True)
    (brain / ".replypenignore").write_text("/.agents/\n", "utf-8")
    (brain / ".agents").mkdir()
    (brain / ".agents" / "docs").symlink_to("/opt/kit/docs")
    subprocess.run(["git", "-C", str(brain), "add", "-A", "-f"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(brain), "commit", "-qm", "x"], check=True, capture_output=True)

    run = subprocess.run([sys.executable, str(SCRIPT), "--brain", str(brain), "--all"],
                         text=True, capture_output=True, check=False)

    assert run.returncode == 0, run.stdout + run.stderr
    assert "symlink" not in run.stdout


def test_entrypoint_lints_a_mirror_without_skills_dir(tmp_path: Path) -> None:
    """`--as mirror`: no skills/ needed, kind printed, a tag outside the mirror scan dirs FAILs."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "notes.md").write_text("---\ninclude_in: [agent]\n---\nx\n", "utf-8")
    command = [sys.executable, str(SCRIPT), "--brain", str(tmp_path)]

    as_brain = subprocess.run(command, text=True, capture_output=True, check=False)
    as_mirror = subprocess.run([*command, "--as", "mirror"], text=True, capture_output=True, check=False)

    assert as_brain.returncode == 1 and "--as tenant|mirror" in as_brain.stderr
    assert as_mirror.returncode == 1, as_mirror.stdout + as_mirror.stderr
    assert as_mirror.stdout.startswith("brain lint: linting as mirror (--as)\n")
    assert "lib/notes.md — `include_in` is only read in a mirror" in as_mirror.stdout


def test_entrypoint_reports_tags_on_run_hidden_paths(tmp_path: Path) -> None:
    """Hidden paths are not judged, except to say a tag there is never read."""
    brain = _brain(tmp_path, "x = 1\n")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(brain), *args], check=True, capture_output=True)
    (brain / ".replypenignore").write_text("/maint/\n", "utf-8")
    (brain / "maint").mkdir()
    (brain / "maint" / "plan.md").write_text("---\ninclude_in: [agent]\n---\nx\n", "utf-8")
    subprocess.run(["git", "-C", str(brain), "add", "-A"], check=True, capture_output=True)

    run = subprocess.run([sys.executable, str(SCRIPT), "--brain", str(brain)],
                         text=True, capture_output=True, check=False)

    assert run.returncode == 1
    assert "maint/plan.md — `include_in` on a run-hidden path" in run.stdout
