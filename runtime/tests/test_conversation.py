"""Tests for lib.conversation: the staged conversation file read by code."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import conversation  # noqa: E402

MANIFEST = {"type": "manifest", "schema": "rootcause.conversation.v1", "mode": "email"}


def _write(rows: list[dict] | str) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    with handle:
        handle.write(rows if isinstance(rows, str) else "".join(json.dumps(r) + "\n" for r in rows))
    return handle.name


class ConversationFile(unittest.TestCase):
    def test_last_outbound_is_latest_mailbox_message_without_quoted_history(self):
        path = _write([
            MANIFEST,
            {"type": "message", "phase": "prior", "role": "sender", "body": "Kan ik annuleren?"},
            {"type": "message", "phase": "prior", "role": "mailbox", "body": "Oud antwoord: €10"},
            {"type": "message", "phase": "prior", "role": "mailbox", "sent_at": "2026-10-20T08:02:04Z",
             "body": "Je krijgt €75 terug.\n\nOp ma 19 okt 2026 om 21:09 schreef Ouder\n<o@x.be>:\n> Kan ik annuleren? €170"},
            {"type": "note", "phase": "prior", "role": "assistant_internal", "body": "€999"},
            {"type": "message", "phase": "current", "role": "sender", "body": "Ok zo. Bedankt!"},
        ])
        with mock.patch.dict(os.environ, {"RC_CONVERSATION_PATH": path}):
            last = conversation.last_outbound()
            self.assertEqual(len(conversation.messages()), 4)
        self.assertEqual(last["body"], "Je krijgt €75 terug.")
        self.assertEqual(last["sent_at"], "2026-10-20T08:02:04Z")
        self.assertIn("€170", last["raw_body"])

    def test_no_outbound_message_is_none_not_unavailable(self):
        path = _write([MANIFEST, {"type": "message", "phase": "current", "role": "sender", "body": "Hallo"}])
        self.assertIsNone(conversation.last_outbound(path))

    def test_missing_or_garbled_file_is_unavailable(self):
        with self.assertRaises(conversation.ConversationUnavailable):
            conversation.last_outbound("/nonexistent/rc-conversation.jsonl")
        with self.assertRaises(conversation.ConversationUnavailable):
            conversation.messages(_write('{"type": "manifest"}\nnot json\n'))
        with self.assertRaises(conversation.ConversationUnavailable):
            conversation.messages(_write([{"type": "message", "role": "mailbox", "body": "x"}]))  # no manifest
        with self.assertRaises(conversation.ConversationUnavailable):
            conversation.last_outbound(_write([{**MANIFEST, "schema": "rootcause.conversation.v2"},
                                               {"type": "message", "role": "mailbox", "body": "x"}]))

    def test_strip_quoted_handles_gt_lines_and_outlook_separator(self):
        self.assertEqual(conversation.strip_quoted("Nieuw\n> oud\nnog nieuw"), "Nieuw\nnog nieuw")
        self.assertEqual(conversation.strip_quoted("Nieuw\n-----Original Message-----\nVan: x"), "Nieuw")
        self.assertEqual(conversation.strip_quoted("Nieuw\n\nVan: Ouder <o@x.be>\nVerzonden: maandag\nOud €170"), "Nieuw")

    def test_prose_that_looks_like_an_attribution_line_is_kept(self):
        body = "Op je vorige mail schreef ik al dat we €75 terugstorten."
        self.assertEqual(conversation.strip_quoted(body), body)
        self.assertEqual(conversation.strip_quoted("Nieuw\n\nOp ma 20 okt 2026 om 10:02 schreef X:\noud"), "Nieuw")


class EuroAmounts(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(
            conversation.euro_amounts("€75, € 1.234,50, 75 euro, 12,5 EUR, €40,-, 30€ en €19.99"),
            [7500, 123450, 7500, 1250, 4000, 3000, 1999],
        )

    def test_whole_token_or_nothing(self):
        self.assertEqual(conversation.euro_amounts("€1,234.50 en € 1.234 en €1,23"), [123450, 123400, 123])
        self.assertEqual(conversation.euro_amounts("€1,2345"), [])  # never a numeric prefix

    def test_space_groups_thousands(self):
        self.assertEqual(conversation.euro_amounts("€ 1 234,50"), [123450])
        self.assertEqual(conversation.euro_amounts("€1 250"), [125000])
        self.assertEqual(conversation.euro_amounts("1 250 € en 1\u202f250 €"), [125000, 125000])

    def test_space_never_joins_numbers_across_words(self):
        self.assertEqual(conversation.euro_amounts("op 12 maart 2026 €50, €50 op 12 maart, €40 12 keer"),
                         [5000, 5000, 4000])

    def test_plain_numbers_dates_and_phone_tokens_are_not_amounts(self):
        self.assertEqual(conversation.euro_amounts("3 keer, 20/10/2026, +32 472 24 10 18, 170"), [])

    def test_spans_point_at_the_amount(self):
        text = "We rekenen €10 aan. Je ontvangt €75."
        self.assertEqual([text[s:e] for _, s, e in conversation.euro_spans(text)], ["€10", "€75"])


if __name__ == "__main__":
    unittest.main()
