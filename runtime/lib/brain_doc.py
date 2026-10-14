"""Stdlib-only readers for brain markdown frontmatter, mirroring how the host reads it.

Shared by `lib.brain_lint` and the kit's commit/push hygiene gate (`brain_hygiene.py`), which loads
this file by path and must run without PyYAML or the rest of `lib`. Keep it stdlib-only.
"""

from __future__ import annotations

import re

# Mirrors treeview's frontmatterCap: the host only reads frontmatter whose closing fence lies within it.
FRONTMATTER_CAP = 8 << 10


def frontmatter_lines(text: str) -> list[str] | None:
    """The lines between the leading `---` fences exactly as treeview.frontmatterLines sees them:
    opening fence at byte 0, closing fence within FRONTMATTER_CAP BYTES; None when the host reads no block."""
    lines = text.encode("utf-8")[:FRONTMATTER_CAP].decode("utf-8", "ignore").split("\n")
    if len(lines) < 2 or lines[0].rstrip("\r") != "---":
        return None
    close = next((i for i, line in enumerate(lines[1:], start=1) if line.rstrip("\r") == "---"), None)
    return None if close is None else lines[1:close]


def include_in(text: str) -> set[str]:
    """`include_in` entries, read like treeview.frontmatterList: top-level key, flow or block list,
    frontmatter closing within FRONTMATTER_CAP. Line scan, not YAML — what it can't read the host
    doesn't read either."""
    block = frontmatter_lines(text)
    if block is None:
        return set()
    for i, raw in enumerate(block):
        line = raw.rstrip("\r")
        if not line.startswith("include_in:"):
            continue
        rest = re.sub(r"(^|\s)#.*$", "", line[len("include_in:"):]).strip()
        if rest:
            items = rest.strip("[]").split(",")
        else:
            items = []
            for nxt in block[i + 1:]:
                item = re.sub(r"(^|\s)#.*$", "", nxt).strip()
                if not item:
                    continue
                if not item.startswith("- "):
                    break
                items.append(item[2:])
        return {v for v in (unquote(p.strip()) for p in items) if v}
    return set()


def unquote(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0] else v


def strip_frontmatter(text: str) -> str:
    """The body the host pastes (hardload.go's stripHardLoadFrontmatter + TrimRight "\\n")."""
    first, sep, rest = text.partition("\n")
    if sep and first.rstrip("\r") == "---":
        offset = len(first) + 1
        for line in rest.split("\n"):
            if line.rstrip("\r") == "---":
                return text[offset + len(line) + 1:].lstrip("\n").rstrip("\n")
            offset += len(line) + 1
    return text.rstrip("\n")
