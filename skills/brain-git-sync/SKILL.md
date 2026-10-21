---
name: brain-git-sync
description: "Safely reconcile and publish a brain repository's local Git work with origin/main. Use for `$brain dev: git sync`, `$brain-git-sync`, cross-computer Git synchronization, local/remote main divergence, or before brain-publish; do not use brain-publish for a pure Git-sync request."
---

# Brain Git Sync

The owning home for making `origin/main` the cross-computer rendezvous without losing local or remote
work. `scripts/brain_git_sync.py` owns the mechanics — deterministic fetch, merge, bounded push-retry
under a race, ancestry verification. The agent owns intent: what is real work, secret hygiene, conflict
resolution, and which tests count.

Production is written by the brain too (journal/consolidation commits) and the same brain is often
edited from a second computer, so a divergent `origin/main` is normal, not an anomaly — reconcile it,
never overwrite it.

## Workflow

1. **Inventory before mutating.** Read the repo's `AGENTS.md` and commit rules, then look at the full
   working state: branch, staged and unstaged diffs, untracked files (inspect them by content, not just
   name). Never echo secret values.

2. **Decide what is intended repository work.** Include real source; never discard a file because it is
   inconvenient. Exclude secrets, caches, build output, dumps, and unrelated in-flight work — leave that
   in place. Stage intended paths explicitly with one repo-appropriate commit message. Stop and ask only
   when committing safely would mean guessing ownership or exposing/deleting data.

3. **Run the primitive from the repository root**, never hand-written fetch/pull/push logic:
   ```bash
   uv run --no-project python "<skills>/brain-git-sync/scripts/brain_git_sync.py" \
     --repo "$PWD" --commit-message '<message>' --max-push-attempts 4 \
     --verify-command '<focused check>'   # repeatable
   ```
   Omit `--commit-message` when nothing is staged. Each `--verify-command` reruns after every merge and
   before push, so a merged tree is never published untested. Files deliberately left uncommitted cause
   a safe stop — report them rather than hiding them. Before any mutation the primitive checks
   `rp dev brain status` (`--project` explicit, else the checkout's `.replypen.toml`); a project whose
   `status.brain_source.read_only` is true (a read-only brain alias) is refused outright — sync from
   the source project's own checkout instead.

   **Built-in hygiene gate (brains only, no flag):** before every push the script also runs
   `local-brain-work/scripts/brain_hygiene.py` on the merged tree and blocks (exit 2, nothing pushed)
   on git conflict markers anywhere, and, in files changed vs `origin/main`: undeclared/residual
   projection `{{ }}`, `/Users/` paths, dead relative links, em dashes in customer copy (action
   `display_name`/`customer_*`, `projection.yaml` defaults, `customer_facing: true` md), and mermaid
   blocks `mmdc` rejects (skipped with a notice when no renderer runs). Born from the 2026-09-25
   DentAI publish of `<<<<<<<` markers. Fix the file and rerun; `brain_hygiene.py --all` shows legacy debt. `install.sh`
   also installs a managed `pre-commit` hook (`--staged`) so the same rules fail at commit time.

4. **Exit 3 = conflicts.** Resolve with repository context, preserving both sides' intent; never take
   ours/theirs wholesale just to finish. Stage each resolved path, run the focused tests, rerun the same
   script — it resumes the merge and re-enters the bounded race loop.
   [recovery.md](references/recovery.md) covers conflict and blocker recovery.

5. **Finish only on exit `0`.** Report the final SHA, the local and remote commits integrated, files
   committed, and anything intentionally left unpublished. `--json` gives machine evidence; require
   `ancestry_verified: true` — `brain-publish` takes its publish SHA from exactly that receipt. Goal is
   "make it live"? Run `brain-publish/scripts/brain_publish.py` instead: it calls this primitive itself.

## Landing from a shared checkout

Parallel agents share one checkout (a mirror like `kampadmin-replypen-common`, this kit): never
commit, amend or stash there. `scripts/git_land.py` lands only your change via a throwaway detached
worktree at `origin/main` (hooks run, push retried on a race, never forced, worktree always removed):

```bash
uv run --no-project python "<skills>/brain-git-sync/scripts/git_land.py" --repo "$CHECKOUT" \
  --paths skills/a.py docs/b.md --message 'fix: …' \   # or --commit <sha> (reuses its message)
  --also stable --refresh-mirror <mirror-name>   # mirrors only
```

`--refresh-mirror` alone runs `rp dev mirror publish`: every project mirroring the repo is refreshed at
its tracked branch and verified, so exit 0 means the next runs read the commit. `--project p` (repeatable)
narrows it to per-project `rp dev mirror refresh`.

Reruns are safe: when `main` already has the bytes the status is `noop`, yet `--also` branches still
fast-forward and mirrors still refresh. `--dry-run` builds the commit and pushes nothing. Exit 2 = blocked (hook, secret/ignored path,
`stable` not behind `main`), 3 = cherry-pick conflict (nothing landed), 4 = a mirror refresh failed.

## Hard rules (data loss if violated)

- Branch `main`, an `origin`, and a freshly fetched `origin/main`.
- Merge divergent work; never rebase. Fast-forward only when cleanly behind.
- Never reset, force-push, destructively check out, or use the global stash (shared state, its own
  races).
- Never report success until local `main` equals a freshly fetched `origin/main` and every observed
  local and remote commit is an ancestor of the final SHA.
