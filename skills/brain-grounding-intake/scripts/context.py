#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""What a production run receives before it reads anything, written to `$OUT/context.md`.

    uv run skills/brain-grounding-intake/scripts/context.py [--out DIR]

Read-only: `rc project database ls` for the database descriptions a run sees, `rc dev console
capabilities` for the databases actually mounted, `rc project repo ls` for the source mirrors
and the description a run reads about each, and the brain's own
tracked markdown with line counts. Judge every question from this chair, not from your
checkout: what the run has is what grounds the answer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan import find_brain_root, read_toml  # noqa: E402

MAX_BRAIN_LINES = 200


def rc_json(brain_root: Path, args: list[str]) -> object | None:
    """The parsed JSON of one read-only `rc` command, or None when it was unavailable."""
    proc = subprocess.run(["rc", *args], cwd=str(brain_root), text=True,
                          capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "null")
    except ValueError:
        return None


def brain_files(brain_root: Path) -> list[tuple[str, int]]:
    proc = subprocess.run(["git", "ls-files", "*.md"], cwd=str(brain_root), text=True,
                          capture_output=True, check=False)
    if proc.returncode != 0:
        return []
    rows = []
    for rel in sorted(proc.stdout.split()):
        path = brain_root / rel
        try:
            lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        rows.append((rel, lines))
    return rows


def database_lines(listed: object, capabilities: object) -> list[str]:
    """One line per database, description from `database ls`, flags from `capabilities`."""
    described = {str(row.get("id") or row.get("dsn") or ""): str(row.get("description") or "")
                 for row in (listed or []) if isinstance(row, dict)}
    mounted = (capabilities or {}).get("databases") if isinstance(capabilities, dict) else None
    out: list[str] = []
    for row in mounted or []:
        if not isinstance(row, dict):
            continue
        flags = []
        if row.get("pii_masked"):
            flags.append("PII masked")
        if row.get("scoped"):
            flags.append("tenant scoped")
        description = str(row.get("description") or described.get(str(row.get("env") or ""), ""))
        head = f"- `{row.get('name')}`" + (f" ({', '.join(flags)})" if flags else "")
        out.append(f"{head}: {description or 'no description'}")
    if not out:
        for env, description in sorted(described.items()):
            out.append(f"- `{env}`: {description or 'no description'}")
    return out or ["- none: a run has no database to look anything up in."]


def mirror_lines(repos: object) -> list[str]:
    """One line per source mirror from `rc project repo ls`: the description a run reads in its
    mirror roster, plus the branch it is pinned to."""
    if repos is None:
        return ["- unknown: `rc project repo ls` was not available."]
    out = []
    for item in repos if isinstance(repos, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id") or "?"
        branch = item.get("default_branch") or ""
        description = str(item.get("description") or "no description")
        out.append(f"- `/mirrors/{name}`" + (f" ({branch})" if branch else "") + f": {description}")
    return out or ["- none: a run reads no source code, only the brain and the data."]


def brain_lines(files: list[tuple[str, int]]) -> list[str]:
    groups: dict[str, list[tuple[str, int]]] = {}
    for rel, lines in files:
        groups.setdefault(rel.split("/")[0] if "/" in rel else ".", []).append((rel, lines))
    out: list[str] = []
    for group, rows in sorted(groups.items()):
        total = sum(lines for _, lines in rows)
        out.append(f"- {group}: {len(rows)} files, {total} lines")
        for rel, lines in rows:
            if len(out) >= MAX_BRAIN_LINES:
                out.append("- (list truncated)")
                return out
            out.append(f"  - {rel} ({lines})")
    return out or ["- none: this brain has no tracked markdown yet."]


def build(project: str, listed: object, capabilities: object, repos: object,
          files: list[tuple[str, int]], notes: list[str]) -> str:
    out = [f"# what a run of {project} receives", ""]
    out += notes + ([""] if notes else [])
    out += ["## databases", ""] + database_lines(listed, capabilities)
    out += ["", "## source mirrors", ""] + mirror_lines(repos)
    out += ["", "## brain files (tracked markdown, line counts)", ""] + brain_lines(files)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write what a production run receives")
    parser.add_argument("--out", type=Path,
                        help="output directory, default .rootcause/grounding-intake/<date>")
    args = parser.parse_args(argv)

    brain_root = find_brain_root()
    out_dir = Path(args.out or brain_root / ".rootcause" / "grounding-intake"
                   / date.today().isoformat()).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    project = str(read_toml(brain_root / ".rootcause.toml").get("project") or brain_root.name)
    listed = rc_json(brain_root, ["project", "database", "ls", "-o", "json", "--raw-output"])
    capabilities = rc_json(brain_root, ["dev", "console", "capabilities", "-o", "json", "--raw-output"])
    repos = rc_json(brain_root, ["project", "repo", "ls", "-o", "json", "--raw-output"])
    notes = []
    if listed is None:
        notes.append("`rc project database ls` was not available, database descriptions are "
                     "missing here.")
    if capabilities is None:
        notes.append("`rc dev console capabilities` was not available, mounts are missing here.")
    files = brain_files(brain_root)

    (out_dir / "context.md").write_text(build(project, listed, capabilities, repos, files, notes),
                                        encoding="utf-8")
    print(f"{project}: {len(files)} brain markdown files -> context.md")
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
