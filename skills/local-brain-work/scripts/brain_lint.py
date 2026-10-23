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


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"


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
    from lib.brain_lint import REPO_KINDS, detect_repo_kind, format_report, lint_brain
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


def _knowledge_brain(brain: Path) -> bool:
    """A knowledge-only brain (no skills/ or actions/, e.g. a help-centre corpus): `.replypen.toml`
    declares `project =` and runs write its `journal/`. Source repos carry `project =` only to scope
    `rp` and have no journal, so they stay out."""
    import tomllib

    try:
        cfg = tomllib.loads(_marker(brain).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return bool(cfg.get("project")) and not cfg.get("tenant") and (brain / "journal").is_dir()


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
    if (kind == "brain" and not (brain / "skills").is_dir() and not (brain / "actions").is_dir()
            and not _knowledge_brain(brain)):
        print(f"error: no skills/ or actions/ under {brain} — is this a brain checkout? "
              "(lint a tenant overlay or mirror with --as tenant|mirror)", file=sys.stderr)
        return 1

    scoped = bool(args.paths) and not args.all
    # run-hidden paths are already filtered in the core; the near-cliff doc-size WARN is for named paths
    findings = lint_brain(brain, kind, near_cliff=scoped)
    if scoped:
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
