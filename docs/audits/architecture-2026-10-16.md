# Architecture audit: rootcause-brain-skills (2026-10-16)

Intent: `AGENTS.md`, `RELEASING.md`, `docs/brain-model.md`, each `skills/*/SKILL.md`. Scope: churn since
2026-10-02 (kb-mirror, chat-debug, website-kickstart, cockpit, devloop, brain_lint, gdocs/gsheets,
local-brain-work, git-sync, publish, release plumbing). Not timed: Postgres-backed tests
(`FLEET_TEST_DSN`, `test_db*` integration), which skip without infra. Second opinion: Astra (two
rounds, run independently, then compared). Code review: Sol High.

> [!IMPORTANT]
> The root finding: **nothing ran the tests.** There is no CI, the release ran only the
> version-coherence check, and no single command could run the skill tests. Three tests had gone
> stale or flaky without anyone noticing. Releases now gate on the tests.

## Shipped

| # | Fix | Evidence | Impact |
|---|---|---|---|
| 1 | `lib.api` manifests: libyaml `CSafeLoader` + `load_manifests()` parses each manifest once (it re-parsed every YAML key per call, against its docstring; reported by the DentAI-brain audit lead) | 38,963 `safe_load` calls = 82s of a 106s run; output identical on all 91 manifests | runtime suite **106–215s → 7s**; DentAI availability search paid ~1s per month window in prod, first call now 15ms, repeats ~1ms |
| 2 | Release test gate: `scripts/test-skills.sh` always, `runtime/` suite when its digest moved, both run on the merged tree via a brain_git_sync verify-command | v0.12.2 shipped with a red test | broken lib can no longer auto-bump the prod pin |
| 3 | `scripts/test-skills.sh`: one pytest process per skill | a bare `pytest skills` stops at collection: duplicate module names (`schema`, `corpus`, `query`) and `brain_test.py` matching `*_test.py` | 390 skill tests runnable in one command (~95s) |
| 4 | `--no-push` no longer pushes in the initial reconcile | `refresh-brains.sh` called brain_git_sync before checking `NO_PUSH`; the plumbing test proved a local-only commit reached origin | release rehearsals are safe (found by Astra) |
| 5 | `brain_paths._siblings` skips `$TMPDIR` | globbed 13k temp entries for every git_land worktree and test repo | local-brain-work tests **145s → 7s**, brain-publish 102s → 30s, ~8s off each git_land hook run |
| 6 | `gsheets.find_rows`: a missing multi-letter header ("SKU") raises `LookupError` | it was parsed as column letter 13150, so the search silently returned `matches: []` | stops "no such row" answers to customers |
| 7 | devloop: `rc` timeout becomes `DevloopError`; resuming refuses when question/tenant/refs/scenario/simulation changed | a timeout crashed the whole batch; changed inputs got credited to an old run (Astra) | validation evidence stays trustworthy |
| 8 | Stale/flaky tests | brain_lint entrypoint (the script now re-execs via uv); plumbing test checked for a message removed in `b7ca666`; brain_publish race between the fake rc's advance and the parallel `status` call | green suite is real |
| 9 | Retry-backoff sleeps patched out in the newrelic/mollie tests | 14s of real sleep | |
| 10 | KampAdmin-private repo paths in `brain-chat-debug`/`brain-helper-devloop` SKILL.md replaced with placeholders | kit audience is external developers | |
| 11 | brain_lint `script-outside-skills` skips run-hidden code (`.replypenignore`); scope_matrix "Regenerate" prints cwd-relative `--out` (both reported by the KA-common audit lead) | KA-common `action-authoring/` generators (rooted in `.replypenignore`) were a permanent 3 FAIL; committed matrices leaked `/Users/` paths | KA-common lint 3 FAIL → 0; gate trusted again |

Total test wall time **~515s → ~100s**. Measured on a loaded shared Mac, so absolute numbers are noisy;
the ratios hold.

## Needs PJ's green light (ranked)

1. **Move `website-kickstart` out of the kit** into rootcause `.agents/skills/`.
   - Why: it is operator-only but installs into every external brain (`install.sh:319`). It exposes the
     Cloudflare account id (`kickstart_site.py:42`), private host checkout and env paths (`:174`), and
     a DentAI repo path (`SKILL.md:48`).
   - Effort: ~0 LOC of code; installer cleanup plus a doc move.
   - Why approval: cross-repo, and it changes the canonical skill list.
2. **One write-path contract for Python `lib.api` and the host broker.**
   - Problem: `api.py:1045` makes the `/v4`/`/v1` prefix optional, while `broker.go:205` →
     `delegated.go:87` matches the full upstream path. The manifests carry both forms
     (`googlesheets/manifest.yaml:37-50`), so every bare form is dead on the host.
   - Failure mode: the test fixture `SHEETS_CONTRACT` (`test_google_writes.py:37`) uses only the bare
     form, so a new connector copied from it passes locally and is denied in prod.
   - Fix: match on the joined upstream path, drop the bare patterns, run the same fixtures on both
     sides. About −15 LOC.
   - Why approval: it changes the write-gate behaviour in prod.
3. **Port the newrelic, linear and appsignal connectors to `api.Client.post`**, as shopify does
   (`shopify/__init__.py:51`).
   - Problem: all three hand-roll 429/5xx retry with `time.sleep` (`newrelic/__init__.py:83-96`),
     and newrelic's docstring wrongly says retry is "owned by lib.api".
   - Effort: ~−90 LOC.
   - Must preserve: regional URLs, transport retries, audit events, GraphQL error handling.
   - Follow-up: a guardrail banning `time.sleep` under `runtime/lib/connectors/` after the port. All 7
     current hits are true violations.
4. **Shared local `rc` transport** in `skills/_shared/rc_cli.py`.
   - Problem: at least 9 copies with divergent timeout and JSON handling (`devloop.py:108`,
     `chat_case.py:276`, `mirror_try.py:76`, `scope_common.py:80`, …). Trace-step parsing is also
     duplicated (`chat_case.py:198` vs `devloop.py:228`).
   - Plan: pilot on 2 consumers first. Keep retry, cache and write policy per workflow; cockpit stays
     separate.
   - Effort: 200–350 LOC, medium risk. Astra and I agree on this one.

<details><summary>Deferred / deliberately not done</summary>

- **brain_lint has three "which markdown files" enumerations** (`brain_lint.py:398/435/816`). The
  surfaces check reads untracked `.rootcause/` scratch (1,262 files read vs 132 tracked on DentAI), so
  it risks WARN false positives. Kept narrow: limiting it to tracked files would miss newly authored
  docs (Astra).
- **doc-spill stays NOTICE + regression-only** (`brain_hygiene.py:39,100-143`). KA-common saw 7 docs grow
  back past 6 KB in four days and now runs its own ratchet test. Two open choices, both policy changes that
  would turn other brains red: make the check strict, or measure the whole file (a run's `cat` also prints
  the frontmatter). Revisit if a second brain needs the same ratchet.
- **No test cuts.** The 64 `test_no_token_prefixes_*` and 35 manifest-load clusters look alike, but
  each one guards a different directory or field set. Survivors would not cover them, and each test
  takes milliseconds.
- **No table-izing of `test_brain_structure.py`.** Its tests are fast now; the churn would outweigh
  the gain.
- **`brain_env.py` churn** (60 commits/month) is all `release:` version-line bumps, by design.
- **No import contract.** `deps.py` sees no edges because skills import each other via `sys.path`
  (publish→git-sync, devloop→scope-check, brain_structure↔brain_paths through a lazy import). Those
  imports resolve through the single kit clone, so this is fine.
- Script blind spot: hotspot ranking is inflated by release commits.

</details>
