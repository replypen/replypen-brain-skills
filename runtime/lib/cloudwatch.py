"""Read-only CloudWatch Logs access for grounding.

The story behind a thread lives in the app's structured (``slog`` JSON) logs. Two ways in:

- ``insights(query, log_group, ...)`` runs a **CloudWatch Logs Insights** query — the powerful
  path: ``filter`` / ``parse`` / ``stats`` over a time range — and returns rows as dicts.
- ``search`` / ``tail`` are thin conveniences over it for the common "find X" / "show recent"
  cases; ``log_groups`` discovers what's there.
- ``query`` / ``logs`` / ``recent`` / ``groups`` are aliases for the same read-only helpers, so
  common model guesses do not fall off the API.

Insights matches a field in context, so prefer ``filter @message like /"thread_id":"<uuid>"/``
over a bare id, which false-matches any field carrying those digits.

Credentials prefer the structured ``RC_CONN_CLOUDWATCH`` connection JSON injected per run
(``access_key_id``, ``secret_access_key``, ``region``), with the legacy standard AWS env vars as a
fallback during migration. IAM must be read-only. ``boto3`` is imported lazily so the module loads for
projects without CloudWatch.

CLI (token-efficient one-offs from bash):

    python -m lib.cloudwatch --list                       # discover log groups
    python -m lib.cloudwatch --tail /app --hours 1
    python -m lib.cloudwatch --search /app --pattern '"level":"ERROR"' --hours 6
    python -m lib.cloudwatch /app 'fields @message | filter @message like /panic/ | limit 50'
"""

import json
import os
import threading
import time
import uuid

from lib import _http_audit

# Insights runs async: start_query → poll get_query_results. Bound the poll so a slow query
# can't hang the per-bash timeout; the caller can raise max_wait for heavy aggregations.
_POLL_INTERVAL = 1.0
_MAX_WAIT = 60.0


def _client():
    creds = _credentials()
    import boto3

    client = boto3.client(
        "logs",
        region_name=creds["region"],
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
    )
    auditor = _BotocoreHTTPAuditor((creds["access_key_id"], creds["secret_access_key"]))
    client.meta.events.register("before-send.cloudwatch-logs.*", auditor.before_send)
    client.meta.events.register("needs-retry.cloudwatch-logs.*", auditor.needs_retry)
    # Botocore's event emitter keeps bound handlers, but retaining the adapter makes ownership clear
    # and protects against a future weak-handler implementation.
    client._replypen_http_auditor = auditor
    return client


class _BotocoreHTTPAuditor:
    """Bridge botocore's signed/retried transport into the shared per-attempt audit contract."""

    def __init__(self, known_secrets: tuple[str, ...]):
        self.known_secrets = known_secrets
        self._state = threading.local()

    def before_send(self, request, event_name: str = "", **_kwargs) -> None:
        request_id = str(uuid.uuid4())
        request.headers["X-Request-ID"] = request_id
        payload, request_body = _botocore_payload(request.body)
        operation = event_name.rsplit(".", 1)[-1] if event_name else "request"
        self._state.pending = {
            "method": request.method,
            "url": request.url,
            "payload": payload,
            "request_body": request_body,
            "endpoint_template": f"/{operation}",
            "request_id": request_id,
            "reason": getattr(self._state, "next_reason", "initial"),
            "started": time.monotonic(),
        }

    def needs_retry(
        self,
        response=None,
        caught_exception=None,
        attempts: int = 1,
        **_kwargs,
    ) -> None:
        pending = getattr(self._state, "pending", None)
        if pending is None:
            return
        http_response = response[0] if response else None
        status = getattr(http_response, "status_code", None)
        _http_audit.emit_prepared_attempt(
            method=pending["method"],
            url=pending["url"],
            payload=pending["payload"],
            request_body=pending["request_body"],
            status_code=int(status) if status is not None else None,
            duration_ms=round((time.monotonic() - pending["started"]) * 1000),
            attempt=attempts,
            reason="initial" if attempts == 1 else pending["reason"],
            request_id=pending["request_id"],
            endpoint_template=pending["endpoint_template"],
            known_secrets=self.known_secrets,
        )
        self._state.pending = None
        if status is not None:
            self._state.next_reason = f"retry_status_{status}"
        elif caught_exception is not None:
            self._state.next_reason = f"retry_transport_{type(caught_exception).__name__}"
        else:
            self._state.next_reason = f"retry_attempt_{attempts}"


def _botocore_payload(body) -> tuple[bytes, object]:
    if body is None:
        return b"", None
    if isinstance(body, str):
        payload = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray, memoryview)):
        payload = bytes(body)
    else:
        marker = f"[stream body omitted: {type(body).__name__}]"
        return marker.encode("utf-8"), marker
    try:
        return payload, json.loads(payload)
    except (TypeError, ValueError):
        return payload, "[binary body omitted]"


def _credentials() -> dict[str, str]:
    raw = os.environ.get("RC_CONN_CLOUDWATCH", "").strip()
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("RC_CONN_CLOUDWATCH is not valid JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError("RC_CONN_CLOUDWATCH must be a JSON object")
        access_key_id = str(body.get("access_key_id") or body.get("aws_access_key_id") or "").strip()
        secret_access_key = str(body.get("secret_access_key") or body.get("aws_secret_access_key") or "").strip()
        region = str(body.get("region") or body.get("aws_region") or "").strip()
    else:
        access_key_id = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
        region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    missing = [
        name
        for name, value in (
            ("access_key_id", access_key_id),
            ("secret_access_key", secret_access_key),
            ("region", region),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "CloudWatch connection not configured: missing "
            + ", ".join(missing)
            + " (expected RC_CONN_CLOUDWATCH JSON or legacy AWS_* env vars)"
        )
    return {
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "region": region,
    }


def _epoch(t) -> int:
    """Coerce an epoch number or an ISO / ``YYYY-MM-DD HH:MM:SS`` string to epoch seconds."""
    import datetime as dt

    if isinstance(t, (int, float)):
        return int(t)
    return int(dt.datetime.fromisoformat(str(t)).timestamp())


def _time_range(hours: float, start, end) -> tuple[int, int]:
    """Resolve a (start, end) epoch-second window: explicit ``start``/``end`` win, else last ``hours``."""
    now = int(time.time())
    e = _epoch(end) if end else now
    s = _epoch(start) if start else e - int(hours * 3600)
    return s, e


def insights(
    query: str,
    log_group,
    hours: float = 24,
    start=None,
    end=None,
    limit: int = 1000,
    max_wait: float = _MAX_WAIT,
) -> list[dict]:
    """Run a Logs Insights query over ``log_group`` (a name or list) and return rows as dicts.

    Each result row becomes ``{field: value}`` with the internal ``@ptr`` pointer dropped.
    """
    client = _client()
    s, e = _time_range(hours, start, end)
    groups = list(log_group) if isinstance(log_group, (list, tuple)) else [log_group]
    qid = client.start_query(
        logGroupNames=groups, startTime=s, endTime=e, queryString=query, limit=limit
    )["queryId"]
    waited = 0.0
    while True:
        resp = client.get_query_results(queryId=qid)
        status = resp.get("status")
        if status == "Complete":
            break
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"CloudWatch Insights query {status}")
        if waited >= max_wait:
            client.stop_query(queryId=qid)
            raise TimeoutError(f"CloudWatch Insights query exceeded {max_wait}s")
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    return [
        {f["field"]: f["value"] for f in row if f["field"] != "@ptr"}
        for row in resp.get("results", [])
    ]


def _escape_pattern(pattern: str) -> str:
    """Make ``pattern`` safe to interpolate inside a ``/.../`` Logs Insights regex literal.

    ``pattern`` often carries attacker-influenceable text (a subject line, an email body). A bare
    ``/`` would close the literal early and let the rest be reinterpreted as query syntax, and a
    newline corrupts the single-line ``filter`` clause outright. We escape backslash and ``/`` so
    they're matched literally, and reject control characters (newlines, NULs, etc.) — there's no
    valid way to embed them in a single-line clause, so silently stripping them would change the
    user's intent. Raise loudly instead of running a mangled query.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in pattern):
        raise ValueError(
            "CloudWatch search pattern contains a control character (e.g. newline) — "
            "remove it or use insights() with an explicit query"
        )
    # Order matters: escape the escape char first so we don't double-escape the slashes we add.
    return pattern.replace("\\", "\\\\").replace("/", "\\/")


def search(log_group, pattern: str, hours: float = 24, limit: int = 200) -> list[dict]:
    """Return recent log lines whose ``@message`` matches ``pattern`` (a Logs Insights regex).

    ``pattern`` is interpolated into a ``/.../`` regex literal, so it's escaped first to keep
    attacker-influenceable text from breaking out of the literal (see ``_escape_pattern``).
    """
    q = (
        "fields @timestamp, @message\n"
        f"| filter @message like /{_escape_pattern(pattern)}/\n"
        "| sort @timestamp desc\n"
        f"| limit {int(limit)}"
    )
    return insights(q, log_group, hours=hours, limit=limit)


def tail(log_group, hours: float = 1, limit: int = 200) -> list[dict]:
    """Return the most recent log lines in the window (newest first)."""
    q = f"fields @timestamp, @message\n| sort @timestamp desc\n| limit {int(limit)}"
    return insights(q, log_group, hours=hours, limit=limit)


def log_groups(prefix: str | None = None, limit: int = 50) -> list[str]:
    """List log group names, optionally filtered by ``prefix`` — for discovery."""
    kwargs: dict = {"limit": limit}
    if prefix:
        kwargs["logGroupNamePrefix"] = prefix
    groups = _client().describe_log_groups(**kwargs).get("logGroups", [])
    return [g["logGroupName"] for g in groups]


def filter_log_events(log_group: str, pattern: str, limit: int = 50) -> list[dict]:
    """Low-level escape hatch: a synchronous ``FilterLogEvents`` (no Insights polling).

    Cheaper than ``insights`` for a quick grep, but no ``parse``/``stats`` and a different
    (metric-filter) pattern syntax. Prefer ``search`` for the common case.
    """
    events = _client().filter_log_events(logGroupName=log_group, filterPattern=pattern, limit=limit)
    return list(events.get("events", []))


# Affordance aliases for common model guesses; all keep the same read-only implementations.
query = insights
logs = search
grep = search
recent = tail
groups = log_groups
list_log_groups = log_groups
filter_events = filter_log_events


def _main(argv=None) -> int:
    import argparse

    from . import _output

    p = argparse.ArgumentParser(prog="python -m lib.cloudwatch", description=__doc__.split("\n")[0])
    p.add_argument("log_group", nargs="?", help="Log group name.")
    p.add_argument("query", nargs="?", help="A Logs Insights query string.")
    p.add_argument("--list", nargs="?", const="", metavar="PREFIX", help="List log groups and exit.")
    p.add_argument("--tail", metavar="GROUP", help="Show the most recent lines of GROUP.")
    p.add_argument("--search", metavar="GROUP", help="Search GROUP for --pattern.")
    p.add_argument("--pattern", help="Regex for --search.")
    p.add_argument("--hours", type=float, default=24, help="Look back N hours (default 24).")
    p.add_argument("--start", help="Window start (ISO or 'YYYY-MM-DD HH:MM:SS').")
    p.add_argument("--end", help="Window end (ISO or 'YYYY-MM-DD HH:MM:SS').")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--format", choices=("csv", "json", "table"), default="csv")
    args = p.parse_args(argv)

    if args.list is not None:
        for name in log_groups(prefix=args.list or None):
            print(name)
        return 0
    if args.tail:
        rows = tail(args.tail, hours=args.hours, limit=args.limit)
    elif args.search:
        if not args.pattern:
            p.error("--search requires --pattern")
        rows = search(args.search, args.pattern, hours=args.hours, limit=args.limit)
    elif args.log_group and args.query:
        rows = insights(
            args.query, args.log_group, hours=args.hours, start=args.start, end=args.end, limit=args.limit
        )
    else:
        p.error("give a log group + query, or use --list / --tail / --search")
    _output.emit_rows(rows, args.format, label="cw")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
