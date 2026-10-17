"""Deterministic dashboard-settings discovery over replypen's live ``/meta`` API.

This helper is intentionally read-only. ``find`` maps plain language to registry keys and ``resolve``
returns the hierarchy responses needed to explain effective value/provenance. Writes are emitted as a
typed ``respond.settings_change`` candidate and executed by the host proposal plane.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from difflib import SequenceMatcher
from typing import Any, Callable
from urllib.parse import quote

from lib import _http_audit

_SYNONYMS = {
    "tone": "persona tone voice warm friendly concise casual wording",
    "formality": "persona formal informal casual professional",
    "signature": "persona signoff sign-off closing footer",
    "language": "persona language locale translation",
    "guidance": "persona wording style voice instructions",
    "spam_watch_enabled": "spam junk rescue watch processing",
    "labeling_enabled": "label category categorise categorize tagging",
    "inbox_cleaning_enabled": "clean quotes signatures thread inbox",
    "archive_after_reply": "archive after reply inbox cleanup",
    "follow_up_enabled": "follow up reminder unanswered nudge",
    "autonomy_mode": "automatic send auto-send draft review delivery autonomy",
}


class SettingsError(RuntimeError):
    pass


Fetcher = Callable[[str], Any]


def _api_base() -> str:
    value = os.environ.get("RC_API_URL", "").strip().rstrip("/")
    if not value:
        raise SettingsError("RC_API_URL is unavailable; lib.settings is dashboard-only")
    return value


def _fetch(path: str) -> Any:
    token = os.environ.get("RC_API_TOKEN", "").strip()
    if not token:
        raise SettingsError("RC_API_TOKEN is unavailable; lib.settings is dashboard-only")
    url = _api_base() + "/" + path.lstrip("/")
    try:
        response = _http_audit.request(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
            endpoint_template=path,
            known_secrets=(token,),
        )
        if not (200 <= response.status_code < 300):
            raise SettingsError(f"settings API HTTP {response.status_code}: {response.text[:800].strip()}")
        return response.json()
    except SettingsError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalize transport/JSON failures for the CLI
        raise SettingsError(f"settings API request failed: {exc}") from exc


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower().replace("_", " ").replace("-", " ")))


def _reachable_levels(capabilities: dict[str, Any]) -> list[str]:
    explicit = os.environ.get("RC_SCOPE_LEVEL", "").strip().lower()
    if explicit == "mailbox":
        return ["mailbox"]
    if explicit == "tenant" or capabilities.get("tenant"):
        return ["tenant", "mailbox"]
    return ["project", "tenant", "mailbox"]


def _targets(capabilities: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    """Return trusted candidate targets, with the session scope first/default."""
    project = capabilities.get("project") or {}
    tenant = capabilities.get("tenant") or {}
    mailbox_id = os.environ.get("RC_MAILBOX_ID", "").strip()
    scope = os.environ.get("RC_SCOPE_LEVEL", "").strip().lower()
    targets: list[dict[str, str]] = []
    if project.get("id"):
        targets.append({"level": "project", "id": str(project["id"]), "name": str(project.get("name", "project"))})
    if tenant.get("id"):
        targets.append({"level": "tenant", "id": str(tenant["id"]), "name": str(tenant.get("name") or tenant.get("slug") or "tenant")})
    if mailbox_id:
        targets.append({"level": "mailbox", "id": mailbox_id, "name": mailbox_id})
    default_level = scope or ("tenant" if tenant.get("id") else "project")
    default = next((target for target in targets if target["level"] == default_level), None)
    return targets, default


def _field_rows(schema: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for resource_name, resource in (schema.get("resources") or {}).items():
        for field in resource.get("fields") or []:
            api_key = str(field.get("key", ""))
            if not api_key:
                continue
            row = dict(field)
            row["resource"] = resource_name
            row["api_key"] = api_key
            key = api_key if resource_name == "settings" else f"{resource_name}.{api_key}"
            row["key"] = key
            row["settable_at"] = list(field.get("settable_at") or ["project"])
            rows[key] = row
    for group, description in (schema.get("hierarchy_settings") or {}).items():
        levels = list(description.get("settable_at") or [])
        for field in description.get("field_schemas") or []:
            key = str(field.get("key", ""))
            if not key:
                continue
            row = dict(field)
            row["resource"] = "hierarchy_settings"
            row["api_key"] = key
            row["settable_at"] = list(field.get("settable_at") or levels)
            rows[key] = row
        for leaf in description.get("fields") or []:
            key = f"{group}.{leaf}"
            rows.setdefault(key, {"key": key, "api_key": key, "help": "", "type": "unknown", "resource": "hierarchy_settings"})
            rows[key]["settable_at"] = levels
    # tenants.autonomy_mode is a dedicated inheritable column, not hierarchy JSONB yet.
    if "autonomy_mode" in rows:
        rows["autonomy_mode"]["settable_at"] = ["project", "tenant"]
        rows["autonomy_mode"]["special_case"] = "tenant autonomy is stored outside the hierarchy bag"
    return list(rows.values())


def find(intent: str, fetcher: Fetcher = _fetch) -> dict[str, Any]:
    """Return ranked registry candidates fenced to this token/session."""
    query = intent.strip()
    if not query:
        raise SettingsError("find requires a plain-English intent")
    capabilities = fetcher("meta/capabilities")
    schema = fetcher("meta/schema")
    writable = set(capabilities.get("writable_keys") or [])
    reachable = _reachable_levels(capabilities)
    qtokens = _tokens(query)
    ranked = []
    for field in _field_rows(schema):
        key = field["key"]
        leaf = key.rsplit(".", 1)[-1]
        text = " ".join((key, str(field.get("group", "")), str(field.get("help", "")), _SYNONYMS.get(leaf, "")))
        tokens = _tokens(text)
        overlap = len(qtokens & tokens)
        fuzzy = SequenceMatcher(None, query.lower(), text.lower()).ratio()
        score = overlap * 10 + fuzzy
        if overlap == 0 and fuzzy < 0.18:
            continue
        levels = list(field.get("settable_at") or ["project"])
        reachable_for_field = [level for level in levels if level in reachable]
        ranked.append({
            "key": key,
            "help": field.get("help", ""),
            "type": field.get("type", "unknown"),
            "enum": field.get("enum") or [],
            "settable_at": levels,
            "reachable_levels": reachable_for_field,
            "writable": key in writable and bool(reachable_for_field),
            "resource": field.get("resource", ""),
            "score": round(score, 3),
            **({"note": field["special_case"]} if field.get("special_case") else {}),
        })
    ranked.sort(key=lambda row: (-row["score"], row["key"]))
    targets, default_target = _targets(capabilities)
    return {
        "intent": query,
        "session_scope": os.environ.get("RC_SCOPE_LEVEL", "").strip().lower() or ("tenant" if capabilities.get("tenant") else "project"),
        "default_target": default_target,
        "targets": targets,
        "candidates": ranked[:8],
    }


def _scope_paths(capabilities: dict[str, Any]) -> list[tuple[str, str]]:
    project = os.environ.get("RC_PROJECT", "").strip() or str((capabilities.get("project") or {}).get("name", ""))
    tenant = os.environ.get("RC_TENANT", "").strip() or str((capabilities.get("tenant") or {}).get("slug", ""))
    mailbox = os.environ.get("RC_MAILBOX_ID", "").strip()
    if not project:
        raise SettingsError("the dashboard token did not resolve a project")
    base = f"projects/{quote(project, safe='') }"
    paths: list[tuple[str, str]] = [("project", f"{base}/settings?resolved=true")]
    if tenant:
        paths.append(("tenant", f"{base}/tenants/{quote(tenant, safe='')}/settings?resolved=true"))
    if mailbox:
        paths.append(("mailbox", f"{base}/mailboxes/{quote(mailbox, safe='')}/settings?resolved=true"))
    return paths


def _lookup_key(body: Any, key: str) -> Any:
    current = body
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _resolved_level(body: Any, key: str) -> dict[str, Any]:
    """Project the hierarchy envelope into the one key the caller asked for."""
    resolved = _lookup_key((body or {}).get("resolved", {}), key) if isinstance(body, dict) else None
    override = _lookup_key((body or {}).get("settings", {}), key) if isinstance(body, dict) else None
    return {"effective": resolved, "override": override}


def resolve(key: str, fetcher: Fetcher = _fetch) -> dict[str, Any]:
    """Resolve a key at every session-reachable hierarchy rung, retaining provenance verbatim."""
    key = key.strip()
    if not key:
        raise SettingsError("resolve requires a setting key")
    capabilities = fetcher("meta/capabilities")
    schema = fetcher("meta/schema")
    fields = {field["key"]: field for field in _field_rows(schema)}
    if key not in fields:
        raise SettingsError(f"unknown setting key: {key}")
    field = fields[key]
    resource = field.get("resource", "")
    if resource not in {"settings", "hierarchy_settings"}:
        body = fetcher(resource)
        api_key = str(field.get("api_key", key))
        item = (body or {}).get(api_key) if isinstance(body, dict) else None
        return {
            "key": key,
            "writable": key in set(capabilities.get("writable_keys") or []),
            "settable_at": ["project"],
            "levels": [{"level": "project", "effective": item, "override": item}],
        }
    reachable = set(_reachable_levels(capabilities))
    allowed = set(fields[key].get("settable_at") or ["project"])
    levels = []
    for level, path in _scope_paths(capabilities):
        if level not in reachable or level not in allowed:
            continue
        body = fetcher(path)
        levels.append({"level": level, **_resolved_level(body, key)})
    return {
        "key": key,
        "writable": key in set(capabilities.get("writable_keys") or []),
        "settable_at": sorted(allowed),
        "levels": levels,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.settings", description="Read-only dashboard settings discovery")
    commands = parser.add_subparsers(dest="command", required=True)
    find_parser = commands.add_parser("find", help="rank settings for a plain-English intent")
    find_parser.add_argument("intent")
    resolve_parser = commands.add_parser("resolve", help="show current value and hierarchy provenance")
    resolve_parser.add_argument("key")
    args = parser.parse_args(argv)
    try:
        result = find(args.intent) if args.command == "find" else resolve(args.key)
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
