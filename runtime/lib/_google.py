"""Shared plumbing for ``lib.gsheets`` / ``lib.gdocs``: file id parsing, the brokered client, the CLI
error contract.

Writes ride ``lib.api``'s manifest ``write_endpoints``: the host broker allows them only for a file
shared with ReplyPen's address as Editor and answers otherwise with a coded 403 whose message is the
ask for the user (``ApiError.code`` / ``.message``).
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable

from lib import api

SHEETS, DOCS = "googlesheets", "googledocs"
_PRODUCT = {SHEETS: "spreadsheets", DOCS: "document"}
# Account-switched links carry /u/<n> between the product and /d (docs.google.com/spreadsheets/u/0/d/…).
_URL = re.compile(r"/(spreadsheets|document)/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})")
_ID = re.compile(r"[A-Za-z0-9_-]{20,}")


def file_id(target: str, key: str) -> str:
    """The file id from a Google URL or a bare id; a URL of the other product is refused."""
    target = (target or "").strip()
    m = _URL.search(target)
    if m:
        if m.group(1) != _PRODUCT[key]:
            raise ValueError(f"that link is a Google {m.group(1)} file, not {_PRODUCT[key]}")
        return m.group(2)
    if _ID.fullmatch(target):
        return target
    raise ValueError(f"not a Google {_PRODUCT[key]} link or id: {target!r}")


def client(key: str) -> api.Client:
    """The run's ``lib.api`` client for ``key`` (tests patch this one seam)."""
    api.load_manifests()
    return api.client(api.MANIFESTS[key])


def cli(fn: Callable[[], Any]) -> int:
    """Run a CLI command: JSON on stdout; a broker denial or policy refusal as one stderr line."""
    try:
        out = fn()
    except api.ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (api.MethodPolicyError, ValueError, LookupError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False))
    return 0
