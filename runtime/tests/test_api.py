"""Unit tests for lib.api — the generic read-only REST client. No network: every HTTP call is
mocked with the `responses` library. These lock down the logic that decides *what* gets sent and
*how* failures/pages/rate-limits are handled.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_api.py -q
"""

import random
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402


def _manifest(**kw) -> api.Manifest:
    base = dict(key="demo", base_url="https://api.demo.test/v1")
    base.update(kw)
    return api.Manifest(**base)


def _client(manifest=None, **kw):
    """A client with a fixed RNG + recording sleeper so jitter/backoff are deterministic in tests."""
    sleeps: list[float] = []
    c = api.Client(
        manifest=manifest or _manifest(),
        credential="sekret",
        _sleeper=sleeps.append,
        _rng=random.Random(1234),
        **kw,
    )
    return c, sleeps


class AuthPlacement(unittest.TestCase):
    """Each auth strategy puts the credential in the right place; api keys never hit the query string."""

    @responses.activate
    def test_bearer_header(self):
        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="bearer")))
        c.get("ping")
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer sekret")

    @responses.activate
    def test_basic_header(self):
        import base64

        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="basic")))
        c.credential = "user:pass"
        c.get("ping")
        expect = "Basic " + base64.b64encode(b"user:pass").decode()
        self.assertEqual(responses.calls[0].request.headers["Authorization"], expect)

    @responses.activate
    def test_basic_username_only(self):
        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="basic")))
        c.credential = "keyonly"  # no colon ⇒ username with empty password
        c.get("ping")
        self.assertTrue(responses.calls[0].request.headers["Authorization"].startswith("Basic "))

    @responses.activate
    def test_api_key_header_named(self):
        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="api_key_header", name="X-Api-Key")))
        c.get("ping")
        self.assertEqual(responses.calls[0].request.headers["X-Api-Key"], "sekret")
        # Must NOT leak into the query string.
        self.assertNotIn("sekret", responses.calls[0].request.url)

    @responses.activate
    def test_query_param(self):
        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="query_param", name="api_key")))
        c.get("ping")
        self.assertIn("api_key=sekret", responses.calls[0].request.url)

    @responses.activate
    def test_oauth2_client_credentials_is_bearer(self):
        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="oauth2_client_credentials")))
        c.get("ping")
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer sekret")

    @responses.activate
    def test_none_strategy_sends_no_auth(self):
        responses.add(responses.GET, "https://api.demo.test/v1/ping", json={"ok": True})
        c = api.Client(manifest=_manifest(auth=api.Auth(strategy="none")), credential="")
        c.get("ping")
        self.assertNotIn("Authorization", responses.calls[0].request.headers)


class BrokeredRouting(unittest.TestCase):
    @responses.activate
    def test_brokered_manifest_routes_to_virtual_host_without_auth(self):
        responses.add(
            responses.GET,
            "http://rc-broker.internal/demo/__url/https%3A%2F%2Fapi.demo.test%2Fv1%2Fping",
            json={"ok": True},
        )
        m = _manifest(auth=api.Auth(strategy="bearer"), brokered=True)
        with mock.patch.object(api.oauth, "token") as token:
            c = api.client(m)
        token.assert_not_called()
        self.assertEqual(c.get("ping"), {"ok": True})
        self.assertEqual(
            responses.calls[0].request.url,
            "http://rc-broker.internal/demo/__url/https%3A%2F%2Fapi.demo.test%2Fv1%2Fping",
        )
        self.assertNotIn("Authorization", responses.calls[0].request.headers)

    @responses.activate
    def test_brokered_roster_env_routes_without_env_token(self):
        responses.add(
            responses.GET,
            "http://rc-broker.internal/demo/__url/https%3A%2F%2Fapi.demo.test%2Fv1%2Fping",
            json={"ok": True},
        )
        with mock.patch.dict(os.environ, {"RC_CONNECTIONS": '[{"key":"demo","brokered":true}]'}, clear=False):
            with mock.patch.object(api.oauth, "token") as token:
                c = api.client(_manifest(auth=api.Auth(strategy="api_key_header", name="X-Api-Key")))
        token.assert_not_called()
        c.get("/ping")
        self.assertNotIn("X-Api-Key", responses.calls[0].request.headers)

    @responses.activate
    def test_brokered_keeps_read_tier_post_policy(self):
        responses.add(
            responses.POST,
            "http://rc-broker.internal/demo/__url/https%3A%2F%2Fapi.demo.test%2Fv1%2Fcrm%2Fsearch",
            json={"ok": True},
        )
        c, _ = _client(_manifest(brokered=True, allowed_post_paths=("/crm/*",)))
        self.assertEqual(c.post("/crm/search", json_body={"filter": "alice"}), {"ok": True})
        with self.assertRaises(api.MethodPolicyError):
            c.post("/billing/write", json_body={"name": "alice"})
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_brokered_pagination_follow_uses_broker_and_no_auth(self):
        m = _manifest(
            brokered=True,
            pagination=api.Pagination(style="link"),
        )
        responses.add(
            responses.GET,
            "http://rc-broker.internal/demo/__url/https%3A%2F%2Fapi.demo.test%2Fv1%2Flist",
            json=[{"id": 1}],
            headers={"Link": '<https://api.demo.test/v1/list?page=2>; rel="next"'},
        )
        responses.add(
            responses.GET,
            "http://rc-broker.internal/demo/__url/https%3A%2F%2Fapi.demo.test%2Fv1%2Flist%3Fpage%3D2",
            json=[{"id": 2}],
        )
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])
        for call in responses.calls:
            self.assertNotIn("Authorization", call.request.headers)

    def test_brokered_absolute_url_is_encoded_for_host_broker(self):
        got = api._broker_url("freshdesk", "https://acme.freshdesk.com/api/v2/tickets?per_page=100")
        self.assertEqual(
            got,
            "http://rc-broker.internal/freshdesk/__url/https%3A%2F%2Facme.freshdesk.com%2Fapi%2Fv2%2Ftickets%3Fper_page%3D100",
        )

    def test_brokered_absolute_url_refuses_plain_http(self):
        with self.assertRaisesRegex(RuntimeError, "https"):
            api._broker_url("stripe", "http://api.stripe.com/v1/customers")

    def test_brokered_relative_path_uses_runtime_base_url(self):
        got = api._broker_url("shopify", "graphql.json", base_url="https://acme.myshopify.com/admin/api/2026-07")
        self.assertEqual(
            got,
            "http://rc-broker.internal/shopify/__url/https%3A%2F%2Facme.myshopify.com%2Fadmin%2Fapi%2F2026-07%2Fgraphql.json",
        )


class ErrorNormalization(unittest.TestCase):
    @responses.activate
    def test_4xx_raises_apierror_with_body(self):
        responses.add(
            responses.GET, "https://api.demo.test/v1/x",
            json={"error": {"message": "no such customer"}}, status=404,
        )
        c, _ = _client()
        with self.assertRaises(api.ApiError) as cm:
            c.get("x")
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("no such customer", cm.exception.body)
        self.assertFalse(cm.exception.retryable)

    @responses.activate
    def test_non_json_body_raises(self):
        responses.add(responses.GET, "https://api.demo.test/v1/x", body="<html>nope</html>", status=200)
        c, _ = _client()
        with self.assertRaises(api.ApiError):
            c.get("x")

    @responses.activate
    def test_404_is_not_retried(self):
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=404)
        c, sleeps = _client()
        with self.assertRaises(api.ApiError):
            c.get("x")
        self.assertEqual(len(responses.calls), 1)  # no retries on a non-retryable status
        self.assertEqual(sleeps, [])


class RetryBackoff(unittest.TestCase):
    @responses.activate
    def test_retries_5xx_then_succeeds(self):
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=503)
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=503)
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"ok": True}, status=200)
        c, sleeps = _client()
        self.assertEqual(c.get("x"), {"ok": True})
        self.assertEqual(len(responses.calls), 3)
        self.assertEqual(len(sleeps), 2)  # two retries ⇒ two backoff sleeps

    @responses.activate
    def test_full_jitter_within_bounds(self):
        # Each sleep must be in [0, min(cap, base*2**attempt)] — full jitter, never exceeding ceiling.
        for _ in range(6):
            responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=500)
        c, sleeps = _client(max_retries=5, backoff_base=0.5, backoff_cap=30.0)
        with self.assertRaises(api.ApiError):
            c.get("x")
        for attempt, slept in enumerate(sleeps):
            ceiling = min(30.0, 0.5 * (2 ** attempt))
            self.assertGreaterEqual(slept, 0.0)
            self.assertLessEqual(slept, ceiling)

    @responses.activate
    def test_exhausts_retries_then_raises(self):
        for _ in range(10):
            responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=502)
        c, sleeps = _client(max_retries=3)
        with self.assertRaises(api.ApiError):
            c.get("x")
        self.assertEqual(len(responses.calls), 4)  # 1 initial + 3 retries
        self.assertEqual(len(sleeps), 3)

    @responses.activate
    def test_post_not_retried_by_default(self):
        responses.add(responses.POST, "https://api.demo.test/v1/x", json={"e": 1}, status=503)
        c, _ = _client()
        with self.assertRaises(api.MethodPolicyError):
            c.request("POST", "x")
        self.assertEqual(len(responses.calls), 0)  # rejected before any network call

    @responses.activate
    def test_allowlisted_read_post_retries(self):
        responses.add(responses.POST, "https://api.demo.test/v1/search", json={"e": 1}, status=503)
        responses.add(responses.POST, "https://api.demo.test/v1/search", json={"ok": True}, status=200)
        c, sleeps = _client(_manifest(allowed_post_paths=("/search",)))
        self.assertEqual(c.request("POST", "search", json_body={"term": "alice"}), {"ok": True})
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(len(sleeps), 1)

    @responses.activate
    def test_allowlisted_post_with_files_not_retried(self):
        responses.add(responses.POST, "https://api.demo.test/v1/upload", json={"e": 1}, status=503)
        c, sleeps = _client(_manifest(allowed_post_paths=("/upload",)))
        with self.assertRaises(api.ApiError):
            c.post("/upload", files={"attachment": BytesIO(b"abc")})
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(sleeps, [])

    @responses.activate
    def test_action_post_not_retried_without_idempotency_key(self):
        responses.add(responses.POST, "https://api.demo.test/v1/x", json={"e": 1}, status=503)
        c, sleeps = _client(allow_writes=True)
        with self.assertRaises(api.ApiError):
            c.post("x", json={"name": "alice"})
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(sleeps, [])

    @responses.activate
    def test_action_post_retries_with_idempotency_key(self):
        responses.add(responses.POST, "https://api.demo.test/v1/x", json={"e": 1}, status=503)
        responses.add(responses.POST, "https://api.demo.test/v1/x", json={"ok": True}, status=200)
        c, sleeps = _client(allow_writes=True)
        got = c.post("x", json={"name": "alice"}, idempotency_key="idem-123")
        self.assertEqual(got, {"ok": True})
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(responses.calls[0].request.headers["Idempotency-Key"], "idem-123")
        self.assertEqual(len(sleeps), 1)

    @responses.activate
    def test_idempotency_key_retries_file_style_request(self):
        responses.add(responses.POST, "https://api.demo.test/v1/upload", json={"e": 1}, status=503)
        responses.add(responses.POST, "https://api.demo.test/v1/upload", json={"ok": True}, status=200)
        c, sleeps = _client(allow_writes=True)
        got = c.post("/upload", files={"attachment": BytesIO(b"abc")}, idempotency_key="upload-123")
        self.assertEqual(got, {"ok": True})
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(responses.calls[0].request.headers["Idempotency-Key"], "upload-123")
        self.assertEqual(len(sleeps), 1)


class RetryAfterParsing(unittest.TestCase):
    def test_seconds_form(self):
        self.assertEqual(api.parse_retry_after("120"), 120.0)
        self.assertEqual(api.parse_retry_after("0"), 0.0)

    def test_http_date_form(self):
        # A date 60s in the future ⇒ ~60s delay (RFC 9110 §10.2.3 date form — the commonly-missed one).
        now = 1_700_000_000.0
        future = "Tue, 14 Nov 2023 22:13:20 GMT"  # = 1700000000 + 0; build a known offset instead
        # Use a date we compute so the test is timezone-stable:
        from email.utils import formatdate

        hdr = formatdate(now + 60, usegmt=True)
        self.assertAlmostEqual(api.parse_retry_after(hdr, now=now), 60.0, delta=1.0)
        # And a clearly-parseable fixed string still parses to a float (>=0).
        self.assertIsNotNone(api.parse_retry_after(future, now=now))

    def test_past_date_clamps_to_zero(self):
        from email.utils import formatdate

        hdr = formatdate(1_700_000_000.0 - 99, usegmt=True)
        self.assertEqual(api.parse_retry_after(hdr, now=1_700_000_000.0), 0.0)

    def test_absent_or_garbage_is_none(self):
        self.assertIsNone(api.parse_retry_after(None))
        self.assertIsNone(api.parse_retry_after(""))
        self.assertIsNone(api.parse_retry_after("not-a-date"))

    @responses.activate
    def test_429_honours_retry_after_seconds(self):
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=429,
                      headers={"Retry-After": "7"})
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"ok": True}, status=200)
        c, sleeps = _client()
        self.assertEqual(c.get("x"), {"ok": True})
        self.assertEqual(sleeps, [7.0])  # exact Retry-After, not jittered backoff

    @responses.activate
    def test_429_without_retry_after_uses_backoff(self):
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"e": 1}, status=429)
        responses.add(responses.GET, "https://api.demo.test/v1/x", json={"ok": True}, status=200)
        c, sleeps = _client(backoff_base=0.5, backoff_cap=30.0)
        c.get("x")
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 0.5)  # falls back to jittered backoff (attempt 0 ceiling)

    def test_long_retry_after_is_sliced(self):
        # A hostile multi-minute Retry-After is split into MAX_RETRY_AFTER chunks, not one giant sleep.
        chunks: list[float] = []
        api._sleep(300.0, chunks.append)
        self.assertTrue(all(ch <= api.MAX_RETRY_AFTER for ch in chunks))
        self.assertAlmostEqual(sum(chunks), 300.0, places=3)


class CursorPagination(unittest.TestCase):
    @responses.activate
    def test_stitches_pages(self):
        m = _manifest(pagination=api.Pagination(
            style="cursor", cursor_field="next", cursor_param="cursor",
            has_more_field="has_more", items_field="data",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"data": [{"id": 1}, {"id": 2}], "has_more": True, "next": "c1"}, status=200)
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"data": [{"id": 3}], "has_more": False, "next": None}, status=200)
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2, 3])
        self.assertFalse(result["incomplete"])
        # Page 2 carried the cursor from page 1.
        self.assertIn("cursor=c1", responses.calls[1].request.url)

    @responses.activate
    def test_has_more_false_stops_even_with_cursor(self):
        m = _manifest(pagination=api.Pagination(
            style="cursor", cursor_field="next", has_more_field="has_more", items_field="data",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"data": [{"id": 1}], "has_more": False, "next": "still-here"}, status=200)
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 1)


class OffsetPagination(unittest.TestCase):
    @responses.activate
    def test_advances_offset_until_short_page(self):
        m = _manifest(pagination=api.Pagination(
            style="offset", offset_param="offset", limit_param="limit", page_size=2, items_field="rows",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"rows": [{"i": 0}, {"i": 1}]})
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"rows": [{"i": 2}, {"i": 3}]})
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"rows": [{"i": 4}]})
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["i"] for it in result["items"]], [0, 1, 2, 3, 4])
        self.assertIn("offset=2", responses.calls[1].request.url)
        self.assertIn("offset=4", responses.calls[2].request.url)


class LinkPagination(unittest.TestCase):
    @responses.activate
    def test_follows_rfc8288_next(self):
        m = _manifest(pagination=api.Pagination(style="link"))
        responses.add(
            responses.GET, "https://api.demo.test/v1/list",
            json=[{"id": 1}],
            headers={"Link": '<https://api.demo.test/v1/list?page=2>; rel="next"'},
        )
        responses.add(responses.GET, "https://api.demo.test/v1/list?page=2", json=[{"id": 2}])
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])
        self.assertFalse(result["incomplete"])

    def test_parse_link_next(self):
        hdr = '<https://x/y?a=1>; rel="prev", <https://x/y?a=3>; rel="next"'
        self.assertEqual(api._parse_link_next(hdr), "https://x/y?a=3")
        self.assertIsNone(api._parse_link_next(None))
        self.assertIsNone(api._parse_link_next('<https://x>; rel="prev"'))


class BodyUrlPagination(unittest.TestCase):
    """body_url: the next-page URL lives inside the JSON body (not a Link header, not a cursor token)."""

    @responses.activate
    def test_absolute_next_url_stitches_pages(self):
        m = _manifest(pagination=api.Pagination(
            style="body_url", next_url_field="meta.pagination.next", items_field="data",
        ))
        responses.add(
            responses.GET, "https://api.demo.test/v1/list",
            json={"data": [{"id": 1}], "meta": {"pagination": {"next": "https://api.demo.test/v1/list?after=p2"}}},
        )
        responses.add(
            responses.GET, "https://api.demo.test/v1/list?after=p2",
            json={"data": [{"id": 2}], "meta": {"pagination": {"next": None}}},
        )
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])
        self.assertFalse(result["incomplete"])
        # Page 2 followed the absolute URL verbatim.
        self.assertIn("after=p2", responses.calls[1].request.url)

    @responses.activate
    def test_cross_site_next_url_refused(self):
        # SECURITY: a hostile upstream returning a foreign next-host must NOT get the credential. A
        # same-site subdomain (us.demo.test) is allowed; a foreign domain (attacker.test) is refused.
        m = _manifest(pagination=api.Pagination(
            style="body_url", next_url_field="next", items_field="data",
        ))
        responses.add(
            responses.GET, "https://api.demo.test/v1/list",
            json={"data": [{"id": 1}], "next": "https://attacker.test/steal"},
        )
        c, _ = _client(m)
        with self.assertRaises(RuntimeError) as ctx:
            c.collect("list")
        # collect() surfaces it as incomplete-with-reason? No — a security refusal is a hard RuntimeError
        # (not the swallowed ApiError partial path). It propagates out of paginate.
        self.assertIn("foreign host", str(ctx.exception))
        # The attacker host was never contacted.
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_same_site_subdomain_next_allowed(self):
        m = _manifest(pagination=api.Pagination(
            style="body_url", next_url_field="next", items_field="data",
        ))
        responses.add(
            responses.GET, "https://api.demo.test/v1/list",
            json={"data": [{"id": 1}], "next": "https://eu.demo.test/v1/list?after=p2"},
        )
        responses.add(
            responses.GET, "https://eu.demo.test/v1/list?after=p2",
            json={"data": [{"id": 2}], "next": None},
        )
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])

    @responses.activate
    def test_relative_next_path_joins_base_url(self):
        # Recurly-shape: next is a RELATIVE path that must be joined onto base_url before the follow.
        m = _manifest(pagination=api.Pagination(
            style="body_url", next_url_field="next", items_field="data",
        ))
        responses.add(
            responses.GET, "https://api.demo.test/v1/accounts",
            json={"data": [{"id": 1}], "next": "accounts?cursor=abc&limit=200"},
        )
        responses.add(
            responses.GET, "https://api.demo.test/v1/accounts?cursor=abc&limit=200",
            json={"data": [{"id": 2}], "next": None},
        )
        c, _ = _client(m)
        result = c.collect("accounts")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])
        # The relative path joined onto base_url (scheme + host + base path preserved).
        self.assertTrue(responses.calls[1].request.url.startswith("https://api.demo.test/v1/accounts?cursor=abc"))

    @responses.activate
    def test_odata_nextlink_literal_key(self):
        # @odata.nextLink has dots that are NOT path segments — must resolve as a whole literal key.
        m = _manifest(pagination=api.Pagination(
            style="body_url", next_url_field="@odata.nextLink", items_field="value",
        ))
        responses.add(
            responses.GET, "https://api.demo.test/v1/items",
            json={"value": [{"id": 1}], "@odata.nextLink": "https://api.demo.test/v1/items?$skiptoken=x"},
        )
        responses.add(
            responses.GET, "https://api.demo.test/v1/items?$skiptoken=x",
            json={"value": [{"id": 2}]},  # no nextLink ⇒ exhausted
        )
        c, _ = _client(m)
        result = c.collect("items")
        self.assertEqual([it["id"] for it in result["items"]], [1, 2])
        self.assertFalse(result["incomplete"])

    @responses.activate
    def test_partial_result_through_body_url(self):
        # The collect() partial/incomplete contract still holds when a body_url page mid-stream errors.
        m = _manifest(pagination=api.Pagination(
            style="body_url", next_url_field="links.next", items_field="data",
        ))
        responses.add(
            responses.GET, "https://api.demo.test/v1/list",
            json={"data": [{"id": 1}], "links": {"next": "https://api.demo.test/v1/list?page=2"}},
        )
        responses.add(responses.GET, "https://api.demo.test/v1/list?page=2", json={"e": 1}, status=500)
        c, _ = _client(m, max_retries=0)
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1])
        self.assertTrue(result["incomplete"])
        self.assertIn("failed", result["reason"])


class PagePagination(unittest.TestCase):
    """page: numeric page-number paging — increments page_param by 1, terminates on a short page."""

    @responses.activate
    def test_one_based_stitches_and_terminates(self):
        m = _manifest(pagination=api.Pagination(
            style="page", page_param="page", page_start=1, limit_param="per_page",
            page_size=2, items_field="result",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"result": [{"i": 0}, {"i": 1}]})
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"result": [{"i": 2}, {"i": 3}]})
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"result": [{"i": 4}]})
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["i"] for it in result["items"]], [0, 1, 2, 3, 4])
        self.assertFalse(result["incomplete"])
        # page increments by 1 (NOT by item count) from page_start=1.
        self.assertIn("page=1", responses.calls[0].request.url)
        self.assertIn("page=2", responses.calls[1].request.url)
        self.assertIn("page=3", responses.calls[2].request.url)

    @responses.activate
    def test_zero_based_start(self):
        # ClickUp-shape: 0-based page numbers.
        m = _manifest(pagination=api.Pagination(
            style="page", page_param="page", page_start=0, page_size=2, items_field="tasks",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"tasks": [{"i": 0}, {"i": 1}]})
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"tasks": [{"i": 2}]})
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["i"] for it in result["items"]], [0, 1, 2])
        self.assertIn("page=0", responses.calls[0].request.url)
        self.assertIn("page=1", responses.calls[1].request.url)

    @responses.activate
    def test_empty_first_page_stops(self):
        m = _manifest(pagination=api.Pagination(
            style="page", page_param="page", page_start=1, page_size=2, items_field="result",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"result": []})
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual(result["items"], [])
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_has_more_false_stops_even_on_full_page(self):
        # An API that clamps/repeats the last FULL page past the end must terminate on has_more=false
        # rather than looping to max_pages (the short-page check alone would never fire).
        m = _manifest(pagination=api.Pagination(
            style="page", page_param="page", page_start=1, page_size=2,
            items_field="result", has_more_field="has_more",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"result": [{"i": 0}, {"i": 1}], "has_more": True})
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"result": [{"i": 2}, {"i": 3}], "has_more": False})
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual([it["i"] for it in result["items"]], [0, 1, 2, 3])
        self.assertEqual(len(responses.calls), 2)  # stopped on has_more=false despite a full page

    @responses.activate
    def test_partial_result_through_page(self):
        m = _manifest(pagination=api.Pagination(
            style="page", page_param="page", page_start=1, page_size=2, items_field="result",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"result": [{"i": 0}, {"i": 1}]})
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"e": 1}, status=500)
        c, _ = _client(m, max_retries=0)
        result = c.collect("list")
        self.assertEqual([it["i"] for it in result["items"]], [0, 1])
        self.assertTrue(result["incomplete"])
        self.assertIn("failed", result["reason"])


class NonePagination(unittest.TestCase):
    @responses.activate
    def test_single_page(self):
        m = _manifest(pagination=api.Pagination(style="none", items_field="data"))
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"data": [{"id": 1}]})
        c, _ = _client(m)
        result = c.collect("list")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 1)


class ReadMethodPolicy(unittest.TestCase):
    @responses.activate
    def test_allowlisted_post_with_json_body(self):
        def cb(request):
            self.assertEqual(request.body, b'{"filter": "alice"}')
            return (200, {"Content-Type": "application/json"}, '{"results":[{"id":"1"}]}')

        responses.add_callback(responses.POST, "https://api.demo.test/v1/crm/search", callback=cb)
        c, _ = _client(_manifest(allowed_post_paths=("/crm/*",)))
        got = c.post("/crm/search", json_body={"filter": "alice"})
        self.assertEqual(got, {"results": [{"id": "1"}]})

    @responses.activate
    def test_unallowlisted_post_refused_with_manifest_guidance(self):
        c, _ = _client(_manifest())
        with self.assertRaises(api.MethodPolicyError) as cm:
            c.post("/crm/search", json_body={"filter": "alice"})
        self.assertIn("read-only POST allowlist", str(cm.exception))
        self.assertIn("action plane", str(cm.exception))
        self.assertEqual(len(responses.calls), 0)

    def test_allowlist_wildcard_is_one_path_segment(self):
        self.assertTrue(api._matches_any_path("/crm/v3/objects/contacts/search", ("/crm/v3/objects/*/search",)))
        self.assertFalse(api._matches_any_path("/crm/v3/objects/contacts/extra/search", ("/crm/v3/objects/*/search",)))

    @responses.activate
    def test_write_verbs_refused_with_action_guidance(self):
        c, _ = _client(_manifest(allowed_post_paths=("/safe-search",)))
        for verb in ("PUT", "PATCH", "DELETE"):
            with self.assertRaises(api.MethodPolicyError) as cm:
                c.request(verb, "/customers/1", json_body={"name": "x"})
            self.assertIn("human-confirmed action", str(cm.exception))
            self.assertIn("lib.action.client", str(cm.exception))
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_action_client_allows_write_verbs(self):
        for method in (responses.POST, responses.PATCH, responses.PUT, responses.DELETE):
            responses.add(method, "https://api.demo.test/v1/customers/1", json={"ok": True})
        c, _ = _client(allow_writes=True)
        self.assertEqual(c.post("customers/1", json={"name": "a"}), {"ok": True})
        self.assertEqual(c.patch("customers/1", json={"name": "b"}), {"ok": True})
        self.assertEqual(c.put("customers/1", json={"name": "c"}), {"ok": True})
        self.assertEqual(c.delete("customers/1"), {"ok": True})

    @responses.activate
    def test_action_client_refuses_query_param_auth_for_writes(self):
        responses.add(responses.POST, "https://api.demo.test/v1/customers", json={"ok": True})
        c, _ = _client(_manifest(auth=api.Auth(strategy="query_param", name="token")), allow_writes=True)
        with self.assertRaises(RuntimeError) as cm:
            c.post("customers", json={"name": "a"})
        self.assertIn("query-param auth", str(cm.exception))

    @responses.activate
    def test_upload_builds_multipart_related_body(self):
        def cb(request):
            self.assertIn("uploadType=multipart", request.url)
            self.assertIn("multipart/related", request.headers["Content-Type"])
            self.assertIn(b'"name":"invoice.pdf"', request.body)
            self.assertIn(b"%PDF", request.body)
            return (200, {"Content-Type": "application/json"}, '{"id":"file_1"}')

        responses.add_callback(responses.POST, "https://api.demo.test/upload", callback=cb)
        c, _ = _client(_manifest(base_url="https://api.demo.test/v1"), allow_writes=True)
        self.assertEqual(
            c.upload(
                "https://api.demo.test/upload",
                data=b"%PDF",
                content_type="application/pdf",
                metadata={"name": "invoice.pdf"},
            ),
            {"id": "file_1"},
        )

    def test_manifest_parses_read_post_allowlist(self):
        mani = api._manifest_from_dict({
            "key": "demo",
            "base_url": "https://api.demo.test/v1",
            "read_endpoints": {"post": ["/search", "crm/*/search"]},
        })
        self.assertEqual(mani.allowed_post_paths, ("/search", "crm/*/search"))

    def test_manifest_ignores_catalog_broker_exposure_without_runtime_roster(self):
        mani = api._manifest_from_dict({
            "key": "demo",
            "base_url": "https://api.demo.test/v1",
            "credential_exposure": "broker",
        })
        self.assertFalse(mani.brokered)
        explicit = api._manifest_from_dict({
            "key": "demo",
            "base_url": "https://api.demo.test/v1",
            "brokered": True,
        })
        self.assertTrue(explicit.brokered)

    def test_load_manifests_parses_each_manifest_once(self):
        """Brain scripts call it per request (DentAI per availability window): the catalog is parsed once."""
        api.load_manifests(force=True)
        with mock.patch.object(api, "_parse_manifest_file", side_effect=AssertionError("re-parsed")):
            self.assertIn("dentai_agenda_preview", api.load_manifests())

    def test_dentai_agenda_preview_manifest_is_brokered_at_runtime(self):
        mani = api.load_manifests(force=True)["dentai_agenda_preview"]
        self.assertEqual(mani.base_url, "https://app.dentai.be")
        self.assertEqual(mani.auth.strategy, "bearer")
        self.assertEqual(mani.allowed_post_paths, ("/internal/agenda/operations",))

        with mock.patch.dict(
            os.environ, {"RC_API_BROKERED_KEYS": "dentai_agenda_preview"}, clear=False
        ), mock.patch.object(api.oauth, "token") as token:
            client = api.client(mani)
        token.assert_not_called()
        self.assertTrue(client.manifest.brokered)
        self.assertEqual(client.credential, "")

    def test_dentai_booking_preview_manifest_is_brokered_at_runtime(self):
        mani = api.load_manifests(force=True)["dentai_booking_preview"]
        self.assertEqual(mani.base_url, "https://app.dentai.be")
        self.assertEqual(mani.auth.strategy, "bearer")
        self.assertEqual(
            mani.allowed_post_paths,
            ("/internal/booking/slots", "/internal/booking/book", "/internal/booking/refresh-patient"),
        )

        with mock.patch.dict(
            os.environ, {"RC_API_BROKERED_KEYS": "dentai_booking_preview"}, clear=False
        ), mock.patch.object(api.oauth, "token") as token:
            client = api.client(mani)
        token.assert_not_called()
        self.assertTrue(client.manifest.brokered)
        self.assertEqual(client.credential, "")

    def test_post_next_url_pagination_refused(self):
        m = _manifest(
            allowed_post_paths=("/search",),
            pagination=api.Pagination(style="body_url", next_url_field="next", items_field="data"),
        )
        c, _ = _client(m)
        with self.assertRaises(api.MethodPolicyError) as cm:
            c.collect("/search", method="POST", json_body={"term": "alice"})
        self.assertIn("POST pagination", str(cm.exception))

    def test_cli_write_verb_policy_precedes_credential_resolution(self):
        old = dict(api.MANIFESTS)
        try:
            api.MANIFESTS.clear()
            api.MANIFESTS["demo"] = _manifest(key="demo", auth=api.Auth(strategy="bearer"))
            with mock.patch.object(api, "load_manifests"), mock.patch.object(api.oauth, "token") as token:
                with self.assertRaises(SystemExit) as cm:
                    api._main(["put", "demo", "/customers/1"])
            self.assertEqual(cm.exception.code, 2)
            token.assert_not_called()
        finally:
            api.MANIFESTS.clear()
            api.MANIFESTS.update(old)


class PartialFailure(unittest.TestCase):
    @responses.activate
    def test_mid_stream_error_returns_partial_with_flag(self):
        m = _manifest(pagination=api.Pagination(
            style="cursor", cursor_field="next", has_more_field="has_more", items_field="data",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"data": [{"id": 1}], "has_more": True, "next": "c1"}, status=200)
        responses.add(responses.GET, "https://api.demo.test/v1/list", json={"e": 1}, status=500)
        c, _ = _client(m, max_retries=0)  # don't retry, fail straight to partial
        result = c.collect("list")
        self.assertEqual([it["id"] for it in result["items"]], [1])  # what we had
        self.assertTrue(result["incomplete"])  # never masquerades as complete
        self.assertIn("failed", result["reason"])

    @responses.activate
    def test_max_items_marks_incomplete(self):
        m = _manifest(pagination=api.Pagination(
            style="cursor", cursor_field="next", has_more_field="has_more", items_field="data",
        ))
        responses.add(responses.GET, "https://api.demo.test/v1/list",
                      json={"data": [{"id": 1}, {"id": 2}, {"id": 3}], "has_more": True, "next": "c1"})
        c, _ = _client(m)
        result = c.collect("list", max_items=2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["incomplete"])

    @responses.activate
    def test_max_pages_marks_incomplete(self):
        m = _manifest(pagination=api.Pagination(
            style="cursor", cursor_field="next", has_more_field="has_more", items_field="data",
        ))
        for _ in range(3):
            responses.add(responses.GET, "https://api.demo.test/v1/list",
                          json={"data": [{"id": 1}], "has_more": True, "next": "c"})
        c, _ = _client(m)
        result = c.collect("list", max_pages=2)
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(result["incomplete"])
        self.assertIn("max_pages", result["reason"])

    @responses.activate
    def test_default_max_pages_marks_incomplete(self):
        m = _manifest(pagination=api.Pagination(
            style="cursor", cursor_field="next", has_more_field="has_more", items_field="data",
        ))
        for _ in range(3):
            responses.add(responses.GET, "https://api.demo.test/v1/list",
                          json={"data": [{"id": 1}], "has_more": True, "next": "c"})
        c, _ = _client(m, max_pages=2)
        result = c.collect("list")
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(result["incomplete"])
        self.assertIn("max_pages=2", result["reason"])


class Pick(unittest.TestCase):
    def test_simple_paths(self):
        obj = {"id": "x", "customer": {"email": "a@b.com"}, "n": 3}
        self.assertEqual(api.pick(obj, "id,customer.email"), {"id": "x", "customer.email": "a@b.com"})

    def test_missing_path_omitted(self):
        self.assertEqual(api.pick({"id": "x"}, "id,nope.deep"), {"id": "x"})

    def test_list_index_and_wildcard(self):
        obj = {"data": [{"v": 1}, {"v": 2}]}
        self.assertEqual(api.pick(obj, "data.0.v"), {"data.0.v": 1})
        self.assertEqual(api.pick(obj, "data.*.v"), {"data.*.v": [1, 2]})

    def test_list_of_paths(self):
        self.assertEqual(api.pick({"a": 1, "b": 2}, ["a", "b"]), {"a": 1, "b": 2})


class Join(unittest.TestCase):
    def test_absolute_url_overrides_base(self):
        self.assertEqual(api._join("https://b/v1", "https://other/x"), "https://other/x")

    def test_base_prefix_preserved(self):
        # base_url path must survive the join (urljoin footgun: it drops the base path otherwise).
        self.assertEqual(api._join("https://b/api/0", "issues/1/"), "https://b/api/0/issues/1/")

    def test_no_base(self):
        self.assertEqual(api._join("", "https://x/y"), "https://x/y")


class Timeouts(unittest.TestCase):
    @responses.activate
    def test_both_timeouts_set(self):
        def cb(request):
            return (200, {}, '{"ok": true}')

        responses.add_callback(responses.GET, "https://api.demo.test/v1/x", callback=cb)
        c, _ = _client(connect_timeout=3.0, read_timeout=9.0)
        with mock.patch("requests.request", wraps=__import__("requests").request) as spy:
            c.get("x")
        self.assertEqual(spy.call_args.kwargs["timeout"], (3.0, 9.0))


class ClientCredentialResolution(unittest.TestCase):
    def test_client_resolves_token_from_oauth(self):
        with mock.patch.object(api.oauth, "token", return_value="resolved") as t:
            c = api.client(_manifest(key="demo", auth=api.Auth(strategy="bearer")))
        t.assert_called_once_with("demo")
        self.assertEqual(c.credential, "resolved")

    def test_none_strategy_skips_credential(self):
        with mock.patch.object(api.oauth, "token") as t:
            c = api.client(_manifest(auth=api.Auth(strategy="none")))
        t.assert_not_called()
        self.assertEqual(c.credential, "")


class HostEmbeddedCredential(unittest.TestCase):
    """Per-app connectors carry ``<secret>@https://<host>[/<path>]`` in the env credential; locally
    (no broker) ``api.client`` fills the templated base_url and keeps only the secret for auth."""

    def test_bearer_fills_placeholder_and_preserves_path(self):
        m = _manifest(key="bubble", base_url="https://{app_domain}/api/1.1", auth=api.Auth(strategy="bearer"))
        cred = "70f7tok@https://acme-support.bubbleapps.io/version-test"
        with mock.patch.object(api.oauth, "token", return_value=cred):
            c = api.client(m)
        # Path (`/version-test`) preserved between host and the base_url's own `/api/1.1` suffix.
        self.assertEqual(c.manifest.base_url, "https://acme-support.bubbleapps.io/version-test/api/1.1")
        self.assertEqual(c.credential, "70f7tok")  # only the secret half reaches auth

    def test_basic_secret_with_userpass_and_subdir(self):
        m = _manifest(key="woocommerce", base_url="https://{store_url}/wp-json/wc/v3", auth=api.Auth(strategy="basic"))
        cred = "ckey:csecret@https://shop.example.com/shop"
        with mock.patch.object(api.oauth, "token", return_value=cred):
            c = api.client(m)
        self.assertEqual(c.manifest.base_url, "https://shop.example.com/shop/wp-json/wc/v3")
        self.assertEqual(c.credential, "ckey:csecret")

    def test_no_placeholder_base_url_untouched(self):
        m = _manifest(key="demo", base_url="https://api.demo.test/v1", auth=api.Auth(strategy="bearer"))
        with mock.patch.object(api.oauth, "token", return_value="tok@https://evil.test"):
            c = api.client(m)
        # A non-templated base_url must NOT be rewritten even if the credential looks host-embedded.
        self.assertEqual(c.manifest.base_url, "https://api.demo.test/v1")
        self.assertEqual(c.credential, "tok@https://evil.test")

    def test_split_on_last_marker_so_secret_at_may_contain_at(self):
        secret, base = api._split_host_embedded_credential("a@https://b@https://host.test/p")
        self.assertEqual(secret, "a@https://b")
        self.assertEqual(base, "https://host.test/p")

    def test_bare_credential_leaves_templated_base(self):
        m = _manifest(key="bubble", base_url="https://{app_domain}/api/1.1", auth=api.Auth(strategy="bearer"))
        with mock.patch.object(api.oauth, "token", return_value="just_a_token"):
            c = api.client(m)
        # No host embedded: nothing to fill, credential passes through unchanged.
        self.assertEqual(c.manifest.base_url, "https://{app_domain}/api/1.1")
        self.assertEqual(c.credential, "just_a_token")

    def test_degenerate_base_without_host_is_rejected(self):
        # "tok@https://" must NOT split into base "https:" (whose "host" would be the scheme word,
        # sending the secret to a wrong host). Mirrors the Go host-side hostname requirement.
        self.assertEqual(api._split_host_embedded_credential("tok@https://"), ("", ""))
        self.assertEqual(api._split_host_embedded_credential("@https://host.test"), ("", ""))

    def test_fill_treats_host_path_as_literal_text(self):
        # A backslash in the credential URL must not be interpreted as a regex escape in re.sub.
        out = api._fill_base_placeholder("https://{d}/api", "https://h\\x")
        self.assertEqual(out, "https://h\\x/api")


if __name__ == "__main__":
    unittest.main()
