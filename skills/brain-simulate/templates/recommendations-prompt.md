# Recommendations — turn the scores into steering prompts

Read every `scores/<ref>/*.json` and the judge bundles. Group the gaps by **lever** and write
`recommendations.json`:

```json
{"verdict_line": "One sentence a product owner can read: overall state + the single biggest gap.",
 "summary": ["≤5 short lines: pass rate, dominant verdict class, what is blocked on grounding, …"],
 "recommendations": [
   {"lever": "brain|persona|triage|grounding|host",
    "title": "Imperative, ≤10 words",
    "why": "Which cases showed it and what the human did instead (no customer text).",
    "cases": ["S…", "S…"],
    "prompt": "Copy-paste prompt for a coding agent in the brain checkout."}
 ]}
```

Rules for the prompts:

- Written for a **fresh coding agent in the brain checkout** — say which file to edit or which
  setting to change, what to add, and how to verify (`brain-ask` on a `dev/<branch>` ref, then
  `brain-simulate run --ref dev/<branch>` + `report --compare`).
- `brain` → "Add to `skills/<x>.md` under <section>: …" / "Create `knowledge/<y>.md` with …".
  Business facts and playbooks only; never generic support-engineer instructions the host prompt
  already owns.
- `persona` → the exact key and the new text: `rp project settings behavior set persona.guidance="…"`
  (or tenant scope when the brain is a tenant checkout). Keep the existing value's intent, change
  the one phrase that failed.
- `triage` → policy sentence or rule (`effect=`, `match_kind=`, `pattern=`).
- `grounding` → a ReplyPen support request through `brain-publish`: which data (table/API/KB),
  which cases need it, what the draft would do with it. Do not promise a brain fix.
- `host` → the run link and the observed behaviour; `rp-debug` first if unsure it is the platform.

Order: highest customer impact first; merge cases that share one fix into one recommendation;
at most ~6. Every recommendation cites at least one case id. **No cost or token figures.**
