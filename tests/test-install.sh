#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

KIT="$TMP/kit"
mkdir -p "$KIT/skills/alpha" "$KIT/skills/beta"
printf '%s\n' '---' 'name: alpha' 'description: Alpha.' '---' >"$KIT/skills/alpha/SKILL.md"
printf '%s\n' '---' 'name: beta' 'description: Beta.' '---' >"$KIT/skills/beta/SKILL.md"
mkdir -p "$KIT/docs"
printf '%s\n' '# shared doc' >"$KIT/docs/brain-model.md"

new_brain() {
  local name="$1"
  BRAIN="$TMP/$name"
  mkdir -p "$BRAIN"
  git init -q -b main "$BRAIN"
  git -C "$BRAIN" config user.name "Installer Test"
  git -C "$BRAIN" config user.email "installer-test@example.com"
  printf '%s\n' 'project = "fixture"' >"$BRAIN/.replypen.toml"
  printf '%s\n' '# tracked rules stay unchanged' >"$BRAIN/.gitignore"
  git -C "$BRAIN" add .replypen.toml .gitignore
  git -C "$BRAIN" commit -q -m initial
}

run_install() {
  RC_BRAIN_KIT="$KIT" "$ROOT/install.sh" "$BRAIN"
}

# The kit content stays ignored, but the `.claude/skills` alias is meant to be COMMITTED: Claude Code
# discovers only that path and a git worktree carries tracked files only. So it must NOT be ignored.
# `.worktreeinclude` (the allowlist that carries the ignored kit paths into agent worktrees) is written
# too, so both show up untracked after a fresh install.
assert_clean_local_install() {
  test "$(git -C "$BRAIN" status --short --untracked-files=all)" = '?? .claude/skills
?? .worktreeinclude'
  test "$(cat "$BRAIN/.gitignore")" = "# tracked rules stay unchanged"
  local exclude
  exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
  grep -qxF '/.replypen/' "$exclude"
  grep -qxF '/.agents/skills/alpha' "$exclude"
  grep -qxF '/.agents/skills/beta' "$exclude"
  grep -qxF '/.agents/docs' "$exclude"
  grep -qxF '/.claude/docs' "$exclude"
  if grep -qxF '/.claude/skills' "$exclude"; then
    echo "error: committable .claude/skills alias was ignored" >&2
    exit 1
  fi
  if git -C "$BRAIN" check-ignore -q .claude/skills; then
    echo "error: .claude/skills is ignored; it cannot be committed for worktrees" >&2
    exit 1
  fi
}

assert_worktreeinclude() {
  local wti="$BRAIN/.worktreeinclude"
  for path in .agents/skills .agents/docs .claude/skills .claude/docs; do
    test "$(grep -cxF "$path" "$wti")" = 1
  done
}

# A skill's `../../docs/x.md` cross-reference resolves LOGICALLY (as a markdown reader walks it), so
# assert the paths those links name in each tree — not the physical ones a symlink hop would reach.
assert_docs_links_resolve() {
  test "$(readlink "$BRAIN/.agents/docs")" = "$KIT/docs"
  test "$(readlink "$BRAIN/.claude/docs")" = '../.agents/docs'
  test -f "$BRAIN/.agents/docs/brain-model.md"
  test -f "$BRAIN/.claude/docs/brain-model.md"
}

# Clean install: one Codex-native tree and one relative Claude compatibility alias.
new_brain clean
run_install >"$TMP/clean.out"
test "$(readlink "$BRAIN/.agents/skills/alpha")" = "$KIT/skills/alpha"
test "$(readlink "$BRAIN/.agents/skills/beta")" = "$KIT/skills/beta"
test "$(readlink "$BRAIN/.claude/skills")" = '../.agents/skills'
grep -q 'git add .claude/skills' "$TMP/clean.out"
assert_clean_local_install
assert_docs_links_resolve
assert_worktreeinclude

# Once the alias is committed, the installer stops nagging about it, and a dangling (ignored) target is
# fine: prod skips such links and brain lint only warns.
git -C "$BRAIN" add .claude/skills
git -C "$BRAIN" commit -q -m 'commit claude alias'
run_install >"$TMP/committed.out" 2>"$TMP/committed.err"
if grep -q 'git add .claude/skills' "$TMP/committed.out"; then
  echo "error: installer still asked to add an already-tracked alias" >&2
  exit 1
fi
if grep -q 'is TRACKED in' "$TMP/committed.err"; then
  echo "error: installer warned about the supported committed alias" >&2
  exit 1
fi
test "$(git -C "$BRAIN" status --short --untracked-files=all)" = '?? .worktreeinclude'
git -C "$BRAIN" rm -q --cached .claude/skills
git -C "$BRAIN" commit -q -m 'uncommit claude alias'

# Idempotence keeps the same topology and does not duplicate local excludes.
run_install >"$TMP/idempotent.out"
test "$(readlink "$BRAIN/.claude/skills")" = '../.agents/skills'
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
test "$(grep -c '^/.agents/skills/alpha$' "$exclude")" = 1
test "$(grep -c '^/.agents/docs$' "$exclude")" = 1
assert_clean_local_install
assert_docs_links_resolve

# Previous installer-created per-skill Claude links migrate to the single alias.
new_brain migrate
mkdir -p "$BRAIN/.claude/skills"
ln -s "$KIT/skills/alpha" "$BRAIN/.claude/skills/alpha"
ln -s "$KIT/skills/beta" "$BRAIN/.claude/skills/beta"
run_install >"$TMP/migrate.out"
test "$(readlink "$BRAIN/.claude/skills")" = '../.agents/skills'
assert_clean_local_install

# Unrelated Claude content prevents aliasing but is preserved beside compatibility links.
new_brain fallback
mkdir -p "$BRAIN/.claude/skills/custom"
printf '%s\n' 'user-owned' >"$BRAIN/.claude/skills/custom/SKILL.md"
ln -s "$KIT/skills/alpha" "$BRAIN/.claude/skills/alpha"
run_install >"$TMP/fallback.out"
test -d "$BRAIN/.claude/skills"
test ! -L "$BRAIN/.claude/skills"
test "$(cat "$BRAIN/.claude/skills/custom/SKILL.md")" = 'user-owned'
test "$(readlink "$BRAIN/.claude/skills/alpha")" = "$KIT/skills/alpha"
grep -q 'preserved user-owned .claude/skills directory' "$TMP/fallback.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
grep -qxF '/.agents/skills/alpha' "$exclude"
grep -qxF '/.claude/skills/alpha' "$exclude"
assert_docs_links_resolve
if grep -qxF '/.claude/skills' "$exclude"; then
  echo "error: fallback directory was ignored wholesale" >&2
  exit 1
fi
git -C "$BRAIN" check-ignore -q .agents/skills/alpha
git -C "$BRAIN" check-ignore -q .claude/skills/alpha
if git -C "$BRAIN" check-ignore -q .claude/skills/custom/SKILL.md; then
  echo "error: installer hid unrelated Claude user content" >&2
  exit 1
fi
test "$(cat "$BRAIN/.gitignore")" = "# tracked rules stay unchanged"

# A shipped-name collision is user content: fail closed and preserve it.
new_brain refusal
mkdir -p "$BRAIN/.agents/skills/alpha"
printf '%s\n' 'do not overwrite' >"$BRAIN/.agents/skills/alpha/SKILL.md"
if run_install >"$TMP/refusal.out" 2>&1; then
  echo "error: installer overwrote a user-owned canonical skill" >&2
  exit 1
fi
grep -q 'refusing to overwrite user content' "$TMP/refusal.out"
test "$(cat "$BRAIN/.agents/skills/alpha/SKILL.md")" = 'do not overwrite'

# A user-owned .agents/docs is content too: refuse before any link is rewritten.
new_brain docs-refusal
mkdir -p "$BRAIN/.agents/docs"
printf '%s\n' 'user notes' >"$BRAIN/.agents/docs/notes.md"
if run_install >"$TMP/docs-refusal.out" 2>&1; then
  echo "error: installer overwrote a user-owned .agents/docs" >&2
  exit 1
fi
grep -q 'refusing to overwrite user content' "$TMP/docs-refusal.out"
test "$(cat "$BRAIN/.agents/docs/notes.md")" = 'user notes'
test ! -e "$BRAIN/.agents/skills/alpha"

# A foreign Claude symlink is also preserved and refused.
new_brain claude-refusal
mkdir -p "$BRAIN/.claude"
ln -s ../somewhere-else "$BRAIN/.claude/skills"
if run_install >"$TMP/claude-refusal.out" 2>&1; then
  echo "error: installer overwrote a user-managed Claude alias" >&2
  exit 1
fi
grep -q 'user-managed symlink' "$TMP/claude-refusal.out"
test "$(readlink "$BRAIN/.claude/skills")" = '../somewhere-else'

# Retired names are removed only when their target proves installer ownership.
new_brain retired-user-link
mkdir -p "$BRAIN/.agents/skills"
ln -s "$TMP/user-skill" "$BRAIN/.agents/skills/brain-debug"
run_install >"$TMP/retired-user-link.out"
test "$(readlink "$BRAIN/.agents/skills/brain-debug")" = "$TMP/user-skill"

# Retired questionnaire links disappear even after the kit directory was removed.
new_brain retired-feedback-review
mkdir -p "$BRAIN/.agents/skills" "$BRAIN/.claude/skills"
ln -s "$KIT/skills/brain-feedback-review" "$BRAIN/.agents/skills/brain-feedback-review"
ln -s "$KIT/skills/brain-feedback-review" "$BRAIN/.claude/skills/brain-feedback-review"
run_install >"$TMP/retired-feedback-review.out"
test ! -L "$BRAIN/.agents/skills/brain-feedback-review"
test ! -L "$BRAIN/.claude/skills/brain-feedback-review"

# Alias-to-fallback transitions replace the managed ignore block; new user content stays visible.
new_brain transition
run_install >"$TMP/transition-alias.out"
rm "$BRAIN/.claude/skills"
mkdir -p "$BRAIN/.claude/skills/custom"
printf '%s\n' 'new user content' >"$BRAIN/.claude/skills/custom/SKILL.md"
run_install >"$TMP/transition-fallback.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
if grep -qxF '/.claude/skills' "$exclude"; then
  echo "error: alias ignore survived transition to fallback directory" >&2
  exit 1
fi
if git -C "$BRAIN" check-ignore -q .claude/skills/custom/SKILL.md; then
  echo "error: alias-to-fallback transition hid user content" >&2
  exit 1
fi

# Root-anchored excludes include the brain prefix when the brain is nested in a larger checkout.
REPO="$TMP/nested"
BRAIN="$REPO/tenant"
mkdir -p "$BRAIN"
git init -q -b main "$REPO"
git -C "$REPO" config user.name "Installer Test"
git -C "$REPO" config user.email "installer-test@example.com"
printf '%s\n' 'project = "fixture"' >"$BRAIN/.replypen.toml"
git -C "$REPO" add tenant/.replypen.toml
git -C "$REPO" commit -q -m initial
run_install >"$TMP/nested.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
grep -qxF '/tenant/.agents/skills/alpha' "$exclude"
grep -qxF '/tenant/.agents/docs' "$exclude"
test "$(git -C "$REPO" status --short --untracked-files=all)" = '?? tenant/.claude/skills
?? tenant/.worktreeinclude'

# A second nested brain keeps the first brain's independently managed excludes.
FIRST_BRAIN="$BRAIN"
mkdir -p "$FIRST_BRAIN/.replypen" "$REPO/other"
printf '%s\n' 'sensitive local artifact' >"$FIRST_BRAIN/.replypen/dump"
printf '%s\n' 'project = "fixture"' >"$REPO/other/.replypen.toml"
git -C "$REPO" add other/.replypen.toml
git -C "$REPO" commit -q -m 'add second brain'
BRAIN="$REPO/other"
run_install >"$TMP/nested-other.out"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
grep -qxF '/tenant/.agents/skills/alpha' "$exclude"
grep -qxF '/other/.agents/skills/alpha' "$exclude"
git -C "$REPO" check-ignore -q tenant/.replypen/dump
git -C "$REPO" check-ignore -q tenant/.agents/skills/alpha
git -C "$REPO" check-ignore -q other/.agents/skills/alpha
test "$(git -C "$REPO" status --short --untracked-files=all)" = '?? other/.claude/skills
?? other/.worktreeinclude
?? tenant/.claude/skills
?? tenant/.worktreeinclude'

# Managed pre-commit hook: installed on a root brain, rewritten on refresh, never over a user hook.
new_brain hooked
run_install >/dev/null
hook="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path hooks/pre-commit)"
test -x "$hook"
grep -qF 'replypen-brain-skills managed pre-commit hook' "$hook"
grep -qF "$KIT/skills/local-brain-work/scripts/brain_hygiene.py" "$hook"
run_install >/dev/null
new_brain userhook
hook="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path hooks/pre-commit)"
printf '%s\n' '#!/bin/sh' 'echo mine' >"$hook"
run_install >"$TMP/userhook.out"
grep -qF 'kept your own hook' "$TMP/userhook.out"
test "$(sed -n 2p "$hook")" = 'echo mine'

# Pre-rebrand install: ~/.rootcause-brain-skills moves to ~/.replypen-brain-skills (old path kept as a
# link); links, exclude block and hook made by the old installer stay installer-owned; .rootcause.toml
# still marks a brain; retired rc-* skill links are removed.
FAKE_HOME="$TMP/home"
LEGACY="$FAKE_HOME/.rootcause-brain-skills"
mkdir -p "$FAKE_HOME"
cp -R "$KIT" "$LEGACY"
BRAIN="$TMP/legacy"
git init -q -b main "$BRAIN"
printf '%s\n' 'project = "fixture"' >"$BRAIN/.rootcause.toml"
git -C "$BRAIN" add .rootcause.toml
git -C "$BRAIN" -c user.name=t -c user.email=t@example.com commit -q -m initial
mkdir -p "$BRAIN/.agents/skills"
ln -s "$LEGACY/skills/alpha" "$BRAIN/.agents/skills/alpha"
ln -s "$LEGACY/skills/rc-debug" "$BRAIN/.agents/skills/rc-debug"
exclude="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude)"
printf '%s\n' '# rootcause-brain-skills: begin managed local install (.)' '/.rootcause/' \
  '/.agents/skills/rc-debug' '# rootcause-brain-skills: end managed local install (.)' >>"$exclude"
hook="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path hooks/pre-commit)"
mkdir -p "$(dirname "$hook")"
printf '%s\n' '#!/bin/sh' '# rootcause-brain-skills managed pre-commit hook (install.sh)' 'exit 0' >"$hook"
# First pass as a user (no RC_BRAIN_KIT), second as refresh-brains.sh (default path passed explicitly).
env -u RC_BRAIN_KIT HOME="$FAKE_HOME" "$ROOT/install.sh" "$BRAIN" >"$TMP/legacy.out"
HOME="$FAKE_HOME" RC_BRAIN_KIT="$FAKE_HOME/.replypen-brain-skills" "$ROOT/install.sh" "$BRAIN" >"$TMP/legacy2.out"
test -f "$FAKE_HOME/.replypen-brain-skills/skills/alpha/SKILL.md"
test "$(readlink "$LEGACY")" = "$FAKE_HOME/.replypen-brain-skills"
test "$(readlink "$BRAIN/.agents/skills/alpha")" = "$FAKE_HOME/.replypen-brain-skills/skills/alpha"
if [ -L "$BRAIN/.agents/skills/rc-debug" ]; then echo "error: retired rc-debug link survived" >&2; exit 1; fi
if grep -qF 'rootcause-brain-skills: begin' "$exclude"; then echo "error: legacy exclude block kept" >&2; exit 1; fi
grep -qxF '/.replypen/' "$exclude"
grep -qxF '/.rootcause/' "$exclude"
grep -qF 'replypen-brain-skills managed pre-commit hook' "$hook"

echo "install tests passed"
