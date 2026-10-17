---
name: brain-grounding-intake
description: "Sit in the production run's chair once a customer's code (source mirror or local clone) and database become readable: gather what a run receives (brain, database descriptions, mirrors, schema), replay the last window of real customer questions against it, drill the code and the data within a safe budget, and turn every gap that only the developer can close into one closed question in a self-contained report.html whose answers you write back into the brain. Use for 'we got the source', 'we got the database', 'map the codebase or schema for the brain', 'which questions can we ground', 'questions for their dev'."
---

# brain-grounding-intake: from readable code and data to a brain that can ground answers

**Python is the evidence, you are the judgement.** `context.py` writes down what a production
run receives, `scan.py` walks the repo(s) within a hard budget, `collect.py` probes the schema on
the box, `questions.py` pulls a window of real customer questions, `query.py` is the guarded
drill; you write the proposed brain files, one benchmark row per question and a short
questionnaire; `validate.py` refuses any path, table or column that does not exist and any
unanchored claim; `render.py` renders `report.html` for the developer. Their pasted answers are
step 4.

Code and data are one intake, never two: the code says what a column means, the data says which
code path is live, and the run reads both. One benchmark, one questionnaire, one report.

North star: **the run's chair, not an audit.** A future run gets the brain, the database
descriptions, the mirror trees and `lib.db`; it must answer "where would I look for X" in one hop
(which app, which file, which table and column) and know when to stop. The intake finds what that
run would get wrong today and asks the developer only about that. Everything you infer stays
`(?)` until the developer confirms.

Read-only: metadata, bounded samples and file reads; no `rp ask`, no writes, no brain edits before
step 4 ([docs/side-effects.md](../../docs/side-effects.md)). Scratch lives in the gitignored
`.replypen/`.

## When to use

- A source mirror or a grounding database appeared for the project (`rp dev console capabilities`
  lists both), or the developer handed over a clone.
- The brain answers "check your settings" or "please check in your account" where the code could
  name the setting or a row could state the fact.
- Runs keep guessing table names, read the wrong app, or explain behaviour that no longer runs.
- A rerun after answers landed: the benchmark measures progress and finds the next gaps.

Not for: reviewing code, hunting bugs, writing one run's query (that is
[`prod-console`](../prod-console/SKILL.md)), or documenting every column.

## Pipeline

```bash
cd ~/code/replypen/replypen-brain-<project>
GI="$PWD/.agents/skills/brain-grounding-intake"
OUT="$PWD/.replypen/grounding-intake/$(date +%F)"

uv run "$GI/scripts/context.py"                                  # context.md: what a run receives
uv run "$GI/scripts/collect.py" --db staging                     # schema probe on the box (rp dev console database list for the key)
uv run "$GI/scripts/scan.py" --repo app=~/code/customer/app      # local clone; tables come from schema.json
# repo only readable as a production mirror: fetch a listing (paths) or a tarball through the console
rp dev console bash run 'mkdir -p /tmp/replypen-out; find /mirrors/app -type f -not -path "*/.git/*" > /tmp/replypen-out/listing.txt'
rp dev console file get /tmp/replypen-out/listing.txt --out /tmp/listing-app.txt
uv run "$GI/scripts/scan.py" --listing app=/tmp/listing-app.txt
uv run "$GI/scripts/questions.py" --days 60                      # or --from questions.txt (one per line)

cat "$OUT/context.md" "$OUT/scan.md" "$OUT/schema.md" "$OUT/questions.tsv"   # the read-whole tier
uv run "$GI/scripts/query.py" --db staging "select status, count(*) from t where id > 900000 group by status"   # drills, logged

# write $OUT/proposal/**/*.md, benchmark.tsv, devquestions.tsv, headline.txt (intake_schema.md)
uv run "$GI/scripts/validate.py" "$OUT"                          # exit 1 + one line per problem, per file
uv run "$GI/scripts/render.py" "$OUT/intake.json"                # → report.html
open "$OUT/report.html"                                          # hand over the file path, never a localhost URL
```

Either half may be absent (code without a database, a database without code); say which in
`headline.txt`. Local clone beats listing: line counts, manifests and the table mapping need file
contents. `rp dev console database query` refuses MySQL, so `collect.py` and `query.py` run
Python on the box through `rp dev console bash run -` and fetch the result; a freshly sealed DSN
can need one retry (console sessions cache their env for about two minutes).

## Step 0: sit in the chair

Read `context.md`, then the brain's `AGENTS.md` and the skills it routes to, as a run would. Note
what the run is told before it opens anything: the database description line (the first thing it
reads about the data), the mirror names, the routing rows. Half of the gaps are not in the code or
the schema but in that first hop: a description that names eight tables of 172, a routing row
that sends every mail question to the legacy app. Those gaps are yours to fix in step 4, not the
developer's to answer.

## Step 1: orientation (bounded)

Budget: `scan.md` and `schema.md` whole, then **at most 40 code drills** (`head -80`,
`sed -n a,b p`, `rg -n -l`; never `cat` a file over 200 lines, never read a whole directory) and
**at most 20 data drills** through `query.py`, each within [db-drills.md](db-drills.md): metadata
and engine statistics first, `limit 30` samples on named non-PII columns, no grouping or joining
on a big table, `EXPLAIN` before anything not trivially indexed. Sample two or three files per
area and stop when the pattern is clear. Whole-repo reads and table scans are the failure modes
this skill exists to prevent.

What to settle, one line each in the proposal, `(?)` on every guess:

1. **Apps and boundaries.** Which apps live in the repo(s), how a request reaches each, which
   product areas are in no mirror at all. The marker that proved it.
2. **Entry and routing.** Front controllers, route files, how a URL maps to a controller.
3. **Tenant and core entities.** The tenant key and the tables lacking it; the ten tables a
   support answer touches, in the customer's words, with the model or entity that owns each.
4. **Settings and flags.** Where per-tenant settings live (a wide row, a JSON column, a config
   file), which column is the switch the customer asks about, how the app reads it.
5. **Senders, jobs, queues, logs.** Mail and notification senders, the cron that runs at 07:00,
   the row or log that proves a thing was sent, and what it costs to read.
6. **Integrations.** Payment, mail, SMS, accounting: the client class, the config key name (never
   the value), the vendor table.
7. **Conventions and oddities.** A table whose name lies, a status integer nobody documented,
   soft deletes, timestamp styles, money units, dead directories still deployed.
8. **Ownership.** Which tables the current app writes and which only the legacy one still touches.

Write it as the brain files themselves under `proposal/` (`codebase/INDEX.md` routing customer
symptom → area file → first path to open; one file per area; `databases/<db>.md` for the data
map). Paths in the production shape `/mirrors/<name>/…:method`. Progressive disclosure: the index
is what a run reads first, an area file is what it opens next, the code and the data are what it
opens last. On a rerun, copy in only the brain files you change and edit them there.

## Step 2: the benchmark, from the chair

`questions.tsv` is the last window of real inbound questions (email runs, chat runs, Help Scout:
the [`brain-helpcenter-suggestions`](../brain-helpcenter-suggestions/SKILL.md) collector), about
the last 100. No corpus yet: write 20 to 40 representative questions by hand and pass `--from`.

Cluster by the product concept the answer hinges on (booking visibility, reminder delivery,
deposit refund). Then, per question, be the run: with the brain, the context and the maps you just
wrote, where does it ground the answer in one hop, and what does it not know? Above about 60
questions, judge in parallel: write the clusters and one shared brief (the chair, the evidence
paths, the budget, the exact rows to return) and give each sub-agent a slice of clusters; a
sub-agent may split a cluster whose questions hinge on different concepts. You merge, cut and
rank; the sub-agents do not write the questionnaire.

- `grounded`: one place, named. A file and method, a table and column, or a brain runbook.
- `ambiguous`: two candidate places (legacy and new sender both exist), or a place whose meaning
  the code and the data leave open (an integer status, a column that lies). The developer decides.
- `missing`: nothing locatable within budget.
- `knowledge`: static knowledge or the brain's own prose answers it; no code, no row.
- `human`: a write, money, or a judgement call. Support prepares, a person decides.

The `note` on every `ambiguous` and `missing` row says exactly what is unknown; that sentence is
the seed of the dev question. The tally is the acceptance number: the `grounded` share, and how
many open clusters remain.

## Step 3: the questionnaire, only what moves the needle

First split what you found into two piles. **The code settles it**: a value the code names, a
column the code writes, a claim in the brain that the code contradicts. That goes straight into
the proposal without `(?)`, and the report shows it so the developer can strike it; do not spend a
question on it. **Only the developer settles it**: which of two live paths is intended, a codebase
in no mirror, a business rule nobody wrote down. That becomes a question.

Every `ambiguous` and `missing` cluster yields one question; add the `(?)` guesses from step 1 as
questions with a `proposal`, so the developer confirms or denies with one click. Then cut. The
test is the `impact` column: what a run gets wrong today without the answer, in the customer's
terms. A question whose impact you cannot state in one sentence is not worth the developer's
time; a question that unblocks a cluster of five tickets goes first; two questions about one
mechanism merge into one. Groups: `architecture` · `where` · `data` · `settings` · `queues` ·
`conventions` · `ownership`.

- Concrete and closed: "Which column closes the online agenda: `settings.months_up_front` or
  the absence of `schedules_weeks` rows?" beats "How does the agenda work?". A yes, a no, a path
  or a table name must be enough to answer.
- Say what you already checked, in the question itself: the counts you saw, the file that lacks
  the call. The developer corrects a guess in ten seconds and writes an essay for a blank question.
- 15 to 25 questions; the developer gives this fifteen minutes, not an afternoon.

`report.html` is self-contained (file://, no external assets): question cards with the impact
line, the guess with a confirm / deny / not sure toggle, clickable cluster chips that unfold the
customer questions behind them, a free-text answer, a progress counter, and one "Copy all as
Markdown" button. The benchmark, the proposed brain files, the tables at a glance and what the
run receives sit collapsed underneath. Answers persist in the browser until copied.

## Step 4: write the answers into the brain

Paste the copied markdown into this thread. Then, for the project brain:

1. Rewrite denied or corrected proposals in `proposal/`; drop `(?)` on confirmed ones; keep `(?)`
   on the unanswered. Re-run `validate.py`.
2. Move `proposal/codebase/` and `proposal/databases/` into the brain's `skills/`. Add the
   symptom → area rows to `AGENTS.md` routing, new words to `terminology.md`, `human` clusters to
   the escalation rules. Keep `description:` frontmatter on every file.
3. Fix the first hop: rewrite the database description with the helicopter facts a run needs
   before it reads anything (tenant table, the five tables a ticket touches, freshness, the
   expensive tables):
   `rp project database set <key> description="…"` ([docs/secrets.md](../../docs/secrets.md)).
4. Re-run the benchmark in your head against the brain alone: for three `grounded` questions and
   every answered one, can a run name the file or the table and column without opening the code
   or `schema.md`? If not, the map is missing a hop.
5. [`brain-git-sync`](../brain-git-sync/SKILL.md), then [`brain-publish`](../brain-publish/SKILL.md);
   confirm with one [`brain-ask`](../brain-ask/SKILL.md) run on a formerly `ambiguous` question.

## What stays out of the brain

- Secrets, DSNs, keys, `.env` contents: name the key, never the value.
- Customer data: no row values beyond enum-like samples and counts, no names, no addresses.
- File dumps, snippets, code fences, DDL, column-by-column dictionaries: a path and a method, a
  table and a column, is the whole reference.
- Framework tutorials: a run knows Laravel. It does not know that this Laravel serves `/v2/` only.
- Unconfirmed guesses without `(?)`, and any claim without a path or a table behind it.
- Judgements on code quality or performance, and anything the developer asked to keep out.

## Acceptance

A future run, given only the brain and the context, names the file or the table and column for
the benchmark's `grounded` questions, and knows which questions need the developer or a human.
The tally is in the brain's notes with a date, so the next intake measures progress.

## More

[intake_schema.md](intake_schema.md) the files you write and the validator's rules ·
[db-drills.md](db-drills.md) the safe way to look at a production database ·
[docs/mirrors.md](../../docs/mirrors.md) declaring a local checkout as a mirror ·
[docs/secrets.md](../../docs/secrets.md) registering a grounding database and its description ·
[docs/brain-model.md](../../docs/brain-model.md) routing rows, grounding hints, the brain layout ·
[`prod-console`](../prod-console/SKILL.md) console drills and `database schema` ·
[`brain-website-scout`](../brain-website-scout/SKILL.md) the same scout then synthesise shape for a website.

## Iteration log

- **2026-09-08 first merged run: iBeauty rerun** (2 mirrors as code-only tarballs from the box,
  172 tables, 109 Help Scout questions of one release week, three Opus judges on cluster slices,
  25 minutes wall time, 24 dev questions from 27 candidates). The brain had two intakes and one
  round of answers behind it, and the rerun still found nine claims the code contradicts, one of
  them marked confirmed (`mollie_logging` holds webshop rows only; deposits live in another table),
  so a rerun is worth it even on a mature brain. Fixes from this run: the question collector
  picked 3 runs over 86 Help Scout conversations once runs existed (`--source`, and Help Scout wins
  under 20 runs); `query.py` logs refused and failed drills too; a PK is not always `id` and ids
  can be sparse (get `max()` first); the line cap is a warning because routing tables have long
  rows; on a rerun only the changed files go into `proposal/`; the `brain:` locator carried 20
  `knowledge` rows and most `grounded` ones, so a run's first hop is the brain far more often than
  the code.
- **2026-09-08 merged** from `brain-source-intake` and `brain-schema-intake` after three runs on
  iBeauty that produced two questionnaires for the same developer about the same facts. Lessons
  kept from those runs: a manifest under `libraries/`, `third_party/`, `lib/`, `plugins/` is a
  vendored package, not an app root; `system/` and dot directories are never candidates; table
  declarations include the CodeIgniter query builder and raw `FROM t`; Doctrine entities live per
  module (`src/<Module>/Entity/`); `rp dev console database query` refuses MySQL, `lib.db` on the
  box does not; a mirror view carries no `.git`, so ages come from a local clone only; never
  sample id-shaped string columns or tables under 20 rows (a payment provider customer id leaked
  into a profile); spend the exact-count budget on tables the size of the tenant table, that is
  how `settings` earns its one-row-per-tenant role; column lists as bullets, not table rows.
