# /// script
# requires-python = ">=3.11"
# ///
"""Run a brain's pytest tiers the way the workspace container would — offline by default, live on demand.

Brain-dir-relative: `cd` into a `replypen-brain-<project>` checkout and invoke; collects `./skills`
plus every other top-level dir holding tests (`actions/`, `tests/`, …) — not `_internal/` or other
`_`/`.` operator dirs. Tiers (from `lib.livecheck`):

  * **offline (default)** — `-m "not live"`: hermetic L1 fixture tests + the brain description lint + action-script hygiene lint
                            (`lib.brain_lint_pytest`, injected — no per-brain test file). No DSN, no network.
                            Tests requesting `pii_rows` (`lib.pii_pytest`) run twice: plain and PII-masked rows.
  * **live** (`--live`)   — `-m live`: L2 schema canary + L3 render-smoke, read-only against the
                            project's real prod DSN (the brain's `./.env`).
  * **gated** (`--require-live`) — `--live` plus fail-if-no-live-test-ran (CI/cron drift alarm).

    uv run brain_test.py                       # offline tier
    uv run brain_test.py --live                # + live tier (read-only prod)
    uv run brain_test.py --require-live         # gated
    uv run brain_test.py --mode docker --live   # faithful: same image prod runs
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import brain_env as E

# Top-level dirs never collected: operator tooling (`_internal/`, any `_`/`.` prefix) and vendored trees.
_SKIP_DIRS = {"node_modules", "venv"}


def test_dirs(brain_dir: Path) -> list[str]:
    """`skills` first (brain lint finds the brain root from it), then each other top-level dir with a
    `test_*.py`/`*_test.py` in it — action preflight tests, a brain's top-level `tests/`."""
    found = []
    for child in sorted(brain_dir.iterdir()):
        if (child.name == "skills" or child.name[0] in "._" or child.name in _SKIP_DIRS
                or not child.is_dir() or child.is_symlink()):
            continue
        if any("node_modules" not in f.parts for pat in ("test_*.py", "*_test.py") for f in child.rglob(pat)):
            found.append(child.name)
    return ["skills", *found] if (brain_dir / "skills").is_dir() else found


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_test.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--brain", help="brain dir (default: cwd)")
    p.add_argument("--mode", choices=("uv", "docker"), default="uv")
    p.add_argument("--image", default=E.DEFAULT_IMAGE, help="workspace image for docker mode")
    p.add_argument("--mirrors-root", help="dir whose immediate subdirs are source mirrors")
    p.add_argument("--mirror", action="append", default=[], metavar="name=path")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="run the live tier (schema canary + render-smoke)")
    g.add_argument("--require-live", action="store_true",
                   help="--live, and error if no live test ran (no DSN / nothing collected)")
    p.add_argument("--tenant", metavar="ID",
                   help="override the live tier's auto-picked tenant id (lib.livecheck.pick_tenant) — "
                        "pin the schema canary to one tenant. Best-effort; numeric ids are coerced to int.")
    # Every unknown arg (pytest flags like `-k expr`/-x/--tb, test paths) passes through to pytest in
    # its original order; a positional here would split `-k expr` into `expr … -k`.
    args, args.pytest_args = p.parse_known_args(sys.argv[1:] if argv is None else argv)

    brain_dir = E.resolve_brain_dir(args.brain)
    dirs = test_dirs(brain_dir)
    if not dirs:
        if args.require_live:
            print(f"error: --require-live but {brain_dir} has no skills/ or test dirs — no live test can run",
                  file=sys.stderr)
            return 1
        if any((brain_dir / f).is_file() for f in (".replypen.toml", ".rootcause.toml")):
            # Docs-only brain (tenant overlay): nothing to import or test; brain_lint.py covers it.
            print("brain_test: no skills/ or test dirs (docs-only brain) — nothing to test", file=sys.stderr)
            return 0
        print(f"error: no skills/ under {brain_dir} — is this a brain checkout?", file=sys.stderr)
        return 1

    live = args.live or args.require_live
    marker = "live" if live else "not live"
    # The adapter injects one offline lint item and prints one compact end-of-tier report.
    # --import-mode=importlib: brains keep one `tests/test_preflight.py` per action dir with no
    # `__init__.py`, so rootdir-based import would collide on the shared basename (audit 2026-09-05);
    # `lib.sibling_modules` does the same for their bare `import preflight` / `import script`.
    # A brain-root conftest.py (e.g. a `pii_columns` override) reaches every collected dir; rootdir
    # becomes the brain root as soon as a second dir is collected.
    print(f"collecting: {' '.join(d + '/' for d in dirs)}", file=sys.stderr)
    pytest_args = ["-p", "lib.livecheck", "-p", "lib.brain_lint_pytest", "-p", "lib.mirror_skip",
                   "-p", "lib.pii_pytest", "-p", "lib.sibling_modules", "--import-mode=importlib", "-m", marker, "-q", *args.pytest_args]
    if args.require_live:
        pytest_args.append("--require-live")

    try:
        mirrors = E.discover_mirrors(brain_dir, args.mirrors_root, args.mirror)
    except (OSError, ValueError) as exc:
        print(f"error: cannot resolve mirrors: {exc}", file=sys.stderr)
        return 1
    if not E.require_mirrors(mirrors):
        return 1

    # --tenant pins the live tier's canary tenant via RC_LIVE_TENANT (read by lib.livecheck.pick_tenant);
    # inert offline. Only meaningful with --live, so warn if it'd be a no-op.
    if args.tenant and not live:
        print("warning: --tenant only affects the live tier; ignored without --live/--require-live.",
              file=sys.stderr)

    if args.mode == "uv":
        return _run_uv(brain_dir, dirs, mirrors, live, pytest_args, args.mirrors_root, args.tenant)
    return _run_docker(brain_dir, dirs, mirrors, live, pytest_args, args)


def _run_uv(brain_dir: Path, dirs: list[str], mirrors: dict[str, Path], live: bool,
            pytest_args: list[str],
            mirrors_root: str | None, tenant: str | None) -> int:
    # The live tier needs the DSN; the offline tier runs without a .env. Script sees ONLY the
    # brain's .env (+ launcher essentials), like prod — never the operator's whole environment.
    secrets = E.brain_secrets(brain_dir, required=live)
    if secrets is None:
        return 1
    child = E.uv_child_env(secrets, [], mirrors_root, mirrors)
    if tenant:
        child["RC_LIVE_TENANT"] = tenant
    if not E.preflight_lib_db(child, extra_with=["pytest"]):
        return 1
    print(f"[uv mode] {E.UV_MODE_CAVEATS}", file=sys.stderr)
    if dirs[0] == "skills":  # import smoke covers skills/ scripts; an actions-only brain has none
        smoke = subprocess.run(
            [*E.uv_base_cmd(extra_with=["pytest"]), "python", "-m", "lib.import_smoke", str(brain_dir)],
            env=child,
        )
        if smoke.returncode:
            return smoke.returncode
    cmd = [*E.uv_base_cmd(extra_with=["pytest"]), "pytest", *(str(brain_dir / d) for d in dirs), *pytest_args]
    return subprocess.run(cmd, env=child).returncode


def _run_docker(brain_dir: Path, dirs: list[str], mirrors: dict[str, Path], live: bool, pytest_args: list[str],
                args) -> int:
    if not E.docker_available():
        print("error: docker not available (is colima/Docker running?)", file=sys.stderr)
        return 1
    if skew := E.runtime_skew():  # before pytest: an image/kit mismatch is not a brain test failure
        print(f"error: {skew}", file=sys.stderr)
        return 1
    if subprocess.run(["docker", "run", "--rm", "-v", f"{brain_dir.resolve()}:/brain:ro", "--entrypoint", "test",
                       args.image, "-d", f"/brain/{dirs[0]}"], capture_output=True).returncode:
        # A path the Docker VM doesn't share (colima default: only $HOME, so /tmp) mounts as an empty /brain.
        print(f"error: docker cannot read the bind mount {brain_dir}: choose a Docker-shared location. A brain "
              "worktree belongs beside its checkout so relative mirror paths resolve too: `git worktree add "
              "--detach ../<repo>-wt-<task> origin/main`.", file=sys.stderr)
        return 1
    # Inject ONLY the brain's .env (not the host environ) — matches prod, no host-var leakage.
    secrets = E.brain_secrets(brain_dir, required=live)
    if secrets is None:
        return 1
    if args.tenant:
        secrets = {**secrets, "RC_LIVE_TENANT": args.tenant}  # rides `-e RC_LIVE_TENANT`, value off argv
    for name, path in mirrors.items():
        if not path.is_dir():
            print(f"warning: mirror {name!r} path missing: {path}", file=sys.stderr)
    print(f"[docker mode] image={args.image} — egress is OPEN (default bridge), not the prod "
          "default-deny firewall.", file=sys.stderr)
    if dirs[0] == "skills":  # import smoke covers skills/ scripts; an actions-only brain has none
        smoke_args = E.docker_run_args(
            image=args.image, brain_dir=brain_dir, mirrors=mirrors,
            env_names=list(secrets), command=["python", "-m", "lib.import_smoke", E.BRAIN_MOUNT],
        )
        if smoke_code := E.run_docker(smoke_args, secrets):
            return smoke_code
    # pytest over the read-only /brain test dirs; lib + the livecheck plugin are baked into the image.
    # `-p no:cacheprovider`: /brain is :ro (EROFS), and the cache is useless for a one-shot --rm run.
    command = ["pytest", "-p", "no:cacheprovider", *(f"{E.BRAIN_MOUNT}/{d}" for d in dirs), *pytest_args]
    run_args = E.docker_run_args(
        image=args.image, brain_dir=brain_dir, mirrors=mirrors,
        env_names=list(secrets), command=command,
    )
    return E.run_docker(run_args, secrets)


if __name__ == "__main__":
    raise SystemExit(main())
