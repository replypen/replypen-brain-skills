from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from lib import db


class _Cursor:
    description = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchall(self):
        return []


class _Connection:
    read_only = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor()


def test_query_sets_connect_timeout(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return _Connection()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("PG_DSN", "postgresql://example")

    assert db.query("select 1") == []
    assert calls[0][1]["connect_timeout"] == 15
    assert calls[0][1]["autocommit"] is False


def test_local_connect_failure_points_to_rc_primitives(monkeypatch):
    def connect(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    monkeypatch.setenv("RC_LOCAL_BRAIN_RUN", "1")

    with pytest.raises(RuntimeError) as excinfo:
        db.query("select 1")

    msg = str(excinfo.value)
    assert "connect_timeout=15s" in msg
    assert "local brain_run.py live check" in msg
    assert "rp dev console database" in msg
    assert "rp dev console bash run" in msg


def test_host_timeout_is_both_default_and_maximum(monkeypatch):
    monkeypatch.setenv("RC_DB_QUERY_TIMEOUT_SECONDS", "90")
    assert db._configured_timeout_limits() == (90_000, 90_000)
    monkeypatch.delenv("RC_DB_QUERY_TIMEOUT_SECONDS")
    assert db._configured_timeout_limits() == (30_000, 120_000)


# --- Postgres array literals -------------------------------------------------------------------
#
# psycopg hydrates arrays of element types it knows; enum arrays (per-database OIDs) arrive as the
# raw literal string, so lib.db parses those itself. Tests cover the grammar and — separately —
# the OID-driven DECISION to parse at all.


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("{}", []),
        ("{parent}", ["parent"]),
        ("{parent,child}", ["parent", "child"]),
        ('{"a,b",c}', ["a,b", "c"]),  # embedded comma
        (r'{"x\"y"}', ['x"y']),  # escaped quote
        (r'{"a\\b"}', [r"a\b"]),  # escaped backslash
        ("{NULL}", [None]),  # NULL element
        ('{"NULL"}', ["NULL"]),  # quoted NULL is the string
        ("{a,NULL,b}", ["a", None, "b"]),
        ('{""}', [""]),  # empty-string element
        ("{{1,2},{3}}", [["1", "2"], ["3"]]),  # nested
        ("[0:1]={a,b}", ["a", "b"]),  # explicit dimension prefix
        ("{⟦pii:abc⟧,plain}", ["⟦pii:abc⟧", "plain"]),  # dbproxy tokens are ordinary elements
        ('{"⟦pii:a,b⟧"}', ["⟦pii:a,b⟧"]),
        ("{a b,c}", ["a b", "c"]),  # unquoted space
    ],
)
def test_parse_pg_array(literal, expected):
    assert db._parse_pg_array(literal) == expected


@pytest.mark.parametrize("value", ["", "plain text", "not{an}array", "⟦pii:abc⟧", "{unterminated"])
def test_parse_pg_array_passes_through_non_literals(value):
    assert db._parse_pg_array(value) == value


_TEXT_OID = 25
_ENUM_ARRAY_OID = 987654  # enum arrays get a per-database OID — nothing static to match on


class _Col(SimpleNamespace):
    """Stand-in for psycopg's cursor.description entry (name + type_code)."""


class _ResultCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        if "pg_type" in sql:
            self.description = [_Col(name="oid", type_code=26)]
            self._rows = [(_ENUM_ARRAY_OID,)]
        elif sql.lstrip().upper().startswith("SET"):
            pass
        else:
            self.description = self.conn.description
            self._rows = self.conn.rows

    def fetchall(self):
        return self._rows


class _ResultConnection:
    read_only = False

    def __init__(self, description, rows):
        self.description = description
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _ResultCursor(self)


def _run(monkeypatch, description, rows, dsn="postgresql://example"):
    conn = _ResultConnection(description, rows)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *a, **k: conn))
    monkeypatch.setenv("PG_DSN", dsn)
    db._ARRAY_OIDS.clear()
    return db.query("select roles, note from people"), conn


def test_query_hydrates_enum_array_columns(monkeypatch):
    rows, conn = _run(
        monkeypatch,
        [_Col(name="roles", type_code=_ENUM_ARRAY_OID), _Col(name="note", type_code=_TEXT_OID)],
        [("{parent,child}", "hello"), ("{}", None), (None, "{looks,like,an,array}")],
    )
    assert rows[0]["roles"] == ["parent", "child"]
    assert rows[1]["roles"] == []  # empty array, not None
    assert rows[2]["roles"] is None  # NULL column stays None, never []
    # A plain text column is never parsed, even when its value looks exactly like a literal.
    assert rows[2]["note"] == "{looks,like,an,array}"
    assert sum("pg_type" in s for s in conn.executed) == 1  # OID set fetched once, then cached


def test_query_skips_oid_lookup_when_no_array_shaped_value(monkeypatch):
    _, conn = _run(
        monkeypatch,
        [_Col(name="roles", type_code=_ENUM_ARRAY_OID), _Col(name="note", type_code=_TEXT_OID)],
        [(None, "hello"), (None, "⟦pii:abc⟧")],
    )
    assert not any("pg_type" in s for s in conn.executed)


def test_query_leaves_whole_column_pii_token_alone(monkeypatch):
    # The dbproxy can replace an entire array value with one token; it must survive as a string
    # (parsing it would be nonsense) rather than raise.
    rows, _ = _run(
        monkeypatch,
        [_Col(name="roles", type_code=_ENUM_ARRAY_OID), _Col(name="note", type_code=_TEXT_OID)],
        [("⟦pii:abc⟧", "{a,b}")],
    )
    assert rows[0]["roles"] == "⟦pii:abc⟧"


# --- Timeout hard cap + one-statement rule -----------------------------------------------------
#
# The dbproxy in front of production refuses CancelRequest, so a client-side cancel does nothing:
# the SERVER-side statement_timeout is the only thing that ever stops a runaway grounding query.
# These lock down that it is always set, never disabled, and never above the cap.


class _RecordingCursor:
    description = None

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)

    def fetchall(self):
        return []


class _RecordingConnection:
    read_only = False

    def __init__(self, server_version=160004):
        self.executed = []
        self.info = SimpleNamespace(server_version=server_version)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _RecordingCursor(self)


def _executed(monkeypatch, server_version=160004, **kwargs):
    """Run `select 1` against a recording double; return (executed SQL, connect kwargs)."""
    conn = _RecordingConnection(server_version)
    seen = {}

    def connect(*_a, **kw):
        seen.update(kw)
        return conn

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    db.query("select 1", **kwargs)
    return conn.executed, seen


def _timeout_value(executed, guc="statement_timeout"):
    setsql = executed[0]
    assert setsql.lstrip().upper().startswith("SET LOCAL")
    for stmt in setsql.split(";"):
        if guc in stmt:
            return int(stmt.split("=")[1])
    return None


@pytest.mark.parametrize(
    "kwargs, expected, warns",
    [
        ({}, 30_000, False),  # omitted → default
        ({"timeout_ms": 60_000}, 60_000, False),  # under the cap → honoured
        ({"timeout_ms": 600_000}, 120_000, True),  # over the cap → clamped, loudly
        ({"timeout_ms": 0}, 30_000, False),  # 0 means "default", NOT "no timeout"
        ({"timeout_ms": None}, 30_000, False),
    ],
)
def test_statement_timeout_is_always_set_and_capped(monkeypatch, recwarn, kwargs, expected, warns):
    executed, _ = _executed(monkeypatch, **kwargs)
    assert _timeout_value(executed) == expected
    clamped = [w for w in recwarn if "hard cap" in str(w.message)]
    assert bool(clamped) is warns


def test_belt_and_suspenders_gucs(monkeypatch):
    # PG 16 has no transaction_timeout; an unknown SET LOCAL would ABORT the transaction, so it is
    # gated on the server version rather than tried and caught.
    pg16, _ = _executed(monkeypatch, server_version=160004)
    assert "idle_in_transaction_session_timeout = 30000" in pg16[0]
    assert "transaction_timeout" not in pg16[0]

    pg17, _ = _executed(monkeypatch, server_version=170004)
    assert "transaction_timeout = 32000" in pg17[0]  # +slack: statement_timeout must win the race


def test_query_canceled_hint_names_limit_and_referenced_table_stats(monkeypatch):
    exc = Exception("canceling statement due to statement timeout")
    exc.sqlstate = "57014"
    monkeypatch.setattr(
        db,
        "table_stats",
        lambda table, schema=None, db=None: {
            "schema": schema or "public",
            "table": table,
            "estimated_rows": 123456,
            "total_size": "42 MB",
            "seq_scan": 9,
            "idx_scan": 1,
            "indexes": [{"name": "users_email_idx", "definition": "CREATE INDEX ..."}],
        },
    )
    hint = db._mistake_hint(
        exc,
        None,
        10_000,
        sql="select * from public.users join accounts on accounts.user_id = users.id",
        db="app",
    )
    assert hint.startswith(
        "Query exceeded this project's 10s limit and was killed on Postgres. Rewrite for performance: "
        "filter on indexed columns, add LIMIT, pre-aggregate, avoid count(*)/full scans on large tables."
    )
    assert "public.users: ~123456 rows, 42 MB" in hint
    assert "users_email_idx" in hint


def test_table_stats_uses_catalog_queries_only(monkeypatch):
    calls = []

    def fake_query(sql, params=None, db=None, **kwargs):
        calls.append((sql, params, db, kwargs))
        if "from pg_class" in sql:
            return [{
                "schema": "public", "table": "users", "estimated_rows": 1000,
                "total_size": "128 kB", "seq_scan": 3, "idx_scan": 20,
                "last_analyze": None, "last_autoanalyze": "now",
            }]
        if "from pg_indexes" in sql:
            return [{"name": "users_pkey", "definition": "CREATE UNIQUE INDEX users_pkey ..."}]
        if "from pg_stats" in sql:
            return [{"column": "email", "n_distinct": -0.8, "null_frac": 0.0}]
        raise AssertionError(sql)

    monkeypatch.setattr(db, "query", fake_query)
    got = db.table_stats("users", db="app")
    assert got["estimated_rows"] == 1000
    assert got["indexes"][0]["name"] == "users_pkey"
    assert got["columns"][0]["n_distinct"] == -0.8
    assert all(call[3]["_timeout_hint_stats"] is False for call in calls)
    assert all("select * from users" not in call[0].lower() for call in calls)


def test_explain_never_analyzes(monkeypatch):
    seen = {}

    def fake_query(sql, params=None, db=None, **_kwargs):
        seen.update(sql=sql, params=params, db=db)
        return [{"QUERY PLAN": "Seq Scan on users"}, {"QUERY PLAN": "  Filter: active"}]

    monkeypatch.setattr(db, "query", fake_query)
    assert db.explain("select * from users where active = %s", [True], db="app") == (
        "Seq Scan on users\n  Filter: active"
    )
    assert seen["sql"] == "EXPLAIN (FORMAT TEXT) select * from users where active = %s"
    with pytest.raises(RuntimeError, match="never runs ANALYZE"):
        db.explain("EXPLAIN ANALYZE select * from users")


def test_cli_timeout_error_has_no_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Query exceeded this project's 10s limit and was killed on Postgres.")
        ),
    )
    assert db._main(["select pg_sleep(20)"]) == 1
    captured = capsys.readouterr()
    assert "Query exceeded this project's 10s limit" in captured.err
    assert "Traceback" not in captured.err


def test_cli_stats_and_explain(monkeypatch, capsys):
    monkeypatch.setattr(
        db,
        "table_stats",
        lambda table, schema=None, db=None: {
            "schema": schema or "public", "table": table, "estimated_rows": 12,
        },
    )
    assert db._main(["--stats", "analytics.users", "--format", "json"]) == 0
    assert '"schema": "analytics"' in capsys.readouterr().out

    monkeypatch.setattr(db, "explain", lambda sql, params=None, db=None: "Index Scan using users_pkey")
    assert db._main(["--explain", "select * from users"]) == 0
    assert capsys.readouterr().out.strip() == "Index Scan using users_pkey"


def test_connect_passes_tcp_keepalives(monkeypatch):
    _, kwargs = _executed(monkeypatch)
    assert kwargs["keepalives"] == 1
    assert kwargs["keepalives_idle"] == 30
    assert kwargs["keepalives_interval"] == 10
    assert kwargs["keepalives_count"] == 3


@pytest.mark.parametrize(
    "sql",
    [
        "SET statement_timeout = 0; select pg_sleep(300)",
        "select 1; select 2",
        "select 1;\n-- comment\nselect 2",
        "select 1; /* c */ select 2",
    ],
)
def test_multi_statement_sql_is_rejected(monkeypatch, sql):
    conn = _RecordingConnection()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *a, **k: conn))
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    with pytest.raises(RuntimeError, match="multiple SQL statements"):
        db.query(sql)
    assert conn.executed == []  # rejected before we ever connect/execute


@pytest.mark.parametrize(
    "sql",
    [
        "select 'a;b'",  # ';' inside a single-quoted literal
        "select 'it''s; fine'",  # …with an escaped quote before it
        'select "weird;col" from t',  # …inside a quoted identifier
        "select $$a;b$$",  # …inside a dollar-quoted string
        "select $tag$a;b$tag$",
        "select 1;",  # a single trailing ';'
        "select 1;  \n",
        "select 1 -- comment; more",  # ';' inside a line comment
        "select 1 /* c; c */ from t",  # …inside a block comment
        "select 1; -- trailing note; still one statement",
    ],
)
def test_single_statement_sql_is_allowed(monkeypatch, sql):
    db._reject_multi_statement(sql)  # must not raise


# --- MySQL branch ------------------------------------------------------------------------------
#
# A `mysql://` DSN switches lib.db to PyMySQL. The proxy in front of a customer MySQL speaks only
# the text protocol and holds its own query ceiling, so what these lock down is that we still emit
# the read-only + max_execution_time guards, still refuse a second statement client-side, and still
# heal/hint exactly like the Postgres path.

_MYSQL_DSN = "mysql://run:tok%40n@proxy.internal:8083/vdb"


class _MySQLError(Exception):
    """PyMySQL puts the server errno in args[0]; that is all lib.db reads."""


class _MySQLCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        self.conn.params.append(params)
        if self.conn.raise_on_query and not sql.lstrip().upper().startswith(("SET", "START")):
            raise self.conn.raise_on_query
        if sql.lstrip().upper().startswith(("SET", "START")):
            return
        self.description = self.conn.description
        self._rows = self.conn.rows

    def fetchall(self):
        return self._rows


class _MySQLConnection:
    def __init__(self, description=None, rows=(), raise_on_query=None, server_version="8.0.34"):
        self.description = description or [("n", 3)]
        self.rows = list(rows)
        self.raise_on_query = raise_on_query
        self.executed = []
        self.params = []
        self.closed = False
        self.server_version = server_version

    def cursor(self):
        return _MySQLCursor(self)

    def get_server_info(self):
        return self.server_version

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clear_mysql_version_cache():
    """Every test gets a fresh version detection — the cache is keyed by DSN, and tests reuse
    `_MYSQL_DSN` across differing fake server versions."""
    db._MYSQL_VERSION_CACHE.clear()
    yield
    db._MYSQL_VERSION_CACHE.clear()


def _fake_pymysql(monkeypatch, conn, seen=None):
    def connect(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return conn

    monkeypatch.setitem(
        sys.modules,
        "pymysql",
        SimpleNamespace(connect=connect, cursors=SimpleNamespace(DictCursor=object)),
    )
    monkeypatch.setenv("PG_DSN", _MYSQL_DSN)
    return conn


def test_mysql_dsn_maps_to_pymysql_connect_kwargs(monkeypatch):
    seen = {}
    _fake_pymysql(monkeypatch, _MySQLConnection(rows=[{"n": 1}]), seen)

    assert db.query("select 1 as n") == [{"n": 1}]
    assert seen["host"] == "proxy.internal"
    assert seen["port"] == 8083
    assert seen["user"] == "run"
    assert seen["password"] == "tok@n"  # percent-decoded
    assert seen["database"] == "vdb"
    assert seen["connect_timeout"] == 15
    assert seen["autocommit"] is False
    assert seen["local_infile"] is False
    # Dead-network backstop: the socket may not go silent for longer than the cap plus slack.
    assert seen["read_timeout"] == 60
    assert seen["write_timeout"] == 60
    assert "ssl" not in seen  # the proxy hop is plaintext unless the DSN says otherwise


@pytest.mark.parametrize(
    ("tls", "expected"),
    [("true", {"check_hostname": True}), ("skip-verify", {"check_hostname": False, "verify_mode": False})],
)
def test_mysql_tls_only_when_the_dsn_asks(monkeypatch, tls, expected):
    seen = {}
    _fake_pymysql(monkeypatch, _MySQLConnection(rows=[]), seen)
    monkeypatch.setenv("PG_DSN", f"mysql://u:p@h:3306/d?tls={tls}")
    db.query("select 1")
    assert seen["ssl"] == expected


def test_mysql_emits_read_only_transaction_and_timeout(monkeypatch):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[]))
    db.query("select 1", timeout_ms=9_000)
    assert conn.executed[:3] == [
        "SET SESSION max_execution_time = 9000",
        "START TRANSACTION READ ONLY",
        "select 1",
    ]
    assert conn.closed


def test_mysql_timeout_is_clamped_to_the_hard_cap(monkeypatch, recwarn):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[]))
    db.query("select 1", timeout_ms=600_000)
    assert conn.executed[0] == "SET SESSION max_execution_time = 120000"
    assert any("hard cap" in str(w.message) for w in recwarn)


def test_mysql_warns_but_continues_when_the_server_refuses_the_cap(monkeypatch, recwarn):
    class _RefusingCursor(_MySQLCursor):
        def execute(self, sql, params=None):
            if sql.startswith("SET SESSION max_execution_time"):
                raise _MySQLError(1193, "Unknown system variable")
            return super().execute(sql, params)

    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[{"n": 1}]))
    monkeypatch.setattr(conn, "cursor", lambda: _RefusingCursor(conn))

    assert db.query("select 1 as n") == [{"n": 1}]
    assert any("refused SET SESSION max_execution_time" in str(w.message) for w in recwarn)


def test_mysql_skips_max_execution_time_before_5_7_8(monkeypatch, recwarn):
    """5.6 doesn't have the GUC (ER_UNKNOWN_SYSTEM_VARIABLE, 1193) — skip it silently rather than
    warn on every single query; the wire proxy enforces the ceiling regardless."""
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[], server_version="5.6.51-log"))
    db.query("select 1", timeout_ms=9_000)
    assert conn.executed == ["START TRANSACTION READ ONLY", "select 1"]
    assert not any("max_execution_time" in str(w.message) for w in recwarn)


def test_mysql_still_sets_max_execution_time_on_5_7_8(monkeypatch):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[], server_version="5.7.8"))
    db.query("select 1", timeout_ms=9_000)
    assert conn.executed[0] == "SET SESSION max_execution_time = 9000"


def test_mysql_refuses_below_5_6_5(monkeypatch):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[], server_version="5.5.62"))
    with pytest.raises(RuntimeError, match="predates 5.6.5"):
        db.query("select 1")
    assert conn.executed == []


def test_mysql_multi_statement_is_rejected_before_connecting(monkeypatch):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection())
    with pytest.raises(RuntimeError, match="multiple SQL statements"):
        db.query("SET SESSION max_execution_time = 0; select sleep(300)")
    assert conn.executed == []


def test_mysql_auto_heals_excluded_columns(monkeypatch, recwarn):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[{"id": 1, "email": "a@b.test"}]))
    monkeypatch.delenv("PG_DSN")  # resolve via the project's own *_DSN, which keys the heal map
    monkeypatch.setenv("IBEAUTY_DSN", _MYSQL_DSN)
    monkeypatch.setenv(
        "RC_DB_EXCLUDED_COLUMNS",
        '{"IBEAUTY_DSN": {"tables": {"clients": {"exclude": ["national_id"]}}}}',
    )
    db.query("select id, national_id, email from clients")
    assert conn.executed[2] == "select id, email from clients"
    assert any("dropped column(s) ['national_id']" in str(w.message) for w in recwarn)


def test_mysql_unknown_column_gets_the_scoping_aware_hint(monkeypatch):
    _fake_pymysql(
        monkeypatch,
        _MySQLConnection(raise_on_query=_MySQLError(1054, "Unknown column 'ssn' in 'field list'")),
    )
    with pytest.raises(RuntimeError) as excinfo:
        db.query("select ssn from clients")
    msg = str(excinfo.value)
    assert "Unknown column 'ssn'" in msg
    assert "intentionally hidden by this project's data-scoping" in msg
    assert "lib.db.columns('<table>')" in msg


def test_mysql_query_timeout_hint_names_the_limit(monkeypatch):
    _fake_pymysql(
        monkeypatch,
        _MySQLConnection(raise_on_query=_MySQLError(3024, "Query execution was interrupted")),
    )
    monkeypatch.setattr(db, "table_stats", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError, match="was killed on MySQL"):
        db.query("select sleep(60)", timeout_ms=10_000)


def test_mysql_syntax_error_hint_calls_out_the_dialect(monkeypatch):
    _fake_pymysql(
        monkeypatch,
        _MySQLConnection(raise_on_query=_MySQLError(1064, "You have an error in your SQL syntax")),
    )
    with pytest.raises(RuntimeError, match="backticks"):
        db.query('select "id"::text from clients')


def test_mysql_syntax_error_hint_is_version_keyed_on_5_6(monkeypatch):
    _fake_pymysql(
        monkeypatch,
        _MySQLConnection(
            raise_on_query=_MySQLError(1064, "You have an error in your SQL syntax"),
            server_version="5.6.51-log",
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        db.query("select 1")
    msg = str(excinfo.value)
    assert "no CTEs" in msg
    assert "no JSON type" in msg


def test_mysql_syntax_error_hint_has_no_stale_caveat_on_8x(monkeypatch):
    _fake_pymysql(
        monkeypatch,
        _MySQLConnection(
            raise_on_query=_MySQLError(1064, "You have an error in your SQL syntax"),
            server_version="8.0.34",
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        db.query("select 1")
    assert "no CTEs" not in str(excinfo.value)


def test_mysql_json_columns_decode(monkeypatch):
    _fake_pymysql(
        monkeypatch,
        _MySQLConnection(
            description=[("meta", 245), ("note", 253)],
            rows=[{"meta": '{"a": 1}', "note": "{not,json}"}],
        ),
    )
    rows = db.query("select meta, note from t")
    assert rows[0]["meta"] == {"a": 1}
    assert rows[0]["note"] == "{not,json}"  # non-JSON column type is never touched


def test_mysql_introspection_uses_database_not_current_schema(monkeypatch):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[]))
    db.tables()
    db.columns("clients")
    assert all("current_schema()" not in s for s in conn.executed)
    assert any("coalesce(%s, database())" in s for s in conn.executed)


def test_mysql_explain_prefers_format_tree(monkeypatch):
    conn = _fake_pymysql(monkeypatch, _MySQLConnection(rows=[{"EXPLAIN": "-> Table scan on t"}]))
    assert db.explain("select * from t") == "-> Table scan on t"
    assert conn.executed[-1] == "EXPLAIN FORMAT=TREE select * from t"
    with pytest.raises(RuntimeError, match="never runs ANALYZE"):
        db.explain("EXPLAIN ANALYZE select * from t")


def test_mysql_explain_skips_format_tree_before_8_0_16(monkeypatch):
    conn = _fake_pymysql(
        monkeypatch,
        _MySQLConnection(
            description=[("id", 3), ("select_type", 253), ("table", 253)],
            rows=[{"id": 1, "select_type": "SIMPLE", "table": "t"}],
            server_version="5.6.51-log",
        ),
    )
    plan = db.explain("select * from t")
    assert "table=t" in plan
    assert all("FORMAT=TREE" not in s for s in conn.executed)


# --- Table visibility (RC_DB_VISIBLE_TABLES / RC_DB_HIDDEN_TABLES) -----------------------------
#
# A hidden table is wire-indistinguishable from a typo: both come back as UndefinedTable. The host
# injects what the projection actually contains, which lets lib.db answer the three questions
# separately — visible / hidden by policy / absent — before AND after a failed query.


@pytest.fixture
def scoped_env(monkeypatch):
    """One scoped database whose projection the host described."""
    for key in list(__import__("os").environ):
        if key.endswith("_DSN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_DSN", "postgresql://app")
    monkeypatch.setenv("RC_DB_VISIBLE_TABLES", '{"APP_DSN": ["activities", "people"]}')
    monkeypatch.setenv("RC_DB_HIDDEN_TABLES", '{"APP_DSN": ["form_fields"]}')


def test_visibility_primitives(scoped_env):
    assert db.visible_tables() == ["activities", "people"]
    assert db.hidden_tables() == ["form_fields"]
    assert db.is_visible("activities") is True
    assert db.is_visible("form_fields") is False
    assert db.is_visible("nonsense") is False
    # Schema-qualified / quoted / mixed-case names resolve to the same relation.
    assert db.is_visible('public."Activities"') is True


def test_visibility_is_unknown_without_host_metadata(monkeypatch):
    monkeypatch.delenv("RC_DB_VISIBLE_TABLES", raising=False)
    monkeypatch.delenv("RC_DB_HIDDEN_TABLES", raising=False)
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    # None, not [] — "we were told nothing" must never read as "nothing is visible".
    assert db.visible_tables() is None
    assert db.hidden_tables() == []
    assert db.is_visible("anything") is None


@pytest.mark.parametrize("raw", ["", "   ", "not json", '["a"]', '{"APP_DSN": 3}'])
def test_malformed_visibility_env_is_ignored(monkeypatch, raw):
    monkeypatch.setenv("APP_DSN", "postgresql://app")
    monkeypatch.setenv("RC_DB_VISIBLE_TABLES", raw)
    monkeypatch.setenv("RC_DB_HIDDEN_TABLES", raw)
    assert db.visible_tables() is None
    assert db.hidden_tables() == []


def _psycopg_double(monkeypatch, message):
    """A psycopg stand-in whose cursor raises UndefinedTable(message) on the query.

    Returns the raised instance so a test can assert on `__cause__` identity."""

    class UndefinedTable(Exception):
        pass

    class UndefinedColumn(Exception):
        pass

    class Error(Exception):
        pass

    error = UndefinedTable(message)

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, sql, params=None):
            if not sql.lstrip().upper().startswith("SET LOCAL"):
                raise error

        def fetchall(self):
            return []

    class Connection:
        read_only = False
        info = SimpleNamespace(server_version=160004)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def cursor(self):
            return Cursor()

    fake = SimpleNamespace(
        connect=lambda *a, **kw: Connection(),
        Error=Error,
        errors=SimpleNamespace(UndefinedTable=UndefinedTable, UndefinedColumn=UndefinedColumn),
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return error


def test_query_on_a_hidden_table_raises_hidden_table_error(monkeypatch, scoped_env):
    exc = _psycopg_double(
        monkeypatch, 'relation "form_fields" does not exist\nLINE 1: select * from form_fields'
    )

    with pytest.raises(db.HiddenTableError) as excinfo:
        db.query("select * from form_fields")

    err = excinfo.value
    assert err.table == "form_fields"
    assert err.db is None
    assert isinstance(err, RuntimeError)  # catchable by helpers that only know RuntimeError
    assert err.__cause__ is exc  # the original psycopg error survives
    msg = str(err)
    assert "permission boundary, not missing data" in msg
    assert "Do not name it to the requester" in msg


def test_query_on_an_unknown_table_says_it_is_absent(monkeypatch, scoped_env):
    _psycopg_double(monkeypatch, 'relation "form_fieldz" does not exist')

    with pytest.raises(RuntimeError) as excinfo:
        db.query("select * from form_fieldz")

    assert not isinstance(excinfo.value, db.HiddenTableError)
    msg = str(excinfo.value)
    assert "does not exist in this project's projection at all" in msg
    assert "lib.db.tables()" in msg


def test_undefined_hint_stays_generic_when_the_projection_is_unknown(monkeypatch):
    monkeypatch.delenv("RC_DB_VISIBLE_TABLES", raising=False)
    monkeypatch.delenv("RC_DB_HIDDEN_TABLES", raising=False)
    monkeypatch.setenv("PG_DSN", "postgresql://example")
    hint = db._undefined_hint(Exception('relation "whatever" does not exist'))
    assert "intentionally hidden by this project's data-scoping" in hint
    assert "does not exist in this project's projection at all" not in hint


def test_undefined_column_keeps_the_column_hint(scoped_env):
    # A missing COLUMN must not be re-labelled as a missing table.
    hint = db._undefined_hint(Exception('column "nope" does not exist'))
    assert "lib.db.columns" in hint


def test_tables_warns_about_policy_hidden_tables(monkeypatch, scoped_env, recwarn):
    import warnings as _warnings

    monkeypatch.setattr(db, "query", lambda *a, **kw: [{"table_name": "activities"}])
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        rows = db.tables()
    assert rows == [{"table_name": "activities"}]
    assert any("form_fields" in str(w.message) for w in caught)


def test_cli_list_shows_hidden_tables(scoped_env, capsys):
    assert db._main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "APP_DSN" in out
    assert "hidden for this requester (app): form_fields" in out
