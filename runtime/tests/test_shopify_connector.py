"""Fixture tests for the Shopify Admin GraphQL connector.

Shopify uses GraphQL (POST), not REST. The connector drives POSTs through lib.api so env-token and
brokered modes share method policy/retry behavior. The fixture bodies mirror Shopify's documented
example payloads, trimmed to support-relevant fields.

Exercises:
- Manifest YAML loads via lib.api's YAML loader and maps every field correctly.
- `fetch_orders` auto-pages across two GraphQL cursor pages (pageInfo.hasNextPage / endCursor).
- `fetch_customer` looks up by email and by GID.
- `fetch_product` fetches a product by GID.
- X-Shopify-Access-Token credential rides EVERY request (not just page 1).
- Markdown renderers produce the expected headings / key lines.
- CLI drives orders/customer/product sub-commands end-to-end.
- Token-prefix hygiene guard: no real Shopify token prefix in the connector directory.

No live creds, no network. All HTTP is mocked.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_shopify_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path
import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import shopify as shopify_conn  # noqa: E402

SHOP = "teststore"


def shopify_conn_manifest_help() -> str:
    import yaml
    path = Path(shopify_conn.__file__).with_name("manifest.yaml")
    return yaml.safe_load(path.read_text())["help_md"]
GRAPHQL_URL = f"https://{SHOP}.myshopify.com/admin/api/2026-07/graphql.json"

# ---------------------------------------------------------------------------
# Fixture payloads (Shopify documented example shapes, trimmed to support fields)
# ---------------------------------------------------------------------------

_ORDER_1 = {
    "id": "gid://shopify/Order/1",
    "name": "#1001",
    "createdAt": "2025-01-10T10:00:00Z",
    "updatedAt": "2025-01-10T11:00:00Z",
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "99.00", "currencyCode": "USD"}},
    "customer": {"email": "alice@example.com", "displayName": "Alice Smith"},
    "shippingAddress": {"city": "New York", "countryCode": "US"},
    "lineItems": {"edges": [{"node": {"title": "Widget Pro", "quantity": 2}}]},
    "tags": ["vip"],
    "cancelledAt": None,
    "cancelReason": None,
}

_ORDER_2 = {
    "id": "gid://shopify/Order/2",
    "name": "#1002",
    "createdAt": "2025-01-09T08:00:00Z",
    "updatedAt": "2025-01-09T09:00:00Z",
    "displayFinancialStatus": "UNPAID",
    "displayFulfillmentStatus": "UNFULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "24.50", "currencyCode": "USD"}},
    "customer": {"email": "bob@example.com", "displayName": "Bob Jones"},
    "shippingAddress": {"city": "Austin", "countryCode": "US"},
    "lineItems": {"edges": [{"node": {"title": "Gadget", "quantity": 1}}]},
    "tags": [],
    "cancelledAt": None,
    "cancelReason": None,
}

def _eur(amount: str) -> dict:
    return {"shopMoney": {"amount": amount, "currencyCode": "EUR"}}


def _tx(status: str, amount: str, at: str, *, gateway: str = "stripe", manual: bool = False,
        test: bool = False) -> dict:
    return {"kind": "REFUND", "status": status, "processedAt": at, "gateway": gateway,
            "formattedGateway": "Stripe" if gateway == "stripe" else gateway,
            "manualPaymentGateway": manual, "test": test, "amountSet": _eur(amount)}


_REFUNDED_ORDER = {
    "id": "gid://shopify/Order/3",
    "name": "#1003",
    "createdAt": "2026-11-01T10:00:00Z",
    "updatedAt": "2026-11-24T12:00:00Z",
    "displayFinancialStatus": "PARTIALLY_REFUNDED",
    "displayFulfillmentStatus": "FULFILLED",
    "totalPriceSet": _eur("80.00"),
    "customer": {"email": "carol@example.com", "displayName": "Carol"},
    "shippingAddress": {"city": "Ghent", "countryCode": "BE"},
    "lineItems": {"edges": [{"node": {"title": "Widget Pro", "quantity": 2}}]},
    "tags": [],
    "cancelledAt": None,
    "cancelReason": None,
    "fulfillments": [],
    "returnStatus": "RETURNED",
    "totalRefundedSet": _eur("40.00"),
    "refunds": [
        {
            "createdAt": "2026-11-20T08:59:00Z",
            "processedAt": "2026-11-20T09:00:00Z",
            "note": "damaged on arrival",
            "totalRefundedSet": _eur("25.00"),
            "refundLineItems": {"nodes": [
                {"quantity": 1, "subtotalSet": _eur("25.00"), "lineItem": {"title": "Widget Pro"}},
            ]},
            "transactions": {"nodes": [
                _tx("FAILURE", "25.00", "2026-11-20T09:00:00Z"),
                _tx("SUCCESS", "25.00", "2026-11-20T09:05:00Z"),
            ]},
        },
        {
            "createdAt": "2026-11-24T12:00:00Z",
            "processedAt": "2026-11-24T12:00:00Z",
            "note": None,
            "totalRefundedSet": _eur("15.00"),
            "refundLineItems": {"nodes": []},
            "transactions": {"nodes": [_tx("PENDING", "15.00", "2026-11-24T12:00:00Z")]},
        },
    ],
}

def _mock_order_lookup():
    _mock_post({"data": {"orders": {"nodes": [{"id": "gid://shopify/Order/3", "name": "#1003"}]}}})
    _mock_post({"data": {"order": _REFUNDED_ORDER}})


_PAGE_1_BODY = {
    "data": {
        "orders": {
            "edges": [{"cursor": "cursor1", "node": _ORDER_1}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
        }
    }
}

_PAGE_2_BODY = {
    "data": {
        "orders": {
            "edges": [{"cursor": "cursor2", "node": _ORDER_2}],
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor2"},
        }
    }
}

_CUSTOMER_EMAIL_BODY = {
    "data": {
        "customers": {
            "edges": [
                {
                    "node": {
                        "id": "gid://shopify/Customer/42",
                        "displayName": "Alice Smith",
                        "email": "alice@example.com",
                        "phone": "+15555551234",
                        "numberOfOrders": 3,
                        "amountSpent": {"amount": "245.00", "currencyCode": "USD"},
                        "tags": ["loyal"],
                        "createdAt": "2024-06-01T00:00:00Z",
                        "state": "ENABLED",
                    }
                }
            ]
        }
    }
}

_CUSTOMER_GID_BODY = {
    "data": {
        "customer": {
            "id": "gid://shopify/Customer/42",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
            "phone": "+15555551234",
            "numberOfOrders": 3,
            "amountSpent": {"amount": "245.00", "currencyCode": "USD"},
            "tags": ["loyal"],
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "ENABLED",
        }
    }
}

_PRODUCT_BODY = {
    "data": {
        "product": {
            "id": "gid://shopify/Product/7",
            "title": "Widget Pro",
            "handle": "widget-pro",
            "status": "ACTIVE",
            "tags": ["bestseller"],
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T00:00:00Z",
            "variants": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/ProductVariant/71",
                            "title": "Default Title",
                            "price": "49.99",
                            "sku": "WP-001",
                            "inventoryQuantity": 120,
                            "availableForSale": True,
                        }
                    }
                ]
            },
        }
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_post(body: dict, status: int = 200):
    """Register a `responses` mock for a POST to the Shopify GraphQL endpoint."""
    responses_lib.add(
        responses_lib.POST,
        GRAPHQL_URL,
        json=body,
        status=status,
    )


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class ShopifyManifestLoad(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("shopify", m)
        s = m["shopify"]
        self.assertEqual(s.key, "shopify")
        self.assertIn("myshopify.com", s.base_url)
        self.assertEqual(s.auth.strategy, "api_key_header")
        self.assertEqual(s.auth.name, "X-Shopify-Access-Token")
        # Pagination is `none` — GraphQL cursor is query-embedded, not HTTP-header driven.
        self.assertEqual(s.pagination.style, "none")
        # No remaining-count header (Shopify uses query-cost throttle in response body).
        self.assertEqual(s.rate_limit_remaining_header, "")

    def test_manifest_page_size_field(self):
        m = api.load_manifests()
        self.assertEqual(m["shopify"].pagination.page_size, 50)


# ---------------------------------------------------------------------------
# Orders tests
# ---------------------------------------------------------------------------


class ShopifyOrders(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_orders_auto_pages_two_pages(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        orders = shopify_conn.fetch_orders(SHOP, limit=10)

        self.assertEqual(len(orders), 2)
        # Order from page 1
        self.assertEqual(orders[0]["name"], "#1001")
        self.assertEqual(orders[0]["financial_status"], "PAID")
        self.assertEqual(orders[0]["total"], "99.00 USD")
        self.assertEqual(orders[0]["customer_email"], "alice@example.com")
        self.assertEqual(orders[0]["line_items"][0]["title"], "Widget Pro")
        self.assertEqual(orders[0]["tags"], ["vip"])
        # Order from page 2
        self.assertEqual(orders[1]["name"], "#1002")
        self.assertEqual(orders[1]["financial_status"], "UNPAID")
        # Two POST requests were made
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_credential_rides_every_request(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        shopify_conn.fetch_orders(SHOP, limit=10)

        for call in responses_lib.calls:
            self.assertEqual(call.request.headers["X-Shopify-Access-Token"], "shpat_test_dummy")

    @responses_lib.activate
    def test_brokered_mode_posts_without_client_side_token(self):
        os.environ.pop("RC_CONN_SHOPIFY", None)
        old_brokered = os.environ.get("RC_API_BROKERED_KEYS")
        os.environ["RC_API_BROKERED_KEYS"] = "shopify"
        try:
            responses_lib.add(
                responses_lib.POST,
                "http://rc-broker.internal/shopify/__url/"
                "https%3A%2F%2Fteststore.myshopify.com%2Fadmin%2Fapi%2F2026-07%2Fgraphql.json",
                json=_PAGE_2_BODY,
                status=200,
            )
            orders = shopify_conn.fetch_orders(SHOP, limit=1)
            self.assertEqual(len(orders), 1)
            self.assertNotIn("X-Shopify-Access-Token", responses_lib.calls[0].request.headers)
            self.assertNotIn("Authorization", responses_lib.calls[0].request.headers)
        finally:
            if old_brokered is None:
                os.environ.pop("RC_API_BROKERED_KEYS", None)
            else:
                os.environ["RC_API_BROKERED_KEYS"] = old_brokered

    @responses_lib.activate
    def test_second_page_uses_after_cursor(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        shopify_conn.fetch_orders(SHOP, limit=10)

        # The second request should include the cursor from page 1.
        body2 = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(body2["variables"]["after"], "cursor1")

    @responses_lib.activate
    def test_limit_respected(self):
        _mock_post(_PAGE_1_BODY)
        # Only 1 requested — should stop after page 1 even though hasNextPage=True.
        orders = shopify_conn.fetch_orders(SHOP, limit=1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["name"], "#1001")

    @responses_lib.activate
    def test_orders_to_markdown(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)

        orders = shopify_conn.fetch_orders(SHOP, limit=10)
        md = shopify_conn.orders_to_markdown(orders, SHOP)

        self.assertIn("# Shopify orders:", md)
        self.assertIn("#1001", md)
        self.assertIn("PAID", md)
        self.assertIn("99.00 USD", md)
        self.assertIn("alice@example.com", md)
        self.assertIn("#1002", md)
        self.assertIn("UNPAID", md)

    def test_orders_to_markdown_empty(self):
        md = shopify_conn.orders_to_markdown([], SHOP)
        self.assertIn("No orders found", md)

    @responses_lib.activate
    def test_order_detail_lists_each_refund_transaction(self):
        _mock_order_lookup()
        o = shopify_conn.fetch_order(SHOP, "#1003")
        search, detail = (json.loads(c.request.body) for c in responses_lib.calls)
        self.assertEqual(search["variables"]["query"], 'name:"#1003"')
        self.assertNotIn("refunds(", search["query"])
        self.assertEqual(detail["variables"]["id"], "gid://shopify/Order/3")
        self.assertIn("refunds(first: 5)", detail["query"])

        self.assertEqual(o["total_refunded"], "40.00 EUR")
        self.assertEqual(o["return_status"], "RETURNED")
        retried, _pending = o["refunds"]
        self.assertEqual([t["status"] for t in retried["transactions"]], ["FAILURE", "SUCCESS"])
        self.assertEqual(retried["line_items"], [{"title": "Widget Pro", "quantity": 1, "subtotal": "25.00 EUR"}])

        md = shopify_conn.order_to_markdown(o, "#1003", SHOP)
        self.assertIn("# Shopify order #1003", md)
        self.assertIn("- Refund record: 25.00 EUR created 2026-11-20T08:59:00Z", md)
        self.assertIn("  - 25.00 EUR to customer's payment method via Stripe: FAILURE at 2026-11-20T09:00:00Z", md)
        self.assertIn("  - 25.00 EUR to customer's payment method via Stripe: SUCCESS at 2026-11-20T09:05:00Z", md)
        self.assertIn("  - 15.00 EUR to customer's payment method via Stripe: PENDING at 2026-11-24T12:00:00Z", md)
        self.assertIn("Refunded items: Widget Pro ×1", md)
        self.assertIn("- Return: RETURNED", md)
        self.assertIn("Note: damaged on arrival", md)
        self.assertNotIn("more transactions exist", md)

    def test_store_credit_manual_and_test_are_not_payment_method(self):
        r = shopify_conn._refund({"transactions": {"nodes": [
            _tx("SUCCESS", "5.00", "2026-11-20T09:00:00Z", gateway="shopify_store_credit"),
            _tx("SUCCESS", "6.00", "2026-11-20T09:00:00Z", manual=True),
            _tx("SUCCESS", "7.00", "2026-11-20T09:00:00Z", test=True),
        ]}})
        self.assertEqual([t["destination"] for t in r["transactions"]],
                         ["store credit", "manual gateway (recorded only; Shopify moved no money)",
                          "test transaction (no real money)"])

    def test_only_refund_kind_transactions_count(self):
        r = shopify_conn._refund({"transactions": {"nodes": [
            dict(_tx("SUCCESS", "5.00", "2026-11-20T09:00:00Z"), kind="SUGGESTED_REFUND")]}})
        self.assertEqual(r["transactions"], [])
        md = shopify_conn.orders_to_markdown([{"name": "#1", "refunds": [r]}], SHOP)
        self.assertIn("No refund transaction", md)

    def test_truncated_transactions_are_flagged(self):
        r = shopify_conn._refund({"totalRefundedSet": _eur("10.00"),
                                  "transactions": {"nodes": [_tx("FAILURE", "10.00", "2026-11-20T09:00:00Z")],
                                                   "pageInfo": {"hasNextPage": True}}})
        md = shopify_conn.orders_to_markdown([{"name": "#1", "refunds": [r]}], SHOP)
        self.assertIn("more transactions exist than shown", md)

    def test_buyer_currency_comes_first(self):
        bag = {"shopMoney": {"amount": "54.00", "currencyCode": "CAD"},
               "presentmentMoney": {"amount": "40.00", "currencyCode": "USD"}}
        self.assertEqual(shopify_conn._money(bag), "40.00 USD (shop currency: 54.00 CAD)")

    @responses_lib.activate
    def test_exact_name_behind_a_near_match_is_found(self):
        _mock_post({"data": {"orders": {"nodes": [{"id": "gid://shopify/Order/9", "name": "#10030"},
                                                  {"id": "gid://shopify/Order/3", "name": "#1003"}]}}})
        _mock_post({"data": {"order": _REFUNDED_ORDER}})
        o = shopify_conn.fetch_order(SHOP, "#1003")
        self.assertEqual(o["name"], "#1003")
        self.assertEqual(json.loads(responses_lib.calls[1].request.body)["variables"]["id"], "gid://shopify/Order/3")

    @responses_lib.activate
    def test_only_near_matches_is_not_found(self):
        _mock_post({"data": {"orders": {"nodes": [{"id": "gid://shopify/Order/9", "name": "#10030"}]}}})
        self.assertIsNone(shopify_conn.fetch_order(SHOP, "#1003"))
        self.assertEqual(len(responses_lib.calls), 1)

    @responses_lib.activate
    def test_order_search_value_is_escaped(self):
        _mock_post({"data": {"orders": {"nodes": []}}})
        shopify_conn.fetch_order(SHOP, 'A"1\\')
        sent = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(sent["variables"]["query"], 'name:"A\\"1\\\\"')

    @responses_lib.activate
    def test_order_bare_number_retries_with_hash(self):
        _mock_post({"data": {"orders": {"nodes": []}}})
        _mock_order_lookup()
        o = shopify_conn.fetch_order(SHOP, "1003")
        self.assertEqual(o["name"], "#1003")
        queries = [json.loads(c.request.body)["variables"].get("query") for c in responses_lib.calls]
        self.assertEqual(queries[:2], ['name:"1003"', 'name:"#1003"'])

    @responses_lib.activate
    def test_order_not_found_mentions_60_day_window(self):
        _mock_post({"data": {"orders": {"nodes": []}}})
        self.assertIsNone(shopify_conn.fetch_order(SHOP, "#9999"))
        md = shopify_conn.order_to_markdown(None, "#9999", SHOP)
        self.assertIn("not found", md)
        self.assertIn("60 days", md)

    @responses_lib.activate
    def test_list_query_stays_cheap_and_points_to_order_detail(self):
        listed = {k: v for k, v in _REFUNDED_ORDER.items() if k != "refunds"}
        _mock_post({"data": {"orders": {"edges": [{"cursor": "c", "node": listed}],
                                        "pageInfo": {"hasNextPage": False, "endCursor": "c"}}}})
        orders = shopify_conn.fetch_orders(SHOP, limit=50)
        sent = json.loads(responses_lib.calls[0].request.body)
        self.assertNotIn("refunds(", sent["query"])
        self.assertLessEqual(sent["variables"]["first"], 25)
        md = shopify_conn.orders_to_markdown(orders, SHOP)
        self.assertIn("- Refunded so far: 40.00 EUR", md)
        self.assertIn('run `order --name "#1003"`', md)

    @responses_lib.activate
    def test_order_without_refunds_renders_no_refund_lines(self):
        _mock_post(_PAGE_2_BODY)
        orders = shopify_conn.fetch_orders(SHOP, limit=1)
        md = shopify_conn.orders_to_markdown(orders, SHOP)
        self.assertNotIn("Refund", md)
        self.assertNotIn("Return:", md)

    def test_order_email_shown_when_it_differs_from_customer(self):
        md = shopify_conn.orders_to_markdown([{"name": "#1", "order_email": "guest@example.com",
                                               "customer_email": "alice@example.com"}], SHOP)
        self.assertIn("- Order email: guest@example.com", md)


# ---------------------------------------------------------------------------
# Customer tests
# ---------------------------------------------------------------------------


class ShopifyCustomer(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_fetch_customer_by_email(self):
        _mock_post(_CUSTOMER_EMAIL_BODY)

        c = shopify_conn.fetch_customer(SHOP, "alice@example.com")

        self.assertIsNotNone(c)
        self.assertEqual(c["email"], "alice@example.com")
        self.assertEqual(c["name"], "Alice Smith")
        self.assertEqual(c["orders_count"], 3)
        self.assertEqual(c["total_spent"], "245.00 USD")
        self.assertEqual(c["state"], "ENABLED")
        self.assertIn("loyal", c["tags"])
        # Credential was in the request
        self.assertEqual(
            responses_lib.calls[0].request.headers["X-Shopify-Access-Token"],
            "shpat_test_dummy",
        )

    @responses_lib.activate
    def test_fetch_customer_by_gid(self):
        _mock_post(_CUSTOMER_GID_BODY)

        c = shopify_conn.fetch_customer(SHOP, "gid://shopify/Customer/42")

        self.assertIsNotNone(c)
        self.assertEqual(c["id"], "gid://shopify/Customer/42")
        self.assertEqual(c["email"], "alice@example.com")

    @responses_lib.activate
    def test_fetch_customer_not_found(self):
        _mock_post({"data": {"customers": {"edges": []}}})

        c = shopify_conn.fetch_customer(SHOP, "nobody@example.com")
        self.assertIsNone(c)

    @responses_lib.activate
    def test_customer_to_markdown(self):
        _mock_post(_CUSTOMER_EMAIL_BODY)
        c = shopify_conn.fetch_customer(SHOP, "alice@example.com")
        md = shopify_conn.customer_to_markdown(c, "alice@example.com", SHOP)

        self.assertIn("# Shopify customer: alice@example.com", md)
        self.assertIn("Alice Smith", md)
        self.assertIn("245.00 USD", md)
        self.assertIn("ENABLED", md)

    def test_customer_to_markdown_not_found(self):
        md = shopify_conn.customer_to_markdown(None, "ghost@example.com", SHOP)
        self.assertIn("not found", md.lower())
        self.assertIn("ghost@example.com", md)


# ---------------------------------------------------------------------------
# Product tests
# ---------------------------------------------------------------------------


class ShopifyProduct(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_fetch_product_by_gid(self):
        _mock_post(_PRODUCT_BODY)

        p = shopify_conn.fetch_product(SHOP, "gid://shopify/Product/7")

        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "Widget Pro")
        self.assertEqual(p["handle"], "widget-pro")
        self.assertEqual(p["status"], "ACTIVE")
        self.assertEqual(len(p["variants"]), 1)
        v = p["variants"][0]
        self.assertEqual(v["price"], "49.99")
        self.assertEqual(v["sku"], "WP-001")
        self.assertEqual(v["inventory"], 120)
        self.assertTrue(v["available"])

    @responses_lib.activate
    def test_fetch_product_not_found(self):
        _mock_post({"data": {"product": None}})
        p = shopify_conn.fetch_product(SHOP, "gid://shopify/Product/999")
        self.assertIsNone(p)

    @responses_lib.activate
    def test_product_to_markdown(self):
        _mock_post(_PRODUCT_BODY)
        p = shopify_conn.fetch_product(SHOP, "gid://shopify/Product/7")
        md = shopify_conn.product_to_markdown(p, "gid://shopify/Product/7", SHOP)

        self.assertIn("# Shopify product: Widget Pro", md)
        self.assertIn("widget-pro", md)
        self.assertIn("ACTIVE", md)
        self.assertIn("49.99", md)
        self.assertIn("WP-001", md)
        self.assertIn("120", md)
        self.assertIn("in stock", md)

    def test_product_to_markdown_not_found(self):
        md = shopify_conn.product_to_markdown(None, "gid://shopify/Product/999", SHOP)
        self.assertIn("not found", md.lower())


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class ShopifyCLI(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_SHOPIFY")
        os.environ["RC_CONN_SHOPIFY"] = "shpat_test_dummy"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_SHOPIFY", None)
        else:
            os.environ["RC_CONN_SHOPIFY"] = self._saved

    @responses_lib.activate
    def test_cli_orders(self):
        _mock_post(_PAGE_1_BODY)
        _mock_post(_PAGE_2_BODY)
        rc = shopify_conn.main(["--shop", SHOP, "orders", "--limit", "10"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_cli_shop_after_subcommand(self):
        _mock_order_lookup()
        rc = shopify_conn.main(["order", "--shop", SHOP, "--name", "#1003"])
        self.assertEqual(rc, 0)

    def test_cli_requires_shop(self):
        with self.assertRaises(SystemExit):
            shopify_conn.main(["order", "--name", "#1003"])

    @responses_lib.activate
    def test_published_help_examples_parse(self):
        """Every command line in help_md must run as written."""
        import re
        import shlex
        help_md = shopify_conn_manifest_help()
        cmds = re.findall(r"python -m lib\.connectors\.shopify (.+)", help_md)
        self.assertGreaterEqual(len(cmds), 4)
        empty = {"data": {"orders": {"edges": [], "nodes": [], "pageInfo": {"hasNextPage": False}},
                          "customers": {"edges": []}, "product": None}}
        for _ in cmds:
            responses_lib.add(responses_lib.POST, "https://mystore.myshopify.com/admin/api/2026-07/graphql.json",
                              json=empty, status=200)
        for c in cmds:
            self.assertEqual(shopify_conn.main(shlex.split(c)), 0, c)

    @responses_lib.activate
    def test_cli_order(self):
        _mock_order_lookup()
        rc = shopify_conn.main(["--shop", SHOP, "order", "--name", "#1003"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_cli_customer(self):
        _mock_post(_CUSTOMER_EMAIL_BODY)
        rc = shopify_conn.main(["--shop", SHOP, "customer", "--ref", "alice@example.com"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_product(self):
        _mock_post(_PRODUCT_BODY)
        rc = shopify_conn.main(["--shop", SHOP, "product", "--id", "gid://shopify/Product/7"])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Token-prefix hygiene guard
# ---------------------------------------------------------------------------


class ShopifyTokenHygiene(unittest.TestCase):
    """CI guard: no real Shopify token prefix may land in the connector directory files.

    Scoped to the connector dir only (NOT this test file, which legitimately names the prefixes).
    Splits prefix literals with concatenation so this guard doesn't flag itself.
    """

    # Shopify offline/online token prefix, private-app token prefix.
    _TOKEN_PREFIXES = (
        "shpat" "_",    # offline/online access token (Admin API)
        "shpss" "_",    # session token
        "shpca" "_",    # custom app token
        "shppa" "_",    # private app password
    )

    def test_no_token_prefixes_in_shopify_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "shopify"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
