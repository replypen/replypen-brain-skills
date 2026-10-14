# Actions

Everything else in a brain is read-only diagnosis. An action is the deliberate state-changing plane:
the run requests an approved action with parameters; host gates decide whether it executes now or
waits for human confirmation.

Start with [what runs where and which verdicts block](action-boundaries.md) for the read/write
boundary and preflight/policy sequence. Read [side effects](side-effects.md) before reporting results.

## Product Flow

```mermaid
sequenceDiagram
    participant Run as Agent run
    participant Human as Reviewer
    participant Exec as Executor
    participant App as Project app/API
    Run->>Human: proposes action_id + params + digest
    Note over Run: run ends read-only
    Human->>Exec: confirms
    Exec->>App: executes approved digest
    App-->>Exec: structured result
```

A draft that says "booked", "cancelled", or "refunded" is not proof of mutation. Check the action
lifecycle: preflight blocked, proposed and pending, confirmed/executed, or failed.

## Brain Layout

```text
actions/<id>/
  manifest.yaml
  script.rb | script.py
  preflight.py
  policy.py
```

- `manifest.yaml` describes the action, param schema, any hosted write connections, and its `autonomy`.
  `description` is for the agent; `display_name` + `customer_description` are customer-facing.
- Optional `surfaces:` limits host awareness, proposal, and execution to `chat`, `dashboard_chat`,
  `email`, `intercom`, `whatsapp`, `compose`, `prompt_api`, `mcp`, `embassy`, or `console`. `email`
  covers every mailbox transport. Legacy `gmail`, `outlook`, and `imap` values WARN in lint and
  normalize to `email`; new manifests must not use them. Omission means all; unknown values fail lint
  and registry loading. Files remain mounted wholesale: this gates action eligibility, not filesystem
  visibility.
- Optional `exclusive_with: [<action_id>, ...]` names the OTHER answers to the same single decision
  (confirm-vs-cancel a hold, adjust-vs-cancel a subscription). When the reviewer picks one, every listed
  counterpart proposed in the same run with the same shared param values is declined and its buttons
  disappear everywhere. Symmetric: declaring it on one side is enough. Every id must exist in the same
  registry (a typo fails loading), and an action may not list itself. Only use it for genuine
  either/or pairs — two proposals of the SAME action (one per patient, per child) are never exclusive.
- `preflight.py`, when present, is read-only and blocks unsafe/mis-grounded params before proposal.
- `policy.py`, when present, is read-only and decides per-invocation whether an `autonomy: policy` action
  auto-executes or escalates to a human (see [Autonomy](#autonomy-human--policy--auto)).
- `script.rb` is customer-hosted Embassy mode.
- `script.py` is hosted Python mode and should use `lib.action`.

Prefer single-purpose, reviewer-legible actions with one concrete business outcome. Split multi-system
workflows unless the combined operation is atomic and idempotent.

**Write-plane stance:** ReplyPen installs no RLS on customer tables. Tenant scope is host-stamped and
tenant-selection params are rejected; deterministic, human-vetted, digest-pinned actions must scope
every query with that binding. Project rule `actions-only-host-stamped-env` backs up review.

Action docs/runbooks should put exact safety guards and verification checks near the top: required
evidence, disqualifying states, preflight expectations, post-execution proof, and when to refuse or
escalate.

## Script Hygiene

`script.py` / `preflight.py` are single files by construction — only `sha256(script.py)` is pinned and
shipped — so the natural failure mode is copy-paste growth. The offline test tier (`brain_test.py`,
via `lib.action_lint`) guards it:

| Check | Verdict | Rule |
|---|---|---|
| Size budget | WARN ≥ 64 KiB, FAIL ≥ 96 KiB | The executor ships the file inline (argv ~128 KiB after base64). Override in `actions/lint.yaml`: `script_size: {warn_kb, fail_kb}`. |
| Duplicate helpers | WARN | A `_private` def identical in ≥ 3 maintained sources → hoist. Same name, different bodies in ≥ 3 sources → drifted copies (this has produced a real cross-tenant bug). Generated artifacts are analysed through the exact adjacent `.src.py` declared by their standard banner; vague markers are not trusted. |
| Dead private names | WARN | Module-level `_name` never referenced in its own file → delete. |

Conventions:

- **Script = one business outcome's orchestration.** Param reads, the specific mutation, verify-after-
  mutate, the reviewer-facing result. Nothing generic lives here twice.
- **Generic → `lib.action`** (params, results, HTTP retries, credential lookup, write connectors).
  **Project-generic → a shared project module** (API client/login, tenant scoping, date/label
  formatting, notify tail). Copy-pasting a helper into a third script is the signal to hoist it.
- **Split when a script exceeds the soft budget** or serves two outcomes with different guards; keep
  each action reviewer-legible in one sitting.
- A project bundler may keep readable shared/`.src.py` code while emitting one digest-pinned hosted
  artifact. The lint still checks the shipped artifact's raw size, but checks helper drift in the
  declared source so unavoidable single-file transport copies do not masquerade as maintained drift.
- **Tenant settings come from `lib.tenant`**, never from a param the model filled in from a rendered
  `{{ }}` — see [reading a tenant setting from Python](tenant-settings.md).
- **A preflight/policy knows it is not the run**: `lib.runctx.plane()` is `"action"` there, with every
  ingress field null — [what kind of run is this](run-context.md).
- **Delete, don't park.** Unused `_constants`/helpers left "for later" are the growth curve.

## Customer-Facing Copy

```yaml
# actions/<id>/manifest.yaml
description: "Agent-facing: when to propose this action and what it does."
display_name: "Abonnement opzeggen"          # customer-facing, optional
customer_description: "Zegt het abonnement op per einde van de lopende periode."
color: red                                    # confirm-button color, optional
```

Both are optional (fallback: prettified id / `description`) and are shown to the **end customer**, so
write them in the project's customer language. `display_name` becomes the mailbox confirm-button label;
`customer_description` is the plain-language intent on the confirm page and in the action catalog.

`color` tints the mailbox confirm button. Closed named set — pick by what the action *does*:

- `green` — confirm / approve / positive outcome (the default when omitted)
- `red` — destructive: cancel, delete, remove
- `orange` — caution: modify existing state, transitions with side effects
- `blue` — neutral: send, notify, informational
- `gray` — secondary / low-stakes housekeeping. The only non-cosmetic colour: a gray action that succeeded with an empty summary is omitted from the reviewer note's "Executed automatically in this run" ledger (it stays in the canonical executed-actions list); failures and summaries always show.
- `teal` — lookup / verify / re-check
- `purple` — special / uncommon operations

Unknown or absent values fall back to green — a color can never fail an action.

### `resource_url` — link to the record

Per-invocation, returned by the **preflight** (`preflight.result(..., resource_url=...)`) — not the
manifest, not the LLM. An absolute `https` URL to the record the action will modify, in the customer's
own admin UI. The host renders it as a link on the confirm button/card/page. Non-`http(s)` or invalid
values are dropped silently, so a bad URL can never fail an action.

### `summary` — the confirm-surface blurb

The preflight `summary` is the human's one-line blurb on the confirm surface, so keep it ≤ ~60 chars in
`<who/what> · <what changes>` shape — e.g. `Maité · afwezig di 19/09`. No full sentences; details belong
behind `resource_url`. The host now shows the preflight summary instead of the agent's `intent` when
both exist.

### `param_labels` — a grounded human label per param

The card and confirm page show each param as a row. For a param that is an id (a record uuid), the
**preflight** may return `param_labels`: `{"<param name>": {"value": "<the validated param, verbatim>",
"label": "Maité J. · Basketbalkamp (2026-0142)"}}`. The host shows the label as the row's value and folds
the raw id under "Technical details" — but only when `value` equals the param it will execute with;
a mismatch or an unknown name drops the label (raw value stays). Labels are deterministic: resolve the
supplied id with a tenant-scoped query, never a model-written name and never a new param. Plain text,
≤120 characters, no e-mail/RRN/medical data. Add the key to the result dict yourself
(`out["param_labels"] = {...}`) until `preflight.result` grows the kwarg. Distinct from `observed`,
which stays model/reviewer grounding and never reaches an embed card.

### `label` / `display` — how a param reads on the card

Per param in `manifest.yaml`: `label: Uitleg` (customer-facing, project language; absent → the
humanized name) and `display: default | details | markdown`. `details` folds a technical row (session
ids, bound identities) under one collapsed "Technical details" disclosure — still sent and inspectable,
never hidden; `from_claim` params fold there by default. `markdown` renders a long text through the
sanitized markdown pipeline, clamped with a "Show all" toggle. Nothing in the card may make the
identity, target or effect of the action unclear: keep those rows `default`.

## Param Types

`manifest.yaml` params are a small closed vocabulary:

- `string` (default), `integer`, `number`, `boolean`, `string[]`, `object` for JSON values.
- `attachment` for a user-provided email/dashboard file. The agent proposes an attachment id; hosted
  execution rewrites it to a file descriptor, consumed with `p.file("<name>")`.
- `generated_file` for a host-rendered artifact. The manifest must name a `generator`; currently
  `email_message_pdf` renders an email message to PDF from
  `{"kind":"email_message_pdf","message_id":"<local_message_id>"}`. Before `script.py` starts, the host
  rewrites it to the same file descriptor shape consumed with `p.file("<name>")`. Generated-file params
  are hosted-only and cannot have `preflight.py`.

Use `format`, `pattern`, and `enum` on scalar params; use `accept` as a MIME allowlist for file params.

## Autonomy (`human` | `policy` | `auto`)

By default an action is **human-gated**: the run proposes, a reviewer confirms, and only then does the
executor run — the flow the diagram above shows. A manifest may raise its **autonomy** so eligible actions
execute **mid-loop** (inside the run, via the agent's `action` tool) — the agent then sees the real
`{ok, summary}` and writes a **factual** draft from it, instead of the optimistic "as-if-done" draft used
for human-gated proposals.

```yaml
# actions/<id>/manifest.yaml
autonomy: human   # default (omit = human)
# autonomy: policy   # a per-invocation policy.py decides allow → auto-execute, deny → escalate to human
# autonomy: auto     # always auto-execute — reserve for idempotent / low-blast-radius actions
```

**You only DECLARE autonomy; the host GATES it.** The effective autonomy is
`min(manifest, project cap)`, then clamped to `human` by any of these floors — so a raised manifest never
means "always auto":

- `projects.action_autonomy_max` — an operator-set ceiling (`human` by default). It can only lower, never
  raise. Read it with `rc --project <p> project action-settings get -o json` (field `action_autonomy_max`); an
  operator raises it in the console Action settings. Until the operator raises it, every action stays
  `human` no matter what the manifest says.
- `risk: high` in the manifest → always `human` (risk is now **load-bearing**, not just informational).
- **brain-TEST runs** (a `--brain-ref` dev run) → always `human`, so testing a new `auto` action never
  fires a real write.
- **Decision authority:** no in-band confirmer means human. Requester-confirmed automation also
  requires `confirm: requester` and a claim-bound input. Reviewer/admin surfaces can use effective
  policy/auto autonomy. Chat Studio previews and shadow delivery stay human.
- These gates apply to hosted and Embassy execution; execution mode alone does not force human review.

The catalog the agent sees labels each action with its **effective** autonomy (`(auto)`, `(policy-gated)`,
or nothing for human) — so the agent is never told it can auto-run something the cap or a floor forbids.

### `policy.py` — the per-invocation gate

`autonomy: policy` **requires** a `policy.py` (the host refuses to resolve the action without one). It is
**orthogonal to preflight**: preflight answers *"will these params do the intended thing?"*. It runs
in the read-only workspace: a completed rejection blocks execution; a technical failure requires
human review. Policy answers *"is a human needed for THIS invocation?"*
(authorization). An action may have either, both, or neither.

Because the verdict **replaces a human**, policy runs **host-side** in a fresh one-shot **read-only
grounding container** (the grounding `.env` read DSNs, brain `:ro`, no write plane, no agent) — a run
cannot forge it. Contract, mirroring preflight:

- params in via `$RC_POLICY_PARAMS` (JSON), verdict out to `$RC_POLICY_RESULT`:
  `{"allow": bool, "reason": "human-readable", "observed": {...}?}`.
- `reason` rides back to the agent and onto the human-confirm button on a deny, so write it for the reviewer.
- **Fail-closed to escalate, NEVER allow**: a crash, timeout, non-zero exit, unparseable result, or a
  missing `allow` key all resolve to `deny` → the action escalates to the ordinary human-confirm proposal.
  A `deny` is never a hard refusal — the capability still runs, just with a human in the loop.

```python
#!/usr/bin/env python3
# actions/refund_order/policy.py — auto-refund small orders, escalate the rest.
import json, os
from lib import db  # grounding read plane (RC_TENANT_ID/RC_TENANT_SLUG also available when tenant-bound)

p = json.load(open(os.environ["RC_POLICY_PARAMS"]))
order = db.query_one("select total_cents, status from orders where id = %s", [p["order_id"]])

allow = bool(order) and order["status"] == "paid" and order["total_cents"] <= 5000
reason = "order ≤ €50 and paid — safe to auto-refund" if allow else "over the €50 auto-refund cap or not in a refundable state — needs a human"

json.dump({"allow": allow, "reason": reason, "observed": {"total_cents": order and order["total_cents"]}},
          open(os.environ["RC_POLICY_RESULT"], "w"))
```

**Determinism is burned at approval.** `policy.py` is part of the action's composite **approval digest**
(manifest + script + policy + preflight), so once an action version is approved the rule that gates its
autonomy cannot silently drift. Put project-specific thresholds (refund caps, allowed settings) **in the
script or in files in this brain** — brains are per-project, so this stays data-not-code with no host
branching. `approved_by` on the settled row records exactly which rule authorized a write
(`policy:<approval_digest>` or `autonomy:auto`).

### Best practices

- **Default to `human`.** Raise autonomy only for an action whose blast radius you would sign off on
  unsupervised, with params a burned rule already vets.
- **`auto` only for idempotent / low-blast-radius** actions (resend a receipt, regenerate a token,
  re-enqueue a mailer) — something a retry or a wrong-but-valid param can't make dangerous. The mid-loop
  path dedups real River retries by `(run_id, action_id, sha256(params))`, but design the write to be
  safely repeatable anyway.
- **Prefer `policy` over `auto`** whenever a data-observable precondition separates the safe cases from the
  ones that need a human (an amount cap, a state check, a whitelist). Keep the policy tight and readable —
  a reviewer approves it at the library gate.
- **Scope tenant writes by the trusted identity.** On a tenant-enabled project the policy (and the body)
  must read `RC_TENANT_ID` / `RC_TENANT_SLUG` and treat `params` as the untrusted in-tenant target only —
  never let params choose the tenant.
- **Write the draft factually for executed actions.** When an action auto-executes, the run already has the
  real result — an `ok:false` outcome means adapt the draft or escalate, not claim success.

## Failure Classes (infra vs domain)

A settled `failed` action carries an error class. It answers the first triage question — was this OUR
machinery or the action's own refusal — before anyone reads a trace:

| Class | Meaning | Who fixes it |
|---|---|---|
| `executor_predispatch` | Provably nothing ran (unbound tenant, missing grant, oversized argv, unresolvable script). Retry-safe: no side effect. | RootCause / wiring |
| `executor_error` | The executor failed and may have reached the interpreter — side effect uncertain. | RootCause |
| `no_executor`, `no_runner_url` | Hosted mode with no executor, or Embassy mode with no `action_runner_url`. | Project config |
| `attachment_fetch` | An attachment param could not be materialized before execution. | RootCause / params |
| anything else | Stamped by the action script itself (e.g. `ActionError`) — a domain refusal, i.e. the guard worked. | The brain / the action |

Where to read it:

```bash
rc fleet actions --days 7 --status failed          # CLASS column + Error: detail line (message clamped)
rc fleet actions --days 7 --status failed -o json |
  jq '.items[] | select(.error_class) | {action_id, error_class, error_message}'   # untruncated message
rc run actions <run-uuid>                          # per-run CLASS column
```

`rc run actions` is the customer-safe projection: its class is narrowed to the host vocabulary above and
every action-authored class collapses to `action_error`; the message is not exposed there at all. Read
the raw pair on `rc fleet actions` (operator-only). Repeated infra classes across runs are an outage
signal, not a brain bug — do not "fix" the brain for them.

## Inspect Access

Before depending on a connector or write plane, check the project from the same login/profile that will
run the action:

```bash
rc --project <project> project connection ls -o json
rc --project <project> dev console capabilities -o json
rc --project <project> dev console action list -o json
rc --project <project> project action-settings get -o json
```

`connection ls` shows connected OAuth/API grants. `capabilities` shows console planes such as
`planes.action`. `action list` shows the hosted/cataloged actions visible to this login. `action config
get` shows whether action execution is enabled and wired. For read-plane confidence, smoke-test the
read connector through `rc dev console bash run 'python -m lib.api get ...'` or the provider's `lib.connectors.*`
module.

## Hosted Python Harness

Hosted Python actions import the baked runtime harness instead of hand-rolling params, result files,
credential lookup, HTTP retries, and error envelopes. This minimal example shows the harness shape; add
an action-specific idempotency/reuse guard before using a write helper in production.

```python
#!/usr/bin/env python3
from lib import action
from lib.action import googledrive

p = action.params()
f = p.file("attachment")

uploaded = googledrive.upload_file(folder_id=p["folder_id"], file=f)

action.ok(
    f"Saved **{f.filename}** to Drive.",
    {"drive_file_id": uploaded.id, "drive_web_url": uploaded.web_url},
)
```

`action.params()` reads `$RC_ACTION_PARAMS` and installs crash capture. Match access to the manifest:
use `p["name"]` only for a required param; it raises `ActionError` when missing. For an optional param,
use `p.get("name")` (missing returns `None`) or `p.get("name", default)`. The executor legitimately
omits optional params, so do not index them with `p["name"]`.

`p.file("attachment")` returns a required `FileParam` with `path`, `filename`, `mime_type`,
`size_bytes`, `attachment_id`, `sha256` when provided, plus `open()` and `read_bytes()`.

Preflight scripts can reuse the matching small primitives: `preflight.params()` loads the host or
local JSON object, while `preflight.result()` builds the verdict envelope. Keep the project's checks
and failure wording in the script.

```python
from lib.action import preflight

params = preflight.params()
verdict = preflight.result(params.get("status") == "open", "Order checked")
```

`lib` is baked into the harness, so a tenant setting is read the same way here as in a grounding
script — the action container gets the profile as `RC_TENANT_PROFILE_JSON`
([recipe](tenant-settings.md)):

```python
from lib import tenant

min_hours = tenant.get("latecancel_min_hours", 48)
```

`action.ok(summary, data)` writes the success Result to `$RC_ACTION_RESULT`, prints it, and exits.
`action.fail(summary, data)` is a handled negative: the executor worked, but the reviewer should not
send the optimistic draft. `raise action.ActionError("message")` is a hard handled failure without a
Python backtrace. Any other uncaught exception is captured with a backtrace in the Result file.

Function style is also supported:

```python
from lib import action

@action.main
def run(p):
    return {"summary": "Done.", "id": p["id"]}
```

## Write Connections

Hosted OAuth/API writes go through `lib.action.client("<capability>")` or provider helpers under
`lib.action.*`. For brokered write connections, action code should not read env vars directly.

```yaml
connections:
  - airtable.write
  - googledrive.write
  - notion.write
```

`action.client("airtable.write")` resolves only `RC_ACTION_AIRTABLE`, never `RC_CONN_AIRTABLE`;
`action.client("notion.write")` resolves only `RC_ACTION_NOTION`, never `RC_CONN_NOTION`. Both return
a `lib.api.Client` with write verbs enabled. Missing credentials fail closed with guidance to declare
the capability and connect a write grant (`label=actions`). Read connectors under `lib.connectors.*`
remain read-only and `RC_CONN_*`-backed.

Available provider helpers are intentionally small and grown as actions need them. When adding or
changing a provider helper, update that provider module's literal `ACTION_HELPER_DOCS` constant in
`runtime/lib/action/<provider>.py`. The dashboard authoring overlay in the rootcause host repo loads
those constants from the pinned `rootcause-runtime` tag, parses them with Python AST without importing
the modules, derives helper signatures from real top-level functions, and renders
`dashboard-overlay/action-integrations*.generated.md`. That constant is model-facing downstream, so keep
it as the explicit write-plane whitelist inside `lib.action.*`, not under `lib.connectors.*`; connector
manifests and private underscored connector functions stay read-plane and are not action-authoring docs
unless deliberately promoted as public action helpers.

One-off write calls can use the generic client:

```python
from lib import action

c = action.client("linear.write")
c.post("issues", json={"title": "Customer follow-up"}, idempotency_key="issue-123")
```

Write requests are not retried blindly. Passing `idempotency_key=` sets the `Idempotency-Key` header
and opts that request into transient-status retry.

When a self-contained hosted action cannot use a catalogued client, use
`lib.http.action_request(...)`, never raw `requests`. It asserts action context, emits the same
per-attempt HTTP audit event, adds a fresh `X-Request-ID`, and performs no implicit retry. Always pass
an endpoint template for a dynamic path. Full contract and example: [Runtime HTTP audit](http-audit.md).

## Local Hosted-Python Checks

```bash
SKILL=<local-brain-work skill dir>
uv run "$SKILL/scripts/brain_action.py" --list
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --preflight-only
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --policy-only   # Layer-1 + preflight + the autonomy verdict
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>'
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --commit
```

The local runner uses Layer-1 → preflight → **policy gate** → write body. Production also rejects
completed negative checks, but sends unavailable checks to human review. A local pass does not
prove production data or permissions match. See [the gate contract](action-boundaries.md).

`--policy-only` runs `policy.py` read-only in the grounding env and prints whether this invocation would **auto-execute**
(allow) or **escalate to a human** (deny), with the exit code reflecting the verdict — the way to iterate on
an `autonomy: policy` rule before publishing. Default body execution is a local dry-run rollback. `--commit`
writes for real to whatever `.env.action` targets; use only safe local/staging targets unless explicitly
intending a real write. Inside scripts, `action.dry_run()` follows `--commit` > `--dry-run` >
`RC_ACTION_DRY_RUN=1`.

For `generated_file`, local Layer-1 accepts the hosted source object shape, but the local runner cannot
render it. Body tests need a materialized descriptor with a local `path`.

For tenant-enabled projects, use `action.require_tenant()` and scope every write by the trusted
`RC_TENANT_ID` / `RC_TENANT_SLUG` values, never by model-proposed params.

## Embassy Ruby Checks

Embassy actions (`runtime: ruby`, `script.rb`) execute inside the customer's app, so the local brain kit
cannot faithfully dry-run the write body. Keep the feedback loop split by fidelity:

```bash
# manifest + preflight, read-only, from the brain checkout
uv run "$SKILL/scripts/brain_action.py" <id> --params '<json>' --preflight-only

# Ruby body syntax, mirroring the Embassy executor's lambda wrapper
{ printf 'lambda do |params|\n'; cat actions/<id>/script.rb; printf '\nend\n'; } | ruby -c -

# deployed brain, prod data: Python preflight, then the Embassy would_execute dry-run
rc --tenant <slug> dev console action preflight <id> --params '<json>'

# real signed path, after pushing/syncing a safe target
rc dev console action run <id> --params '<json>'
```

For `runtime: ruby`, local body execution is not a substitute for the Embassy path: Rails constants,
tenant middleware, callbacks, PaperTrail, Sidekiq, and service credentials all live in the app. Use
preflight for data-observable checks (row exists, current state, shape/diff), Ruby syntax for cheap parse
errors, and the signed dev-trigger for final confirmation on a safe idempotent or staging target.

When a project exposes a safe Rails runner helper, add an intermediate check before the signed
dev-trigger: run the body through the app's installed Embassy executor with non-mutating params
(validation failure, unknown record, default-tenant refusal, or a no-change update). Transport the
`script.rb` body as Base64 into the runner; JSON or double-quoted Ruby strings can accidentally
interpolate `#{...}` inside the action source before the executor sees it. Current Ruby Embassies use the
`rootcause-embassy` entrypoint and `RootCause::Embassy::Executor`.

Review checklist for Embassy Ruby bodies:

- Resolve every model/class/job choice through an in-script allowlist or app-owned registry lookup; never
  `constantize` user input.
- Re-check all params in Ruby even when manifest and preflight already passed.
- For cross-tenant staff actions that must accept `tenant_id`, resolve `Tenant.find_by(id:)`, refuse the
  default tenant for settings/record writes, and wrap the body in `ActsAsTenant.with_tenant`.
- Put batch work behind an app job and return the job id; the Embassy request itself should stay bounded.
- Return a reviewer-readable `{ok, summary, ...}` with read-back proof and any rollback handle, not just
  "done".

## Ground First

Do not author an action blind:

1. Find relevant real runs with `rc run list`, `rc fleet runs`, or `rc fleet patterns`.
2. Inspect what the agent actually did with `rc run events <id>` or `rc-debug`.
3. Shape `description`, params, and preflight from evidence.
4. Verify with local checks.
5. Push a dev branch and run `rc ask --brain-ref dev/<branch>` to see whether the agent proposes the
   action with sane params.
6. Use `brain-publish` for live publish/promote/support handoff.
7. For an explicit production action, use `prod-console` / `rc dev console action preflight` /
   `rc dev console action run`
   after params are grounded.

Do not document private RootCause commands here. Use public `rc` surfaces; if a publish/support step is
missing, route through `brain-publish`.

## Triage

Use [`skills/local-brain-work/action-run-triage.md`](../skills/local-brain-work/action-run-triage.md)
when a run mentions an action, a preflight, or apparent mutation.
