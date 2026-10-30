# Git Sync Recovery

## Merge conflict

The sync script exits `3` and leaves the merge in progress. Inspect `git status`, each conflicted file,
and all three index stages with `git show :1:path`, `git show :2:path`, and `git show :3:path` when
useful. Resolve from repository intent, including compatible changes from both computers. Search
callers/tests before resolving source conflicts; run focused tests afterward.

Remove conflict markers, stage only resolved paths, and rerun `brain_git_sync.py`. With a clean index
and `MERGE_HEAD`, it creates the merge commit via the existing merge message, fetches again, and
continues bounded push retries.

## Local-work blocker

Exit `2` never discards work, but a safe local commit or merge may already exist when a verifier or
push blocks. Inspect the reported SHAs plus every staged, unstaged, and untracked path. Stage intended
repository work explicitly and rerun with `--commit-message`; leave ignored/local-only material out.
If unrelated work must remain unpublished, report the blocker rather than stashing, deleting, or
silently including it.

The script keeps retry accounting and protected remote tips inside Git metadata
(`.git/brain-git-sync-state.json`, `refs/brain-git-sync/`), not the worktree. Rerun the same workflow
so it can preserve the original report and clear that state on success ("Resuming an unfinished sync
(N days old)" says one is pending); never delete it by hand: the rewritten-remote case below is the
scripted way out.

## Rewritten remote (status `rewritten`, exit 2)

`origin/main` was force-updated and no longer holds a remote commit this checkout remembers (its
tracking ref or a pending sync's state). Merging that commit back would republish the replaced history
(2026-10-30: `journal/` stripped from every brain's history; a remembered tip would have re-added 1092
journal commits), so the script stops before merging or pushing, and keeps stopping on reruns.

- **Intended rewrite** (the project owner rewrote history): rerun with `--rewritten-remote accept`.
  Commit intended uncommitted work first. The old HEAD is kept at `refs/brain-git-sync-rescue/<time>`,
  main moves onto the new `origin/main`, commits only this computer had are cherry-picked on top, the
  stale sync state is cleared, and the normal sync continues. A cherry-pick conflict (e.g. a commit
  touching a path the rewrite removed) stops with exit 3: resolve, `git cherry-pick --continue`, rerun
  without the flag. Delete the rescue ref once the result is checked.
  A local merge commit with changes of its own (a conflict resolution) stops `accept`: keep the old
  main (`git branch brain-git-sync-rescue`), `git reset --keep origin/main`, cherry-pick the listed own
  commits, re-apply the merge's changes (`git show --remerge-diff <sha>`) as a commit, then rerun
  with `--rewritten-remote accept` (it now only clears the state).
- **Commits dropped by mistake** (someone force-pushed by accident): rerun with
  `--rewritten-remote restore` to merge the dropped commits back and push.
- Unsure which: ask the brain owner; do not pick one to make the error go away.

## Remote race

Do not push manually. The script refetches, merges the newly observed `origin/main`, and retries up to
`--max-push-attempts`. If the bound is exhausted, rerun the same workflow; persistent churn or an
unresolvable managed-cache divergence is a concise ReplyPen support request, not a force-push.

## Evidence

Use `--json` for handoff or publish integration. Preserve `initial_local_sha`, `initial_remote_sha`,
`initial_topology`, `final_sha`, `local_commits`, `remote_commits`, `committed_files`,
`observed_remote_shas`, `push_attempts`, and `ancestry_verified`. Exit `0` plus
`ancestry_verified: true` is the publication precondition.
