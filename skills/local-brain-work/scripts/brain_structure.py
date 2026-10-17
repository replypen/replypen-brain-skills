# /// script
# requires-python = ">=3.11"
# ///
"""Default structural validator for a Markdown-only brain checkout — the check `brain_test.py` cannot
do (it exits "no tests" for docs-only brains). Used by `brain-harvest` and `brain-publish` before push.

Run it from a brain checkout root; it grounds every check in `git ls-files`, so only committed/tracked
content is judged. Checks (each independently reported, skippable with `--skip <name>`):

  * links        — every relative Markdown link/route target in tracked *.md resolves to a tracked
                   path. Run-hidden sources (`.replypenignore`/`.rcignore`/`_internal/`) are not
                   judged: maintainer-only docs may link to gitignored kit skills.
  * frontmatter  — every tracked `skills/*/SKILL.md` has a valid front-matter block (name + description).
  * reachability — routed case/notes/playbook files are reachable from the project router (AGENTS.md).
  * ignored-refs — NOTICE when run-visible text references a path hidden by `.replypenignore`,
                   `.rcignore`, the conventional `_internal/` tree, `.gitignore`, or the controls.
  * lint         — `brain_lint.py` passes on staged files and on the tree scope (below).
  * hygiene      — `brain_hygiene.py`: conflict markers, unresolved `{{ }}`, `/Users/` paths, dead
                   links, em dashes in customer copy, unrenderable mermaid, a `chat_inspiration.md`
                   the host would silently mangle (changed-file scope).
  * raw-tracked  — no raw-harvest path is tracked now (`.rootcause/` fragments or split-file shapes).
  * raw-history  — no raw-harvest path appears in git history (deleted-but-still-in-history case).
  * scratch      — (`--expect-clean` only) no `.rootcause/harvest/` scratch root remains on disk.

Scope (`--scope new|full`, default `new`): a publish gate must judge the work being published, not
re-litigate a mature tree's legacy debt on every push — a gate that always fails gets bypassed.
Under the default `new` scope, pre-existing findings already present on `origin/main` do not fail
the run; only regressions do. Two mechanisms, both falling back to `full` when `origin/main` is
absent:

  * links/frontmatter/reachability run twice — once against the materialized `origin/main` tree as a
    baseline — and only findings absent from the baseline fail (suppressed counts are still
    reported). Whole-tree semantics are kept so e.g. a router edit that orphans an *untouched* file
    still fails.
  * lint's tree pass scans only files carrying local work vs `origin/main` (committed-ahead, staged,
    unstaged, untracked) instead of `--all`; add `--strict-lint` to make SOFT findings fatal too —
    the harvest gate runs its own `--all --strict` pass.

`--scope full` restores the strict everything-must-be-clean behavior for cleanup sweeps.

    uv run --no-project python brain_structure.py                 # default checks, from a brain root
    uv run --no-project python brain_structure.py --expect-clean  # + post-cleanup scratch-root check
    uv run --no-project python brain_structure.py --skip lint     # compose inside harvest/publish flows
    uv run --no-project python brain_structure.py --json          # machine-readable report

Exit status: 1 if any active check produced a blocking finding, else 0. NOTICE findings never affect
the exit status. Findings print grep-style: `path:line: <check>: <message>` (or `path: …` /
`<check>: …` when no line/path applies), with NOTICE prefixed explicitly, followed by a
machine-readable `SUMMARY …` line.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ── conventions (see docs/brain-model.md) ───────────────────────────────────────────────────────────
ROUTER = "AGENTS.md"
# Directories whose Markdown leaves a run must be *routed to* — orphaning one hides it from the loop.
ROUTED_DIRS = {"cases", "notes", "playbooks"}
ROUTED_FILES = {"terminology.md"}
# Audit artifacts (e.g. committed harvest records) are records, not run-routed content leaves;
# they never need a router path.
AUDIT_DIRS = {"harvest-records"}
# Infrastructure/entry-surface trees that have their own discovery path, so they never need routing
# from the project router. `skills/` are their own entry surface; `docs/`, `actions/`, `tests/`,
# `agents/`, and dot-trees are tooling, not run-routed brain leaves.
EXEMPT_DIRS = {"skills", "docs", "actions", "tests", "agents", ".git", ".claude", ".rootcause",
               ".github", "node_modules"}

# Raw-harvest fingerprints. A split corpus lands as `<dir>/threads/<yyyy-mm>--<slug>--<idx>.md` under
# the gitignored `.rootcause/`; either shape appearing as a tracked path is a privacy leak.
RAW_ROOTCAUSE_RE = re.compile(r"(?:^|/)\.rootcause/")
RAW_SPLIT_RE = re.compile(r"(?:^|/)\d{4}-\d{2}--.+?--\d+\.md$")
SCRATCH_ROOT = ".rootcause/harvest"

# A Markdown inline link/image target: `[text](target)` or `![alt](target "title")`.
MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)]+?)\s*\)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
# CommonMark code span: a backtick run closed by the next run of EXACTLY the same length (an unmatched
# run is literal text); it may wrap lines inside one paragraph, never across a blank line.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
INDENTED_RE = re.compile(r"^(?: {4}|\t)")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])(?:\s|$)")
# A backticked path reference: the documented router convention is a symptom -> path table whose
# cells carry plain/backticked relative paths (see docs/brain-model.md routing index), not inline
# Markdown links — those references are reachability edges too.
PATH_REF_RE = re.compile(r"`([^`\s]+\.md)`")
# Non-file targets: any scheme (http:, mailto:, tel:), protocol-relative `//host`, or anchor-only `#x`.
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//|#)", re.I)

DEFAULT_LINT_SCRIPT = (Path(__file__).resolve().parent / ".." / ".." /
                       "brain-harvest" / "scripts" / "brain_lint.py").resolve()

# Mirrors brain_lint.py's SCAN_SUFFIXES: the changed-scope pass passes explicit paths, and explicit
# paths bypass the linter's own suffix filter, so filter here to keep parity with its `--all` set.
LINT_SUFFIXES = {".md", ".py", ".rb", ".sh", ".toml", ".yaml", ".yml", ".json"}

IGNORE_CONTROLS = (".replypenignore", ".rcignore")
ALWAYS_HIDDEN_FILES = {".gitignore", ".replypenignore", ".rcignore"}
CONTROL_REFERENCE_FILES = (".gitignore", ".replypenignore")
NAVIGATION_HINTS = ("see ", "read ", "open ", "consult ", "refer to", "authoritative")
BACKTICK_REF_RE = re.compile(r"`([^`\s]+)`")
EXTERNAL_MOUNT_PREFIXES = ("/mirrors/", "/tenant/", "/kb/", "/skills/")


class StructureError(RuntimeError):
    """The checkout cannot be validated at all (not a git repo, git unusable)."""


@dataclass
class Finding:
    check: str
    message: str
    path: str | None = None
    line: int | None = None
    severity: str = "ERROR"

    def render(self) -> str:
        prefix = "NOTICE " if self.severity == "NOTICE" else ""
        if self.path and self.line:
            return f"{prefix}{self.path}:{self.line}: {self.check}: {self.message}"
        if self.path:
            return f"{prefix}{self.path}: {self.check}: {self.message}"
        return f"{prefix}{self.check}: {self.message}"

    def as_dict(self) -> dict[str, object]:
        return {"check": self.check, "path": self.path, "line": self.line,
                "message": self.message, "severity": self.severity}


@dataclass
class Ctx:
    root: Path
    tracked: list[str]
    tracked_set: set[str] = field(default_factory=set)
    md_files: list[str] = field(default_factory=list)
    lint_script: Path = DEFAULT_LINT_SCRIPT
    history_limit: int = 2000
    strict_lint: bool = False
    scope: str = "new"
    _hidden: set[str] | None = None

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")


# ── git plumbing ────────────────────────────────────────────────────────────────────────────────────
def git_toplevel(start: Path) -> Path:
    proc = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise StructureError(f"run from a brain checkout (git repository) root: {start}")
    return Path(proc.stdout.strip()).resolve()


def git_tracked(root: Path) -> list[str]:
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise StructureError(f"git ls-files failed: {proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def git_changed_paths(root: Path, base: str = "origin/main") -> list[str] | None:
    """Paths carrying local work relative to `base` (default origin/main): commits ahead of the merge-base, plus
    staged, unstaged, and untracked files. Returns None when origin/main (or an unborn HEAD) makes
    the scope uncomputable — the caller then falls back to a full-tree lint."""
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                            base], capture_output=True, text=True)
    if probe.returncode != 0:
        return None
    paths: dict[str, None] = {}
    for args in (["diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"],
                 ["diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        for ln in proc.stdout.splitlines():
            if ln.strip():
                paths.setdefault(ln.strip())
    return list(paths)


def git_baseline_ctx(root: Path, tmp: Path) -> Ctx | None:
    """Materialize the origin/main tree into `tmp` and return a Ctx over it, or None when
    origin/main is not resolvable (caller then treats every finding as new)."""
    tree = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "origin/main"],
                          capture_output=True)
    if tree.returncode != 0:
        return None
    listing = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only",
                              "origin/main"], capture_output=True, text=True)
    if listing.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(tree.stdout)) as tf:
        tf.extractall(tmp, filter="data")
    tracked = [p for p in listing.stdout.split("\0") if p]
    return Ctx(root=tmp, tracked=tracked, tracked_set=set(tracked),
               md_files=[p for p in tracked if p.lower().endswith(".md")])


def git_history_paths(root: Path, limit: int) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--all", "--name-only", "--pretty=format:", f"-n{limit}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        # No commits yet, or unusable history — treat as nothing to scan rather than a hard error.
        return []
    return list(dict.fromkeys(p for p in (ln.strip() for ln in proc.stdout.splitlines()) if p))


# ── link parsing ────────────────────────────────────────────────────────────────────────────────────
def code_free_lines(text: str) -> list[str]:
    """Lines with code blanked, numbering kept: what a Markdown renderer treats as prose. A
    `[title](url)` or a quoted tag inside code is an example, not a link or leaked markup.

    Code = fenced blocks, indented (4-space/tab) blocks that start after a blank line outside a list
    (inside a list that indentation is item continuation, so it stays prose), and inline code spans,
    also when one wraps lines."""
    kept: list[str] = []
    fence, indented, blank_before, in_list = "", False, True, False
    for line in text.splitlines():
        blank = not line.strip()
        if fence:
            m = FENCE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = ""
            kept.append("")
            continue
        if indented:
            if blank or INDENTED_RE.match(line):
                kept.append("")
                continue
            indented = False
        if not blank and blank_before and not in_list and INDENTED_RE.match(line):
            indented = True
            kept.append("")
            continue
        if m := FENCE_RE.match(line):
            fence = m.group(1)
            kept.append("")
            blank_before = False
            continue
        kept.append(line)
        if not blank:
            in_list = bool(LIST_ITEM_RE.match(line)) or (in_list and line[:1] in " \t")
        blank_before = blank
    prose = CODE_SPAN_RE.sub(lambda m: "\n" * m.group(0).count("\n"), "\n".join(kept))
    return prose.split("\n")


def iter_links(text: str):
    """Yield (lineno, raw_target) for every inline Markdown link/image target outside code."""
    for lineno, line in enumerate(code_free_lines(text), start=1):
        for m in MD_LINK_RE.finditer(line):
            target = m.group(1).split()[0].strip("<>") if m.group(1).split() else ""
            if target:
                yield lineno, target


def resolve_target(root: Path, md_rel: str, target: str) -> str | None:
    """Resolve a relative link target to a posix path relative to root, or None if not resolvable."""
    path_part = target.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        return None
    if path_part.startswith("/"):
        base = root
        path_part = path_part.lstrip("/")
    else:
        base = (root / md_rel).parent
    resolved = Path(os.path.normpath(base / path_part)).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return ".."  # escapes the checkout; caller treats a sentinel as unresolved


# ── checks ──────────────────────────────────────────────────────────────────────────────────────────
def check_links(ctx: Ctx) -> list[Finding]:
    findings: list[Finding] = []
    tracked_dirs = {parent for p in ctx.tracked for parent in _ancestors(p)}
    # A run-hidden source file (`.agents/`, `.claude/`, `_internal/`, … per the ignore controls) is
    # maintainer-only: it may legitimately link to gitignored kit skills, so its links are not judged.
    hidden = hidden_paths(ctx)
    for md in ctx.md_files:
        if md in hidden:
            continue
        for lineno, target in iter_links(ctx.read(md)):
            if EXTERNAL_RE.match(target) or target.startswith("#"):
                continue
            rel = resolve_target(ctx.root, md, target)
            if rel is None:
                continue
            if rel in ctx.tracked_set or rel in tracked_dirs:
                continue
            findings.append(Finding("links", f"link target does not resolve to a tracked path: {target}",
                                    path=md, line=lineno))
    return findings


def _ancestors(posix_path: str) -> list[str]:
    parts = posix_path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Flat `key: value` front-matter between leading `---` fences; None if absent/unterminated."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    data: dict[str, str] = {}
    for raw in lines[1:]:
        if raw.strip() == "---":
            return data
        if ":" in raw and not raw.startswith((" ", "\t", "-")):
            key, value = raw.split(":", 1)
            data[key.strip()] = value.strip()
    return None


def check_frontmatter(ctx: Ctx) -> list[Finding]:
    findings: list[Finding] = []
    for md in ctx.md_files:
        parts = md.split("/")
        if not (parts[0] == "skills" and parts[-1] == "SKILL.md"):
            continue
        data = parse_frontmatter(ctx.read(md))
        if data is None:
            findings.append(Finding("frontmatter", "missing or unterminated YAML front-matter block",
                                    path=md, line=1))
            continue
        for key in ("name", "description"):
            if not data.get(key):
                findings.append(Finding("frontmatter", f"front-matter is missing a non-empty {key!r}",
                                        path=md, line=1))
    return findings


def check_reachability(ctx: Ctx) -> list[Finding]:
    routed = [md for md in ctx.md_files if _needs_reach(md)]
    if not routed:
        return []
    md_set = set(ctx.md_files)
    adjacency: dict[str, set[str]] = {}
    for md in ctx.md_files:
        text = ctx.read(md)
        neighbours: set[str] = set()
        targets = [t for _lineno, t in iter_links(text)]
        targets += PATH_REF_RE.findall(text)
        for target in targets:
            if EXTERNAL_RE.match(target):
                continue
            # Router tables write paths either relative to the referencing file or to the checkout
            # root; accept whichever resolves to a tracked Markdown file.
            for candidate in (resolve_target(ctx.root, md, target),
                              resolve_target(ctx.root, ROUTER, target)):
                if candidate and candidate in md_set:
                    neighbours.add(candidate)
                    break
        adjacency[md] = neighbours
    reachable: set[str] = set()
    if ROUTER in md_set:
        stack = [ROUTER]
        while stack:
            node = stack.pop()
            if node in reachable:
                continue
            reachable.add(node)
            stack.extend(adjacency.get(node, ()))
    return [Finding("reachability",
                    f"routed file is not reachable from the project router ({ROUTER})", path=md)
            for md in routed if md not in reachable]


def _needs_reach(posix_path: str) -> bool:
    parts = posix_path.split("/")
    if posix_path == ROUTER:
        return False
    if any(p in EXEMPT_DIRS or p in AUDIT_DIRS for p in parts[:-1]):
        return False
    if parts[-1] in ROUTED_FILES:
        return True
    return any(p in ROUTED_DIRS for p in parts[:-1])


def _ignored_tracked_paths(ctx: Ctx) -> set[str]:
    """Return the union of tracked paths excluded by either run-visibility control."""
    ignored: set[str] = set()
    for control in IGNORE_CONTROLS:
        if control not in ctx.tracked_set or not (ctx.root / control).is_file():
            continue
        proc = subprocess.run(
            ["git", "-C", str(ctx.root), "ls-files", "-z", "-c", "-i",
             f"--exclude-from={ctx.root / control}"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise StructureError(f"git ls-files failed for {control}: {proc.stderr.strip()}")
        ignored.update(p for p in proc.stdout.split("\0") if p)
    ignored.update(p for p in ctx.tracked if p == "_internal" or p.startswith("_internal/"))
    ignored.update(ALWAYS_HIDDEN_FILES & ctx.tracked_set)
    return ignored


def hidden_paths(ctx: Ctx) -> set[str]:
    """Cached run-hidden tracked paths; empty when they cannot be resolved (e.g. the materialized
    origin/main baseline tree, which is not a git checkout)."""
    if ctx._hidden is None:
        try:
            ctx._hidden = _ignored_tracked_paths(ctx)
        except StructureError:
            ctx._hidden = set()
    return ctx._hidden


def _is_control_navigation(line: str) -> bool:
    """Distinguish a dangling instruction from prose that merely explains ignore mechanics."""
    folded = line.casefold()
    return any(hint in folded for hint in NAVIGATION_HINTS)


def _line_resolved_targets(ctx: Ctx, rel: str, line: str) -> set[str]:
    """Resolve Markdown/backtick path tokens relative to their file or the brain root."""
    raw_targets = [target for _lineno, target in iter_links(line)]
    raw_targets.extend(BACKTICK_REF_RE.findall(line))
    resolved: set[str] = set()
    for target in raw_targets:
        if EXTERNAL_RE.match(target):
            continue
        for candidate in (resolve_target(ctx.root, rel, target),
                          resolve_target(ctx.root, ROUTER, target)):
            if candidate:
                resolved.add(candidate)
    return resolved


def check_ignored_refs(ctx: Ctx) -> list[Finding]:
    """Notice references from run-visible text to paths the production run cannot open."""
    if ".rootcause.toml" not in ctx.tracked_set and not any(c in ctx.tracked_set for c in IGNORE_CONTROLS):
        return []
    hidden = _ignored_tracked_paths(ctx)
    visible = [p for p in ctx.tracked if p not in hidden and (ctx.root / p).is_file()]
    hidden_candidates = sorted(
        (p for p in hidden if p not in ALWAYS_HIDDEN_FILES and not p.startswith("_internal/")),
        key=lambda p: (-len(p), p),
    )
    findings: list[Finding] = []
    for rel in visible:
        raw = (ctx.root / rel).read_bytes()
        if b"\0" in raw:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        for lineno, line in enumerate(lines, start=1):
            references: list[str] = []
            resolved_targets = _line_resolved_targets(ctx, rel, line)
            linked_controls = set()
            for _link_line, target in iter_links(line):
                linked = target.split("#", 1)[0].split("?", 1)[0].strip("<>")
                linked_controls.add(linked.removeprefix("./").lstrip("/"))
            prior_context = lines[lineno - 2] if lineno > 1 else ""
            external_mount_context = any(
                prefix in f"{prior_context}\n{line}" for prefix in EXTERNAL_MOUNT_PREFIXES)
            if "_internal/" in line or "/_internal" in line:
                references.append("_internal/")
            for control in CONTROL_REFERENCE_FILES:
                if control in line and (_is_control_navigation(line) or control in linked_controls):
                    references.append(control)
            for candidate in hidden_candidates:
                if (("/" in candidate and candidate in line)
                        or (candidate in resolved_targets and not external_mount_context)):
                    references.append(candidate)
            for reference in dict.fromkeys(references):
                findings.append(Finding(
                    "ignored-refs",
                    f"run-visible text references hidden path unavailable in runs: {reference}",
                    path=rel,
                    line=lineno,
                    severity="NOTICE",
                ))
    return findings


def check_lint(ctx: Ctx) -> list[Finding]:
    if not ctx.lint_script.is_file():
        return [Finding("lint",
                        f"privacy/contract lint script not found at {ctx.lint_script}; pass --lint-script")]
    findings: list[Finding] = []
    # Strict is opt-in (--strict-lint): under --strict every SOFT class is fatal, and the linter
    # deliberately keeps legit-in-a-brain shapes (own routing addresses, blockquotes, coarse
    # heuristics) SOFT so they warn without blocking. The harvest gate runs `--all --strict` itself
    # (SKILL step 8, spec §9); the generic publish path blocks on HARD findings only.
    strict = ["--strict"] if ctx.strict_lint else []
    passes: list[tuple[str, list[str]]] = [("staged", [])]
    changed = None if ctx.scope == "full" else git_changed_paths(ctx.root)
    if ctx.scope == "full" or changed is None:
        # Whole tree: explicit --lint-scope full, or no origin/main to scope against.
        passes.append(("full-tree", ["--all", *strict]))
    else:
        # Changed scope: lint only files carrying local work vs origin/main, so pre-existing
        # findings elsewhere in a mature tree cannot block an unrelated publish. Touching a file
        # re-exposes every finding in it — the tree ratchets cleaner as files get edited.
        targets = sorted(p for p in changed
                         if Path(p).suffix.lower() in LINT_SUFFIXES and (ctx.root / p).is_file())
        if targets:
            passes.append((f"changed-scope ({len(targets)} file(s) vs origin/main)",
                           [*strict, *targets]))
    for label, extra in passes:
        proc = subprocess.run([sys.executable, str(ctx.lint_script), *extra],
                              cwd=str(ctx.root), capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            reason = tail[-1] if tail else f"exit {proc.returncode}"
            findings.append(Finding("lint", f"brain_lint {label} mode failed (exit {proc.returncode}): "
                                            f"{reason}"))
    return findings


def check_hygiene(ctx: Ctx) -> list[Finding]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import brain_hygiene  # lazy: brain_hygiene imports this module
    return brain_hygiene.check(ctx.root, None if ctx.scope == "full" else git_changed_paths(ctx.root),
                               "origin/main")


def check_raw_tracked(ctx: Ctx) -> list[Finding]:
    return [Finding("raw-tracked", "raw-harvest path is tracked; it must never be committed", path=p)
            for p in ctx.tracked if RAW_ROOTCAUSE_RE.search(p) or RAW_SPLIT_RE.search(p)]


def check_raw_history(ctx: Ctx) -> list[Finding]:
    findings: list[Finding] = []
    for p in git_history_paths(ctx.root, ctx.history_limit):
        if RAW_ROOTCAUSE_RE.search(p) or RAW_SPLIT_RE.search(p):
            findings.append(Finding(
                "raw-history",
                "raw-harvest path exists in git history (deleted from the tree but still recoverable); "
                "escalate to the operator for a deliberate history rewrite — never auto-rewrite",
                path=p))
    return findings


def check_scratch(ctx: Ctx) -> list[Finding]:
    if (ctx.root / SCRATCH_ROOT).exists():
        return [Finding("scratch", "sensitive harvest scratch root still exists on disk; delete it "
                                   "before publishing", path=SCRATCH_ROOT)]
    return []


CHECKS: list[tuple[str, Callable[[Ctx], list[Finding]]]] = [
    ("links", check_links),
    ("frontmatter", check_frontmatter),
    ("reachability", check_reachability),
    ("ignored-refs", check_ignored_refs),
    ("lint", check_lint),
    ("hygiene", check_hygiene),
    ("raw-tracked", check_raw_tracked),
    ("raw-history", check_raw_history),
]
EXPECT_CLEAN_CHECK: tuple[str, Callable[[Ctx], list[Finding]]] = ("scratch", check_scratch)
ALL_CHECK_NAMES = [name for name, _ in CHECKS] + [EXPECT_CLEAN_CHECK[0]]
# Tree checks whose pre-existing findings are baselined against origin/main under --scope new.
# Line numbers are excluded from the baseline key so unrelated edits shifting a finding down a file
# do not resurrect it. lint scopes itself (changed files); raw-* / scratch stay absolute — a privacy
# leak or scratch root must block even if it already sits on origin/main.
BASELINED_CHECKS = {"links", "frontmatter", "reachability"}


def finding_key(f: Finding) -> tuple[str, str | None, str]:
    return (f.check, f.path, f.message)


def baseline_keys(root: Path, skip: set[str]) -> set[tuple[str, str | None, str]] | None:
    """Findings already present on origin/main for the baselined checks; None without origin/main."""
    with tempfile.TemporaryDirectory(prefix="brain-structure-baseline-") as tmp:
        bctx = git_baseline_ctx(root, Path(tmp))
        if bctx is None:
            return None
        return {finding_key(f)
                for name, fn in CHECKS if name in BASELINED_CHECKS and name not in skip
                for f in fn(bctx)}


def run_checks(ctx: Ctx, *, expect_clean: bool, skip: set[str],
               baseline: set[tuple[str, str | None, str]] | None) -> list[dict[str, object]]:
    active = list(CHECKS)
    if expect_clean:
        active.append(EXPECT_CLEAN_CHECK)
    results: list[dict[str, object]] = []
    for name, fn in active:
        if name in skip:
            results.append({"name": name, "skipped": True, "ok": True, "findings": [],
                            "baselined": 0})
            continue
        found = fn(ctx)
        baselined = 0
        if baseline is not None and name in BASELINED_CHECKS:
            fresh = [f for f in found if finding_key(f) not in baseline]
            baselined = len(found) - len(fresh)
            found = fresh
        blocking = [f for f in found if f.severity != "NOTICE"]
        results.append({"name": name, "skipped": False, "ok": not blocking, "findings": found,
                        "baselined": baselined})
    return results


def build_report(results: list[dict[str, object]], *, scope: str) -> dict[str, object]:
    ran = [r for r in results if not r["skipped"]]
    failed = [r for r in ran if not r["ok"]]
    findings = [f for r in results for f in r["findings"]]
    notices = [f for f in findings if f.severity == "NOTICE"]
    baselined = sum(r["baselined"] for r in results)
    return {
        "ok": not failed,
        "scope": scope,
        "checks": [
            {"name": r["name"], "skipped": r["skipped"], "ok": r["ok"],
             "baselined": r["baselined"],
             "findings": [f.as_dict() for f in r["findings"]]}
            for r in results
        ],
        "findings": [f.as_dict() for f in findings],
        "summary": {"checks": len(results), "ran": len(ran), "passed": len(ran) - len(failed),
                    "failed": len(failed), "findings": len(findings), "notices": len(notices),
                    "baselined": baselined,
                    "failed_checks": [r["name"] for r in failed]},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_structure.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="brain checkout root (default: cwd's git toplevel)")
    p.add_argument("--skip", action="append", default=[], choices=ALL_CHECK_NAMES, metavar="CHECK",
                   help=f"skip a check by name ({', '.join(ALL_CHECK_NAMES)}); repeatable")
    p.add_argument("--expect-clean", action="store_true",
                   help="also require no .rootcause/harvest/ scratch root remains (post-cleanup gate)")
    p.add_argument("--lint-script", help="override path to brain_lint.py (default: sibling in the kit)")
    p.add_argument("--strict-lint", action="store_true",
                   help="run the tree lint pass with --strict (SOFT findings fatal too); "
                        "default pass blocks on HARD findings only")
    p.add_argument("--scope", choices=["new", "full"], default="new",
                   help="'new' (default): fail only on findings not already present on origin/main "
                        "(links/frontmatter/reachability baseline against it; lint's tree pass scans "
                        "only files with local work). 'full': everything must be clean. Falls back "
                        "to full when origin/main is absent")
    p.add_argument("--history-limit", type=int, default=2000,
                   help="max commits scanned for the raw-history check (default: 2000)")
    p.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        start = Path(args.root).resolve() if args.root else Path.cwd()
        root = git_toplevel(start)
        tracked = git_tracked(root)
    except StructureError as exc:
        print(f"brain-structure: {exc}", file=sys.stderr)
        return 2

    ctx = Ctx(
        root=root,
        tracked=tracked,
        tracked_set=set(tracked),
        md_files=[p for p in tracked if p.lower().endswith(".md")],
        lint_script=Path(args.lint_script).resolve() if args.lint_script else DEFAULT_LINT_SCRIPT,
        history_limit=args.history_limit,
        strict_lint=args.strict_lint,
        scope=args.scope,
    )

    skip = set(args.skip)
    baseline = baseline_keys(root, skip) if args.scope == "new" else None
    results = run_checks(ctx, expect_clean=args.expect_clean, skip=skip, baseline=baseline)
    report = build_report(results, scope=args.scope)
    summary = report["summary"]

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for finding in (f for r in results for f in r["findings"]):
            print(finding.render())
        for r in results:
            if r["baselined"]:
                print(f"NOTE {r['name']}: {r['baselined']} pre-existing finding(s) already on "
                      f"origin/main suppressed (--scope full to see them)")
        failed = summary["failed_checks"]
        print(f"SUMMARY checks={summary['checks']} ran={summary['ran']} passed={summary['passed']} "
              f"failed={summary['failed']} findings={summary['findings']} "
              f"notices={summary['notices']} "
              f"baselined={summary['baselined']} "
              f"failed_checks={','.join(failed) if failed else '-'}")

    return 1 if not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
