# Action-lint developer experience — instant feedback while building a brain

## Context (2026-08-26)
`lib.action_lint` + `lib.brain_lint` (kit v0.3.17) lint brains in the offline `brain_test.py` tier.
Problem: feedback is pull-based and slow. An agent editing `actions/*/script.py` learns nothing until
it chooses to run `brain_test.py`; first run pays a uv env build (minutes when the cache is cold or
locked — a stale `--with .` wheel bit us today); WARNs surface as pytest warnings buried under
"58 warnings", so dup/drift/dead-code findings are effectively invisible — only the size FAIL is
loud. Nothing runs at git push, in brain CI, or on the host at promote/publish.

Goal: the agent building a brain sees hygiene findings within seconds of an edit and cannot ship an
oversized script. Keep it lean; no new frameworks.

## Deliverables (this order; each independently landable)
1. **Standalone entrypoint** `skills/local-brain-work/scripts/brain_lint.py`: stdlib+PyYAML only,
   no uv env (`uv run --no-project` with the kit's `runtime/` on `sys.path`, or plain python), runs
   `lint_brain` + prints a compact report (FAILs, then WARNs grouped by class; exit 1 on FAIL,
   `--strict` to fail on WARN). Target < 1 s on dentai. Wire it into `local-brain-work/SKILL.md`
   as the step to run after every action-script edit, and into `brain-publish`'s verify step.
2. **Visible in `brain_test.py`**: replace the pytest-warning spray with one compact hygiene block
   printed at the end of the offline tier (same formatter as 1). Keep FAIL semantics.
3. **Host-side gate**: in the sibling `replypen` repo, run the same lint (the runtime is installed
   in the workspace image; call `lib.brain_lint.lint_brain`) during brain publish/promote preflight
   (`rp dev brain promote`/publish path — find it in `replypen/internal/brain/`). FAIL blocks,
   WARN is reported. Tests. Coordinate: `replypen` pins the kit tag — bump only if you need new lib
   surface; prefer using what v0.3.17 already ships.
4. **Optional pre-push hook** installed by the kit's `install.sh` into brain checkouts (opt-in flag,
   documented) that runs (1). Skip if it adds friction; report the decision.

## Constraints
- Two other Sol threads touch `replypen-brain-skills` now (patch release; lint FP/FN eval; generic
  helpers). Rebase on `origin/main` before releasing; expect version bumps.
- `runtime/lib/**` edits owe a kit release (`./refresh-brains.sh --release patch`, RELEASING.md,
  `./check-release-coherence.sh` first). Docs/skill-only changes still release (brains fetch tags).
- Verify on `~/code/replypen/replypen-brain-dentai` via a throwaway `/tmp` worktree on
  `origin/main` (never checkout/reset shared checkouts). Measure wall-clock of (1).
- Report tersely: what landed where (kit sha/tag, replypen sha), measured latency, anything skipped.
