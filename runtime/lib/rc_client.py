"""Small, typed wrapper around the local ``rp`` CLI for deterministic scripts.

This module is for *local brain development* scripts, where OAuth and project scope already live in
``rp``.  It is deliberately not a production-runtime helper: the production loop has no ``rp``
binary and should use the injected ``lib.db`` / ``lib.fs`` capabilities instead.

The console's machine contract is JSON with ``rows`` as arrays plus an ordered ``columns`` list.  A
truncated query is unsafe to accidentally consume, so it raises by default; pass
``allow_truncated=True`` only when an explicitly partial result is useful.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


DEFAULT_DATABASE = "prod"
_DEFAULT_BASH_TIMEOUT_S = 120
_LOCAL_TIMEOUT_GRACE_S = 30


class RCClientError(RuntimeError):
    """Base class for an ``rp`` failure, retaining its typed CLI exit code."""

    exit_code = 1

    def __init__(self, message: str, *, status: int | None = None, fields: Any = None):
        super().__init__(message)
        self.status = status
        self.fields = fields


class UsageError(RCClientError):
    """Invalid local input or CLI invocation (exit 1)."""

    exit_code = 1


class AuthenticationError(RCClientError):
    """OAuth login/scope failure (exit 2)."""

    exit_code = 2


class TruncatedError(RCClientError):
    """The server returned a partial query result (exit 3)."""

    exit_code = 3


class RemoteCommandError(RCClientError):
    """Workspace bash exited non-zero or timed out (exit 4)."""

    exit_code = 4


class TransportError(RCClientError):
    """Server/network failure (exit 5)."""

    exit_code = 5


_ERROR_TYPES = {
    1: UsageError,
    2: AuthenticationError,
    3: TruncatedError,
    4: RemoteCommandError,
    5: TransportError,
}


@dataclass(frozen=True)
class Result:
    """One console query response; each row aligns positionally with ``columns``."""

    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False


@dataclass(frozen=True)
class BashResult:
    """Output from the guarded workspace bash plane."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def __iter__(self):
        """Allow ``exit_code, stdout, stderr = bash(...)`` as documented."""
        yield self.exit_code
        yield self.stdout
        yield self.stderr


Runner = Callable[[Sequence[str], float | None], subprocess.CompletedProcess[str]]


def _default_database(database: str | None) -> str:
    return database or os.environ.get("RC_CONSOLE_DATABASE") or os.environ.get("RC_DB_DEFAULT") or DEFAULT_DATABASE


def _default_runner(args: Sequence[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def _decode_error(stdout: str, stderr: str, returncode: int) -> RCClientError:
    message = stderr.strip() or stdout.strip() or f"CLI exited {returncode}"
    status = None
    fields = None
    try:
        envelope = json.loads(stdout)
        error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
        if isinstance(error, dict) and error:
            message = str(error.get("message") or message)
            status = error.get("status")
            fields = error.get("fields")
        elif isinstance(envelope, dict) and returncode == 3 and envelope.get("truncated") is True:
            message = "query result was truncated; rerun with all=True or allow_truncated=True"
    except json.JSONDecodeError:
        pass
    error_type = _ERROR_TYPES.get(returncode, TransportError)
    return error_type(message, status=status, fields=fields)


class Client:
    """Configurable client; module-level functions use a default instance per call."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        database: str | None = None,
        runner: Runner | None = None,
    ):
        # `rp`, else `rc`: the pre-rebrand name of the same CLI.
        self.executable = executable or ("rp" if shutil.which("rp") else "rc")
        self.database = database
        self._runner = runner or _default_runner

    def _run(
        self,
        args: Iterable[str],
        *,
        timeout: float | None = None,
        allowed_exit_codes: Iterable[int] = (),
    ) -> dict[str, Any]:
        command = [self.executable, "-o", "json", *args]
        try:
            completed = self._runner(command, timeout)
        except FileNotFoundError as exc:
            raise TransportError(f"CLI executable not found: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteCommandError(f"{self.executable} command timed out after {timeout}s") from exc
        if completed.returncode and completed.returncode not in set(allowed_exit_codes):
            raise _decode_error(completed.stdout, completed.stderr, completed.returncode)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TransportError(f"{self.executable} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TransportError(f"{self.executable} returned a non-object JSON response")
        if "error" in payload:
            raise _decode_error(completed.stdout, completed.stderr, completed.returncode)
        return payload

    def query(
        self,
        sql: str,
        params: dict[str, str] | Iterable[tuple[str, str]] | None = None,
        all: bool = False,
        *,
        database: str | None = None,
        allow_truncated: bool = False,
    ) -> Result:
        """Execute parameterized SQL through ``rp`` and return lossless rows.

        ``params`` values are deliberately text, matching repeated ``--param key=value``.  Use
        ``all=True`` for a complete export; otherwise a server-declared truncation raises.
        """
        args = ["dev", "console", "database", "query", _default_database(database or self.database), sql]
        if all:
            args.append("--all")
        if allow_truncated:
            args.append("--allow-truncated")
        args.extend(["--format", "json", "--out", "-"])
        pairs = params.items() if isinstance(params, dict) else (params or ())
        for key, value in pairs:
            args.extend(["--param", f"{key}={value}"])
        payload = self._run(args, allowed_exit_codes=(3,) if allow_truncated else ())
        columns = payload.get("columns")
        rows = payload.get("rows")
        truncated = payload.get("truncated", False)
        if not isinstance(columns, list) or not builtins.all(isinstance(column, str) for column in columns):
            raise TransportError("CLI query response has invalid columns")
        if not isinstance(rows, list) or not builtins.all(isinstance(row, list) for row in rows):
            raise TransportError("CLI query response has invalid rows")
        if not isinstance(truncated, bool):
            raise TransportError("CLI query response has invalid truncated flag")
        result = Result(columns=columns, rows=rows, truncated=truncated)
        if result.truncated and not allow_truncated:
            raise TruncatedError("query result was truncated; rerun with all=True or allow_truncated=True")
        return result

    def query_to_csv(
        self,
        sql: str,
        path: str | Path,
        params: dict[str, str] | Iterable[tuple[str, str]] | None = None,
        all: bool = False,
        *,
        database: str | None = None,
        allow_truncated: bool = False,
    ) -> Path:
        """Stream a query as CLI-rendered CSV to ``path`` without materializing its rows."""
        destination = Path(path)
        args = ["dev", "console", "database", "query", _default_database(database or self.database), sql]
        if all:
            args.append("--all")
        if allow_truncated:
            args.append("--allow-truncated")
        args.extend(["--format", "csv", "--out", str(destination)])
        pairs = params.items() if isinstance(params, dict) else (params or ())
        for key, value in pairs:
            args.extend(["--param", f"{key}={value}"])
        self._run(args, allowed_exit_codes=(3,) if allow_truncated else ())
        return destination

    def bash(self, cmd: str, timeout: int | None = None) -> BashResult:
        """Run one workspace command and return its decoded result.

        A remote non-zero exit or timeout returns its decoded ``BashResult``. The local process
        timeout is the requested remote timeout plus 30 seconds for request/setup overhead.
        """
        args = ["dev", "console", "bash", "run", cmd, "--out", "-"]
        if timeout is not None:
            args.extend(["--timeout", str(timeout)])
        remote_timeout = timeout if timeout is not None else _DEFAULT_BASH_TIMEOUT_S
        payload = self._run(
            args,
            timeout=remote_timeout + _LOCAL_TIMEOUT_GRACE_S,
            allowed_exit_codes=(4,),
        )
        exit_code = payload.get("exit_code", 0)
        stdout = payload.get("stdout", "")
        stderr = payload.get("stderr", "")
        timed_out = payload.get("timed_out", False)
        if not isinstance(exit_code, int) or not isinstance(stdout, str) or not isinstance(stderr, str) or not isinstance(timed_out, bool):
            raise TransportError("CLI bash response has invalid fields")
        return BashResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)

    def file_get(self, remote: str, local: str | Path) -> Path:
        """Fetch one permitted workspace or ``/tmp`` file to ``local``."""
        destination = Path(local)
        # ``file get --out`` writes bytes locally; do not ask the CLI to also print a JSON body.
        self._run(["dev", "console", "file", "get", remote, "--out", str(destination)])
        return destination


def query(sql: str, params: dict[str, str] | Iterable[tuple[str, str]] | None = None, all: bool = False, **kwargs: Any) -> Result:
    return Client().query(sql, params=params, all=all, **kwargs)


def query_to_csv(sql: str, path: str | Path, **kwargs: Any) -> Path:
    return Client().query_to_csv(sql, path, **kwargs)


def bash(cmd: str, timeout: int | None = None) -> BashResult:
    return Client().bash(cmd, timeout=timeout)


def file_get(remote: str, local: str | Path) -> Path:
    return Client().file_get(remote, local)


__all__ = [
    "AuthenticationError", "BashResult", "Client", "RCClientError", "RemoteCommandError",
    "Result", "TransportError", "TruncatedError", "UsageError", "bash", "file_get", "query",
    "query_to_csv",
]
