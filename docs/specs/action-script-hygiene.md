# Action-script hygiene + lint (brain kit)

## Context (2026-08-26)
DentAI brain `actions/confirm_reschedule/script.py` grew 32 KB → 109 KB in 25 days (3.4×/month) and
crossed Linux MAX_ARG_STRLEN (128 KiB b64) in the host's docker argv transport → action dead in prod.
Host transport is being fixed separately (stdin). This thread is about the *hygiene* side.

Measured across the 10 dentai action scripts (472 KB corpus): 63% helper defs, 42% boilerplate
(ClickDoc client/login, reads, db+tenant scope, verify-after-mutate, cache_write, presentation,
notify tail, timezone, entrypoint). 114 KB byte-identical duplicates (193 copies of 43 helpers);
42 helpers drifted (e.g. `_resolve_appointment` tenant guard: 6 copies, 6 variants → produced a real
cross-tenant join bug). Scripts only use 8 `lib` symbols. Single-file is mandated because only
`sha256(script.py)` is pinned/shipped. A parallel thread is moving the ClickDoc/dentai boilerplate
into project-level shared code; do not do that here.

## Goal
Cheap, low-false-positive guardrails in the kit (`replypen-brain-skills`) so brains keep action
scripts small and conventions followed — applied automatically via the existing offline test/lint
tier (`lib.brain_lint`, `brain_test.py`, `local-brain-work` skill). No over-engineering.

## Candidates (evaluate, keep what earns its place)
1. Size budget: every `actions/*/script.py` + `preflight.py` raw < 64 KB (warn) / 96 KB (fail), with
   a per-brain override file. Fail message explains the argv reason.
2. Duplicate-helper detector: same `def` name with byte-identical body in ≥3 action scripts → warn
   "move to lib/shared". Drifted same-name bodies → warn with diff hint. Keep threshold conservative.
3. Dead-code: module-level constants/functions never referenced in-file (e.g. `_ORPHANED`,
   `_UNVERIFIED` strings unused). Use `vulture`-style AST, only flag `_private` names.
4. Conventions doc in the `brain-authoring`/actions skill: what belongs in script vs lib, size
   expectations, when to split an action.
5. Consider whether `lib` should ship generic helpers (brussels tz w/ fold handling, verify-after-
   mutate pattern, idempotency key helper) — propose, don't build unless trivially generic.

## Verification
Run the lint against the dentai brain checkout (`~/code/replypen/replypen-brain-dentai`, use
`origin/main`) and report signal/noise. Cut a kit release per RELEASING.md only if the change is
ready; otherwise land on main and report.
