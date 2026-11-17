# Editing a tenant brain

> [!IMPORTANT]
> **Tenant brain → `rp` throwaway checkout. Project brain → GitHub clone + `brain-publish`.**
> Decide per brain, never by habit: `rp --project <p> [--tenant <slug>] -o json dev brain status` →
> `.status.remote_mode`. `github` ⇒ GitHub flow (its clone, [brain-git-sync](../skills/brain-git-sync/SKILL.md),
> [brain-publish](../skills/brain-publish/SKILL.md)). `none` ⇒ the recipe below. Most tenant brains
> are `none` (the box is their only copy); a few still say `github` and keep the old flow until switched.

```bash
# read only, no checkout (`dev brain render` shows the PROJECT brain as that tenant sees it, not /tenant)
rp dev console bash run --project <p> --tenant <slug> 'cat /tenant/AGENTS.md'

# edit one tenant brain
D=$(rp dev brain clone <p> --tenant <slug> --temp -o json | jq -r .dir)   # throwaway, on main
cd "$D"; …edit, run tests…
rp dev brain push -m "<what and why>"     # optional proof: commits + uploads, prints dev/<name>
rp ask --project <p> --tenant <slug> --tenant-brain-ref dev/<name> "<question>"
rp dev brain publish -m "<what and why>" --rm   # commit, land on main (boot check), verify, delete checkout
```

- `--tenant-brain-ref` tests a tenant ref (`/tenant`); `--brain-ref` is always the project brain.
- Without `-m` a dirty tree is refused. `publish` replays your commit onto a `main` that moved meanwhile
  unless the same paths changed upstream: then it stops, checkout untouched; re-apply on a fresh clone.
- `--rm` deletes the checkout only after the box's `main` is verified at your commit; any failure keeps it
  and prints its path.
- Sweep over many tenants: the same steps in a loop over `rp --project <p> project tenant ls -o json`,
  one receipt per tenant, trial one tenant first; regenerate generated output on a fresh clone.
- Rollback: fresh clone, `git revert <sha>`, publish.
- No channel and no promote: a run reads a tenant brain at its `main`.
- Standing `replypen-brain-<p>-tenant-<slug>` GitHub clones of a `none` brain are stale for good: never
  push or publish from them (`brain_git_sync.py` and `brain_publish.py` refuse); delete them when
  convenient. `refresh-brains.sh` still relinks them (telling them apart would cost an API call per clone).
