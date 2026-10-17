# Client Onboarding

From zero to iterating on a brain with public tooling only.

## 1. Enter A Brain

```bash
cd ~/code/replypen/replypen-brain-<project>
```

Need `.env` for local live checks?

```bash
rp auth login
rp auth status
rp project env pull
```

`rp project env pull` writes a `0600` gitignored `.env` using your OAuth token. It does not print secret
values. Need a new key for a script and your login has secrets-write access? Use
`printf %s "$SECRET_VALUE" | rp project env set key=FOO_API_TOKEN` and document only the key name in the brain;
see [secrets.md](secrets.md).

## 2. Install The Kit

Local gitignored install, recommended:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/replypen/replypen-brain-skills/main/install.sh)
```

This creates/updates one shared clone at `~/.replypen-brain-skills` and links shipped skills into the
brain's Codex-native `.agents/skills/`. Claude Code discovers only `.claude/skills`, so the installer
creates a relative alias `.claude/skills -> ../.agents/skills` when that path has no user content —
**commit it** (`git add .claude/skills`). Git worktrees contain tracked files only, so an uncommitted
alias makes every brain skill an "Unknown command" there. The shipped skills themselves stay ignored
through `.git/info/exclude`, so installation neither changes tracked `.gitignore` nor reaches
committed `/brain`; the committed alias dangles in a fresh checkout, which prod skips and brain lint
only warns about.

Ignored kit paths do not follow a worktree, so the installer lists the kit's four discovery paths in
the repo-root `.worktreeinclude` allowlist; otherwise re-run `install.sh <worktree-dir>` in the worktree.

Do not install Brain Dev as a user/global Codex or Claude Code plugin. These skills must be discovered
from the brain checkout's canonical `.agents/skills/` tree. A user/global install makes
the same skills appear in unrelated projects and can drift from the brain's pinned repo-local install.

Update later with `brain-dev-upgrade`.

## 3. Run Local Checks

```bash
SKILL="${RC_BRAIN_KIT:-$HOME/.replypen-brain-skills}/skills/local-brain-work"
uv run "$SKILL/scripts/brain_run.py" --brief
uv run "$SKILL/scripts/brain_run.py" skills/databases/scripts/lookup_customer.py --email a@b.com
uv run "$SKILL/scripts/brain_test.py"
uv run "$SKILL/scripts/brain_test.py" --live
uv run "$SKILL/scripts/brain_test.py" --mode docker --live
```

Docker mode needs Docker/colima and pulls `ghcr.io/replypen/workspace:<tag>`. It covers image,
deps, read-only mounts, and container isolation; it does not prove production egress allowlists.

## 4. Verify Production Behavior

```bash
rp ask "Hi, my account is sophie@example.com. Do I still have open invoices?"
rp ask "Which table holds invoice state?" --scenario raw
git push origin dev/<branch>
rp ask "<customer-style question>" --brain-ref dev/<branch>
rp run debug <run_id>
```

Use `rp-debug` for trace analysis, `brain-git-sync` to reconcile and push local/cross-computer work,
and `brain-publish` to sync/promote the verified `origin/main` SHA.

## Definition Of Done

For a brain change: local checks pass or known laptop infra gaps are named, `brain-git-sync` proves
local `main` and freshly fetched `origin/main` are the same ancestry-verified SHA, production confidence
is captured with `rp ask --brain-ref` when needed, and `brain-publish` has either completed the public
flow or produced a ReplyPen support request. For a channel-backed shared brain, do not call the
publish successful until status or a normal run without `--brain-ref` proves the exact intended SHA.
