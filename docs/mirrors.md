# Mirrors

A source mirror is a read-only snapshot of an external source repo or knowledge source mounted at
`/mirrors/<name>` during a run. Brain scripts should resolve mirrors with `lib.fs.mirror_path()` or
`lib.fs.mirror_scripts()`; they should never write them or hardcode the production mount.

## Mental Model

- Brain freshness and mirror freshness are independent. `rp ask --brain-ref dev/x` changes which brain
  ref production mounts, but it does not refresh mirrors or knowledge-base sync state.
- In production, mirrors refresh periodically from the repo's tracked branch (`rp project repo ls` →
  `default_branch`; may be a promoted `stable`, not `main`). After pushing to that branch, a project
  admin can request immediate exact-commit feedback:

```bash
rp dev mirror refresh --repo <name> --expect-sha "$(git rev-parse HEAD)"
```

  The command waits for the existing refresh worker, verifies the mirror worktree reached that full
  SHA, and expires warm console workspaces. It does not restart ReplyPen or Docker images. Others
  pushing meanwhile → add `--accept-descendant` (accepts a newer tip that contains your SHA, ancestry
  checked in your checkout) instead of retrying a 409.
- Declare each required local checkout once in committed `.replypen.toml`; values are relative to
  the brain root and ignored by `rp`:

```toml
[mirrors]
"customer-app" = "../../customer/customer-app"
```

  Local uv mode exports `RC_MIRROR_CUSTOMER_APP`; Docker mode bind-mounts the path at
  `/mirrors/customer-app`. A missing declared path raises `lib.fs.MirrorMissing`. For temporary
  overrides, pass mirrors explicitly; CLI flags win over the committed table:

```bash
uv run "$SKILL/scripts/brain_run.py" --mirrors-root ~/mirrors ...
uv run "$SKILL/scripts/brain_run.py" --mirror app=~/code/customer-app ...
```

- `brain_run.py --brief` shows resolved and missing declared mirrors.
- `rp fleet health` reports stale/failed mirrors and dead-lettered runs from the public API.

## Triage

| Evidence | Interpretation |
|---|---|
| `MirrorMissing` locally | Fix the `[mirrors]` path or override it with `--mirror name=path`; declared mirrors never skip silently. |
| `rp fleet health` reports a stale/failed mirror | Retry `rp dev mirror refresh` with the pushed SHA; if it fails, escalate with the command error and mirror name. |
| A prod run read old source content | Check run trace "Files the run read" and `rp fleet health`; mirror freshness may lag brain deploy. |
| A dev-ref run still sees old source content | Expected if only the brain changed. Dev refs do not change mirror snapshots. |
