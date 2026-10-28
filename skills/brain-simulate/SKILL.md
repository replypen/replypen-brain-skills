---
name: brain-simulate
description: "Measure how well a ReplyPen brain behaves today: replay representative, deliberately hard real inbound cases through the production prompt API (`rp ask --simulation`), grade each draft against the human's historical reply on content and tone, and render a self-contained HTML report with run-trace links and copy-paste steering prompts. Use for 'how good is the brain now', a before/after check of a dev/<branch> brain, to find which cases are blocked on grounding vs brain/persona gaps, or to PROVE a brain fix on a seeded fixture dossier when the real thread is retention-scrubbed or its record already changed (fixture cases with deterministic action/draft checks)."
---

# brain-simulate — replay real cases, grade the drafts, steer the brain

Run from a project or tenant brain checkout. Where [`brain-ask`](../brain-ask/SKILL.md) validates
one question and [`brain-fleet-report`](../brain-fleet-report/SKILL.md) judges yesterday's real
traffic, simulate replays a **chosen set of historical cases** so the same cases can be re-run after
a brain change and diffed. Python replays and renders; **you select, judge and recommend**.

Public `rp` only. Every replay is a real production run: always `--simulation` (real brain and
grounding, no action executes, no journal commit, nothing placed in a mailbox —
[docs/side-effects.md](../../docs/side-effects.md)). A `--brain-ref dev/<branch>` run is a test run
on top of that.

**Privacy.** Cases are real customer mail. Everything — cases, bundles, scores, the HTML — lives
under one gitignored scratch root and never enters a tracked file; recommendations describe
patterns, not quotes. The script refuses a scratch dir that `git check-ignore` does not ignore.
Same rules as [`brain-harvest`](../brain-harvest/SKILL.md) §7.

Honest scoring: a case the brain can only answer with one precise question or a handover (no DB,
no write path yet) is a **valid, gradable outcome** — `blocked-on-grounding` with a high routing
score — not a failure. The report separates those from brain/persona gaps so the owner sees what a
brain edit fixes and what needs a grounding request.

> [!TIP]
> **Proving a fix on ONE real failing run?** `rp ask --replay-run <run id> --brain-ref dev/<branch>`
> reruns that exact stored email (thread, mailbox contract, PII seed) as a simulation and diffs it with the
> original. This skill is for a *set* of cases.
>
> **Can't prove a fix on the real case?** Original thread scrubbed by retention, booking already
> cancelled, nothing to act on: don't wait for the next real mail. Replay a [fixture case](#fixture-cases)
> against a seeded dossier and get a verdict today.

## Layout

```
SKILL=<absolute path to skills/brain-simulate>
SCRATCH=.replypen/simulate/<tag>            # <tag> = local label, e.g. hs-2026-09
  cases.json acquire.json candidates.{md,json} selection.json persona.json
  runs/<ref>/<case>.json  judge/<ref>/<case>.md  scores/<ref>/<case>.json
  recommendations.json  report[-<ref>].{html,md}
.replypen/simulate/history.jsonl            # one line per (tag, ref) — trend
```

Templates: [`templates/selection-prompt.md`](templates/selection-prompt.md) ·
[`templates/judge-prompt.md`](templates/judge-prompt.md) ·
[`templates/recommendations-prompt.md`](templates/recommendations-prompt.md).

## Workflow

### 1. Acquire candidates → `cases.json`

One normalized schema (inbound turns up to the human's first reply, that reply, later turns,
metadata, content-derived id). Sources:

```bash
# Help Scout pages already pulled for a harvest (see brain-harvest/acquire.md for the pull itself)
uv run "$SKILL/scripts/simulate.py" acquire --scratch "$SCRATCH" --source helpscout --pages .replypen/harvest/<tag>/corpus
# a v3 harvest corpus from `rp project corpus download` (Gmail / IMAP / Intercom — brain-harvest/acquire.md)
uv run "$SKILL/scripts/simulate.py" acquire --scratch "$SCRATCH" --source harvest --corpus .replypen/harvest/<tag>/corpus --append
# hand-written what-ifs in the case schema
uv run "$SKILL/scripts/simulate.py" acquire --scratch "$SCRATCH" --source manual --cases what-ifs.json --append
```

Agent-first threads, threads without a human prose reply and internal test chatter are dropped;
`acquire.json` says how many and why.

### 2. Cherry-pick → `selection.json`

```bash
uv run "$SKILL/scripts/simulate.py" plan --scratch "$SCRATCH" --seed 7 --target 10
```

Read `candidates.md` (previews + hint flags) and the brain's `notes/mailbox-patterns.md` if it
exists, then choose per [`templates/selection-prompt.md`](templates/selection-prompt.md): cover the
type distribution, half or more deliberately hard, no FAQ one-liners. Persist with `select --pick
'ID|type_tag|difficulty|reason'`; `plan` re-validates an existing selection.

### 3. Run → `runs/<ref>/`

```bash
uv run "$SKILL/scripts/simulate.py" run --scratch "$SCRATCH"                      # main
uv run "$SKILL/scripts/simulate.py" run --scratch "$SCRATCH" --ref dev/<branch>   # A/B a pushed candidate
# --parallel 2 (max 3) --retries 2 --only ID,ID --force --dry-run --timeout 5m
```

Per case: `rp ask <inbound> --scenario email --simulation --subject … --from simulate-<id>@example.test -o json`.
Multi-turn inbound is embedded oldest-first in one question — `--session` is **not** used, it does
not replay prior turns ([brain-ask](../brain-ask/SKILL.md#simulating-a-customers-follow-up-reply)).
Results are re-entrant per case; a persona snapshot (`persona.json`) is taken for the judge. Runs
show up in `rp run list` as simulation prompt runs.

### 4. Score → `scores/<ref>/`

```bash
uv run "$SKILL/scripts/simulate.py" score --scratch "$SCRATCH" [--ref dev/<branch>]   # writes judge bundles
# read each judge/<ref>/<case>.md with templates/judge-prompt.md, write scores/<ref>/<case>.json
# then templates/recommendations-prompt.md → recommendations.json
uv run "$SKILL/scripts/simulate.py" score --scratch "$SCRATCH" --validate
```

You are the judge (your own session, no production LLM spend): five 0–4 scores (content, routing,
tone, format, safety), a verdict class (`pass | style-gap | knowledge-gap | blocked-on-grounding |
routing-error | unsafe`) and the lever that fixes it. Fan out over subagents when there are many
cases; one bundle per subagent, never the whole scratch dir.

### 5. Report → `report.html` + `report.md`

```bash
uv run "$SKILL/scripts/simulate.py" report --scratch "$SCRATCH"                   # main
uv run "$SKILL/scripts/simulate.py" report --scratch "$SCRATCH" --ref dev/<branch> --compare main
open "$SCRATCH/report.html"
```

Self-contained HTML (inline CSS/JS): verdict line, score tiles, class histogram, worst-first case
table expanding to inbound → human answer → our draft with the judge rationale and the run-trace
link, then recommendations grouped by lever, each with a copy button on its prompt. `report.md` is
the agent-facing twin. No cost or token figures anywhere. Hand the owner the HTML path (or attach
it to a ticket); never commit it.

### 6. Iterate

Change the brain on `dev/<branch>` (or a persona setting), push, `run --ref dev/<branch>`, judge
the new drafts, `report --ref dev/<branch> --compare main` for the per-case delta. Promote through
[`brain-publish`](../brain-publish/SKILL.md) when the delta holds; `history.jsonl` keeps the trend
per project. Grounding recommendations become a support request, not a brain edit
([docs/support-boundary.md](../../docs/support-boundary.md)).

## Fixture cases

A fixture case is a **synthetic**, committed manual case aimed at a **seeded dossier**: a fake family
with a known booking in the project's demo tenant, created by a project-side seed script. Same
pipeline as above, three extra fields:

| field | effect |
|---|---|
| `tenant` | `--tenant` for this case (the demo tenant) |
| `from` | sender address = the fixture parent's email, so grounding finds the family |
| `expect` | deterministic checks, graded at `run` and re-graded at `score`/`report` (edit, no re-run) |

```json
"expect": {
  "actions": [{"slug": "cancel_subscription", "description_matches": ["€\\s?25\\b"]}],
  "only_listed_actions": true,
  "draft_matches": ["attest"], "draft_forbids": ["korting"],
  "note_matches": ["attest"], "note_forbids": []
}
```

Decision fixtures (should the brain answer at all?) add `"outcome": "draft" | "decline"` and
`"decline_kind": "identity_unresolved" | "identity_ambiguous" | "identity_lookup_unavailable" | "none"`
(`none` = no identity stop; an ordinary decline still passes; `a|b` accepts either: a simulation turns an
auto lookup action into a proposal, so a stop after a live lookup reads `identity_lookup_unavailable`).
The kind is read from the run's `reply` event. That pins "needs no existing record → draft", "courtesy close → plain decline" and "unknown
person, record required → identity stop" apart.

Regexes are case-insensitive. `actions: []` asserts *no* proposal. Action params are hash-only on the
API, so amounts are matched on the preflight preview (`description`). A ✗ caps the judge verdict
below `pass`. Run output shows `✓ 8/8` per case, failed checks listed under it.

When to write one: a review card / ticket whose done-when needs a state the real record no longer
has, any action-proposal behaviour (which action, which amounts, none at all), and every rule
that should hold for *all* tenants. Overlay-only rules (one tenant's skill) need a fixture in that
tenant, or the next real case. Keep the dossier pristine: simulations never execute, and the seed
script resets it if someone did.

Because the content is synthetic, fixture files may be committed (in a run-excluded path, e.g.
`_internal/simulate/`); scratch output stays gitignored as always. KampAdmin's set:
`replypen-brain-kampadmin/_internal/simulate/demo-fixtures.json`, seeded by the KampAdmin repo's
`scripts/replypen_sim/demo_fixtures.rb`.

## Related

[`rp-debug`](../rp-debug/SKILL.md) on any run link that looks like a host issue ·
[`brain-dream-cycle`](../brain-dream-cycle/SKILL.md) for feedback-driven consolidation ·
[`local-brain-work`](../local-brain-work/SKILL.md) for the edits the prompts ask for.
