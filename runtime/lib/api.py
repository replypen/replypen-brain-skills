"""Generic read-tier REST client — the ``lib.db`` of third-party HTTP integrations.

Most integrations need NO bespoke Python. A compact declarative manifest row (base URL, auth
strategy, pagination style, rate-limit header) plus this caller is enough: the agent runs

    python -m lib.api get <key> <path> [--query k=v ...] [--paginate] [--pick a.b,c]
    python -m lib.api post <key> <path> --json '{"filter":...}' [--pick a.b,c]

and pipes the JSON through ``jq``/``rg`` so a raw API dump never floods the model context. A
dedicated thin connector (``lib.connectors.<x>``) is added ONLY when the integration trips a
force-code trigger: field pre-selection, a multi-call join, exotic auth/signing, non-standard
pagination, or a search DSL (see the integrations skill). Such a connector imports THIS module —
it never re-implements retry/pagination/rate-limiting.

Design posture (mirrors ``lib.db``): read-tier calls only (GET is always allowed; non-mutating POSTs
must be allowlisted in the connector manifest; PUT/PATCH/DELETE are refused with action-plane
guidance), raise LOUDLY with the failing provider detail on error rather than returning a silent
empty/partial, and take the credential from ``lib.oauth.token`` so it never lands in argv, logs, or
model context.

One exception to read-tier: a manifest's ``write_endpoints`` (verb → path patterns + a ``consent``
name) lets a BROKERED run mutate those paths, because the host broker enforces the per-resource consent
on every call (e.g. ``google_drive_share``: the file is shared with ReplyPen's address as Editor). A
non-brokered client has no such gate, so it still refuses.

Env-token auth is the credential VALUE injected as ``RC_CONN_<KEY>`` plus a manifest-declared
*strategy* that says where it goes (bearer header / basic / api-key header / query param / oauth2
client-credentials). Brokered connectors route through ``http://rc-broker.internal/<key>/...`` and
attach no credential client-side. An API key always goes in a HEADER, never the query string.

``requests`` is imported lazily so ``from lib import api`` loads even where it isn't installed (the
manifest helpers and CLI ``--help`` work on a bare host).
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator
from urllib.parse import quote, urljoin, urlsplit

from lib import _http_audit, oauth

# Both halves of the requests timeout tuple (connect, read) — never None, so a hung server can't
# wedge a run. Read is generous because we optimise for thoroughness, not latency.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0

# Retry only read-tier methods by default. GET/HEAD/OPTIONS are intrinsically read-tier here; POST
# becomes read-tier only when the manifest allowlists that path as a non-mutating endpoint. A write
# call may opt into retries by passing an idempotency key.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Transient statuses worth retrying. 429 is handled separately (honours Retry-After).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 0.5  # seconds; first retry waits up to this, then doubles (capped)
DEFAULT_BACKOFF_CAP = 30.0
# A single 429/Retry-After wait longer than this is split into capped sleeps so a misbehaving
# server can't pin a run for an unbounded stretch in one call.
MAX_RETRY_AFTER = 120.0

# Hard ceiling on auto-paging so a runaway cursor (server bug, our bug) can't loop forever.
DEFAULT_MAX_PAGES = 1000

_BROKER_BASE_URL = "http://rc-broker.internal"
_BROKERED_KEYS_ENV = "RC_API_BROKERED_KEYS"
_ROSTER_ENVS = ("RC_INTEGRATIONS", "RC_ACTIVE_INTEGRATIONS", "RC_CONNECTIONS")


class ApiError(RuntimeError):
    """One normalized exception for every non-2xx — carries status + the provider error body.

    Mirrors ``lib.db``'s raise-loud posture: the caller sees the failing detail (status + the
    provider's own error JSON/text, truncated) instead of a silent fallback. ``retryable`` lets the
    retry layer decide without re-parsing.

    ``code``/``message`` are set only for a host-broker denial (``{"error":{"code":"<SYMBOL>",
    "message":…}}`` with a STRING code, e.g. ``DELEGATED_WRITE_DENIED``); the message then leads with
    them verbatim because it carries the exact ask for the user. Provider errors keep numeric codes.
    """

    def __init__(self, status: int, body: str, *, url: str = "", retryable: bool = False):
        self.status = status
        self.body = body
        self.url = url
        self.retryable = retryable
        self.code, self.message = _broker_error(body)
        detail = (body or "").strip()
        if len(detail) > 800:
            detail = detail[:800] + "…(truncated)"
        where = f" for {url}" if url else ""
        if self.code:
            super().__init__(f"HTTP {status} {self.code}: {self.message}")
        else:
            super().__init__(f"HTTP {status}{where}: {detail}" if detail else f"HTTP {status}{where}")


def _broker_error(body: str) -> tuple[str, str]:
    try:
        err = json.loads(body or "").get("error")
    except (ValueError, AttributeError):
        return "", ""
    if not isinstance(err, dict):
        return "", ""
    code = err.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", code):
        return "", ""
    return code, str(err.get("message") or "")


class MethodPolicyError(RuntimeError):
    """The requested HTTP method/path is outside the run-time read-tier policy."""


_WRITE_VERB_HINT = (
    "write verbs are action-plane only - get a client via lib.action.client(...) for a "
    "human-confirmed action"
)


@dataclass(frozen=True)
class Auth:
    """How the injected credential is presented. ``strategy`` is the manifest enum; ``name`` is the
    header/param name where it varies (api-key header name, query-param name)."""

    strategy: str = "bearer"  # bearer | basic | api_key_header | query_param | oauth2_client_credentials | none
    name: str = "Authorization"  # header name (api_key_header) or query key (query_param)
    # basic: the credential value is "user:pass"; if it has no ":", it's the username with empty pass.


@dataclass(frozen=True)
class Pagination:
    """Manifest-driven paging. The framework runs the while-has-more loop, not the connector.

    - ``none``    : single page.
    - ``cursor``  : response carries an opaque next token at ``cursor_field`` (dotted path); sent back
                    as the ``cursor_param`` query arg. ``has_more_field`` (optional) gates the loop;
                    absent ⇒ loop until the cursor field is empty.
    - ``offset``  : send ``offset_param`` advancing by the page length, ``limit_param`` = ``page_size``.
    - ``link``    : follow RFC 8288 ``Link: <url>; rel="next"`` headers; opaque server URL.
    - ``body_url``: like ``link`` but the next URL lives INSIDE the JSON body at ``next_url_field``
                    (e.g. ``meta.pagination.next``, ``links.next``, ``@odata.nextLink``) instead of a
                    header. The value may be absolute (used verbatim) or relative (``_join``ed onto
                    ``base_url``). Generalises the header-based ``link`` style to the body.
    - ``page``    : numeric page-number paging — ``page_param`` starts at ``page_start`` and increments
                    by 1 (NOT by item count, unlike ``offset``); ``limit_param`` = ``page_size``. Stops
                    on a short/empty page, or on ``has_more_field``=false when the API declares one
                    (some APIs clamp/repeat the last full page past the end). ``page_start`` is 0 for
                    0-based APIs (e.g. ClickUp).
    """

    style: str = "none"  # none | cursor | offset | link | body_url | page
    cursor_field: str = ""
    cursor_param: str = "cursor"
    has_more_field: str = ""
    items_field: str = ""  # dotted path to the list inside each page (e.g. "data"); "" ⇒ page is the list
    offset_param: str = "offset"
    limit_param: str = "limit"
    page_size: int = 100
    next_url_field: str = ""  # body_url: dotted path (or whole-key literal) to the next URL/path
    page_param: str = "page"  # page: the page-number query arg
    page_start: int = 1  # page: first page number (0 for 0-based APIs like ClickUp)


@dataclass(frozen=True)
class Manifest:
    """The compact declarative row that lets one integration work with zero bespoke code.

    Mirrors the host ``integration_catalog`` columns (base_url, auth, pagination, rate_limit header)
    so a connector and the Go catalog describe the same shape.
    """

    key: str
    base_url: str
    auth: Auth = field(default_factory=Auth)
    pagination: Pagination = field(default_factory=Pagination)
    rate_limit_remaining_header: str = ""  # e.g. "X-RateLimit-Remaining"; advisory pacing only
    default_headers: dict[str, str] = field(default_factory=dict)
    allowed_post_paths: tuple[str, ...] = ()
    brokered: bool = False
    # Delegated writes: upper-case verb → path patterns, honoured only on a brokered client.
    write_endpoints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    write_consent: str = ""


@dataclass
class Page:
    """One fetched page: parsed ``body``, the ``items`` list extracted per the manifest, and the
    ``next`` continuation token/URL (None when exhausted)."""

    body: Any
    items: list
    next: Any | None


# ---------------------------------------------------------------------------
# Field extraction (response shaping)
# ---------------------------------------------------------------------------


def pick(obj: Any, paths: str | list[str]) -> dict:
    """Select a few dotted paths out of an object — the token-saving field pre-selector.

    ``paths`` is a comma-string ("customer.email,subscriptions.data.0.status") or a list. Each leaf
    is keyed by its FULL path so two paths never collide. A missing path is simply absent (selection,
    not validation). List indices are numeric segments; ``*`` maps over every element of a list.

    Example: ``pick(invoice, "id,status,lines.data.*.amount")`` →
    ``{"id": "...", "status": "...", "lines.data.*.amount": [100, 250]}``.
    """
    if isinstance(paths, str):
        wanted = [p.strip() for p in paths.split(",") if p.strip()]
    else:
        wanted = [str(p).strip() for p in paths if str(p).strip()]
    out: dict[str, Any] = {}
    for p in wanted:
        found, value = _dget(obj, p.split("."))
        if found:
            out[p] = value
    return out


def _dget(obj: Any, segments: list[str]) -> tuple[bool, Any]:
    """Resolve a dotted path. Returns ``(found, value)``; ``*`` maps over a list. ``found`` is False
    for a missing key/index so ``pick`` can omit it rather than inventing ``None``."""
    if not segments:
        return True, obj
    seg, rest = segments[0], segments[1:]
    if seg == "*":
        if not isinstance(obj, list):
            return False, None
        collected = []
        any_found = False
        for el in obj:
            ok, val = _dget(el, rest)
            if ok:
                any_found = True
                collected.append(val)
        return any_found, collected
    if isinstance(obj, dict):
        if seg not in obj:
            return False, None
        return _dget(obj[seg], rest)
    if isinstance(obj, list):
        try:
            idx = int(seg)
        except ValueError:
            return False, None
        if idx < 0 or idx >= len(obj):
            return False, None
        return _dget(obj[idx], rest)
    return False, None


def _dotted(obj: Any, path: str) -> Any:
    """Single-path dotted lookup returning the value or None — for internal manifest fields
    (cursor_field, has_more_field, items_field) where 'absent' and 'null' collapse to the same."""
    if not path:
        return obj
    found, value = _dget(obj, path.split("."))
    return value if found else None


def _next_url_in_body(body: Any, field: str) -> Any:
    """Resolve the body_url next-page field. Tries the WHOLE field as a single literal key first so
    a dotted-but-not-nested key like ``@odata.nextLink`` resolves, then falls back to dotted
    traversal (``meta.pagination.next``)."""
    if not field:
        return None
    if isinstance(body, dict) and field in body:  # literal key (e.g. "@odata.nextLink")
        return body[field]
    return _dotted(body, field)


# ---------------------------------------------------------------------------
# Backoff / rate-limit timing
# ---------------------------------------------------------------------------


def _full_jitter(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Decorrelated/full-jitter backoff: sleep is uniform in [0, min(cap, base*2**attempt)].

    Full jitter (AWS "Exponential Backoff and Jitter") avoids the thundering-herd of a fixed
    schedule. ``attempt`` is 0-based for the first retry.
    """
    ceiling = min(cap, base * (2 ** attempt))
    return rng.uniform(0, ceiling)


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse a ``Retry-After`` header into a delay in seconds.

    Honours BOTH forms in RFC 9110 §10.2.3: a delay in seconds ("120") AND an HTTP-date
    ("Wed, 21 Oct 2026 07:28:00 GMT") — the seconds-only bug is common and starves the
    date form. Returns None when absent/unparseable (caller falls back to jittered backoff);
    a past/negative date clamps to 0.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    # Delay-seconds form (may be a float in the wild though the RFC says integer).
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    # HTTP-date form.
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    target = dt.timestamp()
    current = time.time() if now is None else now
    return max(0.0, target - current)


def _sleep(seconds: float, sleeper: Callable[[float], None]) -> None:
    """Sleep, but never in one unbounded chunk: a hostile ``Retry-After`` is split into
    ``MAX_RETRY_AFTER`` slices so a run can't be pinned arbitrarily by one response."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, MAX_RETRY_AFTER)
        sleeper(chunk)
        remaining -= chunk


# ---------------------------------------------------------------------------
# Core request with retry at ONE layer
# ---------------------------------------------------------------------------


@dataclass
class Client:
    """A configured caller for one integration. Holds the manifest + resolved credential; methods do
    the read. Retry/backoff/rate-limit live HERE and nowhere else (retry at one layer only)."""

    manifest: Manifest
    credential: str = ""
    allow_writes: bool = False
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    max_pages: int = DEFAULT_MAX_PAGES
    # Injected only so tests can pin timing/jitter; production uses the real clock + RNG.
    _sleeper: Callable[[float], None] = time.sleep
    _rng: random.Random = field(default_factory=lambda: random.Random())

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: Any | None = None,
        files: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_header: str = "Idempotency-Key",
        endpoint_template: str | None = None,
    ) -> Any:
        """Make one HTTP call (following retries/rate-limits) and return parsed JSON.

        ``path`` is joined onto the manifest ``base_url`` (an absolute URL overrides). Raises
        ``ApiError`` on a non-2xx after exhausting retries — never a silent empty.
        """
        resp = self._send(
            method,
            path,
            query=query,
            headers=headers,
            json_body=json_body,
            data=data,
            files=files,
            idempotency_key=idempotency_key,
            idempotency_header=idempotency_header,
            endpoint_template=endpoint_template,
        )
        return _parse_json(resp)

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Any:
        _normalize_json_alias(kw)
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw) -> Any:
        _normalize_json_alias(kw)
        return self.request("PATCH", path, **kw)

    def put(self, path: str, **kw) -> Any:
        _normalize_json_alias(kw)
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw) -> Any:
        _normalize_json_alias(kw)
        return self.request("DELETE", path, **kw)

    def upload(
        self,
        path: str,
        *,
        data: bytes,
        content_type: str,
        metadata: dict | None = None,
        media_param: str = "uploadType",
        media_style: str = "multipart",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        idempotency_header: str = "Idempotency-Key",
    ) -> Any:
        """Upload bytes through the same auth/retry/error path as other action writes.

        ``multipart`` emits the Google-style ``multipart/related`` body: JSON metadata part followed
        by the binary media part. ``media`` sends raw bytes as the request body.
        """
        q = dict(query or {})
        h = dict(headers or {})
        if media_style == "media":
            q.setdefault(media_param, "media")
            h.setdefault("Content-Type", content_type)
            body = data
        elif media_style == "multipart":
            q.setdefault(media_param, "multipart")
            boundary = f"rc-action-{self._rng.getrandbits(64):016x}"
            meta = json.dumps(metadata or {}, separators=(",", ":")).encode("utf-8")
            body = b"".join(
                [
                    f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                    meta,
                    b"\r\n",
                    f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode(),
                    data,
                    b"\r\n",
                    f"--{boundary}--\r\n".encode(),
                ]
            )
            h.setdefault("Content-Type", f'multipart/related; boundary="{boundary}"')
        else:
            raise ValueError("media_style must be 'multipart' or 'media'")
        return self.request(
            "POST",
            path,
            query=q,
            headers=h,
            data=body,
            idempotency_key=idempotency_key,
            idempotency_header=idempotency_header,
        )

    def _send(
        self,
        method,
        path,
        *,
        query=None,
        headers=None,
        json_body=None,
        data=None,
        files=None,
        idempotency_key: str | None = None,
        idempotency_header: str = "Idempotency-Key",
        endpoint_template: str | None = None,
    ):
        verb = method.upper()
        read_tier = self._assert_method_allowed(verb, path)
        url = self._request_url(path)
        audit_url = self._audit_url(path, request_url=url)
        req_headers = dict(self.manifest.default_headers)
        req_headers.update(headers or {})
        if idempotency_key:
            req_headers.setdefault(idempotency_header or "Idempotency-Key", idempotency_key)
        req_query = dict(query or {})
        self._apply_auth(req_headers, req_query)
        retry_allowed = bool(idempotency_key) or ((verb in _IDEMPOTENT_METHODS or read_tier) and not files)

        attempt = 0
        reason = "initial"
        while True:
            resp = _http_audit.request(
                verb,
                url,
                params=req_query,
                headers=req_headers,
                json_body=json_body,
                data=data,
                files=files,
                timeout=(self.connect_timeout, self.read_timeout),
                attempt=attempt + 1,
                reason=reason,
                endpoint_template=endpoint_template,
                audit_url=audit_url,
                known_secrets=(self.credential,),
            )
            if 200 <= resp.status_code < 300:
                return resp
            retryable = resp.status_code in _RETRYABLE_STATUS and retry_allowed
            if not retryable or attempt >= self.max_retries:
                raise ApiError(
                    resp.status_code,
                    _body_text(resp),
                    url=url,
                    retryable=resp.status_code in _RETRYABLE_STATUS,
                )
            delay = self._retry_delay(resp, attempt)
            _sleep(delay, self._sleeper)
            reason = f"retry_status_{resp.status_code}"
            attempt += 1

    def _assert_method_allowed(self, verb: str, path: str) -> bool:
        if self.allow_writes:
            if verb in _STATE_CHANGING_METHODS or verb in _IDEMPOTENT_METHODS:
                return verb in _IDEMPOTENT_METHODS
            raise MethodPolicyError(f"HTTP method {verb!r} is not supported by lib.api")
        return self._assert_read_tier(verb, path)

    def _assert_read_tier(self, verb: str, path: str) -> bool:
        """Run-tier policy. True = read-tier (retry-safe); False = an allowed delegated write."""
        if verb in _IDEMPOTENT_METHODS:
            return True
        if verb == "POST" and _matches_any_path(path, self.manifest.allowed_post_paths):
            return True
        if self._is_delegated_write(verb, path):
            if not self.manifest.brokered:
                raise MethodPolicyError(
                    f"{verb} {path!r} is a delegated write for integration {self.manifest.key!r}, but this "
                    "connection is not brokered this run, so its consent "
                    f"({self.manifest.write_consent or 'host'}) cannot be checked. Writes need the host "
                    "broker; locally, or through a customer-connected account, it is read-only."
                )
            return False
        if verb == "POST":
            allowed = ", ".join(self.manifest.allowed_post_paths) or "(none)"
            raise MethodPolicyError(
                f"POST {path!r} is not in integration {self.manifest.key!r}'s read-only POST allowlist "
                f"{allowed}. If this endpoint is truly non-mutating search/list/RPC, add it to the "
                "connector manifest's read_endpoints.post allowlist. Otherwise use the action plane "
                "for a human-confirmed state-changing workflow."
            )
        if verb in _STATE_CHANGING_METHODS:
            raise MethodPolicyError(_WRITE_VERB_HINT)
        raise MethodPolicyError(f"HTTP method {verb!r} is not supported by lib.api read-tier grounding")

    def _is_delegated_write(self, verb: str, path: str) -> bool:
        patterns = self.manifest.write_endpoints.get(verb, ())
        return bool(patterns) and _matches_any_path(path, patterns, base_url=self.manifest.base_url)

    def _retry_delay(self, resp, attempt: int) -> float:
        """Wait before the next retry: honour ``Retry-After`` on a 429 (capped), else jittered
        exponential backoff."""
        if resp.status_code == 429:
            ra = parse_retry_after(resp.headers.get("Retry-After"))
            if ra is not None:
                return min(ra, MAX_RETRY_AFTER)
        return _full_jitter(attempt, self.backoff_base, self.backoff_cap, self._rng)

    def _apply_auth(self, headers: dict, query: dict) -> None:
        """Place the credential per the manifest auth strategy. An api-key always goes in a HEADER;
        ``query_param`` exists only for APIs that genuinely require it and is used sparingly."""
        if self.manifest.brokered:
            return
        cred = self.credential
        a = self.manifest.auth
        strat = a.strategy
        if self.allow_writes and strat == "query_param":
            raise RuntimeError(
                f"integration {self.manifest.key!r} uses query-param auth, which is refused for "
                "action writes because it would put credentials in URLs"
            )
        if strat == "none" or not cred:
            if strat not in ("none", ""):
                raise RuntimeError(
                    f"integration {self.manifest.key!r} needs a credential for auth strategy "
                    f"{strat!r} but none is set"
                )
            return
        if strat == "bearer":
            headers.setdefault("Authorization", f"Bearer {cred}")
        elif strat == "basic":
            import base64

            user, _, pw = cred.partition(":")
            token = base64.b64encode(f"{user}:{pw}".encode()).decode()
            headers.setdefault("Authorization", f"Basic {token}")
        elif strat == "api_key_header":
            headers.setdefault(a.name or "Authorization", cred)
        elif strat == "query_param":
            query.setdefault(a.name or "api_key", cred)
        elif strat == "oauth2_client_credentials":
            # The host injects an already-minted bearer for the client-credentials grant; we present
            # it as a bearer. Minting/refresh is a host concern, not the runtime's.
            headers.setdefault("Authorization", f"Bearer {cred}")
        else:
            raise RuntimeError(f"unknown auth strategy {strat!r} for integration {self.manifest.key!r}")

    # -- pagination -------------------------------------------------------

    def fetch_page(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Page:
        """Fetch one page and extract its items + next token per the manifest pagination style."""
        resp = self._send(method, path, query=query, headers=headers, json_body=json_body, data=data, files=files)
        body = _parse_json(resp)
        items = self._extract_items(body)
        nxt = self._next_token(body, resp)
        return Page(body=body, items=items, next=nxt)

    def paginate(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        max_pages: int | None = None,
    ) -> Iterator[Page]:
        """Auto-page: yield ``Page`` objects following the server's opaque next token until exhausted.

        Fail-safe partial results: if a page mid-stream errors, the ``ApiError`` propagates — but a
        caller that wants what-it-has-so-far should use ``collect`` (which sets an ``incomplete``
        flag) rather than swallowing the error here. The framework owns the loop, so a connector
        never hand-rolls a ``while has_more``.
        """
        p = self.manifest.pagination
        verb = method.upper()
        if verb != "GET" and p.style != "none":
            raise MethodPolicyError(
                f"integration {self.manifest.key!r}: generic {verb} pagination is not supported by "
                "lib.api. Use a single-page POST, or add a connector script for POST pagination that "
                "needs cursor placement in the body, a repeated body, or a different continuation "
                "endpoint."
            )
        cap = self.max_pages if max_pages is None else max_pages
        base_query = dict(query or {})
        seen = 0
        if p.style == "none":
            yield self.fetch_page(path, method=method, query=base_query, headers=headers, json_body=json_body, data=data, files=files)
            return
        if p.style == "offset":
            base_query.setdefault(p.limit_param, p.page_size)
            offset = int(base_query.get(p.offset_param, 0) or 0)
            while seen < cap:
                q = dict(base_query, **{p.offset_param: offset})
                page = self.fetch_page(path, method=method, query=q, headers=headers, json_body=json_body, data=data, files=files)
                yield page
                seen += 1
                if not page.items or len(page.items) < p.page_size:
                    return
                offset += len(page.items)
            raise ApiError(0, f"pagination reached max_pages={cap}", url=_join(self.manifest.base_url, path))
        if p.style == "page":
            # Page-NUMBER paging: advance page_param by 1 from page_start (not by item count).
            base_query.setdefault(p.limit_param, p.page_size)
            page_num = int(base_query.get(p.page_param, p.page_start) or p.page_start)
            while seen < cap:
                q = dict(base_query, **{p.page_param: page_num})
                page = self.fetch_page(path, method=method, query=q, headers=headers, json_body=json_body, data=data, files=files)
                yield page
                seen += 1
                # Stop on a short/empty page; or, when the API declares one, an explicit
                # has_more_field=false — some page-number APIs clamp/repeat the last FULL page past
                # the end instead of returning a short page, so the size check alone wouldn't fire.
                if not page.items or len(page.items) < p.page_size:
                    return
                if p.has_more_field and not _truthy(_dotted(page.body, p.has_more_field)):
                    return
                page_num += 1
            raise ApiError(0, f"pagination reached max_pages={cap}", url=_join(self.manifest.base_url, path))
        # cursor / link / body_url all drive off page.next (an opaque token or URL). For link and
        # body_url, page.next is a URL/path we follow directly (a relative path is _joined to base_url
        # inside _send_url); for cursor it's a token re-sent as a query param.
        next_token: Any | None = None
        next_url: str | None = None
        # The host we pin pagination follows to is the ORIGIN request's host (where the agent actually
        # aimed this call), not the manifest base_url — which may be a multi-tenant placeholder
        # (e.g. woocommerce `{store_url}`) the agent overrides with an absolute path.
        origin_url = _join(self.manifest.base_url, path)
        while seen < cap:
            if next_url is not None:  # link / body_url: follow the URL (absolute verbatim, relative joined)
                resp = self._send_url("GET", next_url, headers=headers, origin_url=origin_url)
                body = _parse_json(resp)
                page = Page(body=body, items=self._extract_items(body), next=self._next_token(body, resp))
            else:
                q = dict(base_query)
                if next_token is not None:
                    q[p.cursor_param] = next_token
                page = self.fetch_page(path, method=method, query=q, headers=headers, json_body=json_body, data=data, files=files)
            yield page
            seen += 1
            if page.next is None:
                return
            if p.style in ("link", "body_url"):
                next_url = page.next
            else:
                next_token = page.next
        raise ApiError(0, f"pagination reached max_pages={cap}", url=origin_url)

    def collect(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        max_items: int | None = None,
        max_pages: int | None = None,
    ) -> dict:
        """Auto-page and gather every item into one list with an explicit completeness flag.

        Returns ``{"items": [...], "incomplete": bool, "reason": str}``. Fail-safe partial results:
        if a page errors mid-stream we return what we already have with ``incomplete=True`` and the
        error in ``reason`` — partial NEVER masquerades as a complete (or empty) result. Hitting
        ``max_items``/``max_pages`` is also ``incomplete`` (more may exist on the server).
        """
        items: list = []
        incomplete = False
        reason = ""
        pages = 0
        try:
            for page in self.paginate(
                path,
                method=method,
                query=query,
                headers=headers,
                json_body=json_body,
                data=data,
                files=files,
                max_pages=max_pages,
            ):
                pages += 1
                items.extend(page.items)
                if max_items is not None and len(items) >= max_items:
                    items = items[:max_items]
                    incomplete = True
                    reason = f"reached max_items={max_items}"
                    break
            else:
                if max_pages is not None and pages >= max_pages:
                    incomplete = True
                    reason = f"reached max_pages={max_pages}"
        except ApiError as e:
            incomplete = True
            reason = f"page fetch failed after {len(items)} item(s): {e}"
        return {"items": items, "incomplete": incomplete, "reason": reason}

    def _send_url(self, method: str, url: str, *, headers=None, origin_url: str | None = None):
        """Follow a next-page URL from a Link header or JSON body (link / body_url styles).

        An absolute URL is used verbatim; a relative path (e.g. recurly ``/sites/.../accounts?cursor=…``,
        twilio ``/2010-04-01/…``) is ``_join``ed onto ``base_url`` so the host/scheme survive the follow.
        """
        upstream_url = _join(self.manifest.base_url, url)
        # SECURITY: the next-page URL comes from the upstream RESPONSE (body field or Link header). We are
        # about to re-attach the connector's live credential to it, so a hostile/compromised upstream that
        # returns `next: https://attacker/…` would otherwise exfiltrate the token (the run egress can be in
        # wildcard mode). Pin the follow to the ORIGIN request's site; a cross-site next is a hard refusal,
        # never a silent partial. (origin_url defaults to base_url for any non-paginated direct call.)
        _assert_same_site(self.manifest.key, origin_url or self.manifest.base_url, upstream_url)
        request_url = self._request_url(url)
        req_headers = dict(self.manifest.default_headers)
        req_headers.update(headers or {})
        empty: dict[str, Any] = {}
        self._apply_auth(req_headers, empty)
        # Auth that lands in the query string must survive on a verbatim follow-URL too.
        params = empty or None
        verb = method.upper()
        retry_read = verb in _IDEMPOTENT_METHODS
        attempt = 0
        reason = "initial"
        while True:
            resp = _http_audit.request(
                verb,
                request_url,
                params=params,
                headers=req_headers,
                timeout=(self.connect_timeout, self.read_timeout),
                attempt=attempt + 1,
                reason=reason,
                audit_url=upstream_url,
                known_secrets=(self.credential,),
            )
            if 200 <= resp.status_code < 300:
                return resp
            retryable = resp.status_code in _RETRYABLE_STATUS and retry_read
            if not retryable or attempt >= self.max_retries:
                raise ApiError(resp.status_code, _body_text(resp), url=request_url,
                               retryable=resp.status_code in _RETRYABLE_STATUS)
            _sleep(self._retry_delay(resp, attempt), self._sleeper)
            reason = f"retry_status_{resp.status_code}"
            attempt += 1

    def _request_url(self, path: str) -> str:
        if self.manifest.brokered:
            return _broker_url(self.manifest.key, path, base_url=self.manifest.base_url)
        return _join(self.manifest.base_url, path)

    def _audit_url(self, path: str, *, request_url: str) -> str:
        """Return the provider URL, not the internal broker URL, when it is knowable in-container."""
        upstream = _join(self.manifest.base_url, path)
        parsed = urlsplit(upstream)
        if parsed.hostname and "{" not in upstream and "}" not in upstream:
            return upstream
        return request_url

    def _extract_items(self, body: Any) -> list:
        p = self.manifest.pagination
        target = _dotted(body, p.items_field) if p.items_field else body
        if isinstance(target, list):
            return list(target)
        return []

    def _next_token(self, body: Any, resp) -> Any | None:
        p = self.manifest.pagination
        if p.style == "cursor":
            if p.has_more_field:
                if not _truthy(_dotted(body, p.has_more_field)):
                    return None
            token = _dotted(body, p.cursor_field)
            return token if token else None
        if p.style == "link":
            return _parse_link_next(resp.headers.get("Link"))
        if p.style == "body_url":
            return _next_url_in_body(body, p.next_url_field) or None
        return None


# ---------------------------------------------------------------------------
# Module-level convenience: build a client from an injected credential
# ---------------------------------------------------------------------------


def client(manifest: Manifest, *, token_key: str | None = None, **kw) -> Client:
    """Build a ``Client``, resolving env-token credentials or selecting broker mode.

    Env-token connectors raise loudly (via ``oauth.token``) when the connection isn't configured, so
    a script fails with the exact missing ``RC_CONN_*`` instead of making anonymous calls.
    ``auth.strategy == "none"`` and brokered connectors skip credential resolution.
    """
    manifest = _manifest_with_runtime_broker_flag(manifest, token_key=token_key)
    cred = ""
    if not manifest.brokered and manifest.auth.strategy not in ("none", ""):
        cred = oauth.token(token_key or manifest.key)
        manifest, cred = _resolve_host_embedded_credential(manifest, cred)
    return Client(manifest=manifest, credential=cred, **kw)


def _resolve_host_embedded_credential(manifest: Manifest, credential: str) -> tuple[Manifest, str]:
    """Split a per-app connector's ``<secret>@https://<host>[/<path>]`` credential locally.

    Per-app connectors (bubble, woocommerce) template their ``base_url`` with a ``{placeholder}`` and
    carry the concrete app URL inside the env credential, because the host is customer-specific. In
    production the BROKER resolves this and the in-container env var holds no raw credential, so this
    never runs there (brokered clients skip credential resolution upstream). Locally there is no
    broker, so we do the same split the host does: fill the placeholder with the credential's
    host+path (path preserved — e.g. a Bubble dev app's ``/version-test``) and keep only the secret
    for auth. Connectors whose base_url has no placeholder are untouched.
    """
    if "{" not in manifest.base_url or "}" not in manifest.base_url:
        return manifest, credential
    secret, base = _split_host_embedded_credential(credential)
    if not base:
        return manifest, credential
    return replace(manifest, base_url=_fill_base_placeholder(manifest.base_url, base)), secret


def _split_host_embedded_credential(credential: str) -> tuple[str, str]:
    """Return ``(secret, base_url)`` from ``<secret>@https://<host>[/<path>]``; ``("", "")`` if absent.

    Split on the LAST ``@https://`` / ``@http://`` so a secret that itself contains ``@`` survives.
    The secret half may be ``user:pass`` (woocommerce basic) or a bare token (bubble bearer); this
    function doesn't interpret it. The base keeps any path and loses only a trailing slash.
    """
    for marker in ("@https://", "@http://"):
        i = credential.rfind(marker)
        if i < 0:
            continue
        secret = credential[:i].strip()
        base = credential[i + 1:].strip().rstrip("/")
        # Require a real host (mirrors the Go host-side split): a degenerate "tok@https://" must not
        # yield a base whose "host" is the scheme word — the secret would be sent to a wrong host.
        if secret and base and urlsplit(base).hostname:
            return secret, base
    return "", ""


def _fill_base_placeholder(base_url: str, credential_base: str) -> str:
    """Substitute the single ``{placeholder}`` in a templated base_url with the credential URL's
    host+path (scheme stripped), preserving the base_url's own API suffix (e.g. ``/api/1.1``)."""
    host_path = re.sub(r"^https?://", "", credential_base)
    # Lambda replacement: host_path is literal text, never a regex template (a stray backslash in the
    # credential URL must not raise "bad escape").
    return re.sub(r"\{[^}]+\}", lambda _m: host_path, base_url, count=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not base:
        return path
    # urljoin drops the base path unless it ends in "/"; we want base_url to be a prefix, so join
    # against base_url + "/" and strip the leading "/" off path.
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _broker_url(key: str, path: str, *, base_url: str = "") -> str:
    broker_base = f"{_BROKER_BASE_URL}/{quote(key.strip(), safe='')}"
    return _join(broker_base, _broker_relative_path(path, base_url))


def _broker_relative_path(path: str, base_url: str) -> str:
    if path.startswith("http://"):
        raise RuntimeError("brokered integration URLs must use https")
    if path.startswith("https://"):
        return "__url/" + quote(path, safe="")
    if base_url and "{" not in base_url and "}" not in base_url:
        joined = _join(base_url, path)
        if joined.startswith("https://"):
            return "__url/" + quote(joined, safe="")
    return path


def _manifest_with_runtime_broker_flag(manifest: Manifest, *, token_key: str | None = None) -> Manifest:
    if manifest.brokered:
        return manifest
    if _roster_marks_brokered(token_key or manifest.key):
        return replace(manifest, brokered=True)
    return manifest


def _roster_marks_brokered(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    if _key_in_brokered_keys_env(key):
        return True
    for name in _ROSTER_ENVS:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            roster = json.loads(raw)
        except ValueError:
            continue
        if _json_roster_marks_brokered(roster, key):
            return True
    return False


def _key_in_brokered_keys_env(key: str) -> bool:
    raw = os.environ.get(_BROKERED_KEYS_ENV, "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = re.split(r"[\s,]+", raw)
    if isinstance(parsed, dict):
        return _json_roster_marks_brokered(parsed, key)
    if isinstance(parsed, list):
        return any(str(v).strip() == key for v in parsed)
    return False


def _json_roster_marks_brokered(roster: Any, key: str) -> bool:
    if isinstance(roster, dict):
        direct = roster.get(key)
        if _entry_marks_brokered(direct):
            return True
        for bucket in ("integrations", "connections", "active_integrations"):
            if _json_roster_marks_brokered(roster.get(bucket), key):
                return True
        return False
    if isinstance(roster, list):
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            entry_key = entry.get("key") or entry.get("integration_key") or entry.get("connector_key")
            if str(entry_key or "").strip() == key and _entry_marks_brokered(entry):
                return True
    return False


def _entry_marks_brokered(entry: Any) -> bool:
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, str):
        return entry.strip().lower() in {"broker", "brokered", "true", "1", "yes"}
    if isinstance(entry, dict):
        for field in ("brokered", "credential_exposure", "exposure", "injection", "credential_injection"):
            if _entry_marks_brokered(entry.get(field)):
                return True
    return False


def _normalize_json_alias(kw: dict[str, Any]) -> None:
    if "json" not in kw:
        return
    if "json_body" in kw:
        raise TypeError("use only one of json= or json_body=")
    kw["json_body"] = kw.pop("json")


def _normalized_path(path: str) -> str:
    bits = urlsplit(path)
    raw = bits.path if bits.scheme or bits.netloc else path.split("?", 1)[0]
    raw = "/" + raw.lstrip("/")
    return raw.rstrip("/") or "/"


def _matches_any_path(path: str, patterns: tuple[str, ...], *, base_url: str = "") -> bool:
    """Match ``path`` against manifest patterns. With ``base_url`` its path prefix (``/v4``) is
    optional on either side, so ``spreadsheets/x`` and ``/v4/spreadsheets/x`` match the same rule."""
    candidates = {_normalized_path(path)}
    prefix = urlsplit(base_url).path.rstrip("/") if base_url and "{" not in base_url else ""
    if prefix:
        for c in list(candidates):
            candidates.add(c[len(prefix):] if c.startswith(prefix + "/") else prefix + c)
    for pattern in patterns:
        rx = _path_pattern_re(_normalized_path(pattern))
        if any(rx.fullmatch(c) for c in candidates):
            return True
    return False


def _path_pattern_re(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        if pattern[i] == "*":
            out.append("[^/]*")
        else:
            out.append(re.escape(pattern[i]))
        i += 1
    return re.compile("".join(out))


def _site(host: str) -> str:
    """Registrable-ish site = the last two DNS labels (``api.github.com`` → ``github.com``,
    ``us.sentry.io`` → ``sentry.io``). Good enough to pin a pagination follow to the connector's own
    domain (tolerating legit subdomain drift) without a public-suffix list."""
    labels = (host or "").lower().rstrip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else (host or "").lower()


def _assert_same_site(key: str, base_url: str, follow_url: str) -> None:
    base_host = urlsplit(base_url).hostname or ""
    target_host = urlsplit(follow_url).hostname or ""
    if base_host and target_host and _site(target_host) != _site(base_host):
        raise RuntimeError(
            f"integration {key!r}: refusing to follow a pagination URL to a foreign host "
            f"{target_host!r} (base host {base_host!r}) — the credential would leak off-site"
        )


def _parse_json(resp) -> Any:
    try:
        return resp.json()
    except ValueError:
        text = _body_text(resp)
        raise ApiError(resp.status_code, f"non-JSON response body: {text[:200]}", url=getattr(resp, "url", ""))


def _body_text(resp) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 — never let error-rendering itself raise
        return ""


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "null")
    return bool(v)


def _parse_link_next(header: str | None) -> str | None:
    """Extract ``rel="next"`` from an RFC 8288 ``Link`` header. Returns the URL or None."""
    if not header:
        return None
    for part in header.split(","):
        seg = part.strip()
        if "<" not in seg or ">" not in seg:
            continue
        url = seg[seg.index("<") + 1 : seg.index(">")]
        params = seg[seg.index(">") + 1 :]
        for attr in params.split(";"):
            attr = attr.strip()
            if attr.lower().replace(" ", "") in ('rel="next"', "rel=next"):
                return url
    return None


# ---------------------------------------------------------------------------
# CLI: python -m lib.api get/post <key> <path>  (manifest-driven, no bespoke code)
# ---------------------------------------------------------------------------

# Manifests the generic CLI can drive directly. A connector with its own CLI registers here too, so
# `python -m lib.api get <key> ...` works for any catalogued integration without a per-key script.
# `post` works only for manifest-allowlisted non-mutating read endpoints.
MANIFESTS: dict[str, Manifest] = {}

# Keys populated from a connectors/*/manifest.yaml. Tracked so loading is idempotent (re-running
# load_manifests never duplicates) and so an explicit register() always WINS: the YAML loader fills
# only keys not already present, and a later register() overwrites the dict entry outright.
_YAML_LOADED_KEYS: set[str] = set()


def register(manifest: Manifest) -> Manifest:
    MANIFESTS[manifest.key] = manifest
    _YAML_LOADED_KEYS.discard(manifest.key)  # an explicit registration is no longer YAML-owned
    return manifest


def load_manifests(*, force: bool = False) -> dict[str, Manifest]:
    """Discover and register every ``lib/connectors/*/manifest.yaml`` into ``MANIFESTS``.

    This is what makes "a manifest row IS the integration" true at runtime: a catalogued connector
    with NO bespoke Python is still drivable via ``python -m lib.api get <key> ...`` (and allowlisted
    read-only ``post`` endpoints). Idempotent — only fills keys not already present, so an explicit
    ``register()`` (e.g. a Python connector that needs a richer Manifest than the YAML expresses) is
    the source of truth and is never clobbered.

    Discovery walks the package's ``connectors`` directory (works from the baked/installed wheel, not
    only the source tree). A single malformed manifest raises ``ManifestError`` naming the file rather
    than silently skipping — a broken catalog row must fail loudly, not vanish.
    """
    from pathlib import Path

    connectors_dir = Path(__file__).resolve().parent / "connectors"
    for manifest_path in sorted(connectors_dir.glob("*/manifest.yaml")):
        key_hint = manifest_path.parent.name
        if not force and key_hint in MANIFESTS:
            continue  # already loaded, or an explicit register() owns this key — leave it
        mani = _parse_manifest_file(manifest_path)
        MANIFESTS[mani.key] = mani
        _YAML_LOADED_KEYS.add(mani.key)
    return MANIFESTS


class ManifestError(RuntimeError):
    """A manifest.yaml could not be parsed into a Manifest — names the offending file."""


def _parse_manifest_file(path) -> Manifest:
    """Parse one connector ``manifest.yaml`` into a ``Manifest``.

    Maps only the fields that DRIVE a REST call (base_url, auth, pagination, rate_limit,
    default_headers). The catalog-only blocks (``oauth:``, ``mcp_url_template``, ``kinds``,
    ``help_md``, …) are read leniently and ignored here — they belong to the host catalog, not the
    runtime caller — so an unknown key never breaks loading (forward-compatible)."""
    import yaml

    # libyaml's C loader parses the ~90-manifest catalog ~10x faster (same output); every
    # `python -m lib.api` call pays this, so fall back to pure Python only when libyaml is absent.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    except (OSError, yaml.YAMLError) as e:
        raise ManifestError(f"failed to read/parse manifest {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {path} is not a YAML mapping")
    try:
        return _manifest_from_dict(raw)
    except (KeyError, TypeError, ValueError) as e:
        raise ManifestError(f"invalid manifest {path}: {e}") from e


def _manifest_from_dict(raw: dict) -> Manifest:
    key = raw.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("missing required 'key'")
    base_url = raw.get("base_url") or ""
    if not isinstance(base_url, str):
        raise ValueError("'base_url' must be a string")

    auth_raw = raw.get("auth") or {}
    if not isinstance(auth_raw, dict):
        raise ValueError("'auth' must be a mapping")
    auth = Auth(
        strategy=str(auth_raw.get("strategy", "bearer")),
        name=str(auth_raw.get("name", "Authorization")),
    )

    pg_raw = raw.get("pagination") or {}
    if not isinstance(pg_raw, dict):
        raise ValueError("'pagination' must be a mapping")
    defaults = Pagination()
    pagination = Pagination(
        style=str(pg_raw.get("style", defaults.style)),
        cursor_field=str(pg_raw.get("cursor_field", defaults.cursor_field)),
        cursor_param=str(pg_raw.get("cursor_param", defaults.cursor_param)),
        has_more_field=str(pg_raw.get("has_more_field", defaults.has_more_field)),
        items_field=str(pg_raw.get("items_field", defaults.items_field)),
        offset_param=str(pg_raw.get("offset_param", defaults.offset_param)),
        limit_param=str(pg_raw.get("limit_param", defaults.limit_param)),
        page_size=int(pg_raw.get("page_size", defaults.page_size)),
        next_url_field=str(pg_raw.get("next_url_field", defaults.next_url_field)),
        page_param=str(pg_raw.get("page_param", defaults.page_param)),
        page_start=int(pg_raw.get("page_start", defaults.page_start)),
    )

    rl_raw = raw.get("rate_limit") or {}
    if not isinstance(rl_raw, dict):
        raise ValueError("'rate_limit' must be a mapping")
    rate_limit_remaining_header = str(rl_raw.get("remaining_header", "") or "")

    dh_raw = raw.get("default_headers") or {}
    if not isinstance(dh_raw, dict):
        raise ValueError("'default_headers' must be a mapping")
    default_headers = {str(k): str(v) for k, v in dh_raw.items()}

    read_raw = raw.get("read_endpoints") or {}
    if not isinstance(read_raw, dict):
        raise ValueError("'read_endpoints' must be a mapping")
    post_raw = read_raw.get("post") or raw.get("allowed_post_paths") or []
    if not isinstance(post_raw, list):
        raise ValueError("'read_endpoints.post' must be a list")
    allowed_post_paths = tuple(str(v) for v in post_raw if str(v).strip())
    # `credential_exposure` is host catalog metadata. A run's roster/env is the authority that turns a
    # local manifest client into broker mode; local fixture usage should keep env-token behavior.
    brokered = _entry_marks_brokered(raw.get("brokered"))

    write_raw = raw.get("write_endpoints") or {}
    if not isinstance(write_raw, dict):
        raise ValueError("'write_endpoints' must be a mapping")
    write_endpoints: dict[str, tuple[str, ...]] = {}
    for verb in ("post", "put", "patch", "delete"):
        paths = write_raw.get(verb) or []
        if not isinstance(paths, list):
            raise ValueError(f"'write_endpoints.{verb}' must be a list")
        if cleaned := tuple(str(v) for v in paths if str(v).strip()):
            write_endpoints[verb.upper()] = cleaned
    write_consent = str(write_raw.get("consent") or "")
    if write_endpoints and not write_consent:
        raise ValueError("'write_endpoints' needs a 'consent' the host broker enforces")

    return Manifest(
        key=key,
        base_url=base_url,
        auth=auth,
        pagination=pagination,
        rate_limit_remaining_header=rate_limit_remaining_header,
        default_headers=default_headers,
        allowed_post_paths=allowed_post_paths,
        brokered=brokered,
        write_endpoints=write_endpoints,
        write_consent=write_consent,
    )


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="python -m lib.api", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, *, body: bool = False) -> None:
        sp.add_argument("key", help="integration key (env-token or brokered connection must be available)")
        sp.add_argument("path", help="API path under the manifest base_url, or an absolute URL")
        sp.add_argument("--query", action="append", default=[], metavar="K=V", help="query param (repeatable)")
        sp.add_argument("--paginate", action="store_true", help="auto-page and collect all items")
        sp.add_argument("--max-items", type=int, default=None)
        sp.add_argument("--max-pages", type=int, default=None)
        sp.add_argument("--pick", default="", help="comma-separated dotted paths to select from each object")
        if body:
            sp.add_argument("--json", default="", help="JSON request body")
            sp.add_argument("--json-file", default="", help="read JSON request body from a file")
            sp.add_argument("--form", action="append", default=[], metavar="K=V", help="form field (repeatable)")
            sp.add_argument("--file", action="append", default=[], metavar="FIELD=PATH", help="multipart file (repeatable)")

    add_common(sub.add_parser("get", help="GET a path on a registered integration and print JSON"))
    add_common(sub.add_parser("post", help="POST to an allowlisted read endpoint or a manifest write endpoint"), body=True)
    for verb in ("put", "patch", "delete"):
        add_common(
            sub.add_parser(verb, help=f"{verb.upper()} to a manifest write endpoint (brokered runs); otherwise refused"),
            body=True,
        )
    args = p.parse_args(argv)

    load_manifests()  # discover catalogued manifests so any zero-Python integration is drivable
    mani = MANIFESTS.get(args.key)
    if mani is None:
        p.error(f"no manifest registered for {args.key!r}; known: {sorted(MANIFESTS) or '(none)'}")
    query = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
    method = args.cmd.upper()
    try:  # policy before credential resolution: a refused verb never touches a token
        Client(manifest=_manifest_with_runtime_broker_flag(mani), credential="")._assert_read_tier(method, args.path)
    except MethodPolicyError as e:
        p.exit(2, f"{e}\n")
    c = client(mani)
    json_body = data = files = None
    open_files = []
    try:
        if method != "GET":
            json_body, data, files, open_files = _request_body_from_args(args)
        try:
            if args.paginate:
                result = c.collect(
                    args.path,
                    method=method,
                    query=query,
                    json_body=json_body,
                    data=data,
                    files=files,
                    max_items=args.max_items,
                    max_pages=args.max_pages,
                )
                if args.pick:
                    result["items"] = [pick(it, args.pick) for it in result["items"]]
                print(json.dumps(result, indent=2, default=str))
            else:
                body = c.request(method, args.path, query=query, json_body=json_body, data=data, files=files)
                print(json.dumps(pick(body, args.pick) if args.pick else body, indent=2, default=str))
        except MethodPolicyError as e:
            p.exit(2, f"{e}\n")
        except ApiError as e:
            if not e.code:
                raise
            p.exit(1, f"{e}\n")  # a broker denial is an answer for the user, not a traceback
    finally:
        for fh in open_files:
            fh.close()
    return 0


def _request_body_from_args(args) -> tuple[Any | None, dict[str, str] | None, dict[str, Any] | None, list[Any]]:
    import json
    from pathlib import Path

    json_sources = [bool(getattr(args, "json", "")), bool(getattr(args, "json_file", ""))]
    multipart_sources = [bool(getattr(args, "form", [])), bool(getattr(args, "file", []))]
    if any(json_sources) and any(multipart_sources):
        raise SystemExit("--json/--json-file cannot be combined with --form/--file")
    if getattr(args, "json", "") and getattr(args, "json_file", ""):
        raise SystemExit("use only one of --json or --json-file")
    if getattr(args, "json", ""):
        return json.loads(args.json), None, None, []
    if getattr(args, "json_file", ""):
        return json.loads(Path(args.json_file).read_text(encoding="utf-8")), None, None, []

    data = dict(kv.split("=", 1) for kv in getattr(args, "form", []) if "=" in kv) or None
    files = {}
    open_files = []
    for item in getattr(args, "file", []):
        if "=" not in item:
            raise SystemExit("--file must be FIELD=PATH")
        field, raw_path = item.split("=", 1)
        fh = open(raw_path, "rb")
        open_files.append(fh)
        files[field] = fh
    return None, data, files or None, open_files


if __name__ == "__main__":
    raise SystemExit(_main())
