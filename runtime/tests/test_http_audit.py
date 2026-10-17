"""Contract tests for the shared, secret-safe HTTP audit transport."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sys
import types
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import pytest
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import _http_audit, action, api, cloudwatch, http  # noqa: E402
from lib import stripe as legacy_stripe  # noqa: E402


_FIELDS = {
    "method",
    "endpoint",
    "host",
    "payload_sha256",
    "request_body",
    "status_code",
    "duration_ms",
    "attempt",
    "reason",
    "request_id",
    "bytes",
}


def _events(stderr: io.StringIO) -> list[dict]:
    lines = [line for line in stderr.getvalue().splitlines() if line.startswith(_http_audit.AUDIT_PREFIX)]
    return [json.loads(line.removeprefix(_http_audit.AUDIT_PREFIX)) for line in lines]


@responses.activate
def test_json_attempt_emits_exact_schema_hash_redaction_and_request_id():
    url = "https://api.example.test/api/v1/appointments/123?access_token=never-log-this"
    responses.add(responses.POST, url, json={"ok": True}, status=201)
    secret = "action-secret-value"
    body = {
        "email": "alice@example.test",
        "password": secret,
        "operation": "confirm",
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, {"RC_ACTION_DEMO": secret}, clear=True), redirect_stderr(stderr):
        response = _http_audit.request(
            "POST",
            url,
            json_body=body,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=3,
        )

    assert response.status_code == 201
    [event] = _events(stderr)
    assert set(event) == _FIELDS
    assert event == {
        **event,
        "method": "POST",
        "endpoint": "/api/v1/appointments/{param}",
        "host": "api.example.test",
        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "request_body": {
            "email": "[redacted]",
            "password": "[redacted]",
            "operation": "confirm",
        },
        "status_code": 201,
        "attempt": 1,
        "reason": "initial",
        "bytes": len(canonical),
    }
    assert event["duration_ms"] >= 0
    assert responses.calls[0].request.headers["X-Request-ID"] == event["request_id"]
    assert secret not in stderr.getvalue()
    assert "access_token" not in stderr.getvalue()
    assert "alice@example.test" not in stderr.getvalue()


@responses.activate
def test_lib_http_uses_audited_transport_and_explicit_template():
    responses.add(responses.GET, "https://status.example.test/incidents/abc", json={"ok": True})
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert http.get_json(
            "https://status.example.test/incidents/abc",
            endpoint_template="/incidents/{incident_id}",
        ) == {"ok": True}
    [event] = _events(stderr)
    assert event["endpoint"] == "/incidents/{incident_id}"
    assert event["method"] == "GET"
    assert event["bytes"] == 0


@responses.activate
def test_public_action_request_is_guarded_and_audited():
    url = "https://dentadmin.example.test/api/v1/appointments/123"
    responses.add(responses.DELETE, url, json={"ok": True})
    with mock.patch.dict(os.environ, {}, clear=True), pytest.raises(RuntimeError, match="hosted action"):
        http.action_request("DELETE", url)

    stderr = io.StringIO()
    env = {"RC_ACTION_PARAMS": "/tmp/params.json", "RC_ACTION_RESULT": "/tmp/result.json"}
    with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(stderr):
        response = http.action_request(
            "DELETE",
            url,
            endpoint_template="/api/v1/appointments/{appointment_id}",
        )
    assert response.status_code == 200
    [event] = _events(stderr)
    assert event["method"] == "DELETE"
    assert event["endpoint"] == "/api/v1/appointments/{appointment_id}"


@responses.activate
def test_dentai_appointment_body_projects_patient_and_treatment_pii_closed():
    url = "https://clickdoc.example.test/api/v1/appointments"
    responses.add(responses.POST, url, json={"success": True})
    body = {
        "user_id": 77,
        "agenda_id": 1567,
        "patient_id": 123456,
        "first_name": "Alice",
        "last_name": "Patient",
        "date": "2026-07-20",
        "start_time": "10:30",
        "end_time": "11:00",
        "subject": "Root canal treatment",
        "slot_id": 9988,
        "appointment_create_type": "patient",
    }
    stderr = io.StringIO()
    env = {"RC_ACTION_PARAMS": "/tmp/params.json", "RC_ACTION_RESULT": "/tmp/result.json"}
    with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(stderr):
        http.action_request(
            "POST",
            url,
            data=body,
            endpoint_template="/api/v1/appointments",
        )
    [event] = _events(stderr)
    projected = event["request_body"]
    for key in (
        "user_id", "patient_id", "first_name", "last_name", "date", "start_time",
        "end_time", "subject", "slot_id", "appointment_create_type",
    ):
        assert projected[key] == "[redacted]"
    serialized = json.dumps(event)
    for raw in ("123456", "Alice", "Root canal treatment", "2026-07-20", "10:30"):
        assert raw not in serialized


@responses.activate
def test_api_retry_emits_one_event_per_attempt_with_fresh_request_ids():
    url = "https://api.example.test/v1/things"
    responses.add(responses.GET, url, json={"error": "busy"}, status=503)
    responses.add(responses.GET, url, json={"ok": True}, status=200)
    client = api.Client(
        manifest=api.Manifest(key="demo", base_url="https://api.example.test/v1", auth=api.Auth(strategy="none")),
        max_retries=1,
        _sleeper=lambda _seconds: None,
    )
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert client.get("things") == {"ok": True}
    events = _events(stderr)
    assert [(e["attempt"], e["reason"], e["status_code"]) for e in events] == [
        (1, "initial", 503),
        (2, "retry_status_503", 200),
    ]
    assert len({e["request_id"] for e in events}) == 2
    assert [call.request.headers["X-Request-ID"] for call in responses.calls] == [
        event["request_id"] for event in events
    ]


@responses.activate
def test_action_write_client_uses_same_audit_primitive():
    manifest = api.Manifest(
        key="demo",
        base_url="https://write.example.test/v2",
        auth=api.Auth(strategy="bearer"),
    )
    responses.add(responses.PATCH, "https://write.example.test/v2/items/42", json={"ok": True})
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, {"RC_ACTION_DEMO": "write-secret"}, clear=True), redirect_stderr(stderr):
        client = action.client("demo.write", manifest=manifest)
        assert client.patch(
            "items/42",
            json={"status": "done", "customer_email": "alice@example.test"},
            idempotency_key="item-42",
            endpoint_template="/v2/items/{item_id}",
        ) == {"ok": True}
    [event] = _events(stderr)
    assert event["method"] == "PATCH"
    assert event["endpoint"] == "/v2/items/{item_id}"
    assert event["request_body"] == {"status": "done", "customer_email": "[redacted]"}
    assert "write-secret" not in stderr.getvalue()
    assert responses.calls[0].request.headers["Authorization"] == "Bearer write-secret"


def test_transport_failure_is_audited_without_logging_exception_message():
    def fail(*_args, **_kwargs):
        raise RuntimeError("secret-bearing https://example.test/?token=leak")

    stderr = io.StringIO()
    with redirect_stderr(stderr), pytest.raises(RuntimeError, match="secret-bearing"):
        _http_audit.request("GET", "https://example.test/v1/ping", sender=fail)
    [event] = _events(stderr)
    assert event["status_code"] is None
    assert event["reason"] == "initial:transport_RuntimeError"
    assert "token=leak" not in stderr.getvalue()


def test_cloudwatch_botocore_hook_emits_every_signed_attempt():
    class Request:
        method = "POST"
        url = "https://logs.eu-central-1.amazonaws.com/"
        headers: dict[str, str] = {}
        body = json.dumps({
            "logGroupNames": ["/customer/app"],
            "queryString": "fields @message | filter @message like /alice@example.test/",
        }).encode()

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    auditor = cloudwatch._BotocoreHTTPAuditor(("access-key", "secret-key"))
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        first = Request()
        first.headers = {}
        auditor.before_send(first, event_name="before-send.cloudwatch-logs.StartQuery")
        auditor.needs_retry(response=(Response(503), {}), attempts=1)
        second = Request()
        second.headers = {}
        auditor.before_send(second, event_name="before-send.cloudwatch-logs.StartQuery")
        auditor.needs_retry(response=(Response(200), {}), attempts=2)
        third = Request()
        third.headers = {}
        auditor.before_send(third, event_name="before-send.cloudwatch-logs.DescribeLogGroups")
        auditor.needs_retry(response=(Response(200), {}), attempts=1)

    events = _events(stderr)
    assert [(e["attempt"], e["reason"], e["status_code"]) for e in events] == [
        (1, "initial", 503),
        (2, "retry_status_503", 200),
        (1, "initial", 200),
    ]
    assert [e["endpoint"] for e in events] == ["/StartQuery", "/StartQuery", "/DescribeLogGroups"]
    assert all(e["host"] == "logs.eu-central-1.amazonaws.com" for e in events)
    assert all(e["request_body"]["queryString"] == "[redacted]" for e in events)
    assert first.headers["X-Request-ID"] == events[0]["request_id"]
    assert second.headers["X-Request-ID"] == events[1]["request_id"]
    assert third.headers["X-Request-ID"] == events[2]["request_id"]
    assert "alice@example.test" not in stderr.getvalue()


def test_cloudwatch_client_installs_botocore_attempt_hooks():
    events = mock.Mock()
    fake_client = types.SimpleNamespace(meta=types.SimpleNamespace(events=events))
    fake_boto3 = types.SimpleNamespace(client=mock.Mock(return_value=fake_client))
    connection = json.dumps({
        "access_key_id": "access-key",
        "secret_access_key": "secret-key",
        "region": "eu-central-1",
    })
    with mock.patch.dict(sys.modules, {"boto3": fake_boto3}), mock.patch.dict(
        os.environ, {"RC_CONN_CLOUDWATCH": connection}, clear=True,
    ):
        assert cloudwatch._client() is fake_client
    assert [call.args[0] for call in events.register.call_args_list] == [
        "before-send.cloudwatch-logs.*",
        "needs-retry.cloudwatch-logs.*",
    ]
    assert isinstance(fake_client._replypen_http_auditor, cloudwatch._BotocoreHTTPAuditor)


@responses.activate
def test_legacy_stripe_helpers_delegate_to_audited_rest_client():
    responses.add(
        responses.GET,
        "https://api.stripe.com/v1/customers/cus_123",
        json={"id": "cus_123"},
    )
    responses.add(
        responses.GET,
        "https://api.stripe.com/v1/invoices?customer=cus_123&limit=1",
        json={"data": [{"id": "in_1"}]},
    )
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, {"STRIPE_RESTRICTED_KEY": "rk_test_secret"}, clear=True), redirect_stderr(stderr):
        assert legacy_stripe.customer("cus_123") == {"id": "cus_123"}
        assert legacy_stripe.latest_invoice("cus_123") == {"id": "in_1"}
    events = _events(stderr)
    assert [event["endpoint"] for event in events] == ["/v1/customers/{customer_id}", "/v1/invoices"]
    assert "rk_test_secret" not in stderr.getvalue()


def test_audit_emission_failure_never_changes_request_result():
    class Response:
        status_code = 204

    class BrokenStderr:
        def write(self, _value):
            raise OSError("sink failed")

        def flush(self):
            raise OSError("sink failed")

    with mock.patch.object(sys, "stderr", BrokenStderr()):
        assert _http_audit.request(
            "GET",
            "https://example.test/ping",
            sender=lambda *_a, **_kw: Response(),
        ).status_code == 204
    with mock.patch.object(_http_audit, "_known_secret_values", side_effect=RuntimeError("redactor failed")):
        assert _http_audit.request(
            "GET",
            "https://example.test/ping",
            sender=lambda *_a, **_kw: Response(),
        ).status_code == 204


def test_runtime_has_no_raw_python_http_bypass():
    """New requests/httpx/urllib calls must route through ``lib._http_audit``.

    SDK-owned transports are deliberately outside this AST check. ``lib.cloudwatch`` bridges
    botocore's before-send/needs-retry hooks into the same contract. ``lib.telemetry`` (PostHog) is
    the sole exemption because it is ReplyPen-owned best-effort internal telemetry, not project
    grounding/customer traffic.
    """
    lib_root = Path(__file__).resolve().parents[1] / "lib"
    violations: list[str] = []
    http_methods = {"request", "get", "post", "put", "patch", "delete", "urlopen"}
    for path in sorted(lib_root.rglob("*.py")):
        if path.name == "_http_audit.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "urlopen":
                violations.append(f"{path.relative_to(lib_root)}:{node.lineno}: urlopen")
            if not isinstance(func, ast.Attribute) or func.attr not in http_methods:
                continue
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in {"requests", "_requests", "httpx", "urllib"}:
                violations.append(f"{path.relative_to(lib_root)}:{node.lineno}: {owner.id}.{func.attr}")
    assert violations == []
