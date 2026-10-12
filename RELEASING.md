# Releasing — the single version line

> **The standard flow is one command** — [`./refresh-brains.sh`](refresh-brains.sh). It does the
> whole table below, requires `main`, pushes and verifies the release commit at `origin/main`, then
> pushes the version tag and re-points every local brain:
>
> ```bash
> ./refresh-brains.sh --release patch            # skill/doc change → bump, main-first publish, tag/image, refresh
> ./refresh-brains.sh --release minor --relock   # deps changed → also regen the lock + REBUILD the image
> ./refresh-brains.sh --release patch --dry-run  # print the whole plan, mutate nothing
> ./refresh-brains.sh                            # no release: just re-point every brain at the newest tag
> ```
>
> **Committed work only.** The release refuses a dirty working tree (tracked edits or non-ignored
> untracked files) before touching anything, and its commit names exactly the files the bump writes —
> a colleague's WIP in a shared checkout can never ride along. Commit first; from a shared checkout
> land your own files with [`git_land.py`](skills/brain-git-sync/SKILL.md#landing-from-a-shared-checkout).
>
> The skill ships **strictly tag-pinned** (every skill change is a release), so brains only pick up a
> change once it's a pushed tag. Remote publication is deliberately **reconciled main first, tag
> second**: `brain_git_sync.py` merges cross-computer work, retests, pushes, and verifies
> `origin/main` before the release creates any tag. `--no-push` leaves the release commit local,
> creates no tag, and keeps remote refs unchanged. The image bakes `runtime/` only, so a skill/doc
> release **re-tags** the prior image instead of rebuilding — `--relock` forces a real rebuild for dep
> changes. "Did runtime change" is decided by a **normalized content digest** (version literals
> canonicalized out) recorded in the committed repo-root `RUNTIME_DIGEST`; the same file lets the host
> `promote.py` tell a byte-identical pin from a real drift. `./refresh-brains.sh --classify` prints the
> newest release's `runtime_changed=0|1` without side effects.
>
> **Prod is consumed automatically.** Step 3 runs the sibling checkout's own
> `rootcause/scripts/bump-workspace-pin.py <tag>` and commits the pin there (only
> `runtime/Dockerfile`, `runtime/requirements.lock`, `dashboard-overlay`), pushing when our commit is
> the only one ahead of `origin/main`. It bumps on *every* release, not only runtime-changing ones —
> pin == newest tag unconditionally is what kills the stranded-release class. It refuses to run if
> those paths already carry someone else's uncommitted edits (that shared checkout has parallel
> agents), and **any failure exits the release non-zero** with a banner saying prod is still on the old
> pin. `--no-prod-bump` opts out; `--no-push` skips it (the tag isn't resolvable yet). Actually
> *promoting* rootcause stays the operator's call.
>
> The rest of this file is the manual reference for what that script automates.

Local and prod must install **identical** `lib` bytes, or "green locally" stops meaning "green in
prod". One tag enforces that. To cut a release, bump **all of these together** to the new `vX.Y.Z`,
commit, then push/verify main before publishing the tag:

| What | Where | Field |
|---|---|---|
| Engine version + image tag | `skills/local-brain-work/scripts/brain_env.py` | `VERSION` (drives `DEFAULT_IMAGE`) |
| `rootcause-runtime` package | `runtime/pyproject.toml` | `version` |
| Dep lockfile (regen on any dep change) | `runtime/requirements.lock` | `uv pip compile runtime/pyproject.toml --universal --python-version 3.12 -o runtime/requirements.lock` |
| Claude Code plugin | `plugin.json` + `.claude-plugin/marketplace.json` | `version` |
| Codex plugin | `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` | `version` / `ref` |
| Docs install snippets | `README.md`, `docs/onboarding.md`, `docs/migration-rootcause.md`, `install.sh` | the `v0.1.0` literals |
| **Prod (separate repo)** | `rootcause/runtime/Dockerfile` | the `rootcause-runtime @ git+…@vX.Y.Z` pin + workspace image tag — automated by step 3, manual fallback `rootcause/scripts/bump-workspace-pin.py vX.Y.Z` |
| **Prod lock copy (separate repo)** | `rootcause/runtime/requirements.lock` | `cp runtime/requirements.lock ../rootcause/runtime/requirements.lock` (lockstep copy; the bump script re-fetches it from the tag) |

Then, when performing the steps manually, preserve the same merge/main-first/tag-second invariant.
Record the runtime digest into the release commit first — the host pin gate reads it at the tag, so a
manual release that skips it looks like a pre-migration tag and fails closed:

```bash
test "$(git branch --show-current)" = main
uv run --no-project python scripts/runtime_digest.py --worktree > RUNTIME_DIGEST  # then git add the bumped files + RUNTIME_DIGEST, commit
uv run --no-project python skills/brain-git-sync/scripts/brain_git_sync.py \
  --repo "$PWD" --max-push-attempts 4 \
  --verify-command 'SKIP_IMAGE=1 SKIP_PROD=1 ./check-release-coherence.sh'
test "$(git rev-parse origin/main)" = "$(git rev-parse HEAD)"
git tag -a -m vX.Y.Z vX.Y.Z  # only after verified origin/main
git -c push.followTags=false push --atomic origin \
  HEAD:refs/heads/main refs/tags/vX.Y.Z  # main must still contain the tagged commit
docker build -f docker/Dockerfile -t ghcr.io/rootcause-org/workspace:vX.Y.Z . && docker push ghcr.io/rootcause-org/workspace:vX.Y.Z
```

Then re-point prod and follow [docs/migration-rootcause.md](docs/migration-rootcause.md).

**Why one line:** the engine resolves `lib` from the sibling `runtime/` when present (offline,
canonical bytes), else `rootcause-runtime @ git+…@vX.Y.Z`. Both must be the same bytes prod's image
bakes. Never float `main` — a push would silently change `lib` under a green local test.

**Why the lockfile:** the constraints in `pyproject.toml` bound the *direct* deps but intentionally allow
compatible upgrades; their transitive tail (botocore, urllib3, certifi, …) would otherwise float as PyPI moves, so two installs days apart
could differ. `runtime/requirements.lock` (universal, Python 3.12) freezes the **full** closure. uv
mode installs from it (`--with-requirements`) and the workspace image constrains to it
(`docker/Dockerfile`, `-c …/requirements.lock`) — same closure both ends. Regenerate it whenever you
change a dependency, in the same commit. Prod (`rootcause/runtime/Dockerfile`) constrains to a
**lockstep COPY** of this file at `rootcause/runtime/requirements.lock` (vendored, not fetched
from the tag, so its build is hermetic and can't break when a tag predates the lock) — re-copy it
there in the same release so prod's box matches byte-for-byte.
