"""Unit tests for the offline brain description lint (lib.brain_lint)."""

from __future__ import annotations

import json
import sys
import types
import os
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.brain_lint import (
    ACTION_MANIFEST_ENUMS,
    ACTION_SURFACE_ALIASES,
    ACTION_SURFACES,
    DESC_FULL_MAX_LEN,
    DESC_MAX_LEN,
    Finding,
    MIRROR_SCAN_DIRS,
    _include_in,
    md_description_full,
    md_description_yaml_tag,
    md_tree_gloss,
    _manifest_description,
    detect_repo_kind,
    format_report,
    lint_brain,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return p


def _fails(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "FAIL"]


def _warns(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "WARN"]


CORPUS = Path(__file__).resolve().parents[1] / "lib" / "contracts" / "frontmatter"
EXPECTED = json.loads((CORPUS / "expected.json").read_text("utf-8"))


def test_frontmatter_corpus_is_complete() -> None:
    assert sorted(EXPECTED) == sorted(p.name for p in CORPUS.glob("*.md"))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_frontmatter_corpus(name: str) -> None:
    """The shared corpus rootcause's treeview.DocDescription replays byte-for-byte."""
    want = EXPECTED[name]
    full = md_description_full(CORPUS / name)
    assert (full or "") == want["description_full"]
    assert md_tree_gloss(full) == want["gloss"]
    assert sorted(_include_in((CORPUS / name).read_bytes().decode("utf-8", "replace"))) == want["include_in"]
    assert md_description_yaml_tag(CORPUS / name) == want["yaml_tag"]


def test_md_description_variants(tmp_path: Path) -> None:
    collapsed = _write(tmp_path / "c.md", "---\ndescription:   lots   of\tspace  \n---\n")
    assert md_description_full(collapsed) == "lots of space"
    assert md_description_full(_write(tmp_path / "b.md", "---\ndescription: |\n  multi\n  line\n---\n")) == "multi line"
    assert md_description_full(_write(tmp_path / "e.md", "---\ndescription:\n---\n")) is None
    assert md_description_full(_write(tmp_path / "k.md", "---\nname: foo\n---\n")) is None


def test_manifest_description(tmp_path: Path) -> None:
    m = _write(tmp_path / "manifest.yaml", "id: refund\ndescription: >-\n  Refund a customer\n")
    assert _manifest_description(m) == "Refund a customer"
    assert _manifest_description(_write(tmp_path / "m2.yaml", "id: x\n")) is None
    assert _manifest_description(_write(tmp_path / "m3.yaml", "id: x\ndescription: '  '\n")) is None


def _seed_brain(root: Path) -> None:
    _write(root / "skills/backups/SKILL.md", "---\ndescription: When a backup job fails\n---\n")
    _write(root / "skills/cases/login.md", "---\ndescription: Customer cannot sign in\n---\n")
    _write(root / "actions/refund/manifest.yaml", "id: refund\ndescription: Refund a customer\n")


def test_lint_brain_all_good(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    assert _fails(lint_brain(tmp_path)) == []


def test_lint_brain_validates_action_surfaces_in_live_and_draft_manifests(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/manifest.yaml",
           "id: refund\ndescription: Refund a customer\nsurfaces: [gmail, dashboard_chat]\n")
    _write(tmp_path / "actions-drafts/bad/manifest.yaml",
           "id: bad\ndescription: Canonical draft\nsurfaces: [email]\n")
    _write(tmp_path / "actions/bogus/manifest.yaml",
           "id: bogus\ndescription: Bad value\nsurfaces: [bogus]\n")
    _write(tmp_path / "actions/bad_shape/manifest.yaml",
           "id: bad_shape\ndescription: Bad shape\nsurfaces: gmail\n")
    _write(tmp_path / "actions/empty/manifest.yaml",
           "id: empty\ndescription: Empty\nsurfaces: []\n")
    _write(tmp_path / "actions/null/manifest.yaml",
           "id: null\ndescription: Null\nsurfaces: null\n")

    all_findings = [f for f in lint_brain(tmp_path) if f.rule == "action-surfaces"]
    findings = _fails(all_findings)

    assert [f.path for f in findings] == [
        "actions/bad_shape/manifest.yaml",
        "actions/bogus/manifest.yaml",
        "actions/empty/manifest.yaml",
        "actions/null/manifest.yaml",
    ]
    assert "must be a list" in findings[0].message
    assert "unknown action surface 'bogus'" in findings[1].message
    assert "omit it to allow all" in findings[2].message
    assert "must be a list" in findings[3].message
    warnings = _warns(all_findings)
    assert len(warnings) == 1
    assert warnings[0].path == "actions/refund/manifest.yaml"
    assert "'gmail' is deprecated; use 'email'" in warnings[0].message


def test_action_surface_vocabulary_is_loaded_from_embassy_contract() -> None:
    assert ACTION_SURFACES == (
        "chat", "dashboard_chat", "email", "intercom", "whatsapp",
        "compose", "prompt_api", "mcp", "embassy", "console",
    )
    assert ACTION_SURFACE_ALIASES == {"gmail": "email", "outlook": "email", "imap": "email"}


def test_lint_brain_validates_action_manifest_enums_in_live_and_draft_manifests(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/manifest.yaml",
           "id: refund\ndescription: Refund a customer\nruntime: python\nautonomy: policy\n"
           "risk: high\nconfirm: requester\nmin_tier: pro\npreflight: {runtime: bash}\n"
           "policy: {runtime: ruby}\n")
    _write(tmp_path / "actions/unknown/manifest.yaml",
           "id: unknown\ndescription: Unknown value\nautonomy: always\n")
    _write(tmp_path / "actions/wrong_case/manifest.yaml",
           "id: wrong_case\ndescription: Wrong case\nrisk: High\n")
    _write(tmp_path / "actions/null_tier/manifest.yaml",
           "id: null_tier\ndescription: Null\nmin_tier: null\n")
    _write(tmp_path / "actions-drafts/bad_preflight/manifest.yaml",
           "id: bad_preflight\ndescription: Draft\npreflight:\n  runtime: node\n")

    findings = [f for f in lint_brain(tmp_path) if f.rule == "action-manifest-enums"]

    assert [(f.path, f.level) for f in findings] == [
        ("actions/null_tier/manifest.yaml", "FAIL"),
        ("actions/unknown/manifest.yaml", "FAIL"),
        ("actions/wrong_case/manifest.yaml", "FAIL"),
        ("actions-drafts/bad_preflight/manifest.yaml", "FAIL"),
    ]
    assert findings[0].message == "`min_tier` must be one of: pro (got None)"
    assert findings[1].message == "`autonomy` must be one of: human, policy, auto (got 'always')"
    assert findings[2].message == "`risk` must be one of: low, medium, high (got 'High')"
    assert findings[3].message == "`preflight.runtime` must be one of: python, bash, ruby (got 'node')"


def test_manifest_enum_vocabulary_is_loaded_from_contract() -> None:
    assert ACTION_MANIFEST_ENUMS == {
        "autonomy": ("human", "policy", "auto"),
        "confirm": ("reviewer", "requester"),
        "min_tier": ("pro",),
        "policy.runtime": ("python", "bash", "ruby"),
        "preflight.runtime": ("python", "bash", "ruby"),
        "risk": ("low", "medium", "high"),
        "runtime": ("ruby", "python"),
    }


def test_lint_brain_flags_python_outside_supported_roots(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / ".replypenignore", "/action-authoring/\n")  # hides only that tree
    _write(tmp_path / "notes/faq/scripts/generate_index.py", "print('index')\n")
    _write(tmp_path / "helper.py", "print('helper')\n")

    findings = [f for f in _fails(lint_brain(tmp_path)) if f.rule == "script-outside-skills"]

    assert [f.path for f in findings] == ["helper.py", "notes/faq/scripts/generate_index.py"]
    assert all("skills/<topic>/scripts/" in f.message for f in findings)
    assert all("import smoke" in f.message for f in findings)


def test_lint_brain_allows_python_in_supported_and_ignored_dirs(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    allowed = [
        "conftest.py",
        "skills/faq/scripts/generate_index.py",
        "actions/refund/script.py",
        "actions-drafts/refund/script.py",
        "actions-drafts/refund/policy.py",
        "tests/test_faq.py",
        ".agents/skills/local/scripts/helper.py",
        ".claude/skills/local/scripts/helper.py",
        ".rootcause/cache/helper.py",
        ".git/hooks/helper.py",
        ".venv/lib/helper.py",
        "_internal/tools/helper.py",
        "node_modules/package/helper.py",
        "action-authoring/generate.py",  # run-hidden operator generator
    ]
    _write(tmp_path / ".replypenignore", "/action-authoring/\n")
    for rel in allowed:
        _write(tmp_path / rel, "# allowed\n")

    assert not any(f.rule == "script-outside-skills" for f in lint_brain(tmp_path))


def test_lint_brain_survives_brain_test_replacing_lib_module(tmp_path: Path, monkeypatch) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/script.py", "_DEAD = 1\n")
    monkeypatch.setitem(sys.modules, "lib", types.SimpleNamespace())

    assert any("_DEAD" in f.message for f in lint_brain(tmp_path))


def test_lint_brain_flags_missing_and_overlong(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "skills/nodesc/SKILL.md", "# no frontmatter here\n")
    _write(tmp_path / "skills/cases/toolong.md", f"---\ndescription: {'x' * (DESC_FULL_MAX_LEN + 1)}\n---\n")
    _write(tmp_path / "skills/listdesc/SKILL.md", "---\ndescription: [a, b]\n---\n")
    _write(tmp_path / "skills/numdesc/SKILL.md", "---\ndescription: 42\n---\n")
    _write(tmp_path / "skills/late/SKILL.md",
           "---\ndescription: |\n  block\n" + "pad: " + "y" * 9000 + "\n---\n")
    _write(tmp_path / "actions/broken/manifest.yaml", "id: broken\n")

    fails = {f.path: f for f in _fails(lint_brain(tmp_path))}
    assert set(fails) >= {"skills/nodesc/SKILL.md", "skills/cases/toolong.md", "skills/listdesc/SKILL.md",
                          "skills/numdesc/SKILL.md", "skills/late/SKILL.md", "actions/broken/manifest.yaml"}
    assert "Agent Skills limit" in fails["skills/cases/toolong.md"].message
    assert "must be a YAML string" in fails["skills/numdesc/SKILL.md"].message
    assert "skills/backups/SKILL.md" not in fails


def test_md_description_any_yaml_form_and_first_sentence(tmp_path: Path) -> None:
    """Block scalars and wrapped values are read whole; past 150 chars only the first sentence matters."""
    assert DESC_MAX_LEN == 150
    _seed_brain(tmp_path)
    lead = "Open when a parent cannot log in."
    _write(tmp_path / "skills/login/SKILL.md",
           f"---\ndescription: >-\n  {lead}\n  " + "Covers resets, 2FA and lockouts. " * 10 + "\n---\n")
    _write(tmp_path / "skills/cases/wrapped.md", "---\ndescription: Open when a refund\n  fails twice\n---\n")
    _write(tmp_path / "skills/cases/atcap.md", f"---\ndescription: {'x' * DESC_MAX_LEN}\n---\n")
    _write(tmp_path / "skills/cases/overcap.md", f"---\ndescription: {'x' * (DESC_MAX_LEN + 1)}\n---\n")
    _write(tmp_path / "skills/cases/atfull.md", f"---\ndescription: {'x' * DESC_FULL_MAX_LEN}\n---\n")

    findings = lint_brain(tmp_path)
    assert _fails(findings) == []
    warned = {f.path for f in _warns(findings) if f.rule == "description-length"}
    assert warned == {"skills/cases/overcap.md", "skills/cases/atfull.md"}


def test_lint_brain_overlong_manifest_warns_not_fails(tmp_path: Path) -> None:
    # Manifest descriptions double as full-length action-catalog copy: overlong is WARN, never FAIL.
    _seed_brain(tmp_path)
    long = "when a refund is due " * 10
    _write(tmp_path / "actions/verbose/manifest.yaml", f"id: verbose\ndescription: {long.strip()}\n")

    findings = lint_brain(tmp_path)
    assert all(f.path != "actions/verbose/manifest.yaml" for f in _fails(findings))
    assert any(f.path == "actions/verbose/manifest.yaml" and "short when-to-use sentence" in f.message
               for f in _warns(findings))


def test_lint_brain_accepts_rich_manifest_with_frontloaded_sentence(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    rich = "Refund a settled duplicate charge. " + ("Detailed safety and verification copy. " * 8)
    _write(tmp_path / "actions/verbose/manifest.yaml", f"id: verbose\ndescription: {rich.strip()}\n")

    findings = lint_brain(tmp_path)
    assert all(f.path != "actions/verbose/manifest.yaml" for f in findings)


def test_lint_brain_warns_on_contains_style(tmp_path: Path) -> None:
    _write(tmp_path / "skills/x/SKILL.md",
           "---\ndescription: This file contains the backup schema\n---\n")
    findings = lint_brain(tmp_path)
    assert _fails(findings) == []  # style is WARN, not FAIL
    assert any("x/SKILL.md" in w.path for w in _warns(findings))


def test_format_report_groups_fail_before_warn_by_rule() -> None:
    report = format_report([
        Finding("actions/b/script.py:2", "WARN", "dead b", "private-dead"),
        Finding("skills/x/SKILL.md", "FAIL", "missing", "description-missing"),
        Finding("actions/a/script.py:1", "WARN", "dead a", "private-dead"),
        Finding("actions/", "WARN", "duplicate", "helper-duplicate"),
    ])

    assert report.splitlines() == [
        "brain lint: 1 FAIL, 3 WARN",
        "FAIL missing descriptions (1)",
        "  skills/x/SKILL.md — missing",
        "WARN dead private names (2)",
        "  actions/a/script.py:1 — dead a",
        "  actions/b/script.py:2 — dead b",
        "WARN duplicate helpers (1)",
        "  actions/ — duplicate",
    ]


def test_pytest_adapter_prints_one_compact_block_without_warning_summary(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "actions/refund/script.py", "_DEAD = 1\n")
    _write(tmp_path / "skills/test_sample.py", "def test_ok():\n    assert True\n")
    runtime = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(runtime)}

    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "skills"), "-q", "-p", "no:cacheprovider",
         "-p", "lib.brain_lint_pytest"],
        text=True, capture_output=True, check=False, env=env,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count("brain lint: 0 FAIL, 1 WARN") == 1
    assert "WARN dead private names (1)" in run.stdout
    assert "warnings summary" not in run.stdout


def test_pytest_adapter_surfaces_script_outside_skills(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "notes/faq/scripts/generate_index.py", "print('index')\n")
    _write(tmp_path / "skills/test_sample.py", "def test_ok():\n    assert True\n")
    runtime = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(runtime)}

    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "skills"), "-q", "-p", "no:cacheprovider",
         "-p", "lib.brain_lint_pytest"],
        text=True, capture_output=True, check=False, env=env,
    )

    assert run.returncode == 1
    assert "FAIL scripts outside skills (1)" in run.stdout
    assert "notes/faq/scripts/generate_index.py" in run.stdout


def _git_brain(root: Path) -> None:
    """A real git repo so the symlink lint can consult the index, not just the working tree."""
    _seed_brain(root)
    _write(root / ".gitignore", ".agents/\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)


def _symlink_findings(root: Path, level: str) -> list[Finding]:
    picker = _fails if level == "FAIL" else _warns
    return [f for f in picker(lint_brain(root)) if f.rule == "symlink-broken"]


def test_lint_brain_silent_on_kit_alias_but_warns_on_other_dangling_symlinks(tmp_path: Path) -> None:
    # The supported layout: a COMMITTED .claude/skills -> ../.agents/skills alias over a gitignored
    # local kit install. It dangles in a fresh checkout and the host skips it, so it is silent; any
    # other link onto an untracked target still WARNs.
    _git_brain(tmp_path)
    _write(tmp_path / ".agents/skills/local/SKILL.md", "---\ndescription: local\n---\n")
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude/skills").symlink_to("../.agents/skills")
    (tmp_path / "kit-docs").symlink_to(".agents/skills")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".claude/skills", "kit-docs"], check=True)

    assert _symlink_findings(tmp_path, "FAIL") == []
    warns = _symlink_findings(tmp_path, "WARN")
    assert [f.path for f in warns] == ["kit-docs"]
    assert "not tracked" in warns[0].message
    assert "dangling in checkouts" in warns[0].message


def test_lint_brain_allows_tracked_symlink_to_tracked_dir(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    (tmp_path / "shortcuts").mkdir()
    (tmp_path / "shortcuts/cases").symlink_to("../skills/cases")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    assert _symlink_findings(tmp_path, "FAIL") == []
    assert _symlink_findings(tmp_path, "WARN") == []


def test_lint_brain_fails_absolute_symlink(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    (tmp_path / "abs").symlink_to("/etc/hosts")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    findings = _symlink_findings(tmp_path, "FAIL")

    assert [f.path for f in findings] == ["abs"]
    assert "absolute" in findings[0].message


def test_lint_brain_fails_symlink_escaping_root(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    (tmp_path / "outside").symlink_to("../elsewhere")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)

    findings = _symlink_findings(tmp_path, "FAIL")

    assert [f.path for f in findings] == ["outside"]
    assert "escapes the brain root" in findings[0].message


def test_lint_brain_without_git_falls_back_to_disk_existence(tmp_path: Path) -> None:
    _seed_brain(tmp_path)  # no .git → existence check only
    (tmp_path / "dangling").symlink_to("skills/missing")
    (tmp_path / "ok").symlink_to("skills/cases")

    assert _symlink_findings(tmp_path, "FAIL") == []
    warns = _symlink_findings(tmp_path, "WARN")

    assert [f.path for f in warns] == ["dangling"]
    assert "does not exist" in warns[0].message


def test_lint_brain_warns_on_unknown_doc_surfaces(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "skills/account-chat/SKILL.md",
           "---\ndescription: Account chat\ninclude_in: [principal]\nsurfaces: [chat, dashboard_chat]\n---\n")
    _write(tmp_path / "skills/commented/SKILL.md",
           "---\ndescription: C\nsurfaces: [chat] # customer chat only\n---\n")
    _write(tmp_path / "skills/nested/SKILL.md", "---\ndescription: N\nmetadata:\n  surfaces: [bogus]\n---\n")
    _write(tmp_path / "skills/long/SKILL.md",
           "---\ndescription: L\n# " + "c" * 8200 + "\nsurfaces: [chat]\n---\n")
    _write(tmp_path / "skills/typo/SKILL.md", "---\ndescription: Typo\nsurfaces: [chat, gmail]\n---\n")
    _write(tmp_path / "skills/broken/SKILL.md", "---\ndescription: B\nsurfaces: [chat\n---\n")

    findings = [f for f in lint_brain(tmp_path) if f.rule == "doc-surfaces"]

    assert [(f.path, f.level) for f in findings] == [
        ("skills/broken/SKILL.md", "WARN"),
        ("skills/long/SKILL.md", "WARN"),
        ("skills/typo/SKILL.md", "WARN"),
    ]
    assert "not valid YAML" in findings[0].message
    assert "exceeds 8192 bytes" in findings[1].message
    assert "stays visible on every surface" in findings[2].message


def _doc(tags: str, kb: float) -> str:
    front = f"---\ndescription: D\ninclude_in: {tags}\n---\n" if tags else "---\ndescription: D\n---\n"
    return front + "x" * int(kb * 1024)


def test_doc_size_skips_chat_gallery_and_mirrored_corpus(tmp_path: Path) -> None:
    """The chat gallery is parsed by the host, not read with bash; a mirrored help-centre article or
    video transcript (`source_url:` / `video_url:`) is generated content the author cannot split."""
    _seed_brain(tmp_path)
    _write(tmp_path / "chat_inspiration.md", "---\nsurfaces: [chat]\n---\n" + "x" * 17 * 1024)
    _write(tmp_path / "knowledge/hc/a.md", "---\nsource_url: https://x/a\n---\n" + "x" * 17 * 1024)
    _write(tmp_path / "knowledge/hc/_videos/v.md", "---\nvideo_url: https://y/v\n---\n" + "x" * 17 * 1024)
    _write(tmp_path / "knowledge/own.md", _doc("", 17))

    findings = [f for f in lint_brain(tmp_path) if f.rule == "doc-size"]

    assert [f.path for f in findings] == ["knowledge/own.md"]


def test_lint_brain_core_skips_run_hidden_paths_except_dead_tags(tmp_path: Path) -> None:
    """One filter for CLI, pytest tier and publish gate: runtime-audience rules ignore run-hidden
    paths; `frontmatter-scope` still says a tag there is never read."""
    _seed_brain(tmp_path)
    _write(tmp_path / ".replypenignore", "/maint/\n/skills/old/\n")
    _write(tmp_path / "skills/old/SKILL.md", "no frontmatter\n")
    _write(tmp_path / "maint/big.md", _doc("", 17))
    _write(tmp_path / "maint/tagged.md", "---\ninclude_in: [agent]\n---\nx\n")

    found = {(f.path, f.rule) for f in lint_brain(tmp_path)}

    assert found == {("maint/tagged.md", "frontmatter-scope")}


def test_lint_brain_judges_internal_dir_without_an_ignore_rule(tmp_path: Path) -> None:
    """`_internal/` is hidden only when an ignore rule says so; otherwise the host materializes it and
    rejects an absolute symlink there (treeview visible.go), so the lint must still FAIL it."""
    _seed_brain(tmp_path)
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal/unsafe").symlink_to("/tmp/missing")

    fails = [(f.path, f.rule) for f in _fails(lint_brain(tmp_path))]

    assert fails == [("_internal/unsafe", "symlink-broken")]


def test_lint_brain_warns_on_hard_loads_over_per_file_cap(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "docs/schema.md", _doc("[agent]", 17))
    _write(tmp_path / "docs/fits.md", _doc("[agent]", 15.9))
    _write(tmp_path / "docs/map.md", _doc("[grounding, agent]", 12))
    _write(tmp_path / "triage.md", "---\ndescription: T\ninclude_in:\n  - triage # gate\n---\n" + "x" * 9000)

    findings = [f for f in lint_brain(tmp_path) if f.rule == "doc-size"]

    assert {(f.path, f.level) for f in findings} == {
        ("docs/schema.md", "WARN"), ("docs/map.md", "WARN"), ("triage.md", "WARN")}
    by_path = {f.path: f.message for f in findings}
    assert "16.0 KB `agent` cap" in by_path["docs/schema.md"]
    assert "8.0 KB `grounding` cap" in by_path["docs/map.md"] and "`agent`" not in by_path["docs/map.md"]
    assert "`triage` cap" in by_path["triage.md"]


def test_lint_brain_warns_when_agent_total_cap_cuts_later_docs(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    for name in ("a", "b", "c", "d"):
        _write(tmp_path / f"docs/{name}.md", _doc("[agent]", 15))
    _write(tmp_path / "skills/chat/SKILL.md", _doc("[principal]", 1))

    findings = [f for f in lint_brain(tmp_path) if f.rule == "doc-size"]

    assert [f.path for f in findings] == ["docs/d.md", "skills/chat/SKILL.md"]
    assert "48.0 KB `agent` total" in findings[0].message


def test_lint_brain_advises_on_large_on_demand_docs_and_agents_md(tmp_path: Path) -> None:
    _seed_brain(tmp_path)
    _write(tmp_path / "AGENTS.md", _doc("[agent]", 17))
    _write(tmp_path / "playbooks/big.md", _doc("", 17))
    _write(tmp_path / "playbooks/ok.md", _doc("", 15))
    _write(tmp_path / ".agents/skills/kit/SKILL.md", _doc("", 40))

    findings = [f for f in lint_brain(tmp_path) if f.rule == "doc-size"]

    assert [f.path for f in findings] == ["AGENTS.md", "playbooks/big.md"]
    assert "pasted whole into every run" in findings[0].message
    assert "will not be read at once" in findings[1].message
    assert not _fails(findings)
    report = format_report(findings)
    assert report.count("fix: keep a lean core") == 1


# ---- frontmatter the host never reads -------------------------------------------------------------

_FM_RULES = {"frontmatter-scope", "frontmatter-unread", "frontmatter-redundant", "include-in-value"}


def _fm(root: Path, kind: str | None = None) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = {}
    for f in lint_brain(root, kind):
        if f.rule in _FM_RULES:
            out.setdefault(f.path, []).append(f)
    return out


def test_mirror_include_in_only_read_at_root_and_scan_dirs(tmp_path: Path) -> None:
    tag = "---\ninclude_in: [agent]\n---\nbody\n"
    for rel in ("AGENTS.md", "docs/schema.md", "doc/a.md", ".claude/x.md", ".agents/y.md",
                "skills/starting-context/SKILL.md", "app/models/README.md", "notes/deep/map.md"):
        _write(tmp_path / rel, tag)

    found = _fm(tmp_path, "mirror")

    assert sorted(found) == ["app/models/README.md", "notes/deep/map.md"]
    [finding] = found["notes/deep/map.md"]
    assert (finding.level, finding.rule) == ("FAIL", "frontmatter-scope")
    assert "only read in a mirror at the repo root *.md or under" in finding.message
    assert all(f"{d}/" in finding.message for d in MIRROR_SCAN_DIRS)
    # The same files in a brain are all read (its root AGENTS.md is pasted anyway, hence redundant).
    assert [f.rule for fs in _fm(tmp_path, "brain").values() for f in fs] == ["frontmatter-redundant"]


def test_triage_tag_only_read_in_project_brain(tmp_path: Path) -> None:
    _write(tmp_path / "triage.md", "---\ninclude_in: [triage, grounding]\n---\nskip bots\n")

    assert _fm(tmp_path, "brain") == {}
    for kind, where in (("tenant", "tenant overlay"), ("mirror", "mirror")):
        [finding] = _fm(tmp_path, kind)["triage.md"]
        assert finding.level == "FAIL" and where in finding.message
        assert "only read from the project brain" in finding.message


def test_mirror_surfaces_are_never_honored(tmp_path: Path) -> None:
    _write(tmp_path / "docs/chat.md", "---\nsurfaces: [chat]\n---\nchat only\n")

    assert _fm(tmp_path, "brain") == {}
    [finding] = _fm(tmp_path, "mirror")["docs/chat.md"]
    assert finding.rule == "frontmatter-scope" and "`surfaces:` is only honored" in finding.message
    assert not [f for f in lint_brain(tmp_path, "mirror") if f.rule == "doc-surfaces"]


def test_tags_in_pruned_and_run_hidden_paths_are_never_read(tmp_path: Path) -> None:
    _git_brain(tmp_path)
    tag = "---\ninclude_in: [agent]\n---\nbody\n"
    _write(tmp_path / ".rootcause/notes.md", tag)
    _write(tmp_path / "_internal/plan.md", tag)
    _write(tmp_path / "_internal/untagged.md", "---\ndescription: |\n  x\n---\n")
    _write(tmp_path / "docs/visible.md", tag)
    _write(tmp_path / ".replypenignore", "/_internal/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A", "-f"], check=True)

    found = _fm(tmp_path, "brain")

    assert sorted(found) == [".rootcause/notes.md", "_internal/plan.md"]
    assert "skips that dir" in found[".rootcause/notes.md"][0].message
    assert "run-hidden path" in found["_internal/plan.md"][0].message


def test_tags_on_already_pasted_agents_md_are_redundant(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "---\ninclude_in: [grounding, agent, triage]\n---\nmap\n")

    [brain] = _fm(tmp_path, "brain")["AGENTS.md"]
    assert (brain.level, brain.rule) == ("WARN", "frontmatter-redundant")
    assert brain.message.endswith("drop `agent`, `grounding`")
    # Tenant: grounding is the useful tag there, agent pastes it twice; triage is never read.
    tenant = {f.rule: f for f in _fm(tmp_path, "tenant")["AGENTS.md"]}
    assert tenant["frontmatter-redundant"].message.endswith("pastes it twice — drop `agent`")
    assert tenant["frontmatter-scope"].level == "FAIL"
    # Mirror: a tagged root AGENTS.md is exactly how a mirror's instructions get loaded.
    assert [f.rule for f in _fm(tmp_path, "mirror")["AGENTS.md"]] == ["frontmatter-scope"]
    _write(tmp_path / "AGENTS.md", "---\ninclude_in: [grounding, agent]\n---\nmap\n")
    assert _fm(tmp_path, "mirror") == {}


def test_unknown_include_in_role_fails_with_hint(tmp_path: Path) -> None:
    _write(tmp_path / "docs/a.md", "---\ninclude_in: [agents, Grounding, kb]\n---\nx\n")
    _write(tmp_path / "docs/ok.md", "---\ninclude_in:\n  - agent  # writer\n  - \"principal\"\n---\nx\n")

    found = _fm(tmp_path)

    assert list(found) == ["docs/a.md"]
    messages = sorted(f.message for f in found["docs/a.md"])
    assert all(f.rule == "include-in-value" and f.level == "FAIL" for f in found["docs/a.md"])
    assert "did you mean `agent`?" in messages[1] and "did you mean `grounding`?" in messages[0]
    assert "'kb'; the host only reads triage, grounding, agent, principal" in messages[2]


def test_frontmatter_forms_the_host_line_scan_misses(tmp_path: Path) -> None:
    cases = {
        "bom.md": "\ufeff---\ninclude_in: [agent]\n---\nx\n",
        "blank.md": "\n---\ndescription: D\ninclude_in: [agent]\n---\nx\n",
        "nested.md": "---\nmeta:\n  include_in: [agent]\n---\nx\n",
        "dash.md": "---\ninclude-in: [agent]\n---\nx\n",
        "surface.md": "---\nsurface: [chat]\n---\nx\n",
        "multiline.md": "---\ninclude_in: [agent,\n  grounding]\n---\nx\n",
        "exclude.md": "---\nexclude_in: [all]\n---\nx\n",
        "dup.md": "---\ndescription: First\ndescription: Second\n---\nx\n",
        "huge.md": "---\ninclude_in: [agent]\nnotes: " + "y" * 9000 + "\n---\nx\n",
        "hugedesc.md": "---\ndescription: >-\n  Open when a refund fails\nnotes: " + "y" * 9000 + "\n---\nx\n",
    }
    for name, text in cases.items():
        _write(tmp_path / "notes" / name, text)
    # Forms the host DOES read: no findings.
    _write(tmp_path / "notes/fine.md",
           "---\r\ndescription: 'Open when: refunds'\r\ninclude_in: agent\r\nsurfaces: [email]\r\n---\r\nx\r\n")
    _write(tmp_path / "notes/plain.md", "# no frontmatter\ninclude_in: [agent]\n")
    # Every YAML string form is read whole now: block scalars, wrapped and late descriptions are fine.
    _write(tmp_path / "notes/folded.md", "---\ndescription: >-\n  Open when a refund fails\n---\nx\n")
    _write(tmp_path / "notes/wrapped.md", "---\ndescription: Open when a refund\n  fails twice\n---\nx\n")
    _write(tmp_path / "notes/late.md", "---\nnotes: " + "y" * 2100 + "\ndescription: Late one\n---\nx\n")
    # One-line description in an over-8 KiB block: the legacy line-scan fallback still renders it.
    _write(tmp_path / "notes/hugeline.md", "---\ndescription: Open when refunds\nnotes: " + "y" * 9000 + "\n---\nx\n")
    # SKILL.md descriptions are owned by `description-missing`; no second finding here.
    _write(tmp_path / "skills/x/SKILL.md", "---\ndescription: |\n  multi\n---\n")

    found = _fm(tmp_path)

    assert sorted(found) == sorted(f"notes/{n}" for n in cases)
    assert all(f.rule == "frontmatter-unread" and f.level == "FAIL" for fs in found.values() for f in fs)
    msg = {p.split("/")[1]: " | ".join(f.message for f in fs) for p, fs in found.items()}
    assert "does not start at byte 0" in msg["bom.md"] and "`include_in`" in msg["bom.md"]
    assert "`description`, `include_in`" in msg["blank.md"]
    assert "indented" in msg["nested.md"]
    assert "`include-in:` is not read" in msg["dash.md"] and "`surface:` is not read" in msg["surface.md"]
    assert "['agent'], YAML as ['agent', 'grounding']" in msg["multiline.md"]
    assert "`exclude_in` is never read" in msg["exclude.md"]
    assert "declared more than once" in msg["dup.md"] and "'First'" in msg["dup.md"]
    assert "closes past the first 8192 bytes" in msg["huge.md"]
    assert "closes past the first 8192 bytes" in msg["hugedesc.md"] and "no gloss" in msg["hugedesc.md"]
    # A mirror's descriptions are the customer's, not our contract: WARN, not FAIL.
    assert [f.level for f in _fm(tmp_path, "mirror")["notes/hugedesc.md"]] == ["WARN"]


def test_detect_repo_kind(tmp_path: Path) -> None:
    brain, tenant = tmp_path / "org/brain", tmp_path / "org/brain-tenant-x"
    mirror, other = tmp_path / "customer/app", tmp_path / "customer/unrelated"
    for repo in (brain, tenant, mirror, other):
        repo.mkdir(parents=True)
    _write(brain / ".rootcause.toml", 'project = "p"\n\n[mirrors]\napp = "../../customer/app"\n')
    _write(tenant / ".rootcause.toml", 'project = "p"\ntenant = "x"\n')

    assert detect_repo_kind(brain)[0] == "brain"
    assert detect_repo_kind(tenant)[0] == "tenant"
    assert detect_repo_kind(mirror) == ("mirror", "listed in brain/.rootcause.toml [mirrors]")
    assert detect_repo_kind(other)[0] == "brain"
    # A source repo may carry `project =` only to scope `rc`; being someone's mirror wins.
    _write(mirror / ".rootcause.toml", 'project = "p-staff"\n')
    assert detect_repo_kind(mirror)[0] == "mirror"
    # lint_brain detects on its own when no kind is passed.
    _write(mirror / "lib/notes.md", "---\ninclude_in: [agent]\n---\nx\n")
    assert [f.rule for f in lint_brain(mirror) if f.rule in _FM_RULES] == ["frontmatter-scope"]


def test_run_hidden_paths_need_no_git(tmp_path: Path) -> None:
    """Publish/canary lint mounts a worktree alone at /brain: its `.git` FILE points at an unmounted
    gitdir, so git is unusable. Hidden docs must still be recognised from the control files."""
    _seed_brain(tmp_path)
    _write(tmp_path / ".git", "gitdir: /nonexistent/worktrees/canary-x\n")
    _write(tmp_path / ".rcignore", "private/\n")
    _write(tmp_path / "private/draft.md", "---\ndescription: >-\n  folded\n---\nx\n")
    _write(tmp_path / "private/tagged.md", "---\ninclude_in: [agent]\n---\nx\n")

    found = _fm(tmp_path, "brain")

    assert list(found) == ["private/tagged.md"]
    assert "run-hidden path" in found["private/tagged.md"][0].message


def test_run_hidden_matcher_follows_gitignore_semantics(tmp_path: Path) -> None:
    from lib.brain_lint import run_hidden

    _write(tmp_path / ".replypenignore",
           "# comment\n/tests/\n*.draft.md\n!keep.draft.md\nnotes/**\n/skills/**/tests/\n\\#hash.md\n")
    _write(tmp_path / ".rcignore", "build\n")
    hidden = run_hidden(tmp_path)

    for rel in ("tests/a.md", "deep/x.draft.md", "notes/a/b.md", "skills/s/tests/t.md", "#hash.md",
                "build/x.md", "a/build", ".git/x", "a/.gitignore", "node_modules/p/README.md", ".rcignore"):
        assert hidden(rel), rel
    for rel in ("deep/tests/a.md", "keep.draft.md", "notes", "skills/s/SKILL.md", "docs/a.md", "comment"):
        assert not hidden(rel), rel


def test_nested_key_inside_block_scalar_is_text(tmp_path: Path) -> None:
    _write(tmp_path / "docs/a.md", "---\ndescription: D\nexample: |\n  include_in: [agent]\n---\nx\n")
    assert _fm(tmp_path, "brain") == {}


def test_line_scan_checks_survive_unrelated_yaml_errors(tmp_path: Path) -> None:
    _write(tmp_path / "docs/a.md", "---\nnotes: invalid: yaml\ninclude_in: [agents]\n---\nx\n")
    _write(tmp_path / "lib/b.md", "---\nnotes: invalid: yaml\ninclude_in: [agent]\n---\nx\n")

    brain = _fm(tmp_path, "brain")
    assert [f.rule for f in brain["docs/a.md"]] == ["include-in-value"]
    assert "lib/b.md" not in brain
    assert [f.rule for f in _fm(tmp_path, "mirror")["lib/b.md"]] == ["frontmatter-scope"]


def test_duplicate_description_on_owned_files(tmp_path: Path) -> None:
    """SKILL.md/runbooks are judged by `description-missing` for an unread value, but a second
    `description:` the host ignores is reported like on any other doc — never twice."""
    dup = "---\ndescription: First routing text\ndescription: Second routing text\n---\n"
    _write(tmp_path / "skills/dup/SKILL.md", dup)
    _write(tmp_path / "skills/cases/dup.md", dup)
    _write(tmp_path / "skills/nulldup/SKILL.md", "---\ndescription: ~\ndescription: Second\n---\n")

    found = {f.path: f for f in lint_brain(tmp_path, "brain") if f.rule == "frontmatter-unread"}
    assert "declared more than once" in found["skills/dup/SKILL.md"].message
    assert "declared more than once" in found["skills/cases/dup.md"].message
    assert "skills/nulldup/SKILL.md" not in found  # already FAILs description-missing
    assert [f.rule for f in lint_brain(tmp_path, "brain") if f.path == "skills/nulldup/SKILL.md"] == [
        "description-missing"]


def test_plain_scalars_resolve_like_the_host(tmp_path: Path) -> None:
    """YAML 1.2 (yaml.v3) typing, not PyYAML's 1.1: `on`/`yes`/`OFF` are strings, `1e3` a float."""
    for name, value in {"on": "on", "yes": "yes", "off": "OFF", "quoted": "'42'"}.items():
        _write(tmp_path / f"skills/{name}/SKILL.md", f"---\ndescription: {value}\n---\n")
    for name, value in {"float": "1e3", "hex": "0x1F", "bool": "true"}.items():
        _write(tmp_path / f"skills/{name}/SKILL.md", f"---\ndescription: {value}\n---\n")

    fails = {f.path for f in _fails(lint_brain(tmp_path, "brain"))}
    assert fails == {"skills/float/SKILL.md", "skills/hex/SKILL.md", "skills/bool/SKILL.md"}
