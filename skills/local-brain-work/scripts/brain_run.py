# /// script
# requires-python = ">=3.11"
# ///
"""Run one brain grounding script the way the workspace container would — fast `uv` or faithful `docker`.

Brain-dir-relative: `cd` into a `replypen-brain-<project>` checkout and invoke; it operates on `.`.
Reads `./.env`, resolves `lib` from `replypen-runtime` (uv) or the published image (docker). No
`accounts.yml`, no project name, no `code_root`.

    uv run brain_run.py skills/records/scripts/lookup_person.py --tenant X --email a@b.com
    uv run brain_run.py -m lib.db --list
    uv run brain_run.py --mode docker skills/records/scripts/lookup_person.py --email a@b.com
    uv run brain_run.py --brief                      # map the brain (env keys, DBs, mirrors, skills)
    uv run brain_run.py --brain ~/code/replypen/replypen-brain-kampadmin -m lib.db --list

Kit flags come FIRST; everything from the script path (or `-m module`) onward is passed through to the
script verbatim — including its own `--flags`, even ones that share a kit flag's name."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import brain_env as E

# Kit flags that take a value, and the boolean ones — used to split the kit's own argv from the
# opaque script passthrough so a script's `--mode`/`--image`/etc. can't be eaten by argparse.
_VALUE_FLAGS = {"--brain", "--mode", "--image", "--mirrors-root", "--mirror"}
_BOOL_FLAGS = {"--brief", "-h", "--help"}


def _split_argv(argv: list[str]) -> tuple[list[str], str | None, str | None, list[str]]:
    """Return (kit_argv, module, target, rest). Scan leading kit flags; the first bare token is the
    script `target` (or `-m` gives `module`), and everything after it is opaque passthrough (`rest`).
    A leading `--` forces the next token to be the target regardless of its leading dash."""
    kit: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            if i + 1 < n:
                return kit, None, argv[i + 1], argv[i + 2 :]
            return kit, None, None, []
        if tok == "-m":
            return kit, (argv[i + 1] if i + 1 < n else None), None, argv[i + 2 :]
        if tok in _VALUE_FLAGS:
            kit += argv[i : i + 2]
            i += 2
            continue
        if tok in _BOOL_FLAGS or (tok.startswith("--") and tok.split("=", 1)[0] in _VALUE_FLAGS):
            kit.append(tok)
            i += 1
            continue
        # First non-kit token: the script path. Everything after it belongs to the script.
        return kit, None, tok, argv[i + 1 :]
    return kit, None, None, []


def _brief(brain_dir: Path, mirrors: dict[str, Path]) -> int:
    """Map the brain without running anything: env key NAMES (values redacted),
    the project DBs, the mirrors the runner can see, and the skills + their scripts."""
    print(f"brain: {brain_dir}")
    env_file = brain_dir / ".env"
    file_env = E.parse_env(env_file) if env_file.is_file() else {}
    file_keys = sorted(file_env)
    print(f"\n.env keys ({len(file_keys)}):" if file_keys else "\n.env: (none)")
    for k in file_keys:
        print(f"  {k}")
    dbs = E.dsn_names(file_env)
    print(f"\ndatabases ({len(dbs)}):")
    for d in dbs:
        print(f"  {d}")
    resolved = sum(path.is_dir() for path in mirrors.values())
    missing = len(mirrors) - resolved
    print(f"\nmirrors configured/resolved/missing: {len(mirrors)}/{resolved}/{missing}")
    for name, path in mirrors.items():
        print(f"  {name} -> {path} ({'resolved' if path.is_dir() else 'MISSING'})")
    skills_dir = brain_dir / "skills"
    print("\nskills:")
    if skills_dir.is_dir():
        for skill in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            scripts = sorted((skill / "scripts").glob("*.py")) if (skill / "scripts").is_dir() else []
            print(f"  {skill.name}/  ({len(scripts)} script(s))")
            for s in scripts:
                print(f"    skills/{skill.name}/scripts/{s.name}")
    else:
        print("  (no skills/ dir — is this a brain checkout?)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    kit_argv, module, target, rest = _split_argv(argv)

    p = argparse.ArgumentParser(
        prog="brain_run.py", allow_abbrev=False,
        description="Run a brain grounding script (uv or docker mode). Kit flags precede the script path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--brain", help="brain dir (default: cwd)")
    p.add_argument("--mode", choices=("uv", "docker"), default="uv",
                   help="uv = fast inner loop (default); docker = faithful pre-push gate")
    p.add_argument("--image", default=E.DEFAULT_IMAGE, help="workspace image for docker mode")
    p.add_argument("--mirrors-root", help="dir whose immediate subdirs are source mirrors (uv: RC_MIRRORS_ROOT)")
    p.add_argument("--mirror", action="append", default=[], metavar="name=path",
                   help="override one declared mirror (repeatable; uv env or docker mount)")
    p.add_argument("--brief", action="store_true", help="map the brain and exit (no run)")
    args = p.parse_args(kit_argv)

    brain_dir = E.resolve_brain_dir(args.brain)
    if not brain_dir.is_dir():
        print(f"error: brain dir not found: {brain_dir}", file=sys.stderr)
        return 1
    E.kit_autoupdate(brain_dir)

    try:
        mirrors = E.discover_mirrors(brain_dir, args.mirrors_root, args.mirror)
    except (OSError, ValueError) as exc:
        print(f"error: cannot resolve mirrors: {exc}", file=sys.stderr)
        return 1
    if args.brief:
        return _brief(brain_dir, mirrors)

    if module is None and target is None:
        print("error: give a script path, or -m <module>", file=sys.stderr)
        return 2
    if module == "":
        print("error: -m needs a module name", file=sys.stderr)
        return 2
    if not E.require_mirrors(mirrors):
        return 1

    if args.mode == "uv":
        return _run_uv(brain_dir, mirrors, args, module, target, rest)
    return _run_docker(brain_dir, mirrors, args, module, target, rest)


def _run_uv(brain_dir: Path, mirrors: dict[str, Path], args, module, target, rest) -> int:
    secrets = E.brain_secrets(brain_dir, required=False)  # script sees ONLY the brain's .env, like prod
    if module is not None:
        invocation, script_dir = ["-m", module, *rest], None
    else:
        try:
            script = E.resolve_brain_script(brain_dir, target)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        invocation, script_dir = [str(script), *rest], script.parent
    child = E.uv_child_env(
        secrets, [script_dir] if script_dir else [], args.mirrors_root, mirrors
    )
    child["RC_LOCAL_BRAIN_RUN"] = "1"
    if not E.preflight_lib_db(child):
        return 1
    print(f"[uv mode] {E.UV_MODE_CAVEATS}", file=sys.stderr)
    return subprocess.run([*E.uv_base_cmd(), "python", *invocation], env=child).returncode


def _run_docker(brain_dir: Path, mirrors: dict[str, Path], args, module, target, rest) -> int:
    if not E.docker_available():
        print("error: docker not available (is colima/Docker running?)", file=sys.stderr)
        return 1
    if module is not None:
        command = ["python", "-m", module, *rest]
    else:
        try:
            script = E.resolve_brain_script(brain_dir, target)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rel = script.relative_to(brain_dir)  # safe: resolve_brain_script forbids escapes
        command = ["python", f"{E.BRAIN_MOUNT}/{rel.as_posix()}", *rest]
    # Inject ONLY the brain's .env (not the host environ) — matches prod, no host-var leakage.
    secrets = E.brain_secrets(brain_dir, required=False)
    _warn_missing_mirrors(mirrors)
    print(f"[docker mode] image={args.image} — egress is OPEN (default bridge), not the prod "
          "default-deny firewall.", file=sys.stderr)
    run_args = E.docker_run_args(
        image=args.image, brain_dir=brain_dir, mirrors=mirrors,
        env_names=list(secrets), command=command,
    )
    return E.run_docker(run_args, secrets)


def _warn_missing_mirrors(mirrors: dict[str, Path]) -> None:
    for name, path in mirrors.items():
        if not path.is_dir():
            print(f"warning: mirror {name!r} path missing: {path} — fs helpers for it will fail",
                  file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
