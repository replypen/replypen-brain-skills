# Archetype: product-support mailbox

A bounded product's support inbox. Topics recur (billing, access, a handful of features), so the brain
leans on playbooks + a routing index + an action catalog. Edit this skeleton; delete unused sections.

```text
AGENTS.md                     # short routing index: customer symptom phrase -> which file/script to open
terminology.md                # product vocabulary <-> customer wording (distilled from the corpus)
notes/
  onboarding-inbox.md         # survey facts about this mailbox (volume, top topics, sign-off norms) — distilled
  mailbox-patterns.md         # recurring question -> canonical answer pattern (no raw quotes)
skills/
  <topic>/SKILL.md            # one playbook per recurring topic cluster the corpus revealed
playbooks/
  <symptom>.md                # investigation -> evidence -> action/no-action for a symptom class
actions/
  <id>/                       # optional: vetted write intents the corpus shows are needed (manifest + preflight)
```

## AGENTS.md routing index (skeleton)

State the runtime boundary near the top: production has `bash` plus its scenario terminal tool, no
`rp` binary, and read-only `/brain`; ground through `/brain` scripts and injected `lib.*` capabilities.

| Customer symptom language | Check | Evidence to open | Action / no-action rule |
|---|---|---|---|
| `<phrase from corpus>` | `<script or query>` | `<playbook / mirror path / KB>` | `<propose action X only when guard passes; else explain>` |

## What goes where (do not misfile)

- **Persona settings**, not brain files: tone, formality, signature, "sound more like us".
- **Triage settings**, not brain prose: which inbound mail deserves a draft; deterministic skip/force
  rules by sender/subject. Configure them from the local authenticated control plane, never as runtime
  command guidance.
- **Brain files**: product facts, terminology, routing, playbooks, action-selection rules.

Privacy: distilled patterns only. No raw thread bodies, credentials, addresses, or payment links.
