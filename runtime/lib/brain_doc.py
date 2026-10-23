"""Stdlib-only readers for brain markdown (frontmatter, tag scope, on-demand doc size), mirroring the host.

Shared by `lib.brain_lint` and the kit's commit/push hygiene gate (`brain_hygiene.py`), which loads
this file by path and must run without PyYAML or the rest of `lib`. Keep it stdlib-only.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Callable

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


# ---- Which tags the host reads where ------------------------------------------------------------
# Every constant below names the replypen source it mirrors; change them together with the host.

# include_in roles the host collects: grounding/agent/principal in internal/grounding/hardload.go
# (roleGrounding/roleAgent/rolePrincipal), triage in internal/triage/brainknowledge.go.
INCLUDE_IN_ROLES = ("triage", "grounding", "agent", "principal")
# A MIRROR repo is scanned for include_in only at its root *.md and recursively under these dirs —
# replypen internal/grounding/hardload.go `mirrorScanSubdirs`. Brains and tenant overlays are scanned whole.
MIRROR_SCAN_DIRS = (".agents", ".claude", "doc", "docs", "skills")
# Dirs treeview.TaggedFiles prunes in every scan (internal/treeview/tagged.go + bootstrap.go skipDirs).
TAG_PRUNED_DIRS = frozenset({".git", ".replypen", ".rootcause", "__pycache__", "node_modules", ".venv", "venv",
                             ".pytest_cache", ".ruff_cache", ".mypy_cache"})
# How a repo is consumed decides which tags the host reads (see detect_repo_kind).
REPO_KINDS = ("brain", "tenant", "mirror")


def detect_repo_kind(root: str | Path) -> tuple[str, str]:
    """(kind, reason): `tenant` when the committed `.replypen.toml` names a tenant; `mirror` when a
    brain checkout beside it lists this repo in its `.replypen.toml [mirrors]` (the table brain_run.py
    resolves) — checked before `project =`, which a source repo may carry only to scope `rp`; else
    `brain`, the widest include_in scope."""
    import tomllib

    root = Path(root).resolve()

    def toml(path: Path) -> dict:
        try:
            return tomllib.loads(path.read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            return {}

    own = toml(_marker(root))
    if own.get("tenant"):
        return "tenant", f".replypen.toml: project {own.get('project')!r}, tenant {own['tenant']!r}"
    # Brains declare mirrors as paths relative to themselves; look in the sibling and cousin checkouts
    # (`../brain`, `../../org/brain`). Skipped at filesystem depth 1 (a container's /brain).
    if root.parent != root.parent.parent:
        for cfg in sorted(c for name in (".replypen.toml", ".rootcause.toml")
                         for c in [*root.parent.glob(f"*/{name}"), *root.parent.parent.glob(f"*/*/{name}")]):
            mirrors = toml(cfg).get("mirrors") if cfg.parent != root else None
            if not isinstance(mirrors, dict):
                continue
            for value in mirrors.values():
                if isinstance(value, str) and (cfg.parent / value).resolve() == root:
                    return "mirror", f"listed in {cfg.parent.name}/{cfg.name} [mirrors]"
    if own.get("project"):
        return "brain", f".replypen.toml: project {own['project']!r}"
    return "brain", "no .replypen.toml identity and no brain lists it under [mirrors]"


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"


def unread_roles(rel: str, kind: str) -> set[str]:
    """include_in roles the host never collects from `rel` in a repo of this kind."""
    parts = rel.split("/")
    if TAG_PRUNED_DIRS.intersection(parts[:-1]):
        return set(INCLUDE_IN_ROLES)
    if kind == "mirror" and len(parts) > 1 and parts[0] not in MIRROR_SCAN_DIRS:
        return set(INCLUDE_IN_ROLES)
    # Triage reads only the flat project brain (internal/triage/brainknowledge.go BrainKnowledge).
    return {"triage"} if kind != "brain" else set()


def hard_loaded(rel: str, text: str, kind: str) -> bool:
    """True when the host pastes `rel` into a prompt instead of leaving it to a `cat`: an `include_in`
    role it reads there, or the root AGENTS.md of a brain or tenant overlay (pasted whole every run)."""
    if rel == "AGENTS.md" and kind != "mirror":
        return True
    return bool((include_in(text) - unread_roles(rel, kind)) & set(INCLUDE_IN_ROLES))


# ---- On-demand docs and the bash spill cliff ----------------------------------------------------
# One measure for lint `doc-size` (publish) and hygiene `doc-spill` (pre-push), so a push the hygiene
# gate passes does not FAIL at publish. Untagged docs are read on demand with `cat`, and bash replaces a
# stream over 6000 bytes with a 2000+1000-char preview (replypen internal/tool/tool.go
# defaultBashSpillThreshold): past it the run rarely sees the middle (76% of 6-7 KB doc reads never
# fetched the spill, 10-12..10-19). So the limit IS the cliff, on the whole file as `cat` prints it
# (frontmatter included; a templated doc at its longest render). Change with the host threshold.
ON_DEMAND_DOC_LIMIT = 6000
# Dirs whose markdown no run reads as a doc: run notes, test fixtures, developer docs, operator-only
# `_internal/`, dependency trees; every dot-dir too (kit installs, `.github/`).
ON_DEMAND_SKIP_DIRS = frozenset({"journal", "tests", "docs", "_internal", "node_modules", "__pycache__", "venv"})


def projection_renderer(spec: object) -> Callable[[str, str], str]:
    """(rel, text) -> the text a run `cat`s, given the decoded projection.yaml (None: no projection):
    a projection-templated doc at its longest render (`render_upper_bound` with the spec defaults),
    anything else verbatim."""
    if not isinstance(spec, dict):
        return lambda rel, text: text
    fills = {k: str(v["default"]) for k, v in (spec.get("placeholders") or {}).items()
             if isinstance(v, dict) and v.get("default") is not None}
    globs = spec.get("templated_globs") or ["**/*.md"]
    return lambda rel, text: (render_upper_bound(text, fills) if any(match_glob(g, rel) for g in globs)
                              else text)


def on_demand_doc_bytes(rel: str, raw: str, loaded: bool,
                        render: Callable[[str, str], str] | None = None) -> int | None:
    """The bytes a run's `cat` of `rel` prints when it is an on-demand doc, else None. `raw` keeps its
    line endings (CRLF counts twice); `loaded` is `hard_loaded(...)`; `render` a `projection_renderer`.
    Not on-demand: hard-loaded, a mirrored corpus page, or under ON_DEMAND_SKIP_DIRS / a dot-dir."""
    if (loaded or not rel.endswith(".md") or mirrored_corpus(raw)
            or any(d in ON_DEMAND_SKIP_DIRS or d.startswith(".") for d in rel.split("/")[:-1])):
        return None
    return len((render(rel, raw) if render else raw).encode("utf-8"))
