"""Report rendering: bot-block goldens per kind, copy-handler targets, edit anchoring.

    cd skills/brain-helpcenter-suggestions
    uv run --with pydantic --with pyyaml --with pytest --no-project pytest tests -q

Run the module directly to drop a demo report in /tmp/r3-render for eyeballing:

    uv run --with pydantic --with pyyaml --no-project python tests/test_render.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render  # noqa: E402
from schema import (  # noqa: E402
    Article, Classification, Conversation, Edit, Evidence, Feed, Kb, Learning, Noise,
    Quote, Reply, Suggestion, Suggestions, Turn, Window,
)

A22_BODY = """---
id: 6a5a2fdba9c25bdda659637e
provider: helpscout
number: 19
collection_id: 5f1b0c1c
locale: nl
status: published
url: https://ibeauty.helpscoutdocs.com/article/19-cabines
---
# Cabines beheren

Je maakt een cabine aan via Instellingen > Cabines.

## Toestellen

Elk toestel hoort bij een cabine.

## Veelgestelde vragen

Kan ik een cabine sluiten terwijl ze in gebruik is?
"""


def _articles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "raw" / "articles"
    d.mkdir(parents=True)
    (d / "A22.md").write_text(A22_BODY, encoding="utf-8")
    return d


def _evidence() -> Evidence:
    articles = [
        Article(id="A20", home="kb", path="/kb/helpscout/cabines-basis.md", title="Cabines in het kort",
                url="https://ibeauty.helpscoutdocs.com/article/12-cabines-basis", provider="helpscout",
                provider_id="5f2c0000000000000000000a", number="12", collection_id="5f1b0c1c",
                locale="nl", status="published"),
        Article(id="A22", home="kb", path="/kb/helpscout/cabines.md", title="Cabines beheren",
                url="https://ibeauty.helpscoutdocs.com/article/19-cabines", provider="helpscout",
                provider_id="6a5a2fdba9c25bdda659637e", number="19", collection_id="5f1b0c1c",
                locale="nl", status="published", audience="customer"),
        Article(id="A23", home="kb", path="/kb/helpscout/agenda.md", title="Agenda en boekingen",
                url="https://ibeauty.helpscoutdocs.com/article/21-agenda", provider="helpscout",
                provider_id="7b6b3fecb0d36ceeb00b", number="21", collection_id="5f1b0c1c", locale="nl"),
        Article(id="A24", home="brain", path="/brain/policies/annulatie.md", title="Annulatiebeleid"),
    ]
    convs = [
        Conversation(id="hs:3438828375", url="https://secure.helpscout.net/conversation/3438828375",
                     channel="email", created_at="2026-08-04T09:12:00Z", tenant="ibeauty",
                     subject="Cabine sluiten", customer="Els",
                     first_message="kan ik een cabine sluiten terwijl ze gebruikt wordt",
                     reply=Reply(text="Dag Els,\n\nJe sluit een cabine via Instellingen > Cabines. "
                                      "Lopende afspraken blijven staan, nieuwe boekingen zijn geblokkeerd.\n\n"
                                      "Groeten,\nMarie-Lore",
                                 by="Marie-Lore Dutilleux", provenance="human")),
        Conversation(id="hs:3438363160", url="https://secure.helpscout.net/conversation/3438363160",
                     channel="email", created_at="2026-08-06T14:02:00Z", tenant="ibeauty",
                     first_message="Nele boekt mijn twee cabines dubbel, hoe koppel ik een toestel aan een cabine?",
                     later=[Turn(role="agent", text="Ik kijk het na.")],
                     reply=Reply(text="Koppel het toestel via de cabinefiche.", by="Marie-Lore Dutilleux",
                                 provenance="human")),
        Conversation(id="run:1a2b3c4d", url="https://app.replypen.example/runs/1a2b3c4d",
                     channel="chat", created_at="2026-08-11T08:40:00Z", tenant="yes_events",
                     first_message="maak een admin url die inschrijvingen filtert op deze week en locatie Mechelen",
                     reply=Reply(text="Hier is de link.", provenance="draft"),
                     linked_articles=["A23"]),
        Conversation(id="run:2b3c4d5e", url="https://app.replypen.example/runs/2b3c4d5e",
                     channel="chat", created_at="2026-08-12T11:05:00Z", tenant="yes_events",
                     first_message="rapport van de last-minute inschrijvingen, kan dat zelf?",
                     reply=Reply(text="Ja, via de filter.", provenance="draft")),
        Conversation(id="hs:3439000001", url="https://secure.helpscout.net/conversation/3439000001",
                     channel="email", created_at="2026-08-14T10:00:00Z", tenant="ibeauty",
                     first_message="waar vind ik het annulatiebeleid voor no-shows?"),
        Conversation(id="hs:3439000002", channel="email", created_at="2026-08-15T10:00:00Z",
                     tenant="ibeauty", first_message="ik vind niets over dubbele boekingen"),
        Conversation(id="hs:3439000003", channel="email", created_at="2026-08-16T10:00:00Z",
                     tenant="yes_events", first_message="factuur van augustus klopt niet",
                     noise=Noise(verdict="not_kb", reason="billing question, no article can answer it")),
        Conversation(id="hs:3439000004", channel="email", created_at="2026-08-17T10:00:00Z",
                     tenant="ibeauty", first_message="staat er ergens welke toestellen bij welke cabine horen?"),
    ]
    return Evidence(
        schema_version=2, project="ibeauty", collected_at="2026-09-08T07:00:00Z",
        window=Window(start="2026-08-01T00:00:00Z", end="2026-09-08T00:00:00Z", days=38),
        source="runs",
        coverage=[
            Feed(feed="email_runs", status="complete", scanned=121, retained=63),
            Feed(feed="chat_runs", status="complete", scanned=44, retained=18),
            Feed(feed="other_runs", status="complete",
                 reason="350 analysis runs seen, 317 of them failed, not part of this corpus"),
            Feed(feed="kb", status="complete"),
        ],
        kb=Kb(status="complete", scope="project", provider="helpscout",
              base_url="https://ibeauty.helpscoutdocs.com", articles=4, brain_docs=1,
              root="/kb/helpscout"),
        conversations=convs, articles=articles,
    )


def _suggestions() -> Suggestions:
    classification = [
        Classification(conversation_id="hs:3438828375", verdict="partial", topics=["cabine-sluiten"], article_ids=["A22"]),
        Classification(conversation_id="hs:3438363160", verdict="missing", topics=["cabine-toestel-koppelen"], article_ids=["A22"]),
        Classification(conversation_id="run:1a2b3c4d", verdict="recipe", topics=["inschrijvingen-filteren"], article_ids=["A23"]),
        Classification(conversation_id="run:2b3c4d5e", verdict="recipe", topics=["inschrijvingen-filteren"]),
        Classification(conversation_id="hs:3439000001", verdict="wrong_title", topics=["annulatie"], article_ids=["A24"]),
        Classification(conversation_id="hs:3439000002", verdict="wrong_title", topics=["dubbele-boeking"], article_ids=["A20", "A22"]),
        Classification(conversation_id="hs:3439000003", verdict="not_kb", topics=[]),
        Classification(conversation_id="hs:3439000004", verdict="missing", topics=["cabine-toestel-koppelen"], article_ids=["A22"]),
    ]
    suggestions = [
        Suggestion(
            id="S1", kind="new", topic="inschrijvingen-filteren",
            title="Zelf een lijst van inschrijvingen filteren", route="kb",
            destination=None, target_articles=[],
            text="# Zelf een lijst filteren\n\n1. Ga naar **Inschrijvingen**.\n"
                 "2. Zet de filter op *deze week* en kies je locatie.\n"
                 "3. Kopieer de url uit de adresbalk en deel ze.\n",
            why="Twee beheerders vroegen deze week om een lijst die ze zelf in twee klikken kunnen maken. "
                "Het help centrum legt de filter nergens uit.",
            evidence=[
                Quote(conversation_id="run:1a2b3c4d",
                      quote="maak een admin url die inschrijvingen filtert op deze week en locatie Mechelen"),
                Quote(conversation_id="run:2b3c4d5e",
                      quote="rapport van de last-minute inschrijvingen, kan dat zelf?"),
            ],
        ),
        Suggestion(
            id="S2", kind="rewrite", topic="cabine-sluiten", title="Cabines beheren",
            target_articles=["A22"], route="kb", flags=["contradiction"],
            edit=Edit(old="Kan ik een cabine sluiten terwijl ze in gebruik is?"),
            text="## Een cabine sluiten\n\nJe kan een cabine sluiten terwijl ze in gebruik is. "
                 "Lopende afspraken blijven staan, nieuwe boekingen worden geblokkeerd.\n",
            why="Het artikel zegt dat sluiten niet kan, terwijl de helpdesk het tegenovergestelde antwoordt. "
                "Die twee spreken elkaar tegen.",
            seed_reply="hs:3438828375",
            evidence=[
                Quote(conversation_id="hs:3438828375", quote="kan ik een cabine sluiten terwijl ze gebruikt wordt"),
                Quote(conversation_id="hs:3439000002", quote="ik vind niets over dubbele boekingen"),
            ],
        ),
        Suggestion(
            id="S3", kind="rewrite", topic="cabine-toestel-koppelen", title="Cabines en toestellen koppelen",
            target_articles=["A22"], route="kb",
            edit=Edit(after="## Toestellen"),
            text="Koppel een toestel aan een cabine via de cabinefiche, tabblad **Toestellen**. "
                 "Zo kan hetzelfde toestel niet twee keer tegelijk geboekt worden.\n",
            why="Twee klanten boekten hetzelfde toestel dubbel omdat het artikel wel de toestellen noemt "
                "maar niet uitlegt hoe je ze koppelt.",
            evidence=[
                Quote(conversation_id="hs:3438363160", quote="hoe koppel ik een toestel aan een cabine?"),
                Quote(conversation_id="hs:3439000004",
                      quote="staat er ergens welke toestellen bij welke cabine horen?"),
            ],
        ),
        Suggestion(
            id="S4", kind="retitle", topic="annulatie", title="Annulatie en no-show: wat rekenen we aan?",
            target_articles=["A24"], route="brain",
            why="Klanten zoeken op no-show, en dat woord staat nergens in de titel.",
            evidence=[
                Quote(conversation_id="hs:3439000001", quote="waar vind ik het annulatiebeleid voor no-shows?"),
                Quote(conversation_id="hs:3439000002", quote="ik vind niets over dubbele boekingen"),
            ],
        ),
        Suggestion(
            id="S5", kind="add_alias", topic="dubbele-boeking", title="Cabines beheren",
            target_articles=["A22"], route="kb",
            aliases=["cabine dubbel geboekt", "twee keer geboekt", "dubbele boeking"],
            why="De woorden die klanten gebruiken staan niet in het artikel, dus de zoekfunctie vindt het niet.",
            evidence=[
                Quote(conversation_id="hs:3439000002", quote="ik vind niets over dubbele boekingen"),
                Quote(conversation_id="hs:3438363160", quote="Nele boekt mijn twee cabines dubbel"),
            ],
        ),
        Suggestion(
            id="S6", kind="merge", topic="dubbele-boeking", title="Cabines beheren",
            target_articles=["A20", "A22"], destination="A22", route="kb",
            text="# Cabines beheren\n\nAlles over cabines staat vanaf nu op deze pagina.\n",
            why="Twee artikels behandelen hetzelfde onderwerp en spreken elkaar tegen over dubbele boekingen.",
            evidence=[
                Quote(conversation_id="hs:3439000002", quote="ik vind niets over dubbele boekingen"),
                Quote(conversation_id="hs:3438363160", quote="Nele boekt mijn twee cabines dubbel"),
            ],
        ),
        Suggestion(
            id="S7", kind="delete", topic="annulatie", title="Annulatiebeleid", target_articles=["A20"],
            destination="A22", route="kb",
            why="Dit artikel is vervangen en stuurt lezers naar een pagina die niet meer bestaat.",
            evidence=[
                Quote(conversation_id="hs:3439000001", quote="waar vind ik het annulatiebeleid voor no-shows?"),
                Quote(conversation_id="hs:3439000002", quote="ik vind niets over dubbele boekingen"),
            ],
        ),
    ]
    return Suggestions(
        schema_version=2, evidence_sha256="0" * 64,
        headline="Twee gaten verklaren de helft van deze week: het koppelen van toestellen aan cabines, "
                 "en beheerders die zelf een lijst willen filteren.",
        classification=classification, suggestions=suggestions,
        learnings=[Learning(observation="Chat sessies van interne tenants komen als ruis binnen",
                            proposed_change="pre-tag ze op tenant", target="normaliser")],
    )


@pytest.fixture(scope="module")
def bundle():
    return _suggestions(), _evidence()


@pytest.fixture()
def html(bundle, tmp_path):
    sug, ev = bundle
    return render.render_html(sug, ev, articles_dir=_articles_dir(tmp_path))


def _sug(bundle, sid):
    return next(s for s in bundle[0].suggestions if s.id == sid)


def _art(bundle, aid):
    return next(a for a in bundle[1].articles if a.id == aid)


# ------------------------------------------------------------------ bot block

def test_bot_block_new(bundle):
    # no collection known (kind new has no target), so the bot gets a placeholder comment
    assert render.bot_block(_sug(bundle, "S1"), None, bundle[1].kb) == (
        render.BOT_INSTRUCTION + "\n\n"
        "---\n"
        "replypen: helpcenter/v1\n"
        "op: create\n"
        "provider: helpscout\n"
        "base_url: https://ibeauty.helpscoutdocs.com\n"
        "title: Zelf een lijst van inschrijvingen filteren\n"
        "audience: customer\n"
        "# collection_id: <pick one, the collection this article belongs to>\n"
        "status: draft\n"
        "why: Twee beheerders vroegen deze week om een lijst die ze zelf in twee klikken kunnen maken."
        " Het help centrum legt de filter nergens uit.\n"
        "---\n"
        "# Zelf een lijst filteren\n\n"
        "1. Ga naar **Inschrijvingen**.\n"
        "2. Zet de filter op *deze week* en kies je locatie.\n"
        "3. Kopieer de url uit de adresbalk en deel ze.\n"
    )


def test_bot_block_rewrite_carries_the_old_anchor(bundle):
    block = render.bot_block(_sug(bundle, "S2"), _art(bundle, "A22"), bundle[1].kb)
    assert "op: update\n" in block
    assert "id: 6a5a2fdba9c25bdda659637e\n" in block
    assert "number: '19'\n" in block
    assert "anchor:\n  old: |\n    Kan ik een cabine sluiten terwijl ze in gebruik is?\n" in block
    assert "collection_id" not in block  # update never moves the article
    assert block.endswith("nieuwe boekingen worden geblokkeerd.\n")


def test_bot_block_rewrite_insert_carries_the_after_anchor(bundle):
    block = render.bot_block(_sug(bundle, "S3"), _art(bundle, "A22"), bundle[1].kb)
    assert "anchor:\n  after: '## Toestellen'\n" in block


def test_bot_block_retitle_has_title_and_empty_body(bundle):
    block = render.bot_block(_sug(bundle, "S4"), _art(bundle, "A24"), bundle[1].kb)
    assert "op: update\n" in block
    assert "title: 'Annulatie en no-show: wat rekenen we aan?'\n" in block
    assert block.endswith("---\n\n")


def test_bot_block_add_alias_has_aliases_and_empty_body(bundle):
    block = render.bot_block(_sug(bundle, "S5"), _art(bundle, "A22"), bundle[1].kb)
    assert "aliases:\n- cabine dubbel geboekt\n- twee keer geboekt\n- dubbele boeking\n" in block
    assert block.endswith("---\n\n")


def test_bot_block_merge_is_manual_and_names_the_destination(bundle):
    block = render.bot_block(_sug(bundle, "S6"), _art(bundle, "A20"), bundle[1].kb,
                             destination=_art(bundle, "A22"))
    assert "op: manual\n" in block
    assert "Merge into: Cabines beheren (6a5a2fdba9c25bdda659637e)." in block


def test_bot_block_delete_is_manual_and_names_the_destination(bundle):
    block = render.bot_block(_sug(bundle, "S7"), _art(bundle, "A20"), bundle[1].kb,
                             destination=_art(bundle, "A22"))
    assert "id: 5f2c0000000000000000000a\n" in block  # the article being deleted, not the survivor
    assert "op: manual\n" in block
    assert "Delete, redirect to: Cabines beheren (6a5a2fdba9c25bdda659637e)." in block


def test_bot_block_is_deterministic(bundle):
    args = (_sug(bundle, "S2"), _art(bundle, "A22"), bundle[1].kb)
    assert render.bot_block(*args) == render.bot_block(*args)


# --------------------------------------------------------------------- report

def test_every_copy_button_points_at_exactly_one_element(html):
    buttons = re.findall(r'<button class="copy"[^>]*>', html)
    assert len(buttons) == 14  # rendered + markdown view for each of the 7 suggestions
    for button in buttons:
        target = re.search(r'data-target="([^"]+)"', button).group(1)
        assert len(re.findall(rf'id="{re.escape(target)}"', html)) == 1, target
        md = re.search(r'data-md="([^"]+)"', button).group(1)
        assert len(re.findall(rf'id="{re.escape(md)}"', html)) == 1, md


def test_card_reads_why_then_evidence_then_edit(html):
    card = html.split('<div class="card">')[1]
    assert card.index('class="why"') < card.index('class="quote"') < card.index('class="editwrap"')


def test_rewrite_with_old_stacks_before_and_after(html):
    assert '<pre class="old">Kan ik een cabine sluiten terwijl ze in gebruik is?</pre>' in html
    assert ">becomes</p>" in html


def test_insert_shows_the_anchor_with_neighbouring_lines(html):
    # context comes from raw/articles/A22.md, so the owner sees where the block lands
    assert ("Je maakt een cabine aan via Instellingen &gt; Cabines.\n"
            '<span class="anchorline">## Toestellen</span></pre>') in html
    assert "Elk toestel hoort bij een cabine." in html


def test_insert_without_article_body_still_renders(bundle):
    sug, ev = bundle
    out = render.render_html(sug, ev, articles_dir=None)
    assert "Insert after the bold line in Cabines beheren" in out


def test_badges_and_seed_reply_wording(html):
    assert '<span class="badge warn">contradiction</span>' in html
    assert '<span class="badge route">brain</span>' in html
    assert "How Marie-Lore answered this in the ticket" in html
    assert "simulation" not in html.lower()


def test_tiles_are_clickable_and_explain_themselves(html):
    assert html.count('<details class="tile">') == len(render.TILES)
    assert "The cheapest fix is putting their words in the title or aliases" in html


def test_markdown_view_holds_the_bot_block_in_a_template(html):
    assert '<template id="md-S1">' in html
    assert "Apply this edit to the help centre." in html
    assert '<pre class="bot" data-src="md-S1"></pre>' in html
    assert "marked.min.js" in html and "offline: showing markdown" in html


def test_multi_tenant_adds_a_column(html):
    assert "<th>tenant</th>" in html
    assert "yes_events" in html


def test_no_em_dash_anywhere(html):
    assert "—" not in html


def _demo() -> Path:
    out = Path("/tmp/r3-render")
    arts = out / "raw" / "articles"
    arts.mkdir(parents=True, exist_ok=True)
    (arts / "A22.md").write_text(A22_BODY, encoding="utf-8")
    report = out / "report.html"
    report.write_text(render.render_html(_suggestions(), _evidence(), articles_dir=arts), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(_demo())
