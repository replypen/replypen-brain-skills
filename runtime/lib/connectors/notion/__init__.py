"""Notion support connector — script connector for Notion's body-heavy read APIs.

Force-code triggers:
  (3) exotic transport — Notion's ``POST /v1/search`` and ``POST /v1/data_sources/{id}/query``
      send query params in a JSON request body.
  (1) field pre-selection — raw page ``properties`` objects are enormous (hundreds of nested keys);
      the script extracts support-relevant fields.

For plain GET endpoints that do not need response shaping (retrieve page, retrieve data source, block
children) the agent can still drive ``python -m lib.api get notion …`` directly.

CLI:
    python -m lib.connectors.notion search "onboarding checklist"
    python -m lib.connectors.notion search "bug" --filter page --page-size 10
    python -m lib.connectors.notion search "roadmap" --filter data_source
    python -m lib.connectors.notion query-db <data_source_id>
    python -m lib.connectors.notion query-db <data_source_id> --page-size 50
    python -m lib.connectors.notion page-md <page_id>
    python -m lib.connectors.notion row <page_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from lib import api

API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2026-03-11"

# Mirrors manifest.yaml; this explicit register() wins over the YAML row, so the read-only POST
# allowlist must be repeated here or brokered search/query (and `lib.api post notion`) are refused.
MANIFEST = api.register(
    api.Manifest(
        key="notion",
        base_url=API_BASE,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(
            style="cursor",
            cursor_param="start_cursor",
            cursor_field="next_cursor",
            has_more_field="has_more",
            items_field="results",
            page_size=100,
        ),
        rate_limit_remaining_header="",  # Notion uses 429 + Retry-After, no remaining-count header
        default_headers={"Notion-Version": _NOTION_VERSION},
        allowed_post_paths=("/search", "/data_sources/*/query", "/v1/search", "/v1/data_sources/*/query"),
    )
)


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="notion")


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    """One Notion call through lib.api, so a brokered run never holds the token.

    The token carries the connection's write capabilities (Notion has no per-grant scopes), so it
    must stay host-side; lib.api's read tier admits only GETs and the allowlisted search/query POSTs.
    """
    path = path.lstrip("/")
    return _client().request(method.upper(), path, json_body=body, query=query, endpoint_template=f"/v1/{path}")


def _get(path: str, *, query: dict[str, Any] | None = None) -> Any:
    return _request("GET", path, query=query)


def _post(path: str, body: dict[str, Any]) -> Any:
    return _request("POST", path, body=body)


def _post_paginate(path: str, body: dict[str, Any], *, page_size: int = 100) -> list[dict]:
    """Paginate a POST endpoint (search / database query) through all cursor pages.

    Notion's cursor pagination on POST endpoints works identically to GET: the next page body
    includes ``next_cursor`` (opaque string) and ``has_more`` (bool); we send ``start_cursor`` in
    the next request body.
    """
    results: list[dict] = []
    req_body = dict(body, page_size=page_size)
    while True:
        page = _post(path, req_body)
        results.extend(page.get("results") or [])
        if not page.get("has_more"):
            break
        req_body = dict(req_body, start_cursor=page["next_cursor"])
    return results


# ---------------------------------------------------------------------------
# Support-relevant field extraction
# ---------------------------------------------------------------------------

_PAGE_PICK = "id,url,created_time,last_edited_time,parent"

# Notion page property values are nested under arbitrary property names with typed objects.
# We extract the plain-text value for every property to keep context tight.
def _plain_text(rich_text_arr: Any) -> str:
    """Flatten a Notion rich_text array to a plain string."""
    if not isinstance(rich_text_arr, list):
        return ""
    return "".join(seg.get("plain_text", "") for seg in rich_text_arr)


def _extract_title(page: dict) -> str:
    """Return the human-readable title of a page/data source object."""
    if isinstance(page.get("title"), list):
        title = _plain_text(page.get("title") or [])
        if title:
            return title
    props = page.get("properties") or {}
    for val in props.values():
        if isinstance(val, dict) and val.get("type") == "title":
            return _plain_text(val.get("title") or [])
    return ""


def compact_page(page: dict) -> dict:
    """Pre-select the support-relevant fields from a raw Notion page object.

    Raw pages have enormous ``properties`` objects with every database column. We extract:
    - id, url, created/edited timestamps, parent reference
    - title (first title-type property, flattened to plain text)
    - a summary of all other properties: {name: plain-text-value} for text/select/date types
    """
    props = page.get("properties") or {}
    compact_props: dict[str, Any] = {}
    for name, val in props.items():
        if not isinstance(val, dict):
            continue
        t = val.get("type")
        if t == "title":
            compact_props[name] = _plain_text(val.get("title") or [])
        elif t in ("rich_text", "text"):
            compact_props[name] = _plain_text(val.get(t) or [])
        elif t == "select":
            sel = val.get("select") or {}
            compact_props[name] = sel.get("name") or ""
        elif t == "multi_select":
            compact_props[name] = [s.get("name", "") for s in (val.get("multi_select") or [])]
        elif t == "status":
            st = val.get("status") or {}
            compact_props[name] = st.get("name") or ""
        elif t == "date":
            d = val.get("date") or {}
            compact_props[name] = d.get("start") or ""
        elif t == "url":
            compact_props[name] = val.get("url") or ""
        elif t == "email":
            compact_props[name] = val.get("email") or ""
        elif t == "phone_number":
            compact_props[name] = val.get("phone_number") or ""
        elif t == "number":
            compact_props[name] = val.get("number")
        elif t == "checkbox":
            compact_props[name] = val.get("checkbox")
        # Skip: formula, rollup, relation, files, people, created_by, last_edited_by (complex/noisy)

    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "parent": page.get("parent"),
        "title": _extract_title(page),
        "properties": compact_props,
    }


def _compact_page(page: dict) -> dict:
    return compact_page(page)


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def search(
    query: str = "",
    *,
    filter_type: str | None = None,
    page_size: int = 100,
) -> list[dict]:
    """POST /v1/search — find pages/databases matching a keyword, return compact page objects.

    ``filter_type`` is ``"page"`` or ``"data_source"``. ``"database"`` is accepted as a legacy
    alias and mapped to ``"data_source"`` for the pinned 2026 API version.
    """
    body: dict[str, Any] = {}
    if query:
        body["query"] = query
    if filter_type:
        if filter_type == "database":
            filter_type = "data_source"
        # Notion's filter object: {"value": "page"|"data_source", "property": "object"}
        body["filter"] = {"value": filter_type, "property": "object"}
    # Paths are relative to API_BASE (https://api.notion.com/v1); no leading "v1/" prefix.
    results = _post_paginate("search", body, page_size=page_size)
    return [compact_page(r) for r in results]


def query_database(
    data_source_id: str,
    *,
    page_size: int = 100,
    filter_json: dict[str, Any] | None = None,
    sorts_json: list[dict[str, Any]] | None = None,
) -> list[dict]:
    """POST /v1/data_sources/{id}/query — return all rows as compact page objects.

    ``filter_json`` and ``sorts_json`` are passed through as Notion's native query DSL. The
    convenience wrapper exists for transport, pagination, and compact row rendering; it does not
    reimplement Notion's full filter language.
    """
    path = f"data_sources/{data_source_id}/query"
    body: dict[str, Any] = {}
    if filter_json:
        body["filter"] = filter_json
    if sorts_json:
        body["sorts"] = sorts_json
    results = _post_paginate(path, body, page_size=page_size)
    return [compact_page(r) for r in results]


def retrieve_markdown(page_id: str, *, include_transcript: bool = False) -> dict[str, Any]:
    """GET /v1/pages/{id}/markdown — return Notion-flavored Markdown for one page."""
    query = {"include_transcript": "true"} if include_transcript else None
    return _get(f"pages/{page_id}/markdown", query=query)


def _loads_json_arg(value: str) -> Any:
    if value == "-":
        value = sys.stdin.read()
    return json.loads(value)


def retrieve_row(page_id: str) -> dict[str, Any]:
    """GET /v1/pages/{id} with compact property rendering."""
    return compact_page(_get(f"pages/{page_id}"))


def _results_to_markdown(results: list[dict], heading: str) -> str:
    lines = [f"# {heading}", f"Found {len(results)} result(s).", ""]
    for r in results:
        title = r.get("title") or r.get("id", "—")
        url = r.get("url") or ""
        edited = r.get("last_edited_time") or ""
        lines.append(f"## {title}")
        if url:
            lines.append(f"- URL: {url}")
        if edited:
            lines.append(f"- Last edited: {edited}")
        props = r.get("properties") or {}
        if props:
            for k, v in props.items():
                if v not in (None, "", [], {}):
                    lines.append(f"- {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def _page_markdown_to_markdown(result: dict[str, Any]) -> str:
    md = result.get("markdown") or ""
    lines = [md.rstrip(), ""]
    if result.get("truncated"):
        unknown = result.get("unknown_block_ids") or []
        lines.append(f"\n<!-- truncated; unknown_block_ids={unknown} -->")
    return "\n".join(lines).lstrip()


def _page_to_markdown(page: dict[str, Any], heading: str = "Notion page") -> str:
    return _results_to_markdown([page], heading)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.notion")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search pages/databases by keyword (POST /v1/search)")
    s.add_argument("query", nargs="?", default="", help="keyword to search (omit for all)")
    s.add_argument(
        "--filter",
        dest="filter_type",
        choices=["page", "data_source", "database"],
        help="restrict to pages or data sources (database is a legacy alias)",
    )
    s.add_argument("--page-size", type=int, default=100)

    q = sub.add_parser("query-db", help="list rows in a data source")
    q.add_argument("data_source_id", help="Notion data source UUID")
    q.add_argument("--page-size", type=int, default=100)
    q.add_argument("--filter-json", help="Notion data source query filter JSON")
    q.add_argument("--sorts-json", help="Notion data source query sorts JSON array")

    pm = sub.add_parser("page-md", help="read one page as Notion-flavored Markdown")
    pm.add_argument("page_id")
    pm.add_argument("--include-transcript", action="store_true")

    rr = sub.add_parser("row", help="read one database row/page by id")
    rr.add_argument("page_id")

    args = parser.parse_args(argv)

    if args.cmd == "search":
        results = search(args.query, filter_type=args.filter_type, page_size=args.page_size)
        print(_results_to_markdown(results, f'Notion search: "{args.query}"'))
    elif args.cmd == "query-db":
        filter_json = _loads_json_arg(args.filter_json) if args.filter_json else None
        sorts_json = _loads_json_arg(args.sorts_json) if args.sorts_json else None
        results = query_database(
            args.data_source_id,
            page_size=args.page_size,
            filter_json=filter_json,
            sorts_json=sorts_json,
        )
        print(_results_to_markdown(results, f"Notion data source: {args.data_source_id}"))
    elif args.cmd == "page-md":
        print(_page_markdown_to_markdown(
            retrieve_markdown(args.page_id, include_transcript=args.include_transcript),
        ))
    elif args.cmd == "row":
        print(_page_to_markdown(retrieve_row(args.page_id), f"Notion row: {args.page_id}"))
    else:
        parser.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
