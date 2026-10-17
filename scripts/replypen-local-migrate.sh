#!/usr/bin/env bash
# ReplyPen local migration: ~/code/rootcause-org/* → ~/code/replypen/* (+ git remotes, mise env, kit dir).
# Idempotent and safe to re-run. Dry run by default; pass --apply to change anything.
# Never touches working trees: only renames directories (git work is preserved byte-for-byte),
# rewrites `origin` URLs, and leaves a symlink at every old path so running tools keep working.
# Works on macOS and Linux/WSL (bash ≥3.2, git).
set -euo pipefail

APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
CODE="${REPLYPEN_CODE_ROOT:-$HOME/code}"
OLD="$CODE/rootcause-org"; NEW="$CODE/replypen"
MISE_OLD="$HOME/.config/mise-env/rootcause-org"; MISE_NEW="$HOME/.config/mise-env/replypen"

run() { if [ $APPLY = 1 ]; then echo "+ $*"; "$@"; else echo "would: $*"; fi; }
note() { echo "== $*"; }

# Repo/dir name mapping (GitHub renamed rootcause-* → replypen-*; archived predecessors → *-legacy).
newname() {
  case "$1" in
    replypen) echo replypen-legacy-host ;;          # archived old host clone (NOT the current host)
    replypen-cli) echo replypen-cli-legacy ;;       # archived old rp CLI clone
    homebrew-replypen) echo homebrew-replypen-legacy ;;
    rootcause) echo replypen ;;
    rootcause-*) echo "replypen-${1#rootcause-}" ;;
    *) echo "$1" ;;
  esac
}

# A dir named replypen / replypen-cli that already tracks the CURRENT repo must not be treated as legacy.
is_legacy_clone() {
  local url; url=$(git -C "$1" remote get-url origin 2>/dev/null || true)
  case "$(basename "$1")" in
    # Root commits of the CURRENT repos; a clone that has them is not the archived predecessor.
    replypen) git -C "$1" cat-file -e e3f34fd69064da2b1acd5b44815cc366d1436904^{commit} 2>/dev/null && return 1 || return 0 ;;
    replypen-cli) git -C "$1" cat-file -e 303b8b1b49157f5b5fbb853666cd597a479f4937^{commit} 2>/dev/null && return 1 || return 0 ;;
    *) return 0 ;;
  esac
}

# 1. Top-level folder: move once, leave a symlink.
if [ -d "$OLD" ] && [ ! -L "$OLD" ]; then
  if [ -e "$NEW" ]; then echo "STOP: both $OLD and $NEW exist as real dirs; merge by hand"; exit 1; fi
  note "move $OLD → $NEW"
  run mv "$OLD" "$NEW"
  run ln -s replypen "$OLD"
fi
BASE="$NEW"; [ $APPLY = 0 ] && [ -d "$OLD" ] && [ ! -L "$OLD" ] && BASE="$OLD"

# 2. Per-repo dirs: legacy collisions first, then rootcause-* → replypen-*.
for pass in legacy rename; do
  for d in "$BASE"/*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    name=$(basename "$d"); target=$(newname "$name")
    [ "$target" = "$name" ] && continue
    case "$pass:$name" in
      legacy:replypen|legacy:replypen-cli|legacy:homebrew-replypen) is_legacy_clone "$d" || continue ;;
      legacy:*) continue ;;
      rename:replypen|rename:replypen-cli|rename:homebrew-replypen) continue ;;
    esac
    if [ -e "$BASE/$target" ]; then echo "SKIP $name: $target already exists"; continue; fi
    run mv "$d" "$BASE/$target"
    run ln -s "$target" "$d"
  done
done

# 3. Git remotes → github.com/replypen/<new name> (keeps ssh vs https), worktree repair.
for d in "$BASE"/*; do
  [ -d "$d/.git" ] || [ -f "$d/.git" ] || continue
  [ -L "$d" ] && continue
  url=$(git -C "$d" remote get-url origin 2>/dev/null || true)
  case "$url" in *github.com[:/]rootcause-org/*) ;; *) continue ;; esac
  repo=${url##*/}; repo=${repo%.git}
  case "$repo" in
    replypen|replypen-cli|homebrew-replypen) [ "$(basename "$d")" != "$repo" ] && repo=$(basename "$d") ;;
    rootcause|rootcause-*) repo=$(newname "$repo") ;;
  esac
  new="${url%%rootcause-org/*}replypen/$repo.git"
  [ "$url" = "$new" ] || run git -C "$d" remote set-url origin "$new"
  [ $APPLY = 1 ] && git -C "$d" worktree repair >/dev/null 2>&1 || true
done

# 4. mise env files: rootcause-org/<repo>.env → replypen/<new repo>.env, old names kept as symlinks.
if [ -d "$MISE_OLD" ] && [ ! -L "$MISE_OLD" ]; then
  if [ -e "$MISE_NEW" ]; then echo "STOP: both $MISE_OLD and $MISE_NEW exist; merge by hand"; exit 1; fi
  run mv "$MISE_OLD" "$MISE_NEW"
  run ln -s replypen "$MISE_OLD"
fi
MBASE="$MISE_NEW"; [ $APPLY = 0 ] && [ -d "$MISE_OLD" ] && [ ! -L "$MISE_OLD" ] && MBASE="$MISE_OLD"
if [ -d "$MBASE" ]; then
  # replypen.env belonged to the archived predecessor; move it aside before rootcause.env takes the name.
  if [ -f "$MBASE/replypen.env" ] && [ ! -L "$MBASE/replypen.env" ] && [ -f "$MBASE/rootcause.env" ] && [ ! -L "$MBASE/rootcause.env" ]; then
    run mv "$MBASE/replypen.env" "$MBASE/replypen-legacy-host.env"
  fi
  for f in "$MBASE"/rootcause.env "$MBASE"/rootcause-*.env; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    b=$(basename "$f" .env); t="$(newname "$b").env"
    [ -e "$MBASE/$t" ] && { echo "SKIP $f: $t exists"; continue; }
    run mv "$f" "$MBASE/$t"; run ln -s "$t" "$f"
  done
  if [ $APPLY = 1 ] && [ -f "$MBASE/replypen.env" ]; then  # absolute self-references inside env files
    tmp=$(mktemp); sed 's#/mise-env/rootcause-org/#/mise-env/replypen/#g' "$MBASE/replypen.env" > "$tmp" && cat "$tmp" > "$MBASE/replypen.env"; rm -f "$tmp"
  fi
fi

# 5. Brain kit clone (the new kit installer does the same; harmless if already done).
if [ -d "$HOME/.rootcause-brain-skills" ] && [ ! -L "$HOME/.rootcause-brain-skills" ] && [ ! -e "$HOME/.replypen-brain-skills" ]; then
  run mv "$HOME/.rootcause-brain-skills" "$HOME/.replypen-brain-skills"
  run ln -s .replypen-brain-skills "$HOME/.rootcause-brain-skills"
fi
if [ -d "$HOME/.replypen-brain-skills/.git" ]; then
  url=$(git -C "$HOME/.replypen-brain-skills" remote get-url origin 2>/dev/null || true)
  case "$url" in *rootcause-org/*) run git -C "$HOME/.replypen-brain-skills" remote set-url origin "${url%%rootcause-org/*}replypen/replypen-brain-skills.git" ;; esac
fi

# 6. mise trusts by path: re-trust every moved repo that has a mise.toml.
if command -v mise >/dev/null 2>&1; then
  for d in "$BASE"/*; do
    [ -d "$d" ] && [ ! -L "$d" ] && [ -f "$d/mise.toml" ] || continue
    run mise trust --quiet "$d/mise.toml"
  done
fi

[ $APPLY = 1 ] && echo "done." || echo "dry run only; re-run with --apply"
