"""Pure-logic tests for the grounding lib — DSN resolution, rendering, time windows, Insights
polling. No real DB/AWS: psycopg/boto3 are imported lazily, so these run on a bare host with

    cd runtime && uv run --no-project python -m unittest discover -s tests

The live paths (real query / real Insights call) are covered by the host's Go workspace smoke
tests; here we lock down the logic that decides *what* gets run.
"""

import io
import json
import os
import re
import sys
import unittest
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import _output, cloudwatch, db, oauth  # noqa: E402
from lib.connectors import cloudwatch as cloudwatch_connector, sentry  # noqa: E402
from lib import stripe as lib_stripe  # noqa: E402


_DSN_ENV_KEYS = ("PG_DSN", "DATABASE_URL", "RC_DB_DEFAULT")


class DSNResolution(unittest.TestCase):
    def setUp(self):
        self._clean = {
            k: v for k, v in os.environ.items() if k.endswith("_DSN") or k in _DSN_ENV_KEYS
        }
        for k in self._clean:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.endswith("_DSN") or k in _DSN_ENV_KEYS:
                del os.environ[k]
        os.environ.update(self._clean)

    def test_databases_excludes_host_store(self):
        os.environ["DATABASE_URL"] = "postgres://host/ops"
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db.databases(), ["MOMENTUM_POWERTOOLS_DSN", "MOMENTUM_RUBY_DSN"])

    def test_raw_dsn_passthrough(self):
        self.assertEqual(db._resolve_dsn("postgres://x/y"), "postgres://x/y")

    def test_exact_env_name(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn("MOMENTUM_RUBY_DSN"), "postgres://ruby")

    def test_short_name_suffix_match(self):
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn("powertools"), "postgres://pt")
        self.assertEqual(db._resolve_dsn("ruby"), "postgres://ruby")

    def test_ambiguous_short_name_raises(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://a"
        os.environ["OTHER_RUBY_DSN"] = "postgres://b"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn("ruby")

    def test_exact_short_name_wins_over_substring(self):
        # "elsa" must bind the EXACT elsa database, never the longer ELSA_REPLICA by substring.
        os.environ["MOMENTUM_ELSA_DSN"] = "postgres://elsa"
        os.environ["MOMENTUM_ELSA_REPLICA_DSN"] = "postgres://replica"
        self.assertEqual(db._resolve_dsn("elsa"), "postgres://elsa")

    def test_substring_fallback_warns(self):
        # No exact match for "elsa"; only a substring (ELSA_REPLICA) — resolve but warn loudly so a
        # silently-wrong DB binding can't happen unnoticed.
        os.environ["MOMENTUM_ELSA_REPLICA_DSN"] = "postgres://replica"
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(db._resolve_dsn("elsa"), "postgres://replica")
        self.assertTrue(any("substring" in str(w.message) for w in caught))

    def test_ambiguous_substring_raises(self):
        # "elsa" substring-matches two DSNs and no exact name — raise rather than guess.
        os.environ["MOMENTUM_ELSA_REPLICA_DSN"] = "postgres://a"
        os.environ["OTHER_ELSA_SHARD_DSN"] = "postgres://b"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn("elsa")

    def test_unknown_name_raises(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn("elsa")

    def test_default_pg_dsn(self):
        os.environ["PG_DSN"] = "postgres://default"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn(None), "postgres://default")

    def test_default_single_database(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        self.assertEqual(db._resolve_dsn(None), "postgres://ruby")

    def test_default_none_configured_raises(self):
        with self.assertRaises(RuntimeError):
            db._resolve_dsn(None)

    def test_default_multiple_requires_pick(self):
        os.environ["A_DSN"] = "postgres://a"
        os.environ["B_DSN"] = "postgres://b"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn(None)

    def test_default_multiple_uses_standard(self):
        # Multiple DBs, no db=, but a STANDARD is set (RC_DB_DEFAULT) → resolve to it instead of raising.
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["RC_DB_DEFAULT"] = "MOMENTUM_RUBY_DSN"
        self.assertEqual(db._resolve_dsn(None), "postgres://ruby")

    def test_default_standard_stale_falls_through_to_raise(self):
        # RC_DB_DEFAULT names a DSN that isn't present this run (tenant overlay / stale) → don't point at a
        # missing DB; raise the normal multi-DB "pass db=" error.
        os.environ["A_DSN"] = "postgres://a"
        os.environ["B_DSN"] = "postgres://b"
        os.environ["RC_DB_DEFAULT"] = "GONE_DSN"
        with self.assertRaises(RuntimeError):
            db._resolve_dsn(None)

    def test_pg_dsn_wins_over_standard(self):
        # PG_DSN is the explicit single-DSN override; it precedes the standard fallback.
        os.environ["PG_DSN"] = "postgres://pg"
        os.environ["A_DSN"] = "postgres://a"
        os.environ["B_DSN"] = "postgres://b"
        os.environ["RC_DB_DEFAULT"] = "A_DSN"
        self.assertEqual(db._resolve_dsn(None), "postgres://pg")

    def test_defaulted_to_standard_only_when_omitted_on_multi_db(self):
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["RC_DB_DEFAULT"] = "MOMENTUM_RUBY_DSN"
        # db omitted on a multi-DB run → the standard's short name (drives the table-not-found message).
        self.assertEqual(db._defaulted_to_standard(None), "ruby")
        # db EXPLICITLY passed → not "defaulted", even to the same DB.
        self.assertIsNone(db._defaulted_to_standard("ruby"))
        # No standard set → nothing defaulted.
        del os.environ["RC_DB_DEFAULT"]
        self.assertIsNone(db._defaulted_to_standard(None))


class DatabaseCatalog(unittest.TestCase):
    """Listing + error messages must surface the valid SHORT names and each DB's purpose
    (RC_DB_DESCRIPTIONS), so a weak model learns which DB is which at the moment of confusion."""

    def setUp(self):
        self._clean = {
            k: v
            for k, v in os.environ.items()
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL", "RC_DB_DESCRIPTIONS")
        }
        for k in self._clean:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL", "RC_DB_DESCRIPTIONS"):
                del os.environ[k]
        os.environ.update(self._clean)

    def test_descriptions_parses_valid_json(self):
        os.environ["RC_DB_DESCRIPTIONS"] = (
            '{"MOMENTUM_RUBY_DSN":"Subscriptions and plans.","MOMENTUM_POWERTOOLS_DSN":"Credits metering."}'
        )
        self.assertEqual(
            db._descriptions(),
            {
                "MOMENTUM_RUBY_DSN": "Subscriptions and plans.",
                "MOMENTUM_POWERTOOLS_DSN": "Credits metering.",
            },
        )

    def test_descriptions_absent_blank_malformed_return_empty(self):
        self.assertEqual(db._descriptions(), {})  # absent
        os.environ["RC_DB_DESCRIPTIONS"] = "   "
        self.assertEqual(db._descriptions(), {})  # blank
        os.environ["RC_DB_DESCRIPTIONS"] = "{not json"
        self.assertEqual(db._descriptions(), {})  # malformed
        os.environ["RC_DB_DESCRIPTIONS"] = "[1, 2, 3]"
        self.assertEqual(db._descriptions(), {})  # valid JSON, wrong shape

    def test_format_catalog_includes_description_and_omits_dash_when_absent(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["RC_DB_DESCRIPTIONS"] = '{"MOMENTUM_RUBY_DSN":"Subscriptions, plans, customers."}'
        cat = db._format_catalog()
        # ruby has a description → the em-dash + text is present on its line.
        ruby_line = next(ln for ln in cat.splitlines() if "MOMENTUM_RUBY_DSN" in ln)
        self.assertIn("ruby", ruby_line)
        self.assertIn("— Subscriptions, plans, customers.", ruby_line)
        # powertools has none → no dash on its line.
        pt_line = next(ln for ln in cat.splitlines() if "MOMENTUM_POWERTOOLS_DSN" in ln)
        self.assertIn("powertools", pt_line)
        self.assertNotIn("—", pt_line)

    def test_format_catalog_none_configured(self):
        self.assertEqual(db._format_catalog(), "  (none configured)")

    def test_bad_db_lists_short_names_and_descriptions(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["RC_DB_DESCRIPTIONS"] = (
            '{"MOMENTUM_RUBY_DSN":"Subscriptions and plans.","MOMENTUM_POWERTOOLS_DSN":"Credits metering."}'
        )
        with self.assertRaises(RuntimeError) as cm:
            db._resolve_dsn("nope")
        msg = str(cm.exception)
        self.assertIn("ruby", msg)
        self.assertIn("powertools", msg)
        self.assertIn("Subscriptions and plans.", msg)

    def test_multi_db_no_pick_lists_catalog_and_does_not_guess(self):
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["RC_DB_DESCRIPTIONS"] = (
            '{"MOMENTUM_RUBY_DSN":"Subscriptions and plans.","MOMENTUM_POWERTOOLS_DSN":"Credits metering."}'
        )
        with self.assertRaises(RuntimeError) as cm:
            db._resolve_dsn(None)  # no db=, several *_DSN, no PG_DSN → must raise, never auto-pick
        msg = str(cm.exception)
        self.assertIn("ruby", msg)
        self.assertIn("powertools", msg)
        self.assertIn("Credits metering.", msg)
        # Must not silently bind one of the DSNs.
        self.assertNotIn("postgres://ruby", msg)
        self.assertNotIn("postgres://pt", msg)


class OAuthConnections(unittest.TestCase):
    def setUp(self):
        self._clean = {k: v for k, v in os.environ.items() if k.startswith("RC_CONN_")}
        for k in self._clean:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("RC_CONN_"):
                del os.environ[k]
        os.environ.update(self._clean)

    def test_env_var_maps_connector_key(self):
        self.assertEqual(oauth.env_var("sentry"), "RC_CONN_SENTRY")
        self.assertEqual(oauth.env_var("hubspot-eu"), "RC_CONN_HUBSPOT_EU")
        self.assertEqual(oauth.env_var("RC_CONN_CUSTOM"), "RC_CONN_CUSTOM")

    def test_token_reads_injected_env(self):
        os.environ["RC_CONN_SENTRY"] = "secret-token"
        self.assertEqual(oauth.token("sentry"), "secret-token")

    def test_token_missing_raises_named_env(self):
        with self.assertRaisesRegex(RuntimeError, "RC_CONN_SENTRY"):
            oauth.token("sentry")


class SentryConnector(unittest.TestCase):
    def test_issue_to_markdown(self):
        md = sentry.issue_to_markdown(
            {
                "title": "Checkout failed",
                "shortId": "SHOP-1",
                "status": "unresolved",
                "count": "42",
                "permalink": "https://sentry.io/issues/1/",
            }
        )
        self.assertIn("# Checkout failed", md)
        self.assertIn("Short ID: SHOP-1", md)
        self.assertIn("Events: 42", md)


class Introspection(unittest.TestCase):
    """columns / tables_with_column build the right information_schema query and pass db= through."""

    def setUp(self):
        self._clean = {
            k: v
            for k, v in os.environ.items()
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL", "RC_DB_EXCLUDED_COLUMNS")
        }
        for k in self._clean:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL", "RC_DB_EXCLUDED_COLUMNS"):
                del os.environ[k]
        os.environ.update(self._clean)

    def test_columns_delegates(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.columns("users", db="ruby")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("information_schema.columns", sql)
        self.assertIn("table_name = %s", sql)
        # Default schema is NULL → coalesced to current_schema() so a scoped run sees its scope_<id>
        # views (public is revoked there); a flat run still resolves to public.
        self.assertIn("current_schema()", sql)
        self.assertEqual(params, [None, "users"])
        self.assertEqual(q.call_args.kwargs["db"], "ruby")

    def test_columns_explicit_schema_overrides(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.columns("users", schema="public", db="ruby")
        self.assertEqual(q.call_args.args[1], ["public", "users"])

    def test_tables_with_column_delegates(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.tables_with_column("%email%", schema="app", db="powertools")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("column_name ilike %s", sql)
        self.assertEqual(params, ["app", "%email%"])
        self.assertEqual(q.call_args.kwargs["db"], "powertools")

    def test_tables_with_column_default_schema_is_current(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.tables_with_column("%email%", db="powertools")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("current_schema()", sql)
        self.assertEqual(params, [None, "%email%"])

    def test_tables_delegates(self):
        with mock.patch.object(db, "query", return_value=[]) as q:
            db.tables(db="powertools")
        sql, params = q.call_args.args[0], q.call_args.args[1]
        self.assertIn("information_schema.tables", sql)
        self.assertIn("current_schema()", sql)
        self.assertEqual(params, [None])
        self.assertEqual(q.call_args.kwargs["db"], "powertools")

    def test_common_db_aliases(self):
        self.assertIs(db.sql, db.query)
        self.assertIs(db.select, db.query)
        self.assertIs(db.one, db.query_one)
        self.assertIs(db.first, db.query_one)
        self.assertIs(db.list_databases, db.databases)
        self.assertIs(db.database_names, db.databases)
        self.assertIs(db.list_tables, db.tables)
        self.assertIs(db.table_names, db.tables)
        self.assertIs(db.schema, db.columns)
        self.assertIs(db.describe_table, db.columns)
        self.assertIs(db.table_info, db.columns)
        self.assertIs(db.find_columns, db.tables_with_column)
        self.assertIs(db.tables_by_column, db.tables_with_column)

    def test_columns_warns_about_hidden_and_allowlisted_columns(self):
        os.environ["APP_DSN"] = "postgres://app"
        os.environ["RC_DB_EXCLUDED_COLUMNS"] = (
            '{"APP_DSN":{"tables":{"admins":{"include":["id","tenant_id"]},'
            '"mail_senders":{"exclude":["smtp_password"]}}}}'
        )
        with mock.patch.object(db, "query", return_value=[]), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            db.columns("admins", db="app")
        messages = [str(w.message) for w in caught]
        self.assertTrue(any("admins shows an allowlisted subset" in m for m in messages))
        self.assertFalse(any("smtp_password" in m for m in messages))

    def test_tables_with_column_warns_about_hidden_pattern_matches_and_allowlists(self):
        os.environ["APP_DSN"] = "postgres://app"
        os.environ["RC_DB_EXCLUDED_COLUMNS"] = (
            '{"APP_DSN":{"tables":{"admins":{"include":["id","tenant_id"]},'
            '"mail_senders":{"exclude":["encrypted_password","smtp_password"]}}}}'
        )
        with mock.patch.object(db, "query", return_value=[]), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            db.tables_with_column("%password%", db="app")
        messages = [str(w.message) for w in caught]
        self.assertTrue(any("mail_senders.encrypted_password" in m for m in messages))
        self.assertTrue(any("mail_senders.smtp_password" in m for m in messages))
        self.assertTrue(any("admins shows an allowlisted subset" in m for m in messages))


class ColumnShape(unittest.TestCase):
    """Introspection results must survive every intuitive access pattern (no DB needed)."""

    def _cols(self):
        with mock.patch.object(
            db,
            "query",
            return_value=[
                {"column_name": "id", "data_type": "uuid"},
                {"column_name": "email", "data_type": "character varying"},
            ],
        ):
            return db.columns("people")

    def test_element_is_the_column_name(self):
        c = self._cols()[1]
        self.assertEqual(c, "email")
        self.assertEqual(str(c), "email")
        self.assertEqual(c.upper(), "EMAIL")  # str methods still work
        self.assertEqual(f"select {c} from people", "select email from people")

    def test_element_mapping_and_attribute_access(self):
        c = self._cols()[1]
        for key in ("name", "column", "column_name"):
            self.assertEqual(c[key], "email")
        for key in ("type", "dtype", "data_type"):
            self.assertEqual(c[key], "character varying")
        self.assertEqual(c.name, "email")
        self.assertEqual(c.type, "character varying")
        self.assertEqual(c.table, "people")
        self.assertEqual(c["table_name"], "people")
        self.assertEqual(c.get("nope", "fallback"), "fallback")
        self.assertEqual(c[0], "e")  # int indexing stays str behaviour
        self.assertIn("column_name", c)  # dict mental model
        self.assertIn("mai", c)  # str mental model

    def test_unknown_key_error_lists_valid_ones(self):
        with self.assertRaises(KeyError) as ctx:
            self._cols()[0]["kolom"]
        self.assertIn("column_name", str(ctx.exception))

    def test_dict_and_json_round_trip(self):
        import json

        from lib import _output

        cols = self._cols()
        self.assertEqual(dict(cols[0]), {"name": "id", "type": "uuid", "table": "people"})
        self.assertEqual(cols.to_dicts()[0]["name"], "id")
        self.assertEqual(json.loads(_output.render(cols, "json"))[0]["type"], "uuid")
        self.assertIn("name,type,table", _output.render(cols, "csv"))

    def test_list_behaves_like_a_mapping_of_names(self):
        cols = self._cols()
        self.assertEqual(cols.keys(), ["id", "email"])
        self.assertEqual(cols.names(), ["id", "email"])
        self.assertIn("email", cols)
        self.assertIn("EMAIL", cols)  # case-insensitive name membership
        self.assertNotIn("nope", cols)
        self.assertEqual(cols["email"].type, "character varying")
        self.assertIsNone(cols.get("nope"))
        self.assertEqual(cols.types(), {"id": "uuid", "email": "character varying"})
        self.assertEqual(cols[0], "id")  # positional access preserved
        self.assertIsInstance(cols[:1], db.ColumnList)
        with self.assertRaises(KeyError) as ctx:
            cols["nope"]
        self.assertIn("available: id, email", str(ctx.exception))

    def test_repr_is_readable(self):
        text = repr(self._cols())
        self.assertIn("2 columns:", text)
        self.assertIn("people.email", text)
        self.assertIn("character varying", text)
        self.assertIn("(no columns", repr(db.ColumnList()))

    def test_tables_with_column_carries_the_table(self):
        with mock.patch.object(
            db,
            "query",
            return_value=[{"table_name": "people", "column_name": "email", "data_type": "text"}],
        ):
            hits = db.tables_with_column("%email%")
        self.assertEqual(hits[0], "email")
        self.assertEqual(hits[0].table, "people")
        self.assertEqual(hits[0]["table_name"], "people")
        self.assertEqual(hits[0].qualified, "people.email")


class UndefinedHint(unittest.TestCase):
    """_undefined_hint turns a projected-away column into actionable guidance, not a rewrite-from-scratch."""

    def test_points_at_columns_helper_and_scoping(self):
        msg = db._undefined_hint(Exception("boom"))  # no .diag → just the guidance
        self.assertIn("lib.db.columns('<table>')", msg)
        self.assertIn("data-scoping", msg)
        self.assertIn("no need to rewrite", msg)

    def test_prepends_postgres_hint_when_present(self):
        diag = mock.Mock(message_hint="Perhaps you meant to reference the column \"x.email\".")
        msg = db._undefined_hint(mock.Mock(diag=diag))
        self.assertTrue(msg.startswith("Perhaps you meant"))
        self.assertIn("lib.db.columns", msg)  # guidance still appended

    def test_missing_pg_hint_is_omitted(self):
        diag = mock.Mock(message_hint=None)
        msg = db._undefined_hint(mock.Mock(diag=diag))
        self.assertNotIn("None", msg)


class QueryPlaceholderHandling(unittest.TestCase):
    """`query` must send a literal `%` verbatim (the `ILIKE 'avo%'` wildcard footgun): pass None to
    psycopg when there are no params, a sequence only when there are. Mocks the lazily-imported
    psycopg so no real DB is needed."""

    def _fake_psycopg(self, calls):
        cur = mock.MagicMock()
        cur.description = None  # short-circuit before array handling
        cur.execute.side_effect = lambda *a: calls.append(a)
        cur.__enter__ = lambda s: cur
        cur.__exit__ = lambda *a: False
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: conn
        conn.__exit__ = lambda *a: False
        fake = mock.MagicMock()
        fake.connect.return_value = conn
        return fake

    def _run(self, sql, params):
        calls = []
        with mock.patch.dict(os.environ, {"PG_DSN": "postgres://x/y"}), mock.patch.dict(
            sys.modules, {"psycopg": self._fake_psycopg(calls)}
        ):
            db.query(sql, params)
        # calls[0] is the SET LOCAL statement_timeout; calls[-1] is the user query.
        return calls[-1]

    def test_no_params_passes_none_so_literal_percent_survives(self):
        sql, passed = self._run("SELECT 1 WHERE table_name ILIKE 'avo%'", None)
        self.assertEqual(sql, "SELECT 1 WHERE table_name ILIKE 'avo%'")
        self.assertIsNone(passed)  # None, NOT [] — else psycopg rejects the literal %

    def test_empty_params_also_passes_none(self):
        _, passed = self._run("SELECT 1 WHERE x ILIKE 'a%'", [])
        self.assertIsNone(passed)

    def test_real_params_are_passed_through(self):
        _, passed = self._run("SELECT 1 WHERE x ILIKE %s", ["%avo%"])
        self.assertEqual(passed, ["%avo%"])


class TableNotFoundMessage(unittest.TestCase):
    """A table-not-found on a multi-DB run where db= was OMITTED explains 'wrong database' — names the
    standard that was used + the alternatives — so the agent re-runs with db= instead of rewriting."""

    def setUp(self):
        self._clean = {
            k: v
            for k, v in os.environ.items()
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL", "RC_DB_DEFAULT", "RC_DB_DESCRIPTIONS")
        }
        for k in self._clean:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.endswith("_DSN") or k in ("PG_DSN", "DATABASE_URL", "RC_DB_DEFAULT", "RC_DB_DESCRIPTIONS"):
                del os.environ[k]
        os.environ.update(self._clean)

    def _fake_psycopg(self):
        """A psycopg double whose cursor.execute raises a REAL UndefinedTable for the USER query (so the
        except clause, which catches the concrete class, fires) and whose errors.* are real classes.
        The timeout SET LOCALs query() always emits first must pass through untouched."""

        class _UndefinedTable(Exception):
            diag = mock.Mock(message_hint=None)

        class _UndefinedColumn(Exception):
            diag = mock.Mock(message_hint=None)

        def execute(sql, *_a, **_k):
            if sql.lstrip().upper().startswith("SET"):
                return None
            raise _UndefinedTable('relation "accounts" does not exist')

        cur = mock.MagicMock()
        cur.execute.side_effect = execute
        cur.__enter__ = lambda s: cur
        cur.__exit__ = lambda *a: False
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: conn
        conn.__exit__ = lambda *a: False
        fake = mock.MagicMock()
        fake.connect.return_value = conn
        fake.errors.UndefinedTable = _UndefinedTable
        fake.errors.UndefinedColumn = _UndefinedColumn
        return fake

    def test_omitted_db_on_multi_db_names_standard_and_alternatives(self):
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["RC_DB_DEFAULT"] = "MOMENTUM_RUBY_DSN"
        os.environ["RC_DB_DESCRIPTIONS"] = '{"MOMENTUM_POWERTOOLS_DSN":"Credits / usage."}'
        with mock.patch.dict(sys.modules, {"psycopg": self._fake_psycopg()}):
            with self.assertRaises(RuntimeError) as ctx:
                db.query("select * from accounts")  # no db= → defaults to the standard
        msg = str(ctx.exception)
        self.assertIn("No db= was passed", msg)
        self.assertIn("'ruby'", msg)  # the standard that was used
        self.assertIn("powertools", msg)  # the alternative it should try
        self.assertIn('relation "accounts" does not exist', msg)  # original error preserved

    def test_explicit_db_keeps_the_generic_scoping_hint(self):
        # db= was passed explicitly → not a "wrong default DB" situation; the scoping/typo hint applies.
        os.environ["MOMENTUM_POWERTOOLS_DSN"] = "postgres://pt"
        os.environ["MOMENTUM_RUBY_DSN"] = "postgres://ruby"
        os.environ["RC_DB_DEFAULT"] = "MOMENTUM_RUBY_DSN"
        with mock.patch.dict(sys.modules, {"psycopg": self._fake_psycopg()}):
            with self.assertRaises(RuntimeError) as ctx:
                db.query("select * from accounts", db="ruby")
        msg = str(ctx.exception)
        self.assertNotIn("No db= was passed", msg)
        self.assertIn("lib.db.columns", msg)  # the generic undefined hint


class MistakeHintRouting(unittest.TestCase):
    """`_mistake_hint` maps a Postgres message shape to ONE corrective hint. Message-shape matching (not
    exception classes) is what keeps these unit-testable without a driver — so test them that way."""

    def _hint(self, msg):
        # conn=None: these classes never introspect (the enum class is covered through query()).
        return db._mistake_hint(Exception(msg), None, 30_000)

    def test_enum_array_operator_points_at_any(self):
        hint = self._hint("operator does not exist: enum_role[] && text[]")
        self.assertIn("= ANY(col)", hint)
        self.assertIn("['child']", hint)

    def test_json_operator_points_at_text_extraction(self):
        hint = self._hint("operator does not exist: jsonb = text")
        self.assertIn("col->>'key'", hint)
        self.assertIn("col::text ILIKE %s", hint)

    def test_plain_type_mismatch_points_at_a_cast(self):
        hint = self._hint("operator does not exist: character varying = uuid")
        self.assertIn("lib.db.columns('<table>')", hint)
        self.assertIn("%s::uuid", hint)
        self.assertNotIn("ANY(col)", hint)  # not the array branch

    def test_malformed_array_literal_points_at_any_or_a_list(self):
        hint = self._hint('malformed array literal: "child"')
        self.assertIn("= ANY(col)", hint)
        self.assertIn("Python list", hint)

    def test_ambiguous_column_says_qualify(self):
        hint = self._hint('column reference "id" is ambiguous')
        self.assertIn("table.column", hint)

    def test_placeholder_mix_explains_the_percent(self):
        hint = self._hint("only '%s', '%b', '%t' are allowed as placeholders, got 'a'")
        self.assertIn("ILIKE %s", hint)
        self.assertIn("%%", hint)

    def test_unknown_error_gets_no_hint(self):
        self.assertIsNone(self._hint("deadlock detected"))

    def test_enum_type_name_must_be_an_identifier(self):
        # The type name arrives from a PG error message ⇒ untrusted. A non-identifier is refused
        # BEFORE anything is interpolated into SQL; the connection is never touched.
        conn = mock.MagicMock()
        self.assertIsNone(db._enum_labels(conn, 30_000, "role'; drop table x --"))
        conn.rollback.assert_not_called()
        conn.cursor.assert_not_called()


class EnumLabelHint(unittest.TestCase):
    """An invented enum label is the single most common agent SQL mistake, so `query` answers it with the
    database's OWN labels — fetched on the same connection after rolling back the aborted transaction."""

    def _fake_psycopg(self, labels=None, introspect_raises=False, events=None):
        """psycopg double: SET LOCALs pass, the user query raises a real 22P02-shaped error, and the
        follow-up enum_range introspection either returns `labels` or blows up."""
        events = events if events is not None else []

        class _Error(Exception):
            pass

        class _UndefinedTable(_Error):
            diag = mock.Mock(message_hint=None)

        class _UndefinedColumn(_Error):
            diag = mock.Mock(message_hint=None)

        state = {"rows": []}

        def execute(sql, *_a, **_k):
            events.append(sql)
            if sql.lstrip().upper().startswith("SET"):
                return None
            if "enum_range" in sql:
                if introspect_raises:
                    raise _Error("current transaction is aborted")
                state["rows"] = [(lbl,) for lbl in labels or []]
                return None
            raise _Error('invalid input value for enum enum_role: "child_care"')

        cur = mock.MagicMock()
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: state["rows"]
        cur.__enter__ = lambda s: cur
        cur.__exit__ = lambda *a: False
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        conn.rollback.side_effect = lambda: events.append("ROLLBACK")
        conn.__enter__ = lambda s: conn
        conn.__exit__ = lambda *a: False
        fake = mock.MagicMock()
        fake.connect.return_value = conn
        fake.Error = _Error
        fake.errors.UndefinedTable = _UndefinedTable
        fake.errors.UndefinedColumn = _UndefinedColumn
        return fake

    def _run(self, fake):
        with mock.patch.dict(os.environ, {"PG_DSN": "postgres://x/y"}), mock.patch.dict(
            sys.modules, {"psycopg": fake}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                db.query("select * from people where role = 'child_care'")
        return str(ctx.exception)

    def test_lists_real_labels_and_the_closest_match(self):
        events = []
        msg = self._run(self._fake_psycopg(labels=["parent", "child-care", "admin"], events=events))
        self.assertIn('invalid input value for enum enum_role: "child_care"', msg)  # original kept
        self.assertIn("\n\n", msg)  # original + blank line + hint, same shape as the other hints
        self.assertIn("Valid: parent, child-care, admin", msg)
        self.assertIn("Closest: 'child-care'", msg)
        # The failed statement aborted the txn: rollback MUST precede the introspection round trip.
        introspect = next(i for i, e in enumerate(events) if "enum_range" in e)
        self.assertIn("ROLLBACK", events[:introspect])
        self.assertTrue(events[introspect - 1].lstrip().upper().startswith("SET"))  # timeout re-emitted

    def test_failed_introspection_degrades_to_the_query_to_run(self):
        msg = self._run(self._fake_psycopg(introspect_raises=True))
        self.assertIn("SELECT unnest(enum_range(NULL::enum_role))", msg)
        self.assertNotIn("Valid:", msg)

    def test_placeholder_mix_is_wrapped_through_query(self):
        # Client-side ProgrammingError (no sqlstate) — still a psycopg.Error, so one clause covers it.
        fake = self._fake_psycopg()
        fake.connect.return_value.cursor.return_value.execute.side_effect = (
            lambda sql, *a, **k: None
            if sql.lstrip().upper().startswith("SET")
            else (_ for _ in ()).throw(fake.Error("only '%s', '%b', '%t' are allowed as placeholders"))
        )
        with mock.patch.dict(os.environ, {"PG_DSN": "postgres://x/y"}), mock.patch.dict(
            sys.modules, {"psycopg": fake}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                db.query("select 1 where x ilike 'a%' and y = %s", ["z"])
        self.assertIn("cannot be mixed with %s params", str(ctx.exception))

    def test_unrecognised_error_re_raises_unchanged(self):
        fake = self._fake_psycopg()
        fake.connect.return_value.cursor.return_value.execute.side_effect = (
            lambda sql, *a, **k: None
            if sql.lstrip().upper().startswith("SET")
            else (_ for _ in ()).throw(fake.Error("deadlock detected"))
        )
        with mock.patch.dict(os.environ, {"PG_DSN": "postgres://x/y"}), mock.patch.dict(
            sys.modules, {"psycopg": fake}
        ):
            with self.assertRaises(fake.Error) as ctx:  # NOT wrapped in RuntimeError
                db.query("select 1")
        self.assertEqual(str(ctx.exception), "deadlock detected")


class ExcludedColumnHeal(unittest.TestCase):
    """_strip_excluded auto-drops manifest-hidden SELECT columns on the simple shape, leaves typos and
    non-simple queries alone, and never empties the SELECT."""

    EMAP = {
        "tables": {
            "mail_senders": {"exclude": ["smtp_password"]},
            "admins": {"include": ["id", "tenant_id"]},
        },
    }

    def test_drops_table_excluded_column(self):
        sql, dropped = db._strip_excluded(
            "SELECT id, smtp_password, host FROM mail_senders WHERE id = 5", self.EMAP
        )
        self.assertEqual(dropped, ["smtp_password"])
        self.assertEqual(sql, "SELECT id, host FROM mail_senders WHERE id = 5")

    def test_whitelist_drops_non_included(self):
        # admins is include-only [id, tenant_id]; `email` isn't whitelisted ⇒ hidden ⇒ dropped.
        sql, dropped = db._strip_excluded("SELECT id, email FROM admins", self.EMAP)
        self.assertEqual(dropped, ["email"])
        self.assertEqual(sql, "SELECT id FROM admins")

    def test_typo_is_left_for_postgres(self):
        # `hostt` is not manifest-hidden ⇒ NOT dropped (Postgres will raise → enriched hint).
        sql, dropped = db._strip_excluded("SELECT id, hostt FROM mail_senders", self.EMAP)
        self.assertEqual(dropped, [])
        self.assertEqual(sql, "SELECT id, hostt FROM mail_senders")

    def test_qualified_column_dropped(self):
        sql, dropped = db._strip_excluded("SELECT m.id, m.smtp_password FROM mail_senders m", self.EMAP)
        self.assertEqual(dropped, ["smtp_password"])
        self.assertEqual(sql, "SELECT m.id FROM mail_senders m")

    def test_select_star_not_touched(self):
        sql, dropped = db._strip_excluded("SELECT * FROM mail_senders", self.EMAP)
        self.assertEqual((sql, dropped), ("SELECT * FROM mail_senders", []))

    def test_join_not_touched(self):
        q = "SELECT a.smtp_password FROM mail_senders a JOIN admins b ON a.id = b.id"
        self.assertEqual(db._strip_excluded(q, self.EMAP), (q, []))

    def test_would_empty_select_not_healed(self):
        # Only column is hidden → stripping would leave an empty SELECT → bail (Postgres errors → hint).
        q = "SELECT smtp_password FROM mail_senders"
        self.assertEqual(db._strip_excluded(q, self.EMAP), (q, []))

    def test_no_map_is_noop(self):
        q = "SELECT id, smtp_password FROM mail_senders"
        self.assertEqual(db._strip_excluded(q, {}), (q, []))

    def test_aliased_expression_item_kept(self):
        # `smtp_password AS pw` is not a bare column ⇒ never auto-dropped (would change result shape).
        q = "SELECT id, smtp_password AS pw FROM mail_senders"
        self.assertEqual(db._strip_excluded(q, self.EMAP), (q, []))


class DurationParsing(unittest.TestCase):
    def test_units(self):
        self.assertEqual(db._parse_duration_ms("500ms"), 500)
        self.assertEqual(db._parse_duration_ms("30s"), 30_000)
        self.assertEqual(db._parse_duration_ms("2min"), 120_000)
        self.assertEqual(db._parse_duration_ms("1m"), 60_000)
        self.assertEqual(db._parse_duration_ms("1h"), 3_600_000)
        self.assertEqual(db._parse_duration_ms("5"), 5_000)  # bare = seconds


class Rendering(unittest.TestCase):
    rows = [{"id": 1, "email": "a@b.com", "meta": {"k": "v"}}, {"id": 2, "email": "c@d.com", "meta": None}]

    def _capture_emit_rows(self, rows, fmt="csv", label="t"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _output.emit_rows(rows, fmt, label=label)
        return buf.getvalue()

    def _spilled_path(self, preview: str) -> Path:
        m = re.search(r"saved to (?:remote workspace file )?(\S+) \(", preview)
        self.assertIsNotNone(m, preview)
        return Path(m.group(1))

    def test_csv(self):
        out = _output.render(self.rows, "csv")
        self.assertIn("id,email,meta", out)
        self.assertIn('"{""k"": ""v""}"', out)  # JSON-encoded dict cell, CSV-quoted

    def test_json_roundtrip(self):
        import json

        self.assertEqual(json.loads(_output.render(self.rows, "json")), self.rows)

    def test_table_has_header_and_separator(self):
        out = _output.render(self.rows, "table").splitlines()
        self.assertTrue(out[0].startswith("id"))
        self.assertIn("--", out[1])

    def test_empty(self):
        self.assertEqual(_output.render([], "csv"), "")

    def test_emit_rows_small_inline_no_spill(self):
        out = self._capture_emit_rows(self.rows, "csv")
        self.assertIn("id,email,meta", out)
        self.assertIn("a@b.com", out)
        self.assertNotIn("saved to", out)

    def test_emit_rows_large_spills_with_structural_preview(self):
        rows = [{"id": i, "email": f"user{i}@example.com", "payload": "x" * 300} for i in range(30)]
        rendered = _output.render(rows, "csv")
        self.assertGreater(len(rendered.encode()), _output.SPILL_BYTES)

        preview = self._capture_emit_rows(rows, "csv", label="rows")
        path = self._spilled_path(preview)

        self.assertIn("30 rows × 3 cols", preview)
        self.assertIn("columns: id, email, payload", preview)
        self.assertIn("remote workspace file", preview)
        self.assertIn("rp dev console file get", preview)
        self.assertIn("query it:", preview)
        self.assertIn("awk -F','", preview)
        self.assertLess(len(preview.encode()), 6000)
        self.assertEqual(path.suffix, ".csv")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes().decode("utf-8"), rendered)

    def test_spilled_file_is_complete_not_preview_sample(self):
        rows = [{"id": i, "payload": "x" * 300} for i in range(40)]
        preview = self._capture_emit_rows(rows, "csv", label="complete")
        path = self._spilled_path(preview)

        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), len(rows) + 1)

    def test_emit_rows_spills_between_new_and_old_thresholds(self):
        rows = [{"id": i, "payload": "x" * 225} for i in range(25)]
        rendered_bytes = len(_output.render(rows, "csv").encode())
        self.assertGreater(rendered_bytes, _output.SPILL_BYTES)
        self.assertLess(rendered_bytes, 50_000)

        preview = self._capture_emit_rows(rows, "csv", label="threshold")

        self.assertIn("saved to", preview)
        self.assertLess(len(preview.encode()), 6000)

    def test_wide_json_cell_hard_truncates_preview(self):
        rows = [{"id": 1, "payload": "x" * 20_000}]
        preview = self._capture_emit_rows(rows, "json", label="wide")

        self.assertIn("1 rows × 2 cols", preview)
        self.assertIn("jq '.[] | select(...)'", preview)
        self.assertIn("truncated", preview)
        self.assertLess(len(preview.encode()), 6000)

    def test_emit_text_small_and_large_back_compat(self):
        small = io.StringIO()
        with redirect_stdout(small):
            _output.emit("hello", label="text")
        self.assertEqual(small.getvalue(), "hello\n")

        large = io.StringIO()
        with redirect_stdout(large):
            _output.emit("x" * (_output.SPILL_BYTES + 1), label="text")
        self.assertIn("spilled to", large.getvalue())


class CloudWatchTimeRange(unittest.TestCase):
    def test_hours_lookback(self):
        with mock.patch.object(cloudwatch.time, "time", return_value=1_000_000):
            s, e = cloudwatch._time_range(hours=1, start=None, end=None)
            self.assertEqual(e, 1_000_000)
            self.assertEqual(s, 1_000_000 - 3600)

    def test_explicit_iso_window(self):
        s, e = cloudwatch._time_range(hours=24, start="2026-01-10 00:00:00", end="2026-01-10 01:00:00")
        self.assertEqual(e - s, 3600)


class CloudWatchCredentials(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "RC_CONN_CLOUDWATCH",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_REGION",
                "AWS_DEFAULT_REGION",
            )
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_structured_connection_credentials_win(self):
        os.environ["RC_CONN_CLOUDWATCH"] = json.dumps(
            {
                "access_key_id": "conn-key",
                "secret_access_key": "conn-secret",
                "region": "eu-west-1",
            }
        )
        os.environ["AWS_ACCESS_KEY_ID"] = "legacy-key"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "legacy-secret"
        os.environ["AWS_REGION"] = "eu-central-1"

        self.assertEqual(
            cloudwatch._credentials(),
            {
                "access_key_id": "conn-key",
                "secret_access_key": "conn-secret",
                "region": "eu-west-1",
            },
        )

    def test_legacy_aws_env_fallback(self):
        os.environ["AWS_ACCESS_KEY_ID"] = "legacy-key"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "legacy-secret"
        os.environ["AWS_REGION"] = "eu-central-1"

        self.assertEqual(cloudwatch._credentials()["region"], "eu-central-1")

    def test_missing_structured_field_names_missing_value(self):
        os.environ["RC_CONN_CLOUDWATCH"] = json.dumps({"access_key_id": "conn-key", "region": "eu-west-1"})

        with self.assertRaisesRegex(RuntimeError, "secret_access_key"):
            cloudwatch._credentials()

    def test_connector_reexports_cloudwatch_helpers(self):
        self.assertIs(cloudwatch_connector.insights, cloudwatch.insights)
        self.assertIs(cloudwatch_connector.search, cloudwatch.search)
        self.assertIs(cloudwatch_connector.query, cloudwatch.insights)
        self.assertIs(cloudwatch_connector.logs, cloudwatch.search)

    def test_common_cloudwatch_aliases(self):
        self.assertIs(cloudwatch.query, cloudwatch.insights)
        self.assertIs(cloudwatch.logs, cloudwatch.search)
        self.assertIs(cloudwatch.grep, cloudwatch.search)
        self.assertIs(cloudwatch.recent, cloudwatch.tail)
        self.assertIs(cloudwatch.groups, cloudwatch.log_groups)
        self.assertIs(cloudwatch.list_log_groups, cloudwatch.log_groups)
        self.assertIs(cloudwatch.filter_events, cloudwatch.filter_log_events)


class InsightsPolling(unittest.TestCase):
    def _fake_client(self, statuses, results):
        c = mock.Mock()
        c.start_query.return_value = {"queryId": "q1"}
        c.get_query_results.side_effect = [{"status": st, "results": results} for st in statuses]
        return c

    def test_polls_until_complete_and_drops_ptr(self):
        rows = [[{"field": "@message", "value": "hi"}, {"field": "@ptr", "value": "xyz"}]]
        client = self._fake_client(["Running", "Complete"], rows)
        with mock.patch.object(cloudwatch, "_client", return_value=client), mock.patch.object(
            cloudwatch.time, "sleep"
        ), mock.patch.object(cloudwatch.time, "time", return_value=0):
            out = cloudwatch.insights("fields @message", "/app", hours=1)
        self.assertEqual(out, [{"@message": "hi"}])

    def test_failed_status_raises(self):
        client = self._fake_client(["Failed"], [])
        with mock.patch.object(cloudwatch, "_client", return_value=client), mock.patch.object(
            cloudwatch.time, "sleep"
        ):
            with self.assertRaises(RuntimeError):
                cloudwatch.insights("q", "/app")


class CloudWatchPatternEscaping(unittest.TestCase):
    """search() must not let attacker-influenceable text break out of the /.../ regex literal."""

    def test_slash_is_escaped(self):
        # A bare "/" would close the literal early; it must be backslash-escaped.
        self.assertEqual(cloudwatch._escape_pattern("a/b"), "a\\/b")

    def test_backslash_is_escaped_first(self):
        # The escape char itself is doubled, and that happens before slashes so we don't double-escape.
        self.assertEqual(cloudwatch._escape_pattern("a\\b"), "a\\\\b")
        self.assertEqual(cloudwatch._escape_pattern("a\\/b"), "a\\\\\\/b")

    def test_control_chars_rejected(self):
        for bad in ("line1\nline2", "tab\there", "nul\x00here", "del\x7f"):
            with self.assertRaises(ValueError):
                cloudwatch._escape_pattern(bad)

    def test_search_interpolates_escaped_pattern(self):
        captured = {}

        def fake_insights(q, *a, **k):
            captured["q"] = q
            return []

        with mock.patch.object(cloudwatch, "insights", side_effect=fake_insights):
            cloudwatch.search("/app", "thread/id")
        # The slash from the pattern is escaped inside the filter clause, not a raw literal-closer.
        self.assertIn("filter @message like /thread\\/id/", captured["q"])

    def test_search_rejects_newline_pattern(self):
        with self.assertRaises(ValueError):
            cloudwatch.search("/app", "evil\n| stats count(*)")


class StripeKeyResolution(unittest.TestCase):
    """The lib must resolve the key from EITHER name a project may seal into its .env: the
    documented STRIPE_RESTRICTED_KEY or the in-the-field STRIPE_API_KEY (preferring the former)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("STRIPE_RESTRICTED_KEY", "STRIPE_API_KEY")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _resolve_key(self):
        """Run the audited REST client and return the credential it resolved."""
        return lib_stripe._client().credential

    def test_missing_key_raises_naming_both_vars(self):
        with self.assertRaises(RuntimeError) as cm:
            lib_stripe._client()
        msg = str(cm.exception)
        self.assertIn("STRIPE_RESTRICTED_KEY", msg)
        self.assertIn("STRIPE_API_KEY", msg)

    def test_restricted_key_initializes_client(self):
        # The documented onboarding name resolves.
        os.environ["STRIPE_RESTRICTED_KEY"] = "rk_test_dummy"
        self.assertEqual(self._resolve_key(), "rk_test_dummy")

    def test_api_key_fallback_initializes_client(self):
        # The in-the-field name (what momentum-tools seals) also resolves — this is the P2 fix.
        os.environ["STRIPE_API_KEY"] = "rk_live_field"
        self.assertEqual(self._resolve_key(), "rk_live_field")

    def test_restricted_key_wins_when_both_present(self):
        # When both are sealed, the documented standard takes precedence (stable, predictable).
        os.environ["STRIPE_RESTRICTED_KEY"] = "rk_restricted"
        os.environ["STRIPE_API_KEY"] = "rk_api"
        self.assertEqual(self._resolve_key(), "rk_restricted")


class PgArrayParsing(unittest.TestCase):
    """`_parse_pg_array` turns the raw literal psycopg leaves for unhandled (enum) array columns
    into a real list — the fix for `list("{parent}")` mangling roles into single characters."""

    def test_array_literals(self):
        cases = [
            ("{}", []),
            ("{parent}", ["parent"]),
            ("{parent,child}", ["parent", "child"]),
            ('{"parent"}', ["parent"]),
            ('{"a,b","x\\"y"}', ["a,b", 'x"y']),  # quoted comma + escaped quote
            ("{NULL,foo}", [None, "foo"]),  # unquoted NULL -> None
            ('{"NULL"}', ["NULL"]),  # quoted NULL stays a string
            ("{{1,2},{3,4}}", [["1", "2"], ["3", "4"]]),  # nested
        ]
        for literal, expected in cases:
            self.assertEqual(db._parse_pg_array(literal), expected, msg=literal)

    def test_non_array_returned_unchanged(self):
        # A real text value that merely contains braces is never an array literal here (the caller
        # also gates on the column's array OID), and round-trips untouched if passed in.
        self.assertEqual(db._parse_pg_array("not an array"), "not an array")


if __name__ == "__main__":
    unittest.main()
