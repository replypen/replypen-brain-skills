#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REMOTE="$TMP/origin.git"
CHECKOUT="$TMP/kit"
mkdir -p "$TMP/brains"
mkdir -p "$TMP/not-a-brain"
git init -q --bare "$REMOTE"
git clone -q "$ROOT" "$CHECKOUT"
git -C "$CHECKOUT" switch -q -C main
git -C "$CHECKOUT" remote set-url origin "$REMOTE"
git -C "$CHECKOUT" config user.name "Release Test"
git -C "$CHECKOUT" config user.email "release-test@example.com"
git -C "$CHECKOUT" config tag.gpgSign false
git -C "$CHECKOUT" config push.followTags false
git -C "$CHECKOUT" push -q -u origin main
# A publisher may enable this globally. The release flow must still keep the annotated tag out of
# the main push until it has fetched and verified origin/main.
git -C "$CHECKOUT" config push.followTags true
cp "$ROOT/refresh-brains.sh" "$CHECKOUT/refresh-brains.sh"
mkdir -p "$CHECKOUT/skills/brain-git-sync/scripts" "$CHECKOUT/scripts"
cp "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
  "$CHECKOUT/skills/brain-git-sync/scripts/brain_git_sync.py"
cp "$ROOT/scripts/runtime_digest.py" "$CHECKOUT/scripts/runtime_digest.py"
git -C "$CHECKOUT" add refresh-brains.sh skills/brain-git-sync/scripts/brain_git_sync.py \
  scripts/runtime_digest.py
if ! git -C "$CHECKOUT" diff --cached --quiet; then
  git -C "$CHECKOUT" commit -q -m "test: install release sync primitive"
fi
git -C "$CHECKOUT" -c push.followTags=false push -q origin main

# Reject a version tag unless main already points at the tag's commit. This makes publication order
# observable rather than merely checking that both refs eventually converge.
cat >"$REMOTE/hooks/pre-receive" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
while read -r _old new ref; do
  case "$ref" in
    refs/tags/v*)
      main="$(git rev-parse refs/heads/main)"
      tagged="$(git rev-parse "$new^{commit}")"
      if [ "$main" != "$tagged" ]; then
        echo "release tag arrived before main" >&2
        exit 1
      fi
      ;;
  esac
done
HOOK
chmod +x "$REMOTE/hooks/pre-receive"

# A release from any other branch must fail before changing files or remote refs.
git -C "$CHECKOUT" switch -q -c release-test
before="$(git -C "$CHECKOUT" rev-parse HEAD)"
if RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image --no-prod-bump \
    "$TMP/not-a-brain" >"$TMP/non-main.out" 2>&1; then
  echo "error: non-main release unexpectedly succeeded" >&2
  exit 1
fi
grep -q "releases must be cut from main" "$TMP/non-main.out"
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$before"
test -z "$(git --git-dir="$REMOTE" tag -l 'v*')"

# A normal publish updates main before exposing the tag, and both resolve to the release commit.
git -C "$CHECKOUT" switch -q main
# The publish sync carries the skills test gate; the fixture releases below skip it (no full kit here).
RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image --no-prod-bump \
  --dry-run "$TMP/not-a-brain" >"$TMP/dry.out" 2>&1
grep -q -- "--verify-command RP_TEST_MERMAID=1 ./scripts/test-skills.sh" "$TMP/dry.out"
# A detached main-lineage worktree uses the same publisher; no shared branch switch is needed.
DETACHED="$TMP/detached"
git -C "$CHECKOUT" worktree add -q --detach "$DETACHED" HEAD
RC_BRAINS_ROOT="$TMP/brains" "$DETACHED/refresh-brains.sh" --release patch --no-image --no-prod-bump \
  --dry-run "$TMP/not-a-brain" >"$TMP/detached.out" 2>&1
grep -q "Reconcile and publish release commit" "$TMP/detached.out"
git -C "$CHECKOUT" worktree remove "$DETACHED"

export SKIP_TESTS=1
# Simulate another computer publishing after this checkout's last fetch. The release must merge this
# commit through the same brain-git-sync primitive before it tags anything.
PEER="$TMP/peer"
git clone -q "$REMOTE" "$PEER"
git -C "$PEER" config user.name "Peer Test"
git -C "$PEER" config user.email "peer-test@example.com"
echo "remote peer work" >"$PEER/peer-note.txt"
git -C "$PEER" add peer-note.txt
git -C "$PEER" commit -q -m "peer: concurrent work"
peer_commit="$(git -C "$PEER" rev-parse HEAD)"
git -C "$PEER" push -q origin main
if ! RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image --no-prod-bump \
    "$TMP/not-a-brain" >"$TMP/release.out" 2>&1; then
  cat "$TMP/release.out" >&2
  exit 1
fi
released="$(git -C "$CHECKOUT" rev-parse HEAD)"
tag="$(git -C "$CHECKOUT" describe --tags --exact-match HEAD)"
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$released"
test "$(git --git-dir="$REMOTE" rev-parse "refs/tags/$tag^{commit}")" = "$released"
git -C "$CHECKOUT" merge-base --is-ancestor "$peer_commit" "$released"
test "$(cat "$CHECKOUT/peer-note.txt")" = "remote peer work"
grep -q "verified origin/main == HEAD ($released)" "$TMP/release.out"
grep -q "Reconcile and publish release commit through brain-git-sync" "$TMP/release.out"

# --no-push creates the next release commit locally but creates no tag and leaves remote refs untouched,
# even when local main already carries an unpushed commit.
remote_main_before="$(git --git-dir="$REMOTE" rev-parse refs/heads/main)"
git -C "$CHECKOUT" commit -q --allow-empty -m "local-only work"
if ! RC_BRAINS_ROOT="$TMP/brains" "$CHECKOUT/refresh-brains.sh" --release patch --no-image --no-prod-bump --no-push \
    "$TMP/not-a-brain" >"$TMP/no-push.out" 2>&1; then
  cat "$TMP/no-push.out" >&2
  exit 1
fi
test "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$remote_main_before"
if git -C "$CHECKOUT" describe --tags --exact-match HEAD >/dev/null 2>&1; then
  echo "error: --no-push unexpectedly created a local release tag" >&2
  exit 1
fi
grep -q -- "--no-push: no sync, no tag; origin/main is unchanged" "$TMP/no-push.out"
grep -q "kept as local branch release-v" "$TMP/no-push.out"

# A peer advancing main after the release commit was verified but before tag publication must make
# the atomic main+tag push fail with no remote tag.
RACE_REMOTE="$TMP/race-origin.git"
RACE_CHECKOUT="$TMP/race-kit"
RACE_PEER="$TMP/race-peer"
git clone -q --bare "$REMOTE" "$RACE_REMOTE"
git clone -q "$RACE_REMOTE" "$RACE_CHECKOUT"
git clone -q "$RACE_REMOTE" "$RACE_PEER"
for repo in "$RACE_CHECKOUT" "$RACE_PEER"; do
  git -C "$repo" config user.name "Release Race Test"
  git -C "$repo" config user.email "release-race@example.com"
  git -C "$repo" config tag.gpgSign false
done
cp "$REMOTE/hooks/pre-receive" "$RACE_REMOTE/hooks/pre-receive"
chmod +x "$RACE_REMOTE/hooks/pre-receive"

REAL_GIT="$(command -v git)"
RACE_BIN="$TMP/race-bin"
RACE_COUNT="$TMP/race-ls-remote-count"
mkdir -p "$RACE_BIN"
cat >"$RACE_BIN/git" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" ls-remote "* && " $* " == *" refs/tags/v"* ]]; then
  count=0
  test ! -f "$RACE_COUNT" || count="$(cat "$RACE_COUNT")"
  count=$((count + 1))
  printf '%s\n' "$count" >"$RACE_COUNT"
  if [ "$count" = 2 ]; then
    "$REAL_GIT" -C "$RACE_PEER" fetch -q origin main
    "$REAL_GIT" -C "$RACE_PEER" merge -q --ff-only origin/main
    printf '%s\n' "peer moved after release verification" >"$RACE_PEER/tag-race.txt"
    "$REAL_GIT" -C "$RACE_PEER" add tag-race.txt
    "$REAL_GIT" -C "$RACE_PEER" commit -q -m "peer: move main before tag"
    "$REAL_GIT" -C "$RACE_PEER" push -q origin main
  fi
fi
exec "$REAL_GIT" "$@"
WRAPPER
chmod +x "$RACE_BIN/git"

# The race release cuts CUR+1; derive it from the checkout instead of hardcoding a version that in-test
# releases against the shared remote may already have consumed.
race_cur="$(grep -E '^VERSION = ' "$RACE_CHECKOUT/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"
IFS=. read -r _rma _rmi _rpa <<<"$race_cur"
race_tag="v$_rma.$_rmi.$((_rpa + 1))"
if PATH="$RACE_BIN:$PATH" REAL_GIT="$REAL_GIT" RACE_COUNT="$RACE_COUNT" RACE_PEER="$RACE_PEER" \
    RC_BRAINS_ROOT="$TMP/brains" "$RACE_CHECKOUT/refresh-brains.sh" --release patch --no-image --no-prod-bump \
    "$TMP/not-a-brain" >"$TMP/tag-race.out" 2>&1; then
  echo "error: release unexpectedly tagged after origin/main moved" >&2
  exit 1
fi
test "$(cat "$RACE_COUNT")" = 2
test "$(git --git-dir="$RACE_REMOTE" show main:tag-race.txt)" = "peer moved after release verification"
if git --git-dir="$RACE_REMOTE" rev-parse "refs/tags/$race_tag" >/dev/null 2>&1; then
  echo "error: atomic release race published $race_tag" >&2
  exit 1
fi

# ── runtime-vs-retag classification ──────────────────────────────────────────
# The image step rebuilds only when runtime/ CONTENT changed. A docs/skill release still sed-bumps the
# version literals in runtime/pyproject.toml, so the classifier canonicalizes those out and reads the
# committed repo-root RUNTIME_DIGEST across real tags — a dirty working tree would prove nothing.
CL_REMOTE="$TMP/classify-origin.git"
CL="$TMP/classify-kit"
git init -q --bare "$CL_REMOTE"
git clone -q "$ROOT" "$CL"
git -C "$CL" switch -q -C main
git -C "$CL" remote set-url origin "$CL_REMOTE"
git -C "$CL" config user.name "Classify Test"
git -C "$CL" config user.email "classify-test@example.com"
git -C "$CL" config tag.gpgSign false
git -C "$CL" config push.followTags false
cp "$ROOT/refresh-brains.sh" "$CL/refresh-brains.sh"
mkdir -p "$CL/scripts" "$CL/skills/brain-git-sync/scripts"
cp "$ROOT/scripts/runtime_digest.py" "$CL/scripts/runtime_digest.py"
cp "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
  "$CL/skills/brain-git-sync/scripts/brain_git_sync.py"
git -C "$CL" add refresh-brains.sh scripts/runtime_digest.py \
  skills/brain-git-sync/scripts/brain_git_sync.py
git -C "$CL" diff --cached --quiet || git -C "$CL" commit -q -m "test: install classifier under test"
git -C "$CL" push -q -u origin main

# A larger dotted number containing the next version is not a release literal (for example RFC
# 9110 section 10.2.3 must not block a v0.2.3 release).
printf '\n# RFC 9110 section 10.2.3\n' >>"$CL/runtime/lib/api.py"
git -C "$CL" commit -q -am "test: add dotted-number collision"
git -C "$CL" push -q origin main

cl_release() {  # release a patch (extra flags in $@); fail loudly on error
  if ! RC_BRAINS_ROOT="$TMP/brains" "$CL/refresh-brains.sh" --release patch --no-image --no-prod-bump \
      "$TMP/not-a-brain" "$@" >"$TMP/classify.out" 2>&1; then
    cat "$TMP/classify.out" >&2
    exit 1
  fi
}
cl_classify() {  # echo the runtime_changed value of the newest release
  RC_BRAINS_ROOT="$TMP/brains" "$CL/refresh-brains.sh" --classify | sed -n 's/^runtime_changed=//p'
}

# Bootstrap: the newest pre-migration tag has no RUNTIME_DIGEST → first release reads as changed.
cl_release
test "$(cl_classify)" = 1

# Docs-only release: version literals in pyproject bump, but canonicalized away → identical digest → 0.
echo "doc note" >>"$CL/README.md"
git -C "$CL" commit -q -am "docs: note"
cl_release
test "$(cl_classify)" = 0

# runtime/lib edit → content digest changes → 1.
printf '\n# classify probe\n' >>"$CL/runtime/lib/db.py"
git -C "$CL" commit -q -am "runtime: lib probe"
cl_release
test "$(cl_classify)" = 1

# A dependency pin that equals the NEXT version is someone else's version: the digest guard must not
# abort the release (defusedxml==0.7.1 did exactly that on the real v0.7.1 bump).
cl_cur="$(grep -E '^VERSION = ' "$CL/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"
IFS=. read -r _cma _cmi _cpa <<<"$cl_cur"
printf 'stray-pin==%s.%s.%s\n' "$_cma" "$_cmi" "$((_cpa + 1))" >>"$CL/runtime/requirements.lock"
git -C "$CL" commit -q -am "test: pin colliding with the next version"
cl_release
test "$(cl_classify)" = 1

# A release that aborts before the commit (here: a real stray literal tripping the digest guard) must
# leave NO half-bumped files behind in the shared checkout.
cl_next="$(grep -E '^VERSION = ' "$CL/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"
IFS=. read -r _sma _smi _spa <<<"$cl_next"
printf '\nRUNTIME_VERSION = "%s.%s.%s"\n' "$_sma" "$_smi" "$((_spa + 1))" >>"$CL/runtime/lib/db.py"
git -C "$CL" commit -q -am "test: stray version literal in runtime/lib"
if RC_BRAINS_ROOT="$TMP/brains" "$CL/refresh-brains.sh" --release patch --no-image --no-prod-bump \
    "$TMP/not-a-brain" >"$TMP/stray.out" 2>&1; then
  echo "error: stray runtime version literal did not abort the release" >&2
  exit 1
fi
grep -q "carries the version literal" "$TMP/stray.out"
if [ -n "$(git -C "$CL" status --porcelain=v1 --untracked-files=all)" ]; then
  echo "error: aborted release left the checkout dirty:" >&2
  git -C "$CL" status --porcelain=v1 --untracked-files=all >&2
  exit 1
fi
git -C "$CL" revert --no-edit HEAD >/dev/null
git -C "$CL" push -q origin main

# pyproject dependency edit + --relock → 1 (real dep add; --relock regenerates the lock).
python3 - "$CL/runtime/pyproject.toml" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read().replace('    "pyyaml==6.0.3",\n', '    "pyyaml==6.0.3",\n    "certifi",\n')
open(p, "w").write(s)
PY
git -C "$CL" commit -q -am "runtime: add certifi dep"
cl_release --relock
test "$(cl_classify)" = 1

echo "refresh-brains release tests passed"
