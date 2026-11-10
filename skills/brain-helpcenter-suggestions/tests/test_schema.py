"""Fixture round-trip, one table-driven case per validator rule, and the file layer.

    cd skills/brain-helpcenter-suggestions
    uv run --with pydantic --with pyyaml --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate  # noqa: E402
from assemble import assemble  # noqa: E402
from schema import Suggestions, data_errors, load, rank, relabel, soft_warnings, summary  # noqa: E402

FIX = ROOT / "fixtures"
EVIDENCE = FIX / "evidence.json"
ARTICLES = FIX / "raw" / "articles"
OUT = FIX / "out"


def _dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _errors(tmp_path: Path, ev: dict, sug: dict, fix_sha: bool = True, articles: Path | None = None) -> list[str]:
    ep = tmp_path / "evidence.json"
    _dump(ep, ev)
    if fix_sha:
        sug["evidence_sha256"] = hashlib.sha256(ep.read_bytes()).hexdigest()
    return data_errors(sug, ep, articles_dir=articles or ARTICLES)


@pytest.fixture
def pair() -> tuple[dict, dict]:
    return (
        json.loads(EVIDENCE.read_text(encoding="utf-8")),
        json.loads((OUT / "suggestions.json").read_text(encoding="utf-8")),
    )


def _out_copy(tmp_path: Path) -> Path:
    """The whole collect+agent tree, so file-level tests can break one file."""
    shutil.copytree(OUT, tmp_path / "out")
    shutil.copytree(ARTICLES, tmp_path / "raw" / "articles")
    shutil.copy(EVIDENCE, tmp_path / "evidence.json")
    return tmp_path / "out"


def _run(target: Path) -> int:
    argv = sys.argv
    sys.argv = ["validate.py", str(target)]
    try:
        return validate.main()
    finally:
        sys.argv = argv


# --------------------------------------------------------------- the fixture

def test_fixture_validates() -> None:
    assert data_errors(json.loads((OUT / "suggestions.json").read_text()), EVIDENCE, articles_dir=ARTICLES) == []
    sug, ev = load(OUT / "suggestions.json", EVIDENCE, articles_dir=ARTICLES)
    tiles = summary(ev, sug)
    assert tiles["scanned"] == 12 and tiles["recipe"] == 2 and tiles["missing"] == 1
    assert tiles["per_tenant"] == [("acmecamp", 7, 6), ("happykids", 5, 4)]
    ranked = [s.id for _, s, _ in rank(sug.suggestions, sug.classification)]
    assert ranked[0] == "S3"                                   # partial 2 + missing 3 beats the rest
    scores = {s.id: sc for _, s, sc in rank(sug.suggestions, sug.classification)}
    assert scores["S1"] == 4                                   # two recipe conversations, weight 2 each
    assert any("only one conversation" in w for w in soft_warnings(sug, ev))


def test_assembled_json_is_stable(tmp_path: Path) -> None:
    out = _out_copy(tmp_path)
    written = out / "suggestions.json"
    written.unlink()
    assert _run(out) == 0
    assert json.loads(written.read_text()) == json.loads((OUT / "suggestions.json").read_text())


# ------------------------------------------------------------------ mutations

def _m_unknown_key(ev, sug):
    sug["headlin"] = sug.pop("headline")


def _m_bad_enum(ev, sug):
    sug["suggestions"][0]["kind"] = "renmae"


def _m_unknown_conversation(ev, sug):
    sug["suggestions"][0]["evidence"][0]["conversation_id"] = "run:9999"


def _m_not_verbatim(ev, sug):
    sug["suggestions"][0]["evidence"][0]["quote"] = "Hoe maak ik een lijst van inschrijvingen?"


def _m_unclassified(ev, sug):
    sug["classification"] = [c for c in sug["classification"] if c["conversation_id"] != "run:a3b4c5d6"]


def _m_duplicate_classification(ev, sug):
    sug["classification"].append(copy.deepcopy(sug["classification"][0]))


def _m_topics_missing(ev, sug):
    sug["classification"][0]["topics"] = []


def _m_old_topic_key(ev, sug):
    c = sug["classification"][3]
    c["topic"] = c.pop("topics")


def _m_unknown_role_quote(ev, sug):
    """run:70819aa3 carries an unknown-role turn: those words are not the customer's."""
    sug["suggestions"][5]["evidence"][0]["quote"] = "een schermafbeelding van de cabine-instellingen"


def _m_new_with_target(ev, sug):
    sug["suggestions"][0]["target_articles"] = ["A1"]


def _m_rewrite_without_edit(ev, sug):
    sug["suggestions"][1]["edit"] = None


def _m_edit_on_retitle(ev, sug):
    sug["suggestions"][4]["edit"] = {"old": "Online boekingsmodule"}


def _m_edit_two_keys(ev, sug):
    sug["suggestions"][2]["edit"] = {"after": "## Uitzonderingen", "before": "## Veelgestelde vragen"}


def _m_edit_old_not_verbatim(ev, sug):
    sug["suggestions"][1]["edit"]["old"] = "Je kan een cabine altijd sluiten.\n"


def _m_edit_anchor_missing(ev, sug):
    sug["suggestions"][2]["edit"]["after"] = "## Uitzonderlijke sluitingsdagen"


def _m_flags_on_new(ev, sug):
    sug["suggestions"][0]["flags"] = ["contradiction"]


def _m_em_dash(ev, sug):
    sug["suggestions"][1]["why"] = "Het artikel spreekt zichzelf tegen — support zegt iets anders."


def _m_retitle_same_title(ev, sug):
    sug["suggestions"][4]["title"] = "Online boekingsmodule"


def _m_alias_present(ev, sug):
    sug["suggestions"][3]["aliases"] = ["Facturatie"]


def _m_merge_destination(ev, sug):
    sug["suggestions"][5]["destination"] = "A3"


def _m_delete_no_destination(ev, sug):
    sug["suggestions"][6]["destination"] = None


def _m_brain_target(ev, sug):
    sug["suggestions"][3]["target_articles"] = ["A5"]


def _m_no_url(ev, sug):
    for c in ev["conversations"]:
        if c["id"] == "run:1a2b3c4d":
            c["url"] = None


def _m_answered_evidence(ev, sug):
    sug["classification"][0]["verdict"] = "answered"


def _m_seed_reply_draft(ev, sug):
    sug["suggestions"][1]["seed_reply"] = "run:2b3c4d5e"


CASES = [
    ("unknown key", _m_unknown_key, "headlin:", "unknown key"),
    ("bad enum", _m_bad_enum, "suggestions[0].kind:", "not in the allowed list"),
    ("unknown conversation", _m_unknown_conversation, "suggestions[0].evidence[0].conversation_id:", "unknown 'run:9999'"),
    ("non-verbatim quote", _m_not_verbatim, "suggestions[0].evidence[0].quote:", "copy an unchanged substring"),
    ("unclassified conversation", _m_unclassified, "classification:", "run:a3b4c5d6"),
    ("duplicate classification", _m_duplicate_classification, "classification[12].conversation_id:", "exactly once"),
    ("topics required", _m_topics_missing, "classification[0].topics:", "gap verdict"),
    ("old topic key", _m_old_topic_key, "classification[3].topic:", "unknown key"),
    ("unknown-role quote", _m_unknown_role_quote, "suggestions[5].evidence[0].quote:", "unknown-role/agent turn in run:70819aa3"),
    ("new takes no target", _m_new_with_target, "suggestions[0].target_articles:", "no target"),
    ("rewrite needs an edit", _m_rewrite_without_edit, "suggestions[1].edit:", "one of old / after / before"),
    ("edit only on rewrite", _m_edit_on_retitle, "suggestions[4].edit:", "only kind rewrite"),
    ("edit takes one key", _m_edit_two_keys, "suggestions[2].edit:", "set: after, before"),
    ("old must be verbatim", _m_edit_old_not_verbatim, "suggestions[1].edit.old:", "not found verbatim in raw/articles/A2.md"),
    ("anchor must exist", _m_edit_anchor_missing, "suggestions[2].edit.after:", "is not a line of raw/articles/A1.md"),
    ("flags need a rewrite", _m_flags_on_new, "suggestions[0].flags:", "only applies to a rewrite or a merge"),
    ("em dash is an error", _m_em_dash, "suggestions[1].why:", "rewrite that passage without the em dash"),
    ("retitle same title", _m_retitle_same_title, "suggestions[4].title:", "current title"),
    ("alias already present", _m_alias_present, "suggestions[3].aliases[0]:", "already on A3"),
    ("merge destination", _m_merge_destination, "suggestions[5].destination:", "one of target_articles"),
    ("delete destination", _m_delete_no_destination, "suggestions[6].destination:", "another existing article"),
    ("brain target", _m_brain_target, "suggestions[3].target_articles[0]:", "brain document"),
    ("evidence without url", _m_no_url, "suggestions[0].evidence[0].conversation_id:", "has no url"),
    ("evidence on answered", _m_answered_evidence, "suggestions[0].evidence[0].conversation_id:", "classified answered"),
    ("seed reply is a draft", _m_seed_reply_draft, "suggestions[1].seed_reply:", "no human reply"),
]


@pytest.mark.parametrize("name,mutate,prefix,hint", CASES, ids=[c[0] for c in CASES])
def test_mutation(tmp_path: Path, pair, name, mutate, prefix, hint) -> None:
    ev, sug = pair
    mutate(ev, sug)
    errors = _errors(tmp_path, ev, sug)
    assert any(e.startswith(prefix) and hint in e for e in errors), f"{name}: {errors}"


def test_ambiguous_anchor(tmp_path: Path, pair) -> None:
    """An anchor line that occurs twice cannot place the insert."""
    ev, sug = pair
    articles = tmp_path / "articles"
    articles.mkdir()
    body = (ARTICLES / "A1.md").read_text(encoding="utf-8")
    (articles / "A1.md").write_text(body + "\n## Uitzonderingen\n\nHerhaalde titel.\n", encoding="utf-8")
    errors = _errors(tmp_path, ev, sug, articles=articles)
    assert any(e.startswith("suggestions[2].edit.after:") and "matches 2 lines" in e for e in errors)


def test_missing_article_body(tmp_path: Path, pair) -> None:
    ev, sug = pair
    empty = tmp_path / "empty"
    empty.mkdir()
    errors = _errors(tmp_path, ev, sug, articles=empty)
    assert any("re-run collect.py" in e for e in errors)


def test_body_check_is_skipped_without_articles(tmp_path: Path, pair) -> None:
    """render.py loads the pair without the raw bodies; anchors are then not re-checked."""
    ev, sug = pair
    ep = tmp_path / "evidence.json"
    _dump(ep, ev)
    sug["evidence_sha256"] = hashlib.sha256(ep.read_bytes()).hexdigest()
    assert data_errors(sug, ep) == []


def test_soft_warnings_flag_en_dash_and_emoji(tmp_path: Path, pair) -> None:
    ev, sug = pair
    sug["suggestions"][0]["why"] = "Beheerders vragen dit wekelijks – en dat kost tijd. 🎯"
    sug["headline"] = "Vier gaten kosten tijd…"
    ep = tmp_path / "evidence.json"
    _dump(ep, ev)
    sug["evidence_sha256"] = hashlib.sha256(ep.read_bytes()).hexdigest()
    assert data_errors(sug, ep, articles_dir=ARTICLES) == []          # warnings never fail
    warnings = soft_warnings(Suggestions.model_validate(sug), _evidence(ep))
    assert any("en dash" in w for w in warnings)
    assert any("emoji" in w for w in warnings)
    assert any("ellipsis" in w for w in warnings)


def test_soft_warnings_flag_markdown_outside_the_canon(tmp_path: Path, pair) -> None:
    ev, sug = pair
    new = next(s for s in sug["suggestions"] if s["kind"] == "new")
    new["text"] = "| kolom | waarde |\n|--|--|\n\nEerste regel van de alinea\ntweede regel, zacht afgebroken.\n\n* ster-bullet"
    ep = tmp_path / "evidence.json"
    _dump(ep, ev)
    sug["evidence_sha256"] = hashlib.sha256(ep.read_bytes()).hexdigest()
    assert data_errors(sug, ep, articles_dir=ARTICLES) == []
    warnings = soft_warnings(Suggestions.model_validate(sug), _evidence(ep))
    assert any("GFM table" in w for w in warnings)
    assert any("soft-wrapped" in w for w in warnings)
    assert any("'*' or '+' bullet" in w for w in warnings)


def _evidence(path: Path):
    from schema import Evidence

    return Evidence.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_sha_mismatch(tmp_path: Path, pair) -> None:
    ev, sug = pair
    sug["evidence_sha256"] = "0" * 64
    errors = _errors(tmp_path, ev, sug, fix_sha=False)
    assert any(e.startswith("evidence_sha256:") and "collect printed" in e for e in errors)


def test_invalid_evidence_is_reported_first(tmp_path: Path, pair) -> None:
    ev, sug = pair
    ev["conversations"][0]["channel"] = "carrier-pigeon"
    errors = _errors(tmp_path, ev, sug)
    assert errors and errors[0].startswith("evidence.conversations[0].channel:")


# ----------------------------------------------------------------- file layer

def test_errors_are_prefixed_with_the_file(tmp_path: Path) -> None:
    out = _out_copy(tmp_path)
    path = out / "suggestions" / "S2.md"
    path.write_text(path.read_text().replace("target_articles: [A2]", "target_articles: [A1]"), encoding="utf-8")
    a = assemble(out)
    errors = relabel(data_errors(a.data, tmp_path / "evidence.json", articles_dir=tmp_path / "raw" / "articles", sources=a.sources), a.labels)
    assert any(e.startswith("suggestions/S2.md: edit.old: not found verbatim in raw/articles/A1.md") for e in errors)
    assert _run(out) == 1


def test_unknown_verdict_names_the_line(tmp_path: Path) -> None:
    out = _out_copy(tmp_path)
    tsv = out / "classification.tsv"
    tsv.write_text(tsv.read_text().replace("\tuncertain\t", "\treciep\t"), encoding="utf-8")
    a = assemble(out)
    assert any(e.startswith("classification.tsv:8: unknown verdict 'reciep'") for e in a.errors)


def test_em_dash_error_names_the_file_and_line(tmp_path: Path) -> None:
    out = _out_copy(tmp_path)
    path = out / "suggestions" / "S5.md"
    path.write_text(path.read_text().replace("de boekingsmodule.", "de boekingsmodule — zo heet het intern."), encoding="utf-8")
    a = assemble(out)
    errors = relabel(data_errors(a.data, tmp_path / "evidence.json", articles_dir=tmp_path / "raw" / "articles", sources=a.sources), a.labels)
    assert any(e.startswith("suggestions/S5.md: why: em dash (U+2014) on line 12:") for e in errors)


def test_quotes_may_hold_an_em_dash(tmp_path: Path) -> None:
    """Customer words are verbatim: the anti-AI rules cover the agent's own prose only."""
    out = _out_copy(tmp_path)
    ev = json.loads((tmp_path / "evidence.json").read_text())
    for c in ev["conversations"]:
        if c["id"] == "run:4d5e6f70":
            c["first_message"] = "online boeken werkt niet meer op mijn website — de knop doet niets"
    _dump(tmp_path / "evidence.json", ev)
    path = out / "suggestions" / "S5.md"
    path.write_text(path.read_text().replace(
        "quote: online boeken werkt niet meer op mijn website",
        "quote: online boeken werkt niet meer op mijn website — de knop doet niets"), encoding="utf-8")
    tsv = out / "classification.tsv"
    sha = hashlib.sha256((tmp_path / "evidence.json").read_bytes()).hexdigest()
    tsv.write_text(f"# evidence_sha256 {sha}\n" + tsv.read_text().split("\n", 1)[1], encoding="utf-8")
    a = assemble(out)
    assert relabel(data_errors(a.data, tmp_path / "evidence.json", articles_dir=tmp_path / "raw" / "articles", sources=a.sources), a.labels) == []


def test_pass_one_is_accepted(tmp_path: Path, capsys) -> None:
    """Classification only: the agent checkpoints before it writes any suggestion."""
    out = _out_copy(tmp_path)
    shutil.rmtree(out / "suggestions")
    (out / "headline.txt").unlink()
    (out / "suggestions.json").unlink()
    assert _run(out) == 0
    printed = capsys.readouterr().out
    assert "pass 1: classification only (12 conversations, 8 topics)" in printed
    data = json.loads((out / "suggestions.json").read_text())
    assert data["suggestions"] == [] and data["headline"] == "" and data["schema_version"] == 2


def test_headline_required_once_suggestions_exist(tmp_path: Path) -> None:
    out = _out_copy(tmp_path)
    (out / "headline.txt").unlink()
    assert any(e.startswith("headline.txt: missing") for e in assemble(out).errors)
