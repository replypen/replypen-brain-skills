"""The v1 corpus blob emitted by local_imap_harvest.py must round-trip through prepare_harvest.py.

Loads both scripts by path (unittest + importlib, matching test_prepare_harvest.py) and proves the
blob's front-matter and section shape parse with parse_front_matter/split_sections/parse_section, that
roles/participants/span/attachments survive, and that a written export flows through prepare end-to-end.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


def _load(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ph = _load("prepare_harvest")
lih = _load("local_imap_harvest")

MAILBOX = "support@example.test"
UTC = timezone.utc


def _msg(uid: str, sender: str, when: datetime | None, subject: str, text: str,
         attachments: tuple[str, ...] = ()) -> "lih.ParsedMessage":
    return lih.ParsedMessage(
        uid=uid, message_id=f"<{uid}@x>", thread_key="<t@x>", subject=subject, date=when,
        sender=sender, recipients=(MAILBOX,), text=text, attachments=attachments,
    )


class RenderBlobParsesTests(unittest.TestCase):
    def _mixed_group(self) -> list["lih.ParsedMessage"]:
        return [
            _msg("1", "anna@customer.example", datetime(2025, 6, 1, tzinfo=UTC),
                 "Warranty question about widget",
                 "Hello, I would like to know how the warranty on the widget works for us."),
            _msg("2", MAILBOX, datetime(2025, 6, 2, tzinfo=UTC),
                 "Re: Warranty question about widget",
                 "Thanks for reaching out. The warranty covers two years and we will gladly help.",
                 attachments=("manual.pdf",)),
        ]

    def test_front_matter_and_flags_round_trip(self):
        blob = lih.render_corpus_blob(MAILBOX, "2026-01-02T03:04:05Z", cleaned=True, truncated=True,
                                      groups=[self._mixed_group()])
        self.assertTrue(blob.startswith("---\nharvest_format: v1\n"))
        meta, body = ph.parse_front_matter(blob)
        self.assertEqual(meta["harvest_format"], "v1")
        self.assertEqual(meta["mailbox"], MAILBOX)
        self.assertEqual(meta["harvested_at"], "2026-01-02T03:04:05Z")
        self.assertEqual(meta["threads"], "1")
        self.assertEqual(meta["cleaned"], "true")
        self.assertEqual(meta["truncated"], "true")
        self.assertEqual(len(ph.split_sections(body)), 1)

    def test_roles_participants_span_attachments_survive(self):
        blob = lih.render_corpus_blob(MAILBOX, "2026-01-02T03:04:05Z", cleaned=True, truncated=False,
                                      groups=[self._mixed_group()])
        meta, body = ph.parse_front_matter(blob)
        thread = ph.parse_section(ph.split_sections(body)[0], "v1", meta["mailbox"], 0)
        self.assertEqual(thread.subject, "Warranty question about widget")
        # sender==mailbox front-matter classifies mailbox; the real inbound address stays external.
        self.assertEqual([(m.role, m.date) for m in thread.messages],
                         [("external", "2025-06-01"), ("mailbox", "2025-06-02")])
        self.assertEqual(thread.participants, ["anna@customer.example", MAILBOX])
        self.assertEqual(thread.span, ["2025-06-01", "2025-06-02"])
        self.assertEqual(thread.messages[1].attachments, ["manual.pdf"])
        self.assertNotIn("_[attachment", thread.messages[1].body)

    def test_single_date_span_collapses_and_no_subject_placeholder(self):
        group = [_msg("1", MAILBOX, datetime(2020, 1, 1, tzinfo=UTC), "", "One line only.")]
        blob = lih.render_corpus_blob(MAILBOX, "2026-01-02T03:04:05Z", cleaned=False, truncated=False,
                                      groups=[group])
        meta, body = ph.parse_front_matter(blob)
        thread = ph.parse_section(ph.split_sections(body)[0], "v1", meta["mailbox"], 0)
        self.assertEqual(thread.subject, "(no subject)")
        self.assertEqual(thread.span, ["2020-01-01"])
        self.assertEqual([(m.role, m.date) for m in thread.messages], [("mailbox", "2020-01-01")])

    def test_empty_body_no_attachment_message_is_dropped(self):
        group = [
            _msg("1", MAILBOX, datetime(2021, 5, 5, tzinfo=UTC), "Subject here", ""),
            _msg("2", MAILBOX, datetime(2021, 5, 6, tzinfo=UTC), "Subject here",
                 "A real reply that should survive rendering."),
        ]
        blob = lih.render_corpus_blob(MAILBOX, "2026-01-02T03:04:05Z", cleaned=False, truncated=False,
                                      groups=[group])
        meta, body = ph.parse_front_matter(blob)
        thread = ph.parse_section(ph.split_sections(body)[0], "v1", meta["mailbox"], 0)
        self.assertEqual([m.date for m in thread.messages], ["2021-05-06"],
                         "empty-body message with no attachment must not render a dangling header")

    def test_dateless_message_with_body_still_renders_a_parseable_block(self):
        group = [_msg("1", MAILBOX, None, "Subject", "Body with no Date header on the message.")]
        blob = lih.render_corpus_blob(MAILBOX, "2026-01-02T03:04:05Z", cleaned=False, truncated=False,
                                      groups=[group])
        meta, body = ph.parse_front_matter(blob)
        thread = ph.parse_section(ph.split_sections(body)[0], "v1", meta["mailbox"], 0)
        self.assertEqual([(m.role, m.date) for m in thread.messages], [("mailbox", "0001-01-01")])


class WriteOutputEndToEndTests(unittest.TestCase):
    ENV = {
        "RC_MAILBOX_ID": "mb-test",
        "RC_IMAP_EMAIL": MAILBOX,
        "RC_IMAP_USERNAME": "user",
        "RC_IMAP_PASSWORD": "secret",
        "RC_IMAP_HOST": "imap.example.test",
        "RC_IMAP_PORT": "993",
        "RC_IMAP_TLS": "implicit",
    }

    def _sent_messages(self) -> list["lih.ParsedMessage"]:
        return [
            lih.parse_message(*lih._fixture_message(
                "1", "Re: Invoice question", "Tue, 1 Apr 2025 10:00:00 +0000",
                "Thanks, here is the invoice you asked us about earlier this week."), max_chars=2000),
            lih.parse_message(*lih._fixture_message(
                "2", "Delivery status", "Wed, 2 Apr 2025 10:00:00 +0000",
                "Your parcel is on the way and should reach you within two business days."), max_chars=2000),
        ]

    def test_blob_subdir_is_the_only_prepare_corpus_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / ".replypen" / "imap-export"
            lih.write_output(out, self.ENV, "Sent", self._sent_messages(),
                             cleaned=False, truncated=False)
            # INDEX.md and threads/ stay at the top level; corpus_files must pick up only the blob.
            self.assertTrue((out / "INDEX.md").exists())
            self.assertTrue((out / "corpus" / "corpus.md").exists())
            self.assertEqual([p.name for p in ph.corpus_files(out / "corpus")], ["corpus.md"])

    def test_written_export_prepares_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / ".replypen" / "imap-export"
            lih.write_output(out, self.ENV, "Sent", self._sent_messages(),
                             cleaned=False, truncated=False)
            scratch = root / ".replypen" / "scratch"
            cfg = dict(ph.DEFAULTS, holdout_count=0)  # sent-only corpus: no external-question holdouts
            summary = ph.prepare_scratch(out / "corpus", scratch, cfg, export_id="exp-test",
                                         preflight=ph.synthetic_preflight("exp-test"))
            self.assertEqual(summary["threads"], 2)
            self.assertEqual(ph.verify_scratch(scratch), [])
            rows = [__import__("json").loads(line)
                    for line in (scratch / "manifest.jsonl").read_text().splitlines() if line.strip()]
            self.assertEqual([r["source_format"] for r in rows], ["v1", "v1"])
            self.assertTrue(all(r["direction"] == "mailbox_first" for r in rows),
                            "every sent-folder message is mailbox-authored")


if __name__ == "__main__":
    unittest.main()
