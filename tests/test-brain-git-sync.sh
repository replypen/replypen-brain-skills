#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

test -f "$SCRIPT"

write_file() {
  local path="$1"
  local contents="$2"
  printf '%s\n' "$contents" >"$path"
}

configure_clone() {
  local repo="$1"
  git -C "$repo" config user.name "Git Sync Test"
  git -C "$repo" config user.email "git-sync-test@example.com"
  git -C "$repo" config commit.gpgSign false
}

new_fixture() {
  local name="$1"
  local fixture="$TMP/$name"
  local seed="$fixture/seed"

  ORIGIN="$fixture/origin.git"
  LOCAL="$fixture/local"
  PEER="$fixture/peer"
  mkdir -p "$fixture"
  git init -q -b main "$seed"
  configure_clone "$seed"
  write_file "$seed/shared.txt" "base"
  git -C "$seed" add shared.txt
  git -C "$seed" commit -q -m "initial"
  git init -q --bare "$ORIGIN"
  git -C "$seed" remote add origin "$ORIGIN"
  git -C "$seed" push -q -u origin main
  git --git-dir="$ORIGIN" symbolic-ref HEAD refs/heads/main
  git clone -q "$ORIGIN" "$LOCAL"
  git clone -q "$ORIGIN" "$PEER"
  configure_clone "$LOCAL"
  configure_clone "$PEER"
  INITIAL="$(git -C "$LOCAL" rev-parse HEAD)"
}

run_sync() {
  uv run --no-project python "$SCRIPT" --repo "$LOCAL" "$@"
}

assert_ancestor() {
  local commit="$1"
  local descendant="$2"
  git -C "$LOCAL" merge-base --is-ancestor "$commit" "$descendant"
}

assert_converged_clean() {
  git -C "$LOCAL" fetch -q origin main
  local local_sha remote_sha
  local_sha="$(git -C "$LOCAL" rev-parse main)"
  remote_sha="$(git -C "$LOCAL" rev-parse origin/main)"
  test "$local_sha" = "$remote_sha"
  git -C "$LOCAL" diff --quiet
  git -C "$LOCAL" diff --cached --quiet
  test -z "$(git -C "$LOCAL" ls-files --others --exclude-standard)"
  FINAL="$local_sha"
}

assert_json_ok() {
  printf '%s\n' "$1" | uv run --no-project python -c \
    'import json, sys; data = json.load(sys.stdin); assert data["status"] == "ok"; assert data["ancestry_verified"] is True'
}

# Already synchronized: no mutation, exact convergence still proven.
new_fixture clean
output="$(run_sync --json)"
assert_json_ok "$output"
assert_converged_clean
test "$FINAL" = "$INITIAL"

# Even an already-equal final candidate runs explicit re-verification.
new_fixture equal-verifier
equal_origin="$(git --git-dir="$ORIGIN" rev-parse main)"
set +e
run_sync --verify-command false --json >"$TMP/equal-verifier-blocked.json" 2>&1
equal_verifier_status=$?
set -e
test "$equal_verifier_status" = 2
test "$(git --git-dir="$ORIGIN" rev-parse main)" = "$equal_origin"

# Local-only commits are pushed without rewriting them.
new_fixture local-ahead
write_file "$LOCAL/local.txt" "local ahead"
git -C "$LOCAL" add local.txt
git -C "$LOCAL" commit -q -m "local ahead"
pre_local="$(git -C "$LOCAL" rev-parse HEAD)"
run_sync >/dev/null
assert_converged_clean
assert_ancestor "$pre_local" "$FINAL"
test "$(cat "$LOCAL/local.txt")" = "local ahead"

# Remote-only commits fast-forward local main.
new_fixture remote-ahead
write_file "$PEER/remote.txt" "remote ahead"
git -C "$PEER" add remote.txt
git -C "$PEER" commit -q -m "remote ahead"
git -C "$PEER" push -q origin main
pre_remote="$(git -C "$PEER" rev-parse HEAD)"
run_sync >/dev/null
assert_converged_clean
assert_ancestor "$pre_remote" "$FINAL"
test "$(cat "$LOCAL/remote.txt")" = "remote ahead"

# Divergence uses a merge and preserves both lines of development.
new_fixture diverged
write_file "$LOCAL/local.txt" "local branch"
git -C "$LOCAL" add local.txt
git -C "$LOCAL" commit -q -m "local branch"
pre_local="$(git -C "$LOCAL" rev-parse HEAD)"
write_file "$PEER/remote.txt" "remote branch"
git -C "$PEER" add remote.txt
git -C "$PEER" commit -q -m "remote branch"
git -C "$PEER" push -q origin main
pre_remote="$(git -C "$PEER" rev-parse HEAD)"
run_sync >/dev/null
assert_converged_clean
assert_ancestor "$pre_local" "$FINAL"
assert_ancestor "$pre_remote" "$FINAL"
test "$(cat "$LOCAL/local.txt")" = "local branch"
test "$(cat "$LOCAL/remote.txt")" = "remote branch"
test "$(git -C "$LOCAL" rev-list --parents -n 1 "$FINAL" | wc -w | tr -d ' ')" = 3

# Dirty intent is never guessed. The blocked run preserves it; explicit staging plus a message
# commits both tracked and formerly-untracked intended work before publishing.
new_fixture dirty
write_file "$LOCAL/shared.txt" "intended tracked change"
write_file "$LOCAL/intended.go" "package intended"
set +e
run_sync --json >"$TMP/dirty-blocked.json" 2>"$TMP/dirty-blocked.err"
blocked_status=$?
set -e
test "$blocked_status" = 2
uv run --no-project python -c \
  'import json, sys; data = json.load(sys.stdin); assert data["status"] == "blocked"; inv = data["inventory"]; assert "shared.txt" in inv["unstaged"]; assert "intended.go" in inv["untracked"]' \
  <"$TMP/dirty-blocked.json"
test "$(cat "$LOCAL/shared.txt")" = "intended tracked change"
test "$(cat "$LOCAL/intended.go")" = "package intended"
git -C "$LOCAL" add shared.txt intended.go
output="$(run_sync --commit-message "publish intended local work" --json)"
assert_json_ok "$output"
assert_converged_clean
test "$(cat "$LOCAL/shared.txt")" = "intended tracked change"
test "$(cat "$LOCAL/intended.go")" = "package intended"
git -C "$LOCAL" show --format= --name-only HEAD | grep -Fxq shared.txt
git -C "$LOCAL" show --format= --name-only HEAD | grep -Fxq intended.go

# Verification covers unpublished local commits, including a just-committed prepared index. A
# failure leaves origin untouched; retrying with a corrected verifier retains transaction reporting.
new_fixture local-verifier
write_file "$LOCAL/verified.go" "package verified"
git -C "$LOCAL" add verified.go
origin_before_verify="$(git --git-dir="$ORIGIN" rev-parse main)"
set +e
run_sync --commit-message "prepared local verifier work" --verify-command false --json \
  >"$TMP/local-verifier-blocked.json" 2>&1
verifier_status=$?
set -e
test "$verifier_status" = 2
test "$(git --git-dir="$ORIGIN" rev-parse main)" = "$origin_before_verify"
test -z "$(git -C "$LOCAL" status --porcelain)"
output="$(run_sync --verify-command true --json)"
assert_json_ok "$output"
printf '%s\n' "$output" | uv run --no-project python -c \
  'import json, sys; data = json.load(sys.stdin); assert "verified.go" in data["committed_files"]; assert data["local_commits"]'
assert_converged_clean

# A successful verifier may generate a non-ignored cache. Sync blocks before push, but remembers the
# exact verified merge and original accounting. Cleaning the cache and retrying publishes that state.
new_fixture verifier-cache-retry
write_file "$LOCAL/local-cache.go" "package cache"
git -C "$LOCAL" add local-cache.go
write_file "$PEER/remote-cache.txt" "remote cache side"
git -C "$PEER" add remote-cache.txt
git -C "$PEER" commit -q -m "remote cache side"
git -C "$PEER" push -q origin main
remote_cache_sha="$(git -C "$PEER" rev-parse HEAD)"
origin_before_cache="$(git --git-dir="$ORIGIN" rev-parse main)"
set +e
run_sync --commit-message "local cache side" --verify-command 'touch generated.cache' --json \
  >"$TMP/verifier-cache-blocked.json" 2>&1
cache_status=$?
set -e
test "$cache_status" = 2
test "$(git --git-dir="$ORIGIN" rev-parse main)" = "$origin_before_cache"
rm "$LOCAL/generated.cache"
output="$(run_sync --json)"
assert_json_ok "$output"
printf '%s\n' "$output" | uv run --no-project python -c \
  'import json, sys; data = json.load(sys.stdin); assert "local-cache.go" in data["committed_files"]; assert len(data["remote_commits"]) == 1'
assert_converged_clean
assert_ancestor "$remote_cache_sha" "$FINAL"

# A real content conflict stops with MERGE_HEAD and the index intact. After a contextual resolution
# preserving both contributions, rerunning finishes the same merge rather than discarding a side.
new_fixture conflict
write_file "$LOCAL/shared.txt" "local contribution"
git -C "$LOCAL" add shared.txt
git -C "$LOCAL" commit -q -m "local conflicting edit"
pre_local="$(git -C "$LOCAL" rev-parse HEAD)"
write_file "$PEER/shared.txt" "remote contribution"
git -C "$PEER" add shared.txt
git -C "$PEER" commit -q -m "remote conflicting edit"
git -C "$PEER" push -q origin main
pre_remote="$(git -C "$PEER" rev-parse HEAD)"
set +e
run_sync --json >"$TMP/conflict.json" 2>"$TMP/conflict.err"
conflict_status=$?
set -e
test "$conflict_status" = 3
uv run --no-project python -c \
  'import json, sys; data = json.load(sys.stdin); assert data["status"] == "conflict"; assert data["inventory"]["conflicts"] == ["shared.txt"]' \
  <"$TMP/conflict.json"
test -f "$(git -C "$LOCAL" rev-parse --path-format=absolute --git-path MERGE_HEAD)"
test -n "$(git -C "$LOCAL" ls-files -u)"
test "$(git -C "$LOCAL" rev-parse HEAD)" = "$pre_local"
assert_ancestor "$INITIAL" "$pre_local"
assert_ancestor "$INITIAL" "$pre_remote"
write_file "$LOCAL/shared.txt" $'local contribution\nremote contribution'
git -C "$LOCAL" add shared.txt
run_sync >/dev/null
assert_converged_clean
assert_ancestor "$pre_local" "$FINAL"
assert_ancestor "$pre_remote" "$FINAL"
grep -Fxq "local contribution" "$LOCAL/shared.txt"
grep -Fxq "remote contribution" "$LOCAL/shared.txt"

# Simulate another computer advancing origin after fetch but before the first push. The first
# non-fast-forward rejection must be handled by refetching, merging, and retrying without force.
new_fixture push-race
write_file "$LOCAL/local-race.txt" "local before race"
git -C "$LOCAL" add local-race.txt
git -C "$LOCAL" commit -q -m "local before race"
pre_local="$(git -C "$LOCAL" rev-parse HEAD)"
hook="$(git -C "$LOCAL" rev-parse --path-format=absolute --git-path hooks/pre-push)"
mkdir -p "$(dirname "$hook")"
apply_hook="$TMP/push-race-fired"
cat >"$hook" <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
if ! test -e "$BRAIN_GIT_SYNC_TEST_SENTINEL"; then
  : >"$BRAIN_GIT_SYNC_TEST_SENTINEL"
  printf '%s\n' "remote during push" >"$BRAIN_GIT_SYNC_TEST_PEER/remote-race.txt"
  git -C "$BRAIN_GIT_SYNC_TEST_PEER" add remote-race.txt
  git -C "$BRAIN_GIT_SYNC_TEST_PEER" commit -q -m "remote during push"
  git -C "$BRAIN_GIT_SYNC_TEST_PEER" push -q origin main
  git -C "$BRAIN_GIT_SYNC_TEST_PEER" rev-parse HEAD >"$BRAIN_GIT_SYNC_TEST_REMOTE_SHA"
fi
HOOK
chmod +x "$hook"
remote_sha_file="$TMP/push-race-remote-sha"
output="$(BRAIN_GIT_SYNC_TEST_SENTINEL="$apply_hook" \
  BRAIN_GIT_SYNC_TEST_PEER="$PEER" \
  BRAIN_GIT_SYNC_TEST_REMOTE_SHA="$remote_sha_file" \
  run_sync --max-push-attempts 3 --json)"
pre_remote="$(cat "$remote_sha_file")"
assert_json_ok "$output"
printf '%s\n' "$output" | uv run --no-project python -c \
  'import json, sys; data = json.load(sys.stdin); assert data["push_attempts"] == 2; assert len(data["observed_remote_shas"]) >= 2'
assert_converged_clean
assert_ancestor "$pre_local" "$FINAL"
assert_ancestor "$pre_remote" "$FINAL"
test "$(cat "$LOCAL/local-race.txt")" = "local before race"
test "$(cat "$LOCAL/remote-race.txt")" = "remote during push"

# A force-updated origin/main (2026-10-30: `journal/` stripped from every brain's history) must never
# be merged back silently. rewrite_origin: LOCAL knows OLD_REMOTE, then PEER force-pushes NEW_REMOTE.
rewrite_origin() {
  write_file "$PEER/old-remote.txt" "old remote history"
  git -C "$PEER" add old-remote.txt
  git -C "$PEER" commit -q -m "old remote history"
  git -C "$PEER" push -q origin main
  OLD_REMOTE="$(git -C "$PEER" rev-parse HEAD)"
  git -C "$LOCAL" fetch -q origin main
  test "$(git -C "$LOCAL" rev-parse origin/main)" = "$OLD_REMOTE"
  "$@"
  git -C "$PEER" reset -q --hard "$INITIAL"
  write_file "$PEER/new-remote.txt" "replacement remote history"
  git -C "$PEER" add new-remote.txt
  git -C "$PEER" commit -q -m "replacement remote history"
  NEW_REMOTE="$(git -C "$PEER" rev-parse HEAD)"
  git -C "$PEER" push -q --force origin main
}
expect_rewritten_stop() {
  set +e
  run_sync --json >"$TMP/rewritten.json" 2>&1
  local status=$?
  set -e
  test "$status" = 2
  uv run --no-project python -c 'import json, sys; d = json.load(open(sys.argv[1])); assert d["status"] == "rewritten", d; assert sys.argv[2] in d["rewritten_remote_shas"]' \
    "$TMP/rewritten.json" "$OLD_REMOTE"
  test "$(git --git-dir="$ORIGIN" rev-parse main)" = "$NEW_REMOTE"
}

# Default: stop before merging or pushing, and keep stopping on a rerun (the fetch already moved
# origin/main, so only the saved recovery state still remembers the replaced tip).
new_fixture rewritten-origin-stops
rewrite_origin git -C "$LOCAL" merge -q --ff-only origin/main
pre_local="$(git -C "$LOCAL" rev-parse HEAD)"
expect_rewritten_stop
expect_rewritten_stop
test "$(git -C "$LOCAL" rev-parse HEAD)" = "$pre_local"

# accept, nothing of our own: main moves onto the new history; recovery state is gone.
output="$(run_sync --rewritten-remote accept --json)"
assert_json_ok "$output"
assert_converged_clean
test "$FINAL" = "$NEW_REMOTE"
test ! -e "$LOCAL/old-remote.txt"
test ! -e "$LOCAL/.git/brain-git-sync-state.json"
test -z "$(git -C "$LOCAL" for-each-ref refs/brain-git-sync/)"
test "$(git -C "$LOCAL" for-each-ref --format='%(objectname)' refs/brain-git-sync-rescue/)" = "$pre_local"

# accept with an unpushed commit made on the replaced history: only that commit is replayed.
new_fixture rewritten-origin-own-commit
add_own_commit() {
  git -C "$LOCAL" merge -q --ff-only origin/main
  write_file "$LOCAL/local.txt" "local work on old history"
  git -C "$LOCAL" add local.txt
  git -C "$LOCAL" commit -q -m "local work"
}
rewrite_origin add_own_commit
expect_rewritten_stop
output="$(run_sync --rewritten-remote accept --json)"
assert_json_ok "$output"
assert_converged_clean
assert_ancestor "$NEW_REMOTE" "$FINAL"
if git -C "$LOCAL" merge-base --is-ancestor "$OLD_REMOTE" "$FINAL"; then exit 1; fi
test ! -e "$LOCAL/old-remote.txt"
test "$(cat "$LOCAL/local.txt")" = "local work on old history"

# accept blocked by an untracked file the new history adds: the rewrite stays remembered, so after
# moving the file aside a plain rerun still stops instead of merging the replaced history back.
new_fixture rewritten-origin-untracked-blocks-reset
rewrite_origin git -C "$LOCAL" merge -q --ff-only origin/main
write_file "$LOCAL/new-remote.txt" "local untracked"
set +e
run_sync --rewritten-remote accept --json >/dev/null 2>&1
accept_status=$?
set -e
test "$accept_status" = 2
test "$(git -C "$LOCAL" rev-parse HEAD)" = "$OLD_REMOTE"
rm "$LOCAL/new-remote.txt"
expect_rewritten_stop

# A local merge carrying changes of its own (here: a file added inside the merge) cannot be replayed
# as plain commits: accept stops before touching main or origin.
new_fixture rewritten-origin-own-merge
own_merge() {
  git -C "$LOCAL" switch -q -c side "$INITIAL"
  write_file "$LOCAL/side.txt" "side work"
  git -C "$LOCAL" add side.txt
  git -C "$LOCAL" commit -q -m "side work"
  git -C "$LOCAL" switch -q main
  git -C "$LOCAL" merge -q --ff-only origin/main
  git -C "$LOCAL" merge -q --no-ff --no-commit side
  write_file "$LOCAL/inside-merge.txt" "edit made inside the merge"
  git -C "$LOCAL" add inside-merge.txt
  git -C "$LOCAL" commit -q -m "merge side"
  git -C "$LOCAL" branch -q -D side
}
rewrite_origin own_merge
pre_local="$(git -C "$LOCAL" rev-parse HEAD)"
set +e
run_sync --rewritten-remote accept --json >"$TMP/own-merge.json" 2>&1
accept_status=$?
set -e
test "$accept_status" = 2
grep -q local_merges_with_changes "$TMP/own-merge.json"
test "$(git -C "$LOCAL" rev-parse HEAD)" = "$pre_local"
test "$(git --git-dir="$ORIGIN" rev-parse main)" = "$NEW_REMOTE"

# A stale recovery state from an earlier blocked sync (here: a failing verifier) is caught too.
new_fixture rewritten-origin-stale-state
leave_stale_state() {
  set +e
  run_sync --verify-command false --json >/dev/null 2>&1
  set -e
  test -f "$LOCAL/.git/brain-git-sync-state.json"
}
rewrite_origin leave_stale_state
expect_rewritten_stop
output="$(run_sync --rewritten-remote accept --json)"
assert_json_ok "$output"
assert_converged_clean
test "$FINAL" = "$NEW_REMOTE"

# restore: an accidental force-push is undone by merging the dropped tip back (old behaviour).
new_fixture rewritten-origin-restore
rewrite_origin true
expect_rewritten_stop
output="$(run_sync --rewritten-remote restore --json)"
assert_json_ok "$output"
assert_converged_clean
assert_ancestor "$OLD_REMOTE" "$FINAL"
assert_ancestor "$NEW_REMOTE" "$FINAL"
test "$(cat "$LOCAL/old-remote.txt")" = "old remote history"
test "$(cat "$LOCAL/new-remote.txt")" = "replacement remote history"

# A read-only brain alias must never write its source project's brain: the check runs before any
# local commit or push, using whatever `rp` a fake PATH entry supplies.
new_fixture shared-consumer
write_file "$LOCAL/shared.txt" "consumer attempted write"
git -C "$LOCAL" add shared.txt
origin_before_consumer="$(git --git-dir="$ORIGIN" rev-parse main)"
pre_head="$(git -C "$LOCAL" rev-parse HEAD)"
FAKE_RC_BIN="$TMP/bin-consumer"
mkdir -p "$FAKE_RC_BIN"
cat >"$FAKE_RC_BIN/rc" <<'RC'
#!/usr/bin/env bash
echo "$FAKE_RC_RESPONSE"
exit "${FAKE_RC_EXIT:-0}"
RC
chmod +x "$FAKE_RC_BIN/rc"
ln -s rc "$FAKE_RC_BIN/rp"  # scripts resolve `rp` first
set +e
output="$(PATH="$FAKE_RC_BIN:$PATH" \
  FAKE_RC_RESPONSE='{"status":{"state":"read_only_alias","brain_source":{"project_id":"00000000-0000-0000-0000-000000000001","project":"pro-backup","read_only":true,"ref":"main","sha":"deadbeef"}}}' \
  run_sync --project pro-backup-staging --commit-message "consumer attempt" --json)"
consumer_status=$?
set -e
test "$consumer_status" = 2
printf '%s\n' "$output" | uv run --no-project python -c \
  'import json, sys; data = json.load(sys.stdin); assert data["status"] == "blocked"; assert "pro-backup-staging" in data["error"]; assert "pro-backup" in data["error"]'
test "$(git --git-dir="$ORIGIN" rev-parse main)" = "$origin_before_consumer"
test "$(git -C "$LOCAL" rev-parse HEAD)" = "$pre_head"
git -C "$LOCAL" diff --cached --name-only | grep -Fxq shared.txt

# A source project (or a server too old to report `brain_source`) is never blocked by the check.
new_fixture shared-owner
write_file "$LOCAL/owner.txt" "owner writes its own brain"
git -C "$LOCAL" add owner.txt
FAKE_RC_BIN_OWNER="$TMP/bin-owner"
mkdir -p "$FAKE_RC_BIN_OWNER"
cat >"$FAKE_RC_BIN_OWNER/rc" <<'RC'
#!/usr/bin/env bash
echo "$FAKE_RC_RESPONSE"
exit "${FAKE_RC_EXIT:-0}"
RC
chmod +x "$FAKE_RC_BIN_OWNER/rc"
ln -s rc "$FAKE_RC_BIN_OWNER/rp"  # scripts resolve `rp` first
output="$(PATH="$FAKE_RC_BIN_OWNER:$PATH" \
  FAKE_RC_RESPONSE='{"status":{"state":"current"}}' \
  run_sync --project pro-backup --commit-message "owner writes its own brain" --json)"
assert_json_ok "$output"
assert_converged_clean
test "$(cat "$LOCAL/owner.txt")" = "owner writes its own brain"

# An `rp dev brain status` failure (older server without the alias field, not logged in, offline)
# must degrade to standalone, never block sync.
new_fixture shared-rc-failing
write_file "$LOCAL/standalone.txt" "rc status unavailable"
git -C "$LOCAL" add standalone.txt
FAKE_RC_BIN_FAIL="$TMP/bin-fail"
mkdir -p "$FAKE_RC_BIN_FAIL"
cat >"$FAKE_RC_BIN_FAIL/rc" <<'RC'
#!/usr/bin/env bash
echo '{"error":"not logged in"}' >&2
exit 1
RC
chmod +x "$FAKE_RC_BIN_FAIL/rc"
ln -s rc "$FAKE_RC_BIN_FAIL/rp"  # scripts resolve `rp` first
output="$(PATH="$FAKE_RC_BIN_FAIL:$PATH" \
  run_sync --project pro-backup --commit-message "rc status unavailable" --json)"
assert_json_ok "$output"
assert_converged_clean
test "$(cat "$LOCAL/standalone.txt")" = "rc status unavailable"

# Keep the exact public command discoverable if the skill integration is present in this checkout.
if test -f "$ROOT/skills/brain-git-sync/SKILL.md"; then
  grep -Fq "\$brain dev: git sync" "$ROOT/skills/brain-git-sync/SKILL.md"
fi

echo "brain git sync tests passed"
