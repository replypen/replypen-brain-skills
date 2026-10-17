# brain-grounding-intake: files and contracts

One output directory per intake: `$OUT = .replypen/grounding-intake/<date>/`. Python writes the
evidence tier, you write four small things, `validate.py` assembles `intake.json`, `render.py`
turns it into `report.html`. Both halves of the evidence are optional on their own (a project can
have a database and no readable code, or the reverse) but at least one must be there.

Locators, everywhere a file asks for one:

- `/mirrors/<name>/<relative path>[:symbol]`: a file or directory in a scanned repo, production
  shape, never the laptop path (`scan.md` prints the mapping). Must exist in `raw/listing-<name>.txt`.
- `db:<table>[.<column>]`: a table or column of the probed database. Must exist in `schema.json`.
- `brain:<relative path>`: a file of the brain checkout itself (`brain:skills/mail-delivery-check.md`).
  Must exist on disk. Use it when the brain already grounds the answer without code or data.

## Python writes (evidence)

| File | Tier | Content |
|---|---|---|
| `context.md` | read whole | what a production run receives before it reads anything: the database descriptions (`rp project database ls`), the source mirrors and their description lines (`rp project repo ls`), the brain's file list with line counts. The agent's chair, written down |
| `scan.md` | read whole (≤ 300 lines) | per repo and per app root: framework guesses with the marker that proved them, then one block per area (entry · routes · config · models · mail · jobs · integrations · migrations · tests) listing ≤ 12 candidate paths with line counts; the mapping `<name> → <local path>`; extension tally per top-level dir; the table mapping summary when a schema was present |
| `scan.json` | drill by key | the same, machine-shaped: `repos[]{name, root, mode: local|listing, files, apps[]{root, frameworks[]{name, marker}, areas{<area>: [{path, lines}]}, deps[]}}` plus `tables[]` |
| `tables.tsv` | read whole | only when a schema was present (or `--tables FILE`): `table \t model_files \t mentions \t guess`; `model_files` = files declaring that table, `mentions` = files in model/migration areas containing the bare word, `guess` = a model file whose class name matches the table. Unmatched tables are listed last |
| `schema.md` | read whole | header (engine, version, database, table/column/FK counts, tenant candidates, tables lacking the tenant column), then one line per base table sorted by rows: `name · rows (exact when counted) · MB · role · tenant column · flags`; then views, the query budget and the skipped list |
| `schema.json` | drill by key | the probe's output: `{engine, version, database, db, collected_at, tenant_candidates[], tables[], columns[], keys[], indexes[], profiles[], counts[], budget{queries, seconds, skipped[]}}` |
| `questions.tsv` | read whole | `id \t date \t channel \t question \t url`; `id` = `Q1…`; `question` ≤ 200 chars, PII-free; noise-tagged conversations are dropped |
| `drills.log` | drill | every `query.py` statement with its row count and seconds, appended; the audit trail of what you touched |
| `raw/listing-<name>.txt` | validator only | every relative file path of that repo |
| `raw/helpcenter/` | drill | the question collector's full output (`digest.md`, `evidence.json`); grep by id, never read whole |

Roles in `schema.md` are a guess from names, indexes and counts: `log` (name looks like a log or
queue, or an `already_sent`-style column next to a time column) · `settings` (one row per tenant,
proven by a unique index or by `count(*) == count(distinct tenant)`) · `link` (two or three
columns, all `*_id`) · `lookup` (at most 200 rows) · `core`. Flags: `soft-delete:<col>`,
`ts:<col>`, `fk-out:N fk-in:N`, `enum:<col>=<top values with shares>`, `comment:<table comment>`.

## You write (judgement)

### `proposal/` — the brain files, as they should read after this intake

The tree mirrors the brain: `proposal/codebase/INDEX.md` plus `proposal/codebase/<area>.md` for
the code map, `proposal/databases/<db>.md` for the data map. First intake: write them. Later
intake: copy in only the brain files you change and edit them there, so the diff is the delta. Rules the
validator enforces on every file:

- YAML frontmatter with `description:` on every file (the brain's skill contract).
- Every backtick token that starts with `/mirrors/<name>/` must exist in that repo's listing
  (`:symbol` suffixes are stripped first; a directory counts when any listed file is under it).
- Every backtick token shaped `table.column` must exist in `schema.json` when a schema is present.
  Inside `databases/`, a bare backtick identifier must be a table or a column name; do not backtick
  prose there.
- No fenced code blocks: paths, method names, table names, never snippets. A line over 400
  characters is a warning; list many columns as bullets instead of one table row.
- No em or en dashes as connectors, no emoji, no secret shapes (`AKIA…`, `sk_live_`, `-----BEGIN`,
  `password=…`, DSN URLs with credentials).
- ≤ 150 lines per file, ≤ 24 files.

Prefix every unconfirmed claim with `(?)`. The report shows the proposal so the developer can
strike those. `proposal/` needs at least one file; `codebase/INDEX.md` is required when a repo was
scanned, `databases/<db>.md` when a schema was probed.

### `benchmark.tsv`

One row per `questions.tsv` id, tab separated, header row required:

```
id	cluster	status	where	note
Q1	booking-visibility	grounded	/mirrors/app/application/models/Treatment_model.php:online_bookable;db:treatments.online	rule in one place, table named in the brain
Q2	mail-delivery	ambiguous	/mirrors/app/src/Mailer/ReminderMailer.php;/mirrors/app/application/libraries/Mailer.php	legacy and new sender both exist, cannot tell which is live
Q3	invoices	missing		no invoice code in either app, no table with an invoice number
Q4	opening-hours	knowledge	brain:skills/agenda-year-open.md	the brain's runbook answers it, no lookup
Q5	refunds	human		money leaves the account, a person decides
```

- `cluster`: a slug for the question type; the same slug on every row of that type. Clusters, not
  rows, are what the dev questions cite.
- `status`, judged from the agent's chair (brain + context + code + data, one hop):
  `grounded` (one place, named: a run reads that and answers) · `ambiguous` (two or more candidate
  places, or a place whose meaning is unclear: the developer decides) · `missing` (nothing locatable
  within budget) · `knowledge` (static knowledge or a brain runbook answers it, no lookup) ·
  `human` (a write, money, or a judgement call: support prepares, a person decides).
- `where`: `;`-separated locators. `grounded` needs ≥ 1, `ambiguous` ≥ 2, `knowledge` takes
  `brain:` locators only (or none), `missing` and `human` none.
- `note`: for `ambiguous` and `missing`, what exactly is unknown. That sentence becomes the dev
  question.

The tally is the acceptance number: the `grounded` share of real questions, and how many
`ambiguous` and `missing` clusters remain.

### `devquestions.tsv`

The questionnaire for the customer's developer, header row required:

```
id	group	question	proposal	evidence	impact
D1	architecture	Which requests still hit the CodeIgniter app and which the Symfony app? Is there a routing rule or is it per URL prefix?	(?) /mirrors/app/public/index.php forwards /v2/* to Symfony, everything else to CI	mail-delivery;/mirrors/app/public/index.php	Without this a run reads the legacy sender for every mail question and explains behaviour that no longer runs.
D2	data	Is `users` the salon (tenant) table? Where is the end-customer table?	(?) users = salons, customers = end-customers	db:users;db:customers	Every per-salon lookup filters on the wrong key until this is settled.
```

- `group`: `architecture` (apps, boundaries, deploy) · `where` (where does X live in code) ·
  `data` (what a table or column really holds) · `settings` (per-tenant switches and flags) ·
  `queues` (jobs, logs, what proves a thing happened) · `conventions` (time, money, deletes, magic
  integers) · `ownership` (which app still writes what).
- `question`: one concrete question the dev answers in a sentence, a path or a table name. Never
  "explain the architecture".
- `proposal`: optional; our hypothesis, rendered with a confirm / deny / unsure toggle. Empty when we
  have none.
- `evidence`: `;`-separated benchmark cluster slugs or locators; every `ambiguous` and `missing`
  cluster must be cited by at least one question (validator error otherwise).
- `impact`: required. One sentence: what a run gets wrong today without the answer, in the
  customer's terms. The developer reads this to decide whether fifteen seconds of theirs is worth
  it; you read it to decide whether the question belongs in the list at all.
- ≤ 40 questions, a warning above 25.

### `headline.txt`

Optional, 1 to 3 lines for the top of the report: coverage caveats (listing mode, no schema,
questions from a hand list), the one thing the dev must know first.

## Assembled

`intake.json` = `{project, db, engine, collected_at, context, scan, schema, questions[],
benchmark[], devquestions[], proposal: {<relative file>: <markdown>}, headline[],
tally{grounded, ambiguous, missing, knowledge, human}}`. `scan` and `schema` are `null` when that
half was absent. `render.py` reads only this file.

## After the dev answers

The report's copy button yields markdown, one block per question:

```
### D2 (data): Is `users` the salon table? …
Proposal: (?) users = salons, customers = end-customers
Verdict: confirmed
Answer: yes; `customers` holds end-customers, `users` is one row per salon login (a salon can have several)
```

Paste that into the agent thread. Denied or corrected proposals are rewritten in the proposal files
before they move into the brain; a question without an answer stays `(?)` in the brain.
