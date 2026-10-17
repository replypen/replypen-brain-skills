---
name: brain-helpcenter-suggestions
description: "Turn a window of real customer questions (email runs, in-app chat runs, Help Scout) into a short, evidence-backed edit list for a public help centre, each edit anchored in the existing article and written in the help centre's own voice. Python collects the corpus and every article body from a brain checkout with public rp; you classify in a tsv and write one small markdown file per suggestion; Python validates every quote and anchor and renders report.html for the owner plus a bot-readable block per card. Use for 'help centre gaps', 'which articles are missing', 'KB suggestions for the owner', 'wat vragen klanten dat niet in de kennisbank staat'."
---

# brain-helpcenter-suggestions: what customers asked vs what the help centre says

**Python is the evidence, you are the judgement.** `collect.py` pulls a window of conversations, the
article inventory and every article body; you read one short line per conversation, cluster, drill
the few bodies that matter, and write small files; `validate.py` refuses anything not provable from
the evidence and assembles `suggestions.json`; `render.py` turns that into one `report.html`.

North star: **five suggestions with proof beat forty guesses.** Every suggestion is one concrete edit
the owner can make in minutes: first why, in the customers' own words with a link per conversation,
then the exact text, placed unambiguously in the existing article, indistinguishable from the
articles the owner already wrote. Zero suggestions is a valid result. The report is for the owner (a
founder, not a developer). **One report = one help centre**: the mount decides, not the tenants. A
tenant KB at `/kb/tenant/**` gives a per-tenant report (`--tenant`); a project KB serves every tenant
in one report with tenant as a column.

Read-only: every `rp` call lists, traces or reads. Never `rp ask`, never edit the brain or the help
centre from here ([docs/side-effects.md](../../docs/side-effects.md)). Scratch stays under the
gitignored `.replypen/`: the report carries customer snippets and tokenized run links.

## Pipeline

```bash
cd ~/code/replypen/replypen-brain-<project>
HC="$PWD/.agents/skills/brain-helpcenter-suggestions"

uv run "$HC/scripts/collect.py" --days 60                       # default 60, max 120 · --tenant SLUG · --out DIR
uv run "$HC/scripts/collect.py" --days 90 --skip-tenant demo    # chat project: pre-tag operator tenants as noise
uv run "$HC/scripts/collect.py" --days 90 --kind analysis        # one channel only (default: email + chat + analysis); repeatable
OUT="$PWD/.replypen/helpcenter/<window-end-date>[-<tenant>]"    # last line of the collect summary

cat "$OUT/conversations.tsv"                    # pass 1 reads this whole (one line per conversation, ≤ 200 chars)
cat "$OUT/articles.tsv"                         # and this whole (one line per article)
# write $OUT/classification.tsv → uv run "$HC/scripts/validate.py" "$OUT"   (pass 1 checkpoint: classification only)

grep -A12 "^##### run:53ff0f29" "$OUT/digest.md"   # pass 2: one conversation in full, by id
cat "$OUT/raw/articles/A22.md"                     # the current body of an article you target (all bodies are local)
rg -n -i -w "cabine|toestel" "$OUT/raw/articles"   # prove absence over the whole KB, offline
# write $OUT/suggestions/S1.md … + headline.txt + learnings.md
uv run "$HC/scripts/validate.py" "$OUT"          # exit 1 + one line per problem, prefixed by file; fix that file, re-run
uv run "$HC/scripts/render.py" "$OUT/suggestions.json"   # → report.html
open "$OUT/report.html"
```

`digest.md` and `evidence.json` are drill tiers: grep them by id, never read them whole. `raw/`
keeps headers, pages and article bodies so a re-run is offline. Every re-run rewrites
`evidence.json` and its sha; `classification.tsv` binds to that sha on its first line (the
validator's error prints the current one).

## Where the questions come from (collect picks automatically)

| Source | When | Corpus | Link | Reply provenance |
|---|---|---|---|---|
| `runs` (email + chat) | the project has `email` or `chat` runs in the window | one `rp run trace --stream` header per session (the last run: its `prior_messages[]` is the whole thread); roles from sender vs mailbox domains, `unknown` when neither; chat: inbound = customer | tokenized run URL | email: `draft` unless a mailbox reply precedes it (`human`); chat: always `draft` (the agent answered live) |
| `runs` (analysis = Embassy support tickets) | the project has `analysis` runs in the window | the header `question` is a markdown ticket (`# Support Ticket:` / `## Metadata` / `## Discussion`); the discussion is the customer turn (split on dated name headers), tags `ticket_type:` + `priority:` | tokenized run URL | always `draft`; `ticket_type:feedback` and `ticket_type:request` are pre-tagged noise (feature wishes) |
| `helpscout` | no runs, a Help Scout mailbox is watched | `lib.api get helpscout /conversations?embed=threads` paged in the prod workspace, spilled to a file | `secure.helpscout.net/conversation/<id>/<number>/` | `human` (`bot` when the mailbox AI assistant answered) |

No runs and no Help Scout mailbox: collect exits 2. Recipes still to write (say so in `learnings`):
Intercom conversations, Gmail/IMAP mailboxes without runs. A `brain-harvest` dump is not a source: it
carries no conversation URLs.

**Roles are derived, not trusted.** `is_inbound` is delivery direction: a sibling vendor cc'd on the
thread also arrives inbound. A `later[]` turn is `customer` only when the sender matches the thread's
originating customer, `agent` when outbound or from one of the project's mailbox domains, else
`unknown`; the validator refuses a quote from an `unknown` turn. Bot furniture (widget greeting, flow
prompts, auto-replies) is dropped; `first_message` is the first customer turn that looks like a
question, the raw opener survives as `first_raw`. Chat clarifier forms (`User selected: doel=…`) are
folded into the previous customer turn as `[koos: …]`; the agent's clarifier prompt is dropped.

Split chats (Help Scout Beacon opens five conversations for one salon) and identical mails from one
sender within an hour merge into one conversation (`tags: merged:<id>`). The collector pre-tags
obvious noise as a *suggested* `not_kb` (`calendar_invite · no_reply_sender · test ·
duplicate_outreach · empty · internal_tenant · repeated_prompt · bare_url · error_paste · alert_mail ·
ticket_type`): override
it when the text says otherwise, never skip the line. Help-centre URLs a human pasted in a reply
become `linked_articles`: proof the content exists and discoverability failed.

Article inventory: every `INDEX.md` under the chosen scope, one line per article with the
provider-native handles from its frontmatter (id, number, collection id, locale) and its full body
in `raw/articles/<Aid>.md`. Ids follow path order. The brain's own `knowledge/` titles are listed
separately (`home: brain`) and never receive a public-KB suggestion. An empty inventory is a hard
stop. Honour `audience:` (KnowledgeOwl mixes customer and internal docs): an internal article does
not answer a customer question.

## Judge in two passes

**Pass 1, from the tsv only** (500 lines ≈ 25k tokens: that is the budget). One verdict per
conversation, every conversation, with `topics[]` (a thread often carries two questions; first =
primary). A line whose opener says nothing ("Hey.", "al nieuws?") is read in the digest by id
before it gets a verdict. Write `classification.tsv`, run the validator, and you have a checkpoint.

- `answered`: an article answers the question as asked (the human may even have linked it).
- `partial`: an article covers the topic but misses the asked point (a condition, an exception, a
  second screen).
- `missing`: no article; the human answered by hand.
- `wrong_title`: the content exists but neither title, keywords nor aliases contain the customer's
  words. A discoverability hypothesis: say what the customer typed and propose those words as an
  `add_alias` (or a `retitle` when the title itself misleads).
- `recipe`: the customer asked for data or an action they could have done themselves in the product
  ("maak een lijst van inschrijvingen deze week op locatie X", "rapport last-minute
  inschrijvingen") and no article teaches that self-service path. On an admin or power-user channel
  this is the main gap; it backs `new`/`rewrite` like `partial` does. A one-off data pull with no
  reusable path stays `not_kb`; a request where part of the path is self-service and the customer
  also asked how it works is `missing` or `partial`.
- `not_kb`: do-it-for-me with no self-service path, billing disputes, bugs, feature requests,
  end customers at the wrong address, pricing, tests and internal chatter, acknowledgements.
- `uncertain`: not decidable from the line and the digest. Classify, never suggest from it.

**Pass 2, bodies for the clusters that matter.** Cluster by the product concept the answer hinges
on, not by the symptom named ("Nele double-books my two cabines", "the reminder mail names the
wrong toestel" and "can I close a ruimte in use" are one cluster). One suggestion per cluster; the
slug goes in `topics[]` of every row it covers and in the suggestion's `topic`. Then:

0. **Twins first.** `articles.tsv` marks same-title articles (`dup`): the same article shipped
   under two collections drifts apart; diff the twins before targeting one.
1. **Cheapest edit first.** `add_alias` / `retitle` when the answer exists; `rewrite` when it is
   partial; `new` only when nothing covers it; `merge` only when two articles visibly confused
   customers; `delete` only with a destination and a reason.
2. **Anchor every rewrite** in the current body (`raw/articles/<Aid>.md`): `edit.old` is the
   verbatim block you replace, or `edit.after` / `edit.before` the verbatim line you insert at. The
   validator checks the anchor exists (once). Copy anchor lines out of the file (non-breaking
   spaces and all). Open the body before you write; the index summary proves nothing.
3. **Rank is computed**, never typed: distinct conversations in the topic × how badly the KB fails
   (missing 3 · partial 2 · recipe 2 · wrong_title 1). One-conversation topics only warn.
4. **Seed text from the human reply** (`seed_reply`) when it was a good, reusable answer. The
   report shows it as "How <name> answered this in the ticket". Drafts and bot replies are never a
   seed. When the human explained the mechanism but not the menu path, still propose the article and
   leave the path as `[[pad: …]]` for the owner.
5. **Quotes are verbatim customer words**: a contiguous substring of `first_message`, `first_raw` or
   a later `[customer]` turn, original language, PII-free by choice of substring.
6. **Contradictions count.** Two live articles that disagree are a `rewrite` with
   `flags: [contradiction]`; the customer need not have named the contradiction, any conversation
   in that topic is the evidence. Say which article the humans confirm as right.
7. **Route** `brain` when the KB is fine and the agent ignored it: hand those to
   [`brain-dream-cycle`](../brain-dream-cycle/SKILL.md).

Stop drilling when more detail would not change the edit. Judge time on 60 to 90 days should stay
under 30 minutes: bodies only for the top clusters.

## Voice: write as the owner writes

`## Why` is one paragraph for the owner: the gap and what customers said. Never type the count of
conversations in it; the card prints the computed one.

Every proposal must be indistinguishable from the existing articles' author. Three voice sources,
read in this order before writing any edit; the earlier one wins on conflict:

1. **A help-centre voice file in the brain**, when it exists: `notes/helpcenter-tone-of-voice.md`
   at the root of the checkout you run from (project brain for a project KB, tenant brain
   `replypen-brain-<project>-tenant-<slug>` for a tenant KB). It is the owner's own description of
   how their docs read and it beats everything below. Keep it out of runs (`.replypenignore`): docs
   voice is not mail voice.
2. **The persona of the level you report on**, via `rp`: `rp project settings behavior get -o json`
   (`persona.*`) for a project KB, `rp project tenant settings get <slug>` for a tenant KB. It
   describes mail, not articles, so take vocabulary, je/u, forbidden words and product names from
   it, never its length, greeting or sign-off rules.
3. **Two or three live articles** of that help centre, read in full, always: je/u, sentence length,
   heading style, numbered steps or prose, bold for UI labels or not, how they open and close, how
   menu paths are written. When only these exist, they are the whole voice.

Then these rules, non-negotiable (the validator hard-fails the first one):

- No em dashes, no en dashes as connectors. Choose a comma, a period, a colon or parentheses.
- No "it's not X, it's Y" and no reversed "X rather than Y" when nobody claimed X.
- No triads for rhythm; three items only when the meaning has three parts.
- No closing sentence that repeats the paragraph, no "in short", no summary line.
- No hedging filler (could potentially, in some cases it may) and no staged run-up (let's look
  at, here's what you need to know, the real question is).
- No emoji, no arrows as decoration, no bold labels with colons on every list item, no title case
  in headings. Menu paths follow the articles' own convention (`Instellingen → Agenda` when that is
  how the owner writes them).
- Concrete over abstract: name the screen, the button, the field, the number. "Klik op
  **Instellingen** en dan **Agenda**" beats "configure your calendar settings".
- Plain verbs: is, has, opens, shows. Not serves as, features, boasts, enables, ensures.
- Say what the article does not know as `[[pad: …]]` for the owner; never invent a menu path.
- Vary sentence length; a real writer alternates short and long.
- Markdown in the help-centre canon (the publisher converts it to provider HTML and back): `- `
  bullets, `**bold**`, ATX headings, one line per paragraph, no tables, no strikethrough, no raw
  HTML. The validator warns on anything else.

## Data semantics

- **Creation-window bias.** Conversations are selected on `created_at`; follow-ups on older threads
  are not in the corpus. Say so when a topic looks thin.
- **Pick the channel where the help centre should have been read.** On KampAdmin the dashboard
  chat is a data-request channel and rarely consults the KB; the Embassy support tickets
  (`--kind analysis`) are where an admin should have found the article first. Say in the
  headline which channel the report covers.
- **Trace payloads expire.** Email run headers older than about two weeks come back without
  `question` or `prior_messages` (kampadmin, 2026-09-08: a clean cutoff 14 days back); chat runs
  kept about a month. Collect drops them as `no message payload left in the trace` and prints
  `text only from <date> on` in the coverage line: that date, not `--days`, is the real horizon.
  Help Scout has no such limit.
- **A draft is not an answer.** The collect summary prints how many conversations carry a human
  reply. Use a draft to understand the question, never as ground truth for the text.
- **Chat sessions** are short, multi-turn, and often a data request; read the later turns before
  classifying, and look for the self-service path (`recipe`) before writing `not_kb`.
- **Absence of questions proves nothing** about an article being unused: never `delete` from silence.
- **Coverage first.** A partial or unavailable feed goes into `headline.txt`. The `other_runs`
  coverage line names run kinds outside the corpus (whatever `--kind` left out): quote it,
  it is for the operator, not the owner.

## Privacy

First names or handles only, no e-mail addresses, no phone numbers: the collector strips them, keep
it that way when you quote. `evidence.json`, `raw/` and `report.html` stay local; deliver the report
where the owner already works (ticket, chat), never by committing it.

## Meta-learning

`learnings.md` (never shown to the owner), 0 to 5 bullets, `- <target>: <observation> -> <proposed change>` with target rubric
/ recipe / normaliser / validator / render. Promote the reusable, anonymised ones into this file's
iteration log and the code; project quirks stay in the brain's notes.

## More

[suggestions_schema.md](suggestions_schema.md) the files you write · [`brain-helpcenter-publish`](../brain-helpcenter-publish/SKILL.md)
applies a card's bot block through `rp project knowledge article apply` · [`brain-fleet-report`](../brain-fleet-report/SKILL.md)
the same collect → judge → render pattern for a day of runs · [`rp-debug`](../rp-debug/SKILL.md) one run in full ·
[`prod-console`](../prod-console/SKILL.md) `/kb` and connector reads · [docs/knowledge-base.md](../../docs/knowledge-base.md).

## Iteration log

- **2026-09-08 voice sources.** iBeauty handed over their docs tone-of-voice (docs write
  `Instellingen → Werkschema's` and Goed-om-te-weten blocks; their mail persona writes lowercase
  slash paths and never uses blocks). Voice now has three ranked sources: brain file
  `notes/helpcenter-tone-of-voice.md` (run-hidden) · persona of the reported level via `rp` ·
  mirrored articles. First instance: replypen-brain-ibeauty.
- **2026-09-08 round 3** (PJ feedback + kampadmin-support 90-day pull). Chat recipe: one trace per
  session (last run), inbound = customer, provenance `draft`, clarifier forms folded; `runs` source
  covers email + chat; `--days` 60/120; header-only cache (`raw/hdr-*.json`, 9 scaffolding keys
  dropped); `other_runs` coverage line (317 of 350 analysis runs failed on kampadmin-support).
  Rubric: `recipe` verdict for self-service gaps on admin channels. Tenant model by mount, not by
  tenants; console tenant inferred when the project refuses tenantless calls. Agent output moved to
  small files (classification.tsv · suggestions/S*.md · headline.txt · learnings.md), validator
  reports per file and assembles suggestions.json; every rewrite anchored (`edit.old/after/before`)
  against the fetched article bodies (`raw/articles/`). Voice rules + em-dash hard fail + markdown
  canon warnings. Report: why → evidence → edit, before/after and insert renderings, bot block per
  card (`replypen: helpcenter/v1`, keys agreed with the write path), both clipboard flavours, tiles
  that explain themselves, per-tenant table. Found on the way: trace payloads expire after ~14 days
  for email runs, so a 60-day runs window is really a two-week corpus (Help Scout is not affected).

- **2026-09-08 round 2** three real runs (iBeauty Help Scout · pro-backup email_runs + Intercom
  KB · kampadmin email_runs + tenant Intercom KB). Collector was the weak layer: agent replies from a
  sibling vendor labelled `[customer]` (roles now sender-vs-mailbox-domain, else `unknown`, refused as
  quotes) · tenant-scoped console never passed `--tenant` so 0 articles slipped through with exit 0
  (now `--tenant`, hard exit, one report per help centre) · INDEX discovery via `find`, read through
  the artifact path (64 KiB stdout cap truncated 192 articles) · `linked_articles` matches any
  article url · bot greetings skipped as reply · question-turn selection with `first_raw` · runs
  merged on sender+text · noise pre-tags · `topics[]` · `conversations.tsv`/`articles.tsv` as the
  read-whole tier. Second judge pass on the same three: replies from the mailbox's AI assistant
  (`operator+…@intercom.io`) were `human` → now provenance `bot`, never a seed; `linked_articles`
  came from drafts too → human replies only; Calendly bodies → `calendar_invite`; article ids now
  follow path order (stable across runs); Beacon `note` threads (IP, browsing history) dropped
  before `raw/` is written; contradictions between two live articles are a `rewrite`. Judge time:
  9–20 min per project. Unverified judge claim: some Beacon chats arrive with a preview-truncated
  body ending in `(...)`: treat such a quote as suspect.
- **2026-09-08** built (collect/corpus/schema/validate/render). First real run: iBeauty (Help Scout
  recipe, 9-day window, 87 → 78 conversations after chat-fragment merge, 66 articles, 12 suggestions).
  Learnings applied: merge split Beacon chats · `linked_articles` from pasted help-centre URLs ·
  keep 12 later turns · absence-grep before opening bodies · console drill needs `-o json
  --raw-output | jq -r .stdout` · cluster on the product concept, not the symptom. Judge misses seen
  on the first pass: split one concept over two topics (cabine), called a wrong-title case
  `answered` because the human linked the article; the pasted link is the wrong-title signal.
