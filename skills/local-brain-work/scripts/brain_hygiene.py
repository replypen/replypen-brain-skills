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
                   residual `{{`/`}}` (rootcause's projection fails the whole run on both).
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
  * chat-inspiration — root `chat_inspiration.md` (chat example-prompt gallery), parsed exactly as the
                   host does (`parse_chat_inspiration`), whose parser drops bad lines silently. FAIL:
                   no prompt at all, a bullet in a category that yields no prompt, a cap exceeded
                   (20 categories, 50 prompts/category, 500-char prompt, 80-char title), a heading
                   that yields no category, a duplicate category id, an unbalanced `[placeholder]`,
                   frontmatter `surfaces:` missing or without `chat`, a plain line after a
                   category's first prompt, an invalid/missing `skills:` entry, a note/skill cap
                   (300-char note, 8 notes, 6 skills effective), a top-level `- skills:` bullet.
                   NOTICE (non-blocking): prompt over 160 chars, no bold title, empty category,
                   duplicate prompt text, bullets swallowed by a sub-heading, an indented bullet
                   before a category's first prompt, a `skill:` typo, a prompt without any skill
                   pointer (only once the file uses `skills:`).
  * doc-spill    — NOTICE only: an on-demand doc (no `include_in`, not root AGENTS.md /
                   chat_inspiration.md, not under journal/_internal/tests/docs/dot-dirs) whose body
                   crosses 6000 bytes in this change (≤6000 or absent at the base: HEAD for --staged,
                   else `--base`); bash shows such a read as a 2000+1000-char preview. Already-over
                   docs stay silent.

Everything but `conflict` is scoped to changed files (vs `--base`, default origin/main) so a mature
tree's legacy debt cannot block an unrelated publish; `--all` judges every tracked file.

    uv run --no-project python brain_hygiene.py            # changed files vs origin/main
    uv run --no-project python brain_hygiene.py --all      # whole tree
    uv run --no-project python brain_hygiene.py --staged   # staged files (managed pre-commit hook)

`install.sh` installs a managed `pre-commit` hook running `--staged`, so a broken diagram or marker
fails at commit time, not at push.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_structure as bs  # noqa: E402

CONFLICT_RE = re.compile(r"^(<{7}|\|{7}|>{7})(?: |$)")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")  # rootcause internal/projection
ABS_PATH_RE = re.compile(r"/Users/[A-Za-z0-9._-]+")
MANIFEST_COPY_RE = re.compile(r"^\s*(display_name|customer_[a-z_]+)\s*:(.*)$")
DEFAULT_VALUE_RE = re.compile(r"\bdefault:\s*(\"[^\"]*\"|'[^']*'|[^#,}]*)")
DEFAULT_TEMPLATED_GLOBS = ["**/*.md"]  # rootcause internal/projection DefaultTemplatedGlobs
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".toml", ".sh", ".rb", ".txt", ".sql"}
EM_DASH = "\u2014"
TOOLING_DIRS = (".agents/", ".claude/", ".github/")
MERMAID_TIMEOUT_S = 30
DESCRIPTION_MAX = 1024  # agentskills.io limit; lib.brain_lint DESC_FULL_MAX_LEN (publish lint)
MERMAID_SYNTAX_RE = re.compile(r"(Parse|Lexical|Syntax) error|No diagram type detected", re.I)

# Mirror of rootcause internal/chatinspiration (library.go): keep caps, regexes and separators in sync.
CHAT_INSPIRATION_FILE = "chat_inspiration.md"
CI_MAX_CATEGORIES, CI_MAX_PROMPTS, CI_MAX_PROMPT_RUNES, CI_MAX_TITLE_RUNES = 20, 50, 500, 80
CI_DERIVED_TITLE_RUNES = 60
CI_READABLE_PROMPT_RUNES = 160  # card readability, not a host cap
CI_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{1,40}\]")
CI_ID_SUFFIX_RE = re.compile(r"\s*\{#([A-Za-z0-9_-]{1,64})\}\s*\Z", re.ASCII)
CI_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$", re.DOTALL)
CI_SEPARATORS = ("\u2014", "\u2013", "-", ":")
CI_MAX_NOTE_RUNES, CI_MAX_NOTES, CI_MAX_SKILLS = 300, 8, 6  # per note line; effective per prompt
CI_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
CI_SKILL_ROOTS = ("/brain/", "/tenant/", "/skills/", "/mirrors/")
CI_SKILL_TYPOS = ("skill:", "skils:", "skillz:")

SPILL_BYTES = 6000  # rootcause internal/tool/tool.go defaultBashSpillThreshold (Go len = bytes)
SPILL_SKIP_DIRS = {"journal", "_internal", "tests", "docs"}
BRAIN_DOC = Path(__file__).resolve().parents[3] / "runtime" / "lib" / "brain_doc.py"


def _brain_doc():
    """The kit's stdlib-only `lib.brain_doc`, loaded by path: the hook and push gate run without
    PyYAML, and a brain's own `lib` package must not shadow it. None when the kit ships no runtime/."""
    spec = importlib.util.spec_from_file_location("rc_kit_brain_doc", BRAIN_DOC)
    if spec is None or not BRAIN_DOC.is_file():
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spill_crossings(root: Path, files: list[str], base: str) -> list[bs.Finding]:
    """NOTICE per on-demand doc this change pushes past the bash spill cliff (regression-only)."""
    doc = _brain_doc()
    if doc is None:
        return []

    def body(text: str) -> int:
        return len(doc.strip_frontmatter(text).encode("utf-8"))

    out: list[bs.Finding] = []
    for rel in sorted(files):
        parts = rel.split("/")
        if (not rel.endswith(".md") or rel in ("AGENTS.md", CHAT_INSPIRATION_FILE)
                or any(d in SPILL_SKIP_DIRS or d.startswith(".") for d in parts[:-1])):
            continue
        text = (root / rel).read_text("utf-8", errors="replace")
        if doc.include_in(text) or (new := body(text)) <= SPILL_BYTES:
            continue
        prev = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        old = body(prev.stdout) if prev.returncode == 0 else None
        if old is not None and old > SPILL_BYTES:
            continue
        out.append(bs.Finding("hygiene", f"doc-spill: body {old if old is not None else 'new'}→{new} "
                              "bytes; bash shows only a 2000+1000-char preview past 6000 chars: split "
                              "into a lean core + detail file", path=rel, severity="NOTICE"))
    return out


def _glob_re(glob: str) -> re.Pattern[str]:
    out = ""
    for part in re.split(r"(\*\*/|\*\*|\*|\?)", glob):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + r"\Z")


def _projection(root: Path) -> tuple[set[str], list[re.Pattern[str]]] | None:
    """(declared placeholder keys, templated-glob matchers) from projection.yaml, or None."""
    spec = root / "projection.yaml"
    if not spec.is_file():
        return None
    keys: set[str] = set()
    globs: list[str] = []
    section = None
    for line in spec.read_text("utf-8", errors="replace").splitlines():
        if top := re.match(r"^([A-Za-z_]+):", line):
            section = top.group(1)
        elif section == "placeholders" and (m := re.match(r"^  ([A-Za-z0-9_.]+):", line)):
            keys.add(m.group(1))
        elif section == "templated_globs" and (m := re.match(r"^\s*-\s*[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", line)):
            globs.append(m.group(1))
    return keys, [_glob_re(g) for g in globs or DEFAULT_TEMPLATED_GLOBS]


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


def _ci_capped(cat: list[tuple[str, int]], own: list[tuple[str, int]], cap: int, dedup: bool
               ) -> tuple[list[str], list[str]]:
    """(effective, dropped own entries): category first, the prompt's own fill the rest."""
    eff = [v for v, _ in cat][:cap]
    dropped = []
    for v, _ in own:
        if dedup and v in eff:
            continue
        if len(eff) < cap:
            eff.append(v)
        else:
            dropped.append(v)
    return eff, dropped


def parse_chat_inspiration(text: str, path: str = CHAT_INSPIRATION_FILE, root: Path | None = None
                           ) -> tuple[list[dict], list[bs.Finding]]:
    """Parse a brain's `chat_inspiration.md` exactly as the host does and judge what it would
    silently drop. Returns (categories, findings); a category is {id, label, emoji, line, prompts,
    notes, skills} (its own), a prompt {title, prompt, placeholders, line, bold, notes, skills}
    (effective: category first, capped). `root` enables the bare-skill-name existence check.
    ERROR findings block; NOTICE ones advise."""
    findings: list[bs.Finding] = []

    def add(message: str, fix: str, line: int | None, severity: str = "ERROR") -> None:
        findings.append(bs.Finding("hygiene", f"chat-inspiration: {message}; fix: {fix}",
                                   path=path, line=line, severity=severity))

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

    cats: list[dict] = []
    by_id: dict[str, dict] = {}
    cur: dict | None = None
    after_subheading = False  # a non-## heading ended a category: its bullets are ignored
    seen_prompts: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    in_preamble = True  # this `##` section has not had a prompt bullet yet
    target: dict | None = None  # prompt the next indented bullets annotate (None: bullet dropped)
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
                if all(r != rendered for r, _ in owner["_skills"]):
                    owner["_skills"].append((rendered, lineno))
            return
        if content.split(maxsplit=1)[0].lower() in CI_SKILL_TYPOS:
            add(f"note starts with {content.split()[0]!r}, read as a note, not a skill pointer",
                "write `skills: a, b`", lineno, "NOTICE")
        if len(content) > CI_MAX_NOTE_RUNES:
            add(f"note is {len(content)} chars (cap {CI_MAX_NOTE_RUNES}), dropped by the host",
                "shorten or split it", lineno)
            return
        owner["_notes"].append((content, lineno))

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
                add(f"duplicate category id {cid!r} (first at line {by_id[cid]['line']}); the host "
                    "merges them", "merge the sections or give one an explicit `{#id}`", lineno)
                cur = by_id[cid]
                continue
            if len(cats) >= CI_MAX_CATEGORIES:
                add(f"category {label!r} exceeds the {CI_MAX_CATEGORIES}-category cap and is dropped",
                    "merge categories", lineno)
                continue
            cur = {"id": cid, "label": label, "emoji": emoji, "line": lineno, "prompts": [],
                   "_notes": [], "_skills": []}
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
                add(f"duplicate card id: same title as line {seen_ids[card_id]} in this category; a gallery "
                    "pick of either card loses its notes (the host resolves fail-open)", "rename one title", lineno)
            else:
                seen_ids[card_id] = lineno
            key = body.lower()
            if key in seen_prompts:
                add(f"duplicate prompt text (first at line {seen_prompts[key]}); the host keeps one",
                    "drop or reword one", lineno, "NOTICE")
            else:
                seen_prompts[key] = lineno
            placeholders = list(dict.fromkeys(CI_PLACEHOLDER_RE.findall(body)))
            target = {"title": title, "prompt": body, "placeholders": placeholders,
                      "line": lineno, "bold": bold, "_notes": [], "_skills": []}
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
        for key, cap, what in (("_notes", CI_MAX_NOTES, "notes"), ("_skills", CI_MAX_SKILLS, "skills")):
            own = c.pop(key)
            c[what] = [v for v, _ in own]
            if len(own) > cap:
                add(f"category {c['id']!r} has {len(own)} {what} (cap {cap}); the host drops "
                    f"{[v for v, _ in own[cap:]]}", f"keep at most {cap}", own[cap][1])
            for p in c["prompts"]:
                p[what], dropped = _ci_capped(own, p.pop(key), cap, what == "skills")
                if dropped:
                    add(f"prompt {p['title']!r} exceeds {cap} effective {what} (category first); the "
                        f"host drops {dropped}", f"keep category + prompt {what} at most {cap}",
                        p["line"])
        if any_skills_key:
            for p in c["prompts"]:
                if not p["skills"]:
                    add(f"prompt {p['title']!r} has no skill pointer; the agent gets no how-to",
                        "add `  - skills: <name>` under it or `skills:` above the category's "
                        "first prompt", p["line"], "NOTICE")
        if not c["prompts"]:
            add(f"category {c['id']!r} has no prompts; the gallery hides it",
                "add `- **Title** \u2014 prompt` bullets or drop the heading", c["line"], "NOTICE")
    if not any(c["prompts"] for c in cats):
        add("no prompt parsed; the gallery would be empty",
            "add `## <emoji> Category` sections with `- **Title** \u2014 prompt` bullets", None)
    return cats, findings


def check(root: Path, files: list[str] | None = None, base: str | None = None) -> list[bs.Finding]:
    """Hygiene findings for a brain at `root`. `files` scopes every rule but `conflict`;
    None judges the whole tracked tree. `base` (the ref `files` changed against) enables doc-spill."""
    tracked = bs.git_tracked(root)
    ctx = bs.Ctx(root=root, tracked=tracked, tracked_set=set(tracked),
                 md_files=[p for p in tracked if p.lower().endswith(".md")])
    hidden = bs.hidden_paths(ctx)
    text_files = [p for p in tracked
                  if (root / p).is_file() and Path(p).suffix.lower() in TEXT_SUFFIXES]
    visible = {p for p in text_files if p not in hidden and not p.startswith(TOOLING_DIRS)}
    scoped = visible if files is None else set(files) & visible
    projection = _projection(root)
    mmdc = _mermaid_renderer()
    findings: list[bs.Finding] = []
    mermaid_seen, renderer_failure = False, ""

    def add(rule: str, message: str, rel: str, line: int | None = None) -> None:
        findings.append(bs.Finding("hygiene", f"{rule}: {message}", path=rel, line=line))

    for rel in text_files:
        text = (root / rel).read_text("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if CONFLICT_RE.match(line):
                add("conflict", f"git conflict marker {line[:7]!r}", rel, lineno)
        if rel not in scoped:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if m := ABS_PATH_RE.search(line):
                add("abs-path", f"absolute laptop path {m.group(0)!r}", rel, lineno)
        if rel.endswith(".md"):
            templated = projection is not None and any(g.match(rel) for g in projection[1])
            if templated:
                for lineno, line in enumerate(text.splitlines(), start=1):
                    for key in PLACEHOLDER_RE.findall(line):
                        if key not in projection[0]:
                            add("placeholder", f"{{{{ {key} }}}} is not declared in projection.yaml "
                                               "placeholders", rel, lineno)
                    if "{{" in (rest := PLACEHOLDER_RE.sub("", line)) or "}}" in rest:
                        add("placeholder", "residual `{{`/`}}` in a projection-templated file",
                            rel, lineno)
            desc = (bs.parse_frontmatter(text) or {}).get("description", "").strip().strip("\"'")
            if rel.startswith("skills/") and len(desc) > DESCRIPTION_MAX:
                add("description", f"frontmatter description is {len(desc)} chars (max "
                                   f"{DESCRIPTION_MAX}; publish rejects it)", rel, 1)
            if _frontmatter_flag(text, "customer_facing"):
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if EM_DASH in line:
                        add("em-dash", "em dash in customer-facing copy", rel, lineno)
            if rel == CHAT_INSPIRATION_FILE:
                findings.extend(parse_chat_inspiration(text, rel, root)[1])
            for lineno, source in _mermaid_blocks(text):
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
        elif rel == "projection.yaml":
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (m := DEFAULT_VALUE_RE.search(line)) and EM_DASH in m.group(1):
                    add("em-dash", "em dash in a placeholder default (lands in customer replies)",
                        rel, lineno)

    for f in bs.check_links(ctx):
        if f.path in scoped:
            findings.append(bs.Finding("hygiene", f"links: {f.message}", path=f.path, line=f.line))
    if mermaid_seen and not mmdc:
        findings.append(bs.Finding("hygiene", "mermaid: skipped, no renderer (`mmdc` or `pnpm`) on "
                                   "PATH; diagrams unchecked",
                                   severity="NOTICE"))
    if renderer_failure:
        findings.append(bs.Finding("hygiene", f"mermaid: renderer failed, diagrams unchecked: "
                                   f"{renderer_failure}", severity="NOTICE"))
    if base and files is not None:
        findings += _spill_crossings(root, sorted(scoped), base)
    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_hygiene.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="brain checkout root (default: cwd's git toplevel)")
    p.add_argument("--base", default="origin/main", help="scope rules to files changed vs this ref")
    p.add_argument("--all", action="store_true", help="judge every tracked file")
    p.add_argument("--staged", action="store_true",
                   help="judge only staged files (the managed pre-commit hook)")
    args = p.parse_args(argv)
    try:
        root = bs.git_toplevel(Path(args.root).resolve() if args.root else Path.cwd())
    except bs.StructureError as exc:
        print(f"brain-hygiene: {exc}", file=sys.stderr)
        return 2
    if args.staged:
        files = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--name-only",
                                "--diff-filter=ACMR"], capture_output=True, text=True).stdout.split()
    else:
        files = None if args.all else bs.git_changed_paths(root, args.base)
    findings = check(root, files, None if args.all else "HEAD" if args.staged else args.base)
    for f in findings:
        print(f.render())
    blocking = [f for f in findings if f.severity != "NOTICE"]
    scope = ("all" if files is None else f"{len(files)} staged file(s)" if args.staged
             else f"{len(files)} changed file(s) vs {args.base}")
    print(f"brain hygiene: {len(blocking)} FAIL ({scope}; conflict markers: whole tree)")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
