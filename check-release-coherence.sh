#!/usr/bin/env bash
# Verify the kit's single version line and the optional sibling prod runtime pin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${RC_WORKSPACE_IMAGE_REPO:-ghcr.io/replypen/workspace}"
# Sibling host checkout: new name first, the pre-rebrand `rootcause` folder until a laptop is migrated.
HOST_DIR="${REPLYPEN_DIR:-${ROOTCAUSE_DIR:-}}"
if [ -z "$HOST_DIR" ]; then
  HOST_DIR="$ROOT/../replypen"
  [ -d "$HOST_DIR/runtime" ] || HOST_DIR="$ROOT/../rootcause"
fi
SKIP_IMAGE="${SKIP_IMAGE:-0}"
SKIP_PROD="${SKIP_PROD:-0}"

version="$(grep -E '^VERSION = ' "$ROOT/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"
tag="v$version"
fail=0

err() {
  echo "error: $*" >&2
  fail=1
}

require_hit() {
  local file="$1" needle="$2"
  if ! grep -qF "$needle" "$ROOT/$file"; then
    err "$file does not contain $needle"
  fi
}

require_hit "runtime/pyproject.toml" "version = \"$version\""
require_hit "runtime/pyproject.toml" "@$tag#subdirectory=runtime"
require_hit "install.sh" "RC_BRAIN_KIT_TAG:-$tag"
require_hit "README.md" "$tag"
require_hit "README.md" "$IMAGE:$tag"
require_hit "plugin.json" "\"version\": \"$version\""
require_hit ".codex-plugin/plugin.json" "\"version\": \"$version\""
require_hit ".claude-plugin/marketplace.json" "\"version\": \"$version\""
require_hit ".agents/plugins/marketplace.json" "\"ref\": \"$tag\""

if [ "$SKIP_IMAGE" = 1 ]; then
  echo "warning: skipped image manifest check for $IMAGE:$tag" >&2
elif command -v docker >/dev/null 2>&1; then
  if ! docker manifest inspect "$IMAGE:$tag" >/dev/null 2>&1; then
    err "missing image manifest: $IMAGE:$tag"
  fi
else
  err "docker unavailable; cannot verify image manifest $IMAGE:$tag"
fi

if [ "$SKIP_PROD" = 1 ]; then
  echo "warning: skipped sibling prod pin/lock checks" >&2
elif [ -d "$HOST_DIR/runtime" ]; then
  # Prod ships replypen origin/main, not the shared checkout's working tree: that one is often stale
  # or carries other agents' edits. Compare the fetched origin/main copies (working tree only offline).
  host_tmp="$(mktemp -d)"; trap 'rm -rf "$host_tmp"' EXIT
  host_ref=""
  git -C "$HOST_DIR" fetch -q origin main 2>/dev/null && host_ref="origin/main"
  [ -n "$host_ref" ] || echo "warning: cannot fetch replypen origin/main; checking the $HOST_DIR working tree" >&2
  host_file() {  # host_file <repo path> → a readable copy of it at $host_ref (empty = absent)
    local out="$host_tmp/${1//\//_}"
    if [ -z "$host_ref" ]; then
      [ -f "$HOST_DIR/$1" ] && echo "$HOST_DIR/$1"
    elif git -C "$HOST_DIR" show "$host_ref:$1" >"$out" 2>/dev/null; then
      echo "$out"
    fi
    return 0
  }
  prod_dockerfile="$(host_file runtime/Dockerfile)"
  prod_lock="$(host_file runtime/requirements.lock)"
  if [ -n "$prod_dockerfile" ]; then
    # Canonical spec, plus the pre-rebrand one a not-yet-renamed host still carries.
    pin_new="replypen-runtime @ git+https://github.com/replypen/replypen-brain-skills@$tag#subdirectory=runtime"
    pin_old="rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@$tag#subdirectory=runtime"
    if ! grep -qF "$pin_new" "$prod_dockerfile" && ! grep -qF "$pin_old" "$prod_dockerfile"; then
      err "replypen ${host_ref:-working tree} runtime/Dockerfile does not pin replypen-runtime to $tag"
    fi
  fi
  if [ -n "$prod_lock" ] && ! cmp -s "$ROOT/runtime/requirements.lock" "$prod_lock"; then
    err "replypen ${host_ref:-working tree} runtime/requirements.lock differs from runtime/requirements.lock"
  fi
else
  echo "warning: sibling replypen repo not found at $HOST_DIR; skipped prod pin/lock checks" >&2
fi

prev_tag="$(git -C "$ROOT" tag -l 'v*' | grep -v "^$tag$" | sort -V | tail -1 || true)"
if [ -n "$prev_tag" ]; then
  dep_diff="$(git -C "$ROOT" diff "$prev_tag" -- runtime/pyproject.toml \
    | grep -E '^[+-][[:space:]]+"[^"]+==|^[+-]test = ' || true)"
  if [ -n "$dep_diff" ] && git -C "$ROOT" diff --quiet "$prev_tag" -- runtime/requirements.lock; then
    err "runtime dependency edits since $prev_tag require regenerating runtime/requirements.lock"
  fi
fi

if [ "$fail" = 1 ]; then
  exit 1
fi
echo "release coherence ok: $tag"
