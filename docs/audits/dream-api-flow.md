# Dream API / CLI Flow Audit

Date: 2026-07-05.

Command names below use the current nine-root CLI. The original ambient-scope failure was fixed by
brain-marker project resolution plus local selector validation; explicit `--project` remains useful for
an all-projects token outside a bound brain checkout.

Scope: `rp dev learning evidence`, the run/status/observability ladder, account-level surfaces, and triage-skip
restart flow. Reviewed `replypen-brain-skills`, `replypen-cli`, and the modern `replypen` backend;
sampled production with explicit `--project` for `kampadmin`, `dentai`, and `player1labs`.

## Recommendation

Keep `GET /api/v1/dream/evidence` / `rp dev learning evidence`, but narrow its contract: it should be the
dream-cycle inbox, not the general account/status view.

Best flow:

```text
1. rp status / rp fleet runs
   Decide whether the account is healthy and which run classes matter.

2. rp dev learning evidence --limit N
   Pull ranked learning candidates: explicit feedback, sent-edit deltas, and triage corrections.

3. rp run show <id> / rp run thread <id> / rp run debug <id>
   Drill one candidate only when it can justify a brain/settings/triage change.

4. rp project settings behavior get + rp project triage policy/rules + rp project mailbox ls + rp dev brain status
   Decide durable home and deployment risk before editing.
```

Do not fold dream evidence fully into `rp status`. Status is privacy-safe account health; dream evidence
is operator/developer learning input and can include raw proposed/sent bodies. Mixing them would either
weaken status privacy or make dream evidence too shallow.

Do improve the progressive-disclosure ladder around it:

- Add a safe `learning_signal` summary to `GET /api/v1/runs` rows: `feedback`, `sent_delta`,
  `triage_correction`, maybe counts/booleans only. This lets `rp status`/`rp fleet runs` point to dream
  candidates without carrying bodies.
- Add `rp dev learning evidence --summary` or make table mode useful. Today the command always prints JSON.
  A compact table should show plane, run id, tenant, score/similarity, changed chars, topic, and body
  lengths.
- Split body detail from index: default dream evidence should omit `proposed_body`/`sent_body`; add
  `--include-bodies` or a detail endpoint such as `GET /api/v1/dream/evidence/{id}`. Current raw bodies
  are useful for consolidation but too heavy for first-pass agent routing.
- Keep explicit `--project` in operator examples outside a brain checkout. Inside a brain checkout,
  `.replypen.toml` now supplies the project and the CLI validates it before requesting evidence.

## Live Endpoint Results

All samples used explicit `rp --project <project> ...`.

| Project | Dream evidence | Status / fleet | Account surfaces |
|---|---:|---|---|
| `kampadmin` | `0` feedback, `20/20` deltas. Deltas are Embassy-sourced, high divergence, but top rows have no `related_run_id`. | `232` fleet rows: mostly `analysis`; `124` failed, `5` error, `38` fallbacks, `130` no-journal. `patterns` saw `24` `grounding_aborted` rows and blocked `admin.kampadmin.be`. | `54` active tenants, no watched mailboxes via `rp project mailbox ls`, brain current. |
| `dentai` | `1` feedback, `20/20` deltas. Dense Google sent-edit corpus; some deltas have no related run. | `230` fleet rows: `20` errors, `17` failed, `21` fallbacks. Recent status unhealthy with `dead_lettered` + internal errors. | `11` tenants, `5` watched mailboxes, all feedback `off`; one force-process triage rule. Brain cache stale: origin/main ahead by 1. |
| `player1labs` | `4` feedback, `4` deltas. Feedback is the clearest: low scores with long comments and actionable topics. | `89` fleet rows: `49` failed, `5` dead-lettered, mostly email. Recent failed rows often have zero turns/bash, suggesting pre-loop or synthetic failures. | `1` watched mailbox, feedback `all`; brain current; `2` actions. |

Observed rough merits:

- `dream evidence` has strong signal density. For Player1Labs, it beats status/fleet immediately.
- For DentAI, deltas are abundant but body-heavy; first-pass agents need a summary/digest more than raw
  text.
- For Kampadmin, Embassy deltas without `related_run_id` are hard to drill. Session/thread join is not
  enough for `rp run debug <id>`.
- `patterns` and `fleet` expose systemic run health better than dream evidence: grounding aborts,
  fallbacks, no-journal, blocked egress, and dead-lettering do not belong inside the dream endpoint.

## Current Architecture Read

Relevant files:

- `skills/brain-dream-cycle/SKILL.md`: current external workflow.
- `docs/rp-cli.md`: public CLI contract.
- `replypen-cli/SKILL.md`: endpoint ladder and fat-client doctrine.
- `replypen/internal/api/dream_evidence.go`: dream endpoint.
- `replypen/db/queries/{run_feedback,sent_messages}.sql`: ranking rules.
- `replypen/.agents/skills/features/{runs-index-api.md,status_page.md,observability-api.md}`: status/run/feed design.
- `replypen/internal/pipeline/steps/triage.go`: triage skip and feedback-run materialization.
- `replypen/internal/web/customer/retry.go`: “Answer this now” correction flow.
- `replypen/internal/api/run_actions.go`: API feedback/retry surface.

The backend design is coherent:

- `GET /api/v1/runs` is the status-page JSON twin: safe projection, shared `runindex.Enrich`, no raw
  bodies.
- `GET /api/v1/run-events`, `/egress-log`, `/health` are raw feeds for fat CLI aggregation.
- `GET /api/v1/dream/evidence` is not a raw feed; it is a ranked bundle over two learning planes:
  negative/commented feedback and proposed-vs-sent deltas.

That specialization has merit, because dream evidence cuts across tables (`run_feedback`,
`sent_messages`, `runs`) and ranks by learning value, not operational severity.

## Gaps

1. Ambient project scope was risky in the audited version.
   `rp dev learning evidence` returned `NO_PROJECT_SCOPE` for `kampadmin` in its brain checkout and a
   wrong-project result for `player1labs` before using explicit `--project`. The current CLI resolves
   `.replypen.toml`, validates project selectors, and rejects unsupported/mismatched scope locally.

2. Dream evidence is too body-forward for index use.
   It returns raw `proposed_body` and `sent_body` by default. That is useful for final consolidation, but
   not for the first pass and not consistent with the status-page privacy posture.

3. Missing run joins reduce drillability.
   Kampadmin Embassy deltas are high signal but `related_run_id` is null. Add `session_id`/thread trace
   drill instructions, or improve Embassy `ResolveRelatedRun` attribution.

4. Triage-skip learning is only indirectly present.
   The product can materialize skipped mail as synthetic done email runs when mailbox `feedback_level`
   is `triage`/`all`, and the HTML run page can start processing via “Answer this now”. But dream
   evidence only sees it if someone writes run feedback. It does not expose triage skip volume,
   categories, correction rates, or skip-to-start outcomes.

5. API retry is not equivalent to the HTML triage correction.
   HTML triage correction accepts a required comment, saves it as feedback, and enqueues a normal
   standard-flow retry. `POST /api/v1/runs/{id}/retry` supports tier escalation for replayable email
   runs, but not the triage-correction comment path. CLI agents cannot cleanly “start this skipped mail”
   with the same semantics.

6. `failed` with `category=ok` is hard to interpret from status alone.
   Player1Labs has many `outcome=failed`, `category=ok`, zero-turn rows. This is explainable from the
   “guardrail gap”/result-verdict model, but a first-pass agent needs a short failure reason or
   `metadata.outcome`/`failure.reason` glimpse in the run index, gated like `declined_reason`.

## Suggested API / CLI Changes

### Small, high-value

- Use `rp dev learning evidence --project <slug>` in all-projects operator examples outside a bound brain.
- Add table rendering for `rp dev learning evidence`.
- Add `--plane feedback|deltas|triage`; tenant scope is already available through the global
  `--tenant <slug>` selector.
- Add `--include-bodies`; omit raw bodies by default.
- Add `related_run_missing: true` and `drill_hint` when a delta has session/thread but no run id.
- Add `rp dev learning evidence --since 30d`; current endpoint has only `limit`.

### Triage skip signal

Add a third dream plane:

```jsonc
{
  "triage": [
    {
      "run_id": "...",              // synthetic feedback run when present
      "thread_id": "...",
      "tenant": "de-kies",
      "category": "bulk|auto|blocked|...",
      "reason": "safe short reason",
      "deterministic": true,
      "corrected": true,
      "retry_run_id": "...",
      "feedback_comment_present": true,
      "created_at": "..."
    }
  ]
}
```

Source it from synthetic feedback runs (`metadata.outcome=triage_skipped`) plus retry links, not raw
mail bodies. This gives the dream cycle “was skipped, human said process, then it started” without
opening private content.

CLI:

```bash
rp dev learning evidence --plane triage --limit 50 -o json
rp run list --kind email --outcome failed      # if outcome filter is added
rp run retry <triage-run-id> --comment "why this should process" # if comment is added
```

API:

- Either extend `POST /api/v1/runs/{id}/retry` with `{comment, triage_correction:true}` for
  `metadata.outcome=triage_skipped`.
- Or add `POST /api/v1/runs/{id}/process-skipped` as a clearer verb.

### Status / run index improvements

Keep status safe, but add pointers:

```jsonc
"learning": {
  "feedback": true,
  "sent_delta": true,
  "triage_skipped": false,
  "triage_corrected": false
}
```

Optionally add filters:

- `rp run list --outcome failed|declined|answered`
- `rp run list --learning feedback|sent_delta|triage`
- `rp fleet runs --learning` to print only rows with learning candidates.

This makes the status page/index the navigation root while keeping the dream endpoint as the evidence
detail.

## Bottom Line

Dedicated dream endpoint: yes. It has full merit as a curated learning inbox.

But do not make it the first account overview. The best architecture is:

- status/fleet = safe account health and candidate pointers;
- dream evidence = ranked learning candidates, body-light by default;
- run/thread/debug = exact proof;
- config/triage/mailbox/brain status = durable-home and deployment context.

The most important missing piece is triage skip correction telemetry. Player1Labs proves mailbox
feedback can create the signal; the API/CLI should expose it as a first-class dream plane and provide a
non-HTML “start this skipped mail” path.
