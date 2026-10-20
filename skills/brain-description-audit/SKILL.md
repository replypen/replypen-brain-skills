---
name: brain-description-audit
description: "Audit and fix the one-liners grounding picks files by: help-centre article summaries in /kb and `description:` frontmatter of brain/mirror docs. Haiku (local `claude -p`) judges each against its body (ok | vague | wrong | boilerplate) and rewrites only the bad ones; HTML before→after report; apply edits docs in place or sends KB summaries to the host. Use when grounding skips the right article/doc, summaries open with 'This article…/Dit artikel…', or after a big KB or brain rewrite."
---

# brain-description-audit

Grounding chooses files from one line each: a KB article's `summary` (frontmatter + `/kb/INDEX.md` row,
written by the host's enrich model at sync time) and a doc's `description:` (tree gloss, cut at 150
runes; full value in `/tmp/rc-context/skills.md`). A vague or wrong line means the right file is skipped.

Engine: `scripts/audit.py` (one CLI, work dir per scope). Needs `claude` logged in locally (Haiku 5.5);
`rp` for the KB mode.

```bash
A=~/.replypen-brain-skills/skills/brain-description-audit/scripts/audit.py   # or the kit checkout
R="uv run --no-project --with pyyaml python"

# docs: every described .md of a brain checkout (mirrors: --mirror → skills/.agents/skills/doc/docs only)
$R $A collect-docs .                       # → .replypen/description-audit/docs/
# kb: the live /kb this project's runs see (console); --tenant SLUG = that tenant's own KB plane
$R $A collect-kb [--tenant SLUG]           # → .replypen/description-audit/kb-<scope>/

$R $A judge  --work .replypen/description-audit/docs      # cached by prompt version + item
$R $A report --work <dir> [--work <dir> …] --out ~/Downloads/description-audit.html

$R $A apply-docs --work <docs dir> [--skip KEY]   # rewrites description:, drops description_pin
$R $A apply-kb   --work <kb dir> --dry-run        # host checks ok/stale/not_found/invalid
$R $A apply-kb   --work <kb dir> [--skip KEY]     # queues the KB sync that applies them
```

## Rules the judge enforces
- Verdict on the CURRENT line vs the body; "when in doubt: ok". Only non-ok items get a rewrite (churn guard).
- Rewrite: content first (answer/rule/trigger), the body's language, only facts in the body, no
  "This article…/Leer hoe…" opener (regex-rejected too). KB ≤200 chars (host cap); docs: trigger in the
  first sentence ≤150, ≤600 total, existing pointers kept.
- KB summaries are judged per article; wrong-language summaries count as `wrong`.

## Review before apply
Read the report. Spot-check ~20 rewrites against their bodies (or have a stronger model do it): wrong
facts → `--skip KEY`; a systematic miss → tighten the prompt in `audit.py`, bump `PROMPT_VERSION`, re-judge.

## Persistence
- **Docs**: ordinary brain edits → `brain-publish`. `description_pin` is removed on rewrite so the host's
  consolidation sweep treats the line as human-owned and never regenerates it.
- **KB**: the box-local KB is the host's. `apply-kb` posts `{provider, id, body_hash, summary}` to
  `rp project knowledge summaries apply`; the host applies a patch only while the article still hashes
  to `body_hash`, and later syncs keep it (frontmatter = enrichment cache) until the article body
  changes; then the host re-enriches and the next audit re-judges. A real apply needs `knowledge:write`
  (or config:write). Verify after the job: `rp dev console bash run "grep -h '^summary:' /kb/<provider>/…"`.
