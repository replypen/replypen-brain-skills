"""Normaliser tests for `brain-helpcenter-suggestions`. Synthetic data only, never real customers.

    cd skills/brain-helpcenter-suggestions && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collect  # noqa: E402
import corpus  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 8, tzinfo=timezone.utc)


def page() -> dict:
    return json.loads((FIXTURES / "helpscout_page.json").read_text(encoding="utf-8"))


def test_raw_page_shape():
    convs = corpus.helpscout_conversations(page())
    assert [c["id"] for c in convs] == ["hs:111", "hs:222", "hs:333"]
    chat = convs[0]
    assert chat["channel"] == "chat"
    assert chat["url"] == "https://secure.helpscout.net/conversation/111/11/"
    assert chat["customer"] == "SalonLies"
    # beaconchat is the customer, note/lineitem are dropped, html became text
    assert chat["first_message"] == "Hoe pas ik de bevestigingsmail aan?"
    assert chat["first_raw"] is None and chat["noise"] is None and chat["tenant"] is None
    assert "Technical Information" not in json.dumps(chat)
    assert chat["reply"] == {"text": "Dag Lies\nDat kan via Prijslijst > Reminders.",
                             "by": "Marie-Lore", "provenance": "human"}
    assert chat["later"] == [] and chat["truncated"] is False


def test_unanswered_and_quote_stripping():
    conv = corpus.helpscout_conversations(page())[1]
    assert conv["reply"] is None  # unanswered conversations are kept
    assert conv["channel"] == "email" and conv["tags"] == ["bonnen"]
    assert conv["first_message"] == "Waar vind ik mijn cadeaubon terug in de webshop?"


def test_window_filter_and_dedupe():
    convs = corpus.helpscout_conversations(page())
    kept = [c for c in convs if corpus.in_window(c["created_at"], START, END)]
    assert [c["id"] for c in kept] == ["hs:111", "hs:222"]


def test_signature_and_email_stripped():
    assert corpus.strip_quoted_and_signature(
        "Vraag?\nMet vriendelijke groeten\nLies") == "Vraag?"
    assert corpus.strip_quoted_and_signature("Vraag?\n> oud\nmeer") == "Vraag?"
    assert corpus.clean("mail me op lies@salon.be", 100) == "mail me op [e-mail]"


DUMP = """##### C1 | email | via=beacon-v2 | tags=[] | subj=None | 2026-09-01T09:00 -> closed 2026-09-01T09:39
[customer|LouLouGlow] Hoe kan ik de bevestigingsmail aanpassen voor mijn klanten?
[message|Michel] Dat kan via de prijslijst.
[customer|LouLouGlow] gevonden, dank

##### C2 | chat | via=beacon-v2 | tags=[boekingen] | subj=Re: agenda | 2026-09-02T11:27 -> active
[customer|Davy] Kan ik de agenda openzetten tot december?
Tweede regel van dezelfde vraag.
[note|Marie-Lore] @michel doe jij?
[message|Marie-Lore] Ja, via Instellingen.

##### C3 | email | via=beacon-v2 | tags=[] | subj=None | 2026-09-03T08:00 -> active
[customer|Ann] Nog geen antwoord gekregen.
"""


def trace_header() -> dict:
    return json.loads((FIXTURES / "trace_header.json").read_text(encoding="utf-8"))


def test_trace_roles_sibling_vendor_is_unknown():
    """is_inbound is delivery direction: a cc'd sibling vendor also arrives inbound."""
    conv = corpus.trace_conversation(trace_header(), frozenset({"vendor.example"}))
    assert conv["id"] == "run:53ff0f29" and conv["tenant"] is None
    assert conv["first_message"].startswith("Hi there")
    roles = [turn["role"] for turn in conv["later"]]
    assert roles[0] == "unknown"  # the sibling vendor answering in-thread
    assert "not shown in the Power Sync toolbox" in conv["later"][0]["text"]
    assert "customer" in roles  # the real customer's follow-up is still labelled
    # the vendor's own reply is the answer, never a customer quote
    assert conv["reply"]["by"] == "Vendor <info@vendor.example>"
    assert conv["reply"]["provenance"] == "human"


def test_trace_agent_by_mailbox_domain():
    """An outbound-looking reply that arrives inbound is still ours when the domain is ours."""
    header = trace_header()
    header["prior_messages"][3]["is_inbound"] = True
    conv = corpus.trace_conversation(header, frozenset({"vendor.example"}))
    assert conv["reply"]["by"] == "Vendor <info@vendor.example>"
    conv = corpus.trace_conversation(header, frozenset())
    assert conv["reply"]["provenance"] == "draft"  # nothing provable → the draft answers
    assert [t["role"] for t in conv["later"]].count("unknown") == 2


CHAT_HEADER = {
    "run_id": "7dca3d0a-0000-4000-8000-000000000000",
    "created_at": "2026-09-03T11:44:56Z", "tenant": "yes_events", "topic": None,
    "question": "dank je", "draft": "",
    "prior_messages": [
        {"sender": "6a995d491a5dd2cd1ca240c3", "is_inbound": True, "body": "Monitoren"},
        {"sender": "operator+box@intercom.io", "is_inbound": False,
         "body": "Hey, ik ben Yumi! 👋 Als ik je ergens mee kan helpen mag je het mij hier laten weten."},
        {"sender": "operator+box@intercom.io", "is_inbound": False, "body": "Hey ! 👋"},
        {"sender": "6a995d491a5dd2cd1ca240c3", "is_inbound": True, "body": "Nieuwe monitor"},
        {"sender": "operator+box@intercom.io", "is_inbound": False,
         "body": "Waar kunnen we je mee helpen? 😄"},
        {"sender": "zita@example.com", "is_inbound": True, "body": "Hallo,"},
        {"sender": "zita@example.com", "is_inbound": True,
         "body": "worden er in Rumst geen kampen meer georganiseerd?"},
        {"sender": "operator+box@intercom.io", "is_inbound": False,
         "body": "In Rumst organiseren we dit jaar geen kampen meer, wel in Boom."},
    ],
}


def test_chat_boilerplate_and_question_turn():
    conv = corpus.trace_conversation(CHAT_HEADER, frozenset())
    assert conv["tenant"] == "yes_events"
    # the bot greeting, the bare "Hey !" and the flow prompt are gone, the salutation too
    assert "Yumi" not in json.dumps(conv) and "Hallo" not in json.dumps(conv)
    assert conv["first_message"] == "worden er in Rumst geen kampen meer georganiseerd?"
    assert conv["first_raw"] == "Monitoren"  # the menu click is kept, never as the question
    assert conv["reply"]["text"].startswith("In Rumst organiseren we")
    # the widget hands over an e-mail mid-chat: same person, still the customer
    assert all(t["role"] != "unknown" for t in conv["later"])


# ---------------------------------------------------------------- P1: noise, merge, links


def test_noise_pretagging():
    assert corpus.noise_reason("Invitation: 20 min meeting", "cal@x.be", "A new event") == \
        "calendar_invite"
    assert corpus.noise_reason("Backup failed", "noreply@x.be", "Job 12 failed") == "no_reply_sender"
    assert corpus.noise_reason(None, "pj@x.be", "test") == "test"
    assert corpus.noise_reason(None, "pj@x.be", "  ") == "empty"
    assert corpus.noise_reason("Hoe pas ik dit aan?", "pj@x.be", "Hoe pas ik dit aan?") is None


def test_calendar_invite_from_attachment():
    header = trace_header()
    header["prior_messages"][0]["attachments"] = [{"filename": "invite.ics"}]
    assert corpus.trace_conversation(header)["noise"]["reason"] == "calendar_invite"


def make(cid, customer, text, when, reply=None):
    return corpus.build(cid, None, "email", when, "subj", customer, [],
                        [("customer", text, customer)] + ([("agent", reply, "a")] if reply else []))


def test_merge_duplicates_on_sender_and_text():
    text = "Ik heb nog credit op mijn account en wil graag een kamp boeken, hoe doe ik dat?"
    convs = [make("run:aaa", "ann@x.be", text, "2026-09-02T10:00:00Z", "Antwoord."),
             make("run:bbb", "ann@x.be", text + "  ", "2026-09-02T10:20:00Z"),
             make("run:ccc", "ann@x.be", text, "2026-09-04T10:00:00Z")]
    merged = corpus.merge_duplicates(convs)
    assert [c["id"] for c in merged] == ["run:aaa", "run:ccc"]  # outside the hour stays its own
    assert merged[0]["tags"] == ["merged:run:bbb"]


def test_duplicate_outreach_tagged():
    pitch = "We build AI agents for support teams, are you interested in a quick call?"
    convs = [make("run:a", "sales1@x.be", pitch, "2026-09-02T10:00:00Z"),
             make("run:b", "sales2@y.be", pitch, "2026-09-03T10:00:00Z")]
    corpus.tag_duplicate_outreach(convs)
    assert [c["noise"]["reason"] for c in convs] == ["duplicate_outreach"] * 2


def test_link_articles_by_url_and_id():
    articles = [
        {"id": "A1", "url": "https://faq.example.be/nl/articles/3164619-feestdag",
         "path": "/kb/tenant/intercom/algemeen/3164619-feestdag.md"},
        {"id": "A2", "url": None, "path": "/kb/knowledgeowl/aanbod/aanbod-voorbereiden.md"},
        {"id": "A3", "url": "https://help.example.com/article/9287008-invoices",
         "path": "/kb/helpscout/billing/9287008-invoices.md"},
    ]
    convs = [make("c1", "a@x.be", "Gaat het kamp door op een feestdag?", "2026-09-02T10:00:00Z",
                  "Zie https://faq.example.be/nl/articles/3164619-feestdag voor meer."),
             make("c2", "b@x.be", "Waar staan mijn facturen ergens in het portaal?",
                  "2026-09-02T10:00:00Z", "See https://help.example.com/article/9287008-invoices")]
    corpus.link_articles(convs, articles)
    assert convs[0]["linked_articles"] == ["A1"]
    assert convs[1]["linked_articles"] == ["A3"]


def test_tsv_and_digest_tiers():
    evidence = {"project": "demo", "tenant": "yes_events", "window": {"days": 8},
                "source": "email_runs", "coverage": [], "kb": {"status": "complete", "root": "/kb"},
                "conversations": [corpus.trace_conversation(CHAT_HEADER)],
                "articles": [{"id": "A1", "home": "kb", "title": "Feestdagen", "path": "/kb/a.md",
                              "collection": "Algemeen", "audience": "customer",
                              "keywords": ["feestdag"]}]}
    convs_tsv, articles_tsv = corpus.write_tsvs(evidence)
    assert convs_tsv.splitlines()[0].split("\t") == [
        "id", "date", "tenant", "ch", "first", "turns", "reply", "linked", "noise"]
    assert convs_tsv.splitlines()[1].split("\t")[2] == "yes_events"
    assert articles_tsv.splitlines()[0].split("\t")[3] == "collection_id"
    assert articles_tsv.splitlines()[1].split("\t")[4] == "customer"
    digest = corpus.write_digest(evidence)
    assert "demo / yes_events" in digest and "[customer|opener] Monitoren" in digest


def test_tsv_lines_stay_readable_whole():
    """500 lines must fit the judge's pass-1 budget: one sentence per conversation, no bodies."""
    long_one = corpus.build("run:deadbeef", None, "chat", "2026-09-02T10:00:00Z", None, None, [],
                            [("customer", "Ik heb een heel lange vraag. " * 40, None)])
    evidence = {"conversations": [long_one], "articles": []}
    lines = corpus.write_tsvs(evidence)[0].splitlines()
    assert all(len(line) <= corpus.TSV_LINE_MAX for line in lines)
    # the first sentence, not the whole wall of text
    assert lines[1].split("\t")[4] == "Ik heb een heel lange vraag."




# ---------------------------------------------------------------- chat sessions


def chat_header() -> dict:
    return json.loads((FIXTURES / "trace_header_chat.json").read_text(encoding="utf-8"))


def test_chat_roles_and_clarifier_fold():
    """Chat turns carry no sender: inbound is the customer, outbound is us, the reply is a draft."""
    conv = corpus.trace_conversation(chat_header())
    assert conv["channel"] == "chat" and conv["tenant"] == "lbv"
    assert conv["id"] == "run:3de6cda0" and conv["url"] == "https://app.example/runs/3de6cda0"
    assert conv["first_message"].startswith("ik moet voor de verzekeraar een lijst")
    assert conv["reply"]["provenance"] == "draft" and conv["reply"]["by"] is None
    assert conv["reply"]["text"].startswith("Dag, ik heb de lijst opgesteld")
    turns = [t["text"] for t in conv["later"]]
    # the clarifier form is scaffolding, but it rides along on a real answer: cut it, keep the answer
    assert "Questions asked" not in json.dumps(conv)
    assert turns[0] == ("kan je die lijst aanmaken in KA bij inschrijvingen? "
                        "[koos: doel=saved_filter; weergave=all_statuses]")
    assert turns[1] == "Wil je een opgeslagen lijst of een tag?"
    assert turns[-1].startswith("Ik kan geen opgeslagen filterlijst")
    assert all(t["role"] != "unknown" for t in conv["later"])


def test_chat_orphan_clarifier_answer_is_not_a_question():
    header = chat_header()
    header["prior_messages"] = []
    assert corpus.trace_conversation(header) is None


# ---------------------------------------------------------------- Embassy support tickets


def ticket_header() -> dict:
    return json.loads((FIXTURES / "trace_header_ticket.json").read_text(encoding="utf-8"))


def test_ticket_normaliser():
    """`kind: analysis` is a markdown ticket: title, filer, tenant, type/priority tags, draft reply."""
    conv = corpus.trace_conversation(ticket_header())
    assert conv["channel"] == "ticket" and conv["tenant"] == "duinvos"
    assert conv["id"] == "run:9c4b21ef" and conv["url"] == "https://app.example/runs/9c4b21ef"
    assert conv["subject"] == "bevestigingsmail met verkeerde datum"
    assert conv["customer"] == "Nadia"  # first name only, never the address
    assert conv["tags"] == ["ticket_type:question", "priority:low"]
    assert conv["noise"] is None
    assert "@" not in json.dumps(conv) and "Ticket ID" not in json.dumps(conv)
    assert conv["first_message"].startswith("In de bevestigingsmail van de zomerstage")
    assert "Metadata" not in conv["first_message"]
    # the discussion splits on the dated name headers; the later turn stays a customer turn
    assert [t["role"] for t in conv["later"]] == ["customer"]
    assert conv["later"][0]["text"] == ("Ondertussen gevonden bij de activiteit zelf, "
                                        "maar geldt dat ook voor de wachtlijstmail?")
    assert conv["reply"]["provenance"] == "draft" and conv["reply"]["by"] is None
    assert conv["reply"]["text"].startswith("Dag Nadia")


def test_ticket_turn_header_variants():
    """Real tickets decorate the header and put the name on either side of the date."""
    assert corpus._ticket_turns("**Sylvie 27/08/2026**\nHallo, dit is de vraag.") == [
        "Hallo, dit is de vraag."]
    assert corpus._ticket_turns("### Lies 28/08\neen vraag\n\n2/9 - Lies\nen een tweede") == [
        "een vraag", "en een tweede"]
    # prose that merely mentions a date is not a header
    assert corpus._ticket_turns("Ik kom op 26/08 langs bij jullie kantoor.") == [
        "Ik kom op 26/08 langs bij jullie kantoor."]


def test_ticket_without_turn_headers_is_one_turn():
    header = ticket_header()
    header["question"] = header["question"].replace("2/9 - Nadia\n\n", "").replace(
        "### 03/09/26 Nadia\n\n", "")
    conv = corpus.trace_conversation(header)
    assert conv["later"] == []
    assert conv["first_message"].startswith("In de bevestigingsmail")


@pytest.mark.parametrize("kind,noise", [("question", None), ("config", None),
                                        ("feedback", "ticket_type"), ("request", "ticket_type")])
def test_ticket_type_decides_the_noise_pretag(kind, noise):
    """A feature wish is never closed by an article; a usage or config question can be."""
    header = ticket_header()
    header["question"] = header["question"].replace("Type: question (Gebruiksvraag)",
                                                    f"Type: {kind} (Iets)")
    conv = corpus.trace_conversation(header)
    assert (conv["noise"] or {}).get("reason") == noise
    assert conv["tags"][0] == f"ticket_type:{kind}"


def test_ticket_falls_back_to_the_metadata_tenant():
    header = ticket_header()
    header["tenant"] = None
    assert corpus.trace_conversation(header)["tenant"] == "duinvos"


def test_kind_selection_filters_the_corpus(monkeypatch):
    """`--kind` picks the feeds; everything else is only counted in `other_runs`."""
    monkeypatch.setattr(sys, "argv", ["collect.py", "--kind", "analysis"])
    assert collect.parse_args().kind == ("analysis",)
    monkeypatch.setattr(sys, "argv", ["collect.py"])
    assert collect.parse_args().kind == collect.CORPUS_KINDS == ("email", "chat", "analysis")
    rows = [{"kind": "email"}, {"kind": "chat"}, {"kind": "analysis"}, {"kind": "mcp"}]
    kinds = ("analysis",)
    assert [r for r in rows if r["kind"] in kinds] == [{"kind": "analysis"}]
    feed = collect.other_runs_feed([r for r in rows if r["kind"] not in kinds])
    assert feed["reason"] == "1 email runs · 1 chat runs · 1 mcp runs, not part of this corpus"


def test_session_grouping_takes_the_last_run_and_the_earliest_date():
    rows = [
        {"run_id": "r2", "session_id": "s1", "thread_id": "s1", "kind": "chat",
         "created_at": "2026-09-07T15:19:13Z"},
        {"run_id": "r1", "session_id": "s1", "thread_id": "s1", "kind": "chat",
         "created_at": "2026-09-07T15:02:00Z"},
        {"run_id": "r3", "kind": "email", "created_at": "2026-09-06T08:00:00Z"},
    ]
    picked = sorted(collect.sessions(rows), key=lambda p: p["run_id"])
    assert [p["run_id"] for p in picked] == ["r2", "r3"]
    # the whole transcript lives in the last run, but the conversation started earlier
    assert picked[0] == {"run_id": "r2", "created_at": "2026-09-07T15:02:00Z", "kind": "chat",
                         "runs": 2}


def test_other_runs_feed_counts_what_we_did_not_read():
    rows = [{"kind": "analysis", "outcome": "failed"}] * 3 + [{"kind": "analysis"}, {"kind": "mcp"}]
    feed = collect.other_runs_feed(rows)
    assert feed["feed"] == "other_runs" and feed["retained"] == 0
    assert feed["reason"] == "4 analysis runs, 3 failed · 1 mcp runs, not part of this corpus"


# ---------------------------------------------------------------- noise pre-tags


def chat(cid, text, tenant=None):
    return corpus.build(cid, None, "chat", "2026-09-02T10:00:00Z", None, None, [],
                        [("customer", text, None)], "draft", tenant=tenant)


def test_chat_noise_pretags():
    convs = [chat("c1", "hoe verwijder ik een monitor uit de lijst?", "demo"),
             chat("c2", "https://kampadmin.be/admin/inschrijvingen?filter=42"),
             chat("c3", "NoMethodError: undefined method `each' for nil at line 42"),
             chat("c4", "hoeveel inschrijvingen zijn er deze week in totaal genomen?"),
             chat("c5", "hoeveel inschrijvingen zijn er deze week in totaal genomen?"),
             chat("c6", "hoeveel inschrijvingen zijn er deze week in totaal genomen?"),
             chat("c7", "waar vind ik het overzicht van de wachtlijst per activiteit?")]
    corpus.tag_noise(convs, ["demo", "base"])
    assert [(c["noise"] or {}).get("reason") for c in convs] == [
        "internal_tenant", "bare_url", "error_paste", "repeated_prompt", "repeated_prompt",
        "repeated_prompt", None]


def test_internal_tenant_overrides_an_earlier_pretag():
    conv = chat("c1", "test", "demo")
    assert conv["noise"]["reason"] == "test"
    corpus.tag_noise([conv], ["demo"])
    assert conv["noise"]["reason"] == "internal_tenant"


def test_error_paste_with_a_question_is_a_real_question():
    conv = chat("c1", "Error: 500 op de inschrijvingenpagina, wat moet ik nu doen?")
    corpus.tag_noise([conv], [])
    assert conv["noise"] is None


# ---------------------------------------------------------------- P0: inventory + tenant plumbing


INDEX_ARTIFACT = """@@ /kb/tenant/intercom/INDEX.md
# KB index — intercom
# Paths are relative to this directory (kb/intercom/).
algemeen/3164619-feestdag.md · Gaat het kamp door op een feestdag? · feestdag, kampweek · Hoe wij feestdagen verrekenen. · collection: Algemeen · locale: nl · audience: customer
inschrijven/3164700-annuleren.md · Hoe annuleer ik? · annuleren · Annuleren en terugbetaling. · collection: Inschrijven
"""

BODIES_ARTIFACT = """@@ /kb/tenant/intercom/algemeen/3164619-feestdag.md
---
title: Gaat het kamp door op een feestdag?
provider: intercom
id: "3164619"
url: https://faq.example.be/nl/articles/3164619-feestdag
collection: Algemeen
locale: nl
section: "6983603"
status: published
updated_at: 2025-02-21T13:51:56Z
---

# Gaat het kamp door op een feestdag?

Nee, als er een officiele feestdag in een kampweek valt is er geen kamp.
@@ /kb/tenant/intercom/inschrijven/3164700-annuleren.md
---
title: Hoe annuleer ik?
provider: intercom
id: "3164700"
collection_id: "555"
locale: nl
status: published
---

# Hoe annuleer ik?

Via je account.
"""


def test_frontmatter_handles_intercom_sections_and_quoted_ids():
    blocks = collect.at_blocks(BODIES_ARTIFACT)
    first = collect.parse_frontmatter(blocks["/kb/tenant/intercom/algemeen/3164619-feestdag.md"])
    assert first["id"] == "3164619" and first["provider"] == "intercom"
    assert first["section"] == "6983603" and "body" not in first
    assert collect.parse_frontmatter(["no frontmatter here"]) == {}


def fake_workspace(monkeypatch, index_text=INDEX_ARTIFACT, bodies=BODIES_ARTIFACT, count=2,
                   find_stderr=""):
    """Record every rc argv; serve the console envelopes and artifacts the inventory expects."""
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        script = argv[-1]
        if "file" in argv and "get" in argv:
            remote = argv[argv.index("get") + 1]
            Path(argv[argv.index("--out") + 1]).write_text(
                index_text if "hc-index" in remote else bodies if "hc-bodies" in remote else "",
                encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if script.startswith("find /kb"):
            tenantless = "--tenant" not in argv
            if find_stderr and tenantless:
                return type("R", (), {"returncode": 0, "stderr": "", "stdout": json.dumps(
                    {"exit_code": 1, "stdout": "", "stderr": find_stderr})})()
            out = "/kb/tenant/intercom/INDEX.md\n/kb/knowledgeowl/INDEX.md\n"
        elif "hc-index.txt" in script:
            out = f"## /kb/tenant/intercom {count}\n"
        else:
            out = ""
        return type("R", (), {"returncode": 0, "stderr": "",
                              "stdout": json.dumps({"exit_code": 0, "stdout": out})})()

    monkeypatch.setattr(collect.subprocess, "run", fake_run)
    return seen


def test_inventory_tenant_scope_paths_and_bodies(monkeypatch, tmp_path):
    seen = fake_workspace(monkeypatch)
    articles, kb = collect.inventory(tmp_path, tmp_path, "yes_events")
    assert kb["root"] == "/kb/tenant/intercom" and kb["status"] == "complete"
    assert kb["scope"] == "tenant" and kb["provider"] == "intercom"
    assert kb["base_url"] == "https://faq.example.be"
    # provider comes from the path, not from the INDEX header comment (which says kb/intercom/)
    assert [a["path"] for a in articles] == [
        "/kb/tenant/intercom/algemeen/3164619-feestdag.md",
        "/kb/tenant/intercom/inschrijven/3164700-annuleren.md"]
    assert articles[0]["audience"] == "customer" and articles[0]["title"].startswith("Gaat het kamp")
    # provider-native handles the bot block needs, straight from the frontmatter
    assert (articles[0]["provider_id"], articles[0]["collection_id"], articles[0]["parent_type"]) \
        == ("3164619", "6983603", "section")
    assert (articles[1]["collection_id"], articles[1]["parent_type"]) == ("555", None)
    # every kb article body lands verbatim: the anchor source the validator checks `edit.old` against
    body = (tmp_path / "articles" / "A1.md").read_text(encoding="utf-8")
    assert body.startswith("---\ntitle: Gaat het kamp") and "officiele feestdag" in body
    # --tenant rides on every console call, and only on console calls
    console_calls = [a for a in seen if a[:3] == ["rp", "dev", "console"]]
    assert console_calls and all(a[3:5] == ["--tenant", "yes_events"] for a in console_calls)
    assert not any("fleet" in a or "trace" in a for a in seen)


def test_inventory_project_scope_skips_tenant_kbs(monkeypatch, tmp_path):
    """kampadmin-support: several tenants, one project help centre, one report."""
    fake_workspace(monkeypatch, index_text=INDEX_ARTIFACT.replace("/kb/tenant/intercom",
                                                                  "/kb/knowledgeowl"),
                   bodies=BODIES_ARTIFACT.replace("/kb/tenant/intercom", "/kb/knowledgeowl"))
    _, kb = collect.inventory(tmp_path, tmp_path, None)
    assert kb["root"] == "/kb/knowledgeowl" and kb["scope"] == "project"


def test_kb_scope_decided_by_the_mount():
    both = ["/kb/knowledgeowl/INDEX.md", "/kb/tenant/intercom/INDEX.md"]
    assert collect.kb_scope(both, "yes_events") == ("tenant", ["/kb/tenant/intercom/INDEX.md"])
    assert collect.kb_scope(both, None) == ("project", ["/kb/knowledgeowl/INDEX.md"])
    # a tenant asking for a help centre that is only mounted project-wide still gets the project one
    assert collect.kb_scope(["/kb/knowledgeowl/INDEX.md"], "lbv")[0] == "project"
    with pytest.raises(SystemExit) as exc:  # tenant KBs only, no --tenant: no single help centre
        collect.kb_scope(["/kb/tenant/intercom/INDEX.md"], None)
    assert exc.value.code == 2


def test_console_tenant_is_inferred_when_the_project_refuses_tenantless_calls(monkeypatch, tmp_path):
    """kampadmin-support serves a project KB but answers 403 TENANT_REQUIRED without a tenant."""
    seen = fake_workspace(monkeypatch, index_text=INDEX_ARTIFACT.replace("/kb/tenant/intercom",
                                                                         "/kb/knowledgeowl"),
                          bodies=BODIES_ARTIFACT.replace("/kb/tenant/intercom", "/kb/knowledgeowl"),
                          find_stderr="403 TENANT_REQUIRED: this project requires a tenant")
    _, kb = collect.inventory(tmp_path, tmp_path, None, "lbv")
    assert kb["scope"] == "project" and "console scoped to tenant lbv" in kb["reason"]
    assert kb["status"] == "complete"  # an inferred tenant is a note, not missing coverage
    assert ["rp", "dev", "console", "--tenant", "lbv"] == seen[1][:5]


def test_empty_inventory_is_a_hard_exit(monkeypatch, tmp_path):
    fake_workspace(monkeypatch, index_text="@@ /kb/tenant/intercom/INDEX.md\n# empty\n")
    with pytest.raises(SystemExit) as exc:
        collect.inventory(tmp_path, tmp_path, "yes_events")
    assert exc.value.code == 2


def test_partial_inventory_needs_a_real_file_count(monkeypatch, tmp_path):
    fake_workspace(monkeypatch, count=9)
    _, kb = collect.inventory(tmp_path, tmp_path, "yes_events")
    assert kb["status"] == "partial" and "2 indexed vs 9 .md on disk" in kb["reason"]


def test_days_is_capped(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["collect.py", "--days", "365"])
    with pytest.raises(SystemExit) as exc:
        collect.parse_args()
    assert exc.value.code == 2
