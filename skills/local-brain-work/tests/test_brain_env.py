from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_env.py"
SPEC = importlib.util.spec_from_file_location("brain_env", SCRIPT)
assert SPEC and SPEC.loader
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)


def _write_probe(root: Path, value: str) -> None:
    package = root / "lib"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runtime_probe.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")


def _probe(child_env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", "from lib.runtime_probe import VALUE; print(VALUE)"],
        env=child_env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_uv_child_sees_local_runtime_source_edits_ahead_of_stale_install(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    stale_install = tmp_path / "site-packages"
    brain_scripts = tmp_path / "brain" / "skills"
    _write_probe(runtime, "source-before")
    _write_probe(stale_install, "stale-wheel")
    monkeypatch.setattr(E, "RUNTIME", runtime)

    child = E.uv_child_env(
        {"PYTHONPATH": str(stale_install)}, [brain_scripts], mirrors_root=None,
    )

    assert child["PYTHONPATH"].split(os.pathsep) == [
        str(brain_scripts), str(runtime), str(stale_install),
    ]
    assert _probe(child) == "source-before"

    _write_probe(runtime, "source-after-edit")
    assert _probe(child) == "source-after-edit"


def test_uv_child_does_not_shadow_explicit_runtime_override(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    _write_probe(runtime, "local-source")
    monkeypatch.setattr(E, "RUNTIME", runtime)
    monkeypatch.setenv("RC_RUNTIME_SPEC", "replypen-runtime==99")

    child = E.uv_child_env({}, [], mirrors_root=None)

    assert str(runtime) not in child.get("PYTHONPATH", "").split(os.pathsep)


def test_declared_mirrors_are_relative_and_cli_overrides(tmp_path: Path) -> None:
    brain = tmp_path / "org" / "brain"
    brain.mkdir(parents=True)
    (brain / ".replypen.toml").write_text(
        'project = "demo"\n\n[mirrors]\napp = "../../app"\n', encoding="utf-8"
    )
    override = tmp_path / "override"

    mirrors = E.discover_mirrors(brain, None, [f"app={override}"])

    assert mirrors == {"app": override.resolve()}
    child = E.uv_child_env({}, [], None, mirrors)
    assert child["RC_MIRROR_APP"] == str(override.resolve())


def test_mirrors_root_overrides_declared_name_even_when_missing(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / ".replypen.toml").write_text(
        'project = "demo"\n\n[mirrors]\napp = "../usual-app"\n', encoding="utf-8"
    )
    root = tmp_path / "farm"
    root.mkdir()

    assert E.discover_mirrors(brain, str(root), []) == {"app": root / "app"}


def test_require_mirrors_fails_loud(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"

    assert not E.require_mirrors({"app": missing})
    assert f"MirrorMissing: mirror 'app' is missing; candidates: {missing}" in capsys.readouterr().err


def test_invalid_cli_mirror_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Docker-safe repository slug"):
        E.discover_mirrors(tmp_path, None, [f"../escape={tmp_path}"])
    with pytest.raises(ValueError, match="Docker-safe repository slug"):
        E.discover_mirrors(tmp_path, None, [f"foo:rw={tmp_path}"])


def test_cli_and_config_env_name_collision_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".replypen.toml").write_text(
        'project = "demo"\n\n[mirrors]\na-b = "../one"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="both map to RC_MIRROR_A_B"):
        E.discover_mirrors(tmp_path, None, [f"a_b={tmp_path}"])


def test_declared_mirror_path_must_be_relative(tmp_path: Path) -> None:
    (tmp_path / ".replypen.toml").write_text(
        f'project = "demo"\n\n[mirrors]\napp = "{tmp_path}"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must be relative to the brain root"):
        E.declared_mirrors(tmp_path)


def test_runtime_skew_names_the_mismatch_and_the_fix(monkeypatch):
    monkeypatch.delenv("RC_WORKSPACE_IMAGE", raising=False)
    kit = E.RUNTIME.parent
    if not (kit / ".git").exists() or not (kit / "RUNTIME_DIGEST").is_file():
        pytest.skip("needs a kit git checkout")
    real = subprocess.run
    matching = real([sys.executable, str(kit / "scripts" / "runtime_digest.py"), "--worktree"], cwd=kit,
                    capture_output=True, text=True).stdout.strip() == (kit / "RUNTIME_DIGEST").read_text().strip()
    assert (E.runtime_skew() is None) == matching
    monkeypatch.setattr(E.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "0" * 64 + "\n", ""))
    msg = E.runtime_skew()
    assert msg.startswith("KIT_RUNTIME_MISMATCH") and "--mode uv" in msg and E.DEFAULT_IMAGE in msg
    monkeypatch.setenv("RC_WORKSPACE_IMAGE", "local/custom:dev")
    assert E.runtime_skew() is None  # an explicit custom image is the developer's call
