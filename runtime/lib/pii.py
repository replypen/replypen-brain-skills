"""PII masking tokens as a helper sees them, plus a test-side masker for fake DB rows.

In production the DB proxy replaces every declared-PII column with a ``⟦pii:<label>:<10 hex>⟧``
string before the row reaches Python (a ``date`` column arrives as that *string*, not a date). So a
helper never parses a masked column: it computes on it in SQL (``date_part('year', birthdate)``,
``birthdate < %s``) and reads back a verdict or a coarse value. When a value still arrives as a
token, say so (``is_token``) instead of silently rendering "—".

``mask`` / ``mask_row`` reproduce the proxy's output on fake rows so offline tests see what prod
sees; the ``pii_rows`` pytest fixture (``lib.pii_pytest``) runs an opted-in test once plain, once
masked.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import inspect
import re
from collections.abc import Iterable, Mapping

# Same shape the proxy mints: ⟦pii:<column label>:<10 hex>⟧, optionally with a display prefix
# (⟦pii:<kind>_<id>:<label>:<hex>⟧).
TOKEN_RE = re.compile(r"⟦pii:(?:[a-z][a-z0-9]*_[A-Za-z0-9-]+:)?([a-z0-9_]+):([0-9a-f]{10})⟧")

# The host's person-attribute fields. A project's declared DB columns differ (KampAdmin: firstname,
# telephone, city_manual, …): a brain overrides the ``pii_columns`` fixture in its conftest.
DEFAULT_COLUMNS = frozenset({"email", "first_name", "last_name", "phone", "birthdate", "street", "city"})

_DB_METHODS = frozenset({"query", "query_one", "query_all", "rows", "optional_rows", "fetch", "fetchall", "fetchone"})


def is_token(value: object) -> bool:
    """True when ``value`` is exactly one masking token (a masked column), not real data."""
    return isinstance(value, str) and TOKEN_RE.fullmatch(value) is not None


def token(label: str, value: object) -> str:
    """A realistic token for ``value`` read under column/alias ``label`` (deterministic, like prod)."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        value = value.isoformat()
    h = hashlib.sha256(str(value).strip().lower().encode()).hexdigest()[:10]
    return f"⟦pii:{re.sub(r'[^a-z0-9_]+', '', label.lower())}:{h}⟧"


def _declared(key: str, columns: Iterable[str]) -> str | None:
    """The declared column behind ``key``: itself, or the column an alias like ``participant_birthdate``
    ends in. The proxy labels a token by the SOURCE column, so both read the same token."""
    k = key.lower()
    return next((c for c in columns if k == c or k.endswith("_" + c)), None)


def mask_row(row: Mapping, columns: Iterable[str] = DEFAULT_COLUMNS) -> dict:
    """Copy of ``row`` with each declared column (or ``<alias>_<column>``) replaced as the proxy does:
    NULL stays NULL, an array is masked element-wise, anything else becomes one token."""
    columns = tuple(columns)
    out = dict(row)
    for key, value in row.items():
        column = _declared(key, columns) if isinstance(key, str) else None
        if value is None or column is None:
            continue
        if isinstance(value, (list, tuple)):
            out[key] = [None if v is None else token(column, v) for v in value]
        else:
            out[key] = token(column, value)
    return out


class _MaskedDB:
    """Wraps a fake ``db`` object: its query methods return masked rows; everything else passes through."""

    def __init__(self, inner, columns):
        self._inner, self._columns = inner, columns

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in _DB_METHODS and callable(attr):
            return lambda *a, **k: mask(attr(*a, **k), self._columns)
        return attr

    def __call__(self, *a, **k):  # a callable fake keeps working as a fetch function too
        return mask(self._inner(*a, **k), self._columns)


def _defines_db_method(obj) -> bool:
    """A query method the fake really defines (class or instance), not one a Mock synthesizes."""
    if isinstance(obj, type):
        owners = obj.__mro__
    else:
        owners = [obj, *([] if inspect.ismodule(obj) else type(obj).__mro__)]
    return any(m in getattr(o, "__dict__", {}) for o in owners for m in _DB_METHODS)


def mask(obj, columns: Iterable[str] = DEFAULT_COLUMNS):
    """Mask whatever a test feeds a helper: a row, a list of rows, a function returning rows, or a
    fake ``db`` object (its ``query``/``query_one``/``rows``… results). Other values pass through."""
    columns = tuple(columns)
    if isinstance(obj, Mapping):
        return mask_row(obj, columns)
    if isinstance(obj, list):
        return [mask(r, columns) for r in obj]
    if isinstance(obj, tuple):
        return tuple(mask(r, columns) for r in obj)
    if _defines_db_method(obj):
        return _MaskedDB(obj, columns)
    if callable(obj) and not isinstance(obj, type):
        return lambda *a, **k: mask(obj(*a, **k), columns)
    if hasattr(obj, "__next__"):
        return (mask(r, columns) for r in obj)
    return obj
