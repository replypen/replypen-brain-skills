---
name: local-brain-work
description: "Run and check a rootcause BRAIN locally from its checkout: grounding scripts, offline/live/docker test tiers, tenant projection preview, hosted-Python action dry-runs, registering a new grounding database. Use inside a rootcause-brain checkout before production validation, or to route a broad did-it-work prompt to the right rc skill."
---

# Local Brain Work (`local-brain-work`)

A brain is markdown knowledge plus Python grounding/action scripts that production mounts read-only at
`/brain`. This skill runs the local engine in `scripts/` against the current brain checkout, and routes
broad prompts to the focused skills. Production runs go through public `rc`; never RootCause-private
repos, SSM, or host database shells.

## Required Context

- [docs/brain-model.md](../../docs/brain-model.md) — layout, `include_in`, mounts, project/tenant model.
- [docs/side-effects.md](../../docs/side-effects.md) — what is read-only and what is not.
- [docs/mirrors.md](../../docs/mirrors.md) — when a script uses `lib.fs`, `/mirrors`, or KB mounts.

Hide maintainer-only committed content with root `.replypenignore` (brain model owns the rules).
`exclude_in` frontmatter has no run-visibility effect. **Never cite an ignored path from run-visible
content** — the run agent cannot open it and will hallucinate around the gap.

## Route Broad Prompts

| User intent | Use |
|---|---|
| Run a grounding script, local/live/docker tests, projection preview, mirror-dependent check, or hosted-Python action dry-run | Local Brain Work (`local-brain-work`) |
| Try a mirror change on prod data | [`mirror-try`](../mirror-try/SKILL.md) |
| "The tables are missing", register a database, add a new DSN to rootcause | [Register A New Grounding Database](#register-a-new-grounding-database) below |
| "Does this change work on prod infra?" or "simulate this customer email" | `brain-ask` |
| Debug one run/thread/session, read full trace, or explain why a draft/action happened | `rc-debug` |
| Bad chat answer, a share/run link, replay a chat turn after an edit | [`brain-chat-debug`](../brain-chat-debug/SKILL.md) |
| "Is anything broken?" stale mirrors or dead letters | `rc-health` |
| "What keeps failing?" recent fleet/pattern review | `rc-fleet` |
| "Why does this work for me but not for a parent/admin?", per-principal visibility or helper coverage | [`scope-check`](../scope-check/SKILL.md) |
| Update local skills kit or `rc` CLI | `brain-dev-upgrade` |
| Reconcile local work and `origin/main`, including cross-computer divergence | `brain-git-sync` |
| Make reconciled brain edits live, server-sync, promote, publish, or prepare support handoff | `brain-publish` |

## The Engine

Set `SKILL` to the directory holding this `SKILL.md`; every command below is
`uv run "$SKILL/scripts/<script>.py"`, run from the brain checkout root. All scripts take `--help`.

| Script | For |
|---|---|
| `brain_run.py` | one grounding script, or `-m lib.db` for ad-hoc read-only SQL; `--brief` maps the brain |
| `brain_test.py` | test tiers: import smoke + offline pytest, `--live`, `--tenant` |
| `brain_smoke.py` | import smoke alone (what publish verification runs) |
| `brain_lint.py` | dependency-light action + frontmatter lint (tags the host never reads; `action-manifest-enums` rejects unknown autonomy/risk/confirm/min_tier/runtime values); whole tree by default (`--all`) or only the paths you pass; `--as tenant\|mirror` lints an overlay or mirror repo (auto-detected); non-zero on FAIL |
| `brain_structure.py` | links, frontmatter, routing, privacy lint, hygiene |
| `brain_hygiene.py` | pre-push gate `brain_git_sync.py` always runs: conflict markers, projection `{{ }}`, `/Users/`, dead links, customer-copy em dashes, mermaid, `chat_inspiration.md` gallery (`chat-inspiration`), leaked tool-call tags (`tool-markup`); FAIL when a change grows an on-demand doc's whole file past the 6000-byte bash preview cliff (`doc-spill`); NOTICE for a cited `/brain/…` this checkout will not ship (`paths`) |
| `brain_paths.py` | do the absolute container paths docs/scripts cite (`/brain`, `/tenant`, `/mirrors/<repo>`, `/kb`) resolve in production? Local checkouts first (new files pass), the rest in one `rc dev console paths` call per tenant; `--all`, `--tenants a,b\|all`, `--local`. Run it after moving/renaming files, citing a KB article or mirror path, or before publishing |
| `brain_action.py` | hosted-Python action: Layer-1 + preflight + policy + write body |
| `brain_projection.py` | tenant projection preview — see [projection.md](projection.md) |
| `brain_dump.py` | explode one prod run id into `.rootcause/dump/` (markdown index + JSONL) |

**Fixed a brain defect these checks missed?** Propose a new rule only when it generalizes beyond this
brain, flags the demonstrated before-state, and stays quiet on current content of other brains (run it
against them; quotes, negations and examples are the usual false positives). One brain's wording rule
is a brain-local pytest instead. Edit rule source only in a checkout of the canonical
`rootcause-org/rootcause-brain-skills` you can push to (WRITE, MAINTAIN or ADMIN from
`gh repo view rootcause-org/rootcause-brain-skills --json viewerPermission`); the installed kit and the
pinned `rootcause-runtime` are copies. Without that access, the before/after fixture and counterfactual
go into a support request.

`brain_env.py` is shared plumbing (version line, image tag, mirror discovery), not an entry point. The
scripts resolve `lib` from a sibling `runtime/` checkout when present, else from the tag-pinned
`rootcause-runtime` git spec — so unreleased local `lib` edits are visible immediately.

Start any session with `brain_run.py --brief` plus the brain's own `AGENTS.md`.

## Fidelity Ladder

Each rung costs more and proves more; stop at the cheapest one that covers the change, and always
report which rung you reached.

1. **uv mode** (default) — fast inner loop. Host Python, host filesystem, host network.
2. **`--mode docker`** — the published workspace image, `/brain:ro`, `/mirrors:ro`, container
   user/rootfs/env isolation. Catches image/dependency/read-only-mount breakage. Does **not** prove the
   production egress allowlist.
3. **`rc ask --brain-ref dev/<branch>`** (`brain-ask`) — the real loop: LLM turns, warm start, grounding
   pre-step, tenant scoping, egress, callback, journal. The only rung that is a production statement.

On a project that scopes runs to an asserted end-user, rungs 1–2 all run with YOUR wide view — a
helper can pass every one of them and still die for a parent or a regular admin. The per-identity
rung is [`scope-check`](../scope-check/SKILL.md): a real sandbox bound to a real principal, with
exactly the projection that person's hosted run gets.

When a brain rule changes the recipient or sets a subject — web-form relays, voicemail/PBX
notifications, and partner forwards are typical — production validation also checks the result's
`outbound_email.to`/`cc`/`bcc` and `subject` against that rule. Do not turn this into a blanket check:
ordinary same-thread replies correctly omit `outbound_email`, with nothing to report.

A green `uv` run is not a guaranteed-green production run; say so when that is all you ran. Use
`brain_dump.py <run_id>` to explode a run locally (markdown index first, then `jq` the JSONL);
`rc-debug` for analysis-first debugging.

Every `rc` command here is run by the **local development agent**. The production model has no `rc`
binary, so `rc ...` must never appear in committed brain content — the run would be instructing itself
to use a tool it does not have.

Import smoke discovers only `skills/**` — lint therefore rejects grounding Python elsewhere so nothing
escapes coverage (action, test, and root `conftest.py` code are exempt). A single script opts out with
`# rc: no-import-smoke` in its header.

## Hosted Python Actions

For “how do actions work?” start with [the execution boundaries](../../docs/action-boundaries.md):
read workspace, preflight feedback, separate policy gate, human confirmation, and write executor.

`brain_action.py` is the local state-changing exception: it reproduces hosted-Python action validation,
preflight, policy gate, and body execution against whatever `./.env.action` points at. Dry-run with
rollback is the default; **`--commit` writes for real** — use a local/staging target unless a real write
was explicitly asked for.

After every `actions/*/script.py`, `preflight.py`, or `policy.py` edit run `brain_lint.py` before the
slower tiers. The offline `brain_test.py` tier repeats the same lint and prints a hygiene block: shipped
size budget (FAIL ≥ 96 KiB — the action transport's argv limit), helpers copy-pasted across ≥ 3
maintained sources, dead `_private` names. Conventions behind it: "Script Hygiene" in
[docs/actions.md](../../docs/actions.md). Reading production action evidence:
[action-run-triage.md](action-run-triage.md).

### Embassy Ruby actions

For `runtime: ruby` actions there is **no faithful local write body** — it depends on the customer's
Rails app, callbacks, tenant context, jobs, and Embassy signing. Locally you get Layer-1 + read-only
preflight (`brain_action.py --preflight-only`) and a syntax check of the body wrapped in a lambda
(`ruby -c`). Final confidence comes from `rc dev console action run` against a safe/staging/idempotent
target after the brain ref is synced.

## Env

`rc project env pull` writes the production grounding `.env` (0600, names only, never values) using the
logged-in OAuth token. Needed before any `--live` local check.

Production DSNs are usually IP/region allowlisted to RootCause infra, so a laptop connection is often
impossible by design — `lib.db` gives up after 15s with that guidance. Treat it as an infra boundary and
verify with `rc dev console database` / `rc dev console bash` instead of forcing local live tests.

New read-only credential: document the env var **name** only, set it with
`printf %s "$SECRET_VALUE" | rc project env set key=NAME`, then `rc project env pull`. `--plane action`
is for hosted action credentials only, never grounding. Details: [docs/secrets.md](../../docs/secrets.md).

## Register A New Grounding Database

Symptom: a script needs tables no registered database has, or `rc dev console database list` does not
show it at all.

There is no `rc project database add`. **Sealing the DSN key creates the database**; `rc project
database set … description=…` only makes it *visible* in `rc project database ls` (that list shows
annotated DSNs, not sealed env keys) — which is why a working database can look absent. Name it
`<PROJECT>_<DBKEY>_DSN`; `<DBKEY>` lowercased is the `lib.db` short name (`ACME_BILLING_DSN` →
`db="billing"`). Hard requirements: a read-only role, and a DB host that allows the RootCause box.
`*_WRITE_DSN` is the action plane, not a grounding database.

```bash
printf %s "$DSN" | rc project env set key=ACME_BILLING_DSN            # creates it
rc dev console database list                                          # authoritative "does it connect"
rc project database set ACME_BILLING_DSN description="Invoices, subscriptions, payment state."
rc project env pull                                                   # for local live checks
```

Then document it in the brain's `skills/databases/` map and ship. Prerequisites and full recipe:
[docs/secrets.md](../../docs/secrets.md#register-a-new-grounding-database).

## Finish

Smallest useful checks → optionally `brain-ask` against a pushed `dev/*` ref → `brain-publish`
(`brain_publish.py`: commit/reconcile/push `main`, preflight, publish, proof) to make it live. Pushing `main` is **not** the same as
making a channel-backed shared brain live.
