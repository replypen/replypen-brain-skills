"""Fixture test for the QuickBooks Online connector (lib.connectors.quickbooks).

Force-code triggers that required a script:
(a) field pre-selection, (d) pagination embedded in QB SQL, (e) QB SQL query DSL.

No live creds, no network. HTTP is mocked with ``responses``. The fixture bodies mirror
Intuit's documented QB Online API response shapes, trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_quickbooks_connector.py -q
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlsplit
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
import lib.connectors.quickbooks as qb  # noqa: E402

REALM_ID = "123456789"
# In a run the broker resolves the company base; the connector sends paths relative to it.
BROKER = "http://rc-broker.internal/quickbooks"
QUERY_URL = f"{BROKER}/query"
LOCAL_BASE = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM_ID}"

# ---------------------------------------------------------------------------
# Documented example payloads (from Intuit QB Online API docs)
# ---------------------------------------------------------------------------

# Two-page Customer query response. QB returns items under QueryResponse.<Entity>.
# Page 1: 2 customers (matching MAXRESULTS 2 to force a page 2 fetch).
_CUSTOMER_PAGE_1 = {
    "QueryResponse": {
        "Customer": [
            {
                "Id": "1",
                "DisplayName": "Amy Lauterbach",
                "PrimaryEmailAddr": {"Address": "Amy@Lauterbach.com"},
                "PrimaryPhone": {"FreeFormNumber": "555-1234"},
                "CompanyName": "Lauterbach Inc",
                "Balance": 239.00,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-10T12:00:00"},
            },
            {
                "Id": "2",
                "DisplayName": "Bill Lucchini",
                "PrimaryEmailAddr": {"Address": "Bill@Lucchini.com"},
                "Balance": 0.00,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-09T10:00:00"},
            },
        ],
        "startPosition": 1,
        "maxResults": 2,
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Page 2: 1 customer (fewer than MAXRESULTS → exhausted).
_CUSTOMER_PAGE_2 = {
    "QueryResponse": {
        "Customer": [
            {
                "Id": "3",
                "DisplayName": "Cool Cars",
                "PrimaryEmailAddr": {"Address": "cool@cars.com"},
                "Balance": 1500.00,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-08T09:00:00"},
            },
        ],
        "startPosition": 3,
        "maxResults": 1,
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Single customer direct-read response (GET /customer/{id}).
_CUSTOMER_SINGLE = {
    "Customer": {
        "Id": "42",
        "DisplayName": "Geeta Kalapatapu",
        "PrimaryEmailAddr": {"Address": "Geeta@Kalapatapu.com"},
        "Balance": 629.10,
        "Active": True,
        "CompanyName": "Kalapatapu Exports",
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Invoice list response (one invoice).
_INVOICE_LIST = {
    "QueryResponse": {
        "Invoice": [
            {
                "Id": "1001",
                "DocNumber": "1037",
                "TxnDate": "2024-01-05",
                "DueDate": "2024-02-04",
                "TotalAmt": 582.50,
                "Balance": 582.50,
                "CustomerRef": {"value": "42", "name": "Geeta Kalapatapu"},
                "EmailStatus": "NotSet",
                "PrintStatus": "NotSet",
                "MetaData": {"LastUpdatedTime": "2024-01-05T08:00:00"},
            },
        ],
        "startPosition": 1,
        "maxResults": 1,
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Single invoice direct-read (GET /invoice/{id}).
_INVOICE_SINGLE = {
    "Invoice": {
        "Id": "1001",
        "DocNumber": "1037",
        "TxnDate": "2024-01-05",
        "DueDate": "2024-02-04",
        "TotalAmt": 582.50,
        "Balance": 582.50,
        "CustomerRef": {"value": "42", "name": "Geeta Kalapatapu"},
        "EmailStatus": "NotSet",
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}

# Company info.
_COMPANY_INFO = {
    "CompanyInfo": {
        "CompanyName": "Sandbox Company_US_1",
        "LegalName": "Sandbox Company_US_1",
        "CompanyAddr": {
            "Id": "1",
            "Line1": "2500 Garcia Ave",
            "City": "Mountain View",
            "CountrySubDivisionCode": "CA",
            "PostalCode": "94043",
            "Country": "US",
        },
        "FiscalYearStartMonth": "January",
        "Country": "US",
        "Email": {"Address": "donotreply@intuit.com"},
        "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00"},
    },
    "time": "2024-01-10T15:00:00.000-08:00",
}


_PAYMENT_LIST = {
    "QueryResponse": {
        "Payment": [
            {
                "Id": "77",
                "TxnDate": "2024-01-20",
                "TotalAmt": 582.50,
                "UnappliedAmt": 0,
                "CustomerRef": {"value": "42", "name": "Geeta Kalapatapu"},
                "Line": [{"Amount": 582.50, "LinkedTxn": [{"TxnId": "1001", "TxnType": "Invoice"}]}],
            }
        ],
        "startPosition": 1,
        "maxResults": 1,
    }
}

_FAULT_401 = {"fault": {"error": [{"message": "message=AuthenticationFailed", "code": "100"}], "type": "AUTHENTICATION"}}

_ENV_KEYS = ("RC_CONN_QUICKBOOKS", "RC_CONN_QUICKBOOKS_REALM_ID", "RC_CONN_QUICKBOOKS_API_HOST", "RC_API_BROKERED_KEYS")


def _sql(call) -> str:
    return parse_qs(urlsplit(call.request.url).query)["query"][0]


class _Base(unittest.TestCase):
    """Run mode: the host brokers quickbooks (credential + company base stay host-side)."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._orig_env = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
        os.environ["RC_API_BROKERED_KEYS"] = "quickbooks"

    def tearDown(self):
        for key, val in self._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class QuickbooksManifest(_Base):
    def test_manifest_loaded_from_yaml(self):
        qbm = api.load_manifests()["quickbooks"]
        self.assertEqual(qbm.base_url, "https://{api_host}/v3/company/{realm_id}")
        self.assertEqual(qbm.auth.strategy, "bearer")
        self.assertEqual(qbm.pagination.style, "none")
        self.assertEqual(qbm.default_headers.get("Accept"), "application/json")

    def test_manifest_is_oauth_only_without_env_var_instructions(self):
        import yaml

        raw = yaml.safe_load((Path(qb.__file__).parent / "manifest.yaml").read_text())
        self.assertEqual(raw["kinds"], ["oauth"])
        self.assertEqual(raw["oauth"]["default_scopes"], ["com.intuit.quickbooks.accounting"])
        self.assertNotIn("RC_CONN", raw["help_md"])
        self.assertNotIn("realmId", raw["onboarding_md"])


class QuickbooksBrokered(_Base):
    @responses_lib.activate
    def test_customer_query_stitches_two_pages_relative_to_company(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_1, status=200)
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_2, status=200)
        results = qb._qb_query("SELECT Id FROM Customer", "Customer", page_size=2, max_records=50)
        self.assertEqual([r["Id"] for r in results], ["1", "2", "3"])
        self.assertTrue(_sql(responses_lib.calls[0]).endswith("STARTPOSITION 1 MAXRESULTS 2"))
        self.assertTrue(_sql(responses_lib.calls[1]).endswith("STARTPOSITION 3 MAXRESULTS 2"))
        for call in responses_lib.calls:
            q = parse_qs(urlsplit(call.request.url).query)
            self.assertEqual(q["minorversion"], ["75"])
            self.assertEqual(call.request.headers.get("Accept"), "application/json")
            self.assertNotIn("Authorization", call.request.headers)  # the broker attaches it

    @responses_lib.activate
    def test_stops_when_fewer_than_page_size(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_2, status=200)
        self.assertEqual(len(qb._qb_query("SELECT Id FROM Customer", "Customer", page_size=2)), 1)
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_customer_by_email_uses_server_filter(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_2, status=200)
        found = qb.query_customer(email="cool@cars.com")
        self.assertEqual([c["Id"] for c in found], ["3"])
        sql = _sql(responses_lib.calls[0])
        self.assertTrue(sql.startswith("SELECT * FROM Customer WHERE PrimaryEmailAddr = 'cool@cars.com'"))
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_customer_by_email_falls_back_to_local_match_when_filter_refused(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json={"Fault": {"Error": [{"code": "4000"}]}}, status=400)
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_1, status=200)
        found = qb.query_customer(email="AMY@lauterbach.com")
        self.assertEqual([c["Id"] for c in found], ["1"])  # case-insensitive local match
        self.assertTrue(_sql(responses_lib.calls[1]).startswith("SELECT * FROM Customer STARTPOSITION 1"))

    @responses_lib.activate
    def test_customer_name_and_email_quotes_and_filters(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_CUSTOMER_PAGE_1, status=200)
        found = qb.query_customer("O'Brien", email="bill@lucchini.com")
        self.assertEqual([c["Id"] for c in found], ["2"])
        self.assertIn("DisplayName LIKE '%O\\'Brien%'", _sql(responses_lib.calls[0]))

    def test_stale_local_vars_never_beat_the_broker(self):
        os.environ["RC_CONN_QUICKBOOKS_REALM_ID"] = "not-a-number"
        os.environ["RC_CONN_QUICKBOOKS_API_HOST"] = "evil.example.com"
        with responses_lib.RequestsMock() as rsps:
            rsps.add(responses_lib.GET, f"{BROKER}/invoice/1001", json=_INVOICE_SINGLE, status=200)
            self.assertEqual(qb.query_invoices(invoice_id="1001")[0]["Id"], "1001")

    @responses_lib.activate
    def test_customer_direct_read_by_id(self):
        responses_lib.add(responses_lib.GET, f"{BROKER}/customer/42", json=_CUSTOMER_SINGLE, status=200)
        results = qb.query_customer(customer_id="42")
        self.assertEqual(results[0]["Id"], "42")

    def test_bad_id_fails_loudly_without_a_call(self):
        with self.assertRaisesRegex(RuntimeError, "invalid QuickBooks customer id"):
            qb.query_customer(customer_id="42 OR 1=1")
        with self.assertRaisesRegex(RuntimeError, "invalid QuickBooks invoice id"):
            qb.query_invoices(invoice_id="../companyinfo")

    @responses_lib.activate
    def test_invoice_by_number(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_INVOICE_LIST, status=200)
        invs = qb.query_invoices(number="1037")
        self.assertEqual(invs[0]["DocNumber"], "1037")
        self.assertIn("DocNumber = '1037'", _sql(responses_lib.calls[0]))

    @responses_lib.activate
    def test_open_invoices_for_customer(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_INVOICE_LIST, status=200)
        qb.query_invoices(customer_id="42", open_only=True)
        sql = _sql(responses_lib.calls[0])
        self.assertIn("CustomerRef = '42'", sql)
        self.assertIn("Balance > '0'", sql)

    @responses_lib.activate
    def test_invoice_direct_read(self):
        responses_lib.add(responses_lib.GET, f"{BROKER}/invoice/1001", json=_INVOICE_SINGLE, status=200)
        self.assertEqual(qb.query_invoices(invoice_id="1001")[0]["Id"], "1001")

    @responses_lib.activate
    def test_payments_show_linked_invoice(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_PAYMENT_LIST, status=200)
        md = qb.payments_to_markdown(qb.query_payments(customer_id="42"))
        self.assertIn("582.50", md)
        self.assertIn("Applied to invoice Ids: 1001", md)

    @responses_lib.activate
    def test_company_info_via_query_needs_no_realm(self):
        responses_lib.add(
            responses_lib.GET, QUERY_URL,
            json={"QueryResponse": {"CompanyInfo": [_COMPANY_INFO["CompanyInfo"]]}}, status=200,
        )
        info = qb.query_company_info()
        self.assertEqual(info["CompanyName"], "Sandbox Company_US_1")
        self.assertIn("FROM CompanyInfo", _sql(responses_lib.calls[0]))

    @responses_lib.activate
    def test_401_becomes_reconnect_hint(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_FAULT_401, status=401)
        with self.assertRaisesRegex(RuntimeError, "reconnect QuickBooks"):
            qb.query_invoices(number="1037")

    @responses_lib.activate
    def test_429_retried_then_surfaces_rate_limit(self):
        for _ in range(10):
            responses_lib.add(responses_lib.GET, QUERY_URL, json={}, status=429, headers={"Retry-After": "0"})
        orig = api._sleep
        api._sleep = lambda *_a, **_k: None
        try:
            with self.assertRaisesRegex(RuntimeError, "rate limit"):
                qb.query_invoices(number="1037")
        finally:
            api._sleep = orig
        self.assertGreater(len(responses_lib.calls), 1)  # lib.api retried before giving up


class QuickbooksNotConnected(unittest.TestCase):
    def setUp(self):
        self._orig_env = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
        os.environ["RC_CONN_QUICKBOOKS"] = "tok"

    def tearDown(self):
        for key, val in self._orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_env_token_without_broker_or_realm_says_connect(self):
        os.environ.pop("RC_CONN_QUICKBOOKS", None)  # the connect hint comes before any token lookup
        with self.assertRaisesRegex(RuntimeError, "not connected in this run"):
            qb.query_company_info()

    @responses_lib.activate
    def test_local_smoke_fallback_uses_realm_and_sandbox_host(self):
        os.environ["RC_CONN_QUICKBOOKS_REALM_ID"] = REALM_ID
        os.environ["RC_CONN_QUICKBOOKS_API_HOST"] = "sandbox-quickbooks.api.intuit.com"
        responses_lib.add(responses_lib.GET, f"{LOCAL_BASE}/invoice/1001", json=_INVOICE_SINGLE, status=200)
        qb.query_invoices(invoice_id="1001")
        self.assertIn("Bearer tok", responses_lib.calls[0].request.headers["Authorization"])

    def test_local_fallback_refuses_foreign_host(self):
        os.environ["RC_CONN_QUICKBOOKS_REALM_ID"] = REALM_ID
        os.environ["RC_CONN_QUICKBOOKS_API_HOST"] = "evil.example.com"
        with self.assertRaisesRegex(RuntimeError, "API_HOST must be one of"):
            qb.query_company_info()


class QuickbooksMarkdown(unittest.TestCase):
    def test_customers(self):
        md = qb.customers_to_markdown(_CUSTOMER_PAGE_1["QueryResponse"]["Customer"])
        self.assertIn("Amy Lauterbach", md)
        self.assertIn("Amy@Lauterbach.com", md)
        self.assertIn("239.00", md)
        self.assertIn("No customers found", qb.customers_to_markdown([]))

    def test_invoices_paid_vs_open(self):
        inv = dict(_INVOICE_SINGLE["Invoice"])
        self.assertIn("open balance 582.50", qb.invoices_to_markdown([inv]))
        inv["Balance"] = 0
        self.assertIn("**paid**", qb.invoices_to_markdown([inv]))

    def test_company(self):
        md = qb.company_to_markdown(_COMPANY_INFO["CompanyInfo"])
        self.assertIn("Sandbox Company_US_1", md)
        self.assertIn("Mountain View", md)


class QuickbooksCLI(_Base):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = qb.main(argv)
        return rc, out.getvalue(), err.getvalue()

    @responses_lib.activate
    def test_cli_invoice_number(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_INVOICE_LIST, status=200)
        rc, out, _ = self._run(["invoice", "--number", "1037"])
        self.assertEqual(rc, 0)
        self.assertIn("Invoice 1037", out)

    @responses_lib.activate
    def test_cli_error_exit_code(self):
        responses_lib.add(responses_lib.GET, QUERY_URL, json=_FAULT_401, status=401)
        rc, _, err = self._run(["customer", "--email", "a@b.c"])
        self.assertEqual(rc, 1)
        self.assertIn("reconnect", err)

    def test_cli_invoice_requires_one_key(self):
        with self.assertRaises(SystemExit):
            self._run(["invoice"])


class QuickbooksTokenHygiene(unittest.TestCase):
    """CI guard: no QB OAuth token material in the connector directory.

    Splits the prefix literals with string concatenation so this guard
    doesn't flag itself.
    """

    # QB OAuth access token prefixes (Intuit tokens start with "Bearer " in headers
    # but the raw token is a long base64 JWT; check for common Intuit SDK placeholder patterns).
    # We guard against accidentally committing a real token by checking for the Intuit
    # prod token placeholder prefixes used in their own docs/SDKs.
    _TOKEN_PREFIXES = (
        "eyJlb" + "mMiOiJBMTI4",          # Intuit prod JWT token prefix (base64 encoded "{"enc":"A128")
        "intuit_" + "token",               # generic placeholder used in Intuit docs
    )

    def test_no_token_material_in_quickbooks_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "quickbooks"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
