#!/usr/bin/env python3
"""Instant dependency-light lint for a ReplyPen brain checkout.

    brain_lint.py                      # whole tree (default)
    brain_lint.py --all                # whole tree, explicitly
    brain_lint.py AGENTS.md skills/    # only these files/dirs
    brain_lint.py --strict             # WARN findings fail too
    brain_lint.py --as mirror          # lint a repo the way the host reads it as /mirrors/<name>

The repo kind (brain / tenant overlay / mirror) decides which frontmatter the host reads; it is
auto-detected (`.replypen.toml` project/tenant, else a sibling brain's `[mirrors]` entry) and printed
when it is not `brain`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

RUNTIME = (Path(__file__).resolve().parents[3] / "runtime").resolve()
if RUNTIME.is_dir():
    sys.path.insert(0, str(RUNTIME))

_BOOTSTRAP_FLAG = "RC_BRAIN_LINT_BOOTSTRAPPED"

# Suffixes this linter can produce findings for (SKILL.md / cases *.md, action manifests, stray
# *.py). Explicit paths with any other suffix are ignored rather than treated as clean-but-scanned.
SCAN_SUFFIXES = {".md", ".py", ".yaml", ".yml"}

def _reexec_with_pyyaml() -> int:
    """The interpreter that picked us up lacks PyYAML (typical when `python3` resolves to an
    ephemeral `uv run` venv, e.g. as a `brain_git_sync.py --verify-command`). Re-exec once under
    `uv run --no-project --with pyyaml` instead of failing with an exit that aborts a sync."""
    uv = shutil.which("uv")
    if uv is None or os.environ.get(_BOOTSTRAP_FLAG):
        raise SystemExit(
            "error: PyYAML is required; run via `uv run --no-project --with pyyaml python brain_lint.py`"
        )
    cmd = [uv, "run", "--no-project", "--with", "pyyaml", "python", str(Path(__file__).resolve()), *sys.argv[1:]]
    return subprocess.run(cmd, env={**os.environ, _BOOTSTRAP_FLAG: "1"}).returncode


try:
    from lib.brain_lint import REPO_KINDS, detect_repo_kind, format_report, lint_brain, run_hidden
except ModuleNotFoundError as exc:
    if exc.name != "yaml":
        raise
    raise SystemExit(_reexec_with_pyyaml()) from None


def _select(paths: list[str], brain: Path) -> set[str]:
    """Brain-relative posix paths/dir-prefixes to keep findings for.

    Paths outside the brain, or whose suffix this linter never scans, are ignored — callers
    (brain_structure.py's changed-scope pass) hand over whatever git reported as changed.
    """
    selected: set[str] = set()
    for raw in paths:
        candidate = Path(raw)
        resolved = (candidate if candidate.is_absolute() else brain / candidate).resolve()
        try:
            rel = resolved.relative_to(brain).as_posix()
        except ValueError:
            continue
        if resolved.is_dir():
            selected.add(rel.rstrip("/") + "/")
        elif resolved.suffix.lower() in SCAN_SUFFIXES:
            selected.add(rel)
    return selected


def _run_hidden(brain: Path) -> Callable[[str], bool]:
    """Run-hidden trees (`.replypenignore` / `.rcignore` and the host's always-hidden paths, via the
    git-free `lib.brain_lint.run_hidden`, plus the conventional `_internal/`): maintainer-only content
    absent from every run, so no linter judges it — except the rules that exist to say a declaration
    there is never read (`_ALWAYS_REPORTED`)."""
    hidden = run_hidden(brain)
    return lambda rel: rel.startswith("_internal/") or hidden(rel)


_ALWAYS_REPORTED = frozenset({"frontmatter-scope"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain_lint.py", description=__doc__)
    parser.add_argument("--brain", help="brain dir (default: cwd)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when WARN findings exist")
    parser.add_argument("--as", dest="kind", choices=REPO_KINDS,
                        help="how the host consumes this repo (default: auto-detect)")
    parser.add_argument("--all", action="store_true",
                        help="lint the whole brain tree (the default when no paths are given)")
    parser.add_argument("paths", nargs="*",
                        help="lint only these files/dirs (relative to the brain dir); paths whose "
                             "suffix this linter does not scan are ignored")
    args = parser.parse_args(argv)

    brain = Path(args.brain).expanduser().resolve() if args.brain else Path.cwd().resolve()
    kind, reason = (args.kind, "--as") if args.kind else detect_repo_kind(brain)
    if kind == "brain" and not (brain / "skills").is_dir() and not (brain / "actions").is_dir():
        print(f"error: no skills/ or actions/ under {brain} — is this a brain checkout? "
              "(lint a tenant overlay or mirror with --as tenant|mirror)", file=sys.stderr)
        return 1

    findings = lint_brain(brain, kind)
    hidden = _run_hidden(brain)
    findings = [f for f in findings if f.rule in _ALWAYS_REPORTED or not hidden(f.path.split(":", 1)[0])]
    if args.paths and not args.all:
        selected = _select(args.paths, brain)
        dirs = tuple(p for p in selected if p.endswith("/"))
        # Some rules append `:<line>` to the path they report; scope on the file part.
        findings = [f for f in findings
                    if (rel := f.path.split(":", 1)[0]) in selected or rel.startswith(dirs)]
    if kind != "brain":
        print(f"brain lint: linting as {kind} ({reason})")
    print(format_report(findings))
    return int(any(f.level == "FAIL" or (args.strict and f.level == "WARN") for f in findings))


if __name__ == "__main__":
    raise SystemExit(main())
