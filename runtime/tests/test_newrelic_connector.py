"""Fixture test for the New Relic NerdGraph script connector.

Force-code trigger (c): NerdGraph is GraphQL (POST transport); lib.api is GET/REST only.
The connector issues POST requests with a JSON ``query`` body and handles cursor-based
pagination INSIDE the GraphQL query (nextCursor in entitySearch results).

No live creds, no network: HTTP is mocked with ``responses``. Bodies mirror the NerdGraph
documented example payloads (trimmed to support-relevant fields).

Tests cover:
  - YAML manifest loads and maps every field correctly
  - The script's register() wins over the YAML loader (idempotence)
  - Credential rides every POST as Api-Key header
  - entity search: cursor pagination stitches ≥2 pages
  - NRQL: single-call query, results extracted
  - violations: single-page list returned
  - incidents: single-page list returned
  - api.pick selects support fields
  - CLI drive (main([...]) calls for entities, nrql, violations, incidents)
  - Token-prefix hygiene guard for this connector's files

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \\
        pytest tests/test_newrelic_connector.py -q
"""

import io
import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import api  # noqa: E402
from lib.connectors import newrelic as nr  # noqa: E402

GRAPHQL_URL = "https://api.newrelic.com/graphql"
EU_GRAPHQL_URL = "https://api.eu.newrelic.com/graphql"
ACCOUNT_ID = 1234567

# The API-error tests exhaust the connector's inline retry backoff; skip the sleeps, keep the retries.
_NO_SLEEP = mock.patch("time.sleep", lambda *_: None)
setUpModule, tearDownModule = _NO_SLEEP.start, _NO_SLEEP.stop

# ---------------------------------------------------------------------------
# Documented example payloads (trimmed to support-relevant fields).
# NerdGraph always returns HTTP 200 with {"data": {...}} on success.
# ---------------------------------------------------------------------------

# entitySearch — page 1: two entities, nextCursor set
_ENTITIES_PAGE_1 = {
    "data": {
        "actor": {
            "entitySearch": {
                "results": {
                    "entities": [
                        {
                            "name": "my-api-service",
                            "guid": "MXxBUE18QVBQTElDQVRJT058MTIz",
                            "entityType": "APM_APPLICATION_ENTITY",
                            "alertSeverity": "CRITICAL",
                            "reporting": True,
                            "domain": "APM",
                            "type": "APPLICATION",
                        }
                    ],
                    "nextCursor": "cursor_abc123",
                }
            }
        }
    }
}

# entitySearch — page 2: one entity, no nextCursor → stop
_ENTITIES_PAGE_2 = {
    "data": {
        "actor": {
            "entitySearch": {
                "results": {
                    "entities": [
                        {
                            "name": "background-worker",
                            "guid": "MXxBUE18QVBQTElDQVRJT058NDU2",
                            "entityType": "APM_APPLICATION_ENTITY",
                            "alertSeverity": "NOT_ALERTING",
                            "reporting": True,
                            "domain": "APM",
                            "type": "APPLICATION",
                        }
                    ],
                    "nextCursor": None,
                }
            }
        }
    }
}

# NRQL query result
_NRQL_RESULT = {
    "data": {
        "actor": {
            "account": {
                "nrql": {
                    "results": [
                        {"count": 42, "error.class": "RuntimeError"},
                        {"count": 17, "error.class": "TimeoutError"},
                    ]
                }
            }
        }
    }
}

# Open violations
_VIOLATIONS_RESULT = {
    "data": {
        "actor": {
            "account": {
                "alerts": {
                    "violations": {
                        "violations": [
                            {
                                "label": "my-api-service > Error rate > 5%",
                                "duration": 1800,
                                "severity": "CRITICAL",
                                "status": "open",
                                "openedAt": 1719000000000,
                                "closedAt": None,
                                "entity": {"name": "my-api-service", "type": "APPLICATION"},
                                "condition": {"name": "High error rate"},
                            }
                        ]
                    }
                }
            }
        }
    }
}

# Alert incidents
_INCIDENTS_RESULT = {
    "data": {
        "actor": {
            "account": {
                "alerts": {
                    "incidents": {
                        "incidents": [
                            {
                                "incidentId": "inc_001",
                                "title": "my-api-service: High error rate",
                                "priority": "CRITICAL",
                                "state": "CREATED",
                                "createdAt": 1719000000000,
                                "closedAt": None,
                                "sources": [{"policyId": "42", "conditionName": "High error rate"}],
                            }
                        ]
                    }
                }
            }
        }
    }
}

# GraphQL-level error (HTTP 200 but errors array present)
_GQL_ERROR_RESPONSE = {
    "errors": [{"message": "User is not authorized to access this account", "locations": []}],
    "data": None,
}


class NewrelicManifest(unittest.TestCase):
    """Manifest loading and registration contract."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        # Split prefix so the hygiene guard doesn't flag this test file.
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    def test_manifest_loads_from_yaml_and_maps_all_fields(self):
        """YAML manifest loads and every field maps correctly to the Manifest dataclass."""
        m = api.load_manifests()
        self.assertIn("newrelic", m)
        nr_m = m["newrelic"]
        self.assertEqual(nr_m.base_url, "https://api.newrelic.com/graphql")
        self.assertEqual(nr_m.auth.strategy, "api_key_header")
        self.assertEqual(nr_m.auth.name, "Api-Key")
        self.assertEqual(nr_m.pagination.style, "none")
        self.assertEqual(nr_m.rate_limit_remaining_header, "")
        self.assertEqual(nr_m.default_headers.get("Content-Type"), "application/json")

    def test_script_register_wins_over_yaml(self):
        """Explicit register() from the connector module takes priority over the YAML loader."""
        # Re-register explicitly (simulates module import after setUp cleared MANIFESTS).
        api.register(nr.MANIFEST)
        self.assertIn("newrelic", api.MANIFESTS)
        self.assertNotIn("newrelic", api._YAML_LOADED_KEYS)

        # load_manifests must NOT clobber an explicitly registered key.
        api.load_manifests()
        self.assertIn("newrelic", api.MANIFESTS)
        self.assertNotIn("newrelic", api._YAML_LOADED_KEYS)
        self.assertEqual(api.MANIFESTS["newrelic"].base_url, "https://api.newrelic.com/graphql")


class NewrelicAuth(unittest.TestCase):
    """Credential placement on every POST."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_api_key_header_on_every_post_including_cursor_follow(self):
        """Api-Key header must appear on EVERY POST — page 1 and the cursor-follow page 2."""
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_1, status=200)
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        nr.query_entities("type = 'APPLICATION'")

        self.assertEqual(len(responses.calls), 2)
        for call in responses.calls:
            api_key = call.request.headers.get("Api-Key", "")
            self.assertTrue(
                api_key.startswith("NRAK"),
                f"expected NRAK… Api-Key on every request, got: {api_key!r}",
            )

    @responses.activate
    def test_eu_endpoint_used_when_eu_flag_set(self):
        """When eu=True, requests go to the EU NerdGraph endpoint."""
        responses.add(responses.POST, EU_GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        nr.query_entities("type = 'APPLICATION'", eu=True)

        self.assertEqual(len(responses.calls), 1)
        self.assertIn("api.eu.newrelic.com", responses.calls[0].request.url)


class NewrelicEntitySearch(unittest.TestCase):
    """Entity search with cursor-based pagination inside GraphQL."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_entity_search_stitches_two_pages_via_graphql_cursor(self):
        """query_entities() follows nextCursor across two GraphQL POSTs and returns all entities."""
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_1, status=200)
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        result = nr.query_entities("type = 'APPLICATION'")

        self.assertFalse(result["incomplete"], result["reason"])
        names = [it["name"] for it in result["items"]]
        self.assertEqual(names, ["my-api-service", "background-worker"])
        # Second call must include the cursor in its body
        second_body = json.loads(responses.calls[1].request.body)
        self.assertIn("cursor_abc123", second_body["query"])

    @responses.activate
    def test_entity_search_stops_when_next_cursor_is_none(self):
        """When nextCursor is None, the loop stops after one page."""
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        result = nr.query_entities()

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_entity_search_incomplete_on_api_error(self):
        """A 500 mid-stream (after retries exhausted) sets incomplete=True."""
        # First page succeeds, second page errors after retries.
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_1, status=200)
        # Exhaust retries with 500s (DEFAULT_MAX_RETRIES = 4 → 5 total calls for page 2).
        for _ in range(api.DEFAULT_MAX_RETRIES + 1):
            responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)

        result = nr.query_entities("type = 'APPLICATION'")

        self.assertTrue(result["incomplete"])
        self.assertIn("page fetch failed", result["reason"])
        # Already-collected page-1 items are preserved.
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["name"], "my-api-service")

    @responses.activate
    def test_pick_selects_support_fields_from_entities(self):
        """api.pick narrows the entity object to the few support-relevant fields."""
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        result = nr.query_entities()
        picked = [api.pick(it, "name,guid,alertSeverity,reporting") for it in result["items"]]
        self.assertEqual(picked[0]["name"], "background-worker")
        self.assertEqual(picked[0]["alertSeverity"], "NOT_ALERTING")
        self.assertTrue(picked[0]["reporting"])
        self.assertNotIn("entityType", picked[0])


class NewrelicNRQL(unittest.TestCase):
    """NRQL query — single POST, flat results list."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_nrql_returns_results_list(self):
        """run_nrql() extracts the results list from the nested NerdGraph response."""
        responses.add(responses.POST, GRAPHQL_URL, json=_NRQL_RESULT, status=200)

        rows = nr.run_nrql(
            ACCOUNT_ID,
            "SELECT count(*) FROM TransactionError SINCE 1 HOUR AGO FACET error.class LIMIT 20",
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["count"], 42)
        self.assertEqual(rows[0]["error.class"], "RuntimeError")

    @responses.activate
    def test_nrql_credential_on_request(self):
        """NRQL POST carries the Api-Key header."""
        responses.add(responses.POST, GRAPHQL_URL, json=_NRQL_RESULT, status=200)

        nr.run_nrql(ACCOUNT_ID, "SELECT count(*) FROM Transaction SINCE 1 HOUR AGO")

        self.assertEqual(len(responses.calls), 1)
        key = responses.calls[0].request.headers.get("Api-Key", "")
        self.assertTrue(key.startswith("NRAK"))

    @responses.activate
    def test_nrql_query_embedded_in_post_body(self):
        """The NRQL string is embedded inside the GraphQL query body, not as a URL param."""
        responses.add(responses.POST, GRAPHQL_URL, json=_NRQL_RESULT, status=200)
        nrql_str = "SELECT count(*) FROM Transaction SINCE 1 HOUR AGO"

        nr.run_nrql(ACCOUNT_ID, nrql_str)

        body = json.loads(responses.calls[0].request.body)
        self.assertIn("query", body)
        self.assertIn(nrql_str, body["query"])
        self.assertIn(str(ACCOUNT_ID), body["query"])


class NewrelicViolations(unittest.TestCase):
    """Alert violations — single POST."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_violations_returns_items_list(self):
        """query_violations() extracts the violations list."""
        responses.add(responses.POST, GRAPHQL_URL, json=_VIOLATIONS_RESULT, status=200)

        result = nr.query_violations(ACCOUNT_ID)

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        v = result["items"][0]
        self.assertEqual(v["severity"], "CRITICAL")
        self.assertEqual(v["entity"]["name"], "my-api-service")

    @responses.activate
    def test_violations_pick_fields(self):
        """api.pick selects support-relevant fields from each violation."""
        responses.add(responses.POST, GRAPHQL_URL, json=_VIOLATIONS_RESULT, status=200)

        result = nr.query_violations(ACCOUNT_ID)
        picked = [api.pick(v, "label,severity,entity.name") for v in result["items"]]
        self.assertEqual(picked[0]["severity"], "CRITICAL")
        self.assertEqual(picked[0]["entity.name"], "my-api-service")
        self.assertNotIn("duration", picked[0])

    @responses.activate
    def test_violations_incomplete_on_api_error(self):
        """An API error from violations sets incomplete=True."""
        for _ in range(api.DEFAULT_MAX_RETRIES + 1):
            responses.add(responses.POST, GRAPHQL_URL, json={}, status=503)

        result = nr.query_violations(ACCOUNT_ID)

        self.assertTrue(result["incomplete"])
        self.assertEqual(result["items"], [])


class NewrelicIncidents(unittest.TestCase):
    """Alert incidents — single POST."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_incidents_returns_items_list(self):
        """query_incidents() extracts the incidents list."""
        responses.add(responses.POST, GRAPHQL_URL, json=_INCIDENTS_RESULT, status=200)

        result = nr.query_incidents(ACCOUNT_ID)

        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["items"]), 1)
        inc = result["items"][0]
        self.assertEqual(inc["priority"], "CRITICAL")
        self.assertEqual(inc["state"], "CREATED")

    @responses.activate
    def test_incidents_pick_fields(self):
        """api.pick selects support-relevant fields from each incident."""
        responses.add(responses.POST, GRAPHQL_URL, json=_INCIDENTS_RESULT, status=200)

        result = nr.query_incidents(ACCOUNT_ID)
        picked = [api.pick(i, "title,priority,state,createdAt") for i in result["items"]]
        self.assertEqual(picked[0]["title"], "my-api-service: High error rate")
        self.assertEqual(picked[0]["priority"], "CRITICAL")
        self.assertNotIn("closedAt", picked[0])


class NewrelicGraphQLError(unittest.TestCase):
    """GraphQL-level errors (HTTP 200 but errors array)."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_graphql_error_raises_api_error(self):
        """A NerdGraph errors array (HTTP 200) is raised as an ApiError, not silently ignored."""
        responses.add(responses.POST, GRAPHQL_URL, json=_GQL_ERROR_RESPONSE, status=200)

        with self.assertRaises(api.ApiError) as ctx:
            nr._gql("{ actor { user { email } } }")

        self.assertIn("NerdGraph error", str(ctx.exception))
        self.assertIn("not authorized", str(ctx.exception))


class NewrelicCLI(unittest.TestCase):
    """CLI drive via main([...])."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()
        self._saved = os.environ.get("RC_CONN_NEWRELIC")
        os.environ["RC_CONN_NEWRELIC"] = "NRAK" "_test_key_for_unit_tests"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_NEWRELIC", None)
        else:
            os.environ["RC_CONN_NEWRELIC"] = self._saved

    @responses.activate
    def test_cli_entities(self):
        """CLI `entities` command fetches entities and prints JSON."""
        responses.add(responses.POST, GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = nr.main(["entities", "--query", "type = 'APPLICATION'", "--pick", "name,alertSeverity"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertFalse(out["incomplete"])
        self.assertEqual(out["items"][0]["name"], "background-worker")
        self.assertEqual(out["items"][0]["alertSeverity"], "NOT_ALERTING")

    @responses.activate
    def test_cli_nrql(self):
        """CLI `nrql` command runs a NRQL query and prints the results list as JSON."""
        responses.add(responses.POST, GRAPHQL_URL, json=_NRQL_RESULT, status=200)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = nr.main([
                "nrql",
                str(ACCOUNT_ID),
                "SELECT count(*) FROM TransactionError SINCE 1 HOUR AGO FACET error.class",
                "--pick",
                "count,error.class",
            ])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        rows = json.loads(captured.getvalue())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["count"], 42)

    @responses.activate
    def test_cli_violations(self):
        """CLI `violations` command fetches violations and prints JSON."""
        responses.add(responses.POST, GRAPHQL_URL, json=_VIOLATIONS_RESULT, status=200)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = nr.main(["violations", str(ACCOUNT_ID), "--pick", "label,severity"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertFalse(out["incomplete"])
        self.assertEqual(out["items"][0]["severity"], "CRITICAL")

    @responses.activate
    def test_cli_incidents(self):
        """CLI `incidents` command fetches incidents and prints JSON."""
        responses.add(responses.POST, GRAPHQL_URL, json=_INCIDENTS_RESULT, status=200)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = nr.main(["incidents", str(ACCOUNT_ID), "--pick", "title,priority,state"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertFalse(out["incomplete"])
        self.assertEqual(out["items"][0]["priority"], "CRITICAL")

    @responses.activate
    def test_cli_eu_flag_routes_to_eu_endpoint(self):
        """--eu flag sends request to the EU NerdGraph endpoint."""
        responses.add(responses.POST, EU_GRAPHQL_URL, json=_ENTITIES_PAGE_2, status=200)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = nr.main(["--eu", "entities"])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0)
        self.assertEqual(len(responses.calls), 1)
        self.assertIn("api.eu.newrelic.com", responses.calls[0].request.url)


class NewrelicCassetteHygiene(unittest.TestCase):
    """CI guard: no real New Relic API key prefix may land in connector dir files.

    Scoped to the connector directory ONLY — this test file legitimately names the prefixes
    it hunts for (split with concatenation to avoid self-triggering).
    """

    # New Relic User API key prefix is "NRAK-". Split so the guard doesn't flag this test file.
    _TOKEN_PREFIXES = ("NRAK" "-",)

    def test_no_token_prefixes_in_newrelic_connector_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "newrelic"
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
