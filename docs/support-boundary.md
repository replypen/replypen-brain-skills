# Support Boundary

Use this kit to change the brain when the evidence points to project knowledge, grounding, tests, or
actions. Escalate to RootCause support when the missing capability lives outside the public `rc`/API
surface or in managed infrastructure.

| Symptom | Likely owner |
|---|---|
| Wrong query, missing playbook, bad prompt knowledge, missing action description | Brain change. |
| `.rootcause.toml` missing or wrong | Brain repo setup; ask RootCause support if you cannot edit it. |
| OAuth/login/scope failure | Project auth setup or RootCause support. Include `rc auth status` output. |
| Private DB unreachable from laptop | Expected infra boundary. Local `lib.db` fails after a 15s connect timeout; verify with `rc dev console database` / `rc dev console bash`, not local live tests. |
| Stale or failed mirror | RootCause mirror pipeline/support. Brain changes do not refresh mirrors. |
| No inbound email run at all | Upstream channel/ReplyPen routing or RootCause support. |
| Callback rejected or dead-lettered | RootCause/channel integration. Use `rc run thread`/`rc fleet health` evidence. |
| Action plane 404, disabled, or not wired | RootCause support plus the customer app owner for app-side receivers. |
| Pushed brain commit not mounted | Run `rc dev brain status`; then `rc dev brain sync` if behind/stale. |
| `rc dev brain sync` reports manual reconcile | RootCause support request with `rc dev brain status -o json`. |
| Shared project channel points at an old SHA | With a project-maintainer login, run `brain_publish.py --project <p>` from the brain checkout (checks, git sync, preflight, exact-SHA publish, proof). See [`brain-publish`](../skills/brain-publish/SKILL.md). |
| `rc dev brain status` looks fine but the channel is stale | Missing `--scope project`: in a tenant context status answers about the tenant overlay brain. Re-check with `--scope project` and read `.status.channels[]`. |
| `rc dev brain` command 404s | Old client resolving the project onto retired flat routes. Pass `--project <p>` explicitly and upgrade to rc >= 1.16.5. |
| Promotion would break some tenants' projections | Not support: run `rc dev brain preflight --project <p> --scope project --sha <sha>` before promoting `stable`, and promote `edge` → observe → `stable`. |
| Promotion denied for a tenant-scoped login | Expected: one tenant cannot move the shared channel for all tenants. Use an authorized project-maintainer login or request that access. |
| Bug run but no tracker ticket | Usually the gate, not support: `draft` autonomy, test/simulation run, OAuth instead of token connection, or a non-bug dossier. Recipe: [escalation.md](escalation.md). |
| Tenant brain publish / action wiring not exposed through public `rc` | RootCause support request; product gap to close. Tenant brains use `main` and have no channels. |

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
