"""Read-only Postgres access for grounding.

A project usually has SEVERAL databases — Momentum Tools has powertools / ruby / elsa — each
injected as its own ``*_DSN`` env var. Pick one with the ``db`` argument, which accepts a short
name (``"powertools"``), the exact env-var name (``"MOMENTUM_POWERTOOLS_DSN"``), or a raw DSN.
``db`` may be omitted with a single database configured, with ``PG_DSN`` set, or when the project has
a STANDARD database (the operator's default, injected as ``RC_DB_DEFAULT``) — a multi-DB run then
reads the standard. ``databases()`` lists what this run has. ``tables()`` / ``columns()`` introspect
the effective schema; ``columns()`` / ``tables_with_column()`` return a `ColumnList` whose elements
ARE the column names and also answer to ``c.type`` / ``c["data_type"]`` / ``c["column_name"]``. ``--list`` (or a bad ``db=``) shows each database's purpose — the descriptions
come from project metadata in the ``RC_DB_DESCRIPTIONS`` env var (a JSON object keyed by the exact
DSN env-var name), so the agent learns which DB is which without trial-and-error.

On a data-scoped project, `query` AUTO-HEALS: if you SELECT a column the project hides (standard
single-table shape), it's dropped, the trimmed query runs, and a warning names what was dropped — so
one extra field doesn't fail the whole query. A column that ISN'T hidden (a typo) still raises, with a
scoping-aware hint. The hidden-column map comes from the ``RC_DB_EXCLUDED_COLUMNS`` env var.

Whole TABLES can be hidden the same way, by the requester's access policy. The host injects what the
projection contains through ``RC_DB_VISIBLE_TABLES`` and ``RC_DB_HIDDEN_TABLES`` (both JSON, keyed by
the exact DSN env-var name), which turns an ambiguous "does not exist" into three distinct answers:
`visible_tables()` (``None`` = unknown), `hidden_tables()`, and `is_visible(table)` — ask before
writing an enrichment query. Querying a hidden table raises `HiddenTableError` (a ``RuntimeError``
subclass carrying ``.table``/``.db``) instead of a bare ``UndefinedTable``, so a helper can degrade on
the permission boundary; a name in neither list is reported as genuinely absent. As with every other
host-injected map this is best-effort — absent or malformed metadata changes nothing.

Array columns hydrate to real Python lists (``enum[]`` included — see `query`), so
``row["roles"]`` is a ``list``, never the raw ``"{parent,child}"`` literal.

Read-only by provisioning; this module adds a belt-and-suspenders ``READ ONLY``
transaction plus server-side timeouts so a stray write fails loudly and a runaway query can't hang
the run. The timeout is a HARD CAP, not a suggestion: every query runs under ``statement_timeout``
(+ ``idle_in_transaction_session_timeout``, + ``transaction_timeout`` on PG 17+). The host injects
the project's resolved limit through ``RC_DB_QUERY_TIMEOUT_SECONDS``; that value is both default and
maximum. Outside a hosted run, the legacy 30s default / 120s maximum remain. ``timeout_ms=0``/``None``
means "default", never "unlimited". One SQL statement per `query` call (a second top-level
statement raises), so a query can't smuggle a ``SET statement_timeout = 0`` past the cap.
Use ``table_stats()`` for catalog-only size/scan/index/column evidence and ``explain()`` for a safe
planner view (never ``ANALYZE``).
``psycopg`` is imported lazily, so the module — its DSN resolution, and the CLI's ``--list`` —
loads even where the driver isn't installed.

MySQL: a ``mysql://`` DSN transparently switches the whole module to PyMySQL — same helpers, same
`Column`/`ColumnList` results, same read-only + hard-timeout posture (``START TRANSACTION READ
ONLY`` + ``SET SESSION max_execution_time`` where the server has it, with the wire proxy in front
enforcing the ceiling regardless), same one-statement rule, same hidden-column auto-heal and
scoping-aware hints. What differs: ``schema=`` means the MySQL *database* (default: the DSN's own,
via ``database()``); `columns` reports the full ``COLUMN_TYPE`` (``varchar(255)``, ``int
unsigned``); `table_stats` returns ``information_schema`` sizes + the index list and has no
``seq_scan``/``idx_scan``/planner column stats; `explain` uses ``EXPLAIN FORMAT=TREE`` on servers
that support it (8.0.16+), tabular ``EXPLAIN`` otherwise; there are no Postgres arrays to hydrate
(``JSON`` columns decode to Python values instead); and only the text protocol is used — prepared
statements are refused upstream, so ``%s`` params are interpolated client-side exactly as psycopg's
simple protocol does.

Server version is read once per DSN straight off the connection handshake (free — no extra round
trip) and gates the GUCs/features that don't exist on older MySQL: ``max_execution_time`` is
5.7.8+ (silently skipped below that — the wire proxy enforces the timeout ceiling regardless, this
is only belt-and-suspenders); ``START TRANSACTION READ ONLY`` is 5.6.5+ (below that this module
refuses rather than running unguarded); ``EXPLAIN FORMAT=TREE`` is 8.0.16+; CTEs/window functions
are 8.0+ and the ``JSON`` type is 5.7+ (both called out in the syntax-error hint, version-keyed so
newer servers don't get a stale caveat).

CLI (token-efficient one-offs from bash):

    python -m lib.db --list
    python -m lib.db --db powertools "select count(*) from accounts"
    python -m lib.db --format table "select id, email from accounts limit 20"
    python -m lib.db --stats accounts --db powertools --format json
    python -m lib.db --explain --db powertools "select * from accounts where email = 'a@b.test'"
"""

import os
import re
import warnings
from typing import NoReturn


def _configured_timeout_limits() -> tuple[int, int]:
    """Return (default, maximum) milliseconds from the host limit, with standalone compatibility."""
    raw = os.environ.get("RC_DB_QUERY_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            configured = int(raw) * 1000
        except ValueError:
            configured = 0
        if configured > 0:
            return configured, configured
    return 30_000, 120_000


DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS = _configured_timeout_limits()
# How much longer than statement_timeout the PG17 transaction_timeout backstop runs (see `_timeout_sql`).
_TRANSACTION_TIMEOUT_SLACK_MS = 2_000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15

# Dead-network protection: without these libpq inherits the OS default (~2h before a vanished peer
# is noticed), so a dropped NAT/VPN mapping would hang the run far past any statement_timeout —
# which only ticks while the server is reachable. ~60s to notice instead.
_KEEPALIVE_KWARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

# The host's own operational store (registry + River + audit log) — never a grounding target.
# Excluded from discovery so the agent can't accidentally pick it.
_HOST_DSN_VARS = ("DATABASE_URL",)

# Cache of array-type OIDs per resolved DSN (see `_array_oids`). Process-local; OIDs are stable
# for a database, and a run's container is disposable, so a plain dict is enough.
_ARRAY_OIDS: dict[str, frozenset] = {}


# Postgres prefixes the literal with explicit bounds when an array's lower bound isn't 1
# ("[0:1]={a,b}", "[1:2][1:2]={{1,2},{3,4}}"). Rare, but without this the value would fall through
# as a bare string.
_ARRAY_DIMS_RE = re.compile(r"^\[[-\d:\]\[]+\]=")


def _looks_like_array_literal(s: str) -> bool:
    """Could this string be an array output literal? Shape only — NEVER the decision to parse.

    Used purely as a cheap prefilter for whether a result set is worth one `_array_oids` lookup;
    the authoritative test is always the column's type OID. Lossless in that direction: Postgres
    renders every array as ``{...}`` (optionally behind a dimension prefix), so a value failing
    this test cannot be an array."""
    return bool(s[:1] == "{" or _ARRAY_DIMS_RE.match(s))


def _parse_pg_array(text: str):
    """Parse a Postgres array output literal into a (possibly nested) Python list.

    psycopg parses arrays of KNOWN element types (``text[]``, ``int[]``, ``uuid[]``, …) into lists
    itself; it leaves arrays of element types it has no loader for — chiefly **enum arrays** and
    other user-defined types — as the **raw literal string** (``"{parent}"``, ``"{parent,child}"``,
    ``"{}"``). Iterating that string by mistake (``list(role)`` → characters) is a silent footgun,
    so `query` routes such values through here.

    Handles the array grammar: quoted elements with backslash escapes (``{"a,b","x\\"y"}``),
    unquoted ``NULL`` → ``None`` (a quoted ``"NULL"`` stays the string), empty ``{}`` → ``[]``,
    nesting (``{{1,2},{3}}``), and a dimension prefix (``[0:1]={a,b}``). Elements come back as
    ``str`` — the types that reach here are enum/uuid/domain-ish, and psycopg already produced real
    ``int``/``float`` lists for the numeric arrays it knows, so no coercion is attempted (cast
    yourself if a custom numeric domain shows up). A value that isn't an array literal — e.g. a
    whole column replaced by a PII ``⟦pii:…⟧`` token — is returned unchanged; tokens *inside* a
    literal are ordinary string elements.
    """
    s = text.strip()
    dims = _ARRAY_DIMS_RE.match(s)
    if dims:
        s = s[dims.end():]
    if not (s.startswith("{") and s.endswith("}")):
        return text
    i = 0
    n = len(s)

    def parse_array():
        nonlocal i
        i += 1  # consume '{'
        out: list = []
        if i < n and s[i] == "}":
            i += 1
            return out
        while i < n:
            if s[i] == "{":
                out.append(parse_array())
            elif s[i] == '"':
                out.append(parse_quoted())
            else:
                out.append(parse_unquoted())
            if i < n and s[i] == ",":
                i += 1
                continue
            if i < n and s[i] == "}":
                i += 1
            break
        return out

    def parse_quoted():
        nonlocal i
        i += 1  # consume opening '"'
        buf: list = []
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
            elif c == '"':
                i += 1
                break
            else:
                buf.append(c)
                i += 1
        return "".join(buf)

    def parse_unquoted():
        nonlocal i
        start = i
        while i < n and s[i] not in ",}":
            i += 1
        tok = s[start:i]
        return None if tok == "NULL" else tok

    return parse_array()


def _array_oids(conn, dsn: str) -> frozenset:
    """OIDs of all array types in this database (``pg_type.typcategory = 'A'``), cached per DSN.

    Lets `query` tell that a value psycopg returned as a *string* actually came from an array
    column (an unhandled element type, e.g. an enum array) and should be parsed — without touching
    real text columns that merely contain braces. ``typcategory`` is the only robust source: enum
    arrays (and other user-defined types) get **per-database OIDs**, so no static table can cover
    them. One round trip per DSN per process, and `query` only spends it when a result set actually
    contains an array-shaped string."""
    oids = _ARRAY_OIDS.get(dsn)
    if oids is None:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_type WHERE typcategory = 'A'")
            oids = frozenset(r[0] for r in cur.fetchall())
        _ARRAY_OIDS[dsn] = oids
    return oids


def databases() -> list[str]:
    """Names of the project DSN env vars available this run (``*_DSN``, host store excluded)."""
    return sorted(
        k for k, v in os.environ.items() if k.endswith("_DSN") and v and k not in _HOST_DSN_VARS
    )


def _default_db_env() -> str:
    """The project's STANDARD database — the DSN env var name the host injects as ``RC_DB_DEFAULT`` (the
    operator-set default a multi-DB run falls back to when ``db=`` is omitted). ``""`` when none set."""
    return os.environ.get("RC_DB_DEFAULT", "").strip()


def _short_name(env: str) -> str:
    """Short name for a DSN env var = its trailing segment, lowercased.

    ``MOMENTUM_POWERTOOLS_DSN`` → ``powertools``; ``MOMENTUM_ELSA_REPLICA_DSN`` → ``replica``. The
    single source for both the user-facing listing and `_resolve_dsn`'s exact short-name match
    (which compares case-consistently, uppercased)."""
    return env[: -len("_DSN")].rsplit("_", 1)[-1].lower()


def _descriptions() -> dict[str, str]:
    """Parse ``RC_DB_DESCRIPTIONS`` (JSON: exact DSN env-var name → one-sentence purpose).

    Best-effort metadata, host-filtered to this run's DSNs; absent/blank/malformed → ``{}`` (never
    raise — a bad description must never break a query)."""
    import json

    raw = os.environ.get("RC_DB_DESCRIPTIONS")
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(val, dict):
        return {}
    return {str(k): str(v) for k, v in val.items()}


def _format_catalog() -> str:
    """Human listing of this run's databases: short name, exact env var, and purpose when known.

    One line each, ``  - <short>  <ENV_VAR> — <description>`` (the ``— …`` omitted when no
    description); ``  (none configured)`` when there are none."""
    avail = databases()
    if not avail:
        return "  (none configured)"
    descs = _descriptions()
    width = max(len(_short_name(c)) for c in avail)
    lines = []
    for env in avail:
        line = f"  - {_short_name(env):<{width}}  {env}"
        desc = descs.get(env)
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


def _resolve_dsn(db: str | None) -> str:
    """Resolve ``db`` to a DSN: raw DSN → exact env name → exact short name → substring fallback.

    Resolution prefers EXACT matches and only falls back to a substring match (with a warning) so a
    short name can't silently bind the wrong database: ``db="elsa"`` must not quietly resolve to
    ``MOMENTUM_ELSA_REPLICA_DSN`` when an exact ``elsa`` database exists. An ambiguous match (>1
    candidate at any tier) raises rather than guessing.
    """
    if db and "://" in db:
        return db
    if db:
        if os.environ.get(db):  # exact env-var name
            return os.environ[db]
        key = db.upper().replace("-", "_")
        avail = databases()

        # Exact short name: match the env var's trailing segment exactly — "powertools" →
        # MOMENTUM_POWERTOOLS_DSN, "elsa" → MOMENTUM_ELSA_DSN (NOT MOMENTUM_ELSA_REPLICA_DSN, whose
        # trailing segment is "replica"). This is the intended path and wins over any substring, so a
        # short name can't silently bind a longer, differently-named database. `_short_name` lowercases,
        # so compare uppercased to stay consistent with `key`.
        named = [c for c in avail if c == key or c == f"{key}_DSN" or _short_name(c).upper() == key]
        if len(named) == 1:
            return os.environ[named[0]]
        if len(named) > 1:
            raise RuntimeError(
                f"db={db!r} is ambiguous (matches {named}); pick an exact one:\n{_format_catalog()}"
            )
        # Substring fallback — convenient but lossy, so warn: it can bind a name the caller didn't
        # mean (e.g. "elsa" → MOMENTUM_ELSA_REPLICA_DSN). Ambiguity here still raises.
        sub = [c for c in avail if key in c]
        if len(sub) == 1:
            import warnings

            warnings.warn(
                f"db={db!r} matched {sub[0]} by substring (no exact name matched); "
                f"pass an exact name from databases() to be unambiguous",
                stacklevel=2,
            )
            return os.environ[sub[0]]
        if len(sub) > 1:
            raise RuntimeError(
                f"db={db!r} is ambiguous (matches {sub}); pick an exact one:\n{_format_catalog()}"
            )
        raise RuntimeError(f"unknown db={db!r}. Valid databases:\n{_format_catalog()}")
    if os.environ.get("PG_DSN"):
        return os.environ["PG_DSN"]
    avail = databases()
    if len(avail) == 1:
        return os.environ[avail[0]]
    if not avail:
        raise RuntimeError("no project database configured for this run (no *_DSN env var set)")
    # Several DBs and no db=: fall back to the project's STANDARD database when the operator set one
    # (RC_DB_DEFAULT, a DSN env name). Saves a weak model a turn rediscovering db=; query() still names
    # the alternatives if the standard turns out to be the wrong DB for the table.
    default_env = _default_db_env()
    if default_env and os.environ.get(default_env):
        return os.environ[default_env]
    raise RuntimeError(
        "multiple databases available — pass db= to pick one (short name, env var, or raw DSN):\n"
        f"{_format_catalog()}"
    )


def _undefined_hint(exc, db: str | None = None) -> str:
    """Guidance suffix for an undefined-column/table error — the data-scoping footgun, defused.

    On a scoped run the agent queries the per-run ``scope_<id>`` **views**, so a column (or table)
    the project's data-scoping projected away simply "does not exist" — at the wire level it's
    indistinguishable from a typo, and the bare Postgres error tempts the LLM to rewrite the whole
    query from scratch. Instead, point it at the introspection helper so it drops just the one
    unavailable name and re-runs. Best-effort: prepends Postgres's own HINT when present.

    When the host told us what the projection contains (``RC_DB_VISIBLE_TABLES`` /
    ``RC_DB_HIDDEN_TABLES``) a missing RELATION stops being ambiguous, so say which of the three it
    is: hidden by the access policy, or genuinely absent, or (unknown projection) the generic text."""
    parts = []
    diag = getattr(exc, "diag", None)
    pg_hint = getattr(diag, "message_hint", None) if diag is not None else None
    if pg_hint:
        parts.append(pg_hint)
    rel = _undefined_relation(exc)
    if rel is not None:
        norm = _norm_table(rel)
        if norm in {_norm_table(t) for t in hidden_tables(db)}:
            parts.append(_hidden_table_message(rel))
            return " ".join(parts)
        vis = visible_tables(db)
        if vis is not None and norm not in {_norm_table(t) for t in vis}:
            parts.append(
                f"`{rel}` does not exist in this project's projection at all (check spelling / db=); "
                "queryable tables: `lib.db.tables()`."
            )
            return " ".join(parts)
    parts.append(
        "This column/table may be intentionally hidden by this project's data-scoping — you query "
        "projected views, not the base tables, so a hidden column reads as 'does not exist' (NOT "
        "necessarily a typo). Run lib.db.columns('<table>') to list exactly what's queryable, then "
        "drop the unavailable name and re-run — no need to rewrite the whole query."
    )
    return " ".join(parts)


# An (optionally schema-qualified) SQL identifier. The enum type name we interpolate into the
# introspection SELECT comes out of a Postgres *error message*, so it is untrusted input by the time
# it reaches us — anything that isn't a plain identifier is refused rather than quoted.
_IDENT_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_$]*\.)?[A-Za-z_][A-Za-z0-9_$]*")


def _enum_labels(conn, timeout_ms: int, enum_type: str) -> list | None:
    """Valid labels of a PG enum, fetched at error time on the SAME connection. None if unavailable.

    The failed statement left the transaction ABORTED ("current transaction is aborted"), so nothing
    else can run until we ``rollback()``. Reusing the connection is safe precisely because `query`
    opens a fresh one per call: rolling back throws away only our own read-only transaction, and
    ``conn.read_only`` still applies to the next one. The timeout has to be re-emitted, though — the
    ``SET LOCAL``s died with the transaction that carried them.

    Best-effort by construction: any failure degrades to None and the caller falls back to telling
    the agent which query to run itself."""
    if not _IDENT_RE.fullmatch(enum_type or ""):
        return None
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(_timeout_sql(conn, timeout_ms))
            cur.execute(f"SELECT unnest(enum_range(NULL::{enum_type}))")
            return [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001 - introspection is a nicety; never let it mask the real error.
        return None


_TABLE_REFERENCE_RE = re.compile(
    r'(?is)\b(?:from|join)\s+((?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)(?:\.(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*))?)'
)


def _referenced_tables(sql: str | None) -> list[tuple[str | None, str]]:
    """Best-effort FROM/JOIN identifiers, excluding subqueries and duplicate references."""
    out = []
    seen = set()
    for match in _TABLE_REFERENCE_RE.finditer(sql or ""):
        parts = [p.strip('"') for p in match.group(1).split(".")]
        item = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _timeout_table_evidence(sql: str | None, db: str | None) -> str:
    """Compact catalog evidence for referenced tables; failures never obscure the cancellation."""
    evidence = []
    for schema_name, table in _referenced_tables(sql)[:3]:
        try:
            info = table_stats(table, schema=schema_name, db=db)
        except Exception:  # noqa: BLE001 - timeout guidance is best-effort after the real failure.
            continue
        indexes = ",".join(i["name"] for i in info["indexes"][:4]) or "none"
        parts = [f"~{info['estimated_rows']} rows", str(info["total_size"])]
        # MySQL's information_schema has no scan counters; omit rather than print "None".
        if info.get("seq_scan") is not None:
            parts.append(f"seq_scan={info['seq_scan']}, idx_scan={info['idx_scan']}")
        parts.append(f"indexes={indexes}")
        evidence.append(f"{info['schema']}.{table}: " + ", ".join(parts))
    return " Referenced table stats: " + "; ".join(evidence) + "." if evidence else ""


def _timeout_exceeded_hint(timeout_ms: int, sql: str | None, db: str | None, server: str) -> str:
    """The 'your query was killed by the cap' correction, shared by both engines."""
    seconds = timeout_ms / 1000
    limit = str(int(seconds)) if seconds.is_integer() else f"{seconds:g}"
    return (
        f"Query exceeded this project's {limit}s limit and was killed on {server}. Rewrite for "
        "performance: filter on indexed columns, add LIMIT, pre-aggregate, avoid count(*)/full "
        "scans on large tables."
    ) + _timeout_table_evidence(sql, db)


def _mistake_hint(
    exc, conn, timeout_ms: int, sql: str | None = None, db: str | None = None
) -> str | None:
    """One corrective hint for the SQL mistakes agents actually make, or None to re-raise untouched.

    Consolidates the generic Postgres mistake classes that project brains kept hand-rolling (kampadmin
    ``ka.py``'s ``explain_db_error`` and the support brains' per-project ``db.py`` engines) into the
    shipped runtime, so every brain gets the same correction without copy-paste drift.

    Matching is on the **message shape** (``str(exc)``), not on driver exception classes: it keeps the
    whole thing mock-testable, tolerates psycopg version differences, and catches the one class that
    has no sqlstate at all (the client-side placeholder error, raised before anything reaches the
    server). Hint texts are stable, project-agnostic primitives — project pedagogy ("roles are
    hyphenated in this schema") stays in brain scripts."""
    import difflib

    msg = str(exc)

    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
    if sqlstate == "57014":
        return _timeout_exceeded_hint(timeout_ms, sql, db, "Postgres")

    # Invented enum label (InvalidTextRepresentation / 22P02). No hardcoded label maps: show the
    # database's OWN labels, which makes hyphen-vs-underscore and singular-vs-plural self-evident.
    m = re.search(r'invalid input value for enum ((?:\w+\.)?\w+): "([^"]*)"', msg)
    if m:
        enum_type, bad = m.group(1), m.group(2)
        labels = _enum_labels(conn, timeout_ms, enum_type)
        if labels:
            hint = f"{bad!r} is not a {enum_type} label. Valid: {', '.join(labels)}."
            close = difflib.get_close_matches(bad, labels, n=1)
            if close:
                hint += f" Closest: {close[0]!r}."
            return hint
        return (
            f"{bad!r} is not a {enum_type} label. List them: "
            f"SELECT unnest(enum_range(NULL::{enum_type}))"
        )

    if "malformed array literal" in msg:
        return (
            "A scalar was compared/assigned where Postgres expected an ARRAY. For membership in an "
            "array column use `%s = ANY(col)` (bind the single value); to pass a real array, bind a "
            "Python list as one param."
        )

    if "operator does not exist:" in msg:
        if "[]" in msg:
            return (
                "Enum (and other user-defined) arrays don't support the text-array operators. Test "
                "membership with `%s = ANY(col)` instead — e.g. `WHERE %s = ANY(role)` with "
                "['child']."
            )
        if "json" in msg:
            return (
                "JSON/JSONB comparison: use `col->>'key'` (returns text) instead of `col->'key'` "
                "(returns jsonb) when comparing to a string, or cast the whole document with "
                "`col::text ILIKE %s` to text-match it."
            )
        return (
            "The two sides have incompatible types. Check them with lib.db.columns('<table>') and "
            "cast one side, e.g. `col::text = %s` or `%s::uuid`."
        )

    if "is ambiguous" in msg and "column reference" in msg:
        return (
            "That column name exists in more than one table in this query — qualify it as "
            "`table.column` (or `alias.column`)."
        )

    # Client-side, sqlstate None: psycopg only scans the SQL for placeholders when params are bound,
    # and that scan rejects a literal `%`. `query` already sends params=None when there are none, so
    # this fires only on a genuine mix of an inline wildcard AND %s params.
    if re.search(r"only '%s', '%b', '%t' are allowed as placeholders", msg):
        return (
            "A literal % (LIKE/ILIKE wildcard) cannot be mixed with %s params. Pass the whole "
            "pattern as a param instead: `... ILIKE %s` with ['%term%'] — or escape it as %%."
        )

    return None


def _defaulted_to_standard(db: str | None) -> str | None:
    """Short name of the STANDARD database iff THIS call defaulted to it — i.e. ``db`` was omitted and a
    multi-DB run resolved via ``RC_DB_DEFAULT`` (not PG_DSN, not a lone DB). ``None`` otherwise. Lets
    `query` tell a table-not-found apart as "you didn't pick a DB, so the standard was used and the table
    may live in another", instead of the generic typo/scoping hint."""
    if db is not None or os.environ.get("PG_DSN"):
        return None
    if len(databases()) <= 1:
        return None
    default_env = _default_db_env()
    if default_env and os.environ.get(default_env):
        return _short_name(default_env)
    return None


def _excluded_columns() -> dict:
    """Parse ``RC_DB_EXCLUDED_COLUMNS`` (JSON: exact DSN env name → the columns the project's
    data-scoping hides). Shape per env: ``{"tables": {"<t>": {"exclude": [...]}|{"include":
    [...]}}}``. Host-injected from the scope_manifest. Absent/malformed → ``{}`` (never raise —
    auto-heal is best-effort, a query must never break because this is missing)."""
    import json

    raw = os.environ.get("RC_DB_EXCLUDED_COLUMNS")
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _excluded_map_for_db(db: str | None) -> dict:
    """Hidden-column metadata for this db, keyed like RC_DB_EXCLUDED_COLUMNS."""
    excluded = _excluded_columns()
    if not excluded:
        return {}
    dsn = _resolve_dsn(db)
    return excluded.get(_env_name_for_dsn(dsn) or "", {})


def _env_name_for_dsn(dsn: str) -> str | None:
    """The ``*_DSN`` env var whose value is this resolved DSN — the key `RC_DB_EXCLUDED_COLUMNS` uses.
    A raw DSN passed straight to ``db=`` has no env name (→ no heal data, which is fine)."""
    for k, v in os.environ.items():
        if k.endswith("_DSN") and v == dsn:
            return k
    return None


class HiddenTableError(RuntimeError):
    """A table the requester's access policy removed from this run's projection.

    Not the same failure as "no such table": the relation exists in the customer's database, it is
    simply not part of what THIS requester may read. Raised by `query`/`query_one` (and the
    introspection helpers that go through them) when Postgres's ``UndefinedTable`` names a relation
    the host listed in ``RC_DB_HIDDEN_TABLES``. ``__cause__`` keeps the original psycopg error.

    Attributes: ``table`` (the relation as Postgres named it) and ``db`` (the ``db=`` argument in
    play, possibly ``None``)."""

    def __init__(self, message: str, *, table: str, db: str | None = None):
        super().__init__(message)
        self.table = table
        self.db = db


def _hidden_table_message(table: str) -> str:
    """The one wording for a policy-hidden table. Says the boundary out loud AND forbids leaking it:
    naming the table back to the requester would disclose the very thing the policy hid."""
    return (
        f"`{table}` is hidden for this requester by the project's access policy — a permission "
        "boundary, not missing data. Do not name it to the requester; skip it or say the "
        "information is not available to them."
    )


def _norm_table(name) -> str:
    """Comparable form of a (possibly schema-qualified, possibly quoted) relation name."""
    s = str(name).strip().strip('"')
    if "." in s:
        s = s.rsplit(".", 1)[-1].strip().strip('"')
    return s.lower()


# `relation "form_fields" does not exist` (Postgres) / `Table 'db.form_fields' doesn't exist` (MySQL).
# Column errors say `column "x" does not exist`, which neither pattern matches — deliberate, a missing
# COLUMN must keep falling through to the generic hint.
_UNDEFINED_RELATION_RES = (
    re.compile(r'relation "([^"]+)" does not exist', re.IGNORECASE),
    re.compile(r"Table '([^']+)' doesn't exist", re.IGNORECASE),
)


def _undefined_relation(exc) -> str | None:
    """The relation name inside an undefined-table error, or None if this isn't one."""
    texts = []
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    if primary:
        texts.append(str(primary))
    texts.append(str(exc))
    for text in texts:
        for rx in _UNDEFINED_RELATION_RES:
            m = rx.search(text)
            if m:
                return m.group(1)
    return None


def _table_scope_env(name: str) -> dict:
    """Parse a host-injected ``{"<DSN_ENV_NAME>": ["table", ...]}`` JSON map.

    Same best-effort contract as `_excluded_columns`: absent/blank/malformed → ``{}``. Nothing here
    may ever raise — a query must not break because visibility metadata is missing or garbled."""
    import json

    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(val, dict):
        return {}
    return {
        str(k): [str(t) for t in v if isinstance(t, str)]
        for k, v in val.items()
        if isinstance(v, list)
    }


def _table_list_for_db(env_var: str, db: str | None) -> list[str] | None:
    """This db's entry in one of the visibility maps, or None when there is no entry.

    None covers every "we don't know" case: env absent, DSN passed raw (no env name), or the DSN
    simply not listed (flat/passthrough — nothing was projected)."""
    mapping = _table_scope_env(env_var)
    if not mapping:
        return None
    try:
        dsn = _resolve_dsn(db)
    except RuntimeError:
        return None
    val = mapping.get(_env_name_for_dsn(dsn) or "")
    return list(val) if val is not None else None


def visible_tables(db: str | None = None) -> list[str] | None:
    """Table/view names this run's projection exposes — or ``None`` when that is unknown.

    ``None`` is a real answer and must not be read as "nothing is visible": it means the host
    injected no ``RC_DB_VISIBLE_TABLES`` entry for this database (older host, local run, or a flat
    DSN that was never projected). A list means the projection is known and complete."""
    return _table_list_for_db("RC_DB_VISIBLE_TABLES", db)


def hidden_tables(db: str | None = None) -> list[str]:
    """Tables the requester's access policy removed from this run's projection.

    ``[]`` when none are hidden *and* when nothing is known — callers that need the distinction ask
    `visible_tables` (``None`` = unknown). Never raises."""
    return _table_list_for_db("RC_DB_HIDDEN_TABLES", db) or []


def is_visible(table: str, db: str | None = None) -> bool | None:
    """Can this run query ``table``? ``True``/``False``, or ``None`` when the projection is unknown.

    Ask BEFORE writing an enrichment query, so a helper degrades on purpose instead of crashing on a
    permission boundary it cannot see. ``None`` means "no information" — treat it as "try it"."""
    norm = _norm_table(table)
    if norm in {_norm_table(t) for t in hidden_tables(db)}:
        return False
    vis = visible_tables(db)
    if vis is None:
        return None
    return norm in {_norm_table(t) for t in vis}


def _hidden_table_notes(db: str | None = None) -> list[str]:
    """Short warnings naming the policy-hidden tables, mirroring `_hidden_column_notes`."""
    hidden = sorted(hidden_tables(db))
    if not hidden:
        return []
    return [
        "access policy: tables hidden from this requester and absent from the list above: "
        f"{', '.join(hidden)}. Querying one raises HiddenTableError; do not name it to the requester."
    ]


def _pattern_matches(pattern: str, value: str) -> bool:
    """Postgres ILIKE-ish matcher for helper hints (% and _ wildcards only)."""
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.fullmatch(rx, value, flags=re.IGNORECASE) is not None


def _hidden_column_notes(emap: dict, table: str | None = None, pattern: str | None = None) -> list[str]:
    """Short warnings about manifest-hidden columns; never changes the visible schema result."""
    if not emap:
        return []
    notes = []
    tables = emap.get("tables") or {}
    if table:
        rules = [(table, tables.get(table))]
    else:
        rules = sorted((str(t), rule) for t, rule in tables.items())
    hidden = []
    allowlisted = []
    for t, rule in rules:
        if not isinstance(rule, dict):
            continue
        if "exclude" in rule:
            for col in rule.get("exclude") or []:
                if isinstance(col, str) and (pattern is None or _pattern_matches(pattern, col)):
                    hidden.append(f"{t}.{col}")
        if "include" in rule:
            allowlisted.append(t)
    if hidden:
        notes.append(f"data-scoping: hidden columns omitted: {', '.join(hidden)}.")
    if allowlisted:
        target = ", ".join(sorted(allowlisted))
        notes.append(f"data-scoping: {target} shows an allowlisted subset; only shown columns are queryable.")
    return notes


def _warn_hidden_column_notes(emap: dict, table: str | None = None, pattern: str | None = None) -> None:
    for note in _hidden_column_notes(emap, table=table, pattern=pattern):
        warnings.warn(note, stacklevel=2)


def _is_hidden(emap: dict, table: str, col: str) -> bool:
    """Does the scope_manifest hide ``col`` on ``table``? True iff it's in the table's exclude list,
    or (whitelist mode) NOT in the table's include list. The whitelist case is why we can't enumerate
    hidden columns up front — we test per requested column."""
    t = (emap.get("tables") or {}).get(table)
    if not isinstance(t, dict):
        return False
    if "exclude" in t:
        return col in (t["exclude"] or [])
    if "include" in t:
        return col not in (t["include"] or [])
    return False


def _split_top_level_commas(s: str) -> list[str]:
    """Split a SELECT list on commas that aren't inside double-quoted identifiers. Callers only reach
    here for the simple shape (no parens/subqueries — `_parse_simple_select` already bailed on those)."""
    out, buf, in_q = [], [], False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == "," and not in_q:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _bare_column(item: str) -> str | None:
    """The plain column name of a SELECT item, or None if it's not a bare column (alias/AS/expression/
    function/star) — those we never auto-drop. ``t.col`` → ``col``; ``"Weird"`` → ``Weird``."""
    it = item.strip()
    if not it or it == "*" or "(" in it or " " in it:  # space ⇒ alias or AS ⇒ not a bare column
        return None
    if "." in it:
        it = it.rsplit(".", 1)[-1]
    if it.startswith('"') and it.endswith('"') and len(it) >= 2:
        it = it[1:-1]
    return it or None


def _parse_simple_select(sql: str):
    """Match the standard shape ``SELECT <plain col list> FROM <one table> [rest]`` and return
    ``(items, table, list_start, list_end)`` where ``sql[list_start:list_end]`` is exactly the column
    list (so the rebuild preserves the ``FROM`` keyword + everything after). None for anything else
    (``SELECT *``, joins, multiple tables, expressions, subqueries) — a non-match just means "don't heal"."""
    import re

    m = re.match(r"(?is)\s*select\s+(.*?)\s+from\s+(.+)", sql)
    if not m:
        return None
    list_str = m.group(1)
    if "(" in list_str or "*" in list_str:  # expression/subquery/star ⇒ not the simple shape
        return None
    rest = m.group(2).lstrip()
    # First token after FROM is the table; bail on a join or a comma (multiple tables) anywhere after.
    tbl_m = re.match(r'([A-Za-z0-9_."]+)(\s|$)', rest)
    if not tbl_m:
        return None
    table = tbl_m.group(1)
    tail = rest[tbl_m.end():]
    if re.search(r"(?is)\bjoin\b", " " + rest) or "," in rest.split(None, 1)[0] or tail.lstrip().startswith(","):
        return None
    if "." in table:
        table = table.rsplit(".", 1)[-1]
    table = table.strip('"')
    items = _split_top_level_commas(list_str)
    return items, table, m.start(1), m.end(1)


def _strip_excluded(sql: str, emap: dict):
    """Pre-flight heal: drop SELECT-list columns the manifest hides for the FROM table, returning
    ``(new_sql, dropped)``. No-op (``(sql, [])``) unless the query is the simple shape AND names a
    genuinely-hidden column AND at least one column survives — so a working query, a typo (not in the
    manifest), or a query we can't safely parse is left untouched for Postgres to handle."""
    if not emap:
        return sql, []
    parsed = _parse_simple_select(sql)
    if not parsed:
        return sql, []
    items, table, list_start, list_end = parsed
    keep, dropped = [], []
    for it in items:
        col = _bare_column(it)
        if col is not None and _is_hidden(emap, table, col):
            dropped.append(col)
        else:
            keep.append(it)
    if not dropped or not keep:  # nothing hidden, or stripping would empty the SELECT ⇒ don't heal
        return sql, []
    new_sql = sql[:list_start] + ", ".join(s.strip() for s in keep) + sql[list_end:]
    return new_sql, dropped


def _effective_timeout_ms(timeout_ms) -> int:
    """The timeout we will actually SET: caller's value, defaulted and clamped to `MAX_TIMEOUT_MS`.

    ``0``/``None``/negative mean "use the default" — NEVER "no timeout" (the old meaning, which let a
    caller disable the backstop). In hosted runs the injected project limit is both default and cap;
    standalone callers retain the legacy 30s default / 120s cap."""
    try:
        ms = int(timeout_ms or DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        ms = DEFAULT_TIMEOUT_MS
    if ms <= 0:
        ms = DEFAULT_TIMEOUT_MS
    if ms > MAX_TIMEOUT_MS:
        warnings.warn(
            f"timeout_ms={ms} exceeds the {MAX_TIMEOUT_MS} ms hard cap for grounding reads — "
            f"clamped to {MAX_TIMEOUT_MS} ms. Narrow the query (index-friendly filters, LIMIT, "
            f"pre-aggregate) instead of waiting longer.",
            stacklevel=3,
        )
        ms = MAX_TIMEOUT_MS
    return ms


def _skip_noise(s: str, i: int) -> int:
    """Advance past whitespace, ``--`` line comments and (nestable) ``/* */`` block comments."""
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace() or c == ";":  # a stray extra ';' is noise, not a second statement
            i += 1
        elif s.startswith("--", i):
            nl = s.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif s.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if s.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif s.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        else:
            break
    return i


def _first_top_level_semicolon(sql: str) -> int:
    """Index of the first ``;`` that is real SQL punctuation, or ``-1``.

    A scanner, not a split: a ``;`` inside a single-quoted literal (``'a;b'``, ``''`` escapes), a
    double-quoted identifier, a dollar-quoted body (``$$…;…$$``, ``$tag$…$tag$``) or a comment is
    ordinary text and must not count — otherwise legitimate queries would be rejected."""
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # '' = escaped quote, string continues
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif c == '"':
            i += 1
            while i < n and sql[i] != '"':
                i += 1
            i += 1
        elif c == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                i = n if end == -1 else end + len(tag)
            else:  # $1 placeholder or a bare '$' — not a dollar quote
                i += 1
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            i = _skip_noise(sql, i)
        elif c == ";":
            return i
        else:
            i += 1
    return -1


def _reject_multi_statement(sql: str) -> None:
    """Raise unless ``sql`` is ONE statement (a trailing ``;`` and trailing comments are fine).

    psycopg sends a params-less query over the simple protocol, which happily runs every statement in
    the string and returns only the LAST result. So ``"SET statement_timeout=0; select …"`` would
    quietly lift the cap this module exists to enforce (and return ``[]`` while the real query ran
    unbounded). Refuse at the client instead of trusting the server-side GUCs to survive."""
    i = _first_top_level_semicolon(sql)
    if i == -1:
        return
    if _skip_noise(sql, i) >= len(sql):  # only whitespace/comments/`;` after it ⇒ single statement
        return
    raise RuntimeError(
        "multiple SQL statements in one query() call are not allowed (found a ';' followed by more "
        "SQL). Run exactly one statement per call — a second statement can silently override this "
        "run's read-only + timeout guarantees, and only the last result would come back. A trailing "
        "';' is fine."
    )


def _server_version(conn) -> int:
    """``conn.info.server_version`` (e.g. 170004) or ``0`` when unknown/faked. Gates GUCs that don't
    exist on older servers — an unknown ``SET LOCAL`` ABORTS the transaction, so it can't be
    try/excepted; it has to be asked about first."""
    try:
        return int(conn.info.server_version)
    except Exception:  # noqa: BLE001 - test doubles and exotic drivers may not expose info
        return 0


def _timeout_sql(conn, timeout_ms: int) -> str:
    """The SET LOCALs that bound this transaction, as ONE round trip.

    ``statement_timeout`` kills a runaway query; ``idle_in_transaction_session_timeout`` kills a
    transaction we somehow stop driving; ``transaction_timeout`` (PG 17+) bounds the whole thing
    including the gaps — belt and suspenders, because each alone has a hole.

    ``transaction_timeout`` gets a small slack on top: its clock starts at BEGIN (before these SETs),
    so at an identical value it wins the race and TERMINATES the connection ("terminating connection
    due to transaction timeout") instead of letting ``statement_timeout`` cancel the statement with
    the actionable error. Slack keeps it the backstop it is meant to be."""
    stmts = [
        f"SET LOCAL statement_timeout = {timeout_ms}",
        f"SET LOCAL idle_in_transaction_session_timeout = {timeout_ms}",
    ]
    if _server_version(conn) >= 170000:
        stmts.append(f"SET LOCAL transaction_timeout = {timeout_ms + _TRANSACTION_TIMEOUT_SLACK_MS}")
    return "; ".join(stmts)


def _raise_connect_failure(e: Exception) -> NoReturn:
    """Turn any driver connect error into the actionable 'run it on ReplyPen infra' guidance."""
    hint = (
        f"Database connection failed before the query could run "
        f"(connect_timeout={DEFAULT_CONNECT_TIMEOUT_SECONDS}s)."
    )
    if os.environ.get("RC_LOCAL_BRAIN_RUN"):
        hint += (
            " This was a local brain_run.py live check; project DSNs are often IP/region "
            "allowlisted for ReplyPen production. Use `rp dev console database ...` for direct SQL or "
            "`rp dev console bash run 'python /brain/skills/.../scripts/<script>.py ...'` to run the "
            "same brain script on ReplyPen infra."
        )
    else:
        hint += (
            " If this happened from a laptop/local live check, prefer `rp dev console database ...` "
            "or `rp dev console bash run ...` "
            "so the read executes on ReplyPen production infra."
        )
    raise RuntimeError(f"{hint}\n\nOriginal error: {type(e).__name__}: {e}") from e


# --- MySQL branch ------------------------------------------------------------------------------
#
# A `mysql://` DSN reaches us through a wire proxy that speaks only the MySQL TEXT protocol: it
# refuses COM_STMT_PREPARE, multi-statements, and any client `SET ... max_execution_time`, and holds
# its own hard query ceiling. Everything below therefore stays on PyMySQL's client-side `%s`
# interpolation (never a server-side prepare) and treats our session guards as belt-and-suspenders.

_MYSQL_SCHEMES = ("mysql://", "mysql+pymysql://")

# How long past the statement cap the socket may stay silent before we give up on a vanished peer.
# The libpq keepalive kwargs have no PyMySQL equivalent; read/write timeouts are the analogue.
_MYSQL_NETWORK_SLACK_SECONDS = 30

# information_schema errnos worth a tailored correction (mysqlclient/PyMySQL put them in args[0]).
_MYSQL_BAD_FIELD = 1054
_MYSQL_NO_SUCH_TABLE = 1146
_MYSQL_BAD_DB = 1049
_MYSQL_SYNTAX_ERROR = 1064
_MYSQL_EXEC_TIMEOUTS = (3024, 1317)  # max_execution_time exceeded / query interrupted

_MYSQL_JSON_TYPE_CODE = 245  # FIELD_TYPE.JSON

# Version floors for the features/GUCs this module touches (major, minor, patch).
_MYSQL_MIN_READ_ONLY_TXN = (5, 6, 5)  # START TRANSACTION READ ONLY
_MYSQL_MIN_MAX_EXECUTION_TIME = (5, 7, 8)  # SET SESSION max_execution_time (ER_UNKNOWN_SYSTEM_VARIABLE below)
_MYSQL_MIN_EXPLAIN_TREE = (8, 0, 16)  # EXPLAIN FORMAT=TREE
_MYSQL_MIN_WINDOW_AND_CTE = (8, 0, 0)
_MYSQL_MIN_JSON_TYPE = (5, 7, 0)

_MYSQL_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# Cache of (major, minor, patch) per resolved DSN — parsed once from the connection handshake
# (``conn.get_server_info()``), which PyMySQL already captures on connect at zero extra cost, so
# "detect the version" never costs a dedicated round trip once a DSN has connected at least once.
_MYSQL_VERSION_CACHE: dict[str, tuple[int, int, int]] = {}


def _parse_mysql_version(raw: str | None) -> tuple[int, int, int]:
    """``(major, minor, patch)`` from a handshake version string (``"5.6.51-log"`` → ``(5, 6, 51)``),
    or ``(0, 0, 0)`` when unparseable — callers must treat that as "unknown", not "ancient"."""
    m = _MYSQL_VERSION_RE.search(raw or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _mysql_note_version(dsn: str, conn) -> tuple[int, int, int]:
    """Cache+return this DSN's server version from an already-open connection's handshake info."""
    cached = _MYSQL_VERSION_CACHE.get(dsn)
    if cached is not None:
        return cached
    version = _parse_mysql_version(conn.get_server_info())
    _MYSQL_VERSION_CACHE[dsn] = version
    return version


def _mysql_server_version(db: str | None) -> tuple[int, int, int]:
    """This db's MySQL version, connecting only if no query has resolved it yet this process."""
    dsn = _resolve_dsn(db)
    cached = _MYSQL_VERSION_CACHE.get(dsn)
    if cached is not None:
        return cached
    import pymysql

    try:
        conn = pymysql.connect(**_mysql_connect_kwargs(dsn, DEFAULT_TIMEOUT_MS))
    except Exception as e:  # noqa: BLE001 - PyMySQL connect errors vary by socket/TLS path.
        _raise_connect_failure(e)
    try:
        return _mysql_note_version(dsn, conn)
    finally:
        conn.close()


def _is_mysql_dsn(dsn: str) -> bool:
    return dsn.strip().lower().startswith(_MYSQL_SCHEMES)


def _engine_is_mysql(db: str | None) -> bool:
    """Does ``db`` resolve to a MySQL DSN? Unresolvable → False, so `query` raises the good error."""
    try:
        return _is_mysql_dsn(_resolve_dsn(db))
    except RuntimeError:
        return False


def _pretty_bytes(n) -> str:
    """``information_schema`` byte counts in ``pg_size_pretty`` shape, so both engines read alike."""
    try:
        size = float(n or 0)
    except (TypeError, ValueError):
        return "0 bytes"
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} TB"


def _mysql_connect_kwargs(dsn: str, timeout_ms: int) -> dict:
    """PyMySQL connect kwargs from a ``mysql://`` URL. TLS only when the DSN asks (the proxy hop is
    plaintext); ``read_timeout``/``write_timeout`` are the dead-network backstop libpq's keepalives
    give the Postgres path."""
    from urllib.parse import parse_qs, unquote, urlsplit

    parts = urlsplit(dsn)
    kwargs = {
        "host": parts.hostname or "127.0.0.1",
        "port": parts.port or 3306,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "database": unquote(parts.path.lstrip("/")) or None,
        "connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "read_timeout": int(timeout_ms / 1000) + _MYSQL_NETWORK_SLACK_SECONDS,
        "write_timeout": int(timeout_ms / 1000) + _MYSQL_NETWORK_SLACK_SECONDS,
        "autocommit": False,
        "local_infile": False,
        "charset": "utf8mb4",
    }
    tls = (parse_qs(parts.query).get("tls") or [""])[0].lower()
    if tls in ("skip-verify", "preferred"):
        kwargs["ssl"] = {"check_hostname": False, "verify_mode": False}
    elif tls and tls not in ("false", "0", "disable", "disabled"):
        kwargs["ssl"] = {"check_hostname": True}
    return kwargs


def _mysql_syntax_caveats(version: tuple[int, int, int]) -> str:
    """Version-keyed dialect gaps worth mentioning on a 1064, or "" when unknown/all supported."""
    if version == (0, 0, 0):
        return ""
    gaps = []
    if version < _MYSQL_MIN_WINDOW_AND_CTE:
        gaps.append("no CTEs (`WITH ...`) or window functions")
    if version < _MYSQL_MIN_JSON_TYPE:
        gaps.append("no JSON type/functions")
    if not gaps:
        return ""
    return f" Also, this server is MySQL {'.'.join(map(str, version))}: {'; '.join(gaps)}."


def _mysql_hint(
    exc, timeout_ms: int, sql: str | None, db: str | None, version: tuple[int, int, int] = (0, 0, 0)
) -> str | None:
    """One corrective hint per MySQL error class agents actually hit, or None to re-raise untouched."""
    errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if errno in _MYSQL_EXEC_TIMEOUTS:
        return _timeout_exceeded_hint(timeout_ms, sql, db, "MySQL")
    if errno in (_MYSQL_NO_SUCH_TABLE, _MYSQL_BAD_DB):
        std = _defaulted_to_standard(db)
        if std is not None:
            return (
                f"No db= was passed, so the standard database {std!r} was used — that table isn't "
                f"there. If it lives in another database, re-run with db=. Databases this run can "
                f"read:\n{_format_catalog()}"
            )
        return _undefined_hint(exc, db)
    if errno == _MYSQL_BAD_FIELD:
        return _undefined_hint(exc, db)
    if errno == _MYSQL_SYNTAX_ERROR:
        return (
            "MySQL rejected the syntax. This is MySQL, not Postgres: quote identifiers with "
            "`backticks` (a \"double-quoted\" name is a string literal), cast with CAST(x AS CHAR) "
            "instead of x::text, and use JSON_EXTRACT/-> instead of ->>."
        ) + _mysql_syntax_caveats(version)
    return None


def _mysql_query(sql, params, db_arg, dsn, timeout_ms, timeout_hint_stats):
    """`query` on PyMySQL: read-only transaction + session cap, text protocol, dict rows."""
    import pymysql

    try:
        conn = pymysql.connect(
            cursorclass=pymysql.cursors.DictCursor, **_mysql_connect_kwargs(dsn, timeout_ms)
        )
    except Exception as e:  # noqa: BLE001 - PyMySQL connect errors vary by socket/TLS path.
        _raise_connect_failure(e)

    version = _mysql_note_version(dsn, conn)
    try:
        with conn.cursor() as cur:
            if version != (0, 0, 0) and version < _MYSQL_MIN_READ_ONLY_TXN:
                raise RuntimeError(
                    f"MySQL {'.'.join(map(str, version))} predates "
                    f"{'.'.join(map(str, _MYSQL_MIN_READ_ONLY_TXN))}, which is where "
                    "`START TRANSACTION READ ONLY` was introduced — this module refuses to run "
                    "queries against it rather than drop the read-only guarantee."
                )
            # max_execution_time doesn't exist before 5.7.8 (ER_UNKNOWN_SYSTEM_VARIABLE, 1193) — on
            # a 5.6 server EVERY query would warn, so skip it silently below the floor instead of
            # trying and catching. The wire proxy in front enforces the timeout ceiling either way,
            # so this SET is belt-and-suspenders, not the only guard. Keep the warn path for a
            # genuine refusal on a server that claims 5.7.8+ but still rejects it.
            if version == (0, 0, 0) or version >= _MYSQL_MIN_MAX_EXECUTION_TIME:
                try:
                    cur.execute(f"SET SESSION max_execution_time = {timeout_ms}")
                except Exception as e:  # noqa: BLE001 - advisory only; the wire proxy caps regardless.
                    warnings.warn(
                        f"MySQL refused SET SESSION max_execution_time ({e}); the {timeout_ms} ms "
                        "ceiling is still enforced in front of this database.",
                        stacklevel=3,
                    )
            # Read-only transaction: a write attempt errors instead of mutating customer data.
            cur.execute("START TRANSACTION READ ONLY")
            try:
                # params=None keeps PyMySQL from scanning the SQL for placeholders, so an inline
                # `LIKE 'avo%'` wildcard survives verbatim — same contract as the psycopg path.
                cur.execute(sql, params if params else None)
            except Exception as e:  # noqa: BLE001 - PyMySQL error classes vary; errno is the signal.
                hint = _mysql_hint(e, timeout_ms, sql if timeout_hint_stats else None, db_arg, version)
                if hint is None:
                    raise
                raise RuntimeError(f"{e}\n\n{hint}") from e
            if cur.description is None:
                return []
            json_cols = {d[0] for d in cur.description if d[1] == _MYSQL_JSON_TYPE_CODE}
            rows = [dict(r) for r in cur.fetchall()]
        if json_cols:
            import json

            for row in rows:
                for col in json_cols:
                    val = row.get(col)
                    if isinstance(val, str):
                        try:
                            row[col] = json.loads(val)
                        except ValueError:  # a PII token replaced the document — leave it as text
                            pass
        return rows
    finally:
        conn.close()


def query(
    sql: str,
    params: list | tuple | None = None,
    db: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    *,
    _timeout_hint_stats: bool = True,
) -> list[dict]:
    """Run a read-only SELECT and return rows as a list of dicts.

    Opens a fresh read-only connection per call (the container is disposable, so pooling buys
    nothing). ``db`` selects the database (see module docstring).

    ONE statement per call: a second top-level statement raises (a trailing ``;`` is fine), because
    the simple protocol would run it too and let it undo this transaction's guarantees.

    ``timeout_ms`` caps the statement server-side. In hosted runs the project's resolved
    ``RC_DB_QUERY_TIMEOUT_SECONDS`` is both default and hard cap; standalone use retains the legacy
    30s default / 120s cap. A bigger value is clamped and ``0``/``None`` means "default".

    Placeholders: bind UNTRUSTED INPUT as ``%s`` with ``params`` — never string-format input into
    ``sql`` (injection). But a literal ``%`` wildcard is fine inline: ``ILIKE 'avo%'`` with no
    ``params`` runs verbatim (psycopg only treats ``%`` as a placeholder when ``params`` is passed).
    So either inline a static wildcard (``ILIKE 'avo%'``) OR bind a dynamic one (``ILIKE %s`` with
    ``['%' + term + '%']``) — both work; don't mix a static-`%` literal into a query that also binds
    params, since then psycopg scans the whole string and the literal ``%`` needs doubling (``%%``).

    Arrays: an array column always comes back as a real Python ``list`` — including ``enum[]`` and
    other types psycopg has no loader for, which the wire hands us as the literal ``"{a,b}"``
    (`_parse_pg_array`). ``{}`` → ``[]``, a NULL *element* → ``None`` in the list, but a NULL
    *column* stays ``None`` (not ``[]``) so "no value" and "empty array" stay distinguishable.
    Detection is by the column's type OID, so a text column holding ``"{a,b}"`` is left alone.

    Auto-heal: if the project's data-scoping hides a column you SELECT (standard single-table shape),
    that column is dropped, the trimmed query runs, and a warning names what was dropped — so one extra
    field doesn't fail the whole query. A column that ISN'T manifest-hidden (a typo) is left in and
    raises with a scoping-aware hint (`_undefined_hint`) rather than being silently swallowed.
    """
    _reject_multi_statement(sql)
    timeout_ms = _effective_timeout_ms(timeout_ms)
    dsn = _resolve_dsn(db)
    emap = _excluded_columns().get(_env_name_for_dsn(dsn) or "", {})
    sql, dropped = _strip_excluded(sql, emap)
    if dropped:
        warnings.warn(
            f"data-scoping: dropped column(s) {dropped} from your SELECT — hidden by this project's "
            f"scope_manifest. Ran the trimmed query; the rest of your result is intact.",
            stacklevel=2,
        )
    if _is_mysql_dsn(dsn):
        return _mysql_query(sql, params, db, dsn, timeout_ms, _timeout_hint_stats)

    import psycopg

    try:
        conn_cm = psycopg.connect(
            dsn,
            autocommit=False,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
            **_KEEPALIVE_KWARGS,
        )
    except Exception as e:  # noqa: BLE001 - psycopg connection errors vary by driver/libpq path.
        _raise_connect_failure(e)

    with conn_cm as conn:
        # Read-only transaction: a write attempt errors instead of mutating customer data.
        conn.read_only = True
        with conn.cursor() as cur:
            # Always emitted, before anything else can run: the timeout is the run's only backstop
            # against a query that never returns (the dbproxy rejects client-side CancelRequest).
            cur.execute(_timeout_sql(conn, timeout_ms))
            try:
                # Pass None (not []) when there are no params: psycopg only scans the SQL for
                # placeholders when params is a sequence, and that scan rejects a literal `%` (the
                # `ILIKE 'avo%'` wildcard footgun → "only '%s','%b','%t' are allowed as
                # placeholders"). With None the query is sent verbatim, so inline wildcards just work;
                # parameterised queries (params given) still bind `%s` normally.
                cur.execute(sql, params if params else None)
            except psycopg.errors.UndefinedTable as e:
                # The access policy removed this relation from the requester's projection. That is a
                # permission boundary, not a data gap or a typo, so it gets its own exception type a
                # helper can catch and degrade on — and a message that forbids echoing the name back.
                rel = _undefined_relation(e)
                if rel and _norm_table(rel) in {_norm_table(t) for t in hidden_tables(db)}:
                    raise HiddenTableError(_hidden_table_message(rel), table=rel, db=db) from e
                # A table-not-found on a multi-DB run where db= was OMITTED is usually "wrong database",
                # not a typo: name the standard we silently used + the alternatives so the agent re-runs
                # with db= instead of rewriting against the wrong DB. Otherwise the generic scoping/typo
                # hint applies. `from e` keeps the original traceback.
                std = _defaulted_to_standard(db)
                if std is not None:
                    hint = (
                        f"No db= was passed, so the standard database {std!r} was used — that table isn't "
                        f"there. If it lives in another database, re-run with db=. Databases this run can "
                        f"read:\n{_format_catalog()}"
                    )
                else:
                    hint = _undefined_hint(e, db)
                raise RuntimeError(f"{e}\n\n{hint}") from e
            except psycopg.errors.UndefinedColumn as e:
                # Still undefined after the pre-flight heal ⇒ a typo, a hidden column used in WHERE/
                # ORDER BY (which we don't rewrite), or a shape we couldn't parse. Enrich so the agent
                # fixes the one bad name instead of rewriting. `from e` keeps the original traceback.
                raise RuntimeError(f"{e}\n\n{_undefined_hint(e, db)}") from e
            except psycopg.Error as e:
                # Everything else Postgres (or psycopg itself) rejected: attach one corrective hint
                # for the mistake classes agents repeat — invented enum labels, scalar-vs-array,
                # type mismatches, ambiguous columns, mixed `%` wildcards. Order matters: the
                # undefined table/column clauses above are narrower and must win. The client-side
                # placeholder ProgrammingError is also a psycopg.Error, so this one clause covers
                # both server- and client-side shapes. No hint ⇒ re-raise untouched.
                hint = _mistake_hint(
                    e,
                    conn,
                    timeout_ms,
                    sql=sql if _timeout_hint_stats else None,
                    db=db,
                )
                if hint is None:
                    raise
                raise RuntimeError(f"{e}\n\n{hint}") from e
            if cur.description is None:
                return []
            cols = cur.description
            raw = cur.fetchall()
        # Enum/other unhandled array columns come back from psycopg as the raw literal string
        # ("{parent}"); parse those into lists so callers get a real list everywhere. Built-in
        # arrays already arrive as lists (not str) and so are untouched. The DECISION to parse is
        # the column's array type OID — never the value's shape, since a plain text column may
        # legitimately hold "{not,an,array}". Shape is only a prefilter for whether to spend the
        # (cached, once-per-DSN) pg_type round trip at all, so an array-free query pays nothing.
        may_have_arrays = any(
            isinstance(v, str) and _looks_like_array_literal(v) for row in raw for v in row
        )
        array_oids = _array_oids(conn, dsn) if may_have_arrays else frozenset()
        out: list[dict] = []
        for row in raw:
            d = {}
            for col, val in zip(cols, row):
                if isinstance(val, str) and col.type_code in array_oids:
                    val = _parse_pg_array(val)
                d[col.name] = val
            out.append(d)
        return out


def query_one(sql: str, params: list | tuple | None = None, db: str | None = None) -> dict | None:
    """Run a read-only SELECT and return the first row (or None)."""
    rows = query(sql, params, db=db)
    return rows[0] if rows else None


def tables(schema: str | None = None, db: str | None = None) -> list[dict]:
    """Table names in the run's effective schema.

    ``schema=None`` (default) mirrors ``columns``: introspect ``current_schema()`` so scoped runs see
    their projected ``scope_<id>`` views and flat runs see ``public``. On MySQL the effective schema
    is the DSN's own database (``database()``).

    This is the authoritative "what can I query" list. When the host also told us what the access
    policy REMOVED, a warning names those tables — same treatment `columns` gives hidden columns, so
    a helper learns the boundary here instead of through an `UndefinedTable` mid-investigation.
    """
    if _engine_is_mysql(db):
        rows = query(
            "select table_name as table_name, table_type as table_type "
            "from information_schema.tables "
            "where table_schema = coalesce(%s, database()) "
            "order by table_name",
            [schema],
            db=db,
        )
    else:
        rows = query(
            "select table_name, table_type from information_schema.tables "
            "where table_schema = coalesce(%s::text, current_schema()) "
            "order by table_name",
            [schema],
            db=db,
        )
    for note in _hidden_table_notes(db):
        warnings.warn(note, stacklevel=2)
    return rows


# Every key an agent plausibly reaches for on an introspected column, mapped to the canonical
# attribute. Introspection results used to be raw information_schema dicts, so `column_name` /
# `data_type` stay first-class — the point of this type is that NO reasonable guess raises.
_COLUMN_KEYS = {
    "name": "name",
    "column": "name",
    "column_name": "name",
    "type": "type",
    "dtype": "type",
    "data_type": "type",
    "table": "table",
    "table_name": "table",
}


class Column(str):
    """One introspected column: a ``str`` equal to its NAME, that is also dict- and object-like.

    ``c.lower()``/``str(c)``/``c == "email"`` (it is the name) · ``c.name`` / ``c["name"]`` /
    ``c["column_name"]`` · ``c.type`` / ``c["type"]`` / ``c["data_type"]`` · ``c.table`` /
    ``c["table_name"]`` · ``c.get(k, default)`` · ``dict(c)`` / ``c.to_dict()`` for JSON.
    """

    def __new__(cls, name, data_type=None, table=None):
        self = super().__new__(cls, name)
        self.name = str(name)
        self.type = data_type
        self.table = table
        return self

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name

    def __getitem__(self, key):
        # str keys = mapping access; int/slice keeps plain-str indexing (c[0] == 'e').
        if isinstance(key, str):
            try:
                return getattr(self, _COLUMN_KEYS[key])
            except KeyError:
                raise KeyError(
                    f"{key!r} is not a column field — use one of {sorted(_COLUMN_KEYS)} "
                    f"(or just use the column itself: it IS the name {self.name!r})"
                ) from None
        return str.__getitem__(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> list[str]:
        return ["name", "type"] + (["table"] if self.table else [])

    def values(self) -> list:
        return [self[k] for k in self.keys()]

    def items(self) -> list[tuple]:
        return [(k, self[k]) for k in self.keys()]

    def __contains__(self, item) -> bool:
        # Serves both mental models: dict-like key check AND str-like substring check.
        if isinstance(item, str) and item in _COLUMN_KEYS:
            return True
        return str.__contains__(self, item)

    def to_dict(self) -> dict:
        """Plain dict — for ``json.dumps`` and anything that insists on a mapping."""
        return dict(self.items())

    def __repr__(self) -> str:
        return f"{self.qualified} ({self.type})" if self.type else self.qualified


class ColumnList(list):
    """``list[Column]`` that also reads like the mapping an agent expects from introspection.

    ``cols.keys()`` (names) · ``"email" in cols`` · ``cols["email"]`` → `Column` ·
    ``cols.get("email")`` · ``cols[0]`` / slices stay list access · iterating yields `Column`
    (each one a plain column-name string) · ``print(cols)`` shows an aligned name/type listing.
    """

    def keys(self) -> list[str]:
        return [c.name if isinstance(c, Column) else str(c) for c in self]

    names = keys

    def types(self) -> dict:
        """``{name: data_type}``."""
        return {c.name: c.type for c in self if isinstance(c, Column)}

    def _find(self, name: str):
        for c in self:
            if str(c) == name:
                return c
        low = name.lower()
        for c in self:
            if str(c).lower() == low:
                return c
        return None

    def __getitem__(self, key):
        if isinstance(key, str):
            hit = self._find(key)
            if hit is None:
                raise KeyError(f"no column {key!r} — available: {', '.join(self.keys()) or '(none)'}")
            return hit
        got = list.__getitem__(self, key)
        return ColumnList(got) if isinstance(key, slice) else got

    def get(self, key, default=None):
        hit = self._find(key) if isinstance(key, str) else None
        return default if hit is None else hit

    def __contains__(self, item) -> bool:
        return self._find(item) is not None if isinstance(item, str) else list.__contains__(self, item)

    def to_dicts(self) -> list[dict]:
        """Plain ``list[dict]`` — the pre-`Column` shape, for JSON or a DataFrame."""
        return [c.to_dict() if isinstance(c, Column) else dict(c) for c in self]

    def __repr__(self) -> str:
        if not self:
            return "(no columns — wrong table name, or the schema hides them all)"
        cols = [c if isinstance(c, Column) else Column(c) for c in self]
        width = max(len(c.qualified) for c in cols)
        body = "\n".join(f"  {c.qualified.ljust(width)}  {c.type or ''}".rstrip() for c in cols)
        return f"{len(self)} columns:\n{body}"


def columns(table: str, schema: str | None = None, db: str | None = None) -> "ColumnList":
    """Column names + types for one table — schema introspection when the layout is unknown.

    Returns a `ColumnList` (a ``list`` of `Column`) that answers to every intuitive access pattern:

        cols = db.columns("people")
        print(cols)                     # readable name + type listing
        cols.keys()                     # ['id', 'email', ...]
        "email" in cols                 # True (name membership, case-insensitive)
        cols["email"].type              # 'character varying'
        for c in cols:
            c.lower(), str(c)           # c IS the column name (str subclass)
            c["name"], c["column_name"] # mapping access, legacy key still works
            c["type"], c["data_type"], c.type

    ``schema=None`` (default) introspects the run's EFFECTIVE schema via ``current_schema()`` — the
    same resolution an unqualified table reference uses. On a tenant-scoped run that is the per-run
    ``scope_<id>`` schema of projected views (``public`` is revoked, so a hard-coded ``"public"``
    would see nothing); on a flat project it resolves to ``public`` exactly as before. Pass an
    explicit ``schema`` to override.
    """
    if _engine_is_mysql(db):
        # COLUMN_TYPE, not DATA_TYPE: `varchar(255)` / `int unsigned` carries the length and
        # signedness an agent needs to write a correct predicate.
        rows = query(
            "select column_name as column_name, column_type as data_type "
            "from information_schema.columns "
            "where table_schema = coalesce(%s, database()) and table_name = %s "
            "order by ordinal_position",
            [schema, table],
            db=db,
        )
    else:
        rows = query(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = coalesce(%s::text, current_schema()) and table_name = %s "
            "order by ordinal_position",
            [schema, table],
            db=db,
        )
    _warn_hidden_column_notes(_excluded_map_for_db(db), table=table)
    return ColumnList(
        Column(r["column_name"], r["data_type"], table=table) for r in rows
    )


def tables_with_column(name_like: str, schema: str | None = None, db: str | None = None) -> "ColumnList":
    """Find (table, column) pairs whose column name matches an ILIKE pattern, e.g. ``%email%``.

    The entry point for locating where data lives (an account email, a usage column) when the
    schema isn't pinned down — discover the identifier here, never take it from the ticket.
    ``schema=None`` (default) searches the run's EFFECTIVE schema (``current_schema()``) — the
    ``scope_<id>`` views on a scoped run, ``public`` on a flat project (see `columns`).

    Same `ColumnList` shape as `columns`, with the owning table on each hit: ``c.table`` /
    ``c["table_name"]`` (``c.qualified`` → ``"people.email"``).
    """
    if _engine_is_mysql(db):
        # MySQL's information_schema collation is case-insensitive, so plain LIKE is ILIKE here.
        rows = query(
            "select table_name as table_name, column_name as column_name, "
            "column_type as data_type from information_schema.columns "
            "where table_schema = coalesce(%s, database()) and column_name like %s "
            "order by table_name, column_name",
            [schema, name_like],
            db=db,
        )
    else:
        rows = query(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema = coalesce(%s::text, current_schema()) and column_name ilike %s "
            "order by table_name, column_name",
            [schema, name_like],
            db=db,
        )
    _warn_hidden_column_notes(_excluded_map_for_db(db), pattern=name_like)
    return ColumnList(
        Column(r["column_name"], r["data_type"], table=r["table_name"]) for r in rows
    )


def table_stats(table: str, schema: str | None = None, db: str | None = None) -> dict:
    """Catalog-only planner evidence for one table in the effective schema.

    Returns row estimate + pretty total size, scan/analyze counters, index definitions, and the
    planner's per-column ``n_distinct``/``null_frac``. It never scans the target table.

    On MySQL the counters MySQL doesn't keep (``seq_scan``/``idx_scan``/``last_analyze``) come back
    ``None`` and ``columns`` is empty; ``indexes`` lists each index's key columns instead of a DDL
    definition.
    """
    if _engine_is_mysql(db):
        return _mysql_table_stats(table, schema, db)
    relations = query(
        """select n.nspname as schema, c.relname as table,
                  c.reltuples::bigint as estimated_rows,
                  case when c.relkind in ('r','p','m','i','I','t')
                       then pg_size_pretty(pg_total_relation_size(c.oid)) else '0 bytes' end as total_size,
                  coalesce(s.seq_scan, 0)::bigint as seq_scan,
                  coalesce(s.idx_scan, 0)::bigint as idx_scan,
                  s.last_analyze, s.last_autoanalyze
           from pg_class c
           join pg_namespace n on n.oid = c.relnamespace
           left join pg_stat_user_tables s on s.relid = c.oid
           where n.nspname = coalesce(%s::text, current_schema())
             and c.relname = %s and c.relkind in ('r','p','v','m','f')
           limit 1""",
        [schema, table],
        db=db,
        _timeout_hint_stats=False,
    )
    if not relations:
        raise RuntimeError(
            f"table {table!r} not found in {schema or 'the effective schema'}; "
            "use lib.db.tables() to list queryable tables"
        )
    base = relations[0]
    resolved_schema = base["schema"]
    index_rows = query(
        """select indexname as name, indexdef as definition
           from pg_indexes where schemaname = %s and tablename = %s order by indexname""",
        [resolved_schema, table],
        db=db,
        _timeout_hint_stats=False,
    )
    column_rows = query(
        """select attname as column, n_distinct, null_frac
           from pg_stats where schemaname = %s and tablename = %s order by attname""",
        [resolved_schema, table],
        db=db,
        _timeout_hint_stats=False,
    )
    return {
        "schema": resolved_schema,
        "table": base["table"],
        "estimated_rows": base["estimated_rows"],
        "total_size": base["total_size"],
        "seq_scan": base["seq_scan"],
        "idx_scan": base["idx_scan"],
        "last_analyze": base["last_analyze"],
        "last_autoanalyze": base["last_autoanalyze"],
        "indexes": index_rows,
        "columns": column_rows,
    }


def _mysql_table_stats(table: str, schema: str | None, db: str | None) -> dict:
    """`table_stats` on MySQL: information_schema only, so the target table is never scanned."""
    relations = query(
        "select table_schema as `schema`, table_name as `table`, "
        "table_rows as estimated_rows, data_length as data_length, "
        "index_length as index_length, update_time as update_time "
        "from information_schema.tables "
        "where table_schema = coalesce(%s, database()) and table_name = %s limit 1",
        [schema, table],
        db=db,
        _timeout_hint_stats=False,
    )
    if not relations:
        raise RuntimeError(
            f"table {table!r} not found in {schema or 'the effective schema'}; "
            "use lib.db.tables() to list queryable tables"
        )
    base = relations[0]
    resolved_schema = base["schema"]
    index_rows = query(
        "select index_name as name, "
        "concat(if(min(non_unique) = 0, 'UNIQUE ', ''), 'KEY (', "
        "group_concat(column_name order by seq_in_index separator ', '), ')') as definition "
        "from information_schema.statistics "
        "where table_schema = %s and table_name = %s "
        "group by index_name order by index_name",
        [resolved_schema, table],
        db=db,
        _timeout_hint_stats=False,
    )
    data_length = base["data_length"] or 0
    index_length = base["index_length"] or 0
    return {
        "schema": resolved_schema,
        "table": base["table"],
        # A VIEW (what a scoped run actually queries) has no row/byte accounting at all.
        "estimated_rows": base["estimated_rows"],
        "total_size": _pretty_bytes(data_length + index_length),
        "data_size": _pretty_bytes(data_length),
        "index_size": _pretty_bytes(index_length),
        "seq_scan": None,
        "idx_scan": None,
        "last_analyze": base["update_time"],
        "last_autoanalyze": None,
        "indexes": index_rows,
        "columns": [],
    }


def explain(sql: str, params: list | tuple | None = None, db: str | None = None) -> str:
    """Return ``EXPLAIN (FORMAT TEXT)`` output without executing ``ANALYZE``.

    MySQL uses ``EXPLAIN FORMAT=TREE`` on 8.0.16+ servers, tabular ``EXPLAIN`` below that (checked
    up front by version, not by a failed FORMAT=TREE round trip) or if it's rejected anyway. Never
    ``ANALYZE`` on either engine — that would run the query.
    """
    if re.match(r"(?is)^\s*(?:analyze\b|explain\b[^;]*\banalyze\b)", sql):
        raise RuntimeError("lib.db.explain() never runs ANALYZE; pass the underlying SELECT only")
    if _engine_is_mysql(db):
        version = _mysql_server_version(db)
        # FORMAT=TREE is 8.0.16+; below that (or if the version is unknown) go straight to tabular
        # rather than pay a failed round trip we already know will be rejected.
        if version == (0, 0, 0) or version >= _MYSQL_MIN_EXPLAIN_TREE:
            try:
                rows = query("EXPLAIN FORMAT=TREE " + sql, params, db=db)
                return "\n".join(str(next(iter(row.values()))) for row in rows)
            except Exception:  # noqa: BLE001 - SELECT-only / server quirks; degrade cleanly.
                pass
        rows = query("EXPLAIN " + sql, params, db=db)
        return "\n".join(
            ", ".join(f"{k}={v}" for k, v in row.items() if v is not None) for row in rows
        )
    rows = query("EXPLAIN (FORMAT TEXT) " + sql, params, db=db)
    return "\n".join(str(next(iter(row.values()))) for row in rows)


# Affordance aliases for common model guesses. Keep these thin so the canonical helpers stay the
# contract while muscle-memory names still land on the read-only path.
sql = query
select = query
one = query_one
first = query_one
list_databases = databases
database_names = databases
list_tables = tables
table_names = tables
schema = columns
describe_table = columns
table_info = columns
find_columns = tables_with_column
tables_by_column = tables_with_column
stats = table_stats
indexes = table_stats


def _parse_duration_ms(s: str) -> int:
    """Parse a duration like ``30s`` / ``2min`` / ``500ms`` / ``1m`` into milliseconds."""
    s = s.strip().lower()
    for suffix, mult in (("ms", 1), ("min", 60_000), ("s", 1000), ("m", 60_000), ("h", 3_600_000)):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(float(s) * 1000)  # bare number = seconds


def _main(argv=None) -> int:
    import argparse

    from . import _output

    p = argparse.ArgumentParser(prog="python -m lib.db", description=__doc__.split("\n")[0])
    p.add_argument("sql", nargs="?", help="SQL to run (read-only transaction).")
    p.add_argument("--db", help="Database: short name, env-var name, or raw DSN. Omit if only one.")
    p.add_argument("--format", choices=("csv", "json", "table"), default="csv")
    p.add_argument(
        "--timeout",
        help=(
            "statement_timeout, e.g. 30s, 2min "
            f"(default {DEFAULT_TIMEOUT_MS / 1000:g}s, hard cap {MAX_TIMEOUT_MS / 1000:g}s)."
        ),
    )
    p.add_argument("--list", action="store_true", help="List available databases and exit.")
    p.add_argument("--stats", metavar="TABLE", help="Show catalog-only size/scan/index/column stats.")
    p.add_argument(
        "--explain", action="store_true", help="Print EXPLAIN (FORMAT TEXT) for the positional SQL."
    )
    args = p.parse_args(argv)

    if args.list:
        print(_format_catalog())
        for env in databases():
            hidden = sorted(hidden_tables(env))
            if hidden:
                print(f"  hidden for this requester ({_short_name(env)}): {', '.join(hidden)}")
        return 0
    try:
        if args.stats:
            if args.sql or args.explain:
                p.error("--stats cannot be combined with SQL or --explain")
            schema_name, table = (args.stats.rsplit(".", 1) if "." in args.stats else (None, args.stats))
            _output.emit_rows(
                [table_stats(table, schema=schema_name, db=args.db)], args.format, label="db stats"
            )
            return 0
        if not args.sql:
            p.error("provide SQL, --stats TABLE, or --list")
        if args.explain:
            print(explain(args.sql, db=args.db))
            return 0
        timeout_ms = _parse_duration_ms(args.timeout) if args.timeout else DEFAULT_TIMEOUT_MS
        rows = query(args.sql, db=args.db, timeout_ms=timeout_ms)
        _output.emit_rows(rows, args.format, label="db")
        return 0
    except RuntimeError as exc:
        import sys

        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
