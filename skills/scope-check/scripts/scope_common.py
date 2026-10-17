#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Shared plumbing for `scope-check`: audiences and the `rp` invocation.

Nothing here knows about a specific project. Both scripts talk to production only through the public
`rp` CLI — they never see a DSN, never connect to a database, and never write.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

DEFAULT_RC_TIMEOUT_S = 180


class RcError(RuntimeError):
    """`rp` exited non-zero, or its stdout was not the JSON we asked for."""

    def __init__(self, message: str, *, stderr: str = "", returncode: int = 1):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


@dataclass(frozen=True)
class Audience:
    """One identity to replay a session as. ``kind is None`` = the tenant-wide, no-principal view."""

    label: str
    kind: str | None = None
    external_id: str | None = None

    @property
    def is_principal(self) -> bool:
        return self.kind is not None

    def flags(self) -> list[str]:
        if not self.is_principal:
            return []
        return ["--principal-kind", self.kind, "--principal-id", self.external_id]


NO_PRINCIPAL = Audience("no-principal")


def parse_audience(spec: str) -> Audience:
    """``--as <label>=<kind>:<external_id>`` → an `Audience`.

    The label is the human name in the matrix ("regular-admin"), the kind/id pair is what the host
    asserts. Both halves are required: a bare label would silently become the no-principal view,
    which is exactly the confusion this skill exists to remove."""
    label, sep, principal = spec.partition("=")
    if not sep or not label.strip():
        raise ValueError(f"--as {spec!r}: expected <label>=<kind>:<external_id>")
    kind, sep, external_id = principal.partition(":")
    if not sep or not kind.strip() or not external_id.strip():
        raise ValueError(f"--as {spec!r}: expected <label>=<kind>:<external_id>")
    return Audience(label.strip(), kind.strip(), external_id.strip())


def audiences_from_specs(specs) -> list[Audience]:
    """The requested audiences, always led by the implicit no-principal baseline (the matrix's
    reference column: everything else is read as "what this identity LOSES relative to it")."""
    out = [NO_PRINCIPAL]
    seen = {NO_PRINCIPAL.label}
    for spec in specs or []:
        aud = parse_audience(spec)
        if aud.label in seen:
            raise ValueError(f"duplicate audience label {aud.label!r}")
        seen.add(aud.label)
        out.append(aud)
    return out


def run_rc(args: list[str], timeout: int = DEFAULT_RC_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run `rp <args>` and return the completed process (never raises on a non-zero exit)."""
    try:
        return subprocess.run(
            [_cli(), *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment problem, not logic
        raise RcError("`rp` is not on PATH — run this from a brain checkout with rp installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RcError(f"rp {' '.join(args)} timed out after {timeout}s") from exc


_JSON_START = re.compile(r"[{\[]")


def parse_rc_json(text: str):
    """The JSON payload inside `rp`'s stdout, or None. Tolerates a leading human line."""
    match = _JSON_START.search(text or "")
    if not match:
        return None
    try:
        return json.loads(text[match.start() :])
    except ValueError:
        return None


def _with_json_flags(args: list[str]) -> list[str]:
    """Insert `-o json --raw-output` BEFORE any `--` separator — appended after it they would be
    handed to the remote bash command instead of to `rp`."""
    flags = ["-o", "json", "--raw-output"]
    if "--" in args:
        cut = args.index("--")
        return [*args[:cut], *flags, *args[cut:]]
    return [*args, *flags]


def rc_json_raw(args: list[str], timeout: int = DEFAULT_RC_TIMEOUT_S):
    """`rp <args> -o json --raw-output` → (payload|None, completed process), non-zero exits included.

    `rp dev console bash run` mirrors the REMOTE exit code, so a legitimately failing command makes
    `rp` exit non-zero while still printing the full JSON result. That is the case scope_smoke cares
    about most, so the payload must survive a non-zero exit."""
    done = run_rc(_with_json_flags(args), timeout=timeout)
    return parse_rc_json(done.stdout), done


def rc_json(args: list[str], timeout: int = DEFAULT_RC_TIMEOUT_S):
    """Run `rp <args> -o json --raw-output` and parse the payload. Raises `RcError` on any failure.

    `--raw-output` matters: without it a large payload is spilled to an artifact file and stdout
    carries only a preview, which parses as truncated garbage."""
    payload, done = rc_json_raw(args, timeout=timeout)
    if done.returncode != 0:
        raise RcError(
            f"rp {' '.join(args)} exited {done.returncode}: {(done.stderr or done.stdout).strip()}",
            stderr=done.stderr,
            returncode=done.returncode,
        )
    if payload is None:
        raise RcError(f"rp {' '.join(args)} produced no usable JSON", stderr=done.stderr)
    return payload


def unsupported_flag(err: RcError, flag: str) -> bool:
    """Is this failure an older `rp` that simply has no `flag` yet? Then the caller degrades."""
    text = f"{err} {err.stderr}".lower()
    return flag.lower() in text and ("unknown flag" in text or "unknown shorthand" in text)


def scope_flags(project: str | None, tenant: str | None) -> list[str]:
    flags = []
    if project:
        flags += ["--project", project]
    if tenant:
        flags += ["--tenant", tenant]
    return flags
