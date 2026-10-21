# /// script
# requires-python = ">=3.11"
# ///
"""Shared engine core for the Brain Dev kit — env + PYTHONPATH + lib preflight + the two run modes.

`brain_run.py` and `brain_test.py` are thin front-ends over this module. Everything here is
**brain-dir-relative and `accounts.yml`-free**: the brain is just the cwd (or an explicit path), its
env is the gitignored plaintext `./.env`, and `lib` comes from `replypen-runtime` — the exact bytes
the prod workspace image installs at a clean release tag. A sibling `runtime/` source tree takes
precedence during development so unreleased edits are testable; see `UV_MODE_CAVEATS`.

Two run modes:
  * **uv**     — fast inner loop: `uv run` with `replypen-runtime` + its pinned deps, env from `./.env`.
  * **docker** — faithful pre-push gate: `docker run` the published workspace image, brain + mirrors
                 mounted `:ro`, env injected, isolation matching prod.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"

# ── version line ────────────────────────────────────────────────────────────────────────────────
# The single version line: plugin tag == replypen-runtime pin == workspace image tag == prod
# Dockerfile pin. Bump all together (see RELEASING.md) so local and prod cannot diverge.
VERSION = "0.13.24"
REPO_URL = "https://github.com/replypen/replypen-brain-skills"

# The interpreter prod runs (docker/Dockerfile `FROM python:3.12-slim`). uv mode pins to it with
# `uv run --python` — uv fetches a managed CPython if the host lacks it, so this needs NO mise/pyenv
# setup and a green uv run can't silently use a different Python than the box.
PYTHON_VERSION = "3.12"

# brain_env.py lives at <kit>/skills/local-brain-work/scripts/ ; the canonical runtime/ package (holding
# lib/) sits at the kit root — three levels up. Present in every distribution that bundles the whole
# kit (checkout, CC plugin, local symlink); absent only if the skill dir is shipped on its own.
RUNTIME = (Path(__file__).resolve().parents[3] / "runtime").resolve()

# The committed, universal lockfile pinning runtime's FULL transitive closure (not just the `==`
# direct deps in pyproject). uv mode installs from it so a green run can't drift as PyPI moves; the
# prod image installs the same package under this lock as a constraint (docker/Dockerfile), so the
# local import surface == the image's. Regenerate when bumping a dep — see RELEASING.md.
RUNTIME_LOCK = (RUNTIME / "requirements.lock").resolve()

# The published workspace image prod uses. Overridable for a local build or a fork.
DEFAULT_IMAGE = os.environ.get("RC_WORKSPACE_IMAGE", f"ghcr.io/replypen/workspace:v{VERSION}")


def runtime_skew() -> str | None:
    """Why docker mode's image would NOT carry this kit's `runtime/` bytes, else None.

    The image is picked by VERSION, but the scripts come from whatever kit checkout runs them. A
    checkout with unreleased (or unpulled-release) runtime work then asks the image for code it lacks,
    e.g. `-p lib.pii_pytest` → ImportError deep in pytest (2026-10-21). The release records the runtime
    digest in `RUNTIME_DIGEST`; a working tree that differs is not what `v{VERSION}` baked. Skipped for
    an explicit `RC_WORKSPACE_IMAGE` and for kits without git (nothing to compare)."""
    kit = RUNTIME.parent
    recorded, script = kit / "RUNTIME_DIGEST", kit / "scripts" / "runtime_digest.py"
    if "RC_WORKSPACE_IMAGE" in os.environ or not (recorded.is_file() and script.is_file() and (kit / ".git").exists()):
        return None
    out = subprocess.run([sys.executable, str(script), "--worktree"], cwd=kit, capture_output=True, text=True)
    if out.returncode or out.stdout.strip() == recorded.read_text().strip():
        return None
    return (f"KIT_RUNTIME_MISMATCH: {kit}/runtime differs from the released v{VERSION} that image "
            f"{DEFAULT_IMAGE} bakes (unreleased or unpulled runtime work). Fix one of: `git -C {kit} pull "
            "--no-rebase` (a release landed); run the brain's installed kit (`.agents/skills/…`); test this "
            "checkout's runtime with `--mode uv`; release it (`./refresh-brains.sh --release patch`).")

# Container constants, mirrored from replypen internal/workspace/docker.go so docker mode is
# byte-faithful to a real run.
CONTAINER_UID = 10001
CONTAINER_GID = 10001
HOME_DIR = "/home/agent"
BRAIN_MOUNT = "/brain"
MIRRORS_MOUNT = "/mirrors"

UV_MODE_CAVEATS = (
    "uv-mode fidelity gap: open internet (a call that passes here can be EGRESS_BLOCKED in prod), no "
    ":ro mounts (no EROFS), no container isolation, and it runs on THIS host's OS (e.g. macOS), not "
    "the image's Linux — same arm64 arch, but macOS wheels ≠ the manylinux wheels prod installs, so "
    "native deps and OS behaviour can still differ. Deps (lockfile) and the Python 3.12 interpreter "
    "ARE pinned. In a kit checkout, current runtime/ source wins over the installed wheel, so "
    "unreleased lib edits intentionally differ from prod until released. No /skills (platform skills "
    "mount in docker mode only). A green uv run is NOT a "
    "guaranteed-green prod run — gate with `--mode docker` before pushing."
)


def runtime_spec() -> str:
    """What `uv run --with` installs to provide `lib`. ONE pinned source of truth, resolved in order:

      1. `RC_RUNTIME_SPEC` env override — for testing an unreleased runtime/ or a fork.
      2. The sibling `runtime/` dir, if present — offline, the canonical local bytes (kit checkout,
         the CC plugin bundle, a local symlink install). Matches prod at a clean release tag while
         exposing unreleased source edits during development.
      3. Else the tag-pinned git spec — for a skill shipped without the kit alongside it. Needs network
         + repo read access. Pin the TAG, never float main (a push would silently change `lib`).
    """
    override = os.environ.get("RC_RUNTIME_SPEC")
    if override:
        return override
    if RUNTIME.is_dir():
        return str(RUNTIME)
    return f"replypen-runtime @ git+{REPO_URL}@v{VERSION}#subdirectory=runtime"


# ── env parsing (mirrors the Go host's secret.parseEnv, plus shell-quote tolerance) ───────────────
def _unquote(value: str) -> str:
    """Strip ONE matched pair of surrounding quotes — how a bash `source` (and most dotenv loaders)
    treat `KEY="value"`. The Go host's writer never quotes, so this is a no-op on a box-emitted
    `.env`; it only saves a hand-edited local `.env` from sending a quoted DSN to psycopg verbatim
    (`missing "=" after …`) and silently skipping every live test."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env(path: Path) -> dict[str, str]:
    """KEY=VALUE per line; skip blank/`#`; first `=` splits; key trimmed, value `_unquote`d.
    Mirrors internal/secret.parseEnv (the grammar the box stores `.env` in)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        eq = line.find("=")
        if eq < 0:
            continue
        key = line[:eq].strip()
        if key:
            out[key] = _unquote(line[eq + 1 :])
    return out


def resolve_brain_dir(arg: str | None) -> Path:
    """The brain is the cwd by default (you `cd` into the brain and invoke the skill); an explicit
    path overrides for multi-brain / scripted use."""
    return Path(arg).expanduser().resolve() if arg else Path.cwd().resolve()


def brain_secrets(brain_dir: Path, *, required: bool) -> dict[str, str] | None:
    """JUST the brain's `./.env` (no host env merged) — the keys docker mode injects. Prod injects
    only the project's secret set, never the operator's whole environment, so forwarding the merged
    `os.environ` into the container would both leak host vars (HOME, AWS_*, …) and shadow the image's
    own PYTHONPATH/HOME. Returns {} when absent and not `required`; None (caller errors) when required."""
    env_file = brain_dir / ".env"
    if env_file.is_file():
        return parse_env(env_file)
    if required:
        print(
            f"error: no .env at {env_file} — the live tier / a real run needs the project secrets. "
            "Run `rp project env pull` from this brain checkout.",
            file=sys.stderr,
        )
        return None
    return {}


def dsn_names(env: dict[str, str]) -> list[str]:
    """The project DSN env vars present (`*_DSN`), excluding the host store — mirrors lib.db.databases."""
    return sorted(k for k, v in env.items() if k.endswith("_DSN") and v and k != "DATABASE_URL")


def resolve_brain_script(brain_dir: Path, target: str) -> Path:
    """Resolve a brain-relative (or absolute) script path, rejecting any `..` escape out of the brain
    — the kit 'operates on `.`', and docker mode mounts only the brain, so a path that
    climbs out is always a mistake. Raises FileNotFoundError (missing) or ValueError (escapes)."""
    t = Path(target)
    script = t if t.is_absolute() else (brain_dir / t).resolve()
    if not t.is_absolute() and not (script == brain_dir or script.is_relative_to(brain_dir)):
        raise ValueError(f"{target!r} escapes the brain dir {brain_dir}")
    if not script.is_file():
        raise FileNotFoundError(script)
    return script


# ── mirrors (source repos the brain's fs helpers read) ────────────────────────────────────────────
def mirror_env_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError(f"invalid mirror name: {name!r} (want a Docker-safe repository slug)")
    return "RC_MIRROR_" + name.upper().replace("-", "_")


def _validate_mirror_names(mirrors: dict[str, Path]) -> None:
    env_names: dict[str, str] = {}
    for name in mirrors:
        env_name = mirror_env_name(name)
        if env_name in env_names:
            raise ValueError(
                f"mirror names {env_names[env_name]!r} and {name!r} both map to {env_name}"
            )
        env_names[env_name] = name


def declared_mirrors(brain_dir: Path) -> dict[str, Path]:
    """Read committed laptop paths from ``.replypen.toml [mirrors]``."""
    marker = _marker(brain_dir)
    if not marker.is_file():
        return {}
    with marker.open("rb") as fh:
        raw = tomllib.load(fh).get("mirrors", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{marker} [mirrors] must be a TOML table")
    out: dict[str, Path] = {}
    for name, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{marker} [mirrors].{name!r} must be a non-empty path string")
        path = Path(value).expanduser()
        if path.is_absolute():
            raise ValueError(f"{marker} [mirrors].{name!r} must be relative to the brain root")
        out[name] = (brain_dir / path).resolve()
    _validate_mirror_names(out)
    return out


def discover_mirrors(
    brain_dir: Path, mirrors_root: str | None, explicit: list[str]
) -> dict[str, Path]:
    """Resolve declared mirrors, then apply ``--mirrors-root`` and ``--mirror`` overrides."""
    out = declared_mirrors(brain_dir)
    if mirrors_root:
        root = Path(mirrors_root).expanduser().resolve()
        for name in out:
            out[name] = root / name
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    out[child.name] = child
    for spec in explicit:
        if "=" not in spec:
            print(f"warning: ignoring --mirror {spec!r} (want name=path)", file=sys.stderr)
            continue
        name, _, path = spec.partition("=")
        out[name.strip()] = Path(path.strip()).expanduser().resolve()
    _validate_mirror_names(out)
    return out


def require_mirrors(mirrors: dict[str, Path]) -> bool:
    """Fail loudly before a run can turn a declared missing mirror into a skip or empty mount."""
    missing = [(name, path) for name, path in mirrors.items() if not path.is_dir()]
    for name, path in missing:
        print(
            f"MirrorMissing: mirror {name!r} is missing; candidates: {path}",
            file=sys.stderr,
        )
    return not missing


# ── uv mode ───────────────────────────────────────────────────────────────────────────────────────
# Host vars the uv launcher / CPython need to even start. Everything ELSE in the operator's
# environment is dropped from the child, so the brain script sees only the project's `./.env` — just
# like docker mode injects only the brain's secrets. This stops a host-exported KEY from masking a
# missing `.env` entry (a false green) or leaking host creds (AWS_*, …) into an outbound call.
_HOST_PASSTHROUGH = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ",  # OS/shell essentials
    "LANG", "LC_ALL", "LC_CTYPE",                                        # locale → deterministic text
    "UV_CACHE_DIR", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",  # uv cache / managed-python
    "SSL_CERT_FILE", "SSL_CERT_DIR",                                       # TLS roots for uv fetches
})


def _host_base() -> dict[str, str]:
    """The minimal slice of the host environment uv/Python need to launch — NOT the whole environ."""
    return {k: v for k, v in os.environ.items()
            if k in _HOST_PASSTHROUGH or k.startswith(("UV_", "LC_"))}


def uv_child_env(
    brain_env: dict[str, str], extra_pythonpath: list[Path], mirrors_root: str | None,
    mirrors: dict[str, Path] | None = None,
) -> dict[str, str]:
    """The child env for a uv-mode invocation: the launcher essentials (`_host_base`) + the brain's
    `./.env` (the env source of truth, so it wins), the script's own dir(s) on PYTHONPATH (for
    siblings like `from ka import …`), the canonical local runtime source when present (so it wins
    over uv's cached wheel during development), and resolved `RC_MIRROR_*`/`RC_MIRRORS_ROOT` values
    so lib.fs reads local checkouts instead of the absent `/mirrors`."""
    child = _host_base()
    child.update(brain_env)  # the brain's .env wins over any passed-through host var
    local_runtime = [RUNTIME] if not os.environ.get("RC_RUNTIME_SPEC") and RUNTIME.is_dir() else []
    paths = [str(p) for p in [*extra_pythonpath, *local_runtime]]
    paths += [child["PYTHONPATH"]] if child.get("PYTHONPATH") else []
    if paths:
        child["PYTHONPATH"] = os.pathsep.join(paths)
    if mirrors_root:
        child["RC_MIRRORS_ROOT"] = str(Path(mirrors_root).expanduser().resolve())
    for name, path in (mirrors or {}).items():
        child[mirror_env_name(name)] = str(path)
    return child


def uv_base_cmd(extra_with: list[str] | None = None) -> list[str]:
    """`uv run` providing `lib` + its deps, pinned two ways so a green run can't drift from prod:
    `--python 3.12` (the image's interpreter) and, when the canonical `runtime/` is present, the
    committed `requirements.lock` (the full transitive closure) instead of a fresh resolve. Falls
    back to the tag-pinned git spec (`==` direct pins only) when the skill ships without the kit, or
    to a bare `--with` when `RC_RUNTIME_SPEC` overrides the source. `extra_with` adds tooling (pytest)."""
    cmd = ["uv", "run", "--no-project", "--python", PYTHON_VERSION]
    use_lock = not os.environ.get("RC_RUNTIME_SPEC") and RUNTIME.is_dir() and RUNTIME_LOCK.is_file()
    if use_lock:
        cmd += ["--with-requirements", str(RUNTIME_LOCK)]
    cmd += ["--with", runtime_spec()]
    for w in extra_with or []:
        cmd += ["--with", w]
    return cmd


def preflight_lib_db(child_env: dict[str, str], extra_with: list[str] | None = None) -> bool:
    """HARD-FAIL preflight (the footgun guard): prove `import lib.db` resolves in the SAME
    child env before the real script runs. A brain's `ka.py` guards `from lib import db` in
    try/except → `db = None`; a broken import would otherwise fail *silently* at call time. We make
    it loud up front."""
    check = subprocess.run(
        [*uv_base_cmd(extra_with), "python", "-c", "import lib.db"],
        env=child_env, capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(
            "error: `import lib.db` failed in the child env — a brain's try/except guard would "
            f"silently get db=None.\nPYTHONPATH={child_env.get('PYTHONPATH', '')}\n"
            f"{check.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


# ── docker mode ─────────────────────────────────────────────────────────────────────────────────
PLATFORM_SKILLS_MOUNT = "/skills"


@functools.cache
def platform_skills() -> dict[str, Path]:
    """The platform skill library prod mounts at /skills/<name> (`rp dev platform-skills pull`: the
    host's deployed bytes, cached under the user cache dir), resolved once per process so import smoke
    and pytest see one snapshot. `RC_PLATFORM_SKILLS_DIR=<dir>` mounts a local library instead (testing
    unreleased skills); `RC_PLATFORM_SKILLS=off` mounts none. Offline / no login → the last pulled
    snapshot, loudly; nothing cached → no /skills (a brain importing one then fails as it would without)."""
    if os.environ.get("RC_PLATFORM_SKILLS", "").lower() in ("0", "off", "false", "no"):
        return {}
    override = os.environ.get("RC_PLATFORM_SKILLS_DIR")
    if override:
        root, note = Path(override).expanduser().resolve(), f"RC_PLATFORM_SKILLS_DIR={override}"
    else:
        cli = "rp" if shutil.which("rp") else "rc"
        pull, why = _rp_json([cli, "dev", "platform-skills", "pull", "-o", "json"])
        if pull is None:
            pull, _ = _rp_json([cli, "dev", "platform-skills", "pull", "--offline", "-o", "json"])
            if pull is None:
                print(f"warning: no /skills mount (platform skills not pulled: {why}). Fix: `rp auth login`, "
                      "then `rp dev platform-skills pull` (needs a current rp: brain-dev-upgrade).", file=sys.stderr)
                return {}
            print(f"warning: platform skills NOT checked against the host ({why}); mounting the last pulled "
                  f"snapshot {pull['digest'][:12]}.", file=sys.stderr)
        root, note = Path(pull["dir"]), f"platform skills {pull['digest'][:12]}"
    skills = {d.name: d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith((".", "_"))} \
        if root.is_dir() else {}
    print(f"[/skills] {note}: {' '.join(skills) or 'none'}", file=sys.stderr)
    return skills


def _rp_json(cmd: list[str]) -> tuple[dict | None, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode:
        return None, (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}").splitlines()[-1]
    try:
        return json.loads(proc.stdout), ""
    except json.JSONDecodeError:
        return None, "non-JSON output"


def docker_run_args(
    *,
    image: str,
    brain_dir: Path,
    mirrors: dict[str, Path],
    env_names: list[str],
    workdir: str = BRAIN_MOUNT,
    command: list[str],
) -> list[str]:
    """`docker run` argv mirroring replypen internal/workspace/docker.go: read-only rootfs,
    all caps dropped, no-new-privileges, tmpfs /tmp + writable agent home, brain + mirrors `:ro`
    (kernel-enforced EROFS), env by `-e KEY` (values ride the docker client's own env, off the argv,
    exactly as prod keeps secrets off argv). One-shot `--rm` — no detached keep-alive needed for the
    pre-push check. Egress is left open (default bridge); the caller says so."""
    args = [
        "docker", "run", "--rm", "--init",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp",
        "--tmpfs", f"{HOME_DIR}:uid={CONTAINER_UID},gid={CONTAINER_GID}",
        "-v", f"{brain_dir}:{BRAIN_MOUNT}:ro",
    ]
    for name, path in mirrors.items():
        args += ["-v", f"{path}:{MIRRORS_MOUNT}/{name}:ro"]
    # Every platform skill, like prod's per-skill :ro mounts (prod filters by run scenario; a gate
    # mounts the whole library so any helper or test that builds on one imports).
    for name, path in platform_skills().items():
        args += ["-v", f"{path}:{PLATFORM_SKILLS_MOUNT}/{name}:ro"]
    for k in sorted(env_names):
        args += ["-e", k]  # value comes from the docker client's process env (off argv)
    args += ["-w", workdir, image, *command]
    return args


def run_docker(args: list[str], env_values: dict[str, str]) -> int:
    """Exec a docker argv built by `docker_run_args`, feeding the `-e KEY` values via the docker
    client's own environment (so they never appear on the command line)."""
    child = dict(os.environ)
    child.update(env_values)
    return subprocess.run(args, env=child).returncode


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False
