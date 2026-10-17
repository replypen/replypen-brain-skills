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
  prod_dockerfile="$HOST_DIR/runtime/Dockerfile"
  prod_lock="$HOST_DIR/runtime/requirements.lock"
  if [ -f "$prod_dockerfile" ]; then
    # Canonical spec, plus the pre-rebrand one a not-yet-renamed host still carries.
    pin_new="replypen-runtime @ git+https://github.com/replypen/replypen-brain-skills@$tag#subdirectory=runtime"
    pin_old="rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@$tag#subdirectory=runtime"
    if ! grep -qF "$pin_new" "$prod_dockerfile" && ! grep -qF "$pin_old" "$prod_dockerfile"; then
      err "$prod_dockerfile does not pin replypen-runtime to $tag"
    fi
  fi
  if [ -f "$prod_lock" ] && ! cmp -s "$ROOT/runtime/requirements.lock" "$prod_lock"; then
    err "$prod_lock differs from runtime/requirements.lock"
  fi
  # The host integration_catalog is a generated projection of these connector manifests. Fail the
  # release if a manifest changed without regenerating the prod snapshot (`make catalog`).
  catalog_gen="$HOST_DIR/scripts/gen_catalog_migration.py"
  if [ -f "$catalog_gen" ]; then
    if command -v uv >/dev/null 2>&1; then
      if ! uv run --with pyyaml python "$catalog_gen" \
          "$ROOT/runtime/lib/connectors" "$HOST_DIR/db/catalog.generated.sql" --check >/dev/null; then
        err "db/catalog.generated.sql is stale vs connector manifests — run \`make catalog\` in replypen"
      fi
    else
      echo "warning: uv unavailable; skipped catalog snapshot lockstep check" >&2
    fi
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
