# Action/run triage after `rp ask`

Use when a prod run looks like it booked, moved, deleted, or otherwise changed customer state. Goal:
decide whether the run only drafted text, *proposed* an action, or actually *executed* one.

## Proposed ≠ not executed

Every eligible action goes through the `action` tool, but the autonomy path decides what that means.
Human autonomy records a proposal for a reviewer. **`auto` / `policy` autonomy may execute mid-loop** —
a real mutation during the run, no reviewer confirm. Always read the autonomy path before concluding
nothing happened: an `action` tool event plus an `action_run` whose `approved_by` is `autonomy:auto` or
`policy:<digest>` means the write already landed.

## Read the evidence

```bash
rp run events <run_id>        # look around preflight / policy / action / reply
rp run brain-diff <run_id>
```

| What you see | What it means | Report it as |
|---|---|---|
| Draft claims a mutation, no proposed action | No mutation path existed. Brain/playbook drafted unsafely. | "Draft unsafe; no action was proposed/executed." |
| Preflight `ok:false`, crashed, or unparseable | Proposal blocked. No `action_run`, no mutation. | "Preflight blocked proposal; draft must not claim success." |
| `action` event + proposed action row, or result `Proposed` | Human autonomy, denied policy gate, or pre-dispatch failure. No mutation yet. | "Action proposed; pending reviewer confirm." |
| `action` event + `action_run` `approved_by = autonomy:auto` / `policy:<digest>`, `succeeded` | Auto-executed mid-loop; real mutation, no confirm. | "Action auto-executed in-run; result says …" |
| Action `succeeded` / result note after confirm | Post-loop execution. | "Action executed; result says …" |
| Action `failed` / error result | Attempted and failed (post-confirm or mid-loop). | "Action execution failed; hold/adapt draft." |
| `rp dev console action run` output | Dev-trigger executed for real, usually runless. | "Dev-trigger executed; not just a run proposal." |

## Optimistic drafts

An email run may draft optimistically ("I moved it") **only** when the proposal survived validation and
preflight, leaving a confirmable action for the reviewer before sending. If preflight blocked it, the
draft is unsafe unless it says manual action is needed. An auto-executed action is not optimistic at all
— the write ran, so the draft is factual; and an `ok:false` mid-loop result should have made the run
adapt or escalate, never claim success.

## Which layer failed

| Layer | Runs | Plane | Reproduce locally |
|---|---|---|---|
| `preflight.py` | during the LLM run | grounding, read-only | `brain_action.py <id> --params … --preflight-only` |
| `policy.py` (`autonomy: policy`) | host-side one-shot per invocation, fail-closed to `deny` | grounding, read-only | `brain_action.py <id> --params … --policy-only` |
| `script.py` / `script.rb` | only after confirm or dev-trigger | action plane, write credentials | `brain_action.py` dry-run (Python only) |

Ruby/Embassy bodies have no faithful local dry run — use `rp dev console action preflight` / `run`
against a safe target.

Usual root causes: **not proposed** → brain content (action description, playbook altitude, draft logic
after a failed preflight). **Preflight failed** → read-plane data/schema/permission/param grounding.
**Execution failed** → write-body code, action credentials, tenant scope, or the customer app.

Concepts and authoring: [docs/actions.md](../../docs/actions.md). Publish/support handoff:
[`brain-publish`](../brain-publish/SKILL.md).
