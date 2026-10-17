from __future__ import annotations

import json
import subprocess

import pytest

from lib import rc_client


def _runner(*, payload=None, returncode=0, stderr="", seen=None):
    def run(args, timeout):
        if seen is not None:
            seen.extend([list(args), timeout])
        return subprocess.CompletedProcess(args, returncode, json.dumps(payload or {}), stderr)

    return run


def test_query_builds_machine_safe_command_and_preserves_array_rows(monkeypatch):
    seen = []
    client = rc_client.Client(executable="rc", runner=_runner(payload={"columns": ["id", "id"], "rows": [[1, 2]], "truncated": False}, seen=seen))

    result = client.query("select * from things where name = @name", {"name": "A,B"}, all=True, database="billing")

    assert result == rc_client.Result(columns=["id", "id"], rows=[[1, 2]])
    assert seen[0] == [
        "rc", "-o", "json", "dev", "console", "database", "query", "billing",
        "select * from things where name = @name", "--all", "--format", "json", "--out", "-",
        "--param", "name=A,B",
    ]


def test_query_uses_configured_default_database(monkeypatch):
    seen = []
    monkeypatch.setenv("RC_CONSOLE_DATABASE", "warehouse")
    client = rc_client.Client(runner=_runner(payload={"columns": [], "rows": [], "truncated": False}, seen=seen))

    client.query("select 1")

    assert seen[0][7] == "warehouse"


def test_query_raises_for_truncation_unless_explicitly_allowed():
    client = rc_client.Client(runner=_runner(payload={"columns": ["id"], "rows": [[1]], "truncated": True}, returncode=3))

    with pytest.raises(rc_client.TruncatedError, match="all=True"):
        client.query("select 1")
    seen = []
    allowed = rc_client.Client(runner=_runner(payload={"columns": ["id"], "rows": [[1]], "truncated": True}, returncode=3, seen=seen))

    assert allowed.query("select 1", allow_truncated=True).truncated is True
    assert "--allow-truncated" in seen[0]


def test_exit_codes_map_to_typed_errors():
    client = rc_client.Client(runner=_runner(payload={"error": {"message": "login expired", "status": 401}}, returncode=2))

    with pytest.raises(rc_client.AuthenticationError) as excinfo:
        client.query("select 1")
    assert excinfo.value.status == 401


def test_query_to_csv_delegates_streaming_rendering_to_rc(tmp_path):
    seen = []
    client = rc_client.Client(executable="rc", runner=_runner(payload={"path": "ignored"}, seen=seen))
    path = tmp_path / "rows.csv"

    result = client.query_to_csv("select id from things where state = @state", path, {"state": "queued"}, all=True)

    assert result == path
    assert seen[0] == [
        "rc", "-o", "json", "dev", "console", "database", "query", "prod",
        "select id from things where state = @state", "--all", "--format", "csv", "--out", str(path),
        "--param", "state=queued",
    ]


def test_bash_returns_remote_nonzero_payload_and_adds_local_grace():
    seen = []
    client = rc_client.Client(runner=_runner(payload={"exit_code": 7, "stdout": "ok", "stderr": "failed", "timed_out": False}, returncode=4, seen=seen))

    exit_code, stdout, stderr = client.bash("echo ok", timeout=30)

    assert (exit_code, stdout, stderr) == (7, "ok", "failed")
    assert seen[0][-4:] == ["--out", "-", "--timeout", "30"]
    assert seen[1] == 60


def test_bash_default_timeout_allows_remote_default_plus_grace():
    seen = []
    client = rc_client.Client(runner=_runner(payload={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}, seen=seen))

    client.bash("true")

    assert seen[1] == 150


def test_file_get_returns_destination_after_cli_confirmation(tmp_path):
    seen = []
    destination = tmp_path / "result.csv"
    client = rc_client.Client(runner=_runner(payload={"path": str(destination)}, seen=seen))

    assert client.file_get("/tmp/replypen-out/rows.csv", destination) == destination
    assert seen[0][-5:] == ["file", "get", "/tmp/replypen-out/rows.csv", "--out", str(destination)]


def test_default_executable_prefers_rp_and_falls_back_to_rc(monkeypatch):
    monkeypatch.setattr(rc_client.shutil, "which", lambda name: "/bin/rp" if name == "rp" else None)
    assert rc_client.Client().executable == "rp"
    monkeypatch.setattr(rc_client.shutil, "which", lambda name: None)
    assert rc_client.Client().executable == "rc"
