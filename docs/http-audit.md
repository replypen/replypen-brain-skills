# Runtime HTTP audit contract

Every HTTP attempt made by the Python runtime's owned transports emits one structured line on stderr:

```text
RC_HTTP_AUDIT {"attempt":1,"bytes":42,...}
```

The host recognizes only that prefix, allowlists the JSON fields, and overwrites correlation from its
trusted execution context. Scripts must not add run/project/action ids. A malformed line is ignored and
audit emission can never fail the request.

## Event shape

Exactly these caller-supplied fields are emitted:

| Field | Meaning |
|---|---|
| `method` | Uppercase HTTP method. |
| `endpoint` | Query-free endpoint template/path. Prefer an explicit template for dynamic paths. |
| `host` | Lowercase provider hostname. Brokered `lib.api` calls use the upstream provider when known. |
| `payload_sha256` | SHA-256 of canonical JSON/form/body bytes; empty requests hash the empty byte string. |
| `bytes` | Byte count of that canonical request payload. |
| `request_body` | Redacted JSON/body shape; binary content is omitted and oversized bodies become hash/size metadata. |
| `status_code` | Provider status, or `null` when transport failed before a response. |
| `duration_ms` | Wall-clock duration of this network attempt. |
| `attempt` | One-based attempt number. |
| `reason` | `initial`, the prior retry cause (for example `retry_status_503`), or a safe transport class. |
| `request_id` | Fresh UUID for this attempt; also sent as `X-Request-ID`. |

Headers and query strings never enter the event. Identity/appointment/customer ids, names/contact
fields, dates/times, treatment/free-text/search/query/variables, credential-looking keys, email values,
bearer/JWT patterns, injected secret env values, and explicit `redact_values` are replaced. Audit
endpoint fallback masks UUIDs, numeric/opaque segments, and percent-encoded values, while preserving
conventional versions such as `v1`; explicit endpoint templates are still the reliable contract:

```python
from lib import api

client.get(
    f"appointments/{appointment_id}",
    endpoint_template="/api/v1/appointments/{appointment_id}",
)
```

## Action writes without a catalog client

Prefer `lib.action.client("provider.write")` when a connector manifest describes the provider. It
inherits the same audit transport and retains the action client's method/retry policy.

For a self-contained hosted action whose provider cannot use a catalog client:

```python
from lib import http

response = http.action_request(
    "PATCH",
    f"https://api.example.test/v1/appointments/{appointment_id}",
    headers={"Authorization": f"Bearer {token}"},
    json={"status": "confirmed"},
    endpoint_template="/v1/appointments/{appointment_id}",
    redact_values=(token,),
)
response.raise_for_status()
```

`action_request` returns the raw `requests.Response`, performs exactly one attempt, and requires the
hosted-action harness markers (`RC_ACTION_PARAMS` + `RC_ACTION_RESULT`) as a misuse assertion. Those
caller-settable env vars are not an authorization boundary: ordinary runs already have raw HTTP, while
write-credential isolation and the egress gateway enforce the real boundary. A caller may issue a
second attempt with explicit `attempt=2, reason="retry_status_503"` only when provider idempotency makes
that safe. It always replaces a caller-supplied `X-Request-ID` with a fresh attempt id.

## Coverage boundary

The runtime bypass test rejects new direct `requests`, `httpx`, or `urllib` calls outside the shared
primitive. Today this covers `lib.http`, `lib.api` (including action clients), MCP, sender history,
dashboard settings, and the bespoke GraphQL/RPC connectors.

`lib.stripe` delegates to the audited REST client. `lib.cloudwatch` keeps boto3 signing/retry behavior
but bridges botocore's per-attempt `before-send`/`needs-retry` hooks into the same event and adds
`X-Request-ID` after signing (an intentionally unsigned correlation header).

The sole exemption is `lib.telemetry`: PostHog's best-effort exporter is ReplyPen-owned internal
telemetry, not project grounding/customer traffic. It remains visible to the egress gateway backstop
but does not emit a recursive HTTP audit event.
