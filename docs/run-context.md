# What kind of run is this? (`lib.runctx`)

The host stamps one document per container as env `RC_RUN_CONTEXT_JSON` (run, action, preflight,
policy), plus a `/brain/run_context.json` copy in a run's view. The env document wins whenever present:
an action container mounts the raw project clone, where a `run_context.json` would be committed brain
content. The file is only a local-fixture fallback; never commit one to a brain.

Before it, a script that had to behave differently for a parent than for an operator was reading the
prompt's `Request source:` prose, or ids out of the message body. Both are model-influenced.

```python
from lib import runctx

if runctx.is_principal_scoped():      # a parent / leader run: one person's own rows
    ...
if runctx.surface() == "email":       # the answer lands in a mailbox, not a chat panel
    ...
if runctx.is_simulation():            # dress rehearsal: credentials stripped, claim no change
    ...
runctx.get("tenant.scope_value")      # dotted keys walk nested objects
runctx.context()                      # the whole document
```

```bash
python -m lib.runctx                  # whole document (source on stderr)
python -m lib.runctx get surface
python -m lib.runctx get principal.kind
```

## Rules

- **Branch on this document — never on the prompt's prose, and never on ids from the message body.**
- **Descriptive, never authorization.** It grants nothing. Scope is enforced by the database engine
  (the scope manifest / table grants), the sealed env and the egress gateway. A script that decides
  "may I" from `principal` or `plane` is reading a hint as a boundary. A script that must behave
  differently for parents/leaders checks `is_principal_scoped()`; it still writes the same scoped query.
- **Absent is explicit `null`, never a missing key** — `context()["channel"]` never raises, and `get`
  treats null as absent (so `get("channel", "google")` returns the fallback).
- **Malformed JSON raises `RunContextError`.** That is an injection bug, not a missing fact.
- **`schema_version` is 1.** A new nullable field is additive; the version bumps only for a removal or
  a re-type.
- No document at all (a host too old to stamp it) ⇒ `context()` is `{}` and every accessor degrades —
  except `is_principal_scoped()`, which falls back to the legacy `RC_PRINCIPAL_SCOPED` env.

## Fields, with real values

`plane` is `"run"` (the run's own workspace) or `"action"` (a hosted action / preflight / policy
container: identity only, every ingress field `null` — that *is* the "I am not the run" signal).

| Field | Example | Notes |
|---|---|---|
| `schema_version` | `1` | |
| `plane` | `"run"` \| `"action"` | |
| `run_id` | `"1f0c…"` | on a resumed hot chat container this is the session's FIRST turn — see the caveat |
| `project.id` / `project.name` | `"kampadmin"` | |
| `tenant` | `{"id": "…", "slug": "solhi", "scope_value": "org-42"}` | `null` on a flat project; `scope_value` `null` for a tenant without a data-scope key |
| `kind` | `"chat"` | `email\|chat\|prompt\|mcp\|analysis\|console\|compose` |
| `mode` | `"chat"` | `email\|compose\|chat\|prompt\|analysis` |
| `scenario` | `"chat"` | `email\|raw\|chat\|dashboard\|compose` |
| `surface` | `"chat"` | the same string the prompt prints as `Request source:` — `chat\|dashboard_chat\|email\|intercom\|whatsapp\|compose\|prompt_api\|mcp\|embassy\|console` |
| `channel` | `"google"` | mailbox provider; `null` off the channel plane. Transport telemetry — branch on `surface` |
| `origin` | `"email"` | `email\|beacon\|whatsapp\|conversation` |
| `simulation` | `false` | `true` ⇒ side-effecting credentials were stripped; `null` on the action plane |
| `principal` | `{"kind": "kampadmin_person", "external_id": "p-88", "asserted_by": "embassy", "assurance": "session"}` | presence = principal-scoped; `asserted_by`/`assurance` are `null` on the action plane (a stored proposal keeps only the identity core). Claims are NOT here — they are data-plane facts in the scope manifest and `RC_PRINCIPAL_CLAIM_*` |
| `session_id` / `thread_id` | `"…"` | chat continuity handle / local thread row |
| `brain_ref` | `{"requested": null, "resolved": "channel:stable @ abc123", "sha": "abc123def…"}` | `requested` is set on a `--ref dev/<branch>` run |
| `action` | `{"id": "cancel_registration", "action_run_id": "…"}` | action plane only |

Four runs, the fields that differ:

- a parent's embedded-chat turn on kampadmin — `plane=run kind=chat mode=chat surface=chat
  principal.kind=kampadmin_person tenant.slug=solhi simulation=false`
- `rp ask --simulation` — `plane=run kind=prompt mode=prompt surface=prompt_api simulation=true
  principal=null`
- a Gmail email run — `plane=run kind=email mode=email surface=email channel=google origin=email`
- a hosted action preflight — `plane=action surface=null simulation=null
  action.id=cancel_registration`, tenant + principal core still filled

## Caveat — a resumed hot chat container

A parked chat container is adopted whole, so its file and env are the ones minted for the session's
first turn. `session_id` is stable; `run_id` names that first turn. Correlate per turn on `session_id`.

## Testing locally

Point at a fixture: `RC_RUN_CONTEXT_PATH=/tmp/run_context.json python your_script.py`. Or fetch a real
one — a run's document is at the root of the `/brain` view, so `rp dev console bash run --cmd 'cat
/brain/run_context.json'` shows exactly what production stamped.

Sibling document, same mechanism, different question ("what are this tenant's values?"):
[tenant-settings.md](tenant-settings.md).
