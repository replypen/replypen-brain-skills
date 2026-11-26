# Reduction prompt (pipeline step 6)

Per-topic reduction: **after** the critic (step 5), **before** tracked edits (step 7). Turns the
first-draft set — as judged by `critic/critic.md` — into tight final deltas against the induced taxonomy
(step 4). Emits nothing tracked yet. Era bands and other numeric knobs are **tunable defaults from the
prepare config**.

---

## Prompt

You reduce the harvested proposals topic by topic into final brain deltas. Read the induced taxonomy,
every `drafts/<cluster>.md`, the critic report `critic/critic.md`, and the existing brain. Also read
`{{SCRATCH_ROOT}}/templates.json` in full when `template_count > 0`. For each topic:

1. **Apply the critic.** Drop anything the critic rejected that cannot be cleanly fixed. Fix what it
   flagged as fixable (misfiled home, missing era tag, scope violation → pending recommendation). Do not
   revive a rejected skip/`force_process` proposal.

2. **Resolve or surface contradictions.** Where evidence reconciles, state the single resolved rule and
   note how the conflict was settled. Where it does not, **surface** the contradiction for the review
   brief rather than inventing a resolution.

3. **Apply era supersessions (§5a).** On conflict, **prefer recent evidence**; when newer evidence
   overrides a `stale-era` rule, keep the recent rule and **record the supersession** (what was replaced,
   by which era) so the review brief can show it. Persona/tone synthesis weights the trailing era.

4. **Tighten into deltas against the taxonomy.** Collapse per-cluster restatements into one delta per
   fact/rule, phrased as add / revise / retire against the specific existing brain file or the persona /
   triage surface. No duplicate rules across topics; no from-scratch rewrites.

5. **Keep the home split.** Brain facts stay brain prose; persona signals go to the persona surface;
   triage decisions to triage — never mixed. Triage/hard-rule widening beyond mailbox scope stays a
   **pending recommendation** unless explicit scope authority exists (§6).

6. **Apply the authored template fingerprint.** Prefer templates for voice, stock phrasing, and reusable
   answer structure. Never use them as interaction counts, era evidence, skip evidence, or factual/policy
   authority; those claims still need harvested threads or current-source verification.

Privacy is unchanged: distilled deltas only — no raw quotes, names, addresses, identifiers,
counterparties, links, filenames, or opaque IDs in anything destined for tracked files.

---

## Output — `{{SCRATCH_ROOT}}/critic/reduced.md` + `reduced.json`

Final deltas grouped by home and topic, plus the two carry-forwards the review brief needs:

```markdown
# Reduced deltas

## Brain facts        — per topic: add|revise|retire <file>: <delta> · era · [superseded: <old era>]
## Persona            — mandatory `persona.tone` (voice, formality, wording) and language deltas at tenant/project scope; signature only with repeated evidence
## Triage             — draft|escalate rules; skip/no-draft (§5, with occurrence count)
## Pending recommendations — scope-blocked triage/hard-rule widenings (§6)

## Supersessions      — <topic>: recent rule replaced <stale-era rule>
## Unresolved contradictions — <topic>: <both positions> → surface in review brief
```

Step 7 applies these deltas to the tracked working tree and the settings surfaces.

Also write `{{SCRATCH_ROOT}}/critic/reduced.json` for the deterministic review generator. Use these
exact top-level keys and fields; use empty arrays, never omitted keys:

```json
{
  "settings_changes": [
    {"surface":"persona|triage_policy|hard_rule", "scope":"mailbox|tenant|project",
     "status":"applied|pending", "summary":"distilled change", "scope_authority":true,
     "verification": {
       "pre_read_at":"2026-07-22T10:00:00Z", "post_read_at":"2026-07-22T10:01:00Z",
       "before_file":"settings-verification/persona-before.json",
       "after_file":"settings-verification/persona-after.json",
       "before_sha256":"64 lowercase hex", "after_sha256":"64 lowercase hex",
       "resolved_scope":"tenant", "resolved_target":"exact preflight target"
     }}
  ],
  "skip_proposals": [
    {"summary":"distilled proposal", "evidence_class":"presence_without_prose_reply",
     "evidence_count":3, "evidence_ids":["H<32-hex>"]}
  ],
  "durable_rules": [
    {"summary":"distilled rule", "evidence_strength":4,
     "evidence_ids":["H<32-hex>","H<32-hex>","H<32-hex>","H<32-hex>"],
     "era":"recent|mid|old|mixed",
     "stale_era":false}
  ],
  "contradictions": [
    {"topic":"topic", "status":"resolved|unresolved", "resolution":"result",
     "supersession":"old -> recent or empty"}
  ]
}
```

Evidence IDs are required scratch-only provenance. For skip proposals, list only non-holdout manifest
rows with `prose_reply=false`; `evidence_count` must equal their summed `occurrences`. For durable rules,
`evidence_strength` must equal the number of distinct IDs and every ID must have a semantic read in the
ledger. The generator rejects unknown or held-out IDs, and none of these references enters tracked output.

For an applied settings change, `verification` is mandatory and binds the immediate before/after
`get -o json` snapshots to the preflight scope and target; both files stay in scratch and the post-read
must be within five minutes. Use `"verification": null` for a pending recommendation.
