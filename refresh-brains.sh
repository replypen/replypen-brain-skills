#!/usr/bin/env bash
# THE standard flow: cut a Brain Dev kit release and refresh every local brain to it.
#
# Why this exists: all local brains symlink ONE shared clone (~/.replypen-brain-skills) at a pinned
# TAG (see install.sh). The skill ships strictly tag-pinned — every skill/doc change is a release — so
# "refresh all brains" means: bump the single version line (RELEASING.md), commit on main, reconcile
# and verify origin/main through brain-git-sync, then create/push the tag, ensure its workspace image
# exists, and refresh each brain.
#
# Usage:
#   ./refresh-brains.sh                         # refresh-only: re-point shared clone to the newest
#                                               #   existing tag + re-symlink every local brain
#   ./refresh-brains.sh --release patch         # cut vX.Y.(Z+1) from committed main, then refresh
#                                               #   (uncommitted work in this checkout never rides along)
#   ./refresh-brains.sh --release minor|major   # bump that field instead
#   ./refresh-brains.sh --release 0.2.0         # explicit version
#   ./refresh-brains.sh --release patch --relock  # ALSO regen requirements.lock + REBUILD the image
#                                               #   (use whenever runtime/ dependencies changed)
#   ./refresh-brains.sh --release patch --dry-run # print the whole plan, mutate NOTHING
#   ./refresh-brains.sh --classify              # print runtime_changed=0|1 + digest, no side effects
#
# Flags:
#   --release <patch|minor|major|X.Y.Z>  cut a release before refreshing
#   --relock      regen runtime/requirements.lock and force a full image rebuild (deps changed)
#   --no-image    skip the ghcr image step entirely (uv mode still works; docker mode needs it later)
#   --no-push     commit locally but do NOT sync/tag/push/image (brains can't fetch it yet)
#   --no-prod-bump  skip auto-bumping the sibling replypen pin (you will bump it yourself)
#   --dry-run     show every step without running it
#   --classify    print whether the two newest tags differ in runtime content, then exit
#   [BRAIN_DIR...]  explicit brains to refresh (default: auto-discover siblings)
#
# The release phase runs in a throwaway detached worktree at this checkout's committed main, so a shared
# checkout holding colleagues' WIP is never touched; it is pulled afterwards only when its tree is clean.
#
# Prod (ReplyPen): step 3 bumps + commits the sibling checkout's pin itself. Promoting it stays the
# operator's call, but a released-yet-unpinned kit now fails this script instead of being a reminder.
set -euo pipefail

# ── repo + tooling ──────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -d "$ROOT/skills/local-brain-work" ] || { echo "error: run from the replypen-brain-skills repo root" >&2; exit 1; }
KIT="${RC_BRAIN_KIT:-$HOME/.replypen-brain-skills}"
BRAINS_ROOT="${RC_BRAINS_ROOT:-$(dirname "$ROOT")}"   # where the sibling replypen-brain-* repos live
IMAGE="ghcr.io/replypen/workspace"

# ── args ────────────────────────────────────────────────────────────────────
RELEASE="" RELOCK=0 NO_IMAGE=0 NO_PUSH=0 DRY=0 CLASSIFY=0 NO_PROD_BUMP=0; BRAINS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="${2:?--release needs patch|minor|major|X.Y.Z}"; shift 2;;
    --relock)   RELOCK=1; shift;;
    --no-image) NO_IMAGE=1; shift;;
    --no-push)  NO_PUSH=1; shift;;
    --no-prod-bump) NO_PROD_BUMP=1; shift;;
    --dry-run)  DRY=1; shift;;
    --classify) CLASSIFY=1; shift;;
    -h|--help)  sed -n '2,36p' "$0"; exit 0;;
    -*) echo "unknown flag: $1" >&2; exit 1;;
    *)  BRAINS+=("$1"); shift;;
  esac
done

run() { echo "  + $*"; [ "$DRY" = 1 ] || "$@"; }   # echo every mutating step; skip it on --dry-run
say() { echo; echo "▸ $*"; }

# The single content-based digest of runtime/ at a working tree or ref (version literals canonicalized).
runtime_digest() { uv run --no-project python "$ROOT/scripts/runtime_digest.py" "$@" --root "$ROOT"; }

# Side-effect-free classification of the most recent release: do the two newest tags carry different
# committed RUNTIME_DIGEST values? A single tag (or a tag missing the file) reads as changed.
if [ "$CLASSIFY" = 1 ]; then
  latest="$(git -C "$ROOT" tag -l 'v*' | sort -V | tail -1)"
  prev="$(git -C "$ROOT" tag -l 'v*' | sort -V | tail -2 | head -1)"
  latest_d="$(git -C "$ROOT" show "$latest:RUNTIME_DIGEST" 2>/dev/null || true)"
  prev_d="$(git -C "$ROOT" show "$prev:RUNTIME_DIGEST" 2>/dev/null || true)"
  if [ -z "$latest" ] || [ "$latest" = "$prev" ] || [ -z "$latest_d" ] || [ "$latest_d" != "$prev_d" ]; then
    echo "runtime_changed=1"
  else
    echo "runtime_changed=0"
  fi
  echo "digest=$latest_d"
  exit 0
fi

verify_origin_main() {
  local head_sha origin_main_sha
  head_sha="$(git -C "$ROOT" rev-parse HEAD)"
  origin_main_sha="$(git -C "$ROOT" rev-parse refs/remotes/origin/main)"
  if [ "$origin_main_sha" != "$head_sha" ]; then
    echo "error: origin/main is $origin_main_sha, expected release HEAD $head_sha; refusing to push tag" >&2
    exit 1
  fi
  echo "  verified origin/main == HEAD ($head_sha)"
}

# ── 1. release (optional) ───────────────────────────────────────────────────
if [ -n "$RELEASE" ]; then
  BRANCH="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD || true)"
  if [ "$BRANCH" != "main" ] && [ -n "$BRANCH" ]; then
    echo "error: releases must be cut from main or a detached main-lineage worktree (current: $BRANCH)" >&2
    exit 1
  fi

  if [ -z "$BRANCH" ]; then
    git -C "$ROOT" fetch -q origin main
    git -C "$ROOT" merge-base --is-ancestor origin/main HEAD || {
      echo "error: detached release HEAD must descend from origin/main" >&2; exit 1;
    }
  fi

  # Releases carry committed work only, so the whole release phase (bump → commit → sync → tag → image)
  # runs in a throwaway detached worktree at this checkout's committed main: parallel agents' WIP in the
  # shared checkout is never swept in, rolled back or blocking. Removed on every exit path.
  SHARED="$ROOT"
  DIRTY="$(git -C "$SHARED" status --porcelain=v1 --untracked-files=all)"
  if [ -n "$DIRTY" ]; then
    say "Notice: uncommitted work in $SHARED is NOT part of this release"
    printf '%s\n' "$DIRTY" | sed 's/^/    /'
  fi
  # An owned mktemp dir, never a predictable path: cleanup only ever removes what THIS invocation created
  # (a pre-existing /tmp/rc-kit-release-<pid> would otherwise be rm -rf'd after a failed worktree add).
  RELEASE_WT="$(mktemp -d "${TMPDIR:-/tmp}/rc-kit-release.XXXXXX")"
  cleanup_release_wt() {
    [ -n "${RELEASE_WT:-}" ] || return 0
    git -C "$SHARED" worktree remove --force "$RELEASE_WT" 2>/dev/null || rm -rf "$RELEASE_WT"
    git -C "$SHARED" worktree prune 2>/dev/null || true
    RELEASE_WT=""
  }
  trap cleanup_release_wt EXIT
  trap 'exit 130' INT TERM
  say "Release from throwaway worktree $RELEASE_WT (removed on exit; --dry-run reads from it too)"
  rmdir "$RELEASE_WT"   # worktree add wants a fresh path; the mktemp name stays ours
  git -C "$SHARED" worktree add -q --detach "$RELEASE_WT" HEAD
  ROOT="$RELEASE_WT"

  # A stale computer must observe an already-cut release before choosing the next version.
  if [ "$NO_PUSH" = 1 ]; then  # brain-git-sync would push local-only commits; merge without publishing
    say "Merge origin/main locally before choosing the release version (--no-push: no remote writes)"
    run git -C "$ROOT" fetch -q origin main
    run git -C "$ROOT" merge -q --no-edit origin/main
  else
    say "Reconcile main before choosing the release version"
    run uv run --no-project python "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
      --repo "$ROOT" --detached-head --max-push-attempts 4 \
      --verify-command 'SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh'
  fi
  CUR="$(grep -E '^VERSION = ' "$ROOT/skills/local-brain-work/scripts/brain_env.py" | sed -E 's/.*"([0-9.]+)".*/\1/')"

  case "$RELEASE" in
    patch|minor|major)
      IFS=. read -r MA MI PA <<<"$CUR"
      case "$RELEASE" in
        patch) PA=$((PA+1));;
        minor) MI=$((MI+1)); PA=0;;
        major) MA=$((MA+1)); MI=0; PA=0;;
      esac
      NEW="$MA.$MI.$PA";;
    [0-9]*.[0-9]*.[0-9]*) NEW="$RELEASE";;
    *) echo "error: --release must be patch|minor|major|X.Y.Z" >&2; exit 1;;
  esac
  say "Release v$CUR → v$NEW"
  git -C "$ROOT" rev-parse "v$NEW" >/dev/null 2>&1 && { echo "error: tag v$NEW already exists" >&2; exit 1; }
  git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/v$NEW" >/dev/null 2>&1 && {
    echo "error: remote tag v$NEW already exists; refetch before choosing a version" >&2; exit 1; }

  # 1a. bump every literal RELEASING.md lists (all $CUR hits in these files are OUR version).
  say "Bump version literals"
  FILES=(
    skills/local-brain-work/scripts/brain_env.py
    plugin.json .claude-plugin/marketplace.json
    .codex-plugin/plugin.json .agents/plugins/marketplace.json
    runtime/pyproject.toml install.sh
    README.md docs/onboarding.md docs/migration-host.md
  )
  for f in "${FILES[@]}"; do
    [ -f "$ROOT/$f" ] || continue
    # Digit-bounded and never after '==': a bare 0.11.1 → 0.11.2 would also rewrite a dependency pin like
    # pdfplumber==0.11.10, and at kit v0.11.10 the exact pin itself matched (seen 2026-10-08 / 2026-10-12: the
    # coherence check then reports a phantom dependency edit and aborts the release).
    grep -qF "$CUR" "$ROOT/$f" && run sed -i '' -E "s/(^|[^0-9.=])${CUR//./\\.}([^0-9]|$)/\\1$NEW\\2/g" "$ROOT/$f" || true
  done

  # 1b. lockfile: only when deps changed (--relock). Skill/doc releases keep lib identical → identical
  #     lock → the image is re-tagged, not rebuilt (see 1d).
  if [ "$RELOCK" = 1 ]; then
    say "Regenerate requirements.lock (deps changed)"
    run uv pip compile "$ROOT/runtime/pyproject.toml" --universal --python-version 3.12 \
        -o "$ROOT/runtime/requirements.lock"
  fi

  # 1b2. Record runtime/'s content digest (version literals canonicalized) into the repo-root
  #      RUNTIME_DIGEST so the release commit itself carries the rebuild-vs-retag fact both sides read.
  #      After --relock so a regenerated lock counts; the file lives outside runtime/ so it never
  #      perturbs its own input.
  say "Record runtime digest"
  DIGEST="$(runtime_digest --worktree)"
  echo "  + RUNTIME_DIGEST=$DIGEST"
  [ "$DRY" = 1 ] || printf '%s\n' "$DIGEST" > "$ROOT/RUNTIME_DIGEST"

  # 1c. local coherence before commit/tag. Image existence is checked after push/build; prod pin drift
  #     is a separate follow-up unless the sibling replypen checkout is already updated.
  say "Check local release coherence"
  run env SKIP_IMAGE=1 SKIP_PROD=1 "$ROOT/check-release-coherence.sh"

  # 1d. One release commit carrying ONLY the files this bump wrote (version literals, RUNTIME_DIGEST,
  #     the lock on --relock). Do not create even a local release tag until brain-git-sync has
  #     reconciled and verified this commit at origin/main.
  say "Commit release (worktree)"
  RELEASE_PATHS=(RUNTIME_DIGEST)
  for f in "${FILES[@]}"; do [ -f "$ROOT/$f" ] && RELEASE_PATHS+=("$f"); done
  [ "$RELOCK" = 1 ] && RELEASE_PATHS+=(runtime/requirements.lock)
  run git -C "$ROOT" add -- "${RELEASE_PATHS[@]}"
  if [ "$DRY" = 1 ]; then git -C "$ROOT" status --short -- "${RELEASE_PATHS[@]}"; else
    git -C "$ROOT" commit -q -m "release: v$NEW" \
      -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" -- "${RELEASE_PATHS[@]}"
  fi

  # Classify rebuild-vs-retag ONCE (reused by the image + prod-reminder steps). --relock forces a
  # rebuild; else compare the recorded digest to the prior release's. DIGEST equals HEAD's committed
  # RUNTIME_DIGEST here, so this is the committed-state diff even in --dry-run (no commit). Bootstrap:
  # v$CUR predates RUNTIME_DIGEST → PREV empty → changed once, then steady state.
  RUNTIME_CHANGED=0
  PREV_DIGEST="$(git -C "$ROOT" show "v$CUR:RUNTIME_DIGEST" 2>/dev/null || true)"
  if [ "$RELOCK" = 1 ] || [ "$DIGEST" != "$PREV_DIGEST" ]; then
    RUNTIME_CHANGED=1
  fi

  # Test gate on the merged tree before the push: skill engines always, runtime/ when its digest moved.
  # SKIP_TESTS=1 only for a release whose tree another gate already proved (the plumbing test does).
  TEST_GATE=()
  if [ "${SKIP_TESTS:-}" != 1 ]; then
    TEST_GATE+=(--verify-command ./scripts/test-skills.sh)
    [ "$RUNTIME_CHANGED" = 1 ] && TEST_GATE+=(--verify-command \
      "cd runtime && PYTHONPATH=\$PWD uv run --with '.[test]' --no-project pytest tests -q -p no:cacheprovider")
  fi

  if [ "$NO_PUSH" = 1 ]; then
    echo "  (--no-push: no sync, no tag; origin/main is unchanged)"
  else
    say "Reconcile and publish release commit through brain-git-sync"
    run uv run --no-project python "$ROOT/skills/brain-git-sync/scripts/brain_git_sync.py" \
      --repo "$ROOT" --detached-head --max-push-attempts 4 \
      --verify-command 'SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh' ${TEST_GATE[@]+"${TEST_GATE[@]}"}
    run verify_origin_main

    say "Create and publish tag only after verified origin/main"
    if git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/v$NEW" >/dev/null 2>&1; then
      echo "error: remote tag v$NEW appeared during release; refusing to replace it" >&2
      exit 1
    fi
    run git -C "$ROOT" tag -a -m "v$NEW" "v$NEW"   # annotated: repo config may force signed tags
    # Include main in the atomic request so a normal concurrent advance rejects tag publication.
    run git -C "$ROOT" -c push.followTags=false push --atomic origin \
      HEAD:refs/heads/main "refs/tags/v$NEW"
  fi

  # 1e. workspace image for the tag. Rebuild only when runtime/ changed; else re-tag the prior image
  #     (byte-identical — the image bakes runtime/ only, never the skill).
  if [ "$NO_IMAGE" = 1 ]; then
    echo "  (--no-image: skipping image; docker mode needs $IMAGE:v$NEW built before pre-push)"
  elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "  ⚠ docker unavailable — image NOT built. Before any docker-mode run:" >&2
    echo "      docker build -f docker/Dockerfile -t $IMAGE:v$NEW $ROOT && docker push $IMAGE:v$NEW" >&2
  else
    if [ "$RUNTIME_CHANGED" = 1 ]; then
      say "Build + push image $IMAGE:v$NEW (runtime changed)"
      run docker build -f "$ROOT/docker/Dockerfile" -t "$IMAGE:v$NEW" "$ROOT"
    else
      say "Re-tag image $IMAGE:v$CUR → :v$NEW (runtime identical)"
      docker image inspect "$IMAGE:v$CUR" >/dev/null 2>&1 || run docker pull "$IMAGE:v$CUR"
      run docker tag "$IMAGE:v$CUR" "$IMAGE:v$NEW"
    fi
    [ "$NO_PUSH" = 1 ] || run docker push "$IMAGE:v$NEW"
  fi

  TARGET="v$NEW"
  if [ "$NO_PUSH" = 1 ] && [ "$DRY" != 1 ]; then
    # The worktree goes away on exit; a local branch keeps the unpublished release commit reachable.
    git -C "$SHARED" branch -f "release-$TARGET" "$(git -C "$ROOT" rev-parse HEAD)"
    say "Skip fan-out because --no-push leaves $TARGET unfetchable by the shared clone"
    echo; echo "done — release commit $(git -C "$ROOT" rev-parse --short HEAD) kept as local branch release-$TARGET;"
    echo "       publish it with RELEASING.md's manual sync/verify/tag steps"
    exit 0
  fi

  # Verify the single version line after the release image/tag exists, before local brains fan out.
  say "Check release image coherence"
  if [ "$NO_IMAGE" = 1 ]; then
    run env SKIP_IMAGE=1 SKIP_PROD=1 "$ROOT/check-release-coherence.sh"
  else
    run env SKIP_PROD=1 "$ROOT/check-release-coherence.sh"
  fi

  # Release phase done: drop the worktree, return to the shared checkout for the brain fan-out.
  RELEASE_SHA="$(git -C "$ROOT" rev-parse HEAD)"
  cleanup_release_wt
  ROOT="$SHARED"
  if [ "$DRY" != 1 ]; then
    say "Sync the shared checkout"
    # Tracked edits block the pull (other agents' WIP); untracked files never do: git aborts the merge
    # itself if one would be overwritten. A stale untracked symlink kept this checkout on the old
    # release, pairing new scripts with the old image (2026-10-21).
    if [ -z "$(git -C "$ROOT" status --porcelain=v1 --untracked-files=no)" ]; then
      run git -C "$ROOT" pull -q --no-rebase origin main
    else
      echo "  uncommitted work here — NOT pulled. $TARGET = $RELEASE_SHA on origin/main;"
      echo "  run 'git pull --no-rebase' once the tree is clean"
    fi
  fi
else
  # refresh-only: target the newest existing tag.
  TARGET="$(git -C "$ROOT" tag -l 'v*' | sort -V | tail -1)"
  say "Refresh-only → newest tag $TARGET"
fi

# ── 2. fan out to every local brain (install.sh is the per-brain primitive) ──
if [ "${#BRAINS[@]}" -eq 0 ]; then
  while IFS= read -r d; do BRAINS+=("$d"); done < <(
    find "$BRAINS_ROOT" -maxdepth 1 -type d \( -name 'replypen-brain-*' -o -name 'rootcause-brain-*' \) ! -name '*-skills' | sort
  )
fi
say "Refresh ${#BRAINS[@]} brain(s) to $TARGET (shared clone: $KIT)"
# install.sh (~2 s) + the hygiene sweep (~1 s) for ~80 brains ran strictly serially (~4 min). Now:
#   - the first unit runs alone and pins the shared clone to $TARGET (the only fetch/checkout); every
#     later install.sh takes its read-only "already at" fast path, so the kit never mutates mid-fan-out;
#   - brains sharing a git common-dir (worktrees of one repo) form one serial unit, because install.sh
#     rewrites info/exclude + hooks there; units run FANOUT_JOBS at a time;
#   - output is buffered per unit and printed in order; failed installs fail the script after the sweep
#     (hygiene stays advisory).
FANOUT_JOBS=4
VALID=() UNIT_KEYS=() UNITS=()   # UNITS[k] = newline-separated brain dirs sharing UNIT_KEYS[k]
for b in ${BRAINS[@]+"${BRAINS[@]}"}; do  # bash 3.2 + set -u: an empty array is "unbound"
  # accept all brain layouts: project (skills/ | playbooks/ | projection.yaml) + nested tenant
  # (.replypen.toml — its committed project∪tenant binding; a tenant brain holds only a free-form NL
  # delta + sealed .env now, no tenant.json, so .replypen.toml is its marker).
  [ -d "$b/skills" ] || [ -d "$b/playbooks" ] || [ -f "$b/projection.yaml" ] || [ -f "$b/.replypen.toml" ] || [ -f "$b/.rootcause.toml" ] || {
    echo "  skip $b (no skills/ | playbooks/ | projection.yaml | .replypen.toml — not a brain)"; continue; }
  b="$(cd "$b" && pwd -P)"
  dup=0; for v in ${VALID[@]+"${VALID[@]}"}; do [ "$v" != "$b" ] || dup=1; done
  [ "$dup" = 0 ] || continue
  VALID+=("$b")
  key="$(cd "$b" && git rev-parse --git-common-dir 2>/dev/null)" && key="$(cd "$b" && cd "$key" && pwd -P)" || key="$b"
  k=0
  while [ "$k" -lt "${#UNIT_KEYS[@]}" ] && [ "${UNIT_KEYS[$k]}" != "$key" ]; do k=$((k + 1)); done
  if [ "$k" = "${#UNIT_KEYS[@]}" ]; then UNIT_KEYS+=("$key"); UNITS+=("$b"); else UNITS[$k]="${UNITS[$k]}"$'\n'"$b"; fi
done

# One unit (its brains serially) → $FANOUT_DIR/<k>.log; failed brain names → <k>.failed.
refresh_unit() {
  local k="$1" b rc
  : >"$FANOUT_DIR/$k.failed"
  while IFS= read -r b; do
    echo "  → $(basename "$b")"
    rc=0
    RC_BRAIN_KIT="$KIT" RC_BRAIN_KIT_TAG="$TARGET" "$ROOT/install.sh" "$b" >/dev/null 2>"$FANOUT_DIR/$k.err" || rc=$?
    sed 's/^/    /' "$FANOUT_DIR/$k.err"
    if [ "$rc" != 0 ]; then
      echo "    ✗ install.sh failed (exit $rc)"; basename "$b" >>"$FANOUT_DIR/$k.failed"; continue
    fi
    # Legacy-debt sweep: the commit/push gates judge changed files only, so a rule added after a file
    # was written (2026-10-08: a 176-char description from before the 150-char rule) stays invisible
    # until someone runs --all. Report, never block a release.
    (cd "$b" && uv run --no-project python "$ROOT/skills/local-brain-work/scripts/brain_hygiene.py" --all 2>/dev/null | grep -v '^NOTICE' | grep -E ' hygiene: ' | sed 's/^/    ⚠ /' || true)
  done <<<"${UNITS[$k]}" >"$FANOUT_DIR/$k.log" 2>&1
  return 0
}

FAILED=()
if [ "$DRY" = 1 ]; then
  for b in ${VALID[@]+"${VALID[@]}"}; do echo "  → $(basename "$b")"; done
elif [ "${#UNITS[@]}" -gt 0 ]; then
  FANOUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rc-kit-fanout.XXXXXX")"
  PIDS=("")
  # A cancelled/timed-out refresh must not leave installers mutating brains after the parent is gone
  # (a retry would overlap the same destinations) nor leak FANOUT_DIR. Composes with the release trap.
  fanout_cleanup() {
    local p
    # `|| true`: under set -e a kill of an already-finished worker would abort the script right here.
    for p in ${PIDS[@]+"${PIDS[@]}"}; do [ -z "$p" ] || kill "$p" 2>/dev/null || true; done
    wait 2>/dev/null || true
    [ -n "${FANOUT_DIR:-}" ] && rm -rf "$FANOUT_DIR"
    FANOUT_DIR=""
  }
  on_exit() { fanout_cleanup; [ "$(type -t cleanup_release_wt)" = function ] && cleanup_release_wt; return 0; }
  trap on_exit EXIT
  trap 'exit 130' INT TERM
  done_upto=0   # units [0, done_upto) are waited for and printed
  collect() {   # wait for unit $done_upto, print its buffered output, record failures
    [ "$done_upto" = 0 ] || wait "${PIDS[$done_upto]}" || true
    cat "$FANOUT_DIR/$done_upto.log"
    while IFS= read -r f; do FAILED+=("$f"); done <"$FANOUT_DIR/$done_upto.failed"
    done_upto=$((done_upto + 1))
  }
  refresh_unit 0   # serial: the only unit whose install may fetch/checkout the shared clone
  collect
  # Pinned now, or every later install would race on the shared clone (and fail the same way anyway).
  # Exempt the developer override install.sh itself honours (RC_BRAIN_KIT = this checkout, used as-is).
  if [ "${#FAILED[@]}" -gt 0 ] || { [ -d "$KIT/.git" ] && [ "$(cd "$KIT" && pwd -P)" != "$(cd "$ROOT" && pwd -P)" ] && \
       [ "$(git -C "$KIT" describe --tags --exact-match 2>/dev/null || true)" != "$TARGET" ]; }; then
    echo "error: first install failed (${FAILED[*]:-none}) or $KIT is not at $TARGET; aborting fan-out" >&2; exit 1
  fi
  k=1
  while [ "$k" -lt "${#UNITS[@]}" ]; do
    [ $((k - done_upto)) -lt "$FANOUT_JOBS" ] || collect
    refresh_unit "$k" &
    PIDS+=("$!")
    k=$((k + 1))
  done
  while [ "$done_upto" -lt "${#UNITS[@]}" ]; do collect; done
  fanout_cleanup
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "error: install.sh failed for ${#FAILED[@]} brain(s): ${FAILED[*]}" >&2
  exit 1
fi

# ── 3. prod (replypen) — consume the new pin in the sibling checkout ─────────
# A release only reaches prod once ReplyPen's Dockerfile pin + its lockstep requirements.lock move to
# this tag. That used to be a printed reminder, and reminders get missed — so we run replypen's own
# bump-workspace-pin.py there and commit it. We bump on EVERY release, not only runtime-changing ones:
# keeping pin == newest tag unconditionally is what removes the whole "stranded release" class.
#
# Blast radius is deliberately tiny: a throwaway worktree at origin/main, only the pin files committed,
# and only that one commit pushed (the shared checkout and its parallel agents' work are never touched). Any failure is a loud non-zero exit, never a
# silent skip — a stranded pin must cost you the release's exit code.
prod_bump() {
  local target="$1"
  # Host checkout: new name first, the pre-rebrand `rootcause` folder until a laptop is migrated.
  local prod="${RC_REPLYPEN_DIR:-${RC_ROOTCAUSE_DIR:-}}"
  if [ -z "$prod" ]; then
    prod="$BRAINS_ROOT/replypen"
    [ -f "$prod/scripts/bump-workspace-pin.py" ] || prod="$BRAINS_ROOT/rootcause"
  fi
  local bump="$prod/scripts/bump-workspace-pin.py"
  local paths=(runtime/Dockerfile runtime/requirements.lock dashboard-overlay)

  if [ ! -f "$bump" ]; then
    echo "  ⚠ no sibling replypen checkout at $prod" >&2
    echo "    bump it manually there: scripts/bump-workspace-pin.py $target" >&2
    return 1
  fi
  if [ "$DRY" = 1 ]; then
    echo "  + worktree $prod@origin/main: scripts/bump-workspace-pin.py $target && commit + push the pin files"
    return 0
  fi

  # Never the shared checkout: it can be stale (an old bump script) or carry parallel agents' unpushed
  # commits, which strands the pin. Bump in a throwaway worktree at a fresh origin/main instead.
  git -C "$prod" fetch -q origin main || { echo "  ⚠ cannot fetch replypen origin/main" >&2; return 1; }
  local wt="/tmp/rc-pin-$$"
  git -C "$prod" worktree add -q --detach "$wt" origin/main || return 1
  # raw.githubusercontent can lag a freshly pushed tag by seconds; the bump fetches the lock from there.
  local attempt=1 rc=0
  until (cd "$wt" && MISE_TRUSTED_CONFIG_PATHS="$wt" scripts/bump-workspace-pin.py "$target"); do
    if [ "$attempt" -ge 5 ]; then
      echo "  ⚠ bump-workspace-pin.py $target failed after $attempt attempts" >&2
      rc=1; break
    fi
    echo "  … retry $attempt: tag may not be visible on raw.githubusercontent yet; waiting 10s"
    attempt=$((attempt + 1)); sleep 10
  done
  if [ "$rc" = 0 ] && [ -z "$(git -C "$wt" status --porcelain -- "${paths[@]}")" ]; then
    echo "  replypen pin already at $target — nothing to commit"
  elif [ "$rc" = 0 ] && ! { git -C "$wt" add -- "${paths[@]}" &&
      git -C "$wt" commit -q -m "chore(runtime): bump workspace pin to $target" \
        -m "Automated by replypen-brain-skills refresh-brains.sh at release $target."; }; then
    # errexit is off in here (called under `||`): an unchecked failed commit would push the old HEAD.
    echo "  ⚠ could not commit the pin bump in $wt" >&2
    rc=1
  elif [ "$rc" = 0 ]; then
    # origin/main may move meanwhile: replay only our own unpushed pin commit, bounded.
    local push=1
    until git -C "$wt" push -q origin HEAD:main; do
      [ "$push" -ge 3 ] && { echo "  ⚠ pin commit push rejected $push times" >&2; rc=1; break; }
      git -C "$wt" pull -q --rebase origin main || { rc=1; break; }
      push=$((push + 1))
    done
    [ "$rc" = 0 ] && echo "  pushed to replypen main: $(git -C "$wt" log -1 --oneline) → ship it: scripts/promote.py --from main --to stable"
  fi
  git -C "$prod" worktree remove --force "$wt"
  return "$rc"
}

PROD_BUMP_FAILED=0
if [ -n "$RELEASE" ] && [ "$NO_PROD_BUMP" != 1 ]; then
  if [ "$NO_PUSH" = 1 ]; then
    say "Skip prod pin bump (--no-push: $TARGET is not published, so the pin could not resolve)"
  else
    say "Bump the prod (replypen) workspace pin to $TARGET"
    prod_bump "$TARGET" || PROD_BUMP_FAILED=1
  fi
fi
if [ "$PROD_BUMP_FAILED" = 1 ]; then
  echo >&2
  echo "❌ $TARGET is published, but PROD IS STILL ON THE OLD PIN — see the warning above." >&2
  echo "   Until replypen/runtime/{Dockerfile,requirements.lock} move to $TARGET and get promoted," >&2
  echo "   every run in prod keeps executing the previous lib bytes." >&2
  exit 1
fi
echo; echo "✅ done — brains now run $TARGET"
echo "   publish a brain from its checkout (installed kit, never this source checkout):"
echo "   uv run --no-project python .agents/skills/brain-publish/scripts/brain_publish.py --project <p>"
