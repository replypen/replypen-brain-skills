# Cluster agent prompt (pipeline step 3)

Prompt + output contract for one per-cluster synthesis subagent. The orchestrator fans this out once per
cluster, substituting the `{{…}}` slots from `clusters.json`. Numeric values below (sample cap, era
bands, prose-reply threshold) are **tunable defaults from the prepare config** — read the actual plan
handed to you, never assume.

---

## Prompt (substitute and hand to the subagent)

You are distilling one cluster of a sent-mail harvest into brain deltas. Work only from what you are
given; you have no network and no `rp` access.

**Cluster:** `{{CLUSTER_ID}}` — `{{CLUSTER_LABEL}}`
**Scratch root:** `{{SCRATCH_ROOT}}` (e.g. `.replypen/harvest/<tag>/`)
**Your reading plan** (from `clusters.json` → this cluster):
- `sample_ids`: `{{SAMPLE_IDS}}` — the stratified single-pass sample (default cap 50, tunable).
- `deep_read_ids`: `{{DEEP_READ_IDS}}` — risk-flagged members; read **all** of them, on top of the sample.

### Read (only these)
1. Each assigned thread at `{{SCRATCH_ROOT}}/threads/<id>.md` — the pinned `sample_ids` **plus every**
   `deep_read_ids`. One pass. No incremental batch rounds, no reading beyond your plan.
2. The relevant tracked brain files (routing index, terminology, playbooks, case files, notes) so your
   output is a **delta against what already exists**, never a from-scratch rewrite.
3. `{{SCRATCH_ROOT}}/templates.json` in full when `template_count > 0`. These are authored,
   high-signal voice and reusable-response references. Use them for persona and stock phrasing/structure;
   they are not sent-thread evidence and cannot support counts, eras, skip rules, durable facts, or policies.

You may also read the per-thread `manifest.jsonl` rows for your ids to get machine facts (era,
`prose_reply`, `prose_reply_count`, `occurrences`, direction, counts). Never read another cluster's
threads or the raw `corpus/`.

### Produce deltas, separated by home
Compare against the existing brain and return only what changes. Sort every candidate into exactly one
home (see [`../../../docs/brain-model.md`](../../../docs/brain-model.md) prompt boundary):
- **Brain-fact candidates** — product/business facts, terminology, routing, playbooks, escalation
  criteria. Deltas: add / revise / retire, phrased against the existing file.
- **Persona candidates** — voice, warmth, formality, signature, language. Never write these as brain
  prose; name them for the persona surface. Prefer authored template wording when it conflicts with
  incidental sent-thread phrasing, and call out reusable response structures as template fingerprint.
- **Triage candidates** — draft / skip / escalate decisions and deterministic sender/subject rules.

Tag **every durable rule** (fact, price, address, product name, policy) with the **era of its supporting
evidence** (`recent` ≤ 24 mo / `mid` 2–6 yr / `old` > 6 yr, tunable). When *all* supporting threads sit
outside the trailing (`recent`) era, mark the rule **`stale-era`** — reduction will weigh it against
newer evidence and may supersede it.

### Skip / no-draft proposals — the narrow evidence gate (§5)
A sent-history corpus proves only what the mailbox answered, and unanswered inbound mail is never
exported — so **absence proves nothing**: no skip rule from a sender or subject merely being missing or
rare, and none from raw frequency alone.

Propose a skip/no-draft **only** for a subject/sender family that recurs in-corpus with
`prose_reply=false` across multiple threads, and state the **occurrence count** from the manifest.
`force_process` needs deterministic repeated *positive* evidence. This narrowing is **a deliberate
product decision, not an oversight** — do not widen it.

### Honesty signals to report
- **Saturation:** were your final reads still yielding new durable rules, conflicts, terms, routes, or
  setting signals? If yes at the cap, say `still_yielding: true` — the orchestrator issues **one**
  follow-up assignment. Do not silently read more yourself.
- **Route-elsewhere:** threads that clearly belong in another cluster (id + suggested cluster + reason).
- **Contradictions:** threads whose handling conflicts (ids + topic + note). Report them; do not resolve.
- **Current-source verification needs:** facts that must be re-checked against live grounding before use.
- **Coverage counts:** assigned vs actually read.

### Privacy (hard)
Your proposal must contain **no** raw quotes, names, addresses, identifiers, counterparties, links, or
raw filenames — distilled patterns only. Opaque IDs (`H……`) belong **only** in the report JSON, never
in the proposal prose that reduction may promote to tracked brain files.

### Self-lint before you finish (load-bearing)
Run the scratch linter on your proposal and fix every finding before marking the cluster complete:

```bash
uv run --no-project python "$SKILL/scripts/brain_lint.py" --scratch {{SCRATCH_ROOT}}/drafts/{{CLUSTER_ID}}.md
```

(`SKILL` = the absolute path to the installed `brain-harvest` skill directory, exported by the
orchestrating workflow.) Names have leaked into proposals before; this check is the guard.

---

## Output contract (two files, exact paths)

### `{{SCRATCH_ROOT}}/drafts/{{CLUSTER_ID}}.md` — the proposal
Distilled deltas, sectioned by home. Suggested layout:

```markdown
# {{CLUSTER_ID}} — {{CLUSTER_LABEL}}

## Brain-fact deltas
- <add|revise|retire> <target file>: <distilled delta>  · evidence: N threads · era: recent|mid|old [· stale-era]

## Persona candidates
- <voice/formality/signature/language signal>  · era: … [· stale-era]

## Triage candidates
- draft|escalate: <rule>  · evidence: N threads
- skip/no-draft: <subject/sender family>  · presence-without-prose-reply · occurrences: N   # §5 gate

## Contradictions
- <topic>: conflicting handling (see report ids)

## Current-source verification needs
- <fact that must be re-checked against live grounding>

## Coverage & saturation
- assigned N · read M · still yielding: yes|no — <one line>
```

### `{{SCRATCH_ROOT}}/drafts/{{CLUSTER_ID}}.report.json` — machine coverage report
Exactly this schema (consumed by `prepare_harvest.py ledger apply`). Opaque IDs live here, not in the
proposal:

```json
{"cluster":"{{CLUSTER_ID}}",
 "read_deep":["H0123456789abcdef0123456789abcdef"],
 "read_sampled":["H1123456789abcdef0123456789abcdef"],
 "route_elsewhere":[{"id":"H2123456789abcdef0123456789abcdef","suggested_cluster":"C05","reason":"..."}],
 "contradictions":[{"ids":["H3123456789abcdef0123456789abcdef","H4123456789abcdef0123456789abcdef"],"topic":"...","note":"..."}],
 "saturation":{"still_yielding":false,"note":"..."},
 "counts":{"assigned":210,"read":52}}
```

The cluster is **done when both files exist and self-lint is clean**. Resume granularity is the whole
cluster: if either file is missing or lint fails, the cluster reruns — there are no per-batch checkpoints.
