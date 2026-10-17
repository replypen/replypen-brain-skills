# rootcause-brain-skills

One external-developer kit to iterate on a project's **brain** locally and verify production behavior
through public `rc`/API surfaces. No RootCause private source, host credentials, SSM, or infrastructure
shell is required.

A brain is `rootcause-org/rootcause-brain-<project>`: committed markdown knowledge, playbooks,
grounding scripts, tests, projection templates, and optional actions. Production mounts committed
brain refs read-only at `/brain`; local `.env`, installed skills, and `.rootcause/` artifacts never
reach a run. Source mirrors (`/mirrors/<name>`), optional knowledge-base sync (`/kb`), live grounding
data, and actions are separate runtime inputs. Start with [docs/brain-model.md](docs/brain-model.md).

Cross-project file-format skills (xlsx/csv/pdf) are mounted read-only at `/skills` in every run
container — don't reinvent file parsing in brains. Their libraries (openpyxl, pandas, pypdf,
pdfplumber, fpdf2) ship in the workspace image, so brain scripts can import them directly.

The public `rc` CLI belongs to **local brain development only**. The production main LLM loop has
`bash` plus its scenario terminal tool (`reply` for email), no `rc` binary, and a read-only `/brain`.
Its grounding path is `/brain` scripts plus the project/run's injected `lib.*` capabilities. Never
copy CLI command guidance into committed project-brain instructions.

## Install

From a brain checkout:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rootcause-org/rootcause-brain-skills/main/install.sh)
SKILL="${RC_BRAIN_KIT:-$HOME/.rootcause-brain-skills}/skills/local-brain-work"
uv run "$SKILL/scripts/brain_run.py" --brief
uv run "$SKILL/scripts/brain_test.py" --live
```

Do not install this kit as a user/global Codex or Claude Code plugin. Brain Dev skills are
brain-repo-relative: install them from inside each `rootcause-brain-*` checkout so agents discover
exactly one repo-local copy, scoped to that brain. User/global installs make the same skills appear in
unrelated projects and can drift from the pinned brain checkout.

The installer makes `.agents/skills/` the canonical Codex discovery tree. `.claude/skills` is a
**committed** relative symlink to it (`.claude/skills -> ../.agents/skills`): Claude Code only
discovers `.claude/skills`, and a `git worktree` carries tracked files only, so an uncommitted alias
means "Unknown command" for every brain skill inside worktrees. Run `git add .claude/skills` once per
brain. The kit-installed skills the alias exposes stay ignored locally through `.git/info/exclude`, so
setup never modifies the brain's tracked `.gitignore`.

The alias therefore dangles in a fresh checkout (its target is a local, ignored install): prod skips
dangling relative in-repo symlinks when building a run view, and brain lint only WARNs. Ignored kit
paths do not follow a worktree — the installer lists them in the repo-root `.worktreeinclude`
allowlist; otherwise re-run `install.sh <worktree-dir>` inside the worktree.

Full walkthrough: [docs/onboarding.md](docs/onboarding.md).

## Canonical Skills

| Skill | Job |
|---|---|
| `local-brain-work` | Map a brain, run local/live/docker tests, projection, mirror checks, and action dry-runs. |
| [`brain-dream-cycle`](skills/brain-dream-cycle/SKILL.md) | Full local dreamcycle pass from a brain checkout: mine feedback/sent deltas/patterns with `rc`, then update brain files plus persona/triage settings. |
| [`brain-harvest`](skills/brain-harvest/SKILL.md) | Full local harvest pass: trigger a production sent-history export, download the cleaned corpus, fan out per-topic subagents to distil patterns (privacy-linted), decide brain/persona/triage homes, verify, publish, then delete the corpus. |
| [`brain-website-scout`](skills/brain-website-scout/SKILL.md) | Broad local public-site map and Firecrawl capture, followed by per-topic synthesis into a progressive-disclosure brain; raw pages remain gitignored. |
| [`brain-ui-targets`](skills/brain-ui-targets/SKILL.md) | Inventory the project app's click targets for chat-widget guidance: annotated help-center screenshots, render-layer source map, local DOM crawl, stable selectors, minimal `data-rc-guide` hooks. |
| [`brain-kb-mirror`](skills/brain-kb-mirror/SKILL.md) | Re-pull a public help center (Zendesk) into a committed Markdown + image snapshot under `knowledge/` from a per-brain source profile, describe new screenshots and summarize video transcripts with cheap sub-agents, check, publish. |
| `brain-ask` | Last-mile production-loop `rc ask` validation, usually against a pushed `dev/*` ref. |
| `rc-debug` | One run/thread/session to trace/debug/index/JSONL drilldown; analysis-first before edits. |
| [`rc-mailbox-cockpit`](skills/rc-mailbox-cockpit/SKILL.md) | Independently check complex Google-mailbox drafts and surface neglected work in a concise daily cockpit; optional guarded draft corrections. |
| `rc-health` | Stale mirrors plus dead-lettered runs. |
| `rc-fleet` | Recent fleet digest plus recurring failure patterns. |
| [`brain-helper-devloop`](skills/brain-helper-devloop/SKILL.md) | The loop for adding or changing a brain/mirror helper: design from real tickets, build read-only and verdict-first, prove on real data (`mirror-try`, `scope-check`), push a dev ref, ask synthetic admin questions with `rc ask`, read each trace for unprompted pickup, fix developer experience instead of adding prompt rules, publish, re-ask live. |
| [`brain-chat-debug`](skills/brain-chat-debug/SKILL.md) | One bad chat answer from a share/run link or session id: one command gathers transcript, feedback, principal and every run's trace into a case file; decide the owning layer; replay the turn (chat-faithful when `rc` supports it) on a `dev/*` brain/mirror ref and compare old vs new; widen to other tenants via devloop. |
| [`mirror-try`](skills/mirror-try/SKILL.md) | Compare mirror helper revisions on real scoped data without changing the live mirror. |
| [`brain-fleet-report`](skills/brain-fleet-report/SKILL.md) | Daily investigation: collect evidence, judge owner and technical findings, publish rolling review queues. |
| [`brain-simulate`](skills/brain-simulate/SKILL.md) | Replay ~10 representative real inbound cases through `rc ask --simulation`, grade each draft against the human's real reply (content, routing, tone, format, safety), render a self-contained HTML report with run links and copy-paste steering prompts; diff a `dev/<branch>` brain against `main`. |
| [`brain-helpcenter-suggestions`](skills/brain-helpcenter-suggestions/SKILL.md) | A window of real customer questions (email runs, chat runs, Help Scout) vs the public help centre: collect the corpus and every `/kb` article body, classify and cluster the gaps, render a ranked, evidence-backed edit list (new / rewrite / retitle / merge / delete / add-alias), each edit anchored in the current article, with verbatim quotes, a link per conversation and a bot-readable block per card. |
| [`brain-grounding-intake`](skills/brain-grounding-intake/SKILL.md) | A customer's code and database taken in together from the production run's chair: what a run receives, a bounded repo scan and schema probe, the last window of real customer questions benchmarked into grounded / ambiguous / missing / knowledge / human, safe data drills, and a self-contained questionnaire for the customer's developer (impact per question, the real tickets behind each, one copy-as-markdown button) whose answers become `skills/codebase/` and `skills/databases/<db>.md`. |
| [`brain-helpcenter-publish`](skills/brain-helpcenter-publish/SKILL.md) | Apply one approved help-centre suggestion to the live provider: save its `replypen: helpcenter/v1` block, dry-run the provider request, then `rc project knowledge article apply` writes the article (draft by default, `--publish` to go live) and re-syncs `/kb`. |
| [`scope-check`](skills/scope-check/SKILL.md) | Principal-scoped projects: generate the per-audience table/column visibility matrix (`✓` / row-constrained / `hidden` / absent) and run the brain's helpers as every real identity in a production sandbox, as a pass/fail matrix. |
| [`rc-script-wrapper`](skills/rc-script-wrapper/SKILL.md) | Deterministic local Python/shell wrappers for `rc` console queries, exports, typed failures, and remote artifacts. |
| `brain-dev-upgrade` | Update local kit and `rc` CLI. |
| [`brain-git-sync`](skills/brain-git-sync/SKILL.md) | Safely inventory, merge, and push local plus cross-computer work to `origin/main`. |
| [`brain-publish`](skills/brain-publish/SKILL.md) | One command (`scripts/brain_publish.py`): checks, git sync, tenant preflight, edge gate, exact-SHA publish, proof; plus support handoff. |

Older duplicate entrypoints are not shipped; use the canonical skills above.

## Common Recipes

| Need | Where |
|---|---|
| Register a new grounding database ("the tables are missing", add a database, new DSN) | [docs/secrets.md](docs/secrets.md#register-a-new-grounding-database) |
| Add or rotate a grounding / action credential | [docs/secrets.md](docs/secrets.md) |
| Set up or verify developer escalation tickets (ClickUp) | [docs/escalation.md](docs/escalation.md) |
| Understand a run trace | [docs/run-trace-model.md](docs/run-trace-model.md) |

## Side Effects

Diagnosis is read-only by default. Test-run creation and action execution are explicit exceptions.
Details: [docs/side-effects.md](docs/side-effects.md).

| Surface | Side effect |
|---|---|
| `brain_run.py`, `brain_test.py`, `rc run list/show/events/trace/debug/brain-diff/thread`, `rc fleet runs/patterns/health`, `rc status` | Read-only. |
| `rc project mailbox harvest` | Creates a production export job (provider sweep → stored corpus). |
| `rc project corpus download` | Marks the export consumed (eviction grace) and lands raw mail on local disk. |
| `rc project mailbox imap-env` | Writes IMAP/SMTP credential material to a local 0600 env file. |
| `local_imap_harvest.py` | Connects to IMAP from the laptop and writes a raw local sent-history corpus. |
| `brain-website-scout` plan | Calls Firecrawl map and same-domain public discovery endpoints; may consume Firecrawl credits and writes only gitignored local artifacts. |
| `brain-website-scout` scrape/run | Opens the approved public-page selection through Firecrawl, consumes page/proxy credits, and writes split gitignored local captures. |
| `brain-kb-mirror` refresh | Reads a public help center (≤2 req/s, allowlisted hosts) and rewrites the mirrored corpus under the brain's `knowledge/`; committed only when you commit. |
| `brain-kb-mirror` video-transcribe | Reads public video metadata (yt-dlp, no download) and sends public YouTube URLs to the Gemini API; costs tokens (estimate printed first, `--yes` to run all). |
| `rc ask` against `main` | Creates a real production run; may create draft/journal/test artifacts. |
| `rc ask --brain-ref dev/<branch>` | Creates a test run; no callback or durable journal push; proposals are test artifacts. |
| `rc run feedback` / `rc run retry` | Writes learning feedback / creates a replacement production run. |
| `rc dev brain publish\|promote --channel <channel> --sha <sha>` | Moves a shared project brain channel for all its runs/tenants; project-maintainer only and audited. |
| Action proposal | LLM proposes only; no mutation. |
| Action confirmation / public dev-trigger when exposed | Real mutation path. |
| `brain_action.py --commit` | Local real write to whatever `.env.action` targets. |

## Fidelity

- `uv` mode is the fast inner loop. It pins Python/runtime deps and uses only the brain's `.env`, but
  it does not reproduce read-only mounts, container isolation, OS behavior, or production egress.
- `docker` mode uses the published workspace image and read-only mounts. It still does not prove the
  production egress allowlist.
- `rc dev console database` / `rc dev console bash` are the preferred production debugging path for exact SQL, scripts, logs, and
  tool parity; they are much faster than wrapping the check in an LLM run.
- `rc ask --brain-ref dev/<branch>` is the full production-loop confidence check without moving live
  refs.
- `rc dev brain sync` refreshes the deployed brain cache from `origin/main` and invalidates warm console
  workspaces.
- `rc dev brain publish --channel stable|edge --sha <exact-full-40-character-sha>` (rc >= 1.16.5) is the
  preferred one-shot sync + promote + verify for a shared project channel; `rc dev brain preflight`
  dry-runs it per tenant first. Channel questions need `--scope project`; always pass `--project`.
  Verify the resolved channel SHA in status or a normal run before claiming success.

### `lib.db` query limits and diagnostics

Hosted runs inject the project/tenant query limit as `RC_DB_QUERY_TIMEOUT_SECONDS`; `lib.db` uses it
as both the default and maximum for every read-only transaction. Standalone use keeps the 30s default
and 120s maximum. Postgres cancellation (`57014`) becomes a concise rewrite hint with best-effort
catalog evidence for referenced tables; the CLI prints it without a traceback and exits 1.

```python
from lib import db

db.table_stats("accounts", db="ruby")  # size, row estimate, scans, indexes, analyze + column stats
db.explain("select * from accounts where email = %s", ["a@b.test"], db="ruby")  # no ANALYZE
```

CLI equivalents: `python -m lib.db --stats accounts --db ruby --format json` and
`python -m lib.db --explain --db ruby "select * from accounts where email = 'a@b.test'"`.

## Docs

| Path | What |
|---|---|
| [docs/brain-model.md](docs/brain-model.md) | Brain boundary, `include_in`, `.replypenignore`, layout, mounts, and refs. |
| [docs/run-trace-model.md](docs/run-trace-model.md) | How to read `rc run debug` index/JSONL. |
| [docs/mirrors.md](docs/mirrors.md) | Source mirrors and freshness/debug rules. |
| [docs/knowledge-base.md](docs/knowledge-base.md) | Traverse `/kb` and committed `knowledge/`, including frontmatter search from `rc dev console bash`. |
| [docs/support-boundary.md](docs/support-boundary.md) | Brain-change vs RootCause-support decision tree. |
| [docs/actions.md](docs/actions.md) | Action plane and local hosted-Python action tests. |
| [docs/rc-cli.md](docs/rc-cli.md) | Public `rc` CLI reference for this kit. |

## Single Version Line

The plugin versions, `rootcause-runtime` pin, workspace image tag, and production runtime pin move
together; see [RELEASING.md](RELEASING.md). Current line: **`v0.12.6`**.

- Runtime pin:
  `rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.12.6#subdirectory=runtime`
- Workspace image: `ghcr.io/rootcause-org/workspace:v0.12.6`

Check coherence:

```bash
./check-release-coherence.sh
```

## Develop

```bash
# PYTHONPATH first: `--with .` installs a BUILT copy of runtime/, so without it a run can
# import stale bytes (uv's cached wheel, or a same-second .pyc) and go green on old code.
cd runtime && PYTHONPATH=$(pwd) uv run --with . --with pytest --no-project pytest tests -q
```
