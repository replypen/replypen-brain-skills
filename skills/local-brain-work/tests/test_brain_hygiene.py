from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SYNC = SCRIPTS.parents[1] / "brain-git-sync" / "scripts" / "brain_git_sync.py"
sys.path.insert(0, str(SCRIPTS))
import brain_hygiene  # noqa: E402

# The 2026-09-25 DentAI failure shape: an unresolved merge committed into a published SKILL.md.
CONFLICTED_SKILL = """---
name: scheduling
description: Plan appointments.
---
<<<<<<< HEAD
Boek maximaal 3 momenten.
=======
Boek maximaal 5 momenten.
>>>>>>> origin/main
"""

FIXTURES = {
    "conflict": {"skills/scheduling/SKILL.md": CONFLICTED_SKILL},
    "placeholder": {"playbooks/a.md": "Bel {{ contact_phone }} of {{ nope }}.\n",
                    "projection.yaml": "placeholders:\n  contact_phone: { type: string }\n"
                                       "templated_globs:\n  - \"playbooks/**/*.md\"\n"},
    "abs-path": {"skills/x/SKILL.md": "Run /Users/pj/code/x.py first.\n"},
    "links": {"AGENTS.md": "See [gone](playbooks/missing.md).\n"},
    "em-dash": {"actions/refund/manifest.yaml": "id: refund\ndescription: Refund\n"
                                                "display_name: \"Terugbetaling — snel\"\n"},
    "mermaid": {"skills/x/SKILL.md": "```mermaid\ngraph TD\n  A-->>>((B\n```\n"},
    "description": {"skills/cases/x.md": f"---\nname: x\ndescription: \"{'w' * 1025}\"\n---\nBody.\n"},
    # kampkompas-staff knowledge/, 2026-09: an editing transcript's closing tags left in the doc.
    "tool-markup": {"knowledge/criteria.md": "Criteria.\n</content>\n</invoke>\n"},
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_brain(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    for rel, text in {"AGENTS.md": "# Router\n", "skills/.keep": "", **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, "utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    return root


def rules(root: Path, files: list[str] | None = None) -> set[str]:
    return {f.message.split(":", 1)[0] for f in brain_hygiene.check(root, files)
            if f.severity != "NOTICE"}


def test_clean_brain_passes(tmp_path: Path) -> None:
    root = make_brain(tmp_path, {
        "playbooks/a.md": "Placeholder docs use `{{ key }}` in code. See [router](../AGENTS.md).\n",
        "docs/flow.md": "Tom — plain prose may use em dashes.\n",
        "_internal/notes.md": "/Users/pj/code and {{ nope }} are maintainer-only.\n",
    })
    assert rules(root) == set()


@pytest.mark.parametrize("rule", sorted(FIXTURES))
def test_each_failure_is_rejected(tmp_path: Path, rule: str) -> None:
    root = make_brain(tmp_path, FIXTURES[rule])
    findings = brain_hygiene.check(root)
    if rule == "mermaid" and brain_hygiene._mermaid_renderer() is None:
        assert any("mermaid: skipped" in f.message for f in findings)
        pytest.skip("no mermaid renderer on PATH")
    if rule == "mermaid" and any("renderer failed" in f.message for f in findings):
        pytest.skip("mermaid renderer cannot start here")
    assert rule in rules(root)


def test_code_is_not_a_link_and_not_markup(tmp_path: Path) -> None:
    """A `[title](url)` example in backticks or a fence is prose about links, not a link; leaked tags
    quoted as code are documentation, and run-hidden paths are never judged for them."""
    root = make_brain(tmp_path, {
        "skills/records/links.md": "Write `[title](record_url)` and `![alt](/brain/x/<id>.png)`.\n"
                                   "Quote `</invoke>` when explaining it.\n"
                                   "```md\n[gone](missing.md)\n<parameter name=\"x\">\n```\n",
        "_internal/transcript.md": "</content>\n</invoke>\n",
    })
    assert rules(root) == set()


def test_mismatched_backticks_leave_a_rendered_link(tmp_path: Path) -> None:
    """CommonMark: a 2-backtick run closes only on another 2-backtick run, so this link renders."""
    root = make_brain(tmp_path, {"skills/records/links.md": "``[bad](missing.md)`\n"})
    assert rules(root) == {"links"}


def test_indented_code_is_code_but_wrapped_spans_are_prose(tmp_path: Path) -> None:
    """Tags quoted in a 4-space code block are documentation; indentation under a list item is item
    text. Code spans are single-line: one that wraps a line is judged as prose on purpose (rare, and a
    multi-line match would swallow headings and list items between two unrelated backticks)."""
    root = make_brain(tmp_path, {
        "skills/x/doc.md": "Example:\n\n    </invoke>\n    <parameter name=\"a\">\n",
        "skills/y/doc.md": "- step\n\n    </invoke>\n",
        "skills/z/doc.md": "Quote `the\n</content> tag` across a wrap.\n",
    })
    found = [(f.path, f.message.split(":", 1)[0]) for f in brain_hygiene.check(root)
             if f.severity != "NOTICE"]
    assert found == [("skills/y/doc.md", "tool-markup"), ("skills/z/doc.md", "tool-markup")]


@pytest.mark.parametrize("text, rule", [
    ("`start\n# [bad](missing.md)\nend`\n", "links"),
    ("- `start\n- </invoke>\n- end`\n", "tool-markup"),
])
def test_backticks_on_other_lines_do_not_hide_blocks(tmp_path: Path, text: str, rule: str) -> None:
    """Sol round 2: a backtick on one line and another two lines down are not one code span."""
    root = make_brain(tmp_path, {"skills/x/doc.md": text})
    assert rule in rules(root)


def test_mermaid_renderer_failure_names_the_install_command() -> None:
    detail = "Error: Could not find chrome-headless-shell (ver. 155.0.8059.79). This can occur if either"
    assert brain_hygiene._mermaid_fix(detail) == (
        "; fix: `pnpm dlx @puppeteer/browsers install chrome-headless-shell@155.0.8059.79 "
        "--path ~/.cache/puppeteer`")
    assert brain_hygiene._mermaid_fix("exit 1") == ""


def test_changed_scope_keeps_conflicts_whole_tree(tmp_path: Path) -> None:
    root = make_brain(tmp_path, {**FIXTURES["abs-path"], **FIXTURES["conflict"]})
    assert rules(root, files=[]) == {"conflict"}


def test_git_sync_refuses_to_push_conflict_markers(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    brain = make_brain(tmp_path / "brain", {})
    git(brain, "remote", "add", "origin", str(origin))
    git(brain, "push", "-q", "origin", "main")
    before = git(origin, "rev-parse", "main")
    (brain / "skills" / "scheduling").mkdir(parents=True)
    (brain / "skills" / "scheduling" / "SKILL.md").write_text(CONFLICTED_SKILL, "utf-8")
    git(brain, "add", "-A")

    run = subprocess.run([sys.executable, str(SYNC), "--repo", str(brain),
                          "--commit-message", "scheduling: merge"],
                         capture_output=True, text=True)

    assert run.returncode == 2, run.stderr
    assert "brain hygiene gate failed" in run.stderr
    assert "skills/scheduling/SKILL.md:5: hygiene: conflict" in run.stderr
    assert git(origin, "rev-parse", "main") == before


def test_staged_mode_judges_only_the_index(tmp_path: Path) -> None:
    """What the managed pre-commit hook runs: a staged bad file fails, an unstaged one is ignored."""
    root = make_brain(tmp_path, {})
    (root / "a.md").write_text("Run /Users/pj/x first.\n", "utf-8")
    (root / "b.md").write_text("Run /Users/pj/y first.\n", "utf-8")
    git(root, "add", "a.md")
    run = subprocess.run([sys.executable, str(SCRIPTS / "brain_hygiene.py"), "--staged",
                          "--root", str(root)], capture_output=True, text=True)
    assert run.returncode == 1
    assert "a.md:1: hygiene: abs-path" in run.stdout and "b.md" not in run.stdout



# --- doc-spill (6000-byte bash cliff, regression-only NOTICE) -------------------------------------

def _staged_spill(tmp_path: Path, before: dict[str, str], after: dict[str, str]) -> subprocess.CompletedProcess:
    root = make_brain(tmp_path, before)
    for rel, text in after.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, "utf-8")
    git(root, "add", "-A")
    return subprocess.run([sys.executable, str(SCRIPTS / "brain_hygiene.py"), "--staged",
                           "--root", str(root)], capture_output=True, text=True)


def test_doc_spill_fails_a_new_crossing_on_the_whole_file(tmp_path: Path) -> None:
    """`cat` prints the frontmatter too: a 5980-byte body behind a 40-byte header already spills."""
    fm = "---\ndescription: Open for refunds.\n---\n"
    run = _staged_spill(tmp_path, {"skills/cases/a.md": fm + "x" * 5900},
                        {"skills/cases/a.md": fm + "x" * 5980, "skills/cases/b.md": "y" * 6001})
    assert run.returncode == 1, run.stdout
    assert "skills/cases/a.md: hygiene: doc-spill: 5939→6019 bytes" in run.stdout
    assert "skills/cases/b.md: hygiene: doc-spill: new→6001 bytes" in run.stdout
    assert "split into a lean core + detail file linked by an \"Open X for Y\" line" in run.stdout
    assert "NOTICE" not in run.stdout


def test_doc_spill_silent_when_already_over_tagged_or_exempt(tmp_path: Path) -> None:
    tagged = "---\ninclude_in: [agent]\n---\n"
    run = _staged_spill(tmp_path, {"skills/cases/big.md": "x" * 7000},
                        {"skills/cases/big.md": "x" * 9000,
                         "skills/cases/tagged.md": tagged + "x" * 7000,
                         "knowledge/hc/a.md": "---\nsource_url: https://x/a\n---\n" + "x" * 7000,
                         "knowledge/hc/v.md": "---\nvideo_url: https://y/v\n---\n" + "x" * 7000,
                         "docs/long.md": "x" * 7000, "journal/2026-10.md": "x" * 7000,
                         "AGENTS.md": "x" * 7000})
    assert run.returncode == 0, run.stdout
    assert "doc-spill" not in run.stdout


def test_doc_spill_follows_renames(tmp_path: Path) -> None:
    """Moving an unchanged legacy 7000-byte doc is not a new crossing."""
    root = make_brain(tmp_path, {"skills/cases/legacy.md": "x" * 7000})
    git(root, "mv", "skills/cases/legacy.md", "skills/cases/renamed.md")
    run = subprocess.run([sys.executable, str(SCRIPTS / "brain_hygiene.py"), "--staged",
                          "--root", str(root)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
    assert "doc-spill" not in run.stdout


def test_doc_spill_all_mode_compares_against_base_and_needs_one(tmp_path: Path) -> None:
    """`--all` judges every file against `--base`; with no resolvable base no crossing is provable."""
    root = make_brain(tmp_path, {"skills/cases/a.md": "x" * 5000, "skills/cases/old.md": "x" * 7000})
    git(root, "branch", "base")
    (root / "skills/cases/a.md").write_text("x" * 6500, "utf-8")
    git(root, "commit", "-qam", "grow")

    spills = lambda base: [f.path for f in brain_hygiene.check(root, None, base)  # noqa: E731
                           if f.message.startswith("doc-spill")]
    assert spills("base") == ["skills/cases/a.md"]
    assert spills("HEAD") == []
    assert spills("origin/main") == []  # no remote in this fixture


# --- chat-inspiration --------------------------------------------------------------------------

GOOD_INSPIRATION = """---
surfaces: [chat, dashboard_chat]
---
## 📝 Inschrijvingen
- **Wachtlijst** — Welke kampen hebben een wachtlijst?
- **Gezocht** - Zoek de inschrijvingen van [naam].
## 🧑‍🏫 Monitoren {#monitoren}
- **Dichtbij:** Geef me monitoren die in de buurt van [stad] wonen.
* **Rollen**: Welke rollen hebben we?
"""
HEADER = "---\nsurfaces: [chat, dashboard_chat]\n---\n"
REAL_BRAINS = Path(__file__).resolve().parents[4]


def ci(text: str) -> tuple[set[str], set[str]]:
    """(ERROR messages, NOTICE messages) of parse_chat_inspiration, rule prefix stripped."""
    _, found = brain_hygiene.parse_chat_inspiration(text)
    msgs = {sev: {f.message.removeprefix("chat-inspiration: ") for f in found if f.severity == sev}
            for sev in ("ERROR", "NOTICE")}
    return msgs["ERROR"], msgs["NOTICE"]


def test_chat_inspiration_good_file_parses_clean() -> None:
    cats, found = brain_hygiene.parse_chat_inspiration(GOOD_INSPIRATION)
    assert found == []
    assert [c["id"] for c in cats] == ["inschrijvingen", "monitoren"]
    assert [p["title"] for c in cats for p in c["prompts"]] == ["Wachtlijst", "Gezocht", "Dichtbij", "Rollen"]
    assert cats[1]["prompts"][0]["placeholders"] == ["[stad]"]


@pytest.mark.parametrize("text, needle", [
    ("---\ndescription: x\n---\n## A\n- **T** — p\n", "no frontmatter `surfaces:`"),
    ("## A\n- **T** — p\n", "no frontmatter `surfaces:`"),
    ("---\nsurfaces: [dashboard_chat]\n---\n## A\n- **T** — p\n", "lacks `chat`"),
    (HEADER + "Just prose.\n", "no prompt parsed"),
    (HEADER + "## A\n", "no prompt parsed"),
    (HEADER + "## A\n- **T** — p\n- **Empty** —\n", "bullet yields no prompt"),
    (HEADER + "## A\n- **T** — " + "x" * 501 + "\n", "cap 500"),
    (HEADER + "## A\n- **" + "t" * 81 + "** — p\n", "cap 80"),
    (HEADER + "".join(f"## Cat {i}\n- **T** — p{i}\n" for i in range(21)), "20-category cap"),
    (HEADER + "## A\n" + "".join(f"- **T{i}** — p{i}\n" for i in range(51)), "50-prompt cap"),
    (HEADER + "## A\n- **T** — p\n## B {#a}\n- **U** — q\n", "duplicate category id 'a'"),
    (HEADER + "## A\n- **T** — Monitoren in [stad\n", "unbalanced or invalid"),
    (HEADER + "## A\n- **T** — Monitoren in [a [b]]\n", "unbalanced or invalid"),
    (HEADER + "## 📝\n- **T** — p\n## B\n- **U** — q\n", "heading yields no category"),
])
def test_chat_inspiration_fails(text: str, needle: str) -> None:
    errors, _ = ci(text)
    assert any(needle in m for m in errors), errors


@pytest.mark.parametrize("text, needle", [
    (HEADER + "## A\n- **T** — " + "x" * 161 + "\n", "cards read best under 160"),
    (HEADER + "## A\n- Welke kampen hebben een wachtlijst?\n", "no bold title"),
    (HEADER + "## A\n- **T** — p\n## B\n", "category 'b' has no prompts"),
    (HEADER + "## A\n- **T** — Same text\n- **U** — same TEXT\n", "duplicate prompt text"),
    (HEADER + "## A\n- **T** — p\n### Sub\n- **U** — q\n", "under a sub-heading"),
])
def test_chat_inspiration_warns_without_blocking(text: str, needle: str) -> None:
    errors, notices = ci(text)
    assert errors == set()
    assert any(needle in m for m in notices), notices


def test_chat_inspiration_long_title_words_are_fine() -> None:
    assert ci(HEADER + "## A\n- **One two three four five six** — p\n") == (set(), set())


def test_chat_inspiration_runs_in_check_scoped_to_root_file(tmp_path: Path) -> None:
    bad = HEADER + "## A\n- **T** — Monitoren in [stad\n"
    root = make_brain(tmp_path, {"chat_inspiration.md": bad, "docs/chat_inspiration.md": bad})
    found = [f for f in brain_hygiene.check(root) if "chat-inspiration" in f.message]
    assert [(f.path, f.line) for f in found] == [("chat_inspiration.md", 5)]
    assert rules(root, files=[]) == set()  # changed-file scoped like the other rules


@pytest.mark.parametrize("brain", ["kampadmin", "dentai", "pro-backup"])
def test_real_brain_galleries_have_no_fail(brain: str) -> None:
    path = REAL_BRAINS / f"replypen-brain-{brain}" / "chat_inspiration.md"
    if not path.is_file():
        pytest.skip(f"{path} not checked out")
    cats, found = brain_hygiene.parse_chat_inspiration(path.read_text("utf-8"))
    assert cats and [f.render() for f in found if f.severity != "NOTICE"] == []


# --- chat-inspiration author notes + skill pointers (grammar v2; host internal/chatinspiration) ---

GRAMMAR_EXAMPLE = """---
surfaces: [chat, dashboard_chat]
---
## 📝 Inschrijvingen
skills: records, /mirrors/kampadmin-replypen-common/skills/columns
Elke lijst: één rij per inschrijving, status als eerste kolom.
- **Wachtlijst bekijken** — Voor welke activiteiten staat er een wachtlijst?
  - skills: subscription-stats
  - `registrations.waitlist_position IS NOT NULL`; sluit geannuleerde uit; aantal per activiteit.
  - Valkuil: een kind kan op meerdere wachtlijsten staan, tel per activiteit, niet per kind.
- **Bijna volzet** — Welke activiteiten zijn bijna volzet?
"""
CAT_NOTE = "Elke lijst: één rij per inschrijving, status als eerste kolom."
CAT_SKILLS = ["/brain/skills/records/SKILL.md",
              "/mirrors/kampadmin-replypen-common/skills/columns/SKILL.md"]


def test_chat_inspiration_grammar_example_notes_and_skills() -> None:
    cats, found = brain_hygiene.parse_chat_inspiration(GRAMMAR_EXAMPLE)
    assert found == []
    (cat,) = cats
    assert cat["notes"] == [CAT_NOTE] and cat["skills"] == CAT_SKILLS
    wait, full = cat["prompts"]
    assert [p["title"] for p in cat["prompts"]] == ["Wachtlijst bekijken", "Bijna volzet"]
    assert wait["skills"] == CAT_SKILLS + ["/brain/skills/subscription-stats/SKILL.md"]
    assert wait["notes"] == [
        CAT_NOTE,
        "`registrations.waitlist_position IS NOT NULL`; sluit geannuleerde uit; aantal per activiteit.",
        "Valkuil: een kind kan op meerdere wachtlijsten staan, tel per activiteit, niet per kind."]
    assert full["notes"] == [CAT_NOTE] and full["skills"] == CAT_SKILLS


@pytest.mark.parametrize("entry, path", [
    ("records", "/brain/skills/records/SKILL.md"),
    ("/skills/xlsx", "/skills/xlsx/SKILL.md"),
    ("/tenant/skills/a/guide.md", "/tenant/skills/a/guide.md"),
    ("skills/records", None), ("/etc/passwd", None), ("/brain/../x", None), ("my skill", None),
])
def test_chat_inspiration_skill_path(entry: str, path: str | None) -> None:
    assert brain_hygiene.ci_skill_path(entry) == path


def test_chat_inspiration_skill_caps_category_first() -> None:
    text = HEADER + "## A\nskills: " + ", ".join(f"c{i}" for i in range(5)) + "\n" \
        + "- **T** — p\n  - skills: c0, p1, p2\n"
    cats, found = brain_hygiene.parse_chat_inspiration(text)
    names = [s.split("/")[3] for s in cats[0]["prompts"][0]["skills"]]
    assert names == ["c0", "c1", "c2", "c3", "c4", "p1"]
    assert any("drops ['/brain/skills/p2/SKILL.md']" in f.message for f in found)


@pytest.mark.parametrize("text, needle", [
    (HEADER + "## A\n- **T** — p\nmore prompt text\n", "plain line after the category's first prompt"),
    (HEADER + "## A\n- **T** — p\n  - skills: ../x\n", "skill pointer '../x'"),
    (HEADER + "## A\n- **T** — p\n  - skills: /opt/x\n", "skill pointer '/opt/x'"),
    (HEADER + "## A\n- **T** — p\n  - " + "n" * 301 + "\n", "cap 300"),
    (HEADER + "## A\n" + "".join(f"note {i}\n" for i in range(5)) + "- **T** — p\n"
     + "".join(f"  - own {i}\n" for i in range(4)), "drops ['own 3']"),
    (HEADER + "## A\n" + "".join(f"note {i}\n" for i in range(9)) + "- **T** — p\n",
     "category 'a' has 9 notes (cap 8)"),
    (HEADER + "## A\n- **T** — p\n- skills: records\n", "top-level bullet starting `skills:`"),
])
def test_chat_inspiration_note_errors(text: str, needle: str) -> None:
    errors, _ = ci(text)
    assert any(needle in m for m in errors), errors


@pytest.mark.parametrize("text, needle", [
    (HEADER + "## A\n  - stray\n- **T** — p\n", "indented bullet before the category's first prompt"),
    (HEADER + "## A\n- **T** — p\n  - Skill: records\n", "'Skill:', read as a note"),
    (HEADER + "## A\n- **T** — p\n  - skills: x\n- **U** — q\n", "prompt 'U' has no skill pointer"),
])
def test_chat_inspiration_note_notices(text: str, needle: str) -> None:
    errors, notices = ci(text)
    assert errors == set()
    assert any(needle in m for m in notices), notices


def test_chat_inspiration_no_skill_notice_when_feature_unused() -> None:
    assert ci(HEADER + "## A\n- **T** — p\n  - a note\n") == (set(), set())


def test_chat_inspiration_missing_bare_skill_needs_root(tmp_path: Path) -> None:
    text = HEADER + "## A\nskills: present, absent\n- **T** — p\n"
    (tmp_path / "skills" / "present").mkdir(parents=True)
    (tmp_path / "skills" / "present" / "SKILL.md").write_text("x")
    _, found = brain_hygiene.parse_chat_inspiration(text, root=tmp_path)
    assert [f.message for f in found if f.severity == "ERROR"] == [
        ("chat-inspiration: skill 'absent' has no skills/absent/SKILL.md in this brain; the agent "
         "would be pointed at a missing file; fix: fix the name or add the skill")]
    assert ci(text) == (set(), set())  # no root: existence unchecked


@pytest.mark.parametrize("entry,want", [
    ("/brain///x/", "/brain/x/SKILL.md"),
    ("/brain/notes.md/", "/brain/notes.md"),
    ("/mirrors/r/skills/a", "/mirrors/r/skills/a/SKILL.md"),
    ("/brain", None),
    ("/brain//", None),
    ("/brain/../etc", None),
])
def test_chat_inspiration_skill_path_normalises_before_checks(entry: str, want: str | None) -> None:
    # Lockstep with Go SkillPath (TestSkillPathNormalisesBeforeChecks).
    assert brain_hygiene.ci_skill_path(entry) == want


def test_chat_inspiration_duplicate_card_id_fails() -> None:
    text = ("---\nsurfaces: [chat]\n---\n## Cat\n- **Same** — first\n  - skills: /skills/x\n"
            "- **Same** — second\n- **Other** — third\n")
    _, found = brain_hygiene.parse_chat_inspiration(text)
    errors = [f for f in found if f.severity == "ERROR"]
    assert len(errors) == 1 and "duplicate card id" in errors[0].message and errors[0].line == 7


def test_git_sync_gates_a_knowledge_only_brain(tmp_path: Path) -> None:
    """No skills/ or actions/: `.rootcause.toml` `project =` plus a run journal still makes it a brain,
    so the push gate runs. A source repo that only scopes `rc` (no journal) stays ungated."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    brain = tmp_path / "brain"
    brain.mkdir()
    git(brain, "init", "-q", "-b", "main")
    git(brain, "config", "user.email", "t@example.com")
    git(brain, "config", "user.name", "Test")
    for rel, text in {".rootcause.toml": 'project = "helpdesk"\n', "journal/.keep": "",
                      "AGENTS.md": "# Router\n"}.items():
        (brain / rel).parent.mkdir(parents=True, exist_ok=True)
        (brain / rel).write_text(text, "utf-8")
    git(brain, "add", "-A")
    git(brain, "commit", "-q", "-m", "fixture")
    git(brain, "remote", "add", "origin", str(origin))
    git(brain, "push", "-q", "origin", "main")
    (brain / "knowledge").mkdir()
    (brain / "knowledge" / "a.md").write_text(CONFLICTED_SKILL, "utf-8")
    git(brain, "add", "-A")

    run = subprocess.run([sys.executable, str(SYNC), "--repo", str(brain), "--commit-message", "kb"],
                         capture_output=True, text=True)

    assert run.returncode == 2, run.stderr
    assert "knowledge/a.md:5: hygiene: conflict" in run.stderr
