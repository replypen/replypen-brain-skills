# report.json v2

Source of truth: `scripts/report_schema.py`. Unknown keys and v1 reports fail validation.
Array order is rank. English technical prose/prompts; owner prose uses `coverage.owner_lang`, or
`coverage.owner_lang_by_tenant[scope.tenant]` for a tenant-scoped finding (host `persona.language`;
overlay `[owner].lang` overrides). `*_nl` fields are owner-language fields, not Dutch ones.

| Root field | Contract |
|---|---|
| schema_version | Exactly 2 |
| report_id, date, generated_at | Report identity, focus YYYY-MM-DD, timestamp |
| window | Collector window; date equals focus |
| kpis, coverage | Copy kpis.json and manifest.json verbatim, including raw/owner_lang/owner_lang_by_tenant/action_funnel |
| findings | At most 12, unique IDs and signatures, ranked |
| technical | headline ≤220; optional noise_note ≤400 |
| owner | headline_nl ≤220 |
| meta | Optional signal_note ≤600 |

No separate TLDR/actions/watch/regressions arrays or tenant sections.
The lede adds context without repeating cards. Collector artifacts own counters, funnel and coverage.

## Findings

| Field | Contract |
|---|---|
| id, signature | F1 etc.; stable collector signature or `<kind>:<slug>` across days |
| kind | lost_runs, action_failure, script_error, capture_gap, correction, open_feedback, policy_question, regression, watch, pattern, path_miss, usage, other, good |
| audience | technical, owner, both |
| members | Member project names |
| scope | level project/member/channel/tenant; tenant required only for tenant, key optional except project; tenant must exist in per_axis |
| severity | high, medium, low, good |
| recurrence | first_seen, last_seen, focus_count, context_count, state new/recurring/gone, optional known_since; copy evidence cluster |
| title | English ≤90; technical subject |
| title_nl | Owner language ≤90; required owner/both, including unchanged; owner subject |
| status | new, changed, unchanged |
| evidence | run_ids[], run_urls[], entries[] (below); run URLs may carry access tokens |
| impact | runs, threads; no prose |
| root_cause | plane + confidence low/medium/high; no detail prose |
| text_en | ≤700; required technical/both |
| text_nl | ≤500; required owner/both; no code/run IDs |
| ask_nl | ≤300; required owner/both; one direct question or imperative task naming the fill-in items |
| ask_for | Optional person ≤40 |
| update_en, update_nl | ≤200; required for relevant audience when changed/unchanged; forbidden for new |
| options | Required owner/both, including unchanged: 1–4 `{label, instruction}` with nonempty, distinct labels; owner verb phrases; admin instructions are fill-in templates, decision instructions concrete policy text; reject “Done in dashboard” and bare Ja/Nee/Ok |
| prototype | Optional technical helper proof: `{repo, branch, files, command, before, after}`; branch `review/<slug>`, each result `{stdout, stderr, exit_code, sha, run_id}`; AFTER succeeds; see [helper-prototype.md](helper-prototype.md) |
| prompt | Structured below; required high except owner policy_question |

Planes: host, action_plane, brain_script, brain_content, tenant_brain, persona, settings,
mirror, project_code, human_policy, human_context, noise, unknown.

`unchanged` forbids impact, root_cause, text_en, text_nl, ask_nl, ask_for and prompt, including null
values. It requires an active Postgres signature with a prompt or owner ask; the prior row supplies
inherited fields. `changed` requires a prior signature and full fields. New/changed require impact
and root_cause. `gone` never means resolved.

`scripts/prior.py <OUT> [--dsn DSN]` prints the Postgres table also appended to digest.md: status,
decision/instruction, first/last sighting and applied receipt. Active signatures use changed/unchanged.
Same-date retries do not increment occurrences again; `new` may retry when every relevant active row
was last seen on the report date. A decided item's payload stays frozen; sightings/updates still advance.
Closed accepted/noise requires `changed` with an update explicitly explaining how the retest trigger
was met (not `new`). Later reopens strictly after the YYYY-MM-DD date in its instruction. Applied
re-sightings become regressions. Recreated signatures archive the old row as
`<signature>#archived:<id>`; prior lookup canonicalizes it and retains all history.
The publisher checks the migrated schema before any write. `--dsn` also works on collect, validate,
prompt_compose and publish; production uses operator DB access (`RC_HOST_CHECKOUT`). Tests inject
`prior={}` or a prior snapshot into `load_report`, never silently connect to production.

Publication writes standard technical options; owner options come from the finding. `both` creates
linked rows; the technical twin waits for the owner decision. Tenant owner rows use that tenant’s open queue session; technical rows stay on project sessions
with the tenant slug as scope_label. One-practice FAQ/profile/master-data asks are tenant scope;
project scope is for cross-tenant asks. Legacy owner cards on project sessions match by signature
and tenant scope_label (else scope_label `project`): their next publication moves an active card into its tenant session,
preserving ID, decisions and links. No one-off republish/migration is needed.

## Evidence excerpts

`evidence.entries` holds `{run_id, label?, question?, proposed?, sent?, feedback?}` per full UUID.
`feedback` is `{score?, comment?}` from the manual feedback feed only. Missing fields are absent.
The publisher preserves these fields in review_items.evidence for both audiences.

From OUT, `evidence_excerpts(run_id)` in scripts/evidence_excerpts.py returns original source text;
`drill.py --run` also writes details/excerpts-<uuid>.json. Select short verbatim passages; omit
salutations/footers with “…” between passages. Never paraphrase quotations. Validator matches each
passage against collected inbound/draft/sent text after link→text, asterisk removal, curly-apostrophe
normalization and collapsed whitespace (weekly-review excerpt_matches). Manual feedback must match
the feed exactly. Auto-evaluation scores never qualify. Keep evidence.json beside report.json:
load/publish validate populated entries too; missing source text requires a drill or omitted excerpt.
Ask text without “?” or an initial imperative warns; options are 1–4 and meaningful labels required.

Admin option example: label “Prijzen invullen”, instruction
`- mondonderzoek: … EUR\n- RX-foto’s: … EUR\n- poetsbeurt: inbegrepen / … EUR`.

For an existing open card, run `scripts/evidence_update.py --item UUID --run UUID --json '<entry>'`
from OUT (or pass `--evidence PATH`). It validates against the same originals, updates only the
matching evidence entry, and preserves its run ID/label and populated fields. `--force` explicitly
replaces populated fields; `--dsn` selects a test database. This operator repair does not republish
reports or alter decisions.

## Prompt

| Field | Contract |
|---|---|
| task_kind | fix, investigate, decide |
| targets | Nonempty list of `{repo, paths[]}`; existing absolute/~/ checkout, nonempty existing paths inside it; `:line` suffix supported |
| run_refs | Canonical `https://app.replypen.com/runs/<uuid>`, never tokens |
| repro | Optional ≤400: concrete command or exact trace step; missing on fix warns |
| conclusion | 20–600: known diagnosis or bounded unknown |
| change | 20–900: fix steps per repo; investigation checks + stop condition + output path; decide branches |
| done_when | 10–400: immediate verifiable completion criteria |
| decision | Required for decide: question ≤300, 2–4 options ≤120 each |

Composer emits uppercase task kind/title, start checkout + AGENTS.md, other targets, files, canonical
runs, diagnosis, optional repro, change/checks/options, completion, decision, boundaries. It excludes
recurrence and skills. Warns outside 90–260 words and on hedged fixes (“locate”, “consider”, “either”,
“investigate whether”, “or (b)”, “read the full error”). Investigation never runs rp ask or confirms actions. After a human decision, implementation commits
AND ships, then verifies production; one bounded retry when not improved (maximum two rounds).

Owner prompt publication requires **all**: owner/both, fix, brain_content/tenant_brain, and every
resolved target checkout name starts `replypen-brain-`. Otherwise only the human ask appears. Path
containment prevents another repo being smuggled through an absolute path or symlink.

Validation warns on new titles sharing ≥60% of significant words with another prior signature.
These are steering, not proof of identity. KPI/manifest comparisons and evidence-ID checks remain.

## Compact example

```json
{
  "id": "F1", "signature": "policy_question:refund-window", "kind": "policy_question",
  "audience": "owner", "members": ["example"], "scope": {"level": "project"},
  "severity": "high", "status": "new", "title_nl": "Bevestig het terugbetalingsbeleid", "title": "Choose the refund window",
  "recurrence": {"first_seen": "2026-09-16", "last_seen": "2026-09-16",
    "focus_count": 1, "context_count": 0, "state": "new"},
  "evidence": {"run_ids": [], "run_urls": []}, "impact": {"runs": 1, "threads": 1},
  "root_cause": {"plane": "human_policy", "confidence": "high"},
  "text_nl": "De praktijk hanteert een andere termijn dan de huidige instructies.",
  "ask_nl": "Geldt de termijn van zeven of veertien dagen?", "ask_for": "Eigenaar",
  "options": [{"label": "Zeven dagen", "instruction": "Hanteer zeven dagen."},
              {"label": "Veertien dagen", "instruction": "Hanteer veertien dagen."}]
}
```

Full root examples: `fixtures/sample_report.json`, `fixtures/quiet_report.json`.
The sample targets `/tmp` solely to demonstrate portable path validation; real reports name the
actual checkout/files inspected by the author. Tests create isolated brain repos for owner prompts.
