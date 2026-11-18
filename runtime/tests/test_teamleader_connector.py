"""Fixture test for the manifest-ONLY Teamleader Focus connector.

Teamleader's API is RPC-style: every read is `POST https://api.focus.teamleader.eu/<resource>.<action>`
with a JSON body, and pagination lives in that body (`page: {size, number}`), so each call is one page
and the agent advances `page.number` itself. Payloads are trimmed from Teamleader's documented examples
(developer.focus.teamleader.eu, embedded OpenAPI). No live creds, no network.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_teamleader_connector.py -q
"""

import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import responses as resp_mock
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

BASE = "https://api.focus.teamleader.eu"
_TOKEN = "teamleader" "_fixture_tok"
_MANIFEST = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "teamleader" / "manifest.yaml"

_USERS_ME = {
    "data": {
        "id": "00000000-0000-0000-0000-00000000a001",
        "account": {"type": "account", "id": "00000000-0000-0000-0000-00000000acc1"},
        "first_name": "Ann",
        "last_name": "Example",
        "email": "ann@example.be",
        "language": "nl",
        "status": "active",
    }
}

_CONTACTS = {
    "data": [
        {
            "id": "00000000-0000-0000-0000-00000000c001",
            "first_name": "Jan",
            "last_name": "Peeters",
            "emails": [{"type": "primary", "email": "jan@example.be"}],
        }
    ]
}


def _invoice(n: int, status: str = "outstanding") -> dict:
    return {
        "id": f"00000000-0000-0000-0000-0000000{n:05d}",
        "invoice_number": f"2026 / {n}",
        "status": status,
        "due_on": "2026-12-01",
        "paid": status == "matched",
        "invoicee": {"name": "Example BV", "customer": {"type": "company", "id": "00000000-0000-0000-0000-00000000b001"}},
        "total": {"due": {"amount": 121.0 * n, "currency": "EUR"}},
    }


_INVOICES_PAGE_1 = {"data": [_invoice(1), _invoice(2)]}
_INVOICES_PAGE_2 = {"data": [_invoice(3)]}

_INVOICE_INFO = {
    "data": {
        **_invoice(3),
        "paid_at": None,
        "sent": True,
        "payments": [],
        "total": {
            "payable": {"amount": 363.0, "currency": "EUR"},
            "due": {"amount": 363.0, "currency": "EUR"},
        },
    }
}


class TeamleaderConnector(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_TEAMLEADER")
        os.environ["RC_CONN_TEAMLEADER"] = _TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_TEAMLEADER", None)
        else:
            os.environ["RC_CONN_TEAMLEADER"] = self._saved

    def _cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = api._main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_manifest_loads(self):
        m = api.load_manifests()["teamleader"]
        self.assertEqual(m.base_url, BASE)
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual(m.pagination.style, "none")
        self.assertIn("/invoices.list", m.allowed_post_paths)

    def test_get_allowlist_pins_the_same_reviewed_reads(self):
        # The broker forwards every GET when read_endpoints.get is empty; it must mirror the POST list.
        raw = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))
        reads = raw["read_endpoints"]
        self.assertEqual(reads["get"], reads["post"])
        self.assertTrue(all(p.endswith((".list", ".info", ".me")) for p in reads["post"]), reads["post"])
        self.assertFalse(any("*" in p for p in reads["post"]))
        self.assertEqual(raw["kinds"], ["oauth"])
        self.assertEqual(
            sorted(raw["oauth"]["default_scopes"]),
            sorted(["contacts", "companies", "invoices", "projects", "deals", "users"]),
        )

    @resp_mock.activate
    def test_users_me_is_a_bearer_post(self):
        resp_mock.add(resp_mock.POST, f"{BASE}/users.me", json=_USERS_ME, status=200)
        rc, out, err = self._cli("post", "teamleader", "users.me", "--json", "{}", "--pick", "data.account.id,data.email")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            json.loads(out),
            {"data.account.id": "00000000-0000-0000-0000-00000000acc1", "data.email": "ann@example.be"},
        )
        call = resp_mock.calls[0]
        self.assertEqual(call.request.method, "POST")
        self.assertEqual(call.request.headers["Authorization"], f"Bearer {_TOKEN}")
        self.assertEqual(json.loads(call.request.body), {})

    @resp_mock.activate
    def test_contact_lookup_by_primary_email(self):
        resp_mock.add(resp_mock.POST, f"{BASE}/contacts.list", json=_CONTACTS, status=200)
        body = {"filter": {"email": {"type": "primary", "email": "jan@example.be"}}}
        rc, out, err = self._cli("post", "teamleader", "contacts.list", "--json", json.dumps(body), "--pick", "data.*.id")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {"data.*.id": ["00000000-0000-0000-0000-00000000c001"]})
        self.assertEqual(json.loads(resp_mock.calls[0].request.body), body)

    @resp_mock.activate
    def test_invoice_list_two_pages_via_body_page_number(self):
        # Body pagination: one call per page, the agent raises page.number while a page comes back full.
        resp_mock.add(resp_mock.POST, f"{BASE}/invoices.list", json=_INVOICES_PAGE_1, status=200)
        resp_mock.add(resp_mock.POST, f"{BASE}/invoices.list", json=_INVOICES_PAGE_2, status=200)
        c = api.client(api.load_manifests()["teamleader"])
        flt = {"customer": {"type": "company", "id": "00000000-0000-0000-0000-00000000b001"}, "status": ["outstanding"]}
        items = []
        for number in (1, 2):
            page = c.post("invoices.list", json_body={"filter": flt, "page": {"size": 2, "number": number}})
            items += page["data"]
        self.assertEqual([i["invoice_number"] for i in items], ["2026 / 1", "2026 / 2", "2026 / 3"])
        sent = [json.loads(call.request.body) for call in resp_mock.calls]
        self.assertEqual([b["page"] for b in sent], [{"size": 2, "number": 1}, {"size": 2, "number": 2}])
        self.assertTrue(all(b["filter"] == flt for b in sent))
        self.assertEqual(len(resp_mock.calls), 2)  # style none: lib.api never follows on its own

    @resp_mock.activate
    def test_invoice_info_picks_payment_fields(self):
        resp_mock.add(resp_mock.POST, f"{BASE}/invoices.info", json=_INVOICE_INFO, status=200)
        rc, out, err = self._cli(
            "post", "teamleader", "invoices.info", "--json", '{"id":"00000000-0000-0000-0000-000000000003"}',
            "--pick", "data.paid,data.due_on,data.total.due.amount,data.sent",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            json.loads(out),
            {"data.paid": False, "data.due_on": "2026-12-01", "data.total.due.amount": 363.0, "data.sent": True},
        )

    @resp_mock.activate
    def test_projects_v2_path_is_allowed(self):
        resp_mock.add(resp_mock.POST, f"{BASE}/projects-v2/projects.list", json={"data": []}, status=200)
        rc, _, err = self._cli(
            "post", "teamleader", "projects-v2/projects.list",
            "--json", '{"filter":{"customers":[{"type":"company","id":"x"}]}}',
        )
        self.assertEqual(rc, 0, err)

    @resp_mock.activate
    def test_bad_id_fails_loudly(self):
        resp_mock.add(
            resp_mock.POST, f"{BASE}/invoices.info",
            json={"errors": [{"title": "Invoice not found", "status": 404}]}, status=404,
        )
        c = api.client(api.load_manifests()["teamleader"])
        with self.assertRaises(api.ApiError) as ctx:
            c.post("invoices.info", json_body={"id": "does-not-exist"})
        self.assertEqual(ctx.exception.status, 404)

    @resp_mock.activate
    def test_writes_are_refused_before_any_request(self):
        # Writes, plus reads kept out on purpose (quotations need an unrequested scope; users.info
        # exposes staff phone numbers and rates).
        for path in ("contacts.update", "invoices.book", "invoices.send", "contacts.delete", "companies.add",
                     "quotations.list", "users.info"):
            rc, _, err = self._cli("post", "teamleader", path, "--json", '{"id":"x"}')
            self.assertEqual(rc, 2, path)
            self.assertIn("read-only POST allowlist", err)
        rc, _, _ = self._cli("put", "teamleader", "contacts.info", "--json", "{}")
        self.assertEqual(rc, 2)
        self.assertEqual(len(resp_mock.calls), 0)

    def test_no_token_material_in_connector_dir(self):
        text = _MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("RC_CONN_TEAMLEADER=", text)
        self.assertNotIn("Bearer ey", text)


if __name__ == "__main__":
    unittest.main()
