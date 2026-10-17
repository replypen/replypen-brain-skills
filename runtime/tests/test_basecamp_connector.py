"""Fixture test for the manifest-ONLY Basecamp integration — proves a catalogued connector with
NO bespoke Python is drivable end-to-end through `lib.api`'s YAML loader + CLI.

No live creds, no network: HTTP is mocked with `responses`. Bodies are based on Basecamp's own
documented example payloads (github.com/basecamp/bc3-api), trimmed to support-relevant fields.
Basecamp paginates with RFC 5988 `Link: <url>; rel="next"` headers, so two mocked pages exercise
the real `link` pagination style end-to-end. A required User-Agent header is asserted on every
request (missing it returns 400 Bad Request per Basecamp's documented requirement).

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_basecamp_connector.py -q
"""

import os
import sys
import unittest
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402

ACCOUNT_ID = "195539477"
BASE = f"https://3.basecampapi.com/{ACCOUNT_ID}"
PROJECTS_URL = f"{BASE}/projects.json"
TODOS_URL = f"{BASE}/todolists/987654321/todos.json"

# Two pages of projects (bare JSON arrays, as Basecamp returns). Shapes mirror documented example
# project objects; only support-relevant fields are included.
_PROJECTS_PAGE_1 = [
    {
        "id": 2085958504,
        "status": "active",
        "name": "The Leto Laptop",
        "description": "Laptop product launch.",
        "purpose": "topic",
        "created_at": "2025-12-29T18:52:00.000Z",
        "updated_at": "2026-02-26T16:42:05.843Z",
        "clients_enabled": False,
        "bookmarked": False,
        "app_url": "https://3.basecamp.com/195539477/projects/2085958504",
        "url": "https://3.basecampapi.com/195539477/projects/2085958504.json",
    },
]
_PROJECTS_PAGE_2 = [
    {
        "id": 2085958505,
        "status": "active",
        "name": "Marketing Website Redesign",
        "description": "Q3 website overhaul.",
        "purpose": "topic",
        "created_at": "2026-01-10T09:00:00.000Z",
        "updated_at": "2026-06-01T12:00:00.000Z",
        "clients_enabled": True,
        "bookmarked": True,
        "app_url": "https://3.basecamp.com/195539477/projects/2085958505",
        "url": "https://3.basecampapi.com/195539477/projects/2085958505.json",
    },
]

# RFC 5988 Link header: page 1 points at page 2 as rel="next"; page 2 has no next → loop stops.
_PROJECTS_PAGE_1_LINK = (
    f'<{PROJECTS_URL}?page=2>; rel="next", '
    f'<{PROJECTS_URL}?page=2>; rel="last"'
)

# Sample to-do items for a single page (exercises items_field="" bare array + single page stop).
_TODOS_PAGE_1 = [
    {
        "id": 1001,
        "status": "active",
        "title": "Write release notes",
        "completed": False,
        "due_on": "2026-07-15",
        "comments_count": 3,
        "assignees": [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}],
        "position": 1,
    },
    {
        "id": 1002,
        "status": "active",
        "title": "QA sign-off",
        "completed": False,
        "due_on": "2026-07-20",
        "comments_count": 0,
        "assignees": [{"name": "Alan Turing"}],
        "position": 2,
    },
]


class BasecampManifestOnly(unittest.TestCase):
    def setUp(self):
        # Clean registry so the YAML loader is what populates `basecamp` (no leaked register()).
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_BASECAMP")
        # Split prefix with concatenation so the token-hygiene guard in this file can't flag itself.
        os.environ["RC_CONN_BASECAMP"] = "test_" + "bc3_fake_token_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_BASECAMP", None)
        else:
            os.environ["RC_CONN_BASECAMP"] = self._saved

    def test_manifest_loaded_from_yaml_with_link_pagination(self):
        m = api.load_manifests()
        self.assertIn("basecamp", m)
        bc = m["basecamp"]
        self.assertEqual(bc.base_url, "https://3.basecampapi.com")
        self.assertEqual(bc.auth.strategy, "bearer")
        self.assertEqual(bc.pagination.style, "link")
        self.assertEqual(bc.pagination.items_field, "")   # bare JSON array — page IS the list
        self.assertEqual(bc.rate_limit_remaining_header, "")  # Basecamp sends none
        # Required User-Agent must be declared (missing it returns 400 from Basecamp).
        self.assertIn("User-Agent", bc.default_headers)
        self.assertTrue(bc.default_headers["User-Agent"].startswith("replypen-integration"))

    @responses.activate
    def test_link_pagination_stitches_two_pages(self):
        # Page 1: bare project array + Link rel="next" pointing at page 2.
        responses.add(
            responses.GET,
            PROJECTS_URL,
            json=_PROJECTS_PAGE_1,
            status=200,
            headers={"Link": _PROJECTS_PAGE_1_LINK},
        )
        # Page 2: bare array, no Link header → pagination stops.
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["basecamp"])
        result = c.collect(f"{ACCOUNT_ID}/projects.json")

        self.assertFalse(result["incomplete"], result["reason"])
        names = [it["name"] for it in result["items"]]
        self.assertEqual(names, ["The Leto Laptop", "Marketing Website Redesign"])
        self.assertEqual(len(result["items"]), 2)

    @responses.activate
    def test_bearer_credential_on_every_request_including_link_follow(self):
        # Ensure the Bearer token rides both the initial request AND the link-followed page.
        responses.add(
            responses.GET,
            PROJECTS_URL,
            json=_PROJECTS_PAGE_1,
            status=200,
            headers={"Link": _PROJECTS_PAGE_1_LINK},
        )
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["basecamp"])
        c.collect(f"{ACCOUNT_ID}/projects.json")

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            auth = call.request.headers.get("Authorization", "")
            self.assertTrue(
                auth.startswith("Bearer "),
                f"Expected Bearer on request, got: {auth!r}",
            )
            self.assertIn("test_bc3_fake_token_for_unit_tests", auth)

    @responses.activate
    def test_required_user_agent_header_on_every_request(self):
        # Basecamp returns 400 if User-Agent is missing; assert it's on the wire.
        responses.add(
            responses.GET,
            PROJECTS_URL,
            json=_PROJECTS_PAGE_1,
            status=200,
            headers={"Link": _PROJECTS_PAGE_1_LINK},
        )
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS_PAGE_2, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["basecamp"])
        c.collect(f"{ACCOUNT_ID}/projects.json")

        for call in responses.calls:
            ua = call.request.headers.get("User-Agent", "")
            self.assertIn("replypen-integration", ua)

    @responses.activate
    def test_single_page_todos_and_pick_selects_fields(self):
        # Single-page to-do list (no Link rel="next" → stops after first page).
        responses.add(responses.GET, TODOS_URL, json=_TODOS_PAGE_1, status=200)

        api.load_manifests()
        c = api.client(api.MANIFESTS["basecamp"])
        result = c.collect(f"{ACCOUNT_ID}/todolists/987654321/todos.json")

        self.assertFalse(result["incomplete"], result["reason"])
        self.assertEqual(len(result["items"]), 2)

        # --pick prunes to support-relevant fields.
        picked = [api.pick(it, "id,title,completed,due_on,assignees.*.name") for it in result["items"]]
        self.assertEqual(picked[0]["title"], "Write release notes")
        self.assertEqual(picked[0]["completed"], False)
        self.assertEqual(picked[0]["assignees.*.name"], ["Ada Lovelace", "Grace Hopper"])
        self.assertEqual(picked[1]["assignees.*.name"], ["Alan Turing"])

    @responses.activate
    def test_cli_drives_basecamp_with_bearer_and_paginate(self):
        responses.add(
            responses.GET,
            PROJECTS_URL,
            json=_PROJECTS_PAGE_1,
            status=200,
            headers={"Link": _PROJECTS_PAGE_1_LINK},
        )
        responses.add(responses.GET, PROJECTS_URL, json=_PROJECTS_PAGE_2, status=200)

        rc = api._main([
            "get", "basecamp", f"{ACCOUNT_ID}/projects.json",
            "--paginate", "--pick", "id,name,status",
        ])
        self.assertEqual(rc, 0)
        # Both pages were fetched.
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(responses.calls[0].request.url.startswith(PROJECTS_URL))
        auth = responses.calls[0].request.headers.get("Authorization", "")
        self.assertIn("test_bc3_fake_token_for_unit_tests", auth)


class BasecampCassetteHygiene(unittest.TestCase):
    """CI guard: no real Basecamp token material may land in the committed connector files.

    Scopes to the connector dir only — this test file legitimately names prefixes it hunts for,
    so scanning itself would be a false positive.
    """

    # Basecamp personal access tokens are typically long opaque strings; OAuth tokens from
    # 37signals start with "BAhb" (Base64-encoded JSON). Split with concatenation so the guard
    # can't flag itself.
    _TOKEN_PREFIXES = ("BAhb" "8",)   # 37signals serialized OAuth token prefix

    def test_no_token_material_in_basecamp_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "basecamp"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: {pref!r}")
        self.assertEqual(offenders, [], f"token-like material present: {offenders}")


if __name__ == "__main__":
    unittest.main()
