---
name: rp-fleet
description: "Review many production runs at once from a brain checkout — `rp fleet runs` for the digest and human-score audits, `rp fleet actions` for cross-run action history, `rp fleet patterns` for recurring failures. Use for periodic run health, review-score audits, worst-offender triage, or 'something is off and I have no UUID yet'."
---

# rp-fleet — recent run digest

The entry point when no single run is known yet. Flags and windows: `rp fleet <cmd> --help` and
[docs/rp-cli.md](../../docs/rp-cli.md). Public `rp` only, scoped by the OAuth token and brain metadata
([docs/support-boundary.md](../../docs/support-boundary.md)); `--project` needs an all-projects token.
Ignore an installed ReplyPen MCP unless the user asks for it.

## Required Context

[docs/mirrors.md](../../docs/mirrors.md) when failures involve source freshness ·
[docs/side-effects.md](../../docs/side-effects.md) before interpreting action statuses.

## 1. Digest — `rp fleet runs`

Pass through any supplied `--days` / `--kind`. `--format agent` is the token-lean shortlist. The
tool prints its own flag legend; read it rather than memorising one — including `T!` (turns far above
the same-kind median), the heaviness signal.

**The one trap: `--reviewed` ≠ `--learning`.**

- `--learning[=feedback|sent_delta|triage_skipped|triage_corrected]` = runs a dream cycle can learn
  from.
- `--reviewed` = every run with a 1–5 human score, *including held-out evaluation threads that
  learning deliberately excludes*. JSON rows carry `review.score` / `review.comment`.

When the request says "scored" or "reviewed", use `--reviewed`. Substituting `--learning=feedback`
silently drops the eval set.

"What did users thumb down this week?" → `rp run feedback list --score negative --kind chat --days 7`
(windowed on the rating time, so a 👎 today on an old conversation shows; comment + answer preview
inline, drill with `rp run debug <run_id>`).

## 2. Actions across runs — `rp fleet actions`

Use before drilling individual traces: it returns exact grounded params plus a freshly tokenized
`run_url` per row, so it answers "what did we actually do, with what arguments" in one call. Repeated
`--action` / `--status` are exact-match ORs; results page automatically — if the client warns its
page cap was hit, narrow the window rather than trusting the partial tail. The server clamps `--days`
above 14.

**Privacy constraint (params can hold customer values):** the feed needs `console:action` plus
action-view authority. Minimize filters, never commit raw output, never share the tokenized URLs.

Reading a row:

- `proposed` = recorded, never executed. `executing` = non-terminal. `succeeded|failed|canceled` =
  terminal.
- A `failed` row settles into a `CLASS` + one-line `Error:` (`-o json` keeps both whole).
  **Classify before drilling:** infra classes are ReplyPen's machinery failing (report, do not edit
  the brain); anything else is the action's own domain refusal —
  [docs/actions.md](../../docs/actions.md#failure-classes-infra-vs-domain).
- There is no action-detail command. For result/preflight context use the row's `run_id` with
  [`rp-debug`](../rp-debug/SKILL.md), or open `run_url`.

## 3. Drill and close out

Drill two to five flagged runs with [`rp-debug`](../rp-debug/SKILL.md).

`LRN:sent_delta` marks a live human edit; `LRN:sent_delta/<verdict>` a blind shadow comparison
(`/unjudged` = no verdict yet, `equivalent` = positive evidence, not a failure). **Fleet never carries
the two bodies** — for the proposal vs. the human answer, switch to
[`brain-dream-cycle`](../brain-dream-cycle/SKILL.md), which reads them from `rp dev learning evidence`.

For the recurring daily pass — one project, a fixed day, a ranked "what do I fix today" report for
both you and the product owner — use [`brain-fleet-report`](../brain-fleet-report/SKILL.md) instead of
driving these commands by hand.

Mark feedback you acted on processed (`rp run feedback <id> --processed --resolution-note …`;
project-admin only) so the next review starts from the unprocessed remainder.

## 4. Confirm systemic failures — `rp fleet patterns`

Each ranked cluster is a candidate brain fix: a missing runbook, a wrong query, or a domain to
allowlist. Author from evidence, verify with `brain-ask`, finish through `brain-publish` when files
changed.
