"""Stdlib-only readers for brain markdown frontmatter, mirroring how the host reads it.

Shared by `lib.brain_lint` and the kit's commit/push hygiene gate (`brain_hygiene.py`), which loads
this file by path and must run without PyYAML or the rest of `lib`. Keep it stdlib-only.
"""

from __future__ import annotations

import fnmatch
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


# Frontmatter keys a mirrored-corpus page carries (help-centre article, video transcript written by a
# kit mirror skill): generated, rg-retrieved content whose size the brain author cannot change.
MIRRORED_CORPUS_KEYS = ("source_url", "video_url")


def mirrored_corpus(text: str) -> bool:
    """True when the frontmatter declares a top-level `source_url:` or `video_url:`."""
    block = frontmatter_lines(text)
    return block is not None and any(line.startswith(f"{key}:") for line in block
                                     for key in MIRRORED_CORPUS_KEYS)


# replypen internal/projection: `{{ key }}` slots and `<!-- rc:branch -->` regions of `<!-- rc:variant -->` blocks.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
BRANCH_RE = re.compile(r"(?s)<!--\s*rc:branch\s+(.*?)-->(.*?)<!--\s*/rc:branch\s*-->")
VARIANT_RE = re.compile(r"(?s)<!--\s*rc:variant\s+(.*?)-->(.*?)<!--\s*/rc:variant\s*-->")


def _segment(pat: str) -> str:
    """A Go `path.Match` segment as an fnmatch pattern: `[^` negation and `\\x` escapes."""
    return re.sub(r"\\(.)", r"[\1]", pat.replace("[^", "[!"))


def match_glob(pattern: str, rel: str) -> bool:
    """replypen projection.MatchGlob: `**` spans zero or more segments, other segments use path.Match."""
    def segs(pat: list[str], seg: list[str]) -> bool:
        while pat:
            if pat[0] == "**":
                return len(pat) == 1 or any(segs(pat[1:], seg[i:]) for i in range(len(seg) + 1))
            if not seg or not fnmatch.fnmatchcase(seg[0], _segment(pat[0])):
                return False
            pat, seg = pat[1:], seg[1:]
        return not seg

    return segs(pattern.split("/"), rel.split("/"))


def render_upper_bound(text: str, fills: dict[str, str] | None = None) -> str:
    """A templated doc as its longest tenant render: each branch region collapsed to the variant with
    the most UTF-8 bytes once placeholders are filled from `fills` (spec defaults; unknown slots keep
    their marker). Real tenant values can still be longer; the raw file, carrying every variant,
    overstates it."""
    def fill(part: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: (fills or {}).get(m.group(1), m.group(0)), part)

    def longest(m: re.Match[str]) -> str:
        bodies = (fill(v.group(2)) for v in VARIANT_RE.finditer(m.group(2)))
        return max(bodies, key=lambda b: len(b.encode("utf-8")), default="")

    return fill(BRANCH_RE.sub(longest, text))
