# Support Boundary

Use this kit to change the brain when the evidence points to project knowledge, grounding, tests, or
actions. Escalate to ReplyPen support when the missing capability lives outside the public `rp`/API
surface or in managed infrastructure.

| Symptom | Likely owner |
|---|---|
| Wrong query, missing playbook, bad prompt knowledge, missing action description | Brain change. |
| `.replypen.toml` missing or wrong | Brain repo setup; ask ReplyPen support if you cannot edit it. |
| OAuth/login/scope failure | Project auth setup or ReplyPen support. Include `rp auth status` output. |
| Private DB unreachable from laptop | Expected infra boundary. Local `lib.db` fails after a 15s connect timeout; verify with `rp dev console database` / `rp dev console bash`, not local live tests. |
| Stale or failed mirror | ReplyPen mirror pipeline/support. Brain changes do not refresh mirrors. |
| No inbound email run at all | Upstream channel/ReplyPen routing or ReplyPen support. |
| Callback rejected or dead-lettered | ReplyPen/channel integration. Use `rp run thread`/`rp fleet health` evidence. |
| Action plane 404, disabled, or not wired | ReplyPen support plus the customer app owner for app-side receivers. |
| Pushed brain commit not mounted | Run `rp dev brain status`; then `rp dev brain sync` if behind/stale. |
| `rp dev brain sync` reports manual reconcile | ReplyPen support request with `rp dev brain status -o json`. |
| Shared project channel points at an old SHA | With a project-maintainer login, run `brain_publish.py --project <p>` from the brain checkout (checks, git sync, preflight, exact-SHA publish, proof). See [`brain-publish`](../skills/brain-publish/SKILL.md). |
| `rp dev brain status` looks fine but the channel is stale | Missing `--scope project`: in a tenant context status answers about the tenant overlay brain. Re-check with `--scope project` and read `.status.channels[]`. |
| `rp dev brain` command 404s | Old client resolving the project onto retired flat routes. Pass `--project <p>` explicitly and upgrade to rp >= 1.16.5. |
| Promotion would break some tenants' projections | Not support: run `rp dev brain preflight --project <p> --scope project --sha <sha>` before promoting `stable`, and promote `edge` → observe → `stable`. |
| Promotion denied for a tenant-scoped login | Expected: one tenant cannot move the shared channel for all tenants. Use an authorized project-maintainer login or request that access. |
| Bug run but no tracker ticket | Usually the gate, not support: `draft` autonomy, test/simulation run, OAuth instead of token connection, or a non-bug dossier. Recipe: [escalation.md](escalation.md). |
| Tenant brain publish / action wiring not exposed through public `rp` | ReplyPen support request; product gap to close. Tenant brains use `main` and have no channels. |

## Support Request Template

```text
Project/brain:
Tenant, if any:
Brain repo path:
Branch/ref:
Commit SHA:
Requested outcome:
Verification already run:
Run ids / trace URLs:
Why this is not a brain-only change:
```

Keep support requests in product terms: "sync origin", "grant project-maintainer promotion access",
or "wire/diagnose action execution", not private host commands. Operator promotion is break-glass and
outside this external-maintainer kit.
