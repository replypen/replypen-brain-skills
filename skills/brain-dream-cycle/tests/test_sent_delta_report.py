from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sent_delta_report.py"
SPEC = importlib.util.spec_from_file_location("sent_delta_report", SCRIPT)
assert SPEC and SPEC.loader
sdr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sdr  # @dataclass resolves annotations through sys.modules
SPEC.loader.exec_module(sdr)
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def text_of(html_fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", html_fragment)


class SplitQuotesTest(unittest.TestCase):
    def test_dutch_attribution_and_quote_block_go_to_the_trailer(self):
        body = (
            "Uw afspraak is geannuleerd.\n\nVriendelijke groeten,\nTessa\n\n"
            "Op di 14 jul 2026 om 18:28 schreef Afifa Lefhal <\n"
            "afifa@example.be>:\n\n> Beste,\n> Graag annuleren.\n"
        )
        new, quoted = sdr.split_quotes(body)
        self.assertIn("Vriendelijke groeten", new)
        self.assertNotIn("schreef", new)
        self.assertTrue(quoted.startswith("Op di 14 jul 2026"))
        self.assertIn("> Graag annuleren.", quoted)

    def test_english_and_forward_markers(self):
        for marker in ("On Tue, 14 Jul 2026 at 18:28 Ann <a@b.com> wrote:",
                       "-----Original Message-----"):
            new, quoted = sdr.split_quotes(f"Hi there.\n\n{marker}\n> old\n")
            self.assertEqual(new, "Hi there.")
            self.assertTrue(quoted.startswith(marker))

    def test_body_without_quotes_is_untouched(self):
        new, quoted = sdr.split_quotes("Just a reply.\n\nRegards,\nTessa")
        self.assertEqual(new, "Just a reply.\n\nRegards,\nTessa")
        self.assertEqual(quoted, "")


class WordDiffTest(unittest.TestCase):
    def test_edit_inside_a_sentence_stays_word_level(self):
        row, kept, removed, added = sdr.word_diff(
            "We can schedule you on Tuesday 8 September at 11u20.",
            "We can schedule you on Monday 12 July at 11u20.",
        )
        self.assertGreater(kept, removed + added)  # a line diff would call this a full rewrite
        left = "".join(t for op, t in row.left if op == "del")
        right = "".join(t for op, t in row.right if op == "ins")
        self.assertIn("Tuesday", left)
        self.assertIn("Monday", right)
        self.assertNotIn("schedule", left + right)

    def test_adjacent_changed_words_are_one_span(self):
        row, _, _, _ = sdr.word_diff("Bedankt voor uw bericht.", "Dank u voor het bericht.")
        html = sdr.spans(row.inline)
        self.assertLessEqual(html.count("<del>"), 2)
        self.assertLessEqual(html.count("<ins>"), 2)

    def test_inline_view_interleaves_removal_before_replacement(self):
        row, _, _, _ = sdr.word_diff("Beste Liesbeth,", "Beste Klara,")
        self.assertEqual(text_of(sdr.spans(row.inline)), "Beste LiesbethKlara,")

    def test_trailing_whitespace_stays_outside_the_marker(self):
        row, _, _, _ = sdr.word_diff("een twee drie", "een drie")
        self.assertIn("<del>twee</del> ", sdr.spans(row.left))


class BuildDiffTest(unittest.TestCase):
    def test_rewritten_paragraph_stays_paired_and_new_one_does_not(self):
        diff = sdr.build_diff(
            "Beste Bono,\n\nIk heb uw afspraak verplaatst naar vrijdag.",
            "Beste Bono,\n\nIk heb uw afspraak verplaatst naar maandag.\n\nTot dan!",
        )
        kinds = [row.kind for row in diff.rows]
        self.assertEqual(kinds, ["equal", "changed", "insert"])
        self.assertGreater(diff.similarity, 0.75)

    def test_unrelated_bodies_are_reported_as_replaced(self):
        diff = sdr.build_diff("Het vroegste moment is dinsdag 8 september om 11u20.",
                              "Wij hebben u proberen te bellen maar krijgen voicemail.")
        self.assertEqual(diff.shape, "replaced")
        self.assertEqual([row.kind for row in diff.rows], ["delete", "insert"])

    def test_quoted_history_is_excluded_from_the_counts_but_kept(self):
        sent = "Dank u.\n\nOp di 14 jul 2026 om 18:28 schreef Ann <a@b.be>:\n\n> " + "woord " * 200
        diff = sdr.build_diff("Dank u.", sent)
        self.assertEqual(diff.shape, "identical")
        self.assertIn("woord", diff.quoted)

    def test_rewrapped_paragraph_is_not_reported_as_changed(self):
        diff = sdr.build_diff(
            "Bedankt voor je bericht. Ik begrijp dat je op 2 september langskomt.",
            "Bedankt voor je bericht. Ik begrijp dat je op 2 september\nlangskomt.",
        )
        self.assertEqual([row.kind for row in diff.rows], ["equal"])
        self.assertEqual(diff.shape, "identical")

    def test_identical_bodies_have_no_markers(self):
        diff = sdr.build_diff("Zelfde tekst.", "Zelfde tekst.")
        self.assertEqual(diff.shape, "identical")
        self.assertEqual(diff.similarity, 1.0)


class SignalTest(unittest.TestCase):
    def test_dropped_date_link_and_placeholder(self):
        diff = sdr.build_diff(
            "Uw afspraak staat op dinsdag 8 september om 11u20.\n\n"
            "Schrijf u in via https://forms.gle/abc\n\n[[STATUS — in te vullen]]",
            "Wij bellen u volgende week op.",
        )
        found = sdr.signals(diff)
        self.assertIn("date_dropped", found)
        self.assertIn("link_dropped", found)
        self.assertIn("placeholder_leak", found)

    def test_date_kept_by_the_human_is_not_a_signal(self):
        diff = sdr.build_diff("Kan dinsdag 8 september om 11u20?", "Het wordt maandag 12 juli 8u00.")
        self.assertNotIn("date_dropped", sdr.signals(diff))

    def test_confirmation_question_and_length_shift(self):
        diff = sdr.build_diff(
            "Kunt u kort bevestigen of dit past? " + "Nog een zin met veel woorden erin. " * 6,
            "Het is bevestigd.",
        )
        found = sdr.signals(diff)
        self.assertIn("confirm_dropped", found)
        self.assertIn("human_wrote_less", found)

    def test_every_emitted_signal_has_a_label(self):
        self.assertEqual(set(sdr.SIGNAL_LABELS), {
            "date_dropped", "link_dropped", "placeholder_leak", "confirm_dropped",
            "greeting_changed", "signoff_changed", "human_wrote_more", "human_wrote_less"})


class RenderTest(unittest.TestCase):
    item = {
        "id": "d1e2f3a4-0000-0000-0000-000000000000",
        "sender": "Praktijk <info@example.be>",
        "sent_at": "2026-07-15T06:23:41Z",
        "related_run_id": "eda3691b-7122-4f56-a3e1-b80913f830ef",
        "thread_id": "t1",
        "session_id": "s1",
        "similarity": 0.21,
        "proposed_body": "Beste <script>alert(1)</script> Afifa,\n\nKunt u bevestigen?",
        "sent_body": "Beste Afifa,\n\nHet is geregeld.",
    }

    def page(self, **kw):
        diff = sdr.build_diff(self.item["proposed_body"], self.item["sent_body"])
        notes = {self.item["id"]: "brain over-personalised"}
        return sdr.render([self.item], [diff], notes, "dentai / de-kies", ""), diff

    def test_html_escapes_customer_markup_and_keeps_the_conclusion(self):
        page, _ = self.page()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("brain over-personalised", page)

    def test_html_drops_the_technical_labels_the_human_does_not_want(self):
        page, diff = self.page()
        for noise in ("thread t1", "session s1", "rp run debug", "server similarity", "word similarity"):
            self.assertNotIn(noise, page)
        # The one plain-language verdict stays, and the run stays reachable but muted.
        self.assertIn(f'class="tag shape">{diff.shape}<', page)
        self.assertIn("run <span class=\"mono\">eda3691b</span>", page)

    def test_html_links_the_run_when_the_payload_carries_a_url(self):
        item = dict(self.item, run_url="https://app.replypen.com/runs/eda3691b?t=tok")
        diff = sdr.build_diff(item["proposed_body"], item["sent_body"])
        page = sdr.render([item], [diff], {}, "dentai / de-kies", "")
        self.assertIn('href="https://app.replypen.com/runs/eda3691b?t=tok"', page)

    def test_markdown_carries_ids_signals_and_word_level_markers(self):
        _, diff = self.page()
        md = sdr.render_markdown([self.item], [diff], {self.item["id"]: "misread intent"},
                                 "dentai / de-kies", "top-level call", "out.html")
        self.assertIn("### d1e2f3a4", md)
        self.assertIn("run `eda3691b-7122-4f56-a3e1-b80913f830ef`", md)
        self.assertIn("confirm_dropped", md)
        self.assertIn("misread intent", md)
        self.assertIn("top-level call", md)
        self.assertIn("out.html", md)
        self.assertRegex(md, r"\[-.+?-\]")

    def test_markdown_omits_unchanged_paragraphs(self):
        diff = sdr.build_diff("Gelijk.\n\nBedankt voor uw bericht.", "Gelijk.\n\nHet is geregeld.")
        md = sdr.render_markdown([self.item], [diff], {}, "s", "", "out.html")
        self.assertIn("(1 unchanged paragraph omitted)", md)
        self.assertNotIn("Gelijk.", md)

    def test_markdown_keeps_whitespace_outside_the_markers(self):
        diff = sdr.build_diff("praktijk De Kies Alken", "praktijk te Alken")
        md = sdr.render_markdown([self.item], [diff], {}, "s", "", "out.html")
        self.assertNotIn("+}Alken", md)

    def test_markdown_groups_by_server_category_and_labels_the_server_metric(self):
        item = dict(self.item, delta_category="factual", run_url="https://app.example.com/runs/x?t=t")
        _, diff = self.page()
        md = sdr.render_markdown([item], [diff], {}, "s", "", "out.html")
        self.assertIn("## Delta categories", md)
        self.assertIn("| 1 | factual | d1e2f3a4 |", md)
        self.assertIn("· factual", md)                       # on the delta head
        self.assertIn("[trace](https://app.example.com/runs/x?t=t)", md)
        # The payload metric must never read as the script's own word-level similarity.
        self.assertIn("server similarity 21%", md)

    def test_markdown_reports_aged_out_deltas_from_their_descriptions(self):
        _, diff = self.page()
        aged = {"id": "aa11bb22-0000-0000-0000-000000000000", "bodies_scrubbed": True,
                "delta_category": "omission", "delta_description": "Human added the cancellation policy"}
        md = sdr.render_markdown([self.item], [diff], {}, "s", "", "out.html", [aged])
        self.assertIn("## Aged out (description only)", md)
        self.assertIn("Human added the cancellation policy", md)
        self.assertIn("1 further delta(s) aged out", md)


class ServerCleanedBodyTest(unittest.TestCase):
    """The server's own cleaner wins when it ships; split_quotes stays the older-server fallback."""

    proposed = "Bedankt voor je bericht.\n\nWe zien je dinsdag."
    # Outlook's glued header block — split_quotes' (From|Van|Von):.+<.+@.+> shape needs the angle
    # brackets and misses this, which is exactly why the server-side cleaner is preferred.
    raw_sent = ("Bedankt voor je bericht.\n\nWe zien je donderdag.\n\n"
                "Van:Sam <sam@example.com>\nVerzonden:maandag 1 juni 2026 9:00\n"
                "Onderwerp:Afspraak\n\nKan het vroeger?")

    def test_clean_body_is_diffed_and_the_raw_trailer_is_still_shown(self):
        item = {"proposed_body": self.proposed, "sent_body": self.raw_sent,
                "sent_body_clean": "Bedankt voor je bericht.\n\nWe zien je donderdag."}
        diff = sdr.diff_for(item)
        self.assertEqual([r.kind for r in diff.rows], ["equal", "changed"])
        self.assertNotIn("Verzonden", "".join(t for r in diff.rows for _, t in r.inline))

    def test_missing_clean_body_falls_back_to_split_quotes(self):
        item = {"proposed_body": self.proposed, "sent_body": self.raw_sent}
        self.assertEqual(sdr.diff_for(item).rows, sdr.build_diff(self.proposed, self.raw_sent).rows)

    def test_keep_quotes_bypasses_both_cleaners(self):
        item = {"proposed_body": self.proposed, "sent_body": self.raw_sent,
                "sent_body_clean": "Bedankt voor je bericht.\n\nWe zien je donderdag."}
        diff = sdr.diff_for(item, keep_quotes=True)
        self.assertIn("Verzonden", "".join(t for r in diff.rows for _, t in r.inline))


class ShadowReportTest(unittest.TestCase):
    def fixture(self, name: str) -> list[dict]:
        return json.loads((FIXTURES / name).read_text("utf-8"))["deltas"]

    def test_shadow_detection_uses_only_the_wire_boolean(self):
        self.assertTrue(sdr.is_shadow({"shadow": True}))
        self.assertFalse(sdr.is_shadow({"shadow": False}))
        self.assertFalse(sdr.is_shadow({"shadow": "true"}))
        self.assertFalse(sdr.is_shadow({"similarity": 0.0,
                                        "delta_description": "blind shadow comparison"}))

    def test_shadow_fetch_requests_the_verdict_neutral_plane(self):
        args = type("Args", (), {"from_json": "", "shadow": True, "limit": 25,
                                  "project": "", "tenant": ""})()
        result = type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        with patch.object(sdr.subprocess, "run", return_value=result) as run:
            self.assertEqual(sdr.load_evidence(args), {})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--plane") + 1], "shadow")

    def test_shadow_outputs_are_verdict_first_and_show_readiness_context(self):
        items = sorted(self.fixture("shadow.json"), key=sdr.shadow_sort_key)
        diffs = [sdr.diff_for(item) if item.get("proposed_body") and item.get("sent_body")
                 else sdr.Diff([], 0, 0, 0) for item in items]
        md = sdr.render_shadow_markdown(items, diffs, {}, "example / tenant", "", "report.html")
        page = sdr.render_shadow(items, diffs, {}, "example / tenant", "")

        for output in (md, page):
            self.assertIn("50%", output)
            self.assertIn("2/4", output)
            self.assertIn("served score 5/5", output.lower())
            self.assertIn("Synthetic two-part question", output)
            self.assertIn("Is lunch included", output)
            self.assertNotIn("removed by human", output.lower())
            self.assertNotIn("added by human", output.lower())
        self.assertIn("Our blind proposal", page)
        self.assertIn("Human's independent answer", page)
        self.assertIn("only in ours", md)
        self.assertIn("only in human answer", md)
        self.assertLess(md.index("### Divergent facts"), md.index("### Missed content"))
        self.assertLess(md.index("### Missed content"), md.index("### Equivalent"))
        self.assertIn("Bodies unavailable; use the verdict and description only.", md)

    def test_mixed_payload_partitions_shadow_from_live_aggregates(self):
        fixture = FIXTURES / "mixed.json"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(sdr.main(["--from-json", str(fixture), "--out", str(out)]), 0)
            page = out.read_text("utf-8")
            md = out.with_suffix(".md").read_text("utf-8")

        self.assertIn("Shadow readiness", page)
        self.assertIn("Sent vs proposed — live edits", page)
        self.assertIn("Our blind proposal", page)
        self.assertIn("1 aged out", page)
        self.assertIn("Missing live bodies (1)", page)
        self.assertIn("Synthetic live metadata arrived without requested bodies.", page)
        shadow_md, live_md = md.split("# Sent vs proposed", 1)
        self.assertIn("mixed-shadow", shadow_md)
        self.assertNotIn("mixed-live", shadow_md)
        self.assertIn("mixed-live", live_md)
        self.assertNotIn("mixed-shadow", live_md)
        self.assertIn("1 deltas, most-rewritten first", live_md)
        self.assertIn("aged0003", live_md)
        self.assertIn("The live synthetic edit added a sample policy detail.", live_md)
        self.assertIn("## Missing live bodies", live_md)
        self.assertIn("hollow0004", live_md)

    def test_pure_live_main_keeps_the_existing_renderers_byte_identical(self):
        live = self.fixture("mixed.json")[1]
        payload = {"deltas": [live]}

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 31, 10, 30, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "live.json"
            source.write_text(json.dumps(payload), "utf-8")
            out = Path(tmp) / "live.html"
            diff = sdr.diff_for(live)
            with patch.object(sdr, "datetime", FixedDateTime):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(sdr.main(["--from-json", str(source), "--project", "example",
                                               "--out", str(out)]), 0)
                expected_html = sdr.render([live], [diff], {}, "example", "", 0)
                expected_md = sdr.render_markdown([live], [diff], {}, "example", "", str(out), [])
            self.assertEqual(out.read_text("utf-8"), expected_html)
            self.assertEqual(out.with_suffix(".md").read_text("utf-8"), expected_md)

            with patch.object(sdr, "datetime", FixedDateTime):
                baseline_html = sdr.render([live], [diff], {}, "example", "", 0)
                baseline_md = sdr.render_markdown(
                    [live], [diff], {}, "example", "", "/tmp/example-live.html", [])
            # Captured from the untouched main renderer before shadow dispatch was added.
            self.assertEqual(hashlib.sha256(baseline_html.encode()).hexdigest(),
                             "932af26746645cac966c2f152c155b33d8a4d8df7b83063a549e7a8bde81bb45")
            self.assertEqual(hashlib.sha256(baseline_md.encode()).hexdigest(),
                             "d85a2daa21d1a14a0aee65e1d80f1566e29894d3be8a37e5077a6ccc5e1a92b9")


if __name__ == "__main__":
    unittest.main()
