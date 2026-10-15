"""lib.mail broker contract; no network."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import mail  # noqa: E402


class MailTest(unittest.TestCase):
    ENVELOPE = {"threads": [{"ref": "support:t1", "mailbox": "support", "address": "info@praktijk.be"}],
                "warnings": [], "since_days": 30, "truncated": False}

    def _search(self, **kwargs):
        responses.add(responses.POST, mail.BASE_URL + "/search", json=self.ENVELOPE)
        result = mail.search(**kwargs)
        return result, json.loads(responses.calls[0].request.body)

    @responses.activate
    def test_search_with_returns_whole_envelope(self):
        result, body = self._search(with_email="person@example.com", since_days=30, limit=7)
        self.assertEqual(result, self.ENVELOPE)
        self.assertEqual(body, {"with": "person@example.com", "since_days": 30, "limit": 7})

    @responses.activate
    def test_search_subject_only(self):
        _, body = self._search(subject="Factuur maart")
        self.assertEqual(body, {"subject": "Factuur maart", "since_days": 14, "limit": 20})

    @responses.activate
    def test_search_inbox(self):
        _, body = self._search(inbox=True, since_days=7, mailbox="support")
        self.assertEqual(body, {"inbox": True, "since_days": 7, "limit": 20, "mailbox": "support"})

    @responses.activate
    def test_search_with_and_subject(self):
        _, body = self._search(with_email="a@example.com", subject="afspraak")
        self.assertEqual(body["with"], "a@example.com")
        self.assertEqual(body["subject"], "afspraak")
        self.assertNotIn("inbox", body)

    def test_search_without_criteria_is_refused(self):
        with self.assertRaises(ValueError):
            mail.search()
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
            mail._main(["search", "--since", "30d"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--with, --subject or --inbox", err.getvalue())

    @responses.activate
    def test_cli_search_prints_warnings(self):
        envelope = {"threads": [], "warnings": ["mailbox balie: provider unavailable"], "since_days": 7,
                    "truncated": True}
        responses.add(responses.POST, mail.BASE_URL + "/search", json=envelope)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mail._main(["search", "--subject", "Factuur", "--inbox", "--since", "7d"]), 0)
        self.assertEqual(json.loads(out.getvalue()), envelope)
        self.assertEqual(json.loads(responses.calls[0].request.body),
                         {"subject": "Factuur", "inbox": True, "since_days": 7, "limit": 20})

    @responses.activate
    def test_rejected_subject_sentence_surfaces(self):
        sentence = "subject may not contain quotes, backslashes, braces, parentheses or newlines"
        responses.add(responses.POST, mail.BASE_URL + "/search", body=sentence, status=400)
        with self.assertRaises(mail.MailError) as ctx:
            mail.search(subject='say "hi"')
        self.assertEqual(str(ctx.exception), f"HTTP 400: {sentence}")

    @responses.activate
    def test_reply_draft_has_no_send_field(self):
        responses.add(responses.POST, mail.BASE_URL + "/draft", json={"status": "created"})
        mail.draft(reply="support:t1", body_markdown="Hello", key="followup")
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body["reply"], "support:t1")
        self.assertNotIn("send", body)
        self.assertNotIn("mode", body)

    @responses.activate
    def test_masked_draft_refusal_surfaces_verbatim(self):
        sentence = "a masked value in this draft could not be resolved; write the draft without that value"
        responses.add(responses.POST, mail.BASE_URL + "/draft", body=sentence, status=400)
        with self.assertRaises(mail.MailError) as ctx:
            mail.draft(to=["\u27e6pii:email:1\u27e7"], subject="Herinnering", body_markdown="Beste")
        self.assertEqual(str(ctx.exception), f"HTTP 400: {sentence}")

    @responses.activate
    def test_mailbox_conflict_surfaces(self):
        sentence = "choose mailbox: balie, support"
        responses.add(responses.POST, mail.BASE_URL + "/draft", body=sentence, status=409)
        with self.assertRaises(mail.MailError) as ctx:
            mail.draft(to=["a@example.com"], body_markdown="Hallo")
        self.assertIn(sentence, str(ctx.exception))

    @responses.activate
    def test_server_refusal_is_clear(self):
        responses.add(responses.GET, mail.BASE_URL + "/thread", body="thread ref mailbox is not eligible", status=404)
        with self.assertRaises(mail.MailError) as ctx:
            mail.thread("other:t1")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("not eligible", str(ctx.exception))

    @responses.activate
    def test_absent_mount_is_clear(self):
        with self.assertRaises(mail.MailUnavailable) as ctx:
            mail.mailboxes()
        self.assertEqual(str(ctx.exception), "Mailbox access is not enabled for this run")

    @responses.activate
    def test_cli_reads_body_file(self):
        responses.add(responses.POST, mail.BASE_URL + "/draft", json={"status": "created"})
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("Hello there", encoding="utf-8")
            self.assertEqual(mail._main(["draft", "--to", "a@example.com", "--subject", "Hi", "--body-file", str(body)]), 0)
        self.assertEqual(json.loads(responses.calls[0].request.body)["body_markdown"], "Hello there")


if __name__ == "__main__":
    unittest.main()
