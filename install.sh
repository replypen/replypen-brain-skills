#!/usr/bin/env bash
# Install the Brain Dev kit LOCALLY into a brain repo — the kit content is gitignored.
#
# Why the kit content stays untracked:
#   - Prod materializes a filtered run view of the committed brain (rootcause internal/brain/visible.go)
#     and the grounding agent rg/find/ls's the whole tree. Committed harness = run-time pollution; an
#     untracked one can never reach /brain.
#   - Committing the kit into each brain also re-creates the multi-copy skill drift this repo kills.
#
# Model: ONE pinned clone on disk, SYMLINKED into each brain's locally ignored `.agents/skills/`
# discovery tree (Codex is canonical). Claude Code discovers only `.claude/skills`, so each brain
# COMMITS one relative alias `.claude/skills -> ../.agents/skills`. That alias is the single tracked
# kit path: a `git worktree` carries tracked files only, so without it every brain skill is "Unknown
# command" in worktrees. Its target is usually an ignored local install, so the link dangles in a fresh
# checkout — prod skips dangling relative in-repo symlinks when building the run view, and brain lint
# only WARNs. `.worktreeinclude` additionally carries the ignored kit paths into managed worktrees.
# Do not also install this kit as a user/global Claude Code or Codex plugin; that creates a second,
# project-agnostic discovery path and makes Brain Dev appear in unrelated repos.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh) [BRAIN_DIR]
#   RC_BRAIN_KIT=~/src/kit RC_BRAIN_KIT_TAG=v0.11.24 ./install.sh ~/code/rootcause-org/rootcause-brain-foo
#   ./install.sh --latest-version
set -euo pipefail

KIT="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}"
TAG="${RC_BRAIN_KIT_TAG:-v0.11.24}"
REPO="https://github.com/rootcause-org/rootcause-brain-skills"
LATEST_TAG_ENDPOINT="https://api.github.com/repos/rootcause-org/rootcause-brain-skills/git/matching-refs/tags/v"
KIT_OVERRIDE="${RC_BRAIN_KIT+x}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
usage: install.sh [--tag vX.Y.Z] [BRAIN_DIR]
       install.sh --latest-version

No BRAIN_DIR: auto-detect the current brain checkout from \$PWD or its parents.
Outside a brain checkout, pass BRAIN_DIR explicitly.
EOF
}

latest_tag() {
  ENDPOINT="$LATEST_TAG_ENDPOINT" python3 - <<'PY'
import json
import os
import re
import sys
import urllib.request

url = os.environ["ENDPOINT"]
try:
    with urllib.request.urlopen(url, timeout=10) as response:
        refs = json.load(response)
except Exception as exc:
    print(f"error: could not fetch {url}: {exc}", file=sys.stderr)
    sys.exit(1)

tags = []
for item in refs:
    tag = item.get("ref", "").rsplit("/", 1)[-1]
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        tags.append(tag)

if not tags:
    print(f"error: no semver tags found at {url}", file=sys.stderr)
    sys.exit(1)

def version_key(tag):
    return tuple(int(part) for part in tag[1:].split("."))

print(max(tags, key=version_key))
PY
}

BRAIN_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --latest-version|--print-latest-version)
      latest_tag
      exit 0
      ;;
    --tag)
      TAG="${2:?--tag needs vX.Y.Z}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown flag: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [ -n "$BRAIN_ARG" ]; then
        echo "error: expected at most one BRAIN_DIR" >&2
        usage >&2
        exit 1
      fi
      BRAIN_ARG="$1"
      shift
      ;;
  esac
done

is_brain_dir() {
  local dir="$1"
  [ -d "$dir/skills" ] || [ -d "$dir/playbooks" ] || [ -f "$dir/projection.yaml" ] || [ -f "$dir/.rootcause.toml" ]
}

find_brain_dir() {
  local dir
  dir="$(cd "${1:-$PWD}" && pwd)"
  while :; do
    if is_brain_dir "$dir"; then
      echo "$dir"
      return 0
    fi
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
  done
}

if [ -n "$BRAIN_ARG" ]; then
  BRAIN="$(cd "$BRAIN_ARG" && pwd)"
else
  BRAIN="$(find_brain_dir "$PWD" || true)"
fi

if [ -z "${BRAIN:-}" ]; then
  echo "error: not inside a brain checkout; pass BRAIN_DIR explicitly" >&2
  usage >&2
  exit 1
fi

# Sanity-check this is a brain checkout. Accept all layouts: legacy (skills/), the projection-based
# PROJECT layout (playbooks/ + projection.yaml), and a nested TENANT brain — which holds only a free-form
# NL delta + sealed .env now (its values live in the rootcause DB record, no tenant.json), so its marker
# is the committed .rootcause.toml (project∪tenant binding).
is_brain_dir "$BRAIN" || {
  echo "error: $BRAIN has no skills/ or playbooks/ or projection.yaml or .rootcause.toml — not a brain checkout?" >&2; exit 1; }

EXCLUDE="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null)" || {
  echo "error: $BRAIN is not a Git checkout" >&2
  exit 1
}
BRAIN_PREFIX="$(git -C "$BRAIN" rev-parse --show-prefix)"
IGNORE_ROOT="/$BRAIN_PREFIX"

# 1. One pinned clone on disk (shared by every brain). Pin the shared clone to a tag, never float main.
#    If RC_BRAIN_KIT points at the checkout running this install.sh, treat it as a developer/local
#    override and do not mutate it.
if [ -n "$KIT_OVERRIDE" ] && [ -d "$KIT" ] && [ "$(cd "$KIT" && pwd)" = "$SCRIPT_DIR" ]; then
  [ -d "$KIT/skills" ] || { echo "error: RC_BRAIN_KIT=$KIT has no skills/" >&2; exit 1; }
  echo "kit: using RC_BRAIN_KIT=$KIT"
elif [ -d "$KIT/.git" ]; then
  echo "kit: updating $KIT -> $TAG"
  if ! git -C "$KIT" fetch -q --tags origin; then
    if git -C "$KIT" rev-parse -q --verify "$TAG^{commit}" >/dev/null; then
      echo "  (warning: fetch failed; using already-local $TAG)" >&2
    else
      echo "error: could not fetch tags and $TAG is not available locally" >&2
      exit 1
    fi
  fi
  git -C "$KIT" checkout -q "$TAG" || {
    echo "error: tag $TAG not found in $KIT" >&2
    exit 1
  }
  INSTALLED_TAG="$(git -C "$KIT" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$INSTALLED_TAG" != "$TAG" ]; then
    echo "error: expected $KIT to be at $TAG, got ${INSTALLED_TAG:-non-tag checkout}" >&2
    exit 1
  fi
elif [ -d "$KIT/skills" ]; then
  echo "kit: using existing non-git kit at $KIT"
else
  echo "kit: cloning $REPO@$TAG -> $KIT"
  git clone -q "$REPO" "$KIT"
  git -C "$KIT" checkout -q "$TAG" || {
    echo "error: tag $TAG not found in $KIT" >&2
    exit 1
  }
  INSTALLED_TAG="$(git -C "$KIT" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$INSTALLED_TAG" != "$TAG" ]; then
    echo "error: expected $KIT to be at $TAG, got ${INSTALLED_TAG:-non-tag checkout}" >&2
    exit 1
  fi
fi

# 2. Remove retired installer symlinks. A name alone does not prove ownership: preserve links whose
#    target is outside this kit, plus all real user files and directories.
is_kit_link() {
  local path="$1"
  local target
  [ -L "$path" ] || return 1
  target="$(readlink "$path")"
  case "$target" in
    "$KIT"/skills/*|"$KIT"/commands/*) return 0 ;;
    *) return 1 ;;
  esac
}

for old in \
  "$BRAIN/.claude/commands/brain-dev.md" \
  "$BRAIN/.agents/skills/brain-dev" \
  "$BRAIN/.claude/skills/brain-dev" \
  "$BRAIN/.claude/commands/brain-debug.md" \
  "$BRAIN/.agents/skills/brain-debug" \
  "$BRAIN/.claude/skills/brain-debug" \
  "$BRAIN/.agents/skills/observability" \
  "$BRAIN/.claude/skills/observability" \
  "$BRAIN/.agents/skills/rc-inspect" \
  "$BRAIN/.claude/skills/rc-inspect" \
  "$BRAIN/.agents/skills/rc-thread" \
  "$BRAIN/.claude/skills/rc-thread" \
  "$BRAIN/.agents/skills/rc-run" \
  "$BRAIN/.claude/skills/rc-run" \
  "$BRAIN/.agents/skills/brain-source-intake" \
  "$BRAIN/.claude/skills/brain-source-intake" \
  "$BRAIN/.agents/skills/brain-feedback-review" \
  "$BRAIN/.claude/skills/brain-feedback-review" \
  "$BRAIN/.agents/skills/brain-schema-intake" \
  "$BRAIN/.claude/skills/brain-schema-intake"
do
  is_kit_link "$old" && rm "$old"
done

# 3. Codex-first discovery. `.agents/skills/` is the one canonical per-brain tree. Claude Code uses
#    `.claude/skills -> ../.agents/skills` when possible; that relative alias is meant to be COMMITTED
#    (worktrees only carry tracked files), while the kit skills it exposes stay locally ignored — so it
#    dangles in a fresh checkout, which prod skips and brain lint only warns about. An existing
#    compatibility directory is collapsed to the alias only when it contains installer-owned links and
#    nothing user-owned.
if [ -L "$BRAIN/.agents/skills" ]; then
  echo "error: $BRAIN/.agents/skills is a symlink; refusing to replace the canonical Codex tree" >&2
  exit 1
fi
mkdir -p "$BRAIN/.agents/skills" "$BRAIN/.claude"

preflight_link() {
  local src="$1"
  local dst="$2"
  if [ -L "$dst" ] && [ "$(readlink "$dst")" != "$src" ]; then
    echo "error: $dst is a user-managed symlink; refusing to overwrite it" >&2
    exit 1
  elif [ ! -L "$dst" ] && [ -e "$dst" ]; then
    echo "error: $dst exists and is not an installer symlink; refusing to overwrite user content" >&2
    exit 1
  fi
}

link_managed() {
  local src="$1"
  local dst="$2"
  preflight_link "$src" "$dst"
  [ -L "$dst" ] && rm "$dst"
  ln -s "$src" "$dst"
}

# Shared reference docs, linked once per brain. Skills cross-reference them as `../../docs/*.md`
# (templates: `../../../docs/*`), which only resolves if `docs` sits beside the skills tree — and
# `.claude/skills/<skill>/../../docs` lands in `.claude/`, so that side needs its own alias.
DOCS_SRC="$KIT/docs"
DOCS_DST="$BRAIN/.agents/docs"
DOCS_CLAUDE_ALIAS="../.agents/docs"
DOCS_CLAUDE_DST="$BRAIN/.claude/docs"

# Classify the Claude compatibility path without changing it. A directory containing only links made
# by the previous installer can migrate to the alias. Any unrelated entry keeps directory fallback.
CLAUDE_SKILLS="$BRAIN/.claude/skills"
CLAUDE_MODE="alias"
CLAUDE_MIGRATE=0
if [ -L "$CLAUDE_SKILLS" ]; then
  if [ "$(readlink "$CLAUDE_SKILLS")" != "../.agents/skills" ]; then
    echo "error: $CLAUDE_SKILLS is a user-managed symlink; refusing to overwrite it" >&2
    exit 1
  fi
elif [ -e "$CLAUDE_SKILLS" ] && [ ! -d "$CLAUDE_SKILLS" ]; then
  echo "error: $CLAUDE_SKILLS exists and is not a directory; refusing to overwrite user content" >&2
  exit 1
elif [ -d "$CLAUDE_SKILLS" ]; then
  CLAUDE_MIGRATE=1
  while IFS= read -r -d '' entry; do
    name="$(basename "$entry")"
    src="$KIT/skills/$name"
    if [ ! -L "$entry" ] || [ ! -f "$src/SKILL.md" ] || [ "$(readlink "$entry")" != "$src" ]; then
      CLAUDE_MODE="directory"
      CLAUDE_MIGRATE=0
      break
    fi
  done < <(find "$CLAUDE_SKILLS" -mindepth 1 -maxdepth 1 -print0)
fi

# Preflight every shipped-name destination before changing current links.
for src in "$KIT"/skills/*; do
  [ -d "$src" ] || continue
  [ -f "$src/SKILL.md" ] || continue
  preflight_link "$src" "$BRAIN/.agents/skills/$(basename "$src")"
done
if [ -d "$DOCS_SRC" ]; then
  preflight_link "$DOCS_SRC" "$DOCS_DST"
  preflight_link "$DOCS_CLAUDE_ALIAS" "$DOCS_CLAUDE_DST"
fi

if [ "$CLAUDE_MODE" = "directory" ]; then
  for src in "$KIT"/skills/*; do
    [ -d "$src" ] || continue
    [ -f "$src/SKILL.md" ] || continue
    preflight_link "$src" "$CLAUDE_SKILLS/$(basename "$src")"
  done
fi

for src in "$KIT"/skills/*; do
  [ -d "$src" ] || continue
  [ -f "$src/SKILL.md" ] || continue
  link_managed "$src" "$BRAIN/.agents/skills/$(basename "$src")"
done
if [ -d "$DOCS_SRC" ]; then
  link_managed "$DOCS_SRC" "$DOCS_DST"
  link_managed "$DOCS_CLAUDE_ALIAS" "$DOCS_CLAUDE_DST"
fi

if [ "$CLAUDE_MODE" = "alias" ]; then
  if [ "$CLAUDE_MIGRATE" = 1 ]; then
    # Remove only links verified above as belonging to this kit, then collapse the empty directory.
    for src in "$KIT"/skills/*; do
      [ -d "$src" ] || continue
      [ -f "$src/SKILL.md" ] || continue
      dst="$CLAUDE_SKILLS/$(basename "$src")"
      [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ] && rm "$dst"
    done
    rmdir "$CLAUDE_SKILLS"
  fi
  [ -L "$CLAUDE_SKILLS" ] || ln -s ../.agents/skills "$CLAUDE_SKILLS"
else
  # Preserve unrelated Claude user content. Mirror shipped skills only inside this fallback directory.
  for src in "$KIT"/skills/*; do
    [ -d "$src" ] || continue
    [ -f "$src/SKILL.md" ] || continue
    link_managed "$src" "$CLAUDE_SKILLS/$(basename "$src")"
  done
fi

# 4. Local-only ignore rules (idempotent). Never dirty a brain's tracked `.gitignore` just to install
#    developer tooling. Replace only this installer's marked block, preserving every user rule.
#    `.agents/skills/` is listed per SHIPPED NAME, not wholesale: a brain may author and commit its own
#    skills in that tree. `.claude/skills` is never ignored — the alias is committed, and in fallback
#    (directory) mode it holds user content with only the per-name links inside it ignored.
mkdir -p "$(dirname "$EXCLUDE")"
touch "$EXCLUDE"
BRAIN_ID="${BRAIN_PREFIX%/}"
[ -n "$BRAIN_ID" ] || BRAIN_ID="."
EXCLUDE_BEGIN="# rootcause-brain-skills: begin managed local install ($BRAIN_ID)"
EXCLUDE_END="# rootcause-brain-skills: end managed local install ($BRAIN_ID)"
EXCLUDE_TMP="$(mktemp "${EXCLUDE}.tmp.XXXXXX")"
if ! awk -v begin="$EXCLUDE_BEGIN" -v end="$EXCLUDE_END" '
  $0 == begin { managed = 1; next }
  managed && $0 == end { managed = 0; next }
  !managed { print }
  END { if (managed) exit 2 }
' "$EXCLUDE" >"$EXCLUDE_TMP"; then
  rm "$EXCLUDE_TMP"
  echo "error: malformed managed block in $EXCLUDE" >&2
  exit 1
fi
mv "$EXCLUDE_TMP" "$EXCLUDE"
{
  echo "$EXCLUDE_BEGIN"
  echo "${IGNORE_ROOT}.rootcause/"
  for src in "$KIT"/skills/*; do
    [ -d "$src" ] || continue
    [ -f "$src/SKILL.md" ] || continue
    name="$(basename "$src")"
    echo "${IGNORE_ROOT}.agents/skills/$name"
    if [ "$CLAUDE_MODE" = "directory" ]; then
      echo "${IGNORE_ROOT}.claude/skills/$name"
    fi
  done
  if [ -d "$DOCS_SRC" ]; then
    echo "${IGNORE_ROOT}.agents/docs"
    echo "${IGNORE_ROOT}.claude/docs"
  fi
  echo "$EXCLUDE_END"
} >>"$EXCLUDE"

# 4b. Managed pre-commit hook: brain_hygiene.py --staged (conflict markers, projection `{{ }}`,
#     `/Users/` paths, dead links, customer-copy em dashes, mermaid) fails at commit time instead of
#     at push. Never replaces a user-owned hook or a core.hooksPath setup; says so instead.
HOOK_MARK="# rootcause-brain-skills managed pre-commit hook"
HOOKS_PATH="$(git -C "$BRAIN" config core.hooksPath || true)"
HOOK="$(git -C "$BRAIN" rev-parse --path-format=absolute --git-path hooks/pre-commit)"
if [ -n "$BRAIN_PREFIX" ]; then
  echo "pre-commit hook: skipped (brain is a subdirectory of its repo)"
elif [ -n "$HOOKS_PATH" ] || { [ -e "$HOOK" ] && ! grep -qF "$HOOK_MARK" "$HOOK"; }; then
  echo "pre-commit hook: kept your own hook; add this line to it for early hygiene feedback:"
  echo "  python3 \"$KIT/skills/local-brain-work/scripts/brain_hygiene.py\" --staged"
else
  mkdir -p "$(dirname "$HOOK")"
  cat >"$HOOK" <<HOOK_EOF
#!/bin/sh
$HOOK_MARK (install.sh; rewritten on every kit refresh).
# Bypass once: git commit --no-verify
GATE="$KIT/skills/local-brain-work/scripts/brain_hygiene.py"
[ -f "\$GATE" ] || { echo "brain hygiene: kit missing at \$GATE, skipped" >&2; exit 0; }
# An interpreter that cannot start (e.g. a mise shim in an untrusted checkout) must not block commits.
if python3 -c '' 2>/dev/null; then exec python3 "\$GATE" --staged; fi
if uv run --no-project python -c '' 2>/dev/null; then exec uv run --no-project python "\$GATE" --staged; fi
echo "brain hygiene: no working python3/uv here (untrusted mise.toml?), skipped; the push gate still runs" >&2
HOOK_EOF
  chmod +x "$HOOK"
fi

# 5. Agent worktrees carry TRACKED files only; the alias is, the kit content behind it is not. The repo-root
#    `.worktreeinclude` allowlist is the operator convention for ignored local files an agent should
#    copy into a new managed worktree; keep the kit's discovery paths in it. Written but never
#    committed by this installer.
WTI="$BRAIN/.worktreeinclude"
WTI_CHANGED=0
for path in .agents/skills .agents/docs .claude/skills .claude/docs; do
  if [ ! -f "$WTI" ]; then
    {
      echo "# Local operator files an agent should copy into new managed worktrees."
      echo "# Keep this allowlist tight: no caches, build output, logs, or generated artifacts."
      echo
      echo "# Brain Dev kit discovery paths (ignored symlinks into ~/.rootcause-brain-skills)."
    } >"$WTI"
    WTI_CHANGED=1
  fi
  grep -qxF "$path" "$WTI" || { echo "$path" >>"$WTI"; WTI_CHANGED=1; }
done

# 6. Loud audit: a TRACKED kit *content* path ships developer tooling into /brain. Only the
#    `.claude/skills` alias belongs in git.
warn_tracked() {
  local path="$1"
  echo >&2
  echo "  ⚠  WARNING: $path is TRACKED in $BRAIN" >&2
  echo "     Prod materializes it into the run view as brain content." >&2
  echo "     Fix it yourself (this installer never rewrites your git state):" >&2
  echo "       git -C $BRAIN rm --cached -r --quiet $path && git -C $BRAIN commit -m 'chore: untrack local brain-dev kit path' && git -C $BRAIN push" >&2
}
for path in .claude/docs .agents/docs; do
  git -C "$BRAIN" ls-files --error-unmatch -- "$path" >/dev/null 2>&1 || continue
  warn_tracked "$path"
done

echo
echo "installed skills (gitignored):"
for src in "$KIT"/skills/*; do
  [ -d "$src" ] || continue
  [ -f "$src/SKILL.md" ] || continue
  echo "  $(basename "$src")"
done
echo "The engine ships inside the Local Brain Work skill:"
echo "  SKILL=$KIT/skills/local-brain-work"
echo "  uv run \"\$SKILL/scripts/brain_run.py\" --brief"
echo "  uv run \"\$SKILL/scripts/brain_test.py\" --live"
echo "Codex auto-discovers the canonical .agents/skills tree."
if [ "$CLAUDE_MODE" = "alias" ]; then
  echo "Claude Code compatibility: .claude/skills -> ../.agents/skills"
  if ! git -C "$BRAIN" ls-files --error-unmatch .claude/skills >/dev/null 2>&1; then
    echo "  commit that alias so git worktrees see the skills: git add .claude/skills && git commit"
  fi
else
  echo "Claude Code compatibility: preserved user-owned .claude/skills directory"
fi
if [ "$WTI_CHANGED" = 1 ]; then
  echo "Updated $WTI (worktree allowlist) — review and commit it with your own brain changes."
fi
echo "Do not also install Brain Dev as a user/global plugin; keep discovery repo-local."
