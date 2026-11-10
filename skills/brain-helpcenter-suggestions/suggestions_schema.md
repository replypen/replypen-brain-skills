# The files you write

All inside `$OUT` (the collect output directory). `validate.py $OUT` checks them against
`evidence.json` and `raw/articles/`, prints one line per problem prefixed with the file
(`suggestions/S3.md: edit.old: not found verbatim in raw/articles/A22.md`), and when clean assembles
`suggestions.json` (the internal contract, `scripts/schema.py`) for `render.py`. Fix the one file an
error names; never regenerate everything. `warn:` lines are steering, not failure. Report chrome is
English; titles, texts and quotes stay in the customer's language.

## `classification.tsv` (pass 1)

```
# evidence_sha256 <the sha collect printed>
conversation_id	verdict	topics	article_ids
run:3de6cda0	recipe	inschrijvingen-filteren	A41
run:7b1e02aa	answered	moni-plaats	A12,A13
hs:1000000002	not_kb	-	-
```

- Every `evidence.conversations[].id`, exactly once.
- `verdict`: `answered` · `partial` · `missing` · `wrong_title` · `recipe` · `not_kb` · `uncertain`.
- `topics`: comma-separated cluster slugs, first = primary; required for `partial` / `missing` /
  `wrong_title` / `recipe`, optional (`-`) for `answered`, `uncertain`, `not_kb`. An `answered` row
  with a topic still counts toward that topic's conversations.
  `topics` is the join key: a suggestion's score and its "N conversations" come from the rows that
  list its topic.
- `article_ids`: the `evidence.articles[].id`s you judged against (may be `-`).
- The first line binds the file to the `evidence.json` it was written against; a re-collect changes
  the sha and the validator prints the current one.

The validator accepts this file alone ("pass 1: classification only") so you can checkpoint before
drilling bodies.

## `suggestions/S<n>.md` (pass 2), one file per suggestion

```markdown
---
kind: rewrite                        # new | rewrite | retitle | add_alias | merge | delete
topic: cabine-toestel-koppelen       # a slug from classification.tsv
title: Cabines en toestellen koppelen
target_articles: [A22]
route: kb                            # kb | brain (really a brain fix; rendered with a badge)
flags: [contradiction]               # optional, rewrite/merge only
seed_reply: hs:1000000001            # optional; the conversation whose HUMAN reply seeds the text
edit:                                # rewrite only, exactly one key, verbatim from raw/articles/A22.md
  old: |                             # block scalar: the text lines sit 4 spaces in (2 for edit, 2 more for old)
    Je kan een cabine niet sluiten terwijl ze in gebruik is.
  # after: "## Toestellen"           # single line: quote it, no block scalar needed
evidence:
  - conversation_id: hs:1000000001
    quote: kan ik een cabine sluiten terwijl ze gebruikt wordt
  - conversation_id: hs:1000000002
    quote: Eva boekt mijn twee cabines dubbel
---
## Why
Twee salons dachten dat een cabine sluiten hun lopende afspraken zou wissen. Het artikel zegt
alleen dat het niet kan, niet wat er dan wel gebeurt.

## Edit
Je kan een cabine sluiten terwijl ze in gebruik is. Bestaande afspraken blijven staan; nieuwe
online reservaties kiezen automatisch een andere cabine.
```

| Field | Rules |
|---|---|
| file name | `S1.md`, `S2.md`, … (the id) |
| YAML | quote any string that holds `:` or `#` (`quote: "zodra ik de code ingeef: reeds opgebruikt"`); multi-line `old:` as a block scalar (`old: \|`) |
| `kind` | cheapest edit that closes the gap |
| `topic` | must appear in some classification row |
| `title` | proposed title (`retitle`), or the article's title as it will read |
| `target_articles` | `home: kb` article ids. `new`: none · `rewrite` / `retitle` / `add_alias` / `delete`: exactly 1 · `merge`: 2+ |
| `destination` | `merge` (one of the targets, survives) and `delete` (another existing article) only |
| `edit` | `rewrite` only. `old:` a verbatim block of the current body that `## Edit` replaces, or `after:` / `before:` a verbatim line (must occur exactly once) that `## Edit` is inserted at. Forbidden on other kinds |
| `aliases` | `add_alias`: search phrases to add, none already on the article |
| `flags` | `[contradiction]` when two live articles disagree |
| `route` | `kb` or `brain` |
| `seed_reply` | a conversation whose reply has provenance `human`; `draft` and `bot` are refused |
| `evidence` | 1 to 5 `{conversation_id, quote}`; the conversation has a `url` and is classified `partial` / `missing` / `wrong_title` / `recipe` / `uncertain`; the quote is a verbatim substring of that conversation's customer text (`first_message`, `first_raw`, later `[customer]` turns); only case and whitespace are normalised. Agent and `unknown` turns are refused |
| `## Why` | one paragraph, the owner's reason |
| `## Edit` | the markdown pasted into the help centre: the body (`new`, `merge`), the replacement or inserted block (`rewrite`). Absent for `retitle`, `add_alias`, `delete` |

Retitle: the new `title` must differ from the current one. Contradiction: say in `## Why` which
article the humans confirm as right.

### Voice checks the validator runs

Error: an em dash (U+2014) in `title`, `why`, `## Edit`, `aliases`, `headline.txt` or `learnings.md`
(the line prints; rewrite the passage). Warn: a spaced en dash (` – `), an ellipsis character (`…`),
an emoji. `edit.old` / `after` / `before` and quotes are verbatim source text and are exempt.

## `headline.txt`

One sentence for the owner. Required once a suggestion exists; a partial or unavailable feed is
named here.

## `learnings.md`

Zero to five bullets: `- <target>: <observation> -> <proposed change>`, target one of `rubric`,
`recipe`, `normaliser`, `validator`, `render`.

## Computed for you, never typed

Rank and score (distinct conversations in the topic weighted missing 3 · partial 2 · recipe 2 ·
wrong_title 1 · uncertain 1; ties go to the cheaper kind), the summary tiles, the per-tenant table,
the top topics, and the per-card bot block (`replypen: helpcenter/v1` front matter with the
provider-native article handles plus your `## Edit` text) that the markdown tab and its copy button
carry.

## Worked example

`fixtures/out/` is a complete, validating set (every kind, one contradiction, one insert) against
`fixtures/evidence.json` and `fixtures/raw/articles/`.
