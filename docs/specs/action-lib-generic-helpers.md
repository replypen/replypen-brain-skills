# Generic action helpers in `lib.action` (candidate 5 of action-script-hygiene)

## Context (2026-08-26)
`docs/specs/action-script-hygiene.md` measured the dentai action corpus: 63% helper defs, 42%
boilerplate; 193 copies of 43 helpers. The lint (`lib.action_lint`, kit v0.3.17) now warns on
duplicated/drifted helpers. A parallel dentai thread hoists the ClickDoc/dentai-specific boilerplate
into project-level shared code — **do not do that here**. This thread ships only the helpers that are
generic across projects into the kit's `runtime/lib/action/` and then adopts them in the brains that
benefit.

## Task
1. Survey all local brains (`~/code/replypen/replypen-brain-*`, use `origin/main` in a
   throwaway `/tmp` worktree per repo) for helpers duplicated across actions/brains. Candidates from
   the dentai corpus: timezone helper (Europe/Brussels parse/format incl. DST `fold`), verify-after-
   mutate pattern (read back, compare expected fields, `action.fail` on mismatch), idempotency-key
   helper (stable hash of run/action/params), "plain params" coercion, readable datetime labels,
   reviewer-safe label escaping, notify tail. Keep only what is truly project-agnostic and used (or
   obviously usable) by ≥2 projects; the bar is "trivially generic", no framework.
2. Implement in `runtime/lib/action/` (look at existing modules + `docs/actions.md` "Hosted Python
   Harness" for style), with focused tests in `runtime/tests/`. Keep the public surface tiny and
   documented in docstrings.
3. Document concisely in `replypen-brain-skills`: a short section in `docs/actions.md` (what
   exists, when to use, one example) — no god file, no repetition of docstrings.
4. Adopt in the brains that can use it: replace the local copies in each qualifying brain's
   `actions/*/script.py|preflight.py`, run that brain's offline tier (`brain_test.py`) and
   `brain_action.py <id> --preflight-only` where a `.env.action` exists. Commit per brain on its
   `main`, push. Brains consume the *released* kit tag — so release the kit first
   (`./refresh-brains.sh --release <patch|minor>`, RELEASING.md, `./check-release-coherence.sh`),
   then bump/upgrade in each brain (`brain-dev-upgrade` skill) before adopting.
5. Report: helpers shipped (with rationale), helpers rejected (why), brains updated with LOC delta,
   kit version released.
