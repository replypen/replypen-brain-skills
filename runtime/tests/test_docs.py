"""Unit tests for lib.docs — host document fetch over the broker's `docs` mount, faked with `responses`.

    cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests/test_docs.py -q
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import requests
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import docs  # noqa: E402

PDF = b"%PDF-1.7\n%%EOF\n"
LINK = "https://pay.stripe.com/invoice/acct_1/live_abc/pdf?s=ap"


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    @responses.activate
    def test_fetch_url_writes_the_exact_bytes(self):
        responses.add(responses.POST, docs.BROKER_URL + "url", body=PDF, status=200,
                      headers={"X-Docs-Sha256": hashlib.sha256(PDF).hexdigest()})
        dest = str(self.tmp / "outbox" / "Invoice-1.pdf")
        self.assertEqual(docs.fetch_url(LINK, dest), dest)
        self.assertEqual(Path(dest).read_bytes(), PDF)
        self.assertEqual(json.loads(responses.calls[0].request.body), {"url": LINK})

    @responses.activate
    def test_refusal_carries_the_host_sentence(self):
        responses.add(responses.POST, docs.BROKER_URL + "blob", body="that blob is not visible to this run", status=403)
        with self.assertRaisesRegex(docs.DocsError, "not visible"):
            docs.fetch_blob("42", str(self.tmp / "x.pdf"))
        self.assertFalse((self.tmp / "x.pdf").exists())

    @responses.activate
    def test_damaged_transfer_is_refused(self):
        responses.add(responses.POST, docs.BROKER_URL + "url", body=PDF, status=200, headers={"X-Docs-Sha256": "0" * 64})
        with self.assertRaises(docs.DocsError):
            docs.fetch_url(LINK, str(self.tmp / "x.pdf"))

    @responses.activate
    def test_no_mount_is_unavailable(self):
        responses.add(responses.POST, docs.BROKER_URL + "url", body=requests.exceptions.ConnectionError("no broker"))
        with self.assertRaises(docs.DocsUnavailable):
            docs.fetch_url(LINK, str(self.tmp / "x.pdf"))

    @responses.activate
    def test_link_never_reaches_the_audit_line(self):
        responses.add(responses.POST, docs.BROKER_URL + "url", body=PDF, status=200)
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            docs.fetch_url(LINK, str(self.tmp / "x.pdf"))
        self.assertNotIn("live_abc", err.getvalue())


if __name__ == "__main__":
    unittest.main()
