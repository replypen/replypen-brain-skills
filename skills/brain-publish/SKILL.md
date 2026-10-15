---
name: brain-publish
description: "Publish, ship, deploy, or promote rootcause brain changes after safely reconciling Git with origin/main. Use for `$brain dev: publish`, `$brain-publish`, making brain edits live, exact-SHA server sync, stable/edge promotion, tenant/project publish, actions, or a RootCause support request. Do not use for a pure `$brain dev: git sync` request; use brain-git-sync."
---

# brain-publish — make a brain change live

The shared final step after brain edits from `local-brain-work`, `brain-ask`, `rc-debug`, `rc-health`,
`rc-fleet`, or manual authoring. A project-maintainer OAuth login is enough; no private RootCause
surface is involved.

**The one invariant: publish an exact SHA that was verified at `origin/main`.** Never a pre-sync `HEAD`,
an ambient branch, an unverified push, or an implicit remote tip. Everything below exists to protect
that, or to prove afterwards that the intended ref really moved.

These flows assume a current `rc` — upgrade with `brain-dev-upgrade` before debugging odd routing or
missing subcommands. Command syntax lives in the rootcause-cli README; this skill owns the order, the
gates, and the proof.

## Required Context

Paths are relative to this installed skill (`.agents/docs/` in a brain checkout, `docs/` in the kit):
[brain-model](../../docs/brain-model.md), [side-effects](../../docs/side-effects.md),
[support-boundary](../../docs/support-boundary.md), plus [actions](../../docs/actions.md) when
publishing `actions/<id>/`.

## Two flags that decide whether you verified the right brain

- **`--project <project>`, explicitly, on every `rc dev brain` call.** An implicit project is one
  ambient-state guess away from publishing or verifying the wrong brain.
- **`--scope project` for anything about channels.** In a tenant context (tenant-bound login,
  `--tenant`, or a tenant brain checkout) `rc dev brain status` answers about the *tenant overlay*, not
  the project channels. Channel verification MUST pass `--scope project` and read `.status.channels[]`.

## Classify the repository first

- `rootcause-brain-skills` — the **kit**, not a brain: run `./refresh-brains.sh --release patch` and
  stop. It creates the version commit, reuses `brain_git_sync.py`, proves the commit at `origin/main`,
  then tags. Do not run the generic Git step first (unversioned kit bytes on `main` break release
  coherence), and never `rc dev brain sync`/promote for the kit.
- **shared project brain** (`skills/`, `playbooks/`, projection templates, shared action catalog) —
  channel-backed; the rest of this skill applies.
- **tenant brain** (overlay/free-form instructions) — always runs from its own `main`; there is no
  promotion route. Never publish one to a channel.
- **action** (`actions/<id>/`) — same as its host brain, plus `docs/actions.md` proposal/execution rules.

## Workflow

1. **Local checks, recorded as `--verify-command` arguments.** Pick the smallest relevant set from
   `local-brain-work` (`brain_lint.py`, `brain_test.py`, `brain_smoke.py`, `brain_structure.py`; live /
   projection / action preflight only when appropriate; `brain_paths.py` when the change cites or moves
   `/brain`, `/tenant`, `/mirrors` or `/kb` paths). They are passed to the Git-sync primitive so it
   reruns them after every merge — a merge tree must never be published untested. For the kit use its
   own validators plus `SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh`.

   Laptop traps:
   - Spell interpreters as `uv run --no-project … python`, **never bare `python3`**: inside
     `brain_git_sync.py` a verify command runs after `uv run`, where `python3` is a dependency-less venv
     — the sync then aborts *after* commit+merge.
   - Import smoke is mandatory and runs in Docker whenever `docker info` succeeds; on fallback print a
     loud lower-fidelity warning rather than silently downgrading.
   - Declare local source checkouts in `.rootcause.toml [mirrors]` so a missing mirror fails loudly
     instead of becoming a silent collection skip.
   - Missing laptop DB/network access is not a mysterious failure — name what was skipped and cover it
     with production validation later.

2. **Mandatory Git precondition:** run the full [`brain-git-sync`](../brain-git-sync/SKILL.md) workflow
   (inventory and staging happen there) and take `$SHA` from its `--json` receipt only after
   `ancestry_verified` is true and `origin/main` resolves to it. No `rc dev brain` command runs before
   this succeeds.

3. Confirm access: `rc auth status`, `rc auth access`.

4. **Optional production-infra confidence without moving live refs:** push `$SHA` to a `dev/<branch>`
   ref and run `rc ask --brain-ref dev/<branch>`. Capture run id, status, trace URL, and
   `rc run brain-diff <id>` when relevant.

5. **Re-run step 2 immediately before publishing** and replace `$SHA` from the fresh receipt — this
   absorbs production-authored journal/consolidation commits and pushes from another computer.

6. **Templated brains: look before you promote.** `rc dev brain render --project <p> --tenant <slug>
   --sha "$SHA"` shows what 1–2 representative tenants actually read; `rc dev brain preflight --project
   <p> --scope project --sha "$SHA" --channel stable` dry-runs the promotion and names the tenants the
   candidate would break, touching no channel. Fix or consciously accept every degradation.
   (See [projection.md](../local-brain-work/projection.md) for render vs preflight.)

7. **Edge/stable consistency gate** — before every `stable` publish, reconcile an actively used `edge`.
   This is a consistency rule, not a licence to invent a canary pause after stable was authorized.
   - `canary.consumers` in project-channel status is the authoritative active-edge-pin count. Never
     infer use from `canary.checked` (an untemplated brain has consumers but compiles none).
   - `consumers == 0` → do not create, advance, or align edge. Sync asks the server to GC stale
     local/origin edge refs; verify status stops reporting edge. A direct edge promote failing with
     `BRAIN_EDGE_UNUSED` is expected.
   - edge already at `$SHA` → continue. edge an ancestor of `$SHA` → publish `$SHA` to edge first, and
     observe it before stable when the risk warrants a canary interval.
   - edge ahead of `$SHA`, diverged, or ancestry unprovable → never downgrade or overwrite; report the
     exact channel SHAs and the human decision still needed.
   - After stable, read status again: never finish with stable ahead of an actively used,
     fast-forwardable edge — align it and verify both. A divergent edge stays untouched and is called
     out as the next action.

8. **Publish.** `rc dev brain publish --project <p> --scope project --channel stable --sha "$SHA"` does
   sync → promote → verify in one guarded call and exits non-zero on any mismatch (`-o json` for the
   receipt). Never omit `--sha`. A non-zero exit is a failed publish — do not report the change live.
   Only when `publish` is unavailable or a step needs isolating, fall back to the choreography it
   automates: `rc dev brain sync`, then `rc dev brain promote --channel … --sha "$SHA"`, then status.
   Promote is idempotent; treat an unknown/unreachable SHA, unsafe channel, push failure, or
   tenant-scoped/wrong-project denial as a failed publish.

9. **Prove the ref, not the exit code.** In `rc dev brain status --project <p> --scope project -o json`,
   select `.status.channels[]` by channel and confirm `resolved_sha == $SHA`. Reading a mismatch:
   `origin_sha` is `origin/<channel>` (the channel branch on GitHub, **not** `origin/main`), `main_sha`
   is `origin/main`; `matches_origin` only says the box's channel ref equals the pushed channel branch,
   `matches_main` says the channel sits at the `main` tip. So `matches_origin: true` beside an old
   `origin_sha` and a newer `main_sha` is the normal "not promoted yet" picture (`state: behind_main`).
   A `main` state of `current` never proves a channel-backed project is live.

   Stronger proof when warranted: a safe `rc ask` **without** `--brain-ref`, then `rc run debug <id>`
   showing `brain_resolved` = `channel:<channel> @ $SHA`. For direct-`main` projects, confirm the on-box
   and origin `main` SHAs and use a normal run or `rc dev console bash list`.

## Support handoff

Stop before promotion and raise a RootCause support request when `publish` fails, when sync/status
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
rc dev brain status/sync output:
Product gap: tenant brain publish | action wiring | manual reconcile | project promotion authorization
```
