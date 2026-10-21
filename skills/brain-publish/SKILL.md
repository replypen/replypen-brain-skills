---
name: brain-publish
description: "Publish, ship, deploy, or promote replypen brain changes after safely reconciling Git with origin/main. Use for `$brain dev: publish`, `$brain-publish`, making brain edits live, exact-SHA server sync, stable/edge promotion, tenant/project publish, actions, or a ReplyPen support request. Do not use for a pure `$brain dev: git sync` request; use brain-git-sync."
---

# brain-publish — make a brain change live

The shared final step after brain edits from `local-brain-work`, `brain-ask`, `rp-debug`, `rp-health`,
`rp-fleet`, or manual authoring. A project-maintainer OAuth login is enough; no private ReplyPen
surface is involved.

**The one invariant: publish an exact SHA that was verified at `origin/main`.** Never a pre-sync `HEAD`,
an ambient branch, an unverified push, or an implicit remote tip. Everything below exists to protect
that, or to prove afterwards that the intended ref really moved.

Assumes a current `rp` (`brain-dev-upgrade` before debugging odd routing or missing subcommands).
`scripts/brain_publish.py` owns the order, the gates and the proof; command syntax lives in the
replypen-cli README.

## Required Context

Background for actions and side effects, not needed for a routine publish. Paths are relative to this installed skill (`.agents/docs/` in a brain checkout, `docs/` in the kit):
[brain-model](../../docs/brain-model.md), [side-effects](../../docs/side-effects.md),
[support-boundary](../../docs/support-boundary.md), plus [actions](../../docs/actions.md) when
publishing `actions/<id>/`.

## Which repository

- `replypen-brain-skills` (the **kit**) → `./refresh-brains.sh --release patch` and stop; no generic
  Git step first, never `rp dev brain sync`/promote.
- **tenant brain** (`tenant =` in `.replypen.toml`) → the same command below. No channel or preflight:
  the box serves its own clone of `main`, which a push never moves, so the script syncs it (`rp dev
  brain publish --sha`, no `--channel`) and proves served `main` contains the SHA. `LIVE <p> tenant
  <slug> main @ <sha>`.
- **shared project brain**, including its `actions/<id>/` → below.

## Workflow: one command

From the brain checkout root (`<p>` = `project` in `.replypen.toml`). Uncommitted edits: stage the
intended paths first ([brain-git-sync](../brain-git-sync/SKILL.md) steps 1–2); untracked or unstaged
files stop the sync on purpose. A clean tree needs nothing.

```bash
uv run --no-project python .agents/skills/brain-publish/scripts/brain_publish.py --project <p> \
  --commit-message '<msg>'        # omit when nothing is staged
```

It runs lint + `brain_test.py --mode docker` (rerun after every merge), `brain_git_sync.py`, the
server preflight for every tenant on the channel, the edge/stable gate below, `rp dev brain publish
--sha <SHA brain_git_sync verified at origin/main>`, and proves `resolved_sha` from status. The receipt's
first line is the outcome, its last line the exit code:

| Exit | Receipt | Do |
|---|---|---|
| 0 | `LIVE …` / `NO-OP …` | Done; report the SHA and any `WARNING` line (e.g. `triage-source`: two triage homes at one scope). |
| 3 | `CONFLICT at git-sync` | Resolve keeping both sides' intent ([recovery](../brain-git-sync/references/recovery.md)), stage, rerun the same command. |
| 2 | `BLOCKED at <stage>` | Nothing published. `classify`: wrong repo/project (kit → `./refresh-brains.sh --release patch`). `git-sync`: a check, hygiene or local file needs fixing. `preflight`: fix the listed tenants. `edge-gate`: report the channel SHAs as the human decision. |
| 1 | `FAILED at publish/proof` | Not live; [Support handoff](#support-handoff). |

Flags: `--dry-run` (no commit/push/promote; preflights origin/main, ~10 s) · `--channel edge` (canary:
publish edge, observe, later rerun for stable) · `--verify-command '<cmd>'` adds a check, e.g. `'uv run
--no-project python .agents/skills/local-brain-work/scripts/brain_paths.py'` when the change cites
`/brain`, `/tenant`, `/mirrors` or `/kb` paths ·
`--skip-docker-tier` only when Docker cannot run (prints a lower-fidelity warning) · `--json`.
`--detached`: publish a worktree under `~` (docker sees only `$HOME`) at `origin/main` plus your commit,
when the shared `main` holds other agents' local commits. Run from a kit source checkout, the script
re-execs the brain's installed release kit (`RC_KIT_DEV=1` keeps the source kit), and `brain_test.py
--mode docker` stops with `KIT_RUNTIME_MISMATCH` when the kit's `runtime/` isn't the released image's.

Optional, before it: `rp dev brain render --project <p> --tenant <slug> --sha <sha>` to read 1–2
representative tenants' view; `rp ask --brain-ref dev/<branch>` for production-infra confidence
without moving live refs ([brain-ask](../brain-ask/SKILL.md)). Stronger proof after: a safe `rp ask`
without `--brain-ref`, then `rp run debug <id>` shows `brain_resolved` = `channel:<channel> @ <sha>`.

## Manual fallback

Use the steps the script automates only when it is unavailable or one step needs isolating.

- **`--project <p>` explicitly and `--scope project` on every `rp dev brain` call.** In a tenant
  context status answers about the tenant overlay, not the project channels.
- Laptop traps: spell interpreters `uv run --no-project … python`, never bare `python3` (inside
  `brain_git_sync.py` it is a dependency-less venv and the sync aborts after commit+merge); Docker
  import smoke whenever `docker info` succeeds, else say loudly it was skipped; declare local source
  checkouts in `.replypen.toml [mirrors]`; name skipped laptop DB/network checks.

1. Local checks from `local-brain-work`, passed as `--verify-command`s to the sync.
2. `brain_git_sync.py --json`; take `$SHA` only when `ancestry_verified` is true and `origin/main`
   resolves to it. No `rp dev brain` mutation before this.
3. `rp dev brain preflight --project <p> --scope project --sha "$SHA" --channel stable` names the
   tenants the candidate would break, touching no channel; `canary.ok` is the verdict (a
   `preexisting` data fault is excused). See [projection.md](../local-brain-work/projection.md).
4. **Edge/stable gate** before every `stable` publish (a consistency rule, not a licence for a canary
   pause after stable was authorized). Edge consumers = `canary.consumers` of a `--channel edge`
   preflight (never `checked`: an untemplated brain compiles none).
   - `0` → never create, advance or align edge; sync asks the server to GC stale edge refs
     (`BRAIN_EDGE_UNUSED` on a direct edge promote is expected).
   - edge at `$SHA` → continue; edge an ancestor of `$SHA` (or absent) → publish edge first.
   - edge ahead, diverged or unprovable → never downgrade or overwrite; report the channel SHAs and
     the decision needed. Never finish with stable ahead of a used, fast-forwardable edge.
5. `rp dev brain publish --project <p> --scope project --channel <c> --sha "$SHA" -o json` (sync →
   promote → verify). Never omit `--sha`; non-zero = failed publish. Isolating further: `rp dev brain
   sync`, then `promote --channel … --sha "$SHA"` (idempotent).
6. **Prove the ref, not the exit code:** `rp dev brain status --project <p> --scope project -o json`,
   `.status.channels[] | select(.channel == "<c>") | .resolved_sha == $SHA`. `origin_sha` is
   `origin/<channel>`, `main_sha` is `origin/main`; `matches_origin: true` beside a newer `main_sha` is
   the normal not-promoted-yet picture (`behind_main`). A `main` state of `current` never proves a
   channel-backed project is live.

## Support handoff

Stop before promotion and raise a ReplyPen support request when `publish` fails, when sync/status
reports a diverged managed cache or manual reconcile despite a clean Git sync, or for gaps the public
surface genuinely cannot do. Requested outcomes stay product-level ("grant project-maintainer promotion
access", "publish tenant brain main", "manual reconcile diverged brain cache", "wire/verify action
execution") — never private commands or infrastructure mechanics. Operator/break-glass promotion is
outside this skill.

```text
Project/brain:
Tenant, if any:
Brain repo path:
Branch/ref:
Commit SHA:
Change plane: shared project brain | tenant brain | action
Requested outcome:
Verification already run:
Run ids / trace URLs:
rp dev brain status/sync output:
Product gap: tenant brain publish | action wiring | manual reconcile | project promotion authorization
```
