# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic pre-publish hygiene gate for a brain checkout — the mechanical mistakes a human
otherwise finds by eye on the live brain (2026-09-25: DentAI shipped `<<<<<<<` markers in
skills/scheduling/SKILL.md). `brain_git_sync.py` runs it before every push; `brain_structure.py`
runs it as its `hygiene` check. Rules (run-hidden `_internal/`/`.rcignore`/`.replypenignore` paths
and dev-tooling dot-dirs are judged for conflict markers only):

  * conflict     — `<<<<<<<` / `|||||||` / `>>>>>>>` line markers. Whole tree, always.
  * placeholder  — in a file `projection.yaml` templates: a `{{ key }}` it does not declare, or a
                   residual `{{`/`}}` (ReplyPen's projection fails the whole run on both).
                   Untemplated files may quote `{{ }}` (customer merge fields, journals).
  * abs-path     — a `/Users/<name>` laptop path.
  * links        — a relative Markdown link that resolves to no tracked path.
  * em-dash      — U+2014 in customer-facing copy: action manifest `display_name`/`customer_*`,
                   `projection.yaml` placeholder defaults, Markdown with `customer_facing: true`
                   frontmatter.
  * mermaid      — a ```mermaid block `mmdc` rejects as a syntax error (`mmdc` on PATH, else
                   `pnpm dlx @mermaid-js/mermaid-cli`). No renderer, or a renderer that cannot
                   start (e.g. no headless Chrome), skips the rule with a NOTICE.
  * description  — a `skills/**/*.md` frontmatter `description` over 1024 characters (the Agent
                   Skills limit; the publish lint rejects it, the host keeps only the first 1024).
  * action-size  — NOTICE only: an action manifest `description` over 900 characters or a param
                   description over 300. The host renders both into the action tool on every model
                   turn; implementation detail belongs in script.py / README.md (docs/actions.md).
                   Needs PyYAML; without it the rule is skipped.
  * startup-size — NOTICE only: the main-loop startup set (root AGENTS.md of a brain/overlay plus every
                   doc the host pastes for `include_in: [agent]`) over budget: AGENTS.md > 8000 chars,
                   another agent doc > 4000, the set > 12000. Pasted on every run; case detail belongs in
                   on-demand files routed by `description:` (docs/brain-model.md §Startup context budget).
                   Judged when a change touches the set (or --all).
  * chat-inspiration — the chat example-prompt gallery set (root `chat_inspiration.md`, then
                   `chat_inspiration/*.md` by name), parsed as ONE library exactly as the host does
                   (`parse_chat_inspiration_set`), whose parser drops bad lines silently. FAIL:
                   no prompt at all, a bullet in a category that yields no prompt, a cap exceeded
                   (20 categories, 50 prompts/category, 500-char prompt, 80-char title), a heading
                   that yields no category, a duplicate category id within one file, an
                   unbalanced `[placeholder]`, frontmatter `surfaces:` missing or without `chat`
                   (per file), a plain line after a
                   category's first prompt, an invalid/missing `skills:` entry, a note/skill cap
                   (300-char note, 8 notes, 6 skills effective), a top-level `- skills:` bullet.
                   NOTICE (non-blocking): prompt over 160 chars, no bold title, empty category,
                   duplicate prompt text, bullets swallowed by a sub-heading, an indented bullet
                   before a category's first prompt, a `skill:` typo, a prompt without any skill
                   pointer (only once the file uses `skills:`), a prompt or category preamble with
                   over 600 chars of inline notes (move the recipe to a .md behind `skills:`), a
                   continuation heading that differs from its first part.
  * triage-cases — the triage eval corpus `_internal/triage/cases/*.md`, judged whole whenever a case
                   changed, by `triage_cases.py` (the host parser's mirror): schema, ids, message order,
                   expectations, body sections. NOTICE when no ignore file hides the cases from runs.
  * paths        — NOTICE only: a cited `/brain/…` (`/tenant/…` in an overlay, `/mirrors/<self>/…` in a
                   mirror) this checkout will not ship: absent, gitignored or run-hidden. Local only;
                   `brain_paths.py` also checks `/kb`, `/tenant`, other mirrors in production.
  * doc-spill    — an on-demand doc (no `include_in` the host reads there, no mirrored-corpus
                   `source_url:`/`video_url:` frontmatter, not root AGENTS.md, not under
                   journal/_internal/tests/docs/dot-dirs) whose `cat` output (whole file, a templated
                   doc at its longest render: lint `doc-size`'s measure) crosses 6000 bytes in this
                   change (≤6000 or absent at the base: HEAD for --staged, else `--base`, also for
                   --all); bash shows such a read as a 2000+1000-char preview. Already-over docs stay
                   silent; no resolvable base skips the rule.
  * tool-markup  — tool-call/transcript tags (`</invoke>`, `</content>`, `<parameter name=`, `antml:`)
                   leaked into run-visible Markdown outside code; a run reads them verbatim.

Everything but `conflict` is scoped to changed files (vs `--base`, default origin/main) so a mature
tree's legacy debt cannot block an unrelated publish; `--all` judges every tracked file.

    uv run --no-project python brain_hygiene.py            # changed files vs origin/main
    uv run --no-project python brain_hygiene.py --all      # whole tree
    uv run --no-project python brain_hygiene.py --staged   # managed pre-commit hook
    uv run --no-project python brain_hygiene.py --push     # managed pre-push hook (refs on stdin)

The git gates (`--staged`, `--push`, `brain_git_sync.py`) run `gate()`: the exact bytes being committed
(the index) or pushed (the commit), materialized into a temp checkout, never the working tree; plus the
lint FAILs (`lib.brain_lint`). Regression-only: a finding already present at the base (HEAD; the
remote tip) is dropped, except `conflict`/`abs-path`. `--push` also judges cross-file rules whole-tree
(dead links into untouched files, `reachability` from AGENTS.md) and skips mermaid (commit and publish
render it). Which gate runs what: docs/specs/brain-lint-gates.md.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_paths  # noqa: E402
import brain_structure as bs  # noqa: E402
import triage_cases  # noqa: E402

CONFLICT_RE = re.compile(r"^(<{7}|\|{7}|>{7})(?: |$)")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")  # replypen internal/projection
ABS_PATH_RE = re.compile(r"/Users/[A-Za-z0-9._-]+")
ACTION_README_RE = re.compile(r"^actions/[^/]+/README\.md$", re.I)
MANIFEST_COPY_RE = re.compile(r"^\s*(display_name|customer_[a-z_]+)\s*:(.*)$")
DEFAULT_VALUE_RE = re.compile(r"\bdefault:\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^#,}]*)")
DEFAULT_TEMPLATED_GLOBS = ["**/*.md"]  # replypen internal/projection DefaultTemplatedGlobs
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".toml", ".sh", ".rb", ".txt", ".sql"}
EM_DASH = "\u2014"
TOOLING_DIRS = (".agents/", ".claude/", ".github/")
MERMAID_TIMEOUT_S = 30
LINT_TIMEOUT_S = 20  # the git gates' lint subprocess; past it the lint is skipped with a NOTICE
DESCRIPTION_MAX = 1024  # agentskills.io limit; lib.brain_lint DESC_FULL_MAX_LEN (publish lint)
ACTION_DESCRIPTION_NOTICE = 900  # rendered into the action tool every turn (docs/actions.md)
ACTION_PARAM_DESCRIPTION_NOTICE = 300
# Main-loop startup budget, chars (docs/brain-model.md §Startup context budget). Calibrated 2026-11 on the
# trimmed DentAI / ProBackup / KampAdmin brains: AGENTS.md 6-7k with its routing table, rules doc ≤4k.
STARTUP_AGENTS_MD_NOTICE = 8000
STARTUP_DOC_NOTICE = 4000
STARTUP_TOTAL_NOTICE = 12000
# Tool-call / transcript tags a model wrote into a file it was editing (kampkompas-staff knowledge/,
# 2026-09): the agent later reads them back verbatim. Matched outside code spans/fences only.
TOOL_MARKUP_RE = re.compile(r"</(?:content|invoke|parameter|function_calls)>|<invoke\b|<parameter\s+name="
                            r"|<function_calls>|</?antml:[a-z_]+")
MERMAID_SYNTAX_RE = re.compile(r"(Parse|Lexical|Syntax) error|No diagram type detected", re.I)
NEVER_BASELINED = ("conflict:", "abs-path:")  # merge junk and laptop paths block even when already at base
ZERO_SHA = re.compile(r"0+")

# Mirror of replypen internal/chatinspiration (library.go): keep caps, regexes and separators in sync.
CHAT_INSPIRATION_FILE = "chat_inspiration.md"
CHAT_INSPIRATION_DIR = "chat_inspiration"  # split form: one *.md per category, parsed after the root file
CI_MAX_CATEGORIES, CI_MAX_PROMPTS, CI_MAX_PROMPT_RUNES, CI_MAX_TITLE_RUNES = 20, 50, 500, 80
CI_DERIVED_TITLE_RUNES = 60
CI_READABLE_PROMPT_RUNES = 160  # card readability, not a host cap
CI_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{1,40}\]")
CI_ID_SUFFIX_RE = re.compile(r"\s*\{#([A-Za-z0-9_-]{1,64})\}\s*\Z", re.ASCII)
CI_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$", re.DOTALL)
CI_SEPARATORS = ("\u2014", "\u2013", "-", ":")
CI_MAX_NOTE_RUNES, CI_MAX_NOTES, CI_MAX_SKILLS = 300, 8, 6  # per note line; effective per prompt
# Own inline notes per prompt (or category preamble), `skills:` excluded. p90 across all brains on
# 2026-10-19 (178 prompts: p50 265, p75 396, p90 634): two full directives pass, a recipe warns.
CI_LONG_NOTES_RUNES = 600
CI_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
CI_SKILL_ROOTS = ("/brain/", "/tenant/", "/skills/", "/mirrors/")
CI_SKILL_TYPOS = ("skill:", "skils:", "skillz:")

BRAIN_DOC = Path(__file__).resolve().parents[3] / "runtime" / "lib" / "brain_doc.py"


def load_brain_doc():
    """The kit's stdlib-only `lib.brain_doc`, loaded by path: the hook and push gate run without
    PyYAML, and a brain's own `lib` package must not shadow it. None when the kit ships no runtime/."""
    spec = importlib.util.spec_from_file_location("rc_kit_brain_doc", BRAIN_DOC)
    if spec is None or not BRAIN_DOC.is_file():
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spill_crossings(root: Path, files: list[str], base: str) -> list[bs.Finding]:
    """FAIL per on-demand doc this change pushes past the bash spill cliff (regression-only: a doc
    already over at `base` stays silent). Measured by `brain_doc.on_demand_doc_bytes`, the publish
    lint's `doc-size` measure: what `cat` prints, frontmatter included, a templated doc rendered."""
    doc = load_brain_doc()
    if doc is None:
        return []
    if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{base}^{{commit}}"],
                      capture_output=True).returncode:
        return []  # no base to compare against, so no crossing can be proven

    out: list[bs.Finding] = []
    kind = doc.detect_repo_kind(root)[0]
    render = doc.projection_renderer(_projection_spec(root) if kind == "brain" else None)
    old_spec = _git(root, "show", f"{base}:projection.yaml")
    render_old = doc.projection_renderer(_parse_projection(old_spec.stdout) if kind == "brain" and
                                         old_spec.returncode == 0 else None)
    origins = _rename_origins(root, base)
    limit = doc.ON_DEMAND_DOC_LIMIT
    for rel in sorted(files):
        if not rel.endswith(".md"):
            continue
        text = (root / rel).read_bytes().decode("utf-8", errors="replace")
        new = doc.on_demand_doc_bytes(rel, text, doc.hard_loaded(rel, text, kind), render)
        if new is None or new <= limit:
            continue
        prev = subprocess.run(["git", "-C", str(root), "show", f"{base}:{origins.get(rel, rel)}"],
                              capture_output=True)
        old = len(render_old(rel, prev.stdout.decode("utf-8", errors="replace")).encode("utf-8")) \
            if prev.returncode == 0 else None
        if old is not None and old > limit:
            continue
        out.append(bs.Finding("hygiene", f"doc-spill: {old if old is not None else 'new'}→{new} bytes; a "
                              f"bash read past {limit} shows only a 2000+1000-char preview: split into "
                              "a lean core + detail file linked by an \"Open X for Y\" line", path=rel))
    return out


_SNAPSHOT: list[Path] = []  # the active snapshot() view: its tree is the temp index, not the working tree


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _diff_args() -> list[str]:
    """`git diff <base>` judges index + working tree; inside a snapshot only the temp index is real."""
    return ["diff", "--cached"] if _SNAPSHOT else ["diff"]


def _deleted_since(root: Path, base: str | None) -> set[str]:
    """Paths deleted (a rename counts as delete + add) since `base`, index and working tree."""
    if not base:
        return set()
    out = _git(root, *_diff_args(), "--no-renames", "--name-only", "-z", "--diff-filter=D", base)
    return {p for p in out.stdout.split("\0") if p} if out.returncode == 0 else set()


def _rename_origins(root: Path, base: str) -> dict[str, str]:
    """new path -> its path at `base` for every rename since `base` (index + working tree), so a moved
    legacy doc is compared with itself instead of counting as new."""
    return {new: old for old, new in _renames(root, *_diff_args(), base).items()}


def _renames(root: Path, *diff: str) -> dict[str, str]:
    """base path -> new path for every rename `git <diff>` reports."""
    out = _git(root, diff[0], "-M", "--name-status", "-z", *diff[1:]).stdout.split("\0")
    moved: dict[str, str] = {}
    i = 0
    while i < len(out):
        status = out[i]
        if status.startswith(("R", "C")) and i + 2 < len(out):
            if status.startswith("R"):
                moved[out[i + 1]] = out[i + 2]
            i += 3
        else:
            i += 2 if status else 1
    return moved


def _glob_re(glob: str) -> re.Pattern[str]:
    out = ""
    for part in re.split(r"(\*\*/|\*\*|\*|\?)", glob):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + r"\Z")


_YAML_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
                 "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
                 "L": " ", "P": " "}


def _yaml_scalar(value: str) -> str:
    """One-line quoted YAML scalar decoded like PyYAML (escapes in "…", '' in '…'); plain stays as is."""
    if len(value) > 1 and value[0] == value[-1] == '"':
        return re.sub(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)",
                      lambda m: chr(int(m[1][1:], 16)) if len(m[1]) > 1 else _YAML_ESCAPES.get(m[1], m[0]),
                      value[1:-1])
    if len(value) > 1 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _projection_spec(root: Path) -> dict | None:
    """projection.yaml as `{placeholders: {key: {default}}, templated_globs: [...]}`, decoded like the
    publish lint (PyYAML) so `doc-spill` measures its bytes; this gate also runs without PyYAML, then a
    line scan: flow (`key: { default: "x" }`) and block `default:` values, quoted escapes decoded."""
    spec = root / "projection.yaml"
    return _parse_projection(spec.read_text("utf-8", errors="replace")) if spec.is_file() else None


def _parse_projection(text: str) -> dict:
    try:
        import yaml
    except ModuleNotFoundError:
        yaml = None
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except (yaml.YAMLError, ValueError):  # ValueError: a timestamp PyYAML cannot construct (2026-02-30)
            data = None  # malformed: the line scan below still names its placeholders
        if isinstance(data, dict) and isinstance(data.get("placeholders") or {}, dict):
            return {"placeholders": data.get("placeholders") or {}, "templated_globs": data.get("templated_globs") or []}
    placeholders: dict[str, dict[str, str]] = {}
    globs: list[str] = []
    section = key = None
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if top := re.match(r"^([A-Za-z_]+):", line):
            section = top.group(1)
        elif section == "placeholders" and (m := re.match(r"^  ([A-Za-z0-9_.]+):", line)):
            key = m.group(1)
            placeholders[key] = {}
        elif section == "templated_globs" and (m := re.match(r"^\s*-\s*[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", line)):
            globs.append(m.group(1))
        if section == "placeholders" and key and (d := DEFAULT_VALUE_RE.search(line)):
            placeholders[key]["default"] = _yaml_scalar(d.group(1).strip())
    return {"placeholders": placeholders, "templated_globs": globs}


def _projection(root: Path) -> tuple[set[str], list[re.Pattern[str]]] | None:
    """(declared placeholder keys, templated-glob matchers) from projection.yaml, or None."""
    spec = _projection_spec(root)
    if spec is None:
        return None
    return set(spec["placeholders"]), [_glob_re(g) for g in spec["templated_globs"] or DEFAULT_TEMPLATED_GLOBS]


def _frontmatter_flag(text: str, key: str) -> bool:
    fm = bs.parse_frontmatter(text) or {}
    return str(fm.get(key, "")).strip().lower() in {"true", "yes"}


def _mermaid_blocks(text: str):
    lines, block, start = text.splitlines(), None, 0
    for lineno, line in enumerate(lines, start=1):
        if block is None and re.match(r"^\s*```\s*mermaid\s*$", line):
            block, start = [], lineno
        elif block is not None and re.match(r"^\s*```\s*$", line):
            yield start, "\n".join(block)
            block = None
        elif block is not None:
            block.append(line)


def _mermaid_renderer() -> list[str] | None:
    if shutil.which("mmdc"):
        return ["mmdc"]
    if shutil.which("pnpm"):
        return ["pnpm", "--silent", "--package=@mermaid-js/mermaid-cli", "dlx", "mmdc"]
    return None


def _mermaid_render(mmdc: list[str], source: str) -> tuple[bool, str]:
    """(is a diagram syntax error, detail). Renderer/infra failures (no headless Chrome, timeout)
    are not the author's fault: they come back False and become a NOTICE, never a FAIL."""
    with tempfile.TemporaryDirectory(prefix="brain-hygiene-mmd-") as tmp:
        src, out = Path(tmp) / "in.mmd", Path(tmp) / "out.svg"
        src.write_text(source, "utf-8")
        try:
            proc = subprocess.run([*mmdc, "-q", "-i", str(src), "-o", str(out)],
                                  capture_output=True, text=True, timeout=MERMAID_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"mmdc timed out after {MERMAID_TIMEOUT_S}s"
        if proc.returncode == 0:
            return False, ""
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout).splitlines()
                 if ln.strip() and not ln.strip().startswith("at ")]
        hit = next((i for i, ln in enumerate(lines) if MERMAID_SYNTAX_RE.search(ln)), None)
        if hit is None:
            return False, lines[0] if lines else f"exit {proc.returncode}"
        return True, " | ".join(lines[hit:hit + 4])


def _mermaid_fix(detail: str) -> str:
    """The install command for the renderer failure seen in practice: mermaid-cli's puppeteer wants a
    chrome-headless-shell build that was never downloaded (verified 2026-10-17 on macOS arm64)."""
    if m := re.search(r"chrome-headless-shell \(ver\. ([0-9.]+)\)", detail):
        return (f"; fix: `pnpm dlx @puppeteer/browsers install chrome-headless-shell@{m.group(1)} "
                "--path ~/.cache/puppeteer`")
    return ""


def _ci_slug(label: str) -> str:
    out, dash = [], False
    for ch in unicodedata.normalize("NFD", label.lower()):
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        if cat.startswith("L") or cat == "Nd":
            if dash and out:
                out.append("-")
            dash = False
            out.append(ch)
        else:
            dash = True
    return "".join(out)


def _ci_emoji_base(c: int) -> bool:
    return (c >= 0x1F000 or 0x2190 <= c <= 0x2BFF or 0x2300 <= c <= 0x23FF or c in (0xA9, 0xAE)
            or (unicodedata.category(chr(c)) == "So" and c > 0x2000))


def _ci_split_emoji(s: str) -> tuple[str, str]:
    """Peel one leading emoji grapheme off a heading (Go `splitEmoji`)."""
    if not s or not _ci_emoji_base(ord(s[0])):
        return "", s
    regional = lambda c: 0x1F1E6 <= c <= 0x1F1FF  # noqa: E731
    i = 1
    if regional(ord(s[0])) and i < len(s) and regional(ord(s[i])):
        i += 1
    while i < len(s):
        c = ord(s[i])
        if c in (0xFE0F, 0xFE0E, 0x20E3) or 0x1F3FB <= c <= 0x1F3FF or 0xE0020 <= c <= 0xE007F:
            i += 1
        elif c == 0x200D:
            i += 2
        else:
            return s[:i], s[i:].strip()
    return s[:i], ""


def _ci_truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    if (i := cut.rfind(" ")) > len(cut) // 2:
        cut = cut[:i]
    return cut.rstrip(" ,.;:") + "\u2026"


def _ci_surfaces(lines: list[str], end: int) -> list[str] | None:
    """Top-level `surfaces:` of the frontmatter (inline or block list); None when absent."""
    for i in range(1, end):
        if m := re.match(r"^surfaces\s*:(.*)$", lines[i]):
            value = m.group(1).split("#", 1)[0].strip()
            if not value:
                items = []
                for raw in lines[i + 1:end]:
                    if not (b := re.match(r"^\s+-\s*(.*)$", raw)):
                        break
                    items.append(b.group(1).split("#", 1)[0])
                value = ",".join(items)
            return [v.strip().strip("\"'") for v in value.strip("[]").split(",") if v.strip()]
    return None


def ci_skill_path(entry: str) -> str | None:
    """Run path of one `skills:` entry (Go `SkillPath`, project-sourced); None = host drops it."""
    if CI_SKILL_NAME_RE.fullmatch(entry):
        return f"/brain/skills/{entry}/SKILL.md"
    if not entry.startswith("/") or any(c.isspace() for c in entry):
        return None
    while "//" in entry:  # normalise BEFORE the root/.md checks, in lockstep with the Go SkillPath
        entry = entry.replace("//", "/")
    entry = entry.rstrip("/")
    if (not entry.startswith(CI_SKILL_ROOTS) or ".." in entry.split("/")
            or entry + "/" in CI_SKILL_ROOTS):
        return None
    return entry if entry.endswith(".md") else entry + "/SKILL.md"


def _ci_capped(cat: list[tuple], own: list[tuple], cap: int, dedup: bool
               ) -> tuple[list[str], list[str]]:
    """(effective, dropped own entries): category first, the prompt's own fill the rest."""
    eff = [v for v, *_ in cat][:cap]
    dropped = []
    for v, *_ in own:
        if dedup and v in eff:
            continue
        if len(eff) < cap:
            eff.append(v)
        else:
            dropped.append(v)
    return eff, dropped


def parse_chat_inspiration(text: str, path: str = CHAT_INSPIRATION_FILE, root: Path | None = None
                           ) -> tuple[list[dict], list[bs.Finding]]:
    """One gallery file; see parse_chat_inspiration_set."""
    return parse_chat_inspiration_set([(path, text)], root)


def chat_inspiration_files(root: Path, tracked: set[str] | None = None) -> list[str]:
    """The gallery set in host parse order (Go `chatinspiration.Files`): root `chat_inspiration.md`,
    then `chat_inspiration/*.md` by name, one level (`chat_inspiration/recipes/` is not gallery)."""
    out = [CHAT_INSPIRATION_FILE] if (root / CHAT_INSPIRATION_FILE).is_file() else []
    d = root / CHAT_INSPIRATION_DIR
    if d.is_dir():
        out += [f"{CHAT_INSPIRATION_DIR}/{p.name}" for p in sorted(d.iterdir(), key=lambda p: p.name)
                if p.suffix == ".md" and p.is_file()]
    return [p for p in out if tracked is None or p in tracked]


def is_chat_inspiration_path(rel: str) -> bool:
    if rel == CHAT_INSPIRATION_FILE:
        return True
    name = rel.removeprefix(CHAT_INSPIRATION_DIR + "/")
    return name != rel and "/" not in name and name.endswith(".md") and name != ".md"


def parse_chat_inspiration_set(files: list[tuple[str, str]], root: Path | None = None
                               ) -> tuple[list[dict], list[bs.Finding]]:
    """Parse a brain's gallery set [(rel path, text)] exactly as the host's `ParseFiles` does and judge
    what it would silently drop: one library, a later file's `##` with a known id continues that
    category. Returns (categories, findings); a category is {id, label, emoji, line, path, prompts,
    notes, skills} (its own), a prompt {title, prompt, placeholders, line, path, bold, notes, skills}
    (effective: category first, capped). `root` enables the bare-skill-name existence check.
    ERROR findings block; NOTICE ones advise."""
    findings: list[bs.Finding] = []
    path = files[0][0] if files else CHAT_INSPIRATION_FILE

    def add(message: str, fix: str, line: int | None, severity: str = "ERROR", where: str | None = None
            ) -> None:
        findings.append(bs.Finding("hygiene", f"chat-inspiration: {message}; fix: {fix}",
                                   path=where or path, line=line, severity=severity))

    cats: list[dict] = []
    by_id: dict[str, dict] = {}
    seen_prompts: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    any_skills_key = False

    def note(owner: dict, content: str, lineno: int) -> None:
        nonlocal any_skills_key
        if content.lower().startswith("skills:"):
            any_skills_key = True
            for entry in dict.fromkeys(e.strip() for e in content[7:].split(",") if e.strip()):
                if (rendered := ci_skill_path(entry)) is None:
                    add(f"skill pointer {entry!r} is not a bare skill name or an absolute path under "
                        f"{', '.join(CI_SKILL_ROOTS)}; the host drops it",
                        "write `records` or `/mirrors/<repo>/skills/<name>`", lineno)
                    continue
                if (root is not None and CI_SKILL_NAME_RE.fullmatch(entry)
                        and not (root / "skills" / entry / "SKILL.md").is_file()):
                    add(f"skill {entry!r} has no skills/{entry}/SKILL.md in this brain; the agent "
                        "would be pointed at a missing file", "fix the name or add the skill", lineno)
                if all(r != rendered for r, *_ in owner["_skills"]):
                    owner["_skills"].append((rendered, lineno, path))
            return
        if content.split(maxsplit=1)[0].lower() in CI_SKILL_TYPOS:
            add(f"note starts with {content.split()[0]!r}, read as a note, not a skill pointer",
                "write `skills: a, b`", lineno, "NOTICE")
        if len(content) > CI_MAX_NOTE_RUNES:
            add(f"note is {len(content)} chars (cap {CI_MAX_NOTE_RUNES}), dropped by the host",
                "shorten or split it", lineno)
            return
        owner["_notes"].append((content, lineno, path))

    for path, text in files:
        lines = text.replace("\r\n", "\n").split("\n")
        start = 0
        if lines and lines[0].strip() == "---":
            start = next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        surfaces = _ci_surfaces(lines, start - 1) if start else None
        if surfaces is None:
            add("no frontmatter `surfaces:`, so email runs see the gallery as a doc",
                "start the file with `---` / `surfaces: [chat, dashboard_chat]` / `---`", 1)
        elif "chat" not in surfaces:
            add(f"frontmatter `surfaces: {surfaces}` lacks `chat`",
                "use `surfaces: [chat, dashboard_chat]`", 1)

        cur: dict | None = None
        after_subheading = False  # a non-## heading ended a category: its bullets are ignored
        in_preamble = True  # this `##` section has not had a prompt bullet yet
        target: dict | None = None  # prompt the next indented bullets annotate (None: bullet dropped)
        here: set[str] = set()  # category ids headed in THIS file (a repeat in another is a continuation)
        for lineno, line in enumerate(lines[start:], start=start + 1):
            trimmed = line.strip()
            indented = line[:1] in (" ", "\t")
            is_bullet = not indented and line.startswith(("- ", "* "))
            if trimmed.startswith("## "):
                cur, after_subheading, in_preamble, target = None, False, True, None
                heading = trimmed[3:]
                cid = ""
                if m := CI_ID_SUFFIX_RE.search(heading):
                    cid, heading = m.group(1).lower(), heading[:m.start()]
                emoji, label = _ci_split_emoji(heading.strip())
                cid = cid or _ci_slug(label)
                if not label or not cid:
                    add("heading yields no category (no label/id), its bullets are dropped",
                        "write `## <emoji> Label`", lineno)
                    continue
                if cid in by_id:
                    first = by_id[cid]
                    if cid in here:
                        add(f"duplicate category id {cid!r} (first at line {first['line']}); the host "
                            "merges them", "merge the sections or give one an explicit `{#id}`", lineno)
                    elif (first["label"], first["emoji"]) != (label, emoji):
                        add(f"continuation of category {cid!r} differs from its first heading in "
                            f"{first['path']}; the host shows the first", "copy that heading exactly",
                            lineno, "NOTICE")
                    here.add(cid)
                    cur = by_id[cid]
                    continue
                if len(cats) >= CI_MAX_CATEGORIES:
                    add(f"category {label!r} exceeds the {CI_MAX_CATEGORIES}-category cap and is dropped",
                        "merge categories", lineno)
                    continue
                cur = {"id": cid, "label": label, "emoji": emoji, "line": lineno, "path": path,
                       "prompts": [], "_notes": [], "_skills": []}
                here.add(cid)
                cats.append(cur)
                by_id[cid] = cur
            elif trimmed.startswith("#"):
                after_subheading = cur is not None or after_subheading
                cur = None
            elif is_bullet and cur is None and after_subheading:
                add("bullet under a sub-heading is not a prompt (only `##` sections are categories)",
                    "promote the heading to `##` or drop the bullet", lineno, "NOTICE")
            elif is_bullet and cur is not None:
                body_text = trimmed[2:].strip()
                in_preamble, target = False, None
                if body_text.lower().startswith("skills:"):
                    add("top-level bullet starting `skills:` is shown as a prompt card",
                        "indent it under its prompt (`  - skills: ...`) or move it above the first prompt",
                        lineno)
                if len(cur["prompts"]) >= CI_MAX_PROMPTS:
                    add(f"category {cur['id']!r} exceeds the {CI_MAX_PROMPTS}-prompt cap, bullet dropped",
                        "split the category", lineno)
                    continue
                title, bold = "", False
                if m := CI_BOLD_RE.match(body_text):
                    title = m.group(1).strip().removesuffix(":").strip()
                    bold = bool(title)
                    body = m.group(2).strip()
                    for sep in CI_SEPARATORS:
                        if body.startswith(sep):
                            body = body[len(sep):].strip()
                            break
                else:
                    body = body_text
                if not body:
                    add("bullet yields no prompt (empty text after the title), dropped by the host",
                        "write `- **Title** \u2014 prompt text`", lineno)
                    continue
                if len(body) > CI_MAX_PROMPT_RUNES:
                    add(f"prompt is {len(body)} chars (cap {CI_MAX_PROMPT_RUNES}), dropped by the host",
                        "shorten it", lineno)
                    continue
                if bold and len(title) > CI_MAX_TITLE_RUNES:
                    add(f"title is {len(title)} chars (cap {CI_MAX_TITLE_RUNES}), truncated by the host",
                        "shorten the bold title", lineno)
                if not bold:
                    add("bullet has no bold title; the card shows the prompt cut at ~60 chars",
                        "write `- **Title** \u2014 prompt`", lineno, "NOTICE")
                    title = _ci_truncate(body, CI_DERIVED_TITLE_RUNES)
                title = _ci_truncate(title, CI_MAX_TITLE_RUNES)
                leftover = CI_PLACEHOLDER_RE.sub("", body)
                if "[" in leftover or "]" in leftover:
                    add("unbalanced or invalid `[placeholder]` (nested, unclosed, empty or over 40 chars)",
                        "use one flat `[woord]` per blank", lineno)
                if len(body) > CI_READABLE_PROMPT_RUNES:
                    add(f"prompt is {len(body)} chars; cards read best under {CI_READABLE_PROMPT_RUNES}",
                        "tighten the prompt", lineno, "NOTICE")
                card_id = hashlib.sha256(("project\x00" + cur["id"] + "\x00" + title.lower()).encode()).hexdigest()[:12]
                if card_id in seen_ids:
                    add(f"duplicate card id: same title as {seen_ids[card_id]} in this category; a gallery "
                        "pick of either card loses its notes (the host resolves fail-open)", "rename one title", lineno)
                else:
                    seen_ids[card_id] = f"{path}:{lineno}"
                key = body.lower()
                if key in seen_prompts:
                    add(f"duplicate prompt text (first at {seen_prompts[key]}); the host keeps one",
                        "drop or reword one", lineno, "NOTICE")
                else:
                    seen_prompts[key] = f"{path}:{lineno}"
                placeholders = list(dict.fromkeys(CI_PLACEHOLDER_RE.findall(body)))
                target = {"title": title, "prompt": body, "placeholders": placeholders,
                          "line": lineno, "path": path, "bold": bold, "_notes": [], "_skills": []}
                cur["prompts"].append(target)
            elif cur is None or not trimmed or trimmed.startswith("#"):
                continue
            elif indented and trimmed.startswith(("- ", "* ")):
                if in_preamble:
                    add("indented bullet before the category's first prompt is ignored by the host",
                        "un-indent it (category note) or move it under a prompt", lineno, "NOTICE")
                elif target is not None:
                    note(target, trimmed[2:].strip(), lineno)
            elif in_preamble:
                note(cur, trimmed, lineno)
            else:
                add("plain line after the category's first prompt is ignored by the host",
                    "keep a prompt on one line; category notes go above the first prompt; prompt notes "
                    "are indented bullets", lineno)
    for c in cats:
        for owner, kind in [(c, "category preamble")] + [(p, f"prompt {p['title']!r}") for p in c["prompts"]]:
            if (n := sum(len(v) for v, *_ in owner["_notes"])) > CI_LONG_NOTES_RUNES:
                add(f"{kind} carries {n} chars of inline notes (over {CI_LONG_NOTES_RUNES}): a recipe, not a "
                    "directive", "move the steps into a .md (e.g. chat_inspiration/recipes/<slug>.md) and "
                    "point at it with `skills: /brain/chat_inspiration/recipes/<slug>.md`; keep short "
                    "directives inline", owner["line"], "NOTICE", owner["path"])
        for key, cap, what in (("_notes", CI_MAX_NOTES, "notes"), ("_skills", CI_MAX_SKILLS, "skills")):
            own = c.pop(key)
            c[what] = [v for v, *_ in own]
            if len(own) > cap:
                add(f"category {c['id']!r} has {len(own)} {what} (cap {cap}); the host drops "
                    f"{[v for v, *_ in own[cap:]]}", f"keep at most {cap}", own[cap][1], where=own[cap][2])
            for p in c["prompts"]:
                p[what], dropped = _ci_capped(own, p.pop(key), cap, what == "skills")
                if dropped:
                    add(f"prompt {p['title']!r} exceeds {cap} effective {what} (category first); the "
                        f"host drops {dropped}", f"keep category + prompt {what} at most {cap}",
                        p["line"], where=p["path"])
        if any_skills_key:
            for p in c["prompts"]:
                if not p["skills"]:
                    add(f"prompt {p['title']!r} has no skill pointer; the agent gets no how-to",
                        "add `  - skills: <name>` under it or `skills:` above the category's "
                        "first prompt", p["line"], "NOTICE", p["path"])
        if not c["prompts"]:
            add(f"category {c['id']!r} has no prompts; the gallery hides it",
                "add `- **Title** \u2014 prompt` bullets or drop the heading", c["line"], "NOTICE", c["path"])
    if not any(c["prompts"] for c in cats):
        add("no prompt parsed; the gallery would be empty",
            "add `## <emoji> Category` sections with `- **Title** \u2014 prompt` bullets", None)
    return cats, findings


def _action_size_notices(text: str, rel: str) -> list[bs.Finding]:
    """`action-size`: whitespace-collapsed lengths, as the host renders them into the action tool."""
    try:
        import yaml
    except ModuleNotFoundError:
        return []
    try:
        manifest = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError):
        return []  # a broken manifest is brain_lint's finding, not this notice's
    if not isinstance(manifest, dict):
        return []
    out = []
    size = lambda v: len(" ".join(str(v or "").split()))
    if (n := size(manifest.get("description"))) > ACTION_DESCRIPTION_NOTICE:
        out.append(bs.Finding("hygiene", f"action-size: description is {n} chars (aim ≤ {ACTION_DESCRIPTION_NOTICE}); "
                              "it rides every model turn, so keep decision text and move internals to "
                              "the script.py docstring (docs/actions.md)", path=rel, severity="NOTICE"))
    for p in manifest.get("params") or []:
        if isinstance(p, dict) and (n := size(p.get("description"))) > ACTION_PARAM_DESCRIPTION_NOTICE:
            out.append(bs.Finding("hygiene", f"action-size: param `{p.get('name')}` description is {n} chars "
                                  f"(aim ≤ {ACTION_PARAM_DESCRIPTION_NOTICE})", path=rel, severity="NOTICE"))
    return out


def _frontmatter_list(doc, text: str, key: str) -> list[str]:
    """A top-level frontmatter list, flow or block form: `brain_doc.include_in`'s line scan (the host's
    treeview.frontmatterList) for any key."""
    block = doc.frontmatter_lines(text) or []
    strip = lambda s: re.sub(r"(^|\s)#.*$", "", s.rstrip("\r")).strip()
    for i, raw in enumerate(block):
        if not raw.startswith(f"{key}:"):
            continue
        if rest := strip(raw[len(key) + 1:]):
            items = rest.strip("[]").split(",")
        else:
            items = []
            for item in map(strip, block[i + 1:]):
                if not item:
                    continue
                if not item.startswith("- "):
                    break
                items.append(item[2:])
        return [v for v in (doc.unquote(p.strip()) for p in items) if v]
    return []


def _startup_size_notices(root: Path, visible: set[str], files: list[str] | None) -> list[bs.Finding]:
    """`startup-size`: what the host pastes into EVERY main-loop run, judged as a set because the cost
    is the sum. Whole-file chars of AGENTS.md (pasted whole) and each `agent`-tagged doc. A doc limited by
    `surfaces:` is pasted only on those surfaces, so it gets the per-file budget but stays out of the sum."""
    doc = load_brain_doc()
    if doc is None:
        return []
    kind = doc.detect_repo_kind(root)[0]
    sizes, limited = {}, set()
    for rel in sorted(p for p in visible if p.endswith(".md")):
        text = (root / rel).read_text("utf-8", errors="replace")
        if (rel == "AGENTS.md" and kind != "mirror") or \
                "agent" in doc.include_in(text) - doc.unread_roles(rel, kind):
            sizes[rel] = len(text)
            if _frontmatter_list(doc, text, "surfaces"):
                limited.add(rel)
    if not sizes or (files is not None and not set(files) & sizes.keys()):
        return []
    out = []
    hint = "it is pasted into every run: keep rules most runs need, route case detail via `description:`"
    for rel, n in sizes.items():
        cap = STARTUP_AGENTS_MD_NOTICE if rel == "AGENTS.md" else STARTUP_DOC_NOTICE
        if n > cap:
            out.append(bs.Finding("hygiene", f"startup-size: {n} chars (aim ≤ {cap}); {hint} "
                                  "(docs/brain-model.md §Startup context budget)", path=rel, severity="NOTICE"))
    every = {rel: n for rel, n in sizes.items() if rel not in limited}
    if (total := sum(every.values())) > STARTUP_TOTAL_NOTICE:
        parts = ", ".join(f"{rel} {n}" for rel, n in sorted(every.items(), key=lambda kv: -kv[1]))
        out.append(bs.Finding("hygiene", f"startup-size: hard-loaded set is {total} chars (aim ≤ "
                              f"{STARTUP_TOTAL_NOTICE}): {parts}", path="AGENTS.md", severity="NOTICE"))
    return out


def _triage_case_findings(root: Path) -> list[bs.Finding]:
    """`triage-cases`: the whole corpus, as the host parses it (it refuses all cases if one is bad).
    PyYAML is optional for this stdlib-only gate: without it the linter re-runs under `uv`."""
    try:
        import yaml  # noqa: F401
        raw = triage_cases.lint(root)
    except ModuleNotFoundError:
        uv = shutil.which("uv")
        if uv is None:
            return [bs.Finding("hygiene", "triage-cases: skipped, needs PyYAML or uv on PATH",
                               path=triage_cases.CASES_DIR, severity="NOTICE")]
        offline = ["--offline", "--no-python-downloads"] if _SNAPSHOT else []  # a git gate never downloads
        try:
            proc = subprocess.run([uv, "run", *offline, "--no-project", "--with", "pyyaml", "python",
                                   str(Path(triage_cases.__file__).resolve()), "--brain", str(root), "--json"],
                                  capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
                                  timeout=LINT_TIMEOUT_S if _SNAPSHOT else None)
        except subprocess.TimeoutExpired:
            proc = None
        if _SNAPSHOT and (proc is None or proc.returncode not in (0, 1)):
            return [bs.Finding("hygiene", "triage-cases: skipped, uv could not run the linter offline (run "
                               "brain_hygiene.py once to cache PyYAML)", path=triage_cases.CASES_DIR,
                               severity="NOTICE")]
        try:
            raw = json.loads(proc.stdout)  # exit 1 is a lint FAIL with findings; anything else is a crash
            if proc.returncode not in (0, 1) or not isinstance(raw, list):
                raise ValueError
        except ValueError:
            return [bs.Finding("hygiene", "triage-cases: linter did not run (fix uv/PyYAML): "
                                          f"{proc.stderr.strip()[-200:]}", path=triage_cases.CASES_DIR)]
    return [bs.Finding("hygiene", f"triage-cases: {f['message']}", path=f["path"],
                       severity="NOTICE" if f["level"] == "NOTICE" else "ERROR") for f in raw]


def check(root: Path, files: list[str] | None = None, base: str | None = None, *,
          mermaid: bool | set[str] = True, cross_file: bool = False) -> list[bs.Finding]:
    """Hygiene findings for a brain at `root`. `files` scopes every rule but `conflict`;
    None judges the whole tracked tree. `base` (the ref to compare against) enables doc-spill.
    `mermaid`: False skips diagrams, a set skips blocks whose source it holds (unchanged since the
    base). `cross_file`: dead links anywhere in the tree plus `reachability` (the push gate)."""
    tracked = bs.git_tracked(root)
    ctx = bs.Ctx(root=root, tracked=tracked, tracked_set=set(tracked),
                 md_files=[p for p in tracked if p.lower().endswith(".md")])
    hidden = bs.hidden_paths(ctx)
    text_files = [p for p in tracked
                  if (root / p).is_file() and Path(p).suffix.lower() in TEXT_SUFFIXES]
    visible = {p for p in text_files if p not in hidden and not p.startswith(TOOLING_DIRS)}
    scoped = visible if files is None else set(files) & visible
    projection = _projection(root)
    # a projection.yaml edit can orphan a `{{ key }}` in any templated doc, touched or not
    templated_scope = visible if files is None or "projection.yaml" in files else scoped
    mmdc = _mermaid_renderer() if mermaid is not False else None
    findings: list[bs.Finding] = []
    mermaid_seen, renderer_failure = False, ""

    def add(rule: str, message: str, rel: str, line: int | None = None) -> None:
        findings.append(bs.Finding("hygiene", f"{rule}: {message}", path=rel, line=line))

    gallery = chat_inspiration_files(root, set(visible))
    # ids and caps span the split set: judge it whole when a part changed, was deleted or moved out
    if gallery and (files is None or any(is_chat_inspiration_path(rel) for rel in
                                         set(files) | _deleted_since(root, base))):
        findings.extend(parse_chat_inspiration_set(
            [(rel, (root / rel).read_text("utf-8", errors="replace")) for rel in gallery], root)[1])
    cases = triage_cases.case_files(root)
    if cases and (files is None or any(rel.startswith(triage_cases.CASES_DIR + "/") for rel in
                                       set(files) | _deleted_since(root, base))):
        findings.extend(_triage_case_findings(root))
    for rel in text_files:
        text = (root / rel).read_text("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if CONFLICT_RE.match(line):
                add("conflict", f"git conflict marker {line[:7]!r}", rel, lineno)
        if (rel.endswith(".md") and rel in templated_scope and projection is not None
                and any(g.match(rel) for g in projection[1])):
            for lineno, line in enumerate(text.splitlines(), start=1):
                for key in PLACEHOLDER_RE.findall(line):
                    if key not in projection[0]:
                        add("placeholder", f"{{{{ {key} }}}} is not declared in projection.yaml "
                                           "placeholders", rel, lineno)
                if "{{" in (rest := PLACEHOLDER_RE.sub("", line)) or "}}" in rest:
                    add("placeholder", "residual `{{`/`}}` in a projection-templated file", rel, lineno)
        if rel not in scoped:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if m := ABS_PATH_RE.search(line):
                add("abs-path", f"absolute laptop path {m.group(0)!r}", rel, lineno)
        if ACTION_README_RE.match(rel):
            add("action-readme", "an action README sits in every run's tree but only restates the "
                                 "manifest (the action tool renders it every turn); move internals to the "
                                 "script.py docstring, run guidance to a described PLAYBOOK.md, then delete it",
                rel)
        if rel.endswith(".md"):
            for lineno, line in enumerate(bs.code_free_lines(text), start=1):
                if m := TOOL_MARKUP_RE.search(line):
                    add("tool-markup", f"leaked tool-call markup {m.group(0)!r} a run reads verbatim; "
                                       "delete it", rel, lineno)
            desc = (bs.parse_frontmatter(text) or {}).get("description", "").strip().strip("\"'")
            if rel.startswith("skills/") and len(desc) > DESCRIPTION_MAX:
                add("description", f"frontmatter description is {len(desc)} chars (max "
                                   f"{DESCRIPTION_MAX}; publish rejects it)", rel, 1)
            if _frontmatter_flag(text, "customer_facing"):
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if EM_DASH in line:
                        add("em-dash", "em dash in customer-facing copy", rel, lineno)
            for lineno, source in _mermaid_blocks(text):
                if mermaid is False or (isinstance(mermaid, set) and source in mermaid):
                    continue
                mermaid_seen = True
                if not mmdc:
                    continue
                syntax, detail = _mermaid_render(mmdc, source)
                if syntax:
                    add("mermaid", f"block does not parse: {detail}", rel, lineno)
                elif detail:
                    renderer_failure = detail
        elif rel.startswith("actions/") and rel.endswith(("/manifest.yaml", "/manifest.yml")):
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (m := MANIFEST_COPY_RE.match(line)) and EM_DASH in m.group(2):
                    add("em-dash", f"em dash in customer-facing `{m.group(1)}`", rel, lineno)
            findings += _action_size_notices(text, rel)
        elif rel == "projection.yaml":
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (m := DEFAULT_VALUE_RE.search(line)) and EM_DASH in m.group(1):
                    add("em-dash", "em dash in a placeholder default (lands in customer replies)",
                        rel, lineno)

    findings += _startup_size_notices(root, visible, files)
    lay = brain_paths.layout(root)
    refs = brain_paths.extract(root, brain_paths.scan_files(root, sorted(scoped)))
    brain_paths.resolve_local(lay, refs)
    for ref in refs.values():
        if ref.verdict == "missing":
            for rel, lineno in ref.sites:
                findings.append(bs.Finding("hygiene", f"paths: {ref.path} is not shipped by this "
                                           f"checkout ({lay.own}); absent, gitignored or run-hidden",
                                           path=rel, line=lineno, severity="NOTICE"))
    for f in bs.check_links(ctx):
        if cross_file or f.path in scoped:  # cross_file: a deleted target strands links in untouched files
            findings.append(bs.Finding("hygiene", f"links: {f.message}", path=f.path, line=f.line))
    if cross_file:
        findings += [bs.Finding("hygiene", f"reachability: {f.message}", path=f.path)
                     for f in bs.check_reachability(ctx)]
    if mermaid_seen and not mmdc:
        findings.append(bs.Finding("hygiene", "mermaid: skipped, no renderer (`mmdc` or `pnpm`) on "
                                   "PATH; diagrams unchecked; install pnpm (`mise use -g pnpm`), the "
                                   "next check fetches @mermaid-js/mermaid-cli itself",
                                   severity="NOTICE"))
    if renderer_failure:
        findings.append(bs.Finding("hygiene", f"mermaid: renderer failed, diagrams unchecked: "
                                   f"{renderer_failure}{_mermaid_fix(renderer_failure)}", severity="NOTICE"))
    if base:
        findings += _spill_crossings(root, sorted(scoped), base)
    return findings


class Snapshot:
    """A commit's tree (or the index) as a temp checkout that git commands in it treat as `root`'s
    repository at those bytes (`active()`): the gates judge exactly what is committed or pushed, never
    the working tree (partial staging, a dirty tree, unsaved edits). Text files carry real bytes; any
    other tracked file is an empty stand-in, since rules only test that it exists. The view sits two
    empty directories deep and keeps the checkout's name, so sibling scans (mirror detection,
    `brain_paths`) find nothing beside it."""

    def __init__(self, root: Path, commit: str | None, tmp: Path):
        env = {k: v for k, v in os.environ.items() if k not in ("GIT_WORK_TREE", "GIT_INDEX_FILE")}
        git_dir = _git(root, "rev-parse", "--absolute-git-dir").stdout.strip()
        index = tmp / "index"
        if commit is None:  # the index this commit writes: git exports GIT_INDEX_FILE to the hook
            src = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip())
            if src.is_file():
                shutil.copyfile(src, index)
        self.path = tmp / "v" / "v" / root.name
        self.path.mkdir(parents=True)
        self.env = {"GIT_DIR": git_dir, "GIT_WORK_TREE": str(self.path), "GIT_INDEX_FILE": str(index),
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_LFS_SKIP_SMUDGE": "1"}

        def git(*args: str, data: bytes | None = None) -> bytes:
            return subprocess.run(["git", "-C", str(self.path), *args], input=data, capture_output=True,
                                  check=True, env={**env, **self.env}).stdout

        if commit is not None:
            git("read-tree", commit)
        real, stand_ins = [], []
        for entry in git("ls-files", "-s", "-z").split(b"\0"):
            meta, _, rel = entry.partition(b"\t")
            if not rel or meta.startswith(b"160000"):  # a submodule has no bytes here
                continue
            name = rel.rsplit(b"/", 1)[-1].decode("utf-8", "replace")
            text = Path(name).suffix.lower() in TEXT_SUFFIXES or name.startswith(".")
            (real if text or meta.startswith(b"120000") else stand_ins).append(rel)
        if real:
            git("checkout-index", "-f", "-z", "--stdin", f"--prefix={self.path}/", data=b"\0".join(real))
        for rel in stand_ins:
            dest = self.path / os.fsdecode(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.touch()

    @contextlib.contextmanager
    def active(self):
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        _SNAPSHOT.append(self.path)
        try:
            yield self.path
        finally:
            _SNAPSHOT.pop()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@contextlib.contextmanager
def _snapshot(root: Path, commit: str | None):
    with tempfile.TemporaryDirectory(prefix="brain-gate-") as tmp:
        yield Snapshot(root, commit, Path(tmp).resolve())


def _lint_start(view: Path, kind: str) -> subprocess.Popen | None:
    """Start the publish lint (`lib.brain_lint`, needs PyYAML) on `view` in its own interpreter, so it
    runs beside the hygiene pass: this one when it has PyYAML, else `uv run --with pyyaml`. None when
    neither can run it. `_lint_fails` collects its FAIL findings."""
    runtime = BRAIN_DOC.parents[1]
    if not (runtime / "lib" / "brain_lint.py").is_file():
        return None
    if importlib.util.find_spec("yaml") is not None:
        py = [sys.executable]
    elif uv := shutil.which("uv"):
        py = [uv, "run", "-q", "--offline", "--no-project", "--with", "pyyaml", "python"]  # gates: no network
    else:
        return None
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from lib.brain_lint import lint_brain; "
            "print(json.dumps([(f.rule, f.path, f.message) for f in lint_brain(sys.argv[3], sys.argv[2]) "
            "if f.level == 'FAIL']))")
    # cwd=runtime: `-c` puts the cwd first on sys.path, and a brain's own files must not shadow lib/json
    return subprocess.Popen([*py, "-c", code, str(runtime), kind, str(view)], cwd=runtime, text=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _lint_fails(proc: subprocess.Popen | None) -> list[bs.Finding] | None:
    if proc is None:
        return None
    try:
        raw = json.loads(proc.communicate(timeout=LINT_TIMEOUT_S)[0])
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    except ValueError:
        return None
    out = []
    for rule, path, message in raw:
        rel, _, line = path.partition(":")
        out.append(bs.Finding("lint", f"{rule}: {message}", path=rel, line=int(line) if line.isdigit() else None))
    return out


# Measurements in a message (sizes, counts, `line N` / `path:N` back-references) move with unrelated
# edits; every other digit is identity (a link target `missing2.md`, a placeholder key) and stays.
MEASURE_RE = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?=\s*(?:bytes?|chars?|[KM]i?B|%|lines?|notes|skills|prompts|categories)\b|→)"
                        r"|(?<=→)\d+|(?<=line )\d+|(?<=\.md:)\d+")


def _key(f: bs.Finding, moved: dict[str, str] | None = None) -> tuple[str, str | None, str]:
    """Baseline identity, measurements masked (MEASURE_RE); a base finding follows its file through a
    rename (`moved`: base path -> new path)."""
    return f.check, (moved or {}).get(f.path, f.path), MEASURE_RE.sub("#", f.message)


def _resolves(root: Path, ref: str | None) -> bool:
    return bool(ref) and _git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode == 0


def changed_files(root: Path, base: str | None, commit: str | None) -> list[str]:
    """Paths `commit` (None: the index) adds or modifies vs `base`; every path when there is no base."""
    if base is None:
        args = ["ls-files", "-z"] if commit is None else ["ls-tree", "-r", "-z", "--name-only", commit]
    else:
        args = ["diff", "--name-only", "-z", "--diff-filter=ACMR", *(["--cached", base] if commit is None
                                                                     else [base, commit])]
    return [p for p in _git(root, *args).stdout.split("\0") if p]


def gate(root: Path, base: str | None, commit: str | None = None, *, mermaid: bool = True,
         cross_file: bool = False) -> list[bs.Finding]:
    """What `commit` (None: the index) introduces over `base`: hygiene + lint FAILs on the committed
    bytes, minus every finding the base already has (legacy debt never blocks) except NEVER_BASELINED.
    No resolvable base (first commit, nothing to compare): the whole tree is judged as is."""
    base = base if _resolves(root, base) else None
    files = changed_files(root, base, commit)
    moved = _renames(root, "diff", *(["--cached", base] if commit is None else [base, commit])) if base else {}
    origin = {new: old for old, new in moved.items()}
    old_files = [origin.get(rel, rel) for rel in files]  # a renamed file keeps its baseline
    kind = brain_paths.layout(root).kind
    with contextlib.ExitStack() as stack:
        new = stack.enter_context(_snapshot(root, commit))
        old = stack.enter_context(_snapshot(root, base)) if base else None
        lints = [_lint_start(new.path, kind), *([_lint_start(old.path, kind)] if old else [])]
        known: set[str] | None = None
        prior: list[bs.Finding] = []
        if old:
            known = {src for rel in old_files if (old.path / rel).is_file() and rel.endswith(".md")
                     for _, src in _mermaid_blocks((old.path / rel).read_text("utf-8", errors="replace"))}
            with old.active() as view:
                prior = check(view, old_files, None, mermaid=False, cross_file=cross_file)
        with new.active() as view:
            found = check(view, files, base, mermaid=(known or True) if mermaid else False,
                          cross_file=cross_file)
        lint = [_lint_fails(proc) for proc in lints]  # before the views are removed
    if None in lint:  # half a baseline would turn legacy debt into blockers
        found.append(bs.Finding("hygiene", "lint: skipped (needs PyYAML, or uv with pyyaml cached: run "
                                "brain_lint.py once; or it timed out)", severity="NOTICE"))
    else:
        scope = None if cross_file else set(files)
        found += [f for f in lint[0] if scope is None or f.path in scope]
        prior += lint[1] if old else []
    seen = {_key(f, moved) for f in prior}
    return [f for f in found if f.message.startswith(NEVER_BASELINED) or _key(f) not in seen]


def _pushed(root: Path, lines: list[str]) -> list[tuple[str, str, str | None]]:
    """(ref, local sha, base) per pre-push stdin line worth judging: deletes, tags and no-op pushes
    skip; a new branch (or a remote tip this clone lacks) compares against origin/main."""
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local, remote_ref, remote = parts  # `git push origin HEAD:main`: local ref HEAD
        if ZERO_SHA.fullmatch(local) or local == remote or not remote_ref.startswith("refs/heads/"):
            continue
        base = remote if not ZERO_SHA.fullmatch(remote) and _resolves(root, remote) else "origin/main"
        out.append((remote_ref.removeprefix("refs/heads/"), local, base))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_hygiene.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="brain checkout root (default: cwd's git toplevel)")
    p.add_argument("--base", default="origin/main", help="scope rules to files changed vs this ref")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="judge every tracked file")
    mode.add_argument("--staged", action="store_true",
                      help="judge the index, regressions vs HEAD (the managed pre-commit hook)")
    mode.add_argument("--push", action="store_true",
                      help="judge each pushed commit vs the remote tip; pre-push refs on stdin "
                           "(the managed pre-push hook)")
    args = p.parse_args(argv)
    try:
        root = bs.git_toplevel(Path(args.root).resolve() if args.root else Path.cwd())
    except bs.StructureError as exc:
        print(f"brain-hygiene: {exc}", file=sys.stderr)
        return 2
    if args.staged or args.push:
        return _hook(root, args.push)
    files = None if args.all else bs.git_changed_paths(root, args.base)
    findings = check(root, files, args.base)
    scope = "all" if files is None else f"{len(files)} changed file(s) vs {args.base}"
    return _report(findings, f"{scope}; conflict markers: whole tree")


def _report(findings: list[bs.Finding], scope: str, label: str = "brain hygiene") -> int:
    for f in findings:
        print(f.render())
    blocking = [f for f in findings if f.severity != "NOTICE"]
    print(f"{label}: {len(blocking)} FAIL ({scope})")
    return 1 if blocking else 0


def _hook(root: Path, push: bool) -> int:
    """The managed git hooks. A crash in the gate itself fails open (loudly): a gate bug must not stop
    every commit in the fleet, nor teach agents `--no-verify`."""
    if not push:  # the pre-commit hook is the one kit path every brain commit runs (no network on push)
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        with contextlib.suppress(ImportError):
            import kit_update
            kit_update.maybe_update(root)
    label = "brain push gate" if push else "brain hygiene"
    bypass = "git push --no-verify" if push else "git commit --no-verify"
    try:
        if not push:
            findings, scope = gate(root, "HEAD"), "staged vs HEAD"
        else:
            findings, refs = [], _pushed(root, sys.stdin.read().splitlines())
            for _ref, sha, base in dict.fromkeys(refs):
                findings += [f for f in gate(root, base, sha, mermaid=False, cross_file=True)
                             if f not in findings]
            scope = ", ".join(f"{ref} vs {base if base == 'origin/main' else base[:9]}"
                              for ref, _sha, base in refs) or "nothing to judge"
    except Exception as exc:  # noqa: BLE001 — see docstring
        print(f"{label}: ⚠ gate crashed, skipped ({type(exc).__name__}: {exc}); report it to the kit "
              "maintainers", file=sys.stderr)
        return 0
    code = _report(findings, f"{scope}; regressions only, conflict markers + laptop paths always", label)
    if code:
        print(f"{label}: fix the FAIL lines above (legacy debt already at the base never blocks); bypass "
              f"once: {bypass}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
