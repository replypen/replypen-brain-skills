# Action-script lint — fleet evaluation (false positives / negatives)

## Context (2026-08-26)
`runtime/lib/action_lint.py` (shipped in kit v0.3.17, wired via `lib.brain_lint` into every brain's
offline `brain_test.py` tier) adds three checks on `actions/*/{script,preflight}.py`:
size budget (WARN ≥64 KiB / FAIL ≥96 KiB, override `actions/lint.yaml`), duplicate/drifted `_private`
helpers across ≥3 scripts (WARN), dead module-level `_private` names (WARN).
Read the module docstring + `docs/actions.md` "Script Hygiene" + `docs/specs/action-script-hygiene.md`.

Only validated so far on `replypen-brain-dentai` (origin/main): 1 FAIL, ~46 WARNs, 4 dead-code
hits all confirmed real, 0 false positives seen.

## Task
1. Enumerate every local brain checkout: `~/code/replypen/replypen-brain-*` (~22). Use each
   repo's `origin/main` via a throwaway `git worktree add /tmp/... origin/main --detach` (never
   checkout/reset the shared checkout; remove the worktree afterwards).
2. Run the lint on each: `lint_actions(root)` from `lib.action_lint` (runtime at
   `replypen-brain-skills/runtime`, `PYTHONPATH=$(pwd)/runtime` — uv's `--with .` can serve a
   stale cached wheel). Also run the real path once (`brain_test.py`) on 2–3 brains to confirm wiring.
3. Per brain, tabulate findings. For every finding, classify: TRUE / FALSE POSITIVE (with reason).
   Hunt false negatives: helpers duplicated in only 2 scripts, dup helpers among `preflight.py`
   (currently only `script.py` feeds the dup detector), dead public names, near-identical bodies
   differing by one constant, oversized `policy.py`, etc. Judge whether each FN class is worth a
   rule (low-FP bar; spec says lean).
4. Fix real FPs in the lint (with unit tests in `runtime/tests/test_action_lint.py`), tune
   thresholds only with evidence. Do NOT edit brain repos except to note findings.
5. Report: table brain × (FAIL, WARN by class, FP count), the FP/FN analysis, any lint changes.
   `runtime/lib/**` edits owe a kit release: `./refresh-brains.sh --release patch` (RELEASING.md),
   run `./check-release-coherence.sh` first. Commit on `main`.
