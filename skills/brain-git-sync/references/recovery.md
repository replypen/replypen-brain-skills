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

The script keeps retry accounting and protected remote tips inside Git metadata, not the worktree.
Rerun the same workflow so it can preserve the original report and clear that state on success; do
not delete the recovery file or `refs/brain-git-sync/` refs by hand.

## Remote race

Do not push manually. The script refetches, merges the newly observed `origin/main`, and retries up to
`--max-push-attempts`. If the bound is exhausted, rerun the same workflow; persistent churn or an
unresolvable managed-cache divergence is a concise ReplyPen support request, not a force-push.

## Evidence

Use `--json` for handoff or publish integration. Preserve `initial_local_sha`, `initial_remote_sha`,
`initial_topology`, `final_sha`, `local_commits`, `remote_commits`, `committed_files`,
`observed_remote_shas`, `push_attempts`, and `ancestry_verified`. Exit `0` plus
`ancestry_verified: true` is the publication precondition.
