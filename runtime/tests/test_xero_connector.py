"""Tests for the Xero connector (manifest + script).

Fixture test: HTTP is mocked with ``responses`` (no live creds, no network). Payloads mirror Xero's
documented example objects (accounting + identity OpenAPI), trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_xero_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import responses as responses_lib
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api
from lib.connectors import xero

TENANT_ID = "a8f9a6b2-1111-4c2e-9a6b-000000000001"
TENANT_ID_2 = "a8f9a6b2-2222-4c2e-9a6b-000000000002"
CONTACT_ID = "bd2270c3-8706-4c11-9cfb-000000000001"
INVOICE_ID = "f0d71f82-0000-4c2e-9a6b-000000000001"
BASE = "https://api.xero.com/api.xro/2.0"
CONNECTIONS_URL = "https://api.xero.com/connections"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "xero" / "manifest.yaml"

_INVOICE_1 = {
    "InvoiceID": INVOICE_ID,
    "InvoiceNumber": "INV-0001",
    "Type": "ACCREC",
    "Status": "AUTHORISED",
    "Reference": "PO 778",
    "Contact": {"ContactID": CONTACT_ID, "Name": "Acme Corp", "EmailAddress": "billing@acme.example"},
    "Date": "/Date(1733011200000+0000)/",
    "DueDate": "/Date(1735689600000+0000)/",
    "Total": 1500.00,
    "AmountDue": 1000.00,
    "AmountPaid": 400.00,
    "AmountCredited": 100.00,
    "CurrencyCode": "USD",
    "UpdatedDateUTC": "/Date(1735000000000+0000)/",
    "LineItems": [{"Description": "Widget A", "Quantity": 10.0, "UnitAmount": 150.0, "LineAmount": 1500.0}],
    "Payments": [
        {"PaymentID": "0d666415-cf77-43fa-80c7-000000000001", "Date": "/Date(1734393600000+0000)/",
         "Amount": 400.00, "Reference": "bank transfer", "HasAccount": False}
    ],
    "CreditNotes": [
        {"CreditNoteID": "c1a7a7c4-0000-4c2e-9a6b-000000000001", "CreditNoteNumber": "CN-0003",
         "AppliedAmount": 100.00, "Total": 100.00, "Date": "/Date(1734480000000+0000)/"}
    ],
    "Prepayments": [],
    "Overpayments": [],
}

_INVOICE_PAID = {
    "InvoiceID": "f0d71f82-0000-4c2e-9a6b-000000000002",
    "InvoiceNumber": "INV-0002",
    "Type": "ACCREC",
    "Status": "PAID",
    "Total": 800.00,
    "AmountDue": 0.0,
    "AmountPaid": 800.00,
    "CurrencyCode": "USD",
    "FullyPaidOnDate": "/Date(1736899200000+0000)/",
}

_CONTACT_1 = {
    "ContactID": CONTACT_ID,
    "ContactStatus": "ACTIVE",
    "Name": "Acme Corp",
    "EmailAddress": "billing@acme.example",
    "IsCustomer": True,
    "IsSupplier": False,
}

_ONE_ORG = [{"id": "c1", "tenantId": TENANT_ID, "tenantType": "ORGANISATION", "tenantName": "Acme Accounting"}]
_TWO_ORGS = _ONE_ORG + [
    {"id": "c2", "tenantId": TENANT_ID_2, "tenantType": "ORGANISATION", "tenantName": "Acme Accounting NZ"}
]


class _XeroCase(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("RC_CONN_XERO")
        os.environ["RC_CONN_XERO"] = "xero_test_bearer_token_mock"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("RC_CONN_XERO", None)
        else:
            os.environ["RC_CONN_XERO"] = self._old

    def run_cli(self, *argv) -> str:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = xero.main(list(argv))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def cli_fails(self, *argv) -> str:
        with self.assertRaises(SystemExit) as cm, patch("sys.stdout", io.StringIO()):
            xero.main(list(argv))
        return str(cm.exception.code)

    @staticmethod
    def qs(call) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(call.request.url).query).items()}

    @staticmethod
    def connections(body=_ONE_ORG):
        responses_lib.add(responses_lib.GET, CONNECTIONS_URL, json=body, status=200)


class TestManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_loads_and_maps_fields(self):
        api.load_manifests()
        m = api.MANIFESTS["xero"]
        self.assertEqual(m.base_url, BASE)
        self.assertEqual(m.auth.strategy, "bearer")
        self.assertEqual((m.pagination.style, m.pagination.page_param, m.pagination.page_start), ("page", "page", 1))
        self.assertEqual(m.rate_limit_remaining_header, "X-MinLimit-Remaining")

    def test_oauth_only_and_help_has_no_raw_lib_api_reads(self):
        raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw["kinds"], ["oauth"])
        # Raw lib.api calls cannot send Xero-tenant-id, so help must not teach them as examples.
        self.assertNotIn("python -m lib.api get xero", raw["help_md"])
        self.assertTrue(raw["onboarding_md"].startswith("Ask "))


class TestPagination(_XeroCase):
    @responses_lib.activate
    def test_two_pages_with_header_bearer_and_page_numbers(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 100})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_PAID] * 3})
        items, truncated = xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")
        self.assertEqual((len(items), truncated), (103, False))
        self.assertEqual([int(self.qs(c)["page"]) for c in responses_lib.calls], [1, 2])
        for c in responses_lib.calls:
            self.assertEqual(c.request.headers["Xero-tenant-id"], TENANT_ID)
            self.assertTrue(c.request.headers["Authorization"].startswith("Bearer "))
            self.assertEqual(self.qs(c)["pageSize"], "100")
            self.assertEqual(c.request.headers["Accept"], "application/json")  # Xero defaults to XML

    @responses_lib.activate
    def test_short_first_page_stops(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 3})
        self.assertEqual(len(xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")[0]), 3)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_page_cap_is_reported_as_truncated(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 100})
        items, truncated = xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices", max_pages=1)
        self.assertEqual((len(items), truncated), (100, True))

    @responses_lib.activate
    def test_first_page_error_raises_instead_of_empty_list(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Type": "AuthenticationUnsuccessful"}, status=403)
        with self.assertRaisesRegex(RuntimeError, "tenants"):
            xero._xero_pages("Invoices", tenant_id="bogus", items_key="Invoices")

    @responses_lib.activate
    def test_mid_stream_error_raises_instead_of_partial_list(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 100})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Title": "Unauthorized"}, status=401)
        with self.assertRaisesRegex(RuntimeError, "reconnect Xero"):
            xero._xero_pages("Invoices", tenant_id=TENANT_ID, items_key="Invoices")

    @responses_lib.activate
    def test_single_get_sends_json_accept(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        xero.get_invoice(TENANT_ID, "INV-0001")
        self.assertEqual(responses_lib.calls[0].request.headers["Accept"], "application/json")


class TestTenantResolution(_XeroCase):
    def test_not_connected_says_so_in_plain_words(self):
        os.environ.pop("RC_CONN_XERO", None)
        with patch.dict(os.environ, {"RC_API_BROKERED_KEYS": ""}):
            self.assertIn("Xero is not connected in this run", self.cli_fails("tenants"))

    @responses_lib.activate
    def test_single_org_is_picked_automatically(self):
        self.connections()
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        out = json.loads(self.run_cli("invoice", "INV-0001"))
        self.assertEqual(out["InvoiceNumber"], "INV-0001")
        self.assertNotIn("Xero-tenant-id", responses_lib.calls[0].request.headers)  # /connections has none
        self.assertEqual(responses_lib.calls[1].request.headers["Xero-tenant-id"], TENANT_ID)

    @responses_lib.activate
    def test_practice_tenants_are_ignored(self):
        self.connections(_ONE_ORG + [{"tenantId": "p-1", "tenantType": "PRACTICE", "tenantName": "Practice"}])
        self.assertEqual(xero.resolve_tenant(), TENANT_ID)

    @responses_lib.activate
    def test_several_orgs_without_choice_lists_them_and_stops(self):
        self.connections(_TWO_ORGS)
        msg = self.cli_fails("contact", "--email", "billing@acme.example")
        self.assertIn("2 organisations", msg)
        for t in _TWO_ORGS:
            self.assertIn(t["tenantName"], msg)
            self.assertIn(t["tenantId"], msg)
        self.assertEqual(len(responses_lib.calls), 1)  # no accounting call against a guessed org

    @responses_lib.activate
    def test_org_by_name_exact_beats_substring(self):
        self.connections(_TWO_ORGS)
        self.assertEqual(xero.resolve_tenant(org="acme accounting"), TENANT_ID)
        self.assertEqual(xero.resolve_tenant(org="NZ"), TENANT_ID_2)

    @responses_lib.activate
    def test_org_ambiguous_or_unknown_fails_with_listing(self):
        self.connections(_TWO_ORGS)
        with self.assertRaisesRegex(RuntimeError, "matches several.*Acme Accounting NZ"):
            xero.resolve_tenant(org="Acme")
        with self.assertRaisesRegex(RuntimeError, "matches none"):
            xero.resolve_tenant(org="Globex")

    @responses_lib.activate
    def test_explicit_tenant_id_skips_lookup(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        self.run_cli("invoice", "INV-0001", "--tenant-id", TENANT_ID_2)
        self.assertEqual(len(responses_lib.calls), 1)
        self.assertEqual(responses_lib.calls[0].request.headers["Xero-tenant-id"], TENANT_ID_2)

    @responses_lib.activate
    def test_no_org_asks_for_reconnect(self):
        self.connections([])
        self.assertIn("reconnect Xero", self.cli_fails("invoices"))

    @responses_lib.activate
    def test_cli_tenants_lists_all(self):
        self.connections(_TWO_ORGS)
        out = self.run_cli("tenants")
        self.assertIn("Acme Accounting NZ", out)
        self.assertIn(TENANT_ID_2, out)


class TestContact(_XeroCase):
    @responses_lib.activate
    def test_email_exact_where_then_open_and_paid_invoices(self):
        self.connections()
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [_CONTACT_1]})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1]})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_PAID]})
        out = self.run_cli("contact", "--email", "billing@acme.example")
        self.assertEqual(self.qs(responses_lib.calls[1])["where"], 'EmailAddress=="billing@acme.example"')
        open_q, paid_q = self.qs(responses_lib.calls[2]), self.qs(responses_lib.calls[3])
        self.assertEqual((open_q["Statuses"], open_q["ContactIDs"]), ("AUTHORISED", CONTACT_ID))
        self.assertEqual((paid_q["Statuses"], paid_q["pageSize"]), ("PAID", "5"))
        self.assertEqual((open_q["where"], paid_q["where"]), ('Type=="ACCREC"', 'Type=="ACCREC"'))  # no supplier bills
        for c in responses_lib.calls[1:]:
            self.assertEqual(c.request.headers["Xero-tenant-id"], TENANT_ID)
        self.assertIn("# Xero: Acme Corp", out)
        self.assertIn("Open invoices (1): 1000.00 USD due", out)
        self.assertIn("`INV-0001`: 1000.00 USD due by 2025-01-01 (400.00 USD already paid)", out)
        self.assertIn("`INV-0002`: 800.00 USD settled on 2025-01-15 (800.00 USD paid)", out)
        self.assertNotIn("lower bound", out)

    @responses_lib.activate
    def test_paid_by_credit_is_not_shown_as_cash(self):
        credited = dict(_INVOICE_PAID, AmountPaid=0.0, AmountCredited=800.0)
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [_CONTACT_1]})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": []})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [credited]})
        out = self.run_cli("contact", "--email", "billing@acme.example", "--tenant-id", TENANT_ID)
        self.assertIn("(0.00 USD paid + 800.00 USD credited)", out)

    @responses_lib.activate
    def test_several_matches_never_pick_one(self):
        twin = dict(_CONTACT_1, ContactID="bd2270c3-8706-4c11-9cfb-000000000002", Name="Acme Corp (old)")
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [_CONTACT_1, twin]})
        out = self.run_cli("contact", "--email", "billing@acme.example", "--tenant-id", TENANT_ID)
        self.assertIn("several contacts match", out)
        self.assertIn(twin["ContactID"], out)
        self.assertEqual(len(responses_lib.calls), 1)  # no invoices fetched for a guessed contact

    @responses_lib.activate
    def test_exact_name_wins_among_fragment_matches(self):
        sub = dict(_CONTACT_1, ContactID="bd2270c3-8706-4c11-9cfb-000000000003", Name="Acme Corp Europe")
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [sub, _CONTACT_1]})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": []})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": []})
        s = xero.contact_summary(TENANT_ID, ref="acme corp")
        self.assertEqual(s["contact"]["ContactID"], CONTACT_ID)

    @responses_lib.activate
    def test_open_list_capped_says_total_is_lower_bound(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [_CONTACT_1]})
        for _ in range(3):
            responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 100})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": []})
        out = self.run_cli("contact", "--email", "billing@acme.example", "--tenant-id", TENANT_ID)
        self.assertIn("lower bound", out)

    @responses_lib.activate
    def test_email_where_miss_falls_back_to_search_term_case_insensitive(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": []})
        other = dict(_CONTACT_1, ContactID="x", Name="Acme Sub", EmailAddress="ops+billing@acme.example")
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [other, _CONTACT_1]})
        found = xero.find_contacts_by_email(TENANT_ID, "Billing@Acme.example")
        self.assertEqual(self.qs(responses_lib.calls[1])["searchTerm"], "Billing@Acme.example")
        self.assertEqual([c["Name"] for c in found], ["Acme Corp"])

    @responses_lib.activate
    def test_email_not_found_suggests_company_name(self):
        self.connections()
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": []})
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": []})
        out = self.run_cli("contact", "--email", "nobody@acme.example")
        self.assertIn("not found", out)
        self.assertIn("company name", out)

    def test_email_with_quote_is_rejected_before_any_call(self):
        with self.assertRaisesRegex(RuntimeError, "plain address"):
            xero.find_contacts_by_email(TENANT_ID, 'x"||1==1@a.b')

    @responses_lib.activate
    def test_name_uses_search_term_without_id_lookup(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts", json={"Contacts": [_CONTACT_1]})
        self.assertEqual(xero.find_contacts(TENANT_ID, "Acme")[0]["Name"], "Acme Corp")
        self.assertEqual(self.qs(responses_lib.calls[0])["searchTerm"], "Acme")

    @responses_lib.activate
    def test_unknown_contact_id_is_not_found(self):
        missing = "00000000-0000-4000-8000-000000000000"
        responses_lib.add(responses_lib.GET, f"{BASE}/Contacts/{missing}", json={"Type": "NotFound"}, status=404)
        s = xero.contact_summary(TENANT_ID, ref=missing)
        self.assertFalse(s["found"])
        self.assertEqual(len(responses_lib.calls), 1)

    def test_contact_needs_exactly_one_key(self):
        msg = self.cli_fails("contact", "--tenant-id", TENANT_ID)
        self.assertIn("exactly one", msg)


class TestInvoice(_XeroCase):
    @responses_lib.activate
    def test_detail_has_payments_credit_notes_and_iso_dates(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        out = json.loads(self.run_cli("invoice", "INV-0001", "--tenant-id", TENANT_ID))
        self.assertEqual(out["Reference"], "PO 778")
        self.assertEqual(out["DueDate"], "2025-01-01")
        self.assertEqual(out["Payments"], [{"PaymentID": "0d666415-cf77-43fa-80c7-000000000001",
                                             "Date": "2024-12-17", "Amount": 400.0, "Reference": "bank transfer"}])
        self.assertEqual(out["CreditNotes"][0]["CreditNoteNumber"], "CN-0003")
        self.assertNotIn("Prepayments", out)
        self.assertNotIn("OnlineInvoiceUrl", out)

    @responses_lib.activate
    def test_paid_date_shown(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0002", json={"Invoices": [_INVOICE_PAID]})
        out = json.loads(self.run_cli("invoice", "INV-0002", "--tenant-id", TENANT_ID))
        self.assertEqual(out["FullyPaidOnDate"], "2025-01-15")

    @responses_lib.activate
    def test_bad_number_fails_loudly(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-9999", json={"Type": "NotFound"}, status=404)
        msg = self.cli_fails("invoice", "INV-9999", "--tenant-id", TENANT_ID)
        self.assertIn("No Xero invoice `INV-9999`", msg)

    @responses_lib.activate
    def test_online_url_for_authorised_invoice(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        responses_lib.add(
            responses_lib.GET, f"{BASE}/Invoices/{INVOICE_ID}/OnlineInvoice",
            json={"OnlineInvoices": [{"OnlineInvoiceUrl": "https://in.xero.com/abc123"}]},
        )
        out = json.loads(self.run_cli("invoice", "INV-0001", "--online-url", "--tenant-id", TENANT_ID))
        self.assertEqual(out["OnlineInvoiceUrl"], "https://in.xero.com/abc123")
        self.assertEqual(responses_lib.calls[1].request.headers["Xero-tenant-id"], TENANT_ID)

    @responses_lib.activate
    def test_online_url_skipped_for_draft(self):
        draft = dict(_INVOICE_1, Status="DRAFT")
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [draft]})
        out = json.loads(self.run_cli("invoice", "INV-0001", "--online-url", "--tenant-id", TENANT_ID))
        self.assertIn("DRAFT", out["OnlineInvoiceUrlUnavailable"])
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_online_url_rejected_by_xero_is_unavailable(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/{INVOICE_ID}/OnlineInvoice",
                          json={"Type": "ValidationException"}, status=400)
        out = json.loads(self.run_cli("invoice", "INV-0001", "--online-url", "--tenant-id", TENANT_ID))
        self.assertIn("no online invoice", out["OnlineInvoiceUrlUnavailable"])

    @responses_lib.activate
    def test_online_url_auth_failure_is_an_error_not_unavailable(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", json={"Invoices": [_INVOICE_1]})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/{INVOICE_ID}/OnlineInvoice",
                          json={"Title": "Unauthorized"}, status=401)
        self.assertIn("reconnect Xero", self.cli_fails("invoice", "INV-0001", "--online-url", "--tenant-id", TENANT_ID))


class TestGetPassthrough(_XeroCase):
    @responses_lib.activate
    def test_adds_tenant_header_and_query_and_picks_rows(self):
        self.connections()
        responses_lib.add(
            responses_lib.GET, f"{BASE}/CreditNotes",
            json={"CreditNotes": [{"CreditNoteNumber": "CN-0003", "Status": "AUTHORISED", "RemainingCredit": 50.0,
                                   "LineItems": [{"Description": "x"}]}]},
        )
        out = json.loads(self.run_cli("get", "CreditNotes", "--query", 'where=Status=="AUTHORISED"',
                                      "--pick", "CreditNoteNumber,RemainingCredit"))
        self.assertEqual(out, {"CreditNotes": [{"CreditNoteNumber": "CN-0003", "RemainingCredit": 50.0}]})
        call = responses_lib.calls[1]
        self.assertEqual(call.request.headers["Xero-tenant-id"], TENANT_ID)
        self.assertEqual(self.qs(call)["where"], 'Status=="AUTHORISED"')

    @responses_lib.activate
    def test_max_pages_collects_list(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Payments", json={"Payments": [{"PaymentID": "p"}] * 100})
        responses_lib.add(responses_lib.GET, f"{BASE}/Payments", json={"Payments": [{"PaymentID": "q"}]})
        out = json.loads(self.run_cli("get", "Payments", "--max-pages", "3", "--tenant-id", TENANT_ID))
        self.assertEqual((len(out["Payments"]), out["truncated"]), (101, False))

    @responses_lib.activate
    def test_custom_page_size_keeps_paging(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 10})
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices", json={"Invoices": [_INVOICE_1] * 4})
        out = json.loads(self.run_cli("get", "Invoices", "--query", "pageSize=10", "--max-pages", "3",
                                      "--tenant-id", TENANT_ID, "--pick", "InvoiceNumber"))
        self.assertEqual((len(out["Invoices"]), out["truncated"]), (14, False))
        self.assertEqual([self.qs(c)["pageSize"] for c in responses_lib.calls], ["10", "10"])

    @responses_lib.activate
    def test_paging_a_non_list_path_fails(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Organisation", json={"Organisations": [{"Name": "Demo"}]})
        self.assertIn("not a pageable list",
                      self.cli_fails("get", "Organisation", "--max-pages", "2", "--tenant-id", TENANT_ID))

    @responses_lib.activate
    def test_missing_path_fails_loudly(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Nope", body="Not found", status=404)
        self.assertIn("nothing at `Nope`", self.cli_fails("get", "Nope", "--tenant-id", TENANT_ID))

    def test_rejects_other_hosts(self):
        self.assertIn("path under", self.cli_fails("get", "https://evil.example/x", "--tenant-id", TENANT_ID))


class TestErrors(_XeroCase):
    @responses_lib.activate
    def test_revoked_grant_surfaces_reconnect_hint(self):
        responses_lib.add(responses_lib.GET, CONNECTIONS_URL, json={"Title": "Unauthorized"}, status=401)
        msg = self.cli_fails("invoice", "INV-0001")
        self.assertIn("reconnect Xero in ReplyPen", msg)
        self.assertIn("HTTP 401", msg)

    @responses_lib.activate
    def test_missing_scope_hint_covers_permissions(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001", status=401,
                          headers={"WWW-Authenticate": 'Bearer error="insufficent_scope"'})
        self.assertIn("read permission missing", self.cli_fails("invoice", "INV-0001", "--tenant-id", TENANT_ID))

    @responses_lib.activate
    def test_wrong_tenant_points_at_tenants(self):
        responses_lib.add(responses_lib.GET, f"{BASE}/Invoices/INV-0001",
                          json={"Type": "AuthenticationUnsuccessful"}, status=403)
        self.assertIn("tenants", self.cli_fails("invoice", "INV-0001", "--tenant-id", "bogus"))


class TestTokenHygiene(unittest.TestCase):
    _TOKEN_PREFIXES = (("eyJ" "0"),)  # split so this guard doesn't flag itself

    def test_no_token_prefixes_in_xero_files(self):
        connector_dir = MANIFEST_PATH.parent
        offenders = [
            f"{p.name}: {pref!r}"
            for p in connector_dir.rglob("*") if p.is_file()
            for pref in self._TOKEN_PREFIXES if pref in p.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
