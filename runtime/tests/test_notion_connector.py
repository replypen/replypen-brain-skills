"""Fixture tests for the Notion connector (script connector — body-backed Notion read APIs).

No live creds, no network: HTTP is mocked with ``responses``. Bodies are Notion's documented
example payloads (developers.notion.com/reference/pagination + /reference/page-object), trimmed to
support-relevant fields.

Tests cover:
  - YAML manifest loads via lib.api's loader and maps every field correctly.
  - Cursor pagination stitches ≥2 pages on POST endpoints (search + query-db).
  - Bearer credential rides every POST request (auth strategy consistent with manifest).
  - Notion-Version header is present on every request.
  - ``api.pick`` selects support fields from GET-based pages.
  - CLI drives the script connector (search, markdown, and compact row-read subcommands).
  - Token-hygiene guard: no real Notion token prefix leaks into the connector dir.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project pytest tests/test_notion_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import notion as notion_conn  # noqa: E402

API_BASE = "https://api.notion.com/v1"
SEARCH_URL = f"{API_BASE}/search"
DB_ID = "a7c5e2d9-1234-4abc-8def-000000000001"
DB_QUERY_URL = f"{API_BASE}/data_sources/{DB_ID}/query"
PAGE_1_URL = f"{API_BASE}/pages/a7c5e2d9-1234-4abc-8def-000000000002"
PAGE_1_MARKDOWN_URL = f"{PAGE_1_URL}/markdown"

# ---------------------------------------------------------------------------
# Documented example page objects (trimmed to support-relevant fields)
# From: https://developers.notion.com/reference/page-object
# ---------------------------------------------------------------------------

_PAGE_1 = {
    "object": "page",
    "id": "a7c5e2d9-1234-4abc-8def-000000000002",
    "url": "https://www.notion.so/Meeting-Notes-a7c5e2d912344abc8def000000000002",
    "created_time": "2024-01-01T00:00:00.000Z",
    "last_edited_time": "2024-06-15T09:30:00.000Z",
    "parent": {"type": "workspace", "workspace": True},
    "properties": {
        "Name": {
            "id": "title",
            "type": "title",
            "title": [{"type": "text", "text": {"content": "Meeting Notes"}, "plain_text": "Meeting Notes"}],
        },
        "Status": {
            "id": "status",
            "type": "select",
            "select": {"name": "In Progress", "color": "blue"},
        },
        "Due": {
            "id": "due",
            "type": "date",
            "date": {"start": "2024-07-01", "end": None},
        },
    },
}

_PAGE_2 = {
    "object": "page",
    "id": "b8d6f3e0-5678-4bcd-9ef0-000000000003",
    "url": "https://www.notion.so/Onboarding-Checklist-b8d6f3e056784bcd9ef0000000000003",
    "created_time": "2024-02-10T00:00:00.000Z",
    "last_edited_time": "2024-06-20T14:00:00.000Z",
    "parent": {"type": "workspace", "workspace": True},
    "properties": {
        "Name": {
            "id": "title",
            "type": "title",
            "title": [{"type": "text", "text": {"content": "Onboarding Checklist"}, "plain_text": "Onboarding Checklist"}],
        },
        "Status": {
            "id": "status",
            "type": "select",
            "select": {"name": "Done", "color": "green"},
        },
        "Notes": {
            "id": "notes",
            "type": "rich_text",
            "rich_text": [{"plain_text": "All steps completed."}],
        },
    },
}

_PAGE_MARKDOWN = {
    "object": "page_markdown",
    "id": _PAGE_1["id"],
    "markdown": "Body line\n- [ ] old task",
    "truncated": False,
    "unknown_block_ids": [],
}

_DATA_SOURCE_SEARCH_RESULT = {
    "object": "data_source",
    "id": DB_ID,
    "url": "https://www.notion.so/Data-Source-a7c5e2d912344abc8def000000000001",
    "created_time": "2024-01-01T00:00:00.000Z",
    "last_edited_time": "2024-06-15T09:30:00.000Z",
    "parent": {"type": "database_id", "database_id": "db-container"},
    "title": [{"type": "text", "text": {"content": "Support Queue"}, "plain_text": "Support Queue"}],
    "properties": {},
}

# Two-page search response: page 1 has has_more=True + next_cursor; page 2 is the last page.
_SEARCH_PAGE_1 = {
    "object": "list",
    "results": [_PAGE_1],
    "has_more": True,
    "next_cursor": "cursor-token-abc123",
    "type": "page_or_database",
    "page_or_database": {},
}

_SEARCH_PAGE_2 = {
    "object": "list",
    "results": [_PAGE_2],
    "has_more": False,
    "next_cursor": None,
    "type": "page_or_database",
    "page_or_database": {},
}

# Two-page database query response.
_DB_QUERY_PAGE_1 = {
    "object": "list",
    "results": [_PAGE_1],
    "has_more": True,
    "next_cursor": "db-cursor-xyz789",
    "type": "page",
    "page": {},
}

_DB_QUERY_PAGE_2 = {
    "object": "list",
    "results": [_PAGE_2],
    "has_more": False,
    "next_cursor": None,
    "type": "page",
    "page": {},
}


class NotionManifest(unittest.TestCase):
    """The YAML manifest loads correctly and maps every lib.api field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "test_token_placeholder"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    def test_manifest_loaded_from_yaml(self):
        m = api.load_manifests()
        self.assertIn("notion", m)
        n = m["notion"]
        self.assertEqual(n.base_url, "https://api.notion.com/v1")
        self.assertEqual(n.auth.strategy, "bearer")
        # Notion-Version required header must be declared
        self.assertIn("Notion-Version", n.default_headers)
        self.assertEqual(n.default_headers["Notion-Version"], "2026-03-11")

    def test_manifest_pagination_fields(self):
        m = api.load_manifests()
        n = m["notion"]
        pg = n.pagination
        self.assertEqual(pg.style, "cursor")
        self.assertEqual(pg.cursor_param, "start_cursor")
        self.assertEqual(pg.cursor_field, "next_cursor")
        self.assertEqual(pg.has_more_field, "has_more")
        self.assertEqual(pg.items_field, "results")
        self.assertEqual(pg.page_size, 100)

    def test_manifest_rate_limit_no_remaining_header(self):
        m = api.load_manifests()
        # Notion signals rate limits with 429 + Retry-After only, no remaining count header.
        self.assertEqual(m["notion"].rate_limit_remaining_header, "")

    def test_connector_registers_same_key(self):
        # The script connector's MANIFEST constant must declare the same key as the YAML row.
        # Note: setUp clears MANIFESTS so we re-register via load_manifests(); the YAML loader
        # must not clobber an explicit register() (idempotency guard tested in test_api.py).
        api.load_manifests()
        self.assertIn("notion", api.MANIFESTS)
        self.assertEqual(notion_conn.MANIFEST.key, "notion")
        self.assertEqual(notion_conn.MANIFEST.auth.strategy, "bearer")


class NotionBrokeredMode(unittest.TestCase):
    """Brokered run: no token in the container, reads go to the host broker, writes are refused."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("RC_CONN_NOTION", "RC_API_BROKERED_KEYS")}
        os.environ.pop("RC_CONN_NOTION", None)
        os.environ["RC_API_BROKERED_KEYS"] = "notion"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @responses_lib.activate
    def test_search_and_query_post_through_broker_without_token(self):
        broker = "http://rc-broker.internal/notion/__url/"
        responses_lib.add(responses_lib.POST, broker + quote(SEARCH_URL, safe=""), json=_SEARCH_PAGE_2, status=200)
        responses_lib.add(responses_lib.POST, broker + quote(DB_QUERY_URL, safe=""), json=_SEARCH_PAGE_2, status=200)
        notion_conn.search("onboarding")
        notion_conn.query_database(DB_ID)
        self.assertEqual(len(responses_lib.calls), 2)
        for call in responses_lib.calls:
            self.assertNotIn("Authorization", call.request.headers)
            self.assertEqual(call.request.headers["Notion-Version"], notion_conn._NOTION_VERSION)

    def test_write_post_is_refused_before_any_request(self):
        with self.assertRaises(api.MethodPolicyError):
            notion_conn._post("pages", {"parent": {"page_id": "x"}})
        with self.assertRaises(api.MethodPolicyError):
            notion_conn._request("PATCH", "pages/x", body={"archived": True})


class NotionQueryDatabaseIdFallback(unittest.TestCase):
    """query-db given a database id (as in a Notion URL) resolves its single data source."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_database_id_falls_back_to_its_data_source(self):
        ds = "b1b1b1b1-0000-4000-8000-000000000009"
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json={"object": "error", "status": 404}, status=404)
        responses_lib.add(responses_lib.GET, f"{API_BASE}/databases/{DB_ID}", json={"data_sources": [{"id": ds, "name": "Orders"}]})
        responses_lib.add(responses_lib.POST, f"{API_BASE}/data_sources/{ds}/query", json=_SEARCH_PAGE_2, status=200)
        rows = notion_conn.query_database(DB_ID)
        self.assertEqual(len(rows), len(_SEARCH_PAGE_2["results"]))

    @responses_lib.activate
    def test_unknown_id_keeps_the_original_404(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json={"object": "error", "status": 404}, status=404)
        responses_lib.add(responses_lib.GET, f"{API_BASE}/databases/{DB_ID}", json={"object": "error"}, status=404)
        with self.assertRaises(api.ApiError) as ctx:
            notion_conn.query_database(DB_ID)
        self.assertIn("data_sources", ctx.exception.url)


class NotionSearchPagination(unittest.TestCase):
    """POST /v1/search stitches ≥2 pages and places the bearer + Notion-Version on every request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_search_paginates_two_pages_and_applies_field_preselection(self):
        # Page 1: has_more=True, next_cursor set.  Page 2: last page.
        responses_lib.add(
            responses_lib.POST, SEARCH_URL,
            json=_SEARCH_PAGE_1, status=200,
        )
        responses_lib.add(
            responses_lib.POST, SEARCH_URL,
            json=_SEARCH_PAGE_2, status=200,
        )

        results = notion_conn.search("onboarding")

        self.assertEqual(len(responses_lib.calls), 2, "should have fetched exactly 2 pages")
        self.assertEqual(len(results), 2, "both pages stitched into one list")

        # First result: field pre-selection applied — compact_page shape
        r1 = results[0]
        self.assertEqual(r1["id"], _PAGE_1["id"])
        self.assertEqual(r1["title"], "Meeting Notes")
        self.assertEqual(r1["properties"]["Status"], "In Progress")
        self.assertEqual(r1["properties"]["Due"], "2024-07-01")
        self.assertIn("url", r1)
        self.assertIn("last_edited_time", r1)

        r2 = results[1]
        self.assertEqual(r2["title"], "Onboarding Checklist")
        self.assertEqual(r2["properties"]["Notes"], "All steps completed.")

    @responses_lib.activate
    def test_search_bearer_on_every_request(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("test")

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Expected Bearer auth on every POST, got: {auth!r}",
            )

    @responses_lib.activate
    def test_search_notion_version_header_on_every_request(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("test")

        for call in responses_lib.calls:
            version = call.request.headers.get("Notion-Version", "")
            self.assertEqual(
                version, "2026-03-11",
                f"Notion-Version header missing or wrong on POST: {version!r}",
            )

    @responses_lib.activate
    def test_search_cursor_sent_on_page_two(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("cursor test")

        # Second request body must carry the start_cursor from page 1's next_cursor.
        call2_body = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(call2_body.get("start_cursor"), "cursor-token-abc123")

    @responses_lib.activate
    def test_search_filter_type_sent_in_body(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("data sources only", filter_type="data_source")

        call_body = json.loads(responses_lib.calls[0].request.body)
        self.assertIn("filter", call_body)
        self.assertEqual(call_body["filter"]["value"], "data_source")
        self.assertEqual(call_body["filter"]["property"], "object")

    @responses_lib.activate
    def test_search_database_alias_maps_to_data_source(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        notion_conn.search("legacy alias", filter_type="database")

        call_body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(call_body["filter"]["value"], "data_source")

    @responses_lib.activate
    def test_search_data_source_extracts_top_level_title(self):
        responses_lib.add(
            responses_lib.POST, SEARCH_URL,
            json={
                "object": "list",
                "results": [_DATA_SOURCE_SEARCH_RESULT],
                "has_more": False,
                "next_cursor": None,
            },
            status=200,
        )

        results = notion_conn.search("support", filter_type="data_source")

        self.assertEqual(results[0]["title"], "Support Queue")


class NotionQueryDatabase(unittest.TestCase):
    """POST /v1/data_sources/{id}/query stitches pages and pre-selects fields."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_query_db_paginates_two_pages(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        results = notion_conn.query_database(DB_ID)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Meeting Notes")
        self.assertEqual(results[1]["title"], "Onboarding Checklist")

    @responses_lib.activate
    def test_query_db_bearer_on_every_request(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        notion_conn.query_database(DB_ID)

        for call in responses_lib.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(auth.startswith("Bearer "), f"Missing Bearer: {auth!r}")

    @responses_lib.activate
    def test_query_db_cursor_sent_on_page_two(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_1, status=200)
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        notion_conn.query_database(DB_ID)

        call2_body = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(call2_body.get("start_cursor"), "db-cursor-xyz789")

    @responses_lib.activate
    def test_query_db_passes_filter_and_sorts_json(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        notion_conn.query_database(
            DB_ID,
            filter_json={"property": "Status", "status": {"equals": "Open"}},
            sorts_json=[{"property": "Due", "direction": "ascending"}],
        )

        call_body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(call_body["filter"]["property"], "Status")
        self.assertEqual(call_body["sorts"][0]["property"], "Due")


class NotionMarkdown(unittest.TestCase):
    """Current Notion Markdown API wrapper reads page content."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_markdown_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_retrieve_markdown_uses_current_version_and_query(self):
        responses_lib.add(responses_lib.GET, PAGE_1_MARKDOWN_URL, json=_PAGE_MARKDOWN, status=200)

        result = notion_conn.retrieve_markdown(_PAGE_1["id"], include_transcript=True)

        self.assertEqual(result["markdown"], _PAGE_MARKDOWN["markdown"])
        call = responses_lib.calls[0].request
        self.assertEqual(call.headers.get("Notion-Version"), "2026-03-11")
        self.assertIn("include_transcript=true", call.url)

class NotionDatabaseRows(unittest.TestCase):
    """Rows are pages; the connector reads and renders them compactly."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_rows_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_retrieve_row_compacts_page_properties(self):
        responses_lib.add(responses_lib.GET, PAGE_1_URL, json=_PAGE_1, status=200)

        row = notion_conn.retrieve_row(_PAGE_1["id"])

        self.assertEqual(row["title"], "Meeting Notes")
        self.assertEqual(row["properties"]["Status"], "In Progress")


class NotionPickViaLibApi(unittest.TestCase):
    """lib.api's GET client + pick() work on Notion GET endpoints (pages, databases, blocks)."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_pick_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_pick_selects_support_fields_from_get_page(self):
        page_url = f"{API_BASE}/pages/{_PAGE_1['id']}"
        responses_lib.add(responses_lib.GET, page_url, json=_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["notion"])
        body = c.get(f"pages/{_PAGE_1['id']}")

        # Verify Notion-Version rode along on the GET too.
        self.assertEqual(
            responses_lib.calls[0].request.headers.get("Notion-Version"),
            "2026-03-11",
        )
        # pick() extracts dotted paths from the raw body
        picked = api.pick(body, "id,url,last_edited_time")
        self.assertEqual(picked["id"], _PAGE_1["id"])
        self.assertEqual(picked["url"], _PAGE_1["url"])
        self.assertIn("last_edited_time", picked)


class NotionCLIDrive(unittest.TestCase):
    """CLI subcommands for both search and query-db drive the script end-to-end."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_NOTION")
        os.environ["RC_CONN_NOTION"] = "secret_" + "notion_cli_test"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NOTION", None)
        else:
            os.environ["RC_CONN_NOTION"] = self._saved

    @responses_lib.activate
    def test_cli_search_prints_markdown(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["search", "onboarding"])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("# Notion search", output)
        self.assertIn("Onboarding Checklist", output)

    @responses_lib.activate
    def test_cli_search_with_filter_flag(self):
        responses_lib.add(responses_lib.POST, SEARCH_URL, json=_SEARCH_PAGE_2, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["search", "docs", "--filter", "page"])

        self.assertEqual(rc, 0)
        call_body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(call_body["filter"]["value"], "page")

    @responses_lib.activate
    def test_cli_query_db_prints_markdown(self):
        responses_lib.add(responses_lib.POST, DB_QUERY_URL, json=_DB_QUERY_PAGE_2, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["query-db", DB_ID])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn(f"Notion data source: {DB_ID}", output)
        self.assertIn("Onboarding Checklist", output)

    @responses_lib.activate
    def test_cli_page_md_prints_markdown(self):
        responses_lib.add(responses_lib.GET, PAGE_1_MARKDOWN_URL, json=_PAGE_MARKDOWN, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["page-md", _PAGE_1["id"]])

        self.assertEqual(rc, 0)
        self.assertIn("Body line", buf.getvalue())

    @responses_lib.activate
    def test_cli_row_prints_markdown(self):
        responses_lib.add(responses_lib.GET, PAGE_1_URL, json=_PAGE_1, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = notion_conn.main(["row", _PAGE_1["id"]])

        self.assertEqual(rc, 0)
        self.assertIn("Meeting Notes", buf.getvalue())

    @responses_lib.activate
    def test_cli_lib_api_get_notion(self):
        """The manifest-only generic CLI also drives Notion GET endpoints."""
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        page_url = f"{API_BASE}/pages/{_PAGE_1['id']}"
        responses_lib.add(responses_lib.GET, page_url, json=_PAGE_1, status=200)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = api._main([
                "get", "notion", f"pages/{_PAGE_1['id']}",
                "--pick", "id,url",
            ])

        self.assertEqual(rc, 0)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["id"], _PAGE_1["id"])


class NotionTokenHygiene(unittest.TestCase):
    """CI guard: no real Notion integration token prefix may land in the connector directory.

    Scopes to the connector dir only — this test file legitimately names the prefixes it hunts,
    so scanning itself would produce a false positive (split the literals with concatenation).
    """

    # Notion integration tokens are prefixed "secret_"; OAuth access tokens use "ntn_".
    # Both are guarded here.
    _TOKEN_PREFIXES = ("secret" + "_", "ntn" + "_")

    def test_no_token_prefixes_in_notion_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "notion"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: contains {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
