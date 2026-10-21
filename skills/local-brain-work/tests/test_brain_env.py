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


@pytest.fixture(autouse=True)
def _no_platform_skills_pull(monkeypatch):
    """docker_run_args resolves platform skills via `rp`; tests stay hermetic unless they opt back in."""
    monkeypatch.setenv("RC_PLATFORM_SKILLS", "off")
    E.platform_skills.cache_clear()


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


def _library(root: Path) -> Path:
    for name in ("website", "pdf", "__pycache__"):
        (root / name).mkdir(parents=True)
    return root


def _skills_args(monkeypatch) -> list[str]:
    E.platform_skills.cache_clear()
    args = E.docker_run_args(image="img", brain_dir=Path("/b"), mirrors={}, env_names=[], command=["true"])
    return [args[i + 1] for i, a in enumerate(args) if a == "-v" and ":/skills/" in args[i + 1]]


def test_platform_skills_mount_each_skill_read_only_from_the_pulled_snapshot(tmp_path, monkeypatch) -> None:
    lib = _library(tmp_path / "abc123")
    monkeypatch.delenv("RC_PLATFORM_SKILLS_DIR", raising=False)
    monkeypatch.delenv("RC_PLATFORM_SKILLS", raising=False)
    monkeypatch.setattr(E, "_rp_json", lambda cmd: ({"digest": "abc123", "dir": str(lib)}, "") if "--offline" not in cmd else (None, "x"))
    assert _skills_args(monkeypatch) == [f"{lib}/pdf:/skills/pdf:ro", f"{lib}/website:/skills/website:ro"]


def test_platform_skills_fall_back_to_the_cache_loudly_then_to_none(tmp_path, monkeypatch, capsys) -> None:
    lib = _library(tmp_path / "abc123")
    monkeypatch.delenv("RC_PLATFORM_SKILLS_DIR", raising=False)
    monkeypatch.delenv("RC_PLATFORM_SKILLS", raising=False)
    monkeypatch.setattr(E, "_rp_json", lambda cmd: ({"digest": "abc123", "dir": str(lib)}, "") if "--offline" in cmd else (None, "offline"))
    assert len(_skills_args(monkeypatch)) == 2
    assert "NOT checked against the host" in capsys.readouterr().err
    monkeypatch.setattr(E, "_rp_json", lambda cmd: (None, "not logged in"))
    assert _skills_args(monkeypatch) == []
    assert "rp dev platform-skills pull" in capsys.readouterr().err


def test_platform_skills_override_dir_and_off_switch(tmp_path, monkeypatch) -> None:
    lib = _library(tmp_path / "skills")
    monkeypatch.setattr(E, "_rp_json", lambda cmd: pytest.fail("override must not call rp"))
    monkeypatch.delenv("RC_PLATFORM_SKILLS", raising=False)
    monkeypatch.setenv("RC_PLATFORM_SKILLS_DIR", str(lib))
    assert _skills_args(monkeypatch) == [f"{lib}/pdf:/skills/pdf:ro", f"{lib}/website:/skills/website:ro"]
    monkeypatch.setenv("RC_PLATFORM_SKILLS", "off")
    assert _skills_args(monkeypatch) == []
