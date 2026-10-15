# Actions: what runs where

The agent can read action code and choose an action plus arguments. It cannot send arbitrary
Python or shell commands to the write executor. The host selects approved code and controls
whether it runs automatically or waits for a human.

## Hosted Python: the request and feedback loop

Two views of the normal flow. The detailed gate behavior is below.

### 1. Check and refine — no writes

The agent can use preflight feedback to correct its arguments or understand the predicted effect.
A passing check is not permission to execute.

```mermaid
sequenceDiagram
    participant A as Agent
    participant P as Preflight in read-only workspace
    A->>P: Check candidate arguments
    P-->>A: Predicted result or useful correction
```

### 2. Request execution — the host decides

The agent requests an action, not arbitrary code. The host either permits automatic execution or
routes an eligible proposal to a human. Only the separate write container performs the change.

```mermaid
flowchart TD
    A["Agent"] -->|Action and arguments| H["Host approval gates"]
    H -->|Automatic execution permitted| W["Separate write container<br/>Approved Python script"]
    H -->|Human review required| R["Human reviewer"]
    R -->|Confirms| W
```

These diagrams omit refusals and technical failures. A rejected check blocks execution; an
unavailable check requires human review. See the exact behavior below.

## Execution contexts

| Context | Runs here | Access |
|---|---|---|
| Agent workspace | Grounding, optional `preflight.py` | Read-only data; brain mounted read-only; no action write credentials |
| Isolated policy check | `policy.py`, when effective autonomy is `policy` | Fresh read-only environment, outside the agent's control |
| Hosted action executor | Approved `script.py`, once per execution | Separate short-lived container with the configured action write credentials; no model loop |

These are execution contexts, not a promise of different Docker images. The same image can serve
read and write containers with different credentials. A working grounding database connection does
**not** grant action writes: configure the separate [action credentials](secrets.md#tenant-and-action-planes)
and applicable [write connections](actions.md#write-connections).

The action body is trusted code, not a second sandbox per business operation. It must validate inputs,
scope writes to the trusted tenant/principal, and enforce business limits itself. Seeing actions A, B
and C in the mounted brain does not let the model execute them in the write container: each request
passes through the host. But approval must also review any extra effects the chosen body performs.

## Preflight predicts; policy authorizes automation; the body enforces invariants

| Mechanism | Verdict | Current production effect |
|---|---|---|
| Manifest parameter schema | Valid/invalid arguments | Invalid inputs are rejected before dispatch. |
| Optional `preflight.py` | `ok`, `summary`, optional `reason`, `class`, `observed`, `resource_url` | A completed `ok:false` blocks execution. `class: "correctable"` (fixable input content/shape) lets the agent resend corrected inputs, 4 attempts per action per run; any other refusal is final for the run. A technical failure requires human review. |
| `policy.py` for effective `policy` autonomy | `allow`, `reason`, optional `observed` | `allow:false` or a technical failure prevents automatic execution and falls back to human review. It is not an absolute prohibition. |
| `script.py` | Execution result | Recheck hard business limits and current state before writing, including after a human confirms. |

There is no preflight `autonomously_runnable` field. `lib.action.preflight` only loads params and
builds a verdict dictionary; it neither schedules an action nor grants write access. The production
agent requests execution through the host's `action` tool.

The host runs the installed preflight during an action-tool request, so a prior manual preflight
is not accepted as a reusable permission slip. A completed rejection returns feedback to the agent
before automatic execution or proposal creation (human-gated chat may show a disabled card).
Declare `failure_class="correctable"` only when `reason` tells the agent exactly how to fix the
inputs (a missing required section, a wrong format); never for rights, identity, tenant or record-state
refusals, so the agent cannot loop around a check.
A crash, timeout, unparseable result or missing preview prevents automation and instead creates a
human-review proposal/card marked **preview unavailable**. A pass still needs autonomy/policy approval.
No preflight means schema validation alone, not proof that the business operation will succeed.

Human confirmation refuses a stored rejection; it does **not** rerun `preflight.py`. Data can change
between preview and execution. Check invariants again in the body, using a transaction or conditional
write where needed. The executor resolves the current approved action; an approved version changing
since proposal is surfaced/audited, not an unconditional refusal of execution.

For example, “discounts above €50 need review” belongs in `policy.py`. “Discounts above €50 are never
allowed” must be enforced in the body too; mirror it in preflight for early feedback. A policy denial
alone still permits human approval.


Make preflight feedback useful: identify the disqualifying state, suggest a grounded next step,
and show calculated effects in `observed` (for example €150 − 10% = €135). Keep `summary` short
for the confirmation UI. A prediction is never evidence that the write happened.

In Embassy mode, execution happens inside the customer's application instead, letting the action
reuse application methods, callbacks and jobs. The Python preflight still runs in the read-only
workspace; an Embassy validation dry-run is a separate check. See [action authoring](actions.md).
