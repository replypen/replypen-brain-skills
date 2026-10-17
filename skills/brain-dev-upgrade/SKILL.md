---
name: brain-dev-upgrade
description: "Update or check the installed ReplyPen brain-skills kit and the rp CLI on a local brain checkout. Use when asked to get the newest kit skills or rp, when a skill or rp subcommand is missing or behaves oddly, or before debugging routing problems."
---

# brain-dev-upgrade - update the local kit + `rp`

Two independently versioned things live on the laptop: the **kit** (`replypen-brain-skills`, installed
per brain checkout by `install.sh`) and the **`rp` CLI** (sibling repo `replypen/replypen-cli`).
Neither auto-updates inside an already-installed Codex or Claude setup — that is why this skill exists.

## Why the skills tree looks the way it does

`.agents/skills/` is the canonical discovery tree (Codex). `.claude/skills` is a relative alias to it and
is meant to be **committed**: Claude Code discovers only `.claude/skills`, and a git worktree carries
tracked files only, so an uncommitted alias makes every brain skill "Unknown command" there. The skill
content behind the alias stays in `.git/info/exclude`, not a tracked `.gitignore` — so a fresh worktree
needs `install.sh <worktree-dir>` re-run to get content.

`rp` on macOS has one canonical install: Homebrew (`replypen/tap/rp`). `go install` is a
developer path, not an end-user upgrade path — a Go copy earlier on `PATH` shadows the Homebrew one and
`self update --migrate` exists to clean exactly that up.

## Workflow

1. **`rp` first** — the rp-* skills depend on it (`rc` is its legacy alias; same binary):
   ```bash
   which -a rp rc && rp --version && rp self doctor && rp self update --check
   ```
   `rp self doctor` reports executing binary, PATH selection, install kind, duplicates and remediation;
   `--check` is read-only. Missing on macOS → `brew install --cask replypen/tap/rp`. Only `rc` on PATH (pre-rename install)
   → `rc self update --migrate` moves it to `rp` and keeps `rc` as an alias.

2. **Upgrade `rp`** when asked: macOS `rp self update --migrate` then `hash -r`; other platforms
   `rp self update`. A client too old to have `rp self update` needs its legacy `rp upgrade` once (or
   Homebrew) to reach the current command surface. To avoid updating a *shadowed* binary on a pre-doctor
   client:
   ```bash
   brew update && (brew install --cask replypen/tap/rp || brew upgrade --cask rp)
   "$(brew --prefix)/bin/rp" self update --migrate && hash -r
   ```
   If `--migrate` is unknown even on the freshly installed cask, the required CLI release has not reached
   Homebrew yet — **stop**; never delete a binary by hand.

3. **Upgrade the kit.** From inside a brain checkout, or pass the brain path from elsewhere. The
   installer URL always tracks `main`, but it pins the shared kit clone to a *released tag*:
   ```bash
   INSTALL=https://raw.githubusercontent.com/replypen/replypen-brain-skills/main/install.sh
   bash <(curl -fsSL $INSTALL) [~/code/replypen/replypen-brain-<project>]
   bash <(curl -fsSL $INSTALL) --latest-version   # check without installing
   ```
   If the current directory is not inside a brain checkout, locate the brain repo or ask for its path.

4. **Prove the upgrade landed** — compare installed tag against latest; a mismatch is a failed upgrade,
   surface it rather than continuing:
   ```bash
   git -C "${RC_BRAIN_KIT:-$HOME/.replypen-brain-skills}" describe --tags --exact-match
   ```
   Then re-run the step-1 quartet: require one healthy PATH candidate and matching
   executing/Homebrew/latest versions.

5. **Plugin installs** (instead of local symlinks) have their own updater — Claude Code
   `/plugin marketplace update`, Codex `codex plugin marketplace upgrade`.

6. Tell the user that an already-running agent session may need a fresh session to reload changed skill
   text.

## Headless / cloud agents

Follow the canonical [cloud-agent setup](https://github.com/replypen/replypen-cli#headless-cloud-agents)
— least-privilege project-token mint, `.replypen.toml` marker, verification, installer choice. **Never
paste the token into a repo or a command argument.** A headless agent may commit `machine_token_env`
beside `project` in `.replypen.toml` so `rp` seeds that profile from the named variable; one personal
environment may carry several project-named variables, but separate environments stay the security
boundary.

In cloud sandboxes GitHub release assets return 403; `rp self update` falls back to the release mirror
automatically and fresh sandboxes bootstrap with the hosted `install/cloud.sh`
([docs/rp-cli.md § Upgrade](../../docs/rp-cli.md)).

For Claude cloud egress set **Custom** to the ReplyPen API host (`app.replypen.com`) only and leave
Claude's default-domain checkbox checked — its default version-control list already covers the GitHub
installer and release hosts. Duplicating them is only correct if you deliberately disable the defaults.
