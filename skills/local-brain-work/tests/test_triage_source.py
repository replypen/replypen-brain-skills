from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import triage_source  # noqa: E402
from test_brain_hygiene import make_brain  # noqa: E402

DOC = "---\ninclude_in: [triage, agent]\n---\n# Triage\n"
BLOCK_DOC = "---\ninclude_in:\n  - triage\n---\n# Triage\n"


@pytest.fixture
def policies(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Database policy per scope key: None = project, else the tenant slug."""
    db: dict = {}
    monkeypatch.setattr(triage_source, "db_policy", lambda project, tenant, root: db.get(tenant, ""))
    return db


def test_project_doc_and_project_policy_flagged(tmp_path: Path, policies: dict) -> None:
    root = make_brain(tmp_path / "b", {".replypen.toml": 'project = "p"\n', "triage.md": DOC})
    policies[None] = "Skip newsletters."
    out = triage_source.check(root)
    assert len(out) == 1 and "two triage sources at project scope" in out[0] and "triage.md" in out[0]


def test_block_list_frontmatter_counts(tmp_path: Path, policies: dict) -> None:
    root = make_brain(tmp_path / "b", {".replypen.toml": 'project = "p"\n', "rules/skip.md": BLOCK_DOC})
    policies[None] = "x"
    assert "rules/skip.md" in triage_source.check(root)[0]


def test_one_source_per_scope_is_quiet(tmp_path: Path, policies: dict) -> None:
    root = make_brain(tmp_path / "b", {".replypen.toml": 'project = "p"\n', "triage.md": DOC})
    policies["acme"] = "tenant-only rule"  # another scope: not compared
    assert triage_source.check(root) == []


def test_tenant_overlay_compares_its_own_scope_only(tmp_path: Path, policies: dict) -> None:
    marker = 'project = "p"\ntenant = "acme"\n'
    policies[None] = "project policy: never compared with an overlay"
    quiet = make_brain(tmp_path / "quiet", {".replypen.toml": marker, "AGENTS.md": "# overlay\n"})
    assert triage_source.check(quiet) == []

    tagged = make_brain(tmp_path / "tagged", {".replypen.toml": marker, "triage.md": DOC})
    out = triage_source.check(tagged)
    assert len(out) == 1 and "reads only the project brain" in out[0]
    policies["acme"] = "tenant policy"
    out = triage_source.check(tagged)
    assert len(out) == 2 and "two triage sources at tenant acme scope" in out[1]
