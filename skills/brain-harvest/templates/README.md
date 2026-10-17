# Harvest templates

Two kinds of template feed the harvest pipeline (see the v2 spec
[`../../../docs/specs/brain-harvest-long-horizon-v2.md`](../../../docs/specs/brain-harvest-long-horizon-v2.md)):
**archetype skeletons** for laying out the brain, and **pipeline prompt/format templates** for the
fan-out → critic → reduction → brief → record stages.

## Archetype skeletons

Skeleton brain layouts so synthesis **edits structure instead of inventing it**. Pick the one that
matches the harvested mailbox, then fill the skeleton with *distilled patterns only* — never raw thread
text. These are outlines: keep only the sections the corpus actually justifies.

| Template | Use when |
|---|---|
| [`product-support.md`](product-support.md) | A single product's support inbox: bounded topics, playbooks, terminology, actions. |
| [`personal-mixed.md`](personal-mixed.md) | A personal or mixed mailbox (the bollen-klara shape): triage skill + case files + patterns + escalation/privacy. |

Both feed the same durable-home decision: brain files vs persona settings vs triage settings.
Onboarding-shaped outputs land in `notes/onboarding-inbox.md` (survey facts) and
`notes/mailbox-patterns.md` (distilled patterns), matching where the mechanical seeder already points.

## Pipeline prompt/format templates

Standard prompts and output formats for the v2 pipeline stages. Templates 1–3 are instructions for
**local coding-agent subagents** — they may reference scratch paths and opaque IDs but keep laptop-side
`rp` orchestration in the SKILL, not in the prompt. Templates 4–5 are the operator-facing report formats.

| Template | Pipeline step | Purpose |
|---|---|---|
| [`cluster-agent-prompt.md`](cluster-agent-prompt.md) | 3 — bounded topic drafts | Per-cluster subagent prompt + output contract: single-pass stratified read of the pinned sample plus every deep-read id, deltas vs the existing brain, era tags, §5 skip gate, self-lint. Emits `drafts/<cluster>.md` + `.report.json`. |
| [`critic-prompt.md`](critic-prompt.md) | 5 — early critic | Judges the **untouched first-draft set** against the brain contract, §5 evidence rules, §5a era flags, §6 scope matrix, cross-cluster contradictions, and privacy. Emits `critic/`. |
| [`reduction-prompt.md`](reduction-prompt.md) | 6 — reduction | Per-topic reduction against the critic plus the strict `reduced.json` input consumed by step 10. |
| [`review-brief.md`](review-brief.md) | 10 — review brief | Strict evaluation/metrics contracts and generated operator-brief layout. |
| [`harvest-record.md`](harvest-record.md) | 10/12 — record | Exact tracked-safe JSON candidate generated before approval, then copied byte-identically after approval. |

All numeric knobs in these templates (sample cap 50, risk cap 15%, holdout 8, era bands) are **tunable
defaults sourced from the prepare config**, never spec constants.

## Runtime boundary (both kinds)

Generated brain instructions must describe the production runtime: `bash` plus its scenario terminal
tool, no `rp` binary, read-only `/brain`, and grounding through `/brain` scripts plus injected `lib.*`.
See [`../../../docs/brain-model.md`](../../../docs/brain-model.md). Keep local authenticated CLI
workflows in this kit, never in a generated brain or a committed harvest record.
