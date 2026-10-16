from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brain_paths  # noqa: E402

DOC = """See /brain/skills/a/SKILL.md and `/brain/skills/new.md`.
Gone: /brain/skills/gone.md, ignored /brain/skills/secret.md, hidden /brain/skills/draft.md.
Bold **/brain/skills/a/SKILL.md**; line ref /brain/skills/a/SKILL.md:12.
Skip /brain/skills/<name>/SKILL.md, /brain/..., /mirrors/{repo}/x, https://x.io/brain/y, ~/brain/z, /brain.
Code refs /brain/skills/a/SKILL.md:run and /brain/skills/a/SKILL.md:10-18; find /kb -path /kb/.git -prune.
Hedged: the overlay case under /brain/skills/cases/ when present.
Mirror /mirrors/common/terms.md and /mirrors/common/stale.md; remote /kb/owl/a.md; gated /skills/xlsx.
"""


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def make(tmp: Path) -> Path:
    brain, common = tmp / "org" / "brain", tmp / "org" / "common"
    for d in (brain, common):
        d.mkdir(parents=True)
        git(d, "init", "-q")
    (brain / ".rootcause.toml").write_text('project = "p"\n[mirrors]\n"common" = "../common"\n')
    (brain / ".gitignore").write_text("skills/secret.md\n")
    (brain / ".rcignore").write_text("skills/draft.md\n")
    (brain / "skills" / "a").mkdir(parents=True)
    for rel, text in {"AGENTS.md": DOC, "skills/a/SKILL.md": "x", "skills/secret.md": "x",
                      "skills/draft.md": "x", "journal/old.md": "/brain/skills/ancient.md"}.items():
        (brain / rel).parent.mkdir(parents=True, exist_ok=True)
        (brain / rel).write_text(text)
    git(brain, "add", "AGENTS.md", "skills/a/SKILL.md", "skills/draft.md", ".rcignore")
    (brain / "skills" / "new.md").write_text("untracked, not ignored: ships on the next commit")
    (common / "terms.md").write_text("x")
    git(common, "add", "terms.md")
    return brain


def test_local_first(tmp_path: Path) -> None:
    brain = make(tmp_path)
    lay, refs, _ = brain_paths.check(brain, None, remote=False)
    got = {r.path: r.verdict for r in refs}
    assert lay.kind == "brain" and lay.own == "/brain"
    assert got == {
        "/brain/skills/a/SKILL.md": "ok",
        "/brain/skills/new.md": "ok",
        "/brain/skills/gone.md": "missing",
        "/brain/skills/secret.md": "missing",      # gitignored: never ships
        "/brain/skills/draft.md": "missing",       # .rcignore: hidden from runs
        "/mirrors/common/terms.md": "ok",
        "/mirrors/common/stale.md": "unverified",  # local mirror lags prod: ask production
        "/kb/owl/a.md": "unverified",
        "/skills/xlsx": "unverified",
        "/brain/skills/cases": "missing",
    }
    by_path = {r.path: r for r in refs}
    assert brain_paths.level(by_path["/brain/skills/cases"]) == "NOTICE"   # hedged citation
    assert brain_paths.level(by_path["/brain/skills/gone.md"]) == "WARN"
    sites = {r.path: r.sites for r in refs}["/brain/skills/a/SKILL.md"]
    assert [n for _f, n in sites] == [1, 3, 3, 5, 5]


def test_mirror_and_overlay_kinds(tmp_path: Path) -> None:
    brain = make(tmp_path)
    common = brain.parent / "common"
    (common / "doc.md").write_text("Use /mirrors/common/terms.md and /brain/skills/a/SKILL.md, "
                                   "not /mirrors/common/nope.md.\n")
    git(common, "add", "doc.md")
    lay, refs, _ = brain_paths.check(common, None, remote=False)
    assert (lay.kind, lay.own) == ("mirror", "/mirrors/common")
    assert {r.path: r.verdict for r in refs} == {"/mirrors/common/terms.md": "ok",
                                                 "/brain/skills/a/SKILL.md": "ok",
                                                 "/mirrors/common/nope.md": "missing"}

    overlay = brain.parent / "brain-tenant-t"
    overlay.mkdir()
    git(overlay, "init", "-q")
    (overlay / ".rootcause.toml").write_text('project = "p"\ntenant = "t"\n')
    (overlay / "AGENTS.md").write_text("/tenant/AGENTS.md /brain/skills/a/SKILL.md /tenant/x.md\n")
    lay, refs, _ = brain_paths.check(overlay, None, remote=False)
    assert (lay.kind, lay.tenant) == ("tenant", "t")
    assert {r.path: r.verdict for r in refs} == {"/tenant/AGENTS.md": "ok",
                                                 "/brain/skills/a/SKILL.md": "ok",
                                                 "/tenant/x.md": "missing"}


def test_host_visibility_rules(tmp_path: Path) -> None:
    """Mirrors rootcause treeview: dotenv values, nested control files and dependency dirs are hidden
    whatever the rules say; `_internal/` only via rules; in-repo aliases follow their target."""
    brain = make(tmp_path)
    (brain / ".rcignore").write_text("skills/draft.md\nskills/hidden.md\n")
    for rel in (".env.production", ".env.example", "nested/.replypenignore", "_internal/readme.md",
                "node_modules/x.js", "skills/hidden.md"):
        (brain / rel).parent.mkdir(parents=True, exist_ok=True)
        (brain / rel).write_text("x")
    (brain / "alias").symlink_to("skills")
    (brain / "hidden-alias.md").symlink_to("skills/hidden.md")
    (brain / "AGENTS.md").write_text(
        "/brain/.env.production /brain/.env.example /brain/nested/.replypenignore "
        "/brain/_internal/readme.md /brain/node_modules/x.js /brain/alias/a/SKILL.md "
        "/brain/hidden-alias.md /brain/run_context.json\n")
    git(brain, "add", "-A")
    _lay, refs, _ = brain_paths.check(brain, ["AGENTS.md"], remote=False)
    assert {r.path: r.verdict for r in refs} == {
        "/brain/.env.production": "missing", "/brain/.env.example": "ok",
        "/brain/nested/.replypenignore": "missing", "/brain/_internal/readme.md": "ok",
        "/brain/node_modules/x.js": "missing", "/brain/alias/a/SKILL.md": "ok",
        "/brain/hidden-alias.md": "missing",  # host drops an alias whose target is hidden
    }  # /brain/run_context.json: host-generated at run time, never judged


def test_failed_tenant_blocks_universal_missing(tmp_path: Path, monkeypatch) -> None:
    """A tenant that errored may hold the path: never WARN 'missing on all' then."""
    import json
    report = {"project": "p", "paths": ["/kb/a.md", "/kb/b.md"], "results": [
        {"tenant": "t1", "entries": [{"path": "/kb/a.md", "kind": "missing"},
                                     {"path": "/kb/b.md", "kind": "file"}]},
        {"tenant": "t2", "error": "HTTP 502"}]}

    def fake_run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 4, json.dumps(report), "")
    monkeypatch.setattr(brain_paths.subprocess, "run", fake_run)
    refs = [brain_paths.Ref("/kb/a.md"), brain_paths.Ref("/kb/b.md")]
    note = brain_paths.resolve_remote(tmp_path, refs, "t1,t2", [None])
    assert [(r.verdict, brain_paths.level(r)) for r in refs] == [("unverified", "NOTICE"), ("ok", "")]
    assert refs[0].verdict == "unverified" and refs[1].verdict == "ok" and "t2" in note


def test_siblings_skip_the_temp_dir_but_not_nested_layouts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(brain_paths.tempfile, "gettempdir", lambda: str(tmp_path))
    org = tmp_path / "x" / "org"
    for d in (tmp_path / "worktree", tmp_path / "stray", org / "brain", org / "common"):
        d.mkdir(parents=True)
        (d / ".rootcause.toml").write_text('project = "p"\n')
    assert brain_paths._siblings(org / "brain") == [(org / "common").resolve()]
    assert brain_paths._siblings(tmp_path / "worktree") == []  # git_land's mkdtemp checkout
