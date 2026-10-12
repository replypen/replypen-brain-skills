---
name: brain-helper-devloop
description: "The feedback loop for adding or changing a brain/mirror helper script: design from real tickets, build read-only and verdict-first, prove on real data (mirror-try, scope-check), push a dev ref, ask synthetic questions with rc ask, read the trace to see whether the agent picked the helper up unprompted, fix developer experience instead of adding prompt rules, publish, re-ask live. Use when a support topic needs a deterministic helper or case runbook and you want evidence that production runs actually use it."
---

# brain-helper-devloop — build a helper the run agent actually reaches for

A helper is only worth its bytes when a production run opens its SKILL.md and calls it **without
being told to**. This loop measures exactly that and keeps fixes on the developer-experience side
(names, `--help`, routing lines, output shape), not on the steering side (prompt rules per failed
run — [run-trace-model § Debug Discipline](../../docs/run-trace-model.md#debug-discipline)).

Each step links the skill that owns the mechanics; this file owns the order and the stop rules.

## 1. Design from tickets, not from the spec sentence

- Start from 5–8 **real** tickets of the topic (support dump, `rc run list`, help-centre threads).
  Write down per ticket: the admin's opening symptom, what the human actually replied, which record
  ids they looked at. The reply is your expected answer; the spec line is intent, not a contract.
- Read the **source rule** before porting it (the model/service in the mirror), not the KB paraphrase:
  the port is where invisible traps surface. Note file + method + "verified <date>" in the docstring.
  Port the app's own customer-facing messages verbatim instead of paraphrasing them.
- Design the lookup from the **ticket vocabulary**, not the code's object model: cross-cutting admin
  words (signature, footer, logo) that name no record get a pseudo-topic that renders the shared part.
- Check what already exists: a renderer section (`*_to_md`), a pure module, a KB article. Extend the
  existing seam when a symptom is "one record"; add a new script only for a new question shape
  (per-person sweep, export membership, cross-record diagnosis).
- Overlapping topics: decide per symptom which helper owns it **before** writing two routing lines.
- 80/20: one helper, ≤ ~60 lines of output, verdict in the first two lines, `--json` for chaining.
  No settings, no per-tenant knobs, no writes ([side-effects](../../docs/side-effects.md)).

## 2. Build read-only, scope-safe

- Use the mirror's shared libs when present (KampAdmin: `skills/records/scripts/verdict.py` — Report:
  verdict line 1, sections, ✓/⚠/✗/ℹ items, line cap, `--json`; `resolve.py` — admin words → record
  candidates) instead of hand-rolling output and lookup.
- One primary table → `rows()`; enrichment tables → `optional_rows()` / `is_visible()` so a hidden
  table degrades a section instead of killing the answer ([scope-check § The Rule](../scope-check/SKILL.md#the-rule)).
  Never add tenant/deleted filters; the host views already encode them. Tenant-bound views hide
  global rows (`tenant_id IS NULL`, "global row + tenant override" tables): query the scoped view
  before trusting a hand query, and give the verdict a fallback. A non-tenant-scoped database: pin
  every query to the organisation and say in the docstring why the helper may open it.
- Datetimes from the row helpers may come back localised (KampAdmin `ka.rows()`: Europe/Brussels);
  never interpolate them into UTC-naive SQL, convert first (`ka._sql_timestamp`). Know which
  timestamps the app overwrites (KampAdmin: `subscription_submitted_at` at confirmation).
- Importable + CLI, catalog header in the first 40 lines (`# name:` / `# purpose:` / `# args:`,
  [prod-console § catalog](../prod-console/SKILL.md#brain-script-catalog-convention)) so the script
  is discoverable through `bash list`. `--help` is read by the agent: list every call form.
- Every lookup flag accepts the words the admin uses (a name, an address, a pasted URL), not only a
  uuid, and lists candidates on ambiguity: a uuid-only flag costs the agent 3–4 guessing turns.
  Cover every record shape a symptom names (activity *and* period); print organisation-level
  verdicts even without a record id.
- Output carries the facts, not the runbook: advice the human always gives, and every nuance the
  agent would otherwise `sed` out of the source (test mail, timezone), is an ℹ line. A verdict names
  the **remedy**, not only the cause. Say what the helper cannot see ("settings changed after the
  confirmation", "history not visible") or the agent escalates.
- Merge ⚠ per check, never per item (per-item lines explode on real tenants); test the line cap on
  a big fixture. Exclude records that are legitimately odd (leader-audience activities at €0/uncapped)
  from price/pax checks.
- Error text names the **next call** ("not a subscription id — try `--person-id`"), never a trace.
- Unit tests only for non-trivial ported rules (money, eligibility, dates), on pure rows→lines functions.
- Register it — each artefact has one job: the brain `AGENTS.md` routing line = helper path + the
  admin's symptom words (this is what pickup matches); the case runbook (`skills/cases/<slug>.md`:
  symptom → call lines → what to say / escalate) = procedure; the module docstring (read by the
  grounding pre-step) = symptom words + copy-paste CLI examples; the owning `SKILL.md` = one call
  line + one "when". The script name alone never drives pickup.

## 3. Prove the bytes before proving the agent

| Rung | Proves | Skill |
|---|---|---|
| offline pytest / `brain_test.py` | the ported rule | [local-brain-work](../local-brain-work/SKILL.md) |
| `mirror_try.py` on the real ticket ids | the staged helper on real scoped data, before vs after | [mirror-try](../mirror-try/SKILL.md) |
| ONE mirror-try as the regular admin principal | which sections must degrade, before any `rc ask` | [scope-check](../scope-check/SKILL.md) |

Shared mirror checkout: `--diff --only <your paths>` keeps other agents' uncommitted edits out; or
commit and use `--ref <branch> --base <sha>` (pin the base once `origin/main` moved). The narrow
principal is a roster label: `mirror_try.py … --as regular-admin` resolves it through the mirror's
`_internal/scopecheck-<project>.toml` (add a new audience there, ids from the tenant query plane:
KampAdmin `admins` with `is_super_admin=false AND active`). The privacy reducer shortens capitalised
words (`Meerdere` → `M.`): never "fix" a label you only saw through it. Severity (✓/⚠/✗) is a
product call: run on 3+ live records first; working sites/records must not get ✗.

Stop here if the helper is wrong on the real ticket record: the agent test below would only measure
pickup of a wrong answer.

## 4. Ask the brain like the admin would

- Push the brain change to `dev/<branch>` and the mirror change to a `dev/<branch>` of the mirror
  repo (`git push origin HEAD:refs/heads/dev/<branch>` from your worktree; `main`/`stable` stay
  untouched). Both ride one test run: `--brain-ref dev/<branch>` for the brain,
  `--mirror-ref <repo>=dev/<branch>` (or `=<40-char sha>`) per changed mirror (rc ≥ 1.38). No mirror
  publish, no `rc dev mirror refresh`, no moving tip: the run mounts exactly that commit and
  `rc run debug` shows `Mirror <repo>: dev/<branch> @ <sha> · override`.
- Derive 5–8 **synthetic admin questions** from the tickets into `questions.tsv` (`id`, `tenant`,
  `question`, optional `as` = roster label): the admin's words, chat style, anonymised, with the real
  record ids a ticket needs. Never invent ids. Keep the question path-free — do not name your helper
  or SKILL file; that is the thing under test.
- One command asks them all (3 in flight, run ids persisted before waiting, re-entrant):

  ```bash
  DL="$PWD/.agents/skills/brain-helper-devloop/scripts/devloop.py"
  uv run --no-project python "$DL" ask --tag <topic> --questions questions.tsv \
    --brain-ref dev/<branch> --mirror-ref kampadmin-rootcause-common=dev/<branch>
  ```

  Results land in `.rootcause/devloop/<topic>/runs/<id>.json`. A single question stays
  `rc ask … --brain-ref … --mirror-ref …` ([brain-ask](../brain-ask/SKILL.md)); the narrow-admin
  pickup test is the `as` column (or `--principal-kind/--principal-id`). Raw scenario may answer in
  another language: judge pickup, not prose.
- Tickets drift (half are usually fixed in prod by now): derive each question and expected answer
  from **today's** record state; naming the historical cause as a likely explanation is not a miss.

## 5. Read the trace for pickup, not only for correctness

`devloop.py report` reads every run's trace and tables the mechanical part; `rc run debug <run_id>`
([rc-debug](../rc-debug/SKILL.md)) is the drill-down for one run:

```bash
uv run --no-project python "$DL" report --tag <topic> --helper <helper>.py \
  --case skills/cases/<slug>.md --expect-mirror kampadmin-rootcause-common=<sha>
```

Columns: surfaced (grounding pre-step saw the case/helper) · opened (main loop read the case file) ·
called (helper argv, call count, exit, Traceback) · brain/mirror SHA ✓/✗ against the expectation ·
answer head · an empty `right` column. Those are command-text matches (a read attempt, not a proven
read); the four judgements per run are still yours:

1. **Opened** — did the main loop read your SKILL/case file? (files-read section; grounding
   pre-step selections are hints, not proof).
2. **Called** — which helper actually answered, with sane arguments? (timeline `bash` commands; a
   sibling topic's helper answering an overlapping symptom is an ownership question, not a miss).
3. **Right** — does the answer match what the human replied? Note what it still got wrong.
4. **Cause of a miss** — grounding (file never surfaced), routing (surfaced but not opened),
   helper output (called, verdict buried or misread), or model (everything right, wrong prose).

Fill `right` and `cause` into that table: it is the deliverable of the loop.
When the draft contradicts your runbook, check the source method before calling it a miss: the model
reading `/mirrors` code is often right and the runbook paraphrase wrong — fix the runbook.
Knowledge-only questions (no record id) legitimately skip the helper; count them separately.

## 6. Fix developer experience, not steering

Allowed fixes after a miss: rename to the symptom word the admin uses; put the verdict on line 1;
add `--json`; make `--help` show every call form; add the one missing routing line; make the error
text name the next call; move a fact the agent dug out of the source into an ℹ line.
Not allowed: a prompt rule per failed run, broad AGENTS.md rewrites, touching other topics' files.
Two or three loops is plenty — stop when the helper is used unprompted in ≥ 4 of 5 fresh questions,
or when the remaining misses are clearly not yours (model prose, emphasis, a grounding-pass artefact).

## 7. Ship and re-ask live

[brain-publish](../brain-publish/SKILL.md) for the brain (exact SHA, channel proof). The mirror
lands with one command from any checkout, also a shared one holding siblings' uncommitted files
([brain-git-sync § git_land](../brain-git-sync/SKILL.md)): a throwaway worktree at `origin/main`,
only your files or commit, push `main` + `stable`, then the mirror refresh per consuming project:

```bash
uv run --no-project python "$PWD/.agents/skills/brain-git-sync/scripts/git_land.py" \
  --repo <mirror checkout> --paths <your files…> --message "<subject>" --also stable \
  --refresh-mirror kampadmin-rootcause-common --project kampadmin --project kampadmin-support --project kampadmin-staff
```

(`--commit <sha>` lands a commit you already made in a worktree.) Then one `rc ask` **without**
`--brain-ref`/`--mirror-ref` and confirm in its trace `brain_resolved` is the published SHA, the
mirror line shows the landed SHA and the helper still gets called. Done means live, not "ready to
publish". Before publishing: every `skills/**` description ≤ 1024 chars with a first sentence that
fits the 150-char tree gloss (`brain_lint.py`; the pre-commit hygiene gate catches yours).

Shared checkouts with parallel agents: code in a `git worktree add` copy, never amend/rebase/stash in
the shared checkout. Shared files commit whole: a sibling may already have landed your hunk
(`git grep origin/main` first), and a routing-line conflict in `AGENTS.md` is normal — keep both lines.

Per-topic lessons and open gaps live in the brain's `_internal/devloop-lessons.md` (backlog).
