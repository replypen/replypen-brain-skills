# brain-harvest skill — improvement suggestions

Field notes from the **first real deep run** of `brain-harvest` (bollen-klara, 2026-07-07,
1334-thread corpus spanning 2007→2026). Goal: generalizable improvements, not overfit to this mailbox.
Each item = observed friction → concrete proposed change to `skills/brain-harvest/SKILL.md`
(or its `scripts/` / `templates/`).

Severity: **P1** = caused real friction / a correctness or privacy risk; **P2** = clear improvement;
**P3** = nice-to-have.

---

## P1 — Step 1 breaks on an all-projects token (NO_PROJECT_SCOPE)

**Observed:** `rp auth status` returned `login_all_projects: true`, `project: "—"`. Then
`rp project mailbox ls` and `rp project corpus ls` (exactly the step-1 / step-2 commands) failed:
`NO_PROJECT_SCOPE: this all-projects token names no project; pass ?project=<id-or-name>`.
Every subsequent command needed `--project bollen-klara`.

**Proposed change:** Step 1 should detect this and branch:
```bash
rp auth status -o json   # if login_all_projects == true and project is empty:
rp project list          #   list projects, pick the one matching this brain dir
export RC_PROJECT=<slug> #   then pass --project $RC_PROJECT on every call
```
Add a one-liner to the skill: "If `rp auth status` shows an all-projects token with no bound project,
every scoped `rp` call in this skill needs `--project <slug>`; resolve it from `rp project list` matching
the checkout name." Current `rp` also reads the committed `.replypen.toml` project automatically when
the checkout uses the default all-projects profile.

## P1 — Add a write-scope preflight before proposing persona/triage homes

**Observed:** The decision table (step 4) routes signal to persona (`rp project settings behavior`), triage policy,
and triage rules. But whether *this token* can write those is unknown until you try. I ran `rp auth access` and
confirmed `config:write`, `runs:trigger`, `admin:*`, `secrets:write` — but a narrower token would only be
able to touch brain files and would have to route everything else to a `brain-publish` support request.

**Proposed change:** Add `rp auth access` to the step-1 inventory, and note: "If the
token lacks `config:write` / triage write scopes, persona/triage distillations become `brain-publish`
support-request items, not direct writes." Prevents discovering this only at push time.

## P1 — Clustering a large corpus needs a shipped helper, not eyeballing

**Observed:** 1334 threads. Step 3 says "cluster topics from thread metadata (month, slug, subject)" but
gives no method. Eyeballing 1334 INDEX rows is infeasible; I had to write an ad-hoc Python partitioner
(domain-substring + subject-keyword → one bucket per thread, priority-ordered, writing per-cluster TSV
manifests). It worked well and is the natural unit of fan-out.

**Proposed change:** Ship `scripts/cluster_index.py` in the skill: reads `INDEX.md`, emits a domain/subject
frequency report + writes per-cluster manifests (thread-path lists) to a scratch dir. Ship it with an
**empty/example cluster ruleset** the operator edits per mailbox (the buckets are mailbox-specific, but the
mechanism is universal). Step 3 then becomes "run cluster_index.py, review the buckets, adjust the ruleset,
re-run — one subagent per manifest." This is the single biggest time sink the skill leaves unautomated.
(Include a `misc/other` catch-all bucket — ~18% of threads here landed there and it was the richest source
of triage-skip signal.)

## P1 — Fan-out must instruct stratified sampling + a read cap (don't imply "read all")

**Observed:** Clusters ranged 42→268 threads. A subagent cannot read 268 thread files without blowing
context/cost. The skill's "each reads **only its own threads**" reads as "read all of them." I instructed
each agent to read a **stratified sample (~40-50) across the full date range, prioritizing multi-message
threads (msgs≥3) where the owner actually replied** — that's where voice + decision patterns live.
Single-message automated receipts carry almost no handling signal.

**Proposed change:** Step 3 fan-out bullet should say explicitly: "Each subagent reads a *stratified
sample* of its cluster (cap ~40-50 files), spread across the date range, prioritizing multi-message threads
where the mailbox owner replied. Do not read every thread in a large cluster." Add the rationale
(owner-reply threads = the signal; automated one-offs = noise).

## P1 — Actionability signal = "did the owner write a prose reply", not domain/subject

**Observed (strong, cross-cluster):** Multiple agents independently converged on the same discriminator:
whether a thread is *actionable* (worth drafting) correlates far better with **"the mailbox owner wrote a
real prose reply in this thread"** than with sender domain, `no-reply@` shape, or subject vagueness. The
keyword-clustered catch-all bucket (18% of corpus) turned out to be ~55-60% genuine misrouted
correspondence, not noise — precisely because bare subjects ("Re:", numeric codes) and unknown domains
dodge keyword rules. Domain/subject clustering is good for *topic*, weak for *actionability*.

**Proposed change (two parts):**
1. The shipped `cluster_index.py` should compute, per thread, a cheap **`owner_replied`** flag (does a
   message's `From` match the mailbox address with prose body, vs. self-forward / pure inbound). Surface it
   in the manifest and in the frequency report. It's the highest-value derived signal for triage.
2. Reframe the fan-out: the topic clusters drive *case-file / terminology* distillation, but a **separate
   axis — owner-replied vs. never-replied — drives triage-policy distillation.** Tell the misc/other agent
   (and ideally all agents) to use `owner_replied` as the primary draft/skip discriminator, not the
   sender. This is the most generalizable lesson of the run.

## P2 — Persona/voice should be a first-class deliverable of every cluster agent

**Observed:** Persona was **entirely empty** (no language, tone, signature, formality, guidance) — yet a
sent-mail harvest is the single best source of the owner's real voice, sign-off, and per-context language.
The skill mentions persona in the decision table but the workflow doesn't make each cluster agent *collect*
voice evidence, so it's easy to finish a harvest having improved routing but left persona blank (a miss,
since voice is what makes drafts land).

**Proposed change:** Make "## Voice & tone observations (distilled, no verbatim: languages & when each is
used, greeting, sign-off/signature, formality, typical length)" a **required section** in the per-cluster
return template, feeding a dedicated persona-synthesis step. Add a row to step 4's flow: "Synthesize a
persona proposal (language policy, tone, signature) from the aggregated voice sections; apply via
`rp project settings behavior set persona.*`."

## P2 — Multilingual mailboxes: capture language-by-context as a durable persona fact

**Observed:** Dutch-primary, English for one product vendor (Align), plus incidental French/German. "Which
language in which context" is a durable, high-value persona/routing fact that a single global `language`
setting can't express.

**Proposed change:** In the persona-synthesis note, prompt for a *language policy* ("reply in the
correspondent's language; default NL; EN with <vendor-type>"), and note it may live as persona `guidance`
rather than the single `language` enum.

## P2 — "Grow vs bootstrap" mode: diff against the existing brain, propose deltas only

**Observed:** This was **not** a cold start — the brain already had an onboarding seed + a prior partial
harvest (case files, terminology, patterns already present). Re-running risks re-proposing what's already
there. The skill is framed as bootstrap ("cold-start seed").

**Proposed change:** Add a short "Grow vs bootstrap" note: if brain files already exist, each cluster agent
(and the critic) should be handed the current brain tree and asked to return **deltas** (new patterns,
corrections, gaps) rather than a from-scratch proposal. Keeps the diff reviewable and avoids churn.

## P2 — Decouple trigger from download so long jobs / short tokens don't strand the run

**Observed:** The OAuth access token had a short lifetime (~minutes to expiry when I started). `rp project mailbox
harvest --wait` blocks the local process for the whole sweep; if the token lapses mid-wait the local wait
dies even though the server job continues. I triggered **without `--wait`** (fast, returns `export_id`
immediately, job runs server-side), then polled `rp project corpus ls` separately. This fully de-risked token
expiry and is also friendlier to the 5-min `--wait` default on large mailboxes.

**Proposed change:** Step 2 should present the decoupled pattern as the default for large/old mailboxes:
trigger without `--wait`, capture `export_id`, poll `rp project corpus ls` until terminal, then
`rp project corpus download`. Mention that the server job survives local token expiry. Keep `--wait` as the convenience path
for small mailboxes.

## P2 — `--max-threads` guidance + check the `truncated` flag

**Observed:** Skill example uses `--max-threads 1000`. For "go a long time back" I used 2000; the mailbox's
real sent history was 1334, and `rp project corpus ls` reported `"truncated": false` — confirming I reached the
bottom. If it had been `true`, history goes further and the cap needs raising.

**Proposed change:** Step 2: recommend sizing `--max-threads` generously for a deep harvest (or `0` =
server default) and then **verifying `truncated: false`** in `rp project corpus ls` to confirm the full history was
captured; if `true`, re-harvest with a higher cap. One sentence.

## P2 — Pure-noise automated senders: point at `rp project senders`, not only triage rules

**Observed:** ~18% of the corpus is automated notifications / newsletters / receipts — deterministic
always-skip material. The skill routes always-skip to `rp project triage rules`, but `rp project senders`
(never-spam/always-spam lists) exists and may be the more precise home for "this sender is pure noise."

**Proposed change:** In the decision table's "deterministic always-skip" row, mention `rp project senders block`
as an alternative home for pure-noise senders, and let the operator pick between a triage hard rule and a
spam-list entry.

## P3 — Critic should also review persona + triage proposals, not only brain files

**Observed:** Step 3's critic is described against "the existing brain tree and the brain contract" (brain
files). With persona/triage now first-class outputs, the critic should also check that voice wording didn't
leak *into* brain files (contract violation) and that triage/persona proposals are internally consistent.

**Proposed change:** Broaden the critic bullet: "check the whole proposal set — brain files, persona, and
triage — for contract violations and misfiling (voice-in-brain-file, policy-in-persona, etc.)."

## P3 — Manifest format must survive subject lines with quotes/pipes/tabs

**Observed:** Email subjects contain `|`, `"`, and occasionally tab-like whitespace. My TSV manifests
(span/msgs/subject/file) tripped one subagent's parsing on ~24 rows (it recovered via grep). The **file
path column must be robustly recoverable** regardless of subject contents.

**Proposed change:** In the shipped `cluster_index.py`, put the thread **file path in the first column**
(or emit JSONL, one object per thread) so a messy subject can never shift the path column. Have the
subagent key off the path, treating subject as free text.

## P3 — Note the two-cluster property/legal split can over-fragment

**Observed:** My "insurance-legal-realestate" (66) and "house-build-reno" (42) clusters overlap in
correspondents and could have been one. Fine, but a note that adjacent small clusters can be merged for the
fan-out would save a subagent.

**Proposed change:** One line in step 3: "Merge adjacent small clusters (<~30 threads) to keep the fan-out
proportional to signal."

## P2 — Triage `match_kind` enum is undiscoverable + docs are stale

**Observed:** `rp project triage rules add` requires `match_kind=<...>` but neither `--help`, the
validation error, nor `rp dev api openapi` reveals the allowed values. I had to brute-probe. The audited
deployment accepted only a subset; the current closed set is **`sender_address`, `sender_domain`,
`sender_prefix`, `subject_contains`, `body_contains`, `header_exists`, `header_equals`**. The old
skill/CLI example used **`sender_email`**, which the server rejects
(`match_kind is not supported`). Two of my probes also *created* live (disabled) rules as a side effect
of validating, which I then had to delete — so "probe to discover the enum" is not even side-effect-free.

**Proposed change:** (1) Keep examples on the accepted names above (never `sender_email`). (2) Ask the
CLI to list the full enum in `rp project triage rules add --help` and in the
`INVALID_RULE` error message. (3) In the skill, give the confirmed enum inline so an agent doesn't probe
(and accidentally create rules). Minor CLI bug worth filing: `add` with an invalid sibling field still
persisted a rule in some cases.

## P2 — persona.signature can't express register-switching; guidance must carry it

**Observed:** The owner signs "Klara" casually but "Klara Bollen" formally (government/notary/liability),
and the sign-off word changes ("Groetjes" vs "Met vriendelijke groeten"). The single `persona.signature`
string can't encode that. I set `signature="Klara"` and pushed the conditional sign-off + register
switch into `persona.guidance`. Worked, but it's a non-obvious modelling decision.

**Proposed change:** One line in the skill's persona-synthesis note: "If sign-off/formality varies by
correspondent type, keep `persona.signature` minimal (the name) and encode the switching rule in
`persona.guidance`; don't try to jam a formal+casual signature into the one field."

## P3 — Keyword clustering leaves real contamination; note a cleanup/routing-audit follow-up

**Observed:** Multiple cluster agents independently flagged **mis-clustered threads** (finance/tennis/
renovation/social threads keyword-matched into dental or kids buckets; ~10-12 false-positives each in
some clusters). Harmless because agents flagged and excluded them, but it means the raw manifests aren't
clean routing truth.

**Proposed change:** Tell each fan-out agent to explicitly return a "mis-clustered / route-elsewhere"
list (mine did, unprompted — make it standard), and have the orchestrator treat those as a signal to
tighten the `cluster_index.py` ruleset OR just exclude them from that cluster's training signal. Cheap,
improves precision, already emergent behavior.

---

## What worked well (keep as-is)
- **Gitignore preflight** (`git check-ignore` on the split dir) — mechanical, caught nothing to fix here
  because `/.replypen/` is wholesale-ignored, but the check is cheap and correct. Keep.
- **Privacy-first framing + `brain_lint.py`** — the hard/soft split and the "distilled patterns only"
  discipline are the right spine. (Effectiveness verified below once proposals land.)
- **Critic-on-first-draft** guidance — correct and non-obvious; keep prominent.
- **Archetype templates** (personal-mixed matched this mailbox exactly) — good, kept me from inventing
  structure.
- **Progressive disclosure / never load the whole corpus** — essential at 1334 threads; the discipline
  section already says it.

## P1 — Per-cluster distillation privacy is NOT airtight; the second gate is load-bearing

**Observed (important):** Despite explicit "no personal names, roles only" instructions, several fan-out
proposals still contained individual names — practice staff first names, a named surgeon (a real person),
architect first names, and in one case a **minor's full name**. They never reached the committed brain
because the *second-stage* grower re-abstracted them and the critic + `brain_lint` verified — but if I had
committed the proposals directly, names would have leaked.

**Proposed change (two parts):**
1. Strengthen the fan-out privacy instruction and add a **self-lint step inside each cluster agent**: before
   returning, grep your own proposal for capitalized name-like tokens not on the allowed business-identity
   list and re-abstract. (One agent did a self-check unprompted — make it mandatory.)
2. Make explicit in the skill that **the distilled proposals are still sensitive** (they can contain names)
   and must be treated like the corpus: kept only in the ephemeral scratchpad, never committed, deleted at
   session end. And state plainly: the multi-gate (distill → re-abstract in grow → lint → critic) is
   *defense in depth*, not redundant — no single stage is trusted to catch all PII.

## P3 — Token auto-refreshed; decoupling was belt-and-suspenders (still keep it)

**Observed:** The access token that looked ~minutes-from-expiry silently refreshed on continued use
(expiry moved 06:34 → 07:37 across the session). So the "trigger harvest without `--wait` to beat expiry"
precaution turned out unnecessary *here* — but it's still the right robustness default for genuinely long
server jobs and headless runs where refresh may not fire. Keep the recommendation, drop any implication
that expiry is imminent-by-default.

## Verification note (what the end-to-end test proved)
- A replayed notary deed-date case against the pushed `dev/` ref routed to the new
  `admin-logistics.md` notary/legal rule, **correctly escalated** (refused to confirm the date, no
  commitment, flagged Klara/Pieterjan), and drafted in Dutch persona voice — the run's own journal note
  cited the brain rule by name. Confirms harvest → brain-file → live-run grounding works.
- **Minor gap:** the holding draft to a notary used the informal sign-off, though persona guidance says
  formal register for notary/legal. This is a persona-adherence nuance for `brain-dream-cycle` to tune,
  not a harvest defect — worth a skill note that harvest sets the *baseline* voice and dream-cycle refines
  register-switching against real runs.

## Resolved TODOs
- `brain_lint.py --all --strict`: after re-wording two false positives ("quote sign-off" → "approving a
  quote"; "scouts sign-off/greeting" → "standard scouts closing phrase"), the whole tree is **0 findings**.
  The `sign-off`/`greeting`/`salutation` SOFT heuristic fires on legitimate *vocabulary definitions* and on
  "sign off on a quote" (approval sense) — see P2 below.

## P2 — SOFT `response-mechanics` heuristic false-positives on domain vocabulary

**Observed:** `brain_lint`'s SOFT check flags any line containing `sign-off`/`greeting`/`salutation`. It
fired on a **terminology definition** ("stevige linker — scouts closing phrase") and on "quote **sign-off**"
meaning *approving a quote*, neither of which is persona wording. Under `--strict` these block a legit commit
and train `--no-verify`.

**Proposed change:** Tighten the pattern to require an *instructional* context (e.g. an imperative or 2nd
person: "sign off with…", "use a … greeting", "your salutation") rather than the bare noun. Definitions and
the approval-sense of "sign-off" should pass. Low effort, removes the only friction in the lint on this run.

## P1 — Local IMAP harvest path must fail closed when tooling is absent

**Observed (Orthodusart first-run check, 2026-07-09):** The design docs mention a future deep/local IMAP
path (`rp project mailbox imap-env` + `scripts/local_imap_harvest.py`), but the installed `rp` command tree and
brain-skill checkout did not expose either piece. Hosted `rp project mailbox harvest` exists, but IMAP may be
capped to a shallow 100-ref smoke export and production auth was unavailable in the test session.

The current CLI and kit now expose both canonical surfaces; this observation only explains why the
fail-closed preflight remains important for older or partially installed environments.

**Proposed change:** The harvest skill should explicitly check for both local-IMAP surfaces before any
credential handling, then stop with a gap report if absent. Agents must not reveal mailbox credentials,
scrape private stores, or improvise env files. Hosted IMAP harvest may still be used as a capped smoke test
when useful, but it should not be presented as the deep IMAP path.
