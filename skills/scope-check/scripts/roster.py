#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Resolve an audience label ("regular-admin") to the principal `(kind, id)` the host asserts.

The roster is the `[[audience]]` list of `_internal/scopecheck-<project>.toml` (label/kind/id, the
same file `scope_smoke.py --config` reads). It is looked up in the brain checkout and in every
`[mirrors]` path of the brain's `.replypen.toml` (paths relative to the brain root), first hit
wins per label. A literal `kind:id` or `label=kind:id` bypasses the roster.

Importable by path from any skill dir:

    sys.path.insert(0, str(<kit>/skills/scope-check/scripts))
    from roster import resolve_principal
    kind, external_id = resolve_principal("regular-admin", brain_root, "kampadmin-support")
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"


class RosterError(ValueError):
    """Unknown label or unreadable roster; the message lists what is known and where it looked."""


def brain_project(brain_root: Path) -> str | None:
    """The `project` of the brain checkout's `.replypen.toml`, or None."""
    path = _marker(Path(brain_root))
    if not path.is_file():
        return None
    return tomllib.loads(path.read_text()).get("project")


def roster_paths(brain_root: Path, project: str) -> list[Path]:
    """Candidate roster files: the brain checkout first, then each local mirror checkout."""
    brain_root = Path(brain_root)
    roots = [brain_root]
    config = _marker(brain_root)
    if config.is_file():
        mirrors = tomllib.loads(config.read_text()).get("mirrors") or {}
        for rel in mirrors.values():
            path = Path(str(rel)).expanduser()
            roots.append(path if path.is_absolute() else (brain_root / path))
    return [root.resolve() / "_internal" / f"scopecheck-{project}.toml" for root in roots]


def load_roster(brain_root: Path, project: str) -> tuple[dict[str, tuple[str, str]], list[Path]]:
    """`{label: (kind, id)}` merged over every roster file found, plus the paths searched."""
    paths = roster_paths(brain_root, project)
    roster: dict[str, tuple[str, str]] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise RosterError(f"{path}: {exc}") from exc
        for entry in data.get("audience") or []:
            label = str(entry.get("label") or "").strip()
            kind = str(entry.get("kind") or "").strip()
            external_id = str(entry.get("id") or entry.get("external_id") or "").strip()
            if label and kind and external_id:
                roster.setdefault(label, (kind, external_id))
    return roster, paths


def resolve_principal(label: str, brain_root: Path, project: str) -> tuple[str, str]:
    """`label` → `(kind, id)`. Accepts a roster label, `kind:id`, or `label=kind:id`."""
    label = (label or "").strip()
    literal = label.partition("=")[2] if "=" in label else label
    kind, sep, external_id = literal.partition(":")
    if sep and kind.strip() and external_id.strip():
        return kind.strip(), external_id.strip()
    roster, paths = load_roster(brain_root, project)
    if label in roster:
        return roster[label]
    known = ", ".join(sorted(roster)) or "none"
    searched = "\n  ".join(str(p) for p in paths)
    raise RosterError(
        f"unknown audience {label!r} for project {project!r}; known labels: {known}. "
        f"Pass kind:id literally, or add an [[audience]] label/kind/id to one of:\n  {searched}")
