"""Offline brain description + action hygiene lint.

Every routable brain file must carry a `description:` the bootstrap tree can render on its line, or
it is **invisible to the grounding step**: retrieval is `rg`-driven and lexical, so a file whose
tree line has no customer-vocabulary gloss never gets grepped. This lint holds the brain content up to
that contract:

  * **FAIL** — a `skills/*/SKILL.md` or `skills/cases/*.md` whose frontmatter `description:` the host
    cannot read (absent, empty, null, not a YAML string, or in a block the host never decodes) or that
    exceeds 1024 chars (the Agent Skills limit; the host keeps only the first 1024); an
    `actions/*/manifest.yaml` with no top-level `description:`.
  * **FAIL** — a git-tracked symlink whose target is absolute or escapes the repo root; neither can
    resolve to brain content in a fresh checkout.
  * **FAIL** — a Python script outside `skills/`, `actions/`, or `tests/` (except root
    `conftest.py`). Import smoke intentionally discovers only `skills/**`, so grounding scripts must
    live under `skills/<topic>/scripts/` rather than beside notes or at the brain root.
  * **WARN** — a git-tracked relative symlink whose target is not tracked (the supported committed
    `.claude/skills -> ../.agents/skills` alias shape): it dangles in a fresh checkout, which the host
    skips when building a run view.
  * **WARN** — a description (Markdown or action manifest) whose first complete sentence does not fit
    in the 150-character tree gloss: the tree line shows only the first 150 chars, so lead with a short
    when-to-use sentence; rich detail after it is kept in the full value (an action manifest's is
    injected full-length into the per-run action catalog); "what this file contains"-style phrasing
    (`This file…`, `Contains…`, `Dit bestand…`). Deterministic, best-effort; never fails a run.
  * **FAIL** — an on-demand doc (no `include_in`, not mirrored corpus, not under journal/tests/docs)
    over `ON_DEMAND_DOC_LIMIT` bytes as `cat` prints it (the bash spill cliff); **WARN** past
    `ON_DEMAND_DOC_WARN`, an `include_in` hard-load over the host's per-file/total cap
    (`HARD_LOAD_CAPS`), or `AGENTS.md` over `AGENTS_MD_CAP`. Fix: a lean core + grouped detail files.
  * **FAIL** — frontmatter yaml.v3 cannot decode when the host's one-line fallback then drops the
    `description` (a value on following lines, or none); **WARN** for any other invalid block, e.g. an
    unquoted `: ` in a one-line value.
  * **FAIL** — frontmatter the host will never read: an `include_in` tag outside its role's scan scope
    (mirror file outside root *.md + `MIRROR_SCAN_DIRS`, `triage` outside the project brain, pruned or
    run-hidden path), a mirror `surfaces:`, an unknown role, a key the host misses (not at byte 0,
    nested, misspelt, a multi-line `include_in`, a duplicate or past-8 KiB `description`, `exclude_in`).
    **WARN** — a no-op tag on an already-pasted AGENTS.md.
    The repo kind (brain / tenant overlay / mirror) comes from `detect_repo_kind`.

It mirrors `replypen/internal/treeview` so lint and tree **agree**: `md_description_full` is a port
of `treeview.DocDescription` (real YAML over the frontmatter block closed within 8 KiB, any string
form; malformed YAML or no closed block falls back to the legacy 2 KiB line scan), real YAML for
action manifests, and the same `tidyDesc` whitespace collapse. Both sides replay the shared corpus in
`contracts/frontmatter/` (replypen vendors it as `internal/treeview/testdata/frontmatter/`).

This module stays stdlib + PyYAML only so the standalone developer entrypoint and the production
publish gate can import it without pytest. `lib.brain_lint_pytest` owns the optional pytest wiring.
"""

from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .action_lint import lint_actions
from .brain_doc import (
    FRONTMATTER_CAP,
    INCLUDE_IN_ROLES,
    MIRROR_SCAN_DIRS,
    ON_DEMAND_DOC_LIMIT,
    REPO_KINDS,
    TAG_PRUNED_DIRS,
    detect_repo_kind,
    hard_loaded,
    on_demand_doc_bytes,
    projection_renderer,
    unread_roles,
)
from .brain_doc import frontmatter_lines as _frontmatter_lines
from .brain_doc import include_in as _include_in
from .brain_doc import strip_frontmatter as _strip_frontmatter


# Mirror treeview's descMaxLen (tree gloss) / descFullMaxLen (agentskills.io cap) / descHeadBytes
# (legacy line-scan fallback) so the lint's verdict matches what the host reads.
DESC_MAX_LEN = 150
DESC_FULL_MAX_LEN = 1024
DESC_HEAD_BYTES = 2048
_YAML_NULL = "tag:yaml.org,2002:null"
_YAML_STR = "tag:yaml.org,2002:str"
_ACTION_SURFACE_CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "action_surfaces.json").read_text("utf-8")
)
ACTION_SURFACES = tuple(_ACTION_SURFACE_CONTRACT["surfaces"])
ACTION_SURFACE_ALIASES = dict(_ACTION_SURFACE_CONTRACT["deprecated_aliases"])
# Dotted keys address a nested block (`preflight.runtime`). Mirrored byte-for-byte by the host's
# internal/action/testdata copy (guard test there), so both repos change together.
ACTION_MANIFEST_ENUMS = {
    key: tuple(values) for key, values in json.loads(
        (Path(__file__).parent / "contracts" / "action_manifest_enums.json").read_text("utf-8")
    ).items()
}

# Leading-phrase patterns that describe *contents* ("what this holds") instead of *when to open this*.
# WARN-only and deliberately small — a few high-precision openers in English + Dutch, matched at the
# very start after whitespace/quote trim. Not an exhaustive style grader; just the common tells.
_CONTAINS_STYLE = re.compile(
    r"^\s*(this\s+(file|doc|document|page|skill|runbook)|contains\b|describes\b|documentation\s+for"
    r"|dit\s+(bestand|document)|deze\s+(pagina|file))",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s|$)")

# `actions-drafts/<id>/` is written onto brain main by the host's Action Studio (script.py, policy.py).
_SCRIPT_ALLOWED_ROOTS = frozenset({"skills", "actions", "actions-drafts", "tests"})
_SCRIPT_IGNORED_DIRS = frozenset({
    ".agents",
    ".claude",
    ".replypen",
    ".rootcause",
    ".git",
    ".venv",
    "_internal",
    "node_modules",
})


_SYMLINK_WALK_IGNORED_DIRS = frozenset({".git", ".venv", "node_modules"})


@dataclass(frozen=True)
class Finding:
    """One lint result. `level` is "FAIL" (blocks the tier) or "WARN" (reported, never fails)."""

    path: str  # brain-relative path, e.g. "skills/backups/SKILL.md"
    level: str
    message: str
    rule: str = "other"


def _tidy(val: str) -> str:
    """Collapse internal whitespace like bootstrap.go's tidyDesc (strings.Fields + join) — no truncation.

    Length is then measured on the collapsed form, so a description that only *looks* long because of
    wrapping/indentation isn't penalised, matching what the tree actually renders.
    """
    return " ".join(val.split())


def _cap(val: str, limit: int) -> str:
    """Truncate to `limit` code points (Go runes) with a trailing ellipsis, like tidyDesc."""
    return val if len(val) <= limit else val[:limit - 1] + "…"


def _legacy_md_description(head: bytes) -> str | None:
    """The pre-YAML host reader (now only treeview's fallback): a 2 KiB head line scan.

    One-line `description:` values only; a matched quote pair is stripped, embedded quotes stay. None
    for no frontmatter, no key, an empty value, or a block scalar / value on the next line.
    """
    lines = head[:DESC_HEAD_BYTES].decode("utf-8", "replace").split("\n")
    if len(lines) < 2 or lines[0].rstrip("\r") != "---":
        return None
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if line == "---":
            return None  # end of frontmatter, no description
        rest = _strip_prefix(line, "description:")
        if rest is None:
            continue
        val = rest.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        if val == "" or val.startswith("|") or val.startswith(">"):
            return None
        return _tidy(val) or None
    return None


_YAML_TAG_PREFIX = "tag:yaml.org,2002:"
# yaml.v3 resolve() for an untagged plain scalar (gopkg.in/yaml.v3 resolve.go): YAML 1.2 core plus v3's
# quirks — `on`/`yes`/`off` stay strings, `1e3` is a float — where PyYAML's SafeLoader is YAML 1.1.
_V3_PLAIN = {
    **dict.fromkeys(("true", "True", "TRUE", "false", "False", "FALSE"), "bool"),
    **dict.fromkeys(("", "~", "null", "Null", "NULL"), "null"),
    **dict.fromkeys((f"{sign}.{v}" for sign in ("", "+", "-") for v in ("inf", "Inf", "INF")), "float"),
    **dict.fromkeys((".nan", ".NaN", ".NAN"), "float"),
    "<<": "merge",
}
_V3_INT = re.compile(r"[-+]?(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)")
_V3_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?")
_V3_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})"
    r"(?:[Tt][0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}(?:\.[0-9]+)?(?:Z|[-+][0-9]{2}:[0-9]{2})"
    r"| [0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}(?:\.[0-9]+)?)?")


def _v3_plain_tag(value: str) -> str:
    if value in _V3_PLAIN:
        return _YAML_TAG_PREFIX + _V3_PLAIN[value]
    if value[:1] == "." and re.fullmatch(r"\.[0-9]+(?:[eE][-+]?[0-9]+)?", value):
        return _YAML_TAG_PREFIX + "float"
    if value[:1] and value[0] in "+-0123456789":
        if m := _V3_TIMESTAMP.fullmatch(value):
            try:
                datetime.date(int(m[1]), int(m[2]), int(m[3]))
                return _YAML_TAG_PREFIX + "timestamp"
            except ValueError:
                pass
        plain = value.replace("_", "")
        if _V3_INT.fullmatch(plain):
            return _YAML_TAG_PREFIX + "int"
        if _V3_FLOAT.fullmatch(plain):
            return _YAML_TAG_PREFIX + "float"
    return _YAML_STR


class _HostLoader(yaml.SafeLoader):
    """SafeLoader whose untagged plain scalars resolve like yaml.v3, so the lint's string/non-string/null
    verdict on a description is the host's `ShortTag()`. Quoted and explicitly tagged scalars are
    unaffected (PyYAML only calls resolve for them as before)."""

    def resolve(self, kind, value, implicit):  # type: ignore[override]
        if kind is yaml.ScalarNode and implicit[0]:
            return _v3_plain_tag(value)
        return super().resolve(kind, value, implicit)


def _host_yaml_text(block: list[str]) -> str:
    """A frontmatter block as YAML text PyYAML decodes like yaml.v3: a tab after leading indentation (a
    `key:\tvalue` separator or inside a value) is legal YAML that only PyYAML rejects, so it becomes a
    space; whitespace collapses in every rendered description anyway. A tab IN the indentation stays —
    both parsers reject it."""
    out = []
    for line in block:
        line = line.rstrip("\r")
        body = line.lstrip(" \t")
        out.append(line[:len(line) - len(body)] + body.replace("\t", " "))
    return "\n".join(out)


def _description_nodes(block: list[str]) -> list[yaml.Node] | None:
    """Every top-level `description` value node in a frontmatter block, in order; None when the block
    is not a decodable YAML mapping (the host then falls back to the line scan)."""
    try:
        root = yaml.compose(_host_yaml_text(block), Loader=_HostLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    return [v for k, v in root.value if isinstance(k, yaml.ScalarNode) and k.value == "description"]


def _node_text(node: yaml.Node) -> str | None:
    """A description node as the host renders it: any non-null scalar's collapsed source text."""
    if isinstance(node, yaml.ScalarNode) and node.tag != _YAML_NULL:
        return _tidy(node.value) or None
    return None


def _frontmatter_description(raw: bytes) -> tuple[str | None, str]:
    """(description, how) exactly as `treeview.DocDescription` reads it, whitespace-collapsed and NOT
    yet capped at DESC_FULL_MAX_LEN. `how`: "yaml" (a string), "yaml-nonstr" (another non-null scalar,
    read as its source text), "line-scan" (legacy fallback), "none".

    The frontmatter block (`---` at byte 0, closing fence within FRONTMATTER_CAP) is decoded as YAML
    with yaml.v3 scalar typing; the FIRST top-level `description` key wins. Malformed YAML, a
    non-mapping root, or no closed block falls back to the legacy line scan, so nothing the old reader
    rendered regresses.
    """
    block = _frontmatter_lines(raw[:FRONTMATTER_CAP].decode("utf-8", "replace"))
    nodes = _description_nodes(block) if block is not None else None
    if nodes is not None:
        if nodes and (val := _node_text(nodes[0])):
            return val, "yaml" if nodes[0].tag == _YAML_STR else "yaml-nonstr"
        return None, "none"
    legacy = _legacy_md_description(raw)
    return (legacy, "line-scan") if legacy else (None, "none")


def md_description_yaml_tag(path: str | Path) -> str:
    """Short YAML tag (`!!str`, `!!int`, `!!seq`, …) of the description node the host decodes, the
    yaml.v3 `ShortTag()`; "" when the host reads no key from YAML (none, or line-scan fallback)."""
    block = _frontmatter_lines(_read_head(Path(path)).decode("utf-8", "replace"))
    nodes = _description_nodes(block) if block is not None else None
    return nodes[0].tag.replace(_YAML_TAG_PREFIX, "!!") if nodes else ""


def _read_head(path: Path, limit: int = FRONTMATTER_CAP) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def md_description_full(path: str | Path) -> str | None:
    """Port of `treeview.DocDescription`: the full frontmatter description (whitespace collapsed,
    ≤ DESC_FULL_MAX_LEN runes), or None when the host reads none. `md_tree_gloss` is its tree line."""
    val, _ = _frontmatter_description(_read_head(Path(path)))
    return _cap(val, DESC_FULL_MAX_LEN) if val else None


def md_tree_gloss(full: str | None) -> str:
    """The tree-line gloss `tidyDesc` renders from a full description ("" when none)."""
    return _cap(full, DESC_MAX_LEN) if full else ""


def _strip_prefix(line: str, prefix: str) -> str | None:
    return line[len(prefix):] if line.startswith(prefix) else None


def _manifest_description(path: Path) -> str | None:
    """The top-level `description:` of an action manifest, or None when missing/empty/unparseable.

    Real YAML (mirrors bootstrap.go's `manifestGloss`) — action descriptions are routinely `>-` block
    scalars, which a line scan would drop; here they are legitimately present, so parse them properly.
    """
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None
    return _tidy(desc)


def _check_manifest_surfaces(path: Path, rel: str) -> list[Finding]:
    """Reject malformed or unknown manifest surface allowlists."""
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError):
        return []  # Existing manifest parsing/description checks report malformed YAML.
    if not isinstance(data, dict) or "surfaces" not in data:
        return []
    surfaces = data["surfaces"]
    allowed = ", ".join(ACTION_SURFACES)
    if not isinstance(surfaces, list):
        return [Finding(rel, "FAIL", f"`surfaces` must be a list (allowed: {allowed})",
                        "action-surfaces")]
    if not surfaces:
        return [Finding(rel, "FAIL", "`surfaces` must contain at least one value; omit it to allow all",
                        "action-surfaces")]
    findings: list[Finding] = []
    seen: set[str] = set()
    for value in surfaces:
        if not isinstance(value, str):
            findings.append(Finding(
                rel, "FAIL", f"unknown action surface {value!r} (allowed: {allowed})",
                "action-surfaces",
            ))
            continue
        normalized = ACTION_SURFACE_ALIASES.get(value, value)
        if value in ACTION_SURFACE_ALIASES:
            findings.append(Finding(
                rel, "WARN", f"action surface {value!r} is deprecated; use {normalized!r}",
                "action-surfaces",
            ))
        elif value not in ACTION_SURFACES:
            findings.append(Finding(
                rel, "FAIL", f"unknown action surface {value!r} (allowed: {allowed})",
                "action-surfaces",
            ))
            continue
        if normalized in seen:
            findings.append(Finding(rel, "FAIL", f"action surface {normalized!r} is repeated after alias normalization",
                                    "action-surfaces"))
        seen.add(normalized)
    return findings


def _check_manifest_enums(path: Path, rel: str) -> list[Finding]:
    """Reject manifest enum values the host refuses or mis-reads. Stricter than the host (exact case):
    a typo like `risk: Hgih` would otherwise silently drop the high-risk floor."""
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError):
        return []  # Existing manifest parsing/description checks report malformed YAML.
    if not isinstance(data, dict):
        return []
    findings: list[Finding] = []
    for key, allowed in ACTION_MANIFEST_ENUMS.items():
        *parents, leaf = key.split(".")
        node = data
        for part in parents:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or leaf not in node:
            continue  # absent = host default
        value = node[leaf]
        if isinstance(value, str) and value in allowed:
            continue
        findings.append(Finding(
            rel, "FAIL", f"`{key}` must be one of: {', '.join(allowed)} (got {value!r})",
            "action-manifest-enums",
        ))
    return findings


def _doc_surfaces_problem(path: Path) -> str | None:
    """Why the host would NOT enforce this doc's top-level `surfaces:` as written, or None when it would
    (or the doc declares none). Every problem means fail-open: the doc stays visible on every surface."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    close = next((i for i, line in enumerate(lines[1:], start=1) if line.rstrip("\r") == "---"), None)
    if close is None:
        return None
    block = lines[1:close]
    if not any(line.startswith("surfaces:") for line in block):
        return None
    if len("\n".join(lines[:close + 1]).encode("utf-8")) > FRONTMATTER_CAP:
        return f"frontmatter exceeds {FRONTMATTER_CAP} bytes; the host ignores its `surfaces`"
    try:
        data = yaml.safe_load("\n".join(block))
    except yaml.YAMLError:
        return "frontmatter is not valid YAML; `surfaces` may not parse as intended"
    values = data.get("surfaces") if isinstance(data, dict) else None
    values = [values] if isinstance(values, str) else values
    if not isinstance(values, list) or not values or any(v not in ACTION_SURFACES for v in values):
        return f"`surfaces` must list known surfaces ({', '.join(ACTION_SURFACES)}); got {values!r}"
    return None


def _check_doc_surfaces(root: Path) -> list[Finding]:
    """WARN when the host won't enforce a markdown `surfaces:` list: it then fails open (visible on every
    surface), so a typo silently undoes the scoping instead of breaking runs."""
    findings: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(name for name in dirnames if name not in _SYMLINK_WALK_IGNORED_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            path = Path(dirpath) / filename
            if problem := _doc_surfaces_problem(path):
                findings.append(Finding(_rel(root, path), "WARN",
                                        f"{problem} — the doc stays visible on every surface", "doc-surfaces"))
    return findings


# Host size budgets for markdown, in bytes of the frontmatter-stripped body. Mirrors ReplyPen's
# internal/grounding/hardload.go (grounding 8K/24K; agent + principal share 16K/48K) and
# internal/triage/brainknowledge.go (triage 8K/24K). Past a cap the host cuts the body with
# "... (truncated — read the rest with bash)" — and models rarely do.
HARD_LOAD_CAPS = {  # role -> (include_in tags, per-file cap, total cap)
    "triage": (("triage",), 8 << 10, 24 << 10),
    "grounding": (("grounding",), 8 << 10, 24 << 10),
    "agent": (("agent", "principal"), 16 << 10, 48 << 10),
}
# A tagged body past this share of its per-file cap WARNs, so the next edit is not the one that cuts it.
HARD_LOAD_NEAR_CAP = 0.9
# The near-cliff WARN leaves room for one more paragraph below brain_doc.ON_DEMAND_DOC_LIMIT; only for
# paths a caller names (changed files), since a whole-tree run repeats it for docs nobody touches.
ON_DEMAND_DOC_WARN = 5000
# AGENTS.md is pasted whole, uncapped, every run: an advisory names a genuinely oversized one.
AGENTS_MD_CAP = 16 << 10
_DOC_SIZE_IGNORED_DIRS = frozenset({".agents", ".claude", ".replypen", ".rootcause", ".git", ".venv", "_internal",
                                    "node_modules", "__pycache__"})


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def _doc_size_candidates(root: Path) -> list[str]:
    """Run-visible markdown, brain-relative and sorted like the host scan: git-tracked when git can
    answer (ignored kit/scratch trees never reach a run), else a disk walk."""
    index = _git_index(root)
    if index is not None:
        rels = [r for r in index[0] if r.endswith(".md") and r not in index[1]]
    else:
        rels = [_rel(root, p) for p in root.rglob("*.md")]
    return sorted(r for r in rels
                  if not _DOC_SIZE_IGNORED_DIRS.intersection(r.split("/")[:-1])
                  and (root / r).is_file() and not (root / r).is_symlink())


def _projection_renderer(root: Path, kind: str) -> Callable[[str, str], str]:
    """`brain_doc.projection_renderer` over a project brain's projection.yaml (overlays copy verbatim)."""
    try:
        spec = yaml.safe_load((root / "projection.yaml").read_text("utf-8")) if kind == "brain" else None
    except (OSError, yaml.YAMLError, ValueError):  # ValueError: an unconstructible timestamp default
        spec = None
    return projection_renderer(spec)


def _check_doc_sizes(root: Path, kind: str = "brain", near_cliff: bool = True) -> list[Finding]:
    """FAIL when a markdown doc won't reach the model whole: a hard-loaded body over its per-file or
    total cap (the host truncates it on every run), an on-demand doc over ON_DEMAND_DOC_LIMIT (the bash
    spill cliff, `brain_doc.on_demand_doc_bytes`, the measure the hygiene gate's `doc-spill` uses too).
    WARN a hard-loaded body past HARD_LOAD_NEAR_CAP of its cap, AGENTS.md over AGENTS_MD_CAP, an
    on-demand doc over ON_DEMAND_DOC_WARN (`near_cliff`)."""
    findings: list[Finding] = []
    totals = dict.fromkeys(HARD_LOAD_CAPS, 0)
    rendered = _projection_renderer(root, kind)
    for rel in _doc_size_candidates(root):
        try:
            text = (root / rel).read_text("utf-8", "replace")
        except OSError:
            continue
        tags = _include_in(text) - unread_roles(rel, kind)
        size = len(_strip_frontmatter(text).encode("utf-8"))
        over: list[str] = []
        near: list[str] = []
        loaded = False
        for role, (role_tags, per_file, total_cap) in HARD_LOAD_CAPS.items():
            # The brain's root AGENTS.md is pasted verbatim on its own, never via a grounding/agent tag.
            if not tags.intersection(role_tags) or (kind == "brain" and rel == "AGENTS.md" and role != "triage"):
                continue
            loaded = True
            kept = min(size, per_file)
            if size > per_file:
                over.append(f"the {_kb(per_file)} `{role}` cap")
            elif size > per_file * HARD_LOAD_NEAR_CAP:
                near.append(f"the {_kb(per_file)} `{role}` cap")
            if totals[role] >= total_cap or kept > total_cap - totals[role]:
                over.append(f"the {_kb(total_cap)} `{role}` total (earlier tagged docs hold "
                            f"{_kb(totals[role])})")
            totals[role] += min(kept, max(total_cap - totals[role], 0))
        if over:
            findings.append(Finding(rel, "FAIL",
                f"hard-loaded body is {_kb(size)}, over {' and '.join(over)}: the host truncates it "
                "on every run and models rarely read the rest with bash", "doc-size"))
        elif near:
            findings.append(Finding(rel, "WARN",
                f"hard-loaded body is {_kb(size)}, near {' and '.join(near)} (FAIL past it): trim or "
                "move detail into on-demand files before adding more", "doc-size"))
        elif rel == "AGENTS.md" and kind != "mirror":
            if size > AGENTS_MD_CAP:
                findings.append(Finding(rel, "WARN",
                    f"AGENTS.md is {_kb(size)}: pasted whole into every run, every extra byte is tax on "
                    "every thread and buries the routing", "doc-size"))
        elif not loaded:
            # `cat` bytes: decode without read_text's universal newlines, so CRLF counts twice.
            raw = (root / rel).read_bytes().decode("utf-8", "replace")
            read = on_demand_doc_bytes(rel, raw, hard_loaded(rel, raw, kind), rendered) or 0
            if read > ON_DEMAND_DOC_LIMIT:
                findings.append(Finding(rel, "FAIL",
                    f"on-demand doc is {read} bytes, over the {ON_DEMAND_DOC_LIMIT}-byte bash spill "
                    "limit: a `cat` past it shows only a 2000+1000-char preview and runs rarely read "
                    "the rest", "doc-size"))
            elif near_cliff and read > ON_DEMAND_DOC_WARN:
                findings.append(Finding(rel, "WARN",
                    f"on-demand doc is {read} bytes, near the {ON_DEMAND_DOC_LIMIT}-byte bash spill "
                    "limit (FAIL past it)", "doc-size"))
    return findings


# ---- Frontmatter the host never reads ------------------------------------------------------------
# Every constant below names the replypen source it mirrors; change them together with the host.

# Frontmatter keys the host reads from brain markdown, all by a top-level line scan (treeview.frontmatterList,
# bootstrap.go mdDescription). Near-miss spellings the host silently ignores map to the real key.
_HOST_KEYS = ("include_in", "surfaces", "description")
_KEY_SPELLINGS = {"includein": "include_in", "include_in": "include_in", "surface": "surfaces",
                  "surfaces": "surfaces", "description": "description"}

# Paths the host drops from every run view whatever the control files say (internal/treeview/visible.go
# hiddenRunPath): `.git`, `.gitignore`/`.replypenignore` anywhere, the root `.rcignore`, dependency/cache
# dirs, and secret-bearing dotenv files.
_ALWAYS_HIDDEN_PARTS = frozenset({".git", ".gitignore", ".replypenignore", "__pycache__", "node_modules",
                                  ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
_DOTENV_TEMPLATES = (".sample", ".example", ".template", ".dist", ".defaults")


def _glob_regex(glob: str) -> str:
    """gitignore glob -> regex over a slash path: `**/`, `/**`, `**`, `*`, `?`, `[...]`."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out.append("(?:/.*)?"); i += 3
        elif glob.startswith("**", i):
            out.append(".*"); i += 2
        elif glob[i] == "*":
            out.append("[^/]*"); i += 1
        elif glob[i] == "?":
            out.append("[^/]"); i += 1
        elif glob[i] == "[" and (end := glob.find("]", i + 2)) != -1:
            body = glob[i + 1:end]
            out.append("[" + ("^" + body[1:] if body[:1] in "!^" else body).replace("\\", "\\\\") + "]")
            i = end + 1
        elif glob[i] == "\\" and i + 1 < len(glob):
            out.append(re.escape(glob[i + 1])); i += 2
        else:
            out.append(re.escape(glob[i])); i += 1
    return "".join(out)


def _ignore_rules(path: Path) -> list[tuple[re.Pattern[str], bool, bool, bool]]:
    """(regex, negate, dir_only, anchored) per gitignore line, in file order."""
    try:
        lines = path.read_text("utf-8", "replace").splitlines()
    except OSError:
        return []
    rules = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        if not line.endswith("\\ "):
            line = line.rstrip(" ")
        negate = line.startswith("!")
        line = line[1:] if negate else line
        if line[:2] in ("\\#", "\\!"):
            line = line[1:]
        if line.endswith("/**"):  # host: descendants only, never the dir itself (visible.go readRunIgnorePatterns)
            line += "/*"
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        if not line:
            continue
        rules.append((re.compile(_glob_regex(line.lstrip("/"))), negate, dir_only, anchored))
    return rules


def run_hidden(root: str | Path) -> Callable[[str], bool]:
    """Predicate: is this repo-relative path absent from every run view? Evaluates the root
    `.replypenignore` / `.rcignore` (gitignore syntax, last match wins per file, union across files, an
    ignored dir hides everything below it) plus the host's always-hidden paths — in pure Python, because
    the publish/canary lint sees a worktree whose `.git` points at an unmounted gitdir."""
    root = Path(root)
    files = [r for c in (".replypenignore", ".rcignore") if (r := _ignore_rules(root / c))]

    def ignored(path: str, is_dir: bool) -> bool:
        name = path.rsplit("/", 1)[-1]
        for rules in files:
            verdict = None
            for regex, negate, dir_only, anchored in rules:
                if dir_only and not is_dir:
                    continue
                if regex.fullmatch(path if anchored else name):
                    verdict = not negate
            if verdict:
                return True
        return False

    def hidden(rel: str) -> bool:
        parts = rel.strip("/").split("/")
        if rel == ".rcignore" or _ALWAYS_HIDDEN_PARTS.intersection(parts):
            return True
        if any((p == ".env" or p.startswith(".env.")) and not p.endswith(_DOTENV_TEMPLATES) for p in parts):
            return True
        return any(ignored("/".join(parts[:k]), k < len(parts)) for k in range(1, len(parts) + 1))

    return hidden


def _intended_block(text: str) -> tuple[list[str], bool] | None:
    """The frontmatter block an author evidently meant, ignoring the host's byte-0 + 8KB rules:
    (lines, at_byte_0). None when the file opens with no `---` fence at all."""
    lines = text.split("\n")
    start = 0
    while start < len(lines) and not lines[start].lstrip("\ufeff").strip():
        start += 1
    if start >= len(lines) or lines[start].lstrip("\ufeff").strip() != "---":
        return None
    close = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return None
    return lines[start + 1:close], start == 0 and lines[0].rstrip("\r") == "---"


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _nested_keys(node: yaml.Node | None) -> set[str]:
    """Mapping keys below the top level of a composed YAML document (scalar contents are not keys)."""
    out: set[str] = set()
    stack = [(node, 0)] if node is not None else []
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, yaml.MappingNode):
            for key, value in cur.value:
                if depth and isinstance(key, yaml.ScalarNode):
                    out.add(str(key.value))
                stack.append((value, depth + 1))
        elif isinstance(cur, yaml.SequenceNode):
            stack.extend((item, depth + 1) for item in cur.value)
    return out


def _frontmatter_doc_findings(root: Path, rel: str, kind: str, hidden: bool) -> list[Finding]:
    """FAIL/WARN for one .md whose frontmatter declares something the host will never read."""
    path = root / rel
    try:
        text = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    intended = _intended_block(text)
    if intended is None:
        return []
    block, at_byte_0 = intended
    # YAML gives the author's intent (values, nesting); when it can't decode the block, the host's own
    # line scan still reads top-level keys, so the line-scan checks below keep running on those.
    # _HostLoader keeps an impossible date (`2026-02-30`) a string as yaml.v3 does; a value PyYAML still
    # cannot construct (`T25:00:00`) raises ValueError, a parse failure of this file, not of the lint run.
    yaml_error: Exception | None = None
    try:
        node = yaml.compose(_host_yaml_text(block), Loader=_HostLoader)
        data = yaml.load(_host_yaml_text(block), Loader=_HostLoader)  # noqa: S506 - SafeLoader subclass
    except (yaml.YAMLError, ValueError) as exc:
        node = data = None
        yaml_error = exc
    parsed = isinstance(data, dict)
    data = data if parsed else {}
    top = ({str(k) for k in data} if parsed else
           {m.group(1) for line in block if (m := re.match(r"^([^\s#:][^:]*?)\s*:(?:\s|$)", line))})
    nested = _nested_keys(node) & {*_HOST_KEYS, "exclude_in"} if parsed else set()
    spelled = {k: _KEY_SPELLINGS[n] for k in top
               if (n := k.strip().lower().replace("-", "_")) in _KEY_SPELLINGS and k != _KEY_SPELLINGS[n]}
    declared = ((top | nested) & {*_HOST_KEYS, "exclude_in"}) | set(spelled.values())
    if not declared:
        return []
    # skills/*/SKILL.md and runbooks already FAIL `description-missing` when the tree renders no gloss.
    desc_owned = bool(re.fullmatch(r"skills/[^/]+/SKILL\.md|skills/cases/[^/]+\.md", rel))

    def unread(message: str, level: str = "FAIL") -> Finding:
        return Finding(rel, level, message, "frontmatter-unread")

    def scope(message: str) -> Finding:
        return Finding(rel, "FAIL", message, "frontmatter-scope")

    fm = _frontmatter_lines(text)

    def desc_unread() -> list[Finding]:
        """The tree reads another `description` than the author wrote: a second key it ignores, or a
        block past its 8 KiB envelope. An owned file (SKILL.md/runbook) whose host value is unread
        already FAILs `description-missing`; a mirror's descriptions are the customer's, so WARN there."""
        nodes = _description_nodes(block) or []
        shown = _frontmatter_description(text.encode("utf-8"))[0] or ""
        if not nodes or (desc_owned and not shown):
            return []
        if fm is not None:
            if len(nodes) < 2:
                return []
            why = "is declared more than once (the host reads only the first)"
        elif shown == (_node_text(nodes[0]) or ""):
            return []
        else:
            why = f"sits in frontmatter that closes past the first {FRONTMATTER_CAP} bytes the host reads"
        got = repr(_cap(shown, 60)) if shown else "no gloss"
        return [unread(f"`description:` {why}; the tree line renders {got} — keep one `description:` "
                       "in a short frontmatter block", "WARN" if kind == "mirror" else "FAIL")]

    def invalid_yaml() -> list[Finding]:
        """Frontmatter yaml.v3 cannot decode (an unquoted `: ` in a value, a tab in the indentation, an
        unbalanced quote). include_in/surfaces are line-scanned anyway; the description falls back to a
        one-line scan, so FAIL only when that drops it (a value on following lines, or none at all)."""
        if yaml_error is None:
            return []
        mark = getattr(yaml_error, "problem_mark", None) or getattr(yaml_error, "context_mark", None)
        problem = getattr(yaml_error, "problem", None) or str(yaml_error).split("\n", 1)[0]
        what = f"frontmatter is not valid YAML{f' (line {mark.line + 2}' if mark else ' ('}: {problem})"
        fix = "quote the value (`description: \"A: b\"`) or write ` — ` for `: `; indent with spaces only"
        shown = _frontmatter_description(text.encode("utf-8"))[0]
        start = next((i for i, line in enumerate(block) if line.startswith("description:")), None)
        rest = block[start + 1:] if start is not None else []
        tail = next((i for i, line in enumerate(rest) if line.strip() and not line[:1].isspace()), len(rest))
        lost = start is not None and (not shown or any(line.strip() for line in rest[:tail]))
        if lost and not (desc_owned and not shown):
            got = repr(_cap(shown, 60)) if shown else "no gloss"
            return [unread(f"{what}, so the host reads `description:` by its one-line fallback and the tree "
                           f"line renders {got} — {fix}", "WARN" if kind == "mirror" else "FAIL")]
        if lost:
            return []  # an owned file with no gloss already FAILs description-missing
        return [unread(f"{what}; the host still reads it line by line, strict YAML readers do not — {fix}",
                       "WARN")]

    if fm is None:
        lost = sorted(k for k in declared if k != "description" or not desc_owned)
        if not at_byte_0 and lost:
            return [unread(f"frontmatter does not start at byte 0 (BOM, blank line, or text before `---`), "
                           f"so the host reads none of it — its {', '.join(f'`{k}`' for k in lost)} "
                           "are ignored; make `---` the very first line")]
        if not at_byte_0:
            return []
        late = [unread(f"frontmatter closes past the first {FRONTMATTER_CAP} bytes the host reads, so "
                       "its `include_in` is ignored; shorten the frontmatter")] if "include_in" in declared else []
        return late + desc_unread()

    tags = _include_in(text)
    if hidden:
        return [scope("`include_in` on a run-hidden path (.replypenignore/.rcignore): the run never sees "
                      "this file, so it is never hard-loaded; un-hide it or drop the tag")] if tags else []

    out: list[Finding] = []
    if "exclude_in" in top | nested:
        out.append(unread("`exclude_in` is never read by the host (no visibility effect); hide a file with "
                          "`.replypenignore`, scope it per surface with `surfaces:`, or drop the key"))
    for key, real in sorted(spelled.items()):
        if real not in top:
            out.append(unread(f"`{key}:` is not read; the host only reads the exact key `{real}:`"))
    for key in sorted(nested - top - {"exclude_in"}):
        out.append(unread(f"`{key}:` is indented (nested under another key); the host only reads "
                          "top-level keys — move it to column 0"))

    if "include_in" in top and parsed:
        meant = {v for v in _as_list(data["include_in"]) if v}
        if meant != tags:
            out.append(unread(f"the host's line scan reads `include_in` as {sorted(tags)}, YAML as "
                              f"{sorted(meant)}; write it on one line (`include_in: [agent, grounding]`) "
                              "or as a `- role` block list"))
    unknown = sorted(tags - set(INCLUDE_IN_ROLES))
    for value in unknown:
        near = difflib.get_close_matches(value.lower(), INCLUDE_IN_ROLES, n=1)
        hint = f" — did you mean `{near[0]}`?" if near else ""
        out.append(Finding(rel, "FAIL", f"unknown `include_in` role {value!r}{hint}; the host only reads "
                           f"{', '.join(INCLUDE_IN_ROLES)}, so this tag loads nothing", "include-in-value"))

    roles = tags & set(INCLUDE_IN_ROLES)
    dead = roles & unread_roles(rel, kind)
    parts = rel.split("/")
    if dead and TAG_PRUNED_DIRS.intersection(parts[:-1]):
        pruned = next(p for p in parts[:-1] if p in TAG_PRUNED_DIRS)
        out.append(scope(f"`include_in` under `{pruned}/`: the host's tag scan skips that dir, so this "
                         "file is never hard-loaded; move it or drop the tag"))
    elif dead and kind == "mirror" and len(parts) > 1 and parts[0] not in MIRROR_SCAN_DIRS:
        dirs = ", ".join(f"{d}/" for d in MIRROR_SCAN_DIRS)
        out.append(scope(f"`include_in` is only read in a mirror at the repo root *.md or under {dirs} — "
                         "this file is never hard-loaded; move it there or drop the tag"))
    elif dead:
        where = "a tenant overlay" if kind == "tenant" else "a mirror"
        out.append(scope(f"`include_in: [triage]` is only read from the project brain; triage never reads "
                         f"{where} — move the rule into the project brain's triage.md or drop `triage`"))
    if rel == "AGENTS.md":
        pasted = {"brain": {"grounding", "agent", "principal"}, "tenant": {"agent", "principal"}}.get(kind, set())
        if extra := sorted(roles & pasted):
            where = ("/brain/AGENTS.md is already pasted whole into grounding and the main agent on every run"
                     if kind == "brain" else
                     "/tenant/AGENTS.md is already pasted whole into the main agent; the tag pastes it twice")
            out.append(Finding(rel, "WARN", f"{where} — drop {', '.join(f'`{r}`' for r in extra)}",
                               "frontmatter-redundant"))

    if kind == "mirror" and "surfaces" in top:
        out.append(scope("`surfaces:` is only honored in a project or tenant brain; a mirror doc is never "
                         "stubbed per surface — move the doc into the brain or drop the key"))

    out += desc_unread() + invalid_yaml()
    return out


def _check_frontmatter(root: Path, kind: str) -> list[Finding]:
    """Frontmatter the host will never read: tags outside its role's scan scope, unknown roles,
    keys in a form the host's line scan misses, and no-op tags on already-pasted AGENTS.md."""
    hidden = run_hidden(root)
    index = _git_index(root)
    if index is not None:
        rels = sorted(r for r in index[0] if r.endswith(".md") and r not in index[1])
    else:
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in TAG_PRUNED_DIRS)
            rels += [_rel(root, Path(dirpath) / f) for f in sorted(filenames) if f.endswith(".md")]
    findings: list[Finding] = []
    for rel in rels:
        if (root / rel).is_file() and not (root / rel).is_symlink():
            findings += _frontmatter_doc_findings(root, rel, kind, hidden(rel))
    return findings


def _check(rel: str, desc: str | None, kind: str, how: str = "yaml") -> list[Finding]:
    """Turn one file's extracted (uncapped) description into findings: missing/non-string/over 1024
    FAIL, a first sentence past the tree gloss WARN, contents-style WARN."""
    if desc is None:
        return [Finding(rel, "FAIL",
                        f"missing `description:` in {kind} (absent, empty, null, or in frontmatter the "
                        "host cannot read)", "description-missing")]
    out: list[Finding] = []
    if how == "yaml-nonstr":
        out.append(Finding(rel, "FAIL", "`description:` must be a YAML string — quote the value",
                           "description-missing"))
    if kind != "action manifest" and len(desc) > DESC_FULL_MAX_LEN:
        out.append(Finding(rel, "FAIL",
                           f"description is {len(desc)} chars (>{DESC_FULL_MAX_LEN}, the Agent Skills limit); "
                           f"the host keeps only the first {DESC_FULL_MAX_LEN}", "description-length"))
    elif len(desc) > DESC_MAX_LEN:
        # A complete opening sentence within the tree budget is the deterministic proof that the
        # routing signal was intentionally front-loaded; the rich detail after it stays in the full
        # value (an action manifest's feeds the per-run action catalog full-length).
        first_end = next((m.end() for m in _SENTENCE_BOUNDARY.finditer(desc)), None)
        if first_end is None or first_end > DESC_MAX_LEN:
            out.append(Finding(rel, "WARN",
                               f"description is {len(desc)} chars and its first complete sentence exceeds "
                               f"the {DESC_MAX_LEN}-char tree gloss — the tree line shows the first "
                               f"{DESC_MAX_LEN} chars, so lead with a short when-to-use sentence; detail "
                               "after it stays in the full description", "description-length"))
    if _CONTAINS_STYLE.match(desc):
        out.append(Finding(rel, "WARN",
                           "description reads as \"what this contains\"; prefer \"when to open this\" "
                           "phrasing in the customer's own words", "description-style"))
    return out


def _git_index(root: Path) -> tuple[frozenset[str], frozenset[str]] | None:
    """(tracked paths, tracked symlink paths) from the git index, or None when git can't answer.

    `git ls-files -s` is the only source that knows what a *fresh checkout* will contain — the working
    tree cannot distinguish a tracked file from an ignored one that only exists locally.
    """
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-s", "-z"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    tracked: set[str] = set()
    links: set[str] = set()
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, rel = entry.partition("\t")
        if not rel:
            continue
        tracked.add(rel)
        if meta.split(" ", 1)[0] == "120000":
            links.add(rel)
    return frozenset(tracked), frozenset(links)


def _walk_symlinks(root: Path) -> list[str]:
    """Every symlink under `root` (repo-relative, posix), used when git can't tell us what's tracked."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(n for n in dirnames if n not in _SYMLINK_WALK_IGNORED_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for name in sorted(dirnames + filenames):
            if (Path(dirpath) / name).is_symlink():
                found.append((rel_dir / name).as_posix())
    return sorted(set(found))


# The kit's documented alias: the committed link lets Claude Code find the gitignored kit install.
_KIT_SKILLS_ALIAS = (".claude/skills", "../.agents/skills")


def _check_symlinks(root: Path) -> list[Finding]:
    """FAIL symlinks that can never point at brain content; WARN merely dangling relative ones.

    A committed `.claude/skills -> ../.agents/skills` alias over a gitignored target is the supported
    layout: the link dangles in a fresh checkout, and the host skips dangling relative in-repo links
    when it materializes a run view, so that exact alias is silent (`_KIT_SKILLS_ALIAS`); any other
    dangling link still WARNs. Absolute or escaping targets stay fatal — they would reach outside the
    brain if they resolved at all.
    """
    fix = "remove the symlink or track its target; untracked targets dangle in run worktrees"
    dangling = ("dangling in checkouts (target untracked); harmless, the host skips it when building "
                "the run view")
    index = _git_index(root)
    candidates = sorted(index[1]) if index else _walk_symlinks(root)
    tracked = index[0] if index else None

    findings: list[Finding] = []
    for rel in candidates:
        link = root / rel
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if os.path.isabs(target):
            findings.append(Finding(
                rel, "FAIL",
                f"symlink target {target!r} is absolute and exists only on this machine — {fix}",
                "symlink-broken",
            ))
            continue
        resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
        if resolved == ".." or resolved.startswith("../") or os.path.isabs(resolved):
            findings.append(Finding(
                rel, "FAIL",
                f"symlink target {target!r} escapes the brain root — {fix}",
                "symlink-broken",
            ))
            continue
        if (rel, target.rstrip("/")) == _KIT_SKILLS_ALIAS:
            continue
        if tracked is None:
            if not link.exists():  # follows the link; git-less fallback can only prove disk existence
                findings.append(Finding(
                    rel, "WARN", f"symlink target {target!r} does not exist — {dangling}",
                    "symlink-broken",
                ))
            continue
        prefix = resolved + "/"
        if resolved not in tracked and not any(t.startswith(prefix) for t in tracked):
            findings.append(Finding(
                rel, "WARN",
                f"symlink target {target!r} is not tracked in this repo — {dangling}",
                "symlink-broken",
            ))
    return findings


def lint_brain(brain_root: str | Path, kind: str | None = None, near_cliff: bool = False) -> list[Finding]:
    """Lint every routable file under `brain_root` for a host-readable, in-budget `description:`.

    Targets, mirroring the authoring mandate: `skills/*/SKILL.md`, `skills/cases/*.md`, and
    `actions/*/manifest.yaml`. Pure + deterministic (stdlib + PyYAML): no network, no DSN, no model.
    `kind` (brain | tenant | mirror) sets which frontmatter the host reads; None = detect_repo_kind.
    `near_cliff` adds the on-demand doc-size WARN below the FAIL: for named/changed paths only.
    """
    root = Path(brain_root)
    findings: list[Finding] = []
    hidden = run_hidden(root)  # operator code a run never sees (e.g. `/action-authoring/`) is not shipped

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        rel_dir = Path(dirpath).relative_to(root)
        skipped = _SCRIPT_IGNORED_DIRS | (_SCRIPT_ALLOWED_ROOTS if not rel_dir.parts else frozenset())
        dirnames[:] = sorted(name for name in dirnames if name not in skipped)
        for filename in sorted(filenames):
            if not filename.endswith(".py") or (not rel_dir.parts and filename == "conftest.py"):
                continue
            rel_path = rel_dir / filename
            if hidden(rel_path.as_posix()):
                continue
            findings.append(Finding(
                rel_path.as_posix(),
                "FAIL",
                "Python scripts belong in `skills/<topic>/scripts/`; import smoke intentionally "
                "covers only `skills/**`",
                "script-outside-skills",
            ))

    findings += _check_symlinks(root)

    for md, label in [*((p, "SKILL.md") for p in sorted(root.glob("skills/*/SKILL.md"))),
                      *((p, "runbook") for p in sorted(root.glob("skills/cases/*.md")))]:
        desc, how = _frontmatter_description(_read_head(md))
        findings += _check(_rel(root, md), desc, label, how)

    for manifest in sorted(root.glob("actions/*/manifest.yaml")):
        findings += _check(_rel(root, manifest), _manifest_description(manifest), "action manifest")

    surface_manifests = set(root.glob("actions/*/manifest.yaml"))
    surface_manifests.update(root.glob("actions-drafts/*/manifest.yaml"))
    for manifest in sorted(surface_manifests):
        findings += _check_manifest_surfaces(manifest, _rel(root, manifest))
        findings += _check_manifest_enums(manifest, _rel(root, manifest))

    kind = kind or detect_repo_kind(root)[0]
    if kind != "mirror":  # a mirror's `surfaces:` is never honored at all — _check_frontmatter FAILs it
        findings += _check_doc_surfaces(root)
    findings += _check_doc_sizes(root, kind, near_cliff)
    findings += _check_frontmatter(root, kind)

    # Bind the sibling lint when this plugin loads, before collected brain tests can replace the
    # top-level ``lib`` module in ``sys.modules`` with a test double.
    findings += [Finding(f.path, f.level, f.message, f.rule) for f in lint_actions(root)]
    # One run-hidden filter for every caller (CLI, pytest tier, publish gate): paths the host drops from
    # every run view (`run_hidden`: root .replypenignore/.rcignore + always-hidden names) are operator-only,
    # and the host skips them before it inspects anything, symlinks included (treeview visible.go). A path
    # merely named `_internal/` is NOT hidden unless an ignore rule says so, so it is still judged.
    # `_HIDDEN_REPORTED` rules exist to say a declaration there is never read, so they keep reporting.
    return [f for f in findings if f.rule in _HIDDEN_REPORTED or not hidden(f.path.split(":", 1)[0])]


# Rules that still report on run-hidden paths (see the filter at the end of `lint_brain`).
_HIDDEN_REPORTED = frozenset({"frontmatter-scope"})


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


_RULE_LABELS = {
    "action-surfaces": "action surfaces",
    "doc-surfaces": "doc surfaces",
    "doc-size": "doc size",
    "frontmatter-scope": "frontmatter the host never reads here",
    "frontmatter-unread": "frontmatter the host cannot parse",
    "frontmatter-redundant": "redundant frontmatter",
    "include-in-value": "unknown include_in roles",
    "description-missing": "missing descriptions",
    "description-length": "description length",
    "description-style": "description style",
    "script-size": "script size",
    "symlink-broken": "broken symlinks",
    "script-outside-skills": "scripts outside skills",
    "helper-duplicate": "duplicate helpers",
    "helper-drift": "drifted helpers",
    "private-dead": "dead private names",
    "other": "other",
}


# One fix line per rule group, printed under its header instead of on every row.
_RULE_FIXES = {
    "frontmatter-scope": "fix: move the file where its mount's host scan reads the key, or drop the key "
                         "(brain_lint.py prints the repo kind it linted as; override with --as)",
    "frontmatter-unread": "fix: `---` on line 1, keys at column 0 spelled exactly `include_in` / `surfaces` / "
                          "`description`, one-line or `- item` lists, one `description:`, frontmatter "
                          "closed within 8 KB, valid YAML (quote a value containing `: `)",
    "frontmatter-redundant": "fix: drop the listed tags; the file already reaches that prompt whole",
    "include-in-value": "fix: use only triage, grounding, agent, principal",
    "doc-size": "fix: keep a lean core with the most important rules first; move detail into grouped "
                "files (e.g. one per domain), each linked from the core by one \"Open X for Y\" line",
}


def format_report(findings: list[Finding]) -> str:
    """Render one deterministic compact block: FAIL groups first, then WARN groups by rule."""
    fails = sum(f.level == "FAIL" for f in findings)
    warns = sum(f.level == "WARN" for f in findings)
    if not findings:
        return "brain lint: clean"

    lines = [f"brain lint: {fails} FAIL, {warns} WARN"]
    for level in ("FAIL", "WARN"):
        rules = sorted({f.rule for f in findings if f.level == level},
                       key=lambda rule: (_RULE_LABELS.get(rule, rule), rule))
        for rule in rules:
            rows = sorted((f for f in findings if f.level == level and f.rule == rule),
                          key=lambda f: (f.path, f.message))
            lines.append(f"{level} {_RULE_LABELS.get(rule, rule)} ({len(rows)})")
            if rule in _RULE_FIXES:
                lines.append(f"  {_RULE_FIXES[rule]}")
            lines.extend(f"  {f.path} — {f.message}" for f in rows)
    return "\n".join(lines)
