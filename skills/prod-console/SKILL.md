---
name: prod-console
description: "Run a guarded production primitive directly from a brain checkout via rp dev console: SQL/schema read, rare dry-run-first SQL write, workspace bash, cataloged script, action preflight/run, exact mirror refresh. Use when a query, script, or action must be checked against production without wrapping it in an LLM run."
---

# prod-console — direct production primitives

Verbs and flags live in one home: [docs/rp-cli.md](../../docs/rp-cli.md) and `rp dev console <cmd> --help`.
This file is the decision logic around them.

Public `rp` only — never private ReplyPen repos, host shells, SSM, or registry SQL
([docs/support-boundary.md](../../docs/support-boundary.md)). Scope comes from `.replypen.toml`, the
active OAuth login, and optional `--project` / `--tenant`.

**Console vs `rp ask`:** console primitives answer "does this query/script work?" directly and fast.
`rp ask` wraps them in an LLM run — keep it for full-loop behavior validation, ambiguous
investigations, and customer-style simulations.

## Required Context

- [docs/side-effects.md](../../docs/side-effects.md) — what is read-only and what is not.
- [docs/brain-model.md](../../docs/brain-model.md), [docs/mirrors.md](../../docs/mirrors.md).

## Workflow

1. **Discover before guessing.** `rp dev console capabilities` is the manifest: databases, cataloged
   scripts, actions, and which console planes this login actually has. `rp project connection ls`
   adds OAuth/API grants.
2. **Freshness.** A missing pushed script or stale `/brain` → `rp dev brain status` / `rp dev brain
   sync` (sync expires warm bash workspaces, so the next run remounts). A mirror commit pushed to its
   tracked branch (`rp project repo ls` → `default_branch`) that must be visible now → `rp dev
   mirror refresh --repo <name> --expect-sha $(git rev-parse HEAD)`, once per affected project;
   success proves that exact commit is mounted (`--accept-descendant`: or a newer tip containing it,
   when siblings pushed meanwhile). Never restart Docker.
3. **Database.** `rp dev console database list` is the authoritative view of which DSNs exist and
   connect; `rp project database ls` only shows the annotated ones. A database missing from `list`
   is not registered — seal `<PROJECT>_<DBKEY>_DSN`, then annotate
   ([docs/secrets.md](../../docs/secrets.md#register-a-new-grounding-database)).
   Query failing on a column name → stop and read `schema --table`, do not keep guessing.
   Bind values with repeated `--param`, never string interpolation. Analysis over more than a
   preview belongs in a local script over a complete `--all` export —
   [`rp-script-wrapper`](../rp-script-wrapper/SKILL.md). An inline result can be truncated or
   spilled without failing: read [complete output](../../docs/rp-cli.md#complete-output) before
   treating a result as "what exists". Reads run under a statement timeout: a full scan of a huge
   table dies instead of returning, so stay on an indexed prefix or sample a few ids (no
   `count(*)` previews over the biggest prod tables).
4. **Workspace files.** `rp dev console bash run` is the exec plane; prefer a cataloged script
   (`bash list`) over raw bash. Logs are reached through the same plane
   (`python -m lib.cloudwatch …`), not a separate verb. Ad-hoc Python goes in a quoted heredoc,
   `bash run "python - <<'PY' … PY"`; `from lib import …` works from any cwd (verified 2026-10-27,
   older notes claiming stdin is dropped are stale). A command gets ~120 s by default
   (`--timeout N`): start slow remote jobs (Logs Insights queries) together, then poll, and chunk long
   ranges. Large stdout spills to `.replypen/output/…/response.json` (`"spilled": true`): check
   that before concluding a run printed nothing. A diagnostic that recurs becomes a cataloged script
   ([`brain-helper-devloop`](../brain-helper-devloop/SKILL.md)). `/kb` holds synced knowledge-base articles,
   `/brain/knowledge` the brain's own — filters in
   [docs/knowledge-base.md](../../docs/knowledge-base.md).
   Bash stdout is capped at ~64 KiB; a truncated result ends with the exact
   `rp dev console file get /tmp/rc-console/stdout-<seq>.txt …` that downloads the full stdout
   (`bash run --out FILE` does it automatically). To copy files or whole directories to the laptop,
   never base64-chunk through bash: `rp dev console file get <sandbox-path> [local-path]` (any size,
   byte-exact, directories unpacked locally, any readable path incl. `/kb`, `/brain`, `/mirrors`).
5. **Actions.** `list` / `show` / `preflight` are read-only; `run` is a real state-changing
   execution on the project's own production. `preflight` runs the brain's Python
   `actions/<id>/preflight.py` in the console workspace first and returns its
   `{ok, summary, observed, resource_url}`; on Embassy projects a passing preflight is then followed
   by the Ruby `would_execute` dry-run (a refusal short-circuits). So the old
   `bash run 'python actions/<id>/preflight.py …'` workaround is no longer needed for the verdict —
   only for debugging the script's own stdout/stderr. Run it only when the user asked for execution or the
   task plainly requires it and params are grounded; report the action-run id, status, and result.
   For history across runs (stored params, originating run links) use `rp fleet actions` —
   [`rp-fleet`](../rp-fleet/SKILL.md).

## Side effects — the two that are not read-only

**SQL writes.** `--write` uses the project's sealed write-plane DSN (`<X>_WRITE_DSN` in
`.env.action`; its Postgres grants are the ceiling) and COMMITs (scope `console:db:write`, which an
empty-scope admin login inherits; project-level only — a read-only `console:db` token or a
tenant-bound request is refused). Only some databases have a write DSN: `capabilities` /
`database list` report `writable`; check rather than trust a list. Elsewhere `--write` fails
`NO_WRITE_PLANE` and there is no `rp` workaround: get the statement reviewed, rehearse it on staging,
and hand it to whoever holds a direct path. Raw SQL bypasses the app (validations, callbacks,
soft-delete): anything business-shaped or recurring belongs in an action. Discipline, because
there is no undo:

1. read the row first and know the revert statement; one statement per call, `where` on a verified id;
2. rehearse with `--write --dry-run` and a `RETURNING` list covering the key and every changed
   column (`RETURNING *` for deletes, so the removed row can be archived). Dry-run fires real
   constraints and triggers on the same DSN, then rolls back; it does not undo sequence bumps or
   anything outside the database;
3. stop on any unexpected affected-row count;
4. rerun the identical statement without `--dry-run` — the two runs are separate executions, so
   confirm the count still matches.

Every write is audited server-side as `db.write` with the raw SQL; rehearsals carry `dry_run:true`.

**`action run`.** See step 5 and [docs/actions.md](../../docs/actions.md).

## One identity, or all of them

`--principal-kind` / `--principal-id` on `bash run`, `database query` and `database schema` bind a
console primitive to one asserted end-user, so it sees exactly the projection that person's hosted
run gets — the right tool for "reproduce this one requester's failure". When the question is broader
("which audiences does this helper work for", "what does each audience's projection contain"), use
[`scope-check`](../scope-check/SKILL.md), which sweeps a set of audiences into a matrix instead.

## Preflight is not a scope gap

`preflight` honours `--tenant` and runs in the same tenant-scoped grounding workspace a run gets
(scoped `*_DSN`, `RC_TENANT_*` stamped, no action/write credential). So a "record N not found" from
a console preflight means the grounding data no longer holds N — not a scope or env gap — even if a
run saw N earlier. Confirm with `rp dev console bash run --tenant <slug>` + `lib.db` before
suspecting the console.

## Brain-script catalog convention

Host contract (`replypen` parses it): comment metadata in the **first 40 lines** of a
`skills/*/scripts/*.py|.sh` file makes the script discoverable through `rp dev console bash list` and
`capabilities`.

```python
# name: invoice_lookup
# purpose: Find invoice/payment state for a customer-visible invoice id.
# args: --invoice-id <id>
# required_env: APP_DSN, STRIPE_API_KEY
```

`description` is accepted as an alias for `purpose`. Keep names stable and env names only — the
script body stays the source of truth for behavior.

## Close-out

Report what ran, the scoped project/tenant, the material result, and the next command worth running.
Always distinguish a rolled-back dry-run from a committed write, and preflight-only from executed.
