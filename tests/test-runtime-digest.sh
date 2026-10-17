#!/usr/bin/env bash
# The digest guard must fire on a stray copy of OUR version, and stay silent on a dependency pin that
# merely equals it (defusedxml==0.7.1 on a v0.7.1 release aborted a real release at "Record runtime
# digest", leaving a half-bumped checkout behind).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REPO="$TMP/kit"
mkdir -p "$REPO/runtime/lib" "$REPO/scripts"
cp "$ROOT/scripts/runtime_digest.py" "$REPO/scripts/runtime_digest.py"
git init -q "$REPO"
git -C "$REPO" config user.name "Digest Test"
git -C "$REPO" config user.email "digest-test@example.com"

write_version() {  # $1 = version
  cat >"$REPO/runtime/pyproject.toml" <<TOML
[project]
name = "replypen-runtime"
version = "$1"
# uv add "replypen-runtime @ git+https://example.invalid/kit@v$1#subdirectory=runtime"
TOML
}

digest() { uv run --no-project python "$REPO/scripts/runtime_digest.py" --worktree --root "$REPO"; }

write_version 0.7.0
printf 'x = 1\n' >"$REPO/runtime/lib/db.py"
# A pin that will collide with the NEXT version, plus one that never does.
printf 'defusedxml==0.7.1\n    # via replypen-runtime\nbackoff==2.2.1\n' >"$REPO/runtime/requirements.lock"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "init"

before="$(digest)"

# The release bump: 0.7.0 → 0.7.1, colliding with the defusedxml pin.
write_version 0.7.1
after="$(digest)"
test "$before" = "$after" || { echo "error: digest changed on a pure version bump" >&2; exit 1; }

# A real stray literal (our version copied into a runtime file) must still abort.
printf 'RUNTIME_VERSION = "0.7.1"\n' >>"$REPO/runtime/lib/db.py"
git -C "$REPO" add -A
if digest >"$TMP/stray.out" 2>&1; then
  echo "error: stray version literal did not trip the guard" >&2
  cat "$TMP/stray.out" >&2
  exit 1
fi
grep -q "runtime/lib/db.py carries the version literal" "$TMP/stray.out"

echo "runtime digest guard tests passed"
