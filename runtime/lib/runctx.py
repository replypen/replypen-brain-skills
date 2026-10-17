"""What KIND of run is this? — the machine-readable document a script may branch on.

The host stamps the document as env `RC_RUN_CONTEXT_JSON` into every container it starts (run, action,
preflight, policy), plus a `run_context.json` copy at the root of a run's `/brain` view. **The env
document wins whenever present**: an action container mounts the raw project clone, where a
`run_context.json` would be committed brain content, not the host's stamp. The file is read only when
no host document exists (an older host, a local fixture).

Before it, the run's situation lived only as PROSE in the model's prompt ("Request source: chat", the
mode preamble). **Branch on this document, never on prose, and never on ids you read out of the
message body** — the model can be talked into either one.

**Descriptive, never authorization.** Nothing here grants anything: scope is enforced by the database
engine, the sealed env and the egress gateway. Do not derive permissions from it — a script that
decides "may I" from `principal` or `plane` is reading a hint as a boundary.

Shape (`schema_version` and `plane` lead; absent values are explicit `null`, never missing, so
`context()["channel"]` never raises)::

    {"schema_version": 1, "plane": "run", "run_id": "...",
     "project": {"id": "...", "name": "kampadmin"},
     "tenant": {"id": "...", "slug": "solhi", "scope_value": "org-42"},
     "kind": "chat", "mode": "chat", "scenario": "chat", "surface": "chat",
     "channel": null, "origin": null, "simulation": false,
     "principal": {"kind": "kampadmin_person", "external_id": "p-88",
                   "asserted_by": "embassy", "assurance": "session"},
     "session_id": "...", "thread_id": "...", "brain_ref": {...}, "action": null}

`plane` is `"run"` (the run's own workspace, everything filled) or `"action"` (a hosted action /
preflight / policy container: identity only, every ingress field `null`).

A host too old to stamp it leaves both sources absent — `context()` is then `{}` and every accessor
degrades, except `is_principal_scoped()`, which falls back to the legacy `RC_PRINCIPAL_SCOPED` env so
an existing per-audience script keeps working. Malformed JSON from either source RAISES: that is an
injection bug, not a missing fact.

Tests/local runs point at a fixture with ``RC_RUN_CONTEXT_PATH``.

CLI::

    python -m lib.runctx                       # the whole document as JSON (source noted on stderr)
    python -m lib.runctx get surface
    python -m lib.runctx get principal.kind
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

DEFAULT_PATH = "/brain/run_context.json"
ENV_VAR = "RC_RUN_CONTEXT_JSON"

PLANE_RUN = "run"
PLANE_ACTION = "action"


class RunContextError(RuntimeError):
    """The run context is unreadable or malformed."""


# Per path, so one process reads each source once: (source, document).
_cache: dict[str, tuple[str, dict[str, Any]]] = {}


def _path() -> str:
    return os.environ.get("RC_RUN_CONTEXT_PATH", "").strip() or DEFAULT_PATH


def _resolve() -> tuple[str, dict[str, Any]]:
    path = _path()
    if path not in _cache:
        _cache[path] = _load(path)
    return _cache[path]


def _load(path: str) -> tuple[str, dict[str, Any]]:
    # Host document first: the file may be committed brain content (see module doc).
    injected = os.environ.get(ENV_VAR, "").strip()
    if injected:
        return "env", _parse(injected, f"run context env {ENV_VAR}")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return "none", {}
    except OSError as exc:
        raise RunContextError(f"run context {path} is unreadable: {exc}") from exc
    return "file", _parse(raw, f"run context {path}")


def _parse(raw: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise RunContextError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RunContextError(f"{label} must be a JSON object, got {type(document).__name__}")
    return document


def context() -> dict[str, Any]:
    """Return the whole document (a copy); `{}` when the host stamped none."""
    return dict(_resolve()[1])


def source() -> str:
    """Where it came from: ``"env"`` (host-injected), ``"file"`` (fallback), or ``"none"``."""
    return _resolve()[0]


def get(key: str, default: Any = None) -> Any:
    """Return one field, `default` when absent or null. Dotted keys walk nested objects:
    ``get("principal.kind")``, ``get("tenant.scope_value")``, ``get("brain_ref.sha")``."""
    node: Any = _resolve()[1]
    for part in key.split("."):
        if not isinstance(node, dict) or node.get(part) is None:
            return default
        node = node[part]
    return node


def plane() -> str | None:
    """``"run"`` | ``"action"``; None without a document."""
    return get("plane")


def surface() -> str | None:
    """The ingress surface (``chat``, ``dashboard_chat``, ``email``, ``intercom``, ``whatsapp``,
    ``compose``, ``prompt_api``, ``mcp``, ``embassy``, ``console``) — the same value the prompt shows
    as "Request source:". None on the action plane."""
    return get("surface")


def is_simulation() -> bool:
    """True for a dress rehearsal (`rp ask --simulation`): every side-effecting credential was stripped,
    so the script must not claim it changed anything. False when unknown."""
    return get("simulation") is True


def is_principal_scoped() -> bool:
    """True when this run speaks for ONE asserted end-user (a parent, a leader) rather than an operator
    with the project's full reach — the check a helper makes before it decides whose rows it may show.

    It is the PRESENCE of `principal`, with the legacy `RC_PRINCIPAL_SCOPED` env as the fallback for a
    host too old to stamp the document. Still descriptive: the actual row filtering is the database's."""
    if get("principal") is not None:
        return True
    if _resolve()[0] != "none":
        return False
    return os.environ.get("RC_PRINCIPAL_SCOPED", "").strip() == "1"


def _origin() -> str:
    """Human-readable provenance for an error line."""
    return {"file": _path(), "env": ENV_VAR, "none": f"${ENV_VAR} or {_path()}, neither present"}[source()]


def _render(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.runctx", description="Read this run's context document")
    commands = parser.add_subparsers(dest="command")
    get_parser = commands.add_parser("get", help="print one field (dotted keys walk nested objects)")
    get_parser.add_argument("key")
    get_parser.add_argument("--default", dest="default", default=None, help="printed when the field is absent")
    args = parser.parse_args(argv)
    try:
        if args.command != "get":
            # stdout stays a bare JSON object so `python -m lib.runctx | jq` keeps working.
            print(f"# source: {source()} ({_origin()})", file=sys.stderr)
            print(json.dumps(context(), indent=2, sort_keys=True))
            return 0
        value = get(args.key, args.default)
        if value is None:
            print(f"run context has no {args.key!r} (source: {_origin()})", file=sys.stderr)
            return 1
        print(_render(value))
        return 0
    except RunContextError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
