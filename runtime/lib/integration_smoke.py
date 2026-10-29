"""Local live-smoke helper for connector authors.

Runs a few read-only calls against a connector when a developer has a live credential, without needing
a ReplyPen project row. Secrets are read from env only: set ``RC_CONN_<KEY>`` directly, or set a local
override such as ``RC_INTEGRATION_SMOKE_<KEY>`` / ``RC_INTEGRATION_SMOKE_TOKEN`` and this helper copies
it into the connector env slot for this process and any connector CLI subprocess.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

from lib import api, oauth

_GENERIC_TOKEN_ENV = "RC_INTEGRATION_SMOKE_TOKEN"
_PREFIX = "RC_INTEGRATION_SMOKE_"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: dict[str, Any]


def local_token_env(key: str) -> str:
    """Return the key-specific local smoke token env var, e.g. ``RC_INTEGRATION_SMOKE_GITHUB``."""
    return _PREFIX + oauth.env_var(key).removeprefix("RC_CONN_")


def prepare_token_env(key: str, *, token_env: str = "") -> tuple[str, list[str]]:
    """Ensure ``RC_CONN_<KEY>`` exists when a local smoke token override is present.

    Returns the canonical env slot plus the env names whose values must be redacted from helper output.
    """
    target = oauth.env_var(key)
    redact_envs = [target]
    if os.environ.get(target):
        return target, redact_envs

    candidates = [token_env, local_token_env(key), _GENERIC_TOKEN_ENV]
    for candidate in candidates:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        redact_envs.append(candidate)
        value = os.environ.get(candidate, "")
        if value:
            os.environ[target] = value
            return target, redact_envs
    return target, redact_envs


def redact(value: str, env_names: list[str]) -> str:
    """Scrub known secret env values from text before it reaches stdout/stderr."""
    out = value
    for name in env_names:
        secret = os.environ.get(name, "")
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def load_manifest(key: str) -> api.Manifest:
    manifests = api.load_manifests()
    try:
        return manifests[key]
    except KeyError as exc:
        known = ", ".join(sorted(manifests))
        raise SystemExit(f"unknown integration {key!r}; known keys: {known}") from exc


def run_api_check(
    client: api.Client,
    *,
    name: str,
    method: str,
    path: str,
    query: dict[str, str],
    pick: str = "",
    paginate: bool = False,
    max_items: int | None = None,
) -> CheckResult:
    verb = method.upper()
    if paginate:
        body = client.collect(path, method=verb, query=query, max_items=max_items)
        # A deliberate item/page cap is fine; a page that failed mid-way is not. The client-level page
        # cap surfaces as a status-0 ApiError inside the failure reason, hence the second match.
        reason = str(body.get("reason", ""))
        capped = reason.startswith("reached max_") or "pagination reached max_pages=" in reason
        ok = not body.get("incomplete") or capped
    else:
        body = client.request(verb, path, query=query)
        ok = True
    return CheckResult(name=name, ok=ok, detail=summarize_body(body, pick=pick, paginated=paginate))


def run_negative_check(client: api.Client, *, path: str, query: dict[str, str]) -> CheckResult:
    """GET a deliberately wrong id/enum; pass only on a loud 4xx, never a silent 2xx.

    401 and 429 fail too: a broken credential or a rate limit proves nothing about bad-input handling.
    """
    try:
        body = client.request("GET", path, query=query)
    except api.ApiError as exc:
        ok = 400 <= exc.status < 500 and exc.status not in (401, 429)
        return CheckResult(
            name="negative",
            ok=ok,
            detail={"status": exc.status, "error_preview": str(exc)[:240]},
        )
    return CheckResult(
        name="negative",
        ok=False,
        detail={"status": "2xx", "unexpected_success": summarize_body(body)},
    )


def summarize_body(body: Any, *, pick: str = "", paginated: bool = False) -> dict[str, Any]:
    """Return a compact shape summary, not a raw provider dump."""
    if paginated and isinstance(body, dict) and "items" in body:
        items = body.get("items") if isinstance(body.get("items"), list) else []
        detail: dict[str, Any] = {
            "type": "paginated",
            "count": len(items),
            "incomplete": bool(body.get("incomplete")),
        }
        if body.get("reason"):
            detail["reason"] = str(body["reason"])
        if items:
            detail["first_item"] = _selected_preview(items[0], pick)
        return detail
    if isinstance(body, list):
        detail = {"type": "list", "count": len(body)}
        if body:
            detail["first_item"] = _selected_preview(body[0], pick)
        return detail
    if isinstance(body, dict):
        detail = {"type": "object", "keys": sorted(str(k) for k in body.keys())[:30]}
        if pick:
            detail["picked"] = api.pick(body, pick)
        return detail
    return {"type": type(body).__name__, "value_preview": str(body)[:120]}


def _selected_preview(obj: Any, pick: str) -> Any:
    if pick:
        return api.pick(obj, pick)
    if isinstance(obj, dict):
        return {str(k): obj[k] for k in sorted(obj.keys())[:12]}
    return str(obj)[:120]


def parse_query(values: list[str]) -> dict[str, str]:
    query: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise SystemExit("--query values must be K=V")
        k, v = raw.split("=", 1)
        query[k] = v
    return query


def run_connector_cli(key: str, command: str, *, timeout: int, redact_envs: list[str]) -> CheckResult:
    args = shlex.split(command)
    if not args:
        raise SystemExit("--connector-command must contain at least one connector CLI argument")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", f"lib.connectors.{key}", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except Exception as exc:  # noqa: BLE001 - emit sanitized JSON rather than a traceback.
        return _error_result("connector_cli", exc, redact_envs)
    stdout = redact(proc.stdout or "", redact_envs)
    stderr = redact(proc.stderr or "", redact_envs)
    return CheckResult(
        name="connector_cli",
        ok=proc.returncode == 0,
        detail={
            "argv": ["python", "-m", f"lib.connectors.{key}", *args],
            "returncode": proc.returncode,
            "stdout_lines": _preview_lines(stdout),
            "stderr_lines": _preview_lines(stderr),
        },
    )


def _preview_lines(text: str) -> list[str]:
    return [line[:240] for line in text.splitlines() if line.strip()][:12]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.integration_smoke",
        description="Run local read-only live smoke checks for a connector using env-only credentials.",
    )
    parser.add_argument("key", help="connector key")
    parser.add_argument("--token-env", default="", help="env var that holds a local-only token override")
    parser.add_argument("--query", action="append", default=[], metavar="K=V", help="query param for API checks")
    parser.add_argument("--identity-path", default="", help="GET path for identity/account discovery")
    parser.add_argument("--identity-pick", default="", help="picked fields for identity output")
    parser.add_argument("--list-path", default="", help="GET path for one list/search check")
    parser.add_argument("--list-pick", default="", help="picked fields for first list item")
    parser.add_argument("--list-max-items", type=int, default=None, help="max items when --paginate-list is set")
    parser.add_argument("--paginate-list", action="store_true", help="use manifest pagination for --list-path")
    parser.add_argument("--detail-path", default="", help="GET path for one by-id/detail check")
    parser.add_argument("--detail-path-template", default="", help="format string such as /things/{id}")
    parser.add_argument("--detail-id", default="", help="id used with --detail-path-template")
    parser.add_argument("--detail-pick", default="", help="picked fields for detail output")
    parser.add_argument(
        "--negative-path",
        default="",
        help="GET path with a deliberately bad id/enum; must fail with a 4xx, not an empty success",
    )
    parser.add_argument(
        "--connector-command",
        action="append",
        default=[],
        help="arguments for `python -m lib.connectors.<key>`; repeatable, parsed with shlex, never shell-executed",
    )
    parser.add_argument("--timeout", type=int, default=60, help="connector CLI timeout seconds")
    args = parser.parse_args(argv)

    target_env, redact_envs = prepare_token_env(args.key, token_env=args.token_env)
    manifest = load_manifest(args.key)
    results: list[CheckResult] = []
    query = parse_query(args.query)
    if args.list_max_items is not None and not args.paginate_list:
        raise SystemExit("--list-max-items requires --paginate-list; otherwise pass a provider-specific --query limit=...")

    if args.identity_path or args.list_path or args.detail_path or args.detail_path_template or args.negative_path:
        try:
            client = api.client(manifest)
        except Exception as exc:  # noqa: BLE001 - keep helper failures in sanitized JSON.
            client = None
            results.append(_error_result("api_client", exc, redact_envs))
        if client is not None:
            if args.identity_path:
                results.append(_guarded_check(
                    "identity",
                    redact_envs,
                    lambda: run_api_check(
                        client,
                        name="identity",
                        method="GET",
                        path=args.identity_path,
                        query=query,
                        pick=args.identity_pick,
                    )
                ))
            if args.list_path:
                results.append(_guarded_check(
                    "list",
                    redact_envs,
                    lambda: run_api_check(
                        client,
                        name="list",
                        method="GET",
                        path=args.list_path,
                        query=query,
                        pick=args.list_pick,
                        paginate=args.paginate_list,
                        max_items=args.list_max_items,
                    )
                ))
            detail_path = args.detail_path
            if not detail_path and args.detail_path_template:
                if not args.detail_id:
                    raise SystemExit("--detail-id is required with --detail-path-template")
                detail_path = args.detail_path_template.format(id=args.detail_id)
            if detail_path:
                results.append(_guarded_check(
                    "detail",
                    redact_envs,
                    lambda: run_api_check(
                        client,
                        name="detail",
                        method="GET",
                        path=detail_path,
                        query=query,
                        pick=args.detail_pick,
                    )
                ))
            if args.negative_path:
                results.append(_guarded_check(
                    "negative",
                    redact_envs,
                    lambda: run_negative_check(client, path=args.negative_path, query=query),
                ))

    for command in args.connector_command:
        results.append(run_connector_cli(args.key, command, timeout=args.timeout, redact_envs=redact_envs))

    if not results:
        raise SystemExit(
            "provide at least one of --identity-path, --list-path, --detail-path, --negative-path, or --connector-command"
        )

    payload = {
        "key": args.key,
        "credential_env": target_env,
        "ok": all(r.ok for r in results),
        "checks": [r.__dict__ for r in results],
    }
    print(redact(json.dumps(payload, indent=2, default=str), redact_envs))
    return 0 if payload["ok"] else 1


def _guarded_check(name: str, redact_envs: list[str], run: Callable[[], CheckResult]) -> CheckResult:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - smoke output should be sanitized JSON, not a traceback.
        return _error_result(name, exc, redact_envs)


def _error_result(name: str, exc: Exception, redact_envs: list[str]) -> CheckResult:
    return CheckResult(
        name=name,
        ok=False,
        detail={
            "error_type": type(exc).__name__,
            "error": redact(str(exc), redact_envs),
        },
    )


if __name__ == "__main__":
    raise SystemExit(_main())
