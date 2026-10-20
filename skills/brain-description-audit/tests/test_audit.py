import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit  # noqa: E402


def test_replace_description_plain_and_drops_pin():
    raw = "---\nname: x\ndescription: Old line\ndescription_pin: abc\nother: 1\n---\n# Body\n"
    out = audit.replace_description(raw, 'Ouder vraagt: "waar is mijn attest?"')
    fm = yaml.safe_load(out.split("---")[1])
    assert fm == {"name": "x", "description": 'Ouder vraagt: "waar is mijn attest?"', "other": 1}
    assert out.endswith("---\n# Body\n")


def test_replace_description_folded_block():
    raw = "---\ndescription: >\n  folded one\n  folded two\nname: y\n---\nbody"
    out = audit.replace_description(raw, "New")
    assert "folded" not in out
    assert yaml.safe_load(out.split("---")[1]) == {"description": "New", "name": "y"}


def test_replace_description_requires_frontmatter():
    with pytest.raises(ValueError):
        audit.replace_description("# no frontmatter\n", "x")


@pytest.mark.parametrize("rewrite,rejected", [
    ("Dit artikel legt uit hoe je betaalt.", "boilerplate"),
    ("Learn how to restore a backup.", "boilerplate"),
    ("x" * 201, "over 200"),
    ("Betaal binnen 14 dagen via Bancontact.", ""),
])
def test_check_kb_rewrite_guards(rewrite, rejected):
    item = {"key": "k", "kind": "kb", "current": "Leer hoe je betaalt."}
    res = audit.check(item, {"verdict": "boilerplate", "reason": "r", "rewrite": rewrite})
    assert (rejected in res["rejected"]) if rejected else (res["rewrite"] == rewrite and not res["rejected"])


def test_check_ok_never_carries_rewrite():
    res = audit.check({"key": "k", "kind": "doc", "current": "c"}, {"verdict": "ok", "rewrite": "something"})
    assert res["verdict"] == "ok" and res["rewrite"] == ""


def test_collect_docs_brain_skips_journal_dotdirs_and_undescribed(tmp_path):
    (tmp_path / "skills" / "a").mkdir(parents=True)
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("---\nname: a\ndescription: Open when X\n---\n# A\n")
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal" / "j.md").write_text("---\ndescription: residue\n---\n")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "x.md").write_text("---\ndescription: local\n---\n")
    (tmp_path / "plain.md").write_text("# no description\n")
    items = audit.collect_docs(tmp_path, mirror=False)
    assert [i["key"] for i in items] == ["skills/a/SKILL.md"]
    assert items[0]["current"] == "Open when X"


def test_kb_dir_planes(tmp_path):
    art = "---\nid: \"7\"\nprovider: intercom\ntitle: Pay\nsummary: Leer hoe\nbody_hash: h7\n---\n# Pay\nBody\n"
    (tmp_path / "intercom" / "c").mkdir(parents=True)
    (tmp_path / "intercom" / "c" / "7-pay.md").write_text(art)
    (tmp_path / "intercom" / "INDEX.md").write_text("index")
    (tmp_path / "tenant" / "intercom").mkdir(parents=True)
    (tmp_path / "tenant" / "intercom" / "8.md").write_text(art.replace('"7"', '"8"'))
    project = audit.collect_kb_dir(tmp_path, "project")
    tenant = audit.collect_kb_dir(tmp_path, "tenant")
    assert [(i["key"], i["body_hash"]) for i in project] == [("project/intercom/7", "h7")]
    assert [i["key"] for i in tenant] == ["tenant/intercom/8"]
