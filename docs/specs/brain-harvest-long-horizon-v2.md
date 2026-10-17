# Brain harvest v2: fast, complete, privacy-safe long-horizon synthesis

Status: kit-side implementation complete in v0.2.3; the separately listed CLI/server work remains.
Revised after a three-agent review (kit audit, cross-repo feasibility check, adversarial critique).
This revision is the source of truth; where it conflicts with `docs/brain-harvest-improvements.md`,
this file wins, and that file remains the detailed field-note record.

## Problem

Two real deep harvests (1,334 and 911 answered threads) validated the workflow but exposed a poor
profile. The latest 911-thread run took about 40 minutes of wall clock, excluding the mandatory
human diff gate. Most time went to ad-hoc corpus parsing/clustering, large agents reading every
assigned thread, and agents consolidating findings after the reading was already complete.

The workflow is correct in spirit: public CLI only, progressive disclosure, topic fan-out, early
contract criticism, strict privacy lint, production replay, and deliberate publish. V2 preserves
those guarantees while making "thorough" mean mechanically complete coverage plus risk-weighted deep
reading — not repeated full-corpus LLM reading.

Framing note: this workflow runs roughly once per new customer mailbox, occasionally re-run.
Unattended wall clock is a secondary cost; the primary costs are **operator minutes**, **agent
turns**, and — above all — **output quality**, which the previous draft never measured. Targets
below are reweighted accordingly.

## Verified ground truth (do not re-derive; checked 2026-07-19)

A future implementation agent should treat these as established facts:

- **The server now emits corpus format v2 only.** `replypen/internal/export/harvest_render.go` has
  both renderers, but `storeHarvest` (`replypen/internal/export/harvest.go:201-206`) persists only
  the v2 `Result`; the v1 body is computed and discarded. The download path renders v2 for all new
  rows. Released `rp` hard-rejects v2 by design (`replypen-cli/internal/cli/export_split.go:17`,
  with `TestParseCorpusRejectsVersionDrift` asserting rejection). Every future harvest hits this
  break; v2 split support is mandatory, not speculative.
- **Idempotent re-download after a client parse failure already works server-side.**
  `ConsumeHarvestExportBody` (`replypen/db/queries/exports.sql:34`) does not null the body;
  eviction scrubs only after done + consumed ≥48h (`exports.sql:63-78`). Consumed ≠ evicted. The
  only missing piece is client-side: `rp` discards the downloaded bytes on split failure
  (`export.go:124-134`) and `--out`/`--split` are mutually exclusive, so there is no rescue path.
- **Settings scopes:** persona is writable at mailbox, tenant, and project scope via public `rp`;
  harvest deliberately writes it at the business scope (tenant when bound, otherwise project), while
  mailbox reads detect shadowing overrides
  (`hierarchy_settings.go`, `mailbox.go:30`, `tenant.go:43`, `surface.go:63`). **Triage policy and
  hard rules are project + tenant only; no mailbox scope exists** (`triage.go:149`).
- **`rp self doctor` exists** (`doctor.go`) with a structured report; advertising supported corpus
  formats there is a small, natural addition. No export-format field exists in the API status
  projection today (`replypen/internal/api/exports.go:68`).
- **`brain_test.py` exits "no tests" for Markdown-only brains** (returns pytest exit 5 verbatim);
  no structural validator exists anywhere in the kit.
- **The current `skills/brain-harvest/SKILL.md` self-contradicts**: it orders Reduce before Critic
  while also describing the critic as running on the first draft. The ordering fix in this spec
  resolves that.
- **`prepare_harvest.py` is genuinely new.** The field-notes' top recommendation
  (`cluster_index.py`) was never built; this spec supersedes it.
- **`brain-website-scout` is the closest prior art and the only tested script in the kit**
  (`website_scout.py` + `tests/test_website_scout.py`): plan/scrape/run subcommands,
  `inventory.json` manifest, explicit reviewable selection stage, family-based stratified
  selection, gitignore preflight, requested→final capture accounting. Reuse its patterns and
  template structure instead of inventing parallel machinery.
- **An inbound/no-reply decision export is Large.** The export lane deliberately never joins
  pipeline thread state (`replypen/internal/export/types.go:13-15`); only inbound enumeration is
  precedented (survey export). Treat as a separate future project, not a v2 dependency.

## Outcomes

For a Google/Microsoft corpus of roughly 1,000 threads on a four-agent harness, excluding provider
export time and the human approval wait:

- under five minutes of active operator time before the single diff-review gate, aided by a
  generated review brief (see §10);
- turns reported per run; wall clock reported per run. Directional target: meaningfully
  below the ~40-minute baseline. No median/p95 statistics — with two historical runs they are
  unmeasurable; benchmark against synthetic fixtures instead (see Rollout);
- 100% of threads represented in a machine-verified coverage ledger (assigned, deep-read, sampled,
  excluded-as-noise, or routed-elsewhere);
- all mechanically flagged high-risk threads deep-read, within an explicit bound (§3); ordinary
  clusters read to a stratified cap with an honest "still yielding signal" flag;
- a held-out replay evaluation comparing the new brain's answers against historical human answers
  (§10) before cleanup;
- zero raw text, correspondent metadata, identifiers, or sensitive filenames in tracked output;
- raw corpus and sensitive scratch deleted **after** operator approval, never before (§7);
- no project-wide setting change inferred from a narrower mailbox without explicit scope authority.

## Core design

### 1. One deterministic preparation command

Ship `skills/brain-harvest/scripts/prepare_harvest.py` with subcommands or equivalent behavior,
modeled on `website_scout.py`'s structure and testing style:

1. Validate auth/project/mailbox/provider, write scopes, available setting scopes, existing
   grounding, corpus history, Git state, CLI/corpus-format compatibility, and gitignore coverage.
2. Parse every supported corpus format into a private normalized manifest. `prepare_harvest.py`
   must parse raw v1 **and** v2 downloads itself (via `--out` bytes), so the local pipeline never
   blocks on the `rp` splitter; `rp corpus download --split` remains a convenience, not a
   dependency.
3. Assign a content-derived opaque ID (`H` + 32 lowercase hex) to every thread. IDs remain stable
   across full/delta overlap; raw filenames remain internal and never appear in proposals/tracked files.
4. Extract metadata without LLM synthesis: date span, subject family, language, message/reply
   counts, direction, form source, attachment presence, prose-reply-present flag, era band (see
   §5a), and safe risk markers.
5. Emit topic suggestions, cluster sizes, ambiguous/mixed buckets, and a JSONL manifest whose first
   field is always the opaque ID/path handle.
6. Emit a coverage ledger proving every thread is assigned exactly once to a primary cluster, with
   optional secondary tags.

Clustering stays deliberately **dumb**: direction, form source, subject family, and lightweight
content markers. Do not build per-mailbox sender-role heuristics — the field data (18% catch-all,
10–12 false positives per cluster) shows clustering is a work-partitioning unit, not routing truth.
Instead make the mis-cluster feedback loop first-class: every agent returns a route-elsewhere list
(agents already do this unprompted), and the ledger records the reassignments. A mixed/ambiguous
bucket is mandatory. Generic contact-form subjects and words such as "order" or "invoice" must not
determine topic alone.

Acceptance:

- fixtures cover at least corpus v1 and v2;
- rerunning is deterministic and idempotent;
- malformed or unsupported formats fail before synthesis with a recovery path.

### 2. Format compatibility: the proportionate subset

The previous draft proposed a full cross-repo release contract with CI gates. Scoped down to what
the verified facts justify:

- **`replypen-cli` (M, urgent):** support v2 splitting with v1+v2 fixtures. The server already
  ships v2-only, so this is a live break, not future-proofing.
- **`replypen-cli` (S):** on split failure, offer a `--out` rescue path instead of discarding the
  downloaded bytes; error output names the 48h re-download window.
- **Server/API (S):** add the export format to the status projection so clients can see it before
  download; advertise supported formats in `rp self doctor`.

Dropped: CI gate blocking server default-format changes, format negotiation machinery. The 48h
eviction grace window already provides recovery; `prepare_harvest.py` parsing raw v2 removes the
hard dependency on the splitter entirely.

### 3. Adaptive coverage, not "read every thread"

Split coverage into two layers:

- **Mechanical coverage:** preparation scans all threads and records metadata/risk markers.
- **Semantic coverage:** agents deep-read a stratified subset plus every mechanically flagged
  high-risk, multi-reply, ambiguous, policy-sensitive, or safety-sensitive thread.

Per-cluster protocol (all numeric values are tunable defaults in the template/config, not spec
constants):

- stratify across the full date range, languages, subject families, and reply-depth bands;
- read a fixed stratified sample per cluster (default: 50 for large clusters, whole cluster when
  smaller) in one pass — no incremental batch rounds; batching added sequential round trips for a
  self-reported stopping signal that cannot be verified;
- the agent must report honestly whether the final reads were still yielding new durable rules,
  conflicts, terms, routes, or setting signals; a truthful "still yielding at cap" triggers one
  follow-up assignment on that cluster, orchestrator-controlled;
- mechanically flagged high-risk threads are always deep-read, outside the ordinary sample — but
  the flag set itself is bounded: if risk markers flag more than a configured share of the corpus
  (default 15%), preparation reports the distribution and the operator/orchestrator prunes marker
  rules before fan-out, rather than silently reintroducing read-everything.

Contradictions between threads are a semantic property discovered *during* reading; they cannot be
mechanically pre-flagged. Agents must report discovered contradictions in their output contract, and
the reduction step must resolve or surface them — but "every contradictory thread deep-read" is not
a machine-checkable gate and is not claimed as one.

"Be thorough" selects stricter defaults (larger samples, broader risk markers); it does not force
one agent to read hundreds of repetitive threads. The coverage ledger must show what was
mechanically scanned, deeply read, excluded as noise, or routed elsewhere.

### 4. Bounded, incremental agent work

Generate a standard prompt and output contract for each cluster:

- read only the assigned opaque manifest entries and relevant tracked brain files;
- compare against the existing brain and return deltas, not a replacement brain;
- report saturation honesty flag, mis-clustered items (route-elsewhere list), discovered
  contradictions, current-source verification needs, and coverage counts;
- separate brain facts, persona, and triage candidates;
- tag each candidate durable rule with the era of its supporting evidence (§5a);
- never include raw quotes, names, addresses, identifiers, counterparties, links, or raw filenames;
- self-lint the proposal before marking it complete (this is load-bearing: names leaked into
  proposals in run 1).

Resume granularity is the **cluster**, not the batch: a cluster's draft either exists complete or
the cluster reruns. Cluster reruns are cheap; per-batch checkpoint files are bookkeeping that risks
becoming the new time sink and insure against a failure that never occurred in either real run.

Adjacent small clusters may be merged; large diverse clusters should be split into bounded
assignments and reduced once, rather than assigned wholesale to a single agent.

### 5. Sent-only bias: what the corpus can and cannot prove

A sent-history harvest proves what the mailbox answered. **Absence from the corpus proves nothing**
— unanswered inbound mail is not exported at all (`in:sent` by construction), so no skip rule may be
inferred from a sender or subject merely being missing or rare.

However, the corpus does contain one legitimate negative signal: **presence without prose reply**.
Run 1's catch-all bucket (~18% of threads) consisted of automated notifications and receipts the
owner demonstrably never prose-answered across years of history — real, in-corpus negative
evidence, and the field notes' richest triage-skip source. Rules:

- persona, terminology, intake, routing, and historical handling are supported outputs;
- `force_process` may be proposed only when repeated positive evidence is deterministic;
- skip/no-draft policy, sender blocks, and hard skip rules may be proposed **only** from
  presence-without-prose-reply evidence that is repeated, unambiguous, and machine-countable from
  the manifest (`prose-reply-present` flag from §1); frequency of a subject or domain alone is not
  evidence of actionability;
- skip proposals from this evidence class are always surfaced individually in the review brief with
  their occurrence counts — never applied silently;
- absence-based skip inference remains prohibited until a paired inbound/no-reply decision export
  exists (a Large, separate project — see ground truth; explicitly out of scope for v2).

This narrows the current shipped guidance (which routes "mail you never answer" to `effect=skip`
freely). That narrowing is a deliberate product decision made here, not an oversight: sent-only
evidence supports skip rules only for the presence-without-prose-reply class.

This supersedes the earlier field-note suggestion of a general `owner_replied` vs `never_replied`
axis: the axis is valid only within the corpus, never about mail the corpus cannot see.

### 5a. Recency weighting and era flags

Long corpora span decades (2007→2026 in run 1). Stratifying across dates is right for coverage but
wrong for policy truth: a 2012 answer distilled as current policy is a quality bug.

- preparation assigns each thread an era band (e.g. trailing 24 months / 2–6 years / older);
- durable rules, prices, addresses, product names, and policy facts whose supporting evidence is
  entirely outside the trailing era are flagged `stale-era` in the proposal and review brief;
- the reduction step prefers recent evidence on conflict and records the supersession;
- persona/tone synthesis weights the trailing era.

### 6. Scope-aware settings application

Persona is writable at all three hierarchy levels, but harvested voice describes the business, not one
channel. It therefore targets the tenant when tenant-bound and the project otherwise. Triage policy and
hard rules are **project + tenant only**. The matrix:

| Signal | Narrowest writable target today | Rule |
|---|---|---|
| Persona | tenant when tenant-bound; otherwise project | Mandatory harvest output; verify the mailbox resolves it from this scope. |
| Triage policy | tenant (multi-tenant) or project | Mailbox-derived evidence necessarily widens; widen only with explicit scope authority, otherwise emit a pending recommendation. |
| Hard rules | tenant or project | Same widening rule; require deterministic evidence per §5. |
| Brain facts | tenant or project brain | Match the business scope of the fact. |

If the needed narrow scope is unavailable, produce a pending recommendation/support gap instead of
writing a broader setting. Re-read settings immediately before mutation and verify the resolved
source afterward.

### 7. Privacy gates: scratch, filenames, and cleanup after approval

Treat the corpus, manifests, cluster drafts, critic notes, and evidence filenames as sensitive until
reduced. Extend `brain_lint.py` itself (it already supports explicit ignored-path targets and has a
`--selftest` harness to extend; do not build a parallel wrapper) with the missing pattern classes:

- raw-mail shapes and quotations (exists);
- email/phone patterns as first-class rules (today only caught incidentally);
- payment/credential patterns (exists);
- order, invoice, tracking, account, or policy identifiers;
- raw or identifier-bearing filenames and opaque-ID leakage into tracked proposals;
- persona/triage/control-plane wording destined for brain files (exists as SOFT);
- person/counterparty names: **soft warning only** in scratch scanning — name detection is an NER
  problem in a regex linter, and run 1 already saw lint false positives; a hard gate here trains
  bypass behavior. Names remain a HARD failure in the tracked diff;
- fix the known SOFT false-positive on domain vocabulary in `CONTRACT_PATTERNS`
  (sign-off/greeting rules must match instructional context, not topic mentions — field-note P2).

The tracked diff may contain only the reduced, opaque-ID-free result.

**Cleanup ordering (reversed from the previous draft):** sensitive scratch is deleted **after** the
operator approves the diff, not before. Deleting the corpus pre-review would leave the single human
gate with no way to check evidence behind any claimed rule, degrading it to a rubber stamp — and
the export's eviction window makes re-fetching a fresh production operation. Sequence:

1. after lint and staged review pass, generate the review brief (§10), the sanitized replay case,
   and the held-out evaluation — these may reference opaque IDs and short evidence context in a
   **local, ignored, ephemeral** brief only;
2. pause for the mandatory operator diff approval; the operator may consult the local brief and,
   via opaque IDs, the local corpus;
3. on approval: delete corpus, manifests, proposals, the ephemeral brief, IMAP credentials, and
   temporary parsers, then verify cleanup, then push/publish.

Cleanup verification is structural, not registry-based: all sensitive scratch must live under a
single ignored scratch root created by preparation; cleanup removes the root and verifies it is
gone. No "registered artifacts" list — a registry only catches what was registered.

### 8. Unambiguous pipeline ordering

Use this exact sequence. Steps marked ⚙ are script invocations with machine-checkable output; keep
prose guidance for the remaining steps minimal to limit long-session drift.

1. ⚙ preflight and acquire;
2. ⚙ deterministic prepare/map, coverage ledger, scope matrix, risk-flag distribution check;
3. bounded topic drafts (stratified single-pass reads, cluster-level resume);
4. induce one candidate taxonomy;
5. **early critic on the untouched first-draft proposal set**;
6. per-topic reduction against the critic (resolve contradictions, apply era supersessions);
7. tracked edits plus narrow settings changes;
8. ⚙ staged scratch + brain-contract lint;
9. independent staged-diff review and fixes;
10. ⚙ generate review brief, sanitized replay case, and held-out evaluation (§10);
11. one mandatory operator diff approval (with local evidence brief available);
12. ⚙ promote the approved, byte-identical harvest-record candidate into the tracked diff; delete
    sensitive scratch and verify cleanup; then Git sync/commit/push, publish, exact-SHA verification.

Failure/resume semantics: state that persists between steps is exactly the ignored scratch root
(manifest, ledger, cluster drafts, critic output, brief) plus the tracked working diff. Any step may
be re-entered; the expensive irreversible transition is scratch deletion, which is why it happens
only after approval. A failure discovered at the gate is fixable against the still-present corpus.

### 9. Structural verification for docs-only brains

`brain_test.py` currently exits with "no tests" for Markdown-only brains. Add a default structural
validator used by harvest and publish:

- relative route/file targets exist;
- skill frontmatter is valid;
- newly routed case files are reachable from the project router;
- no local control-plane commands appear in brain content;
- privacy/contract lint passes staged and full-tree strict modes;
- no raw-harvest path is tracked now or historically;
- no sensitive scratch root remains after cleanup.

### 10. Prove the output: held-out evaluation and review brief

The previous draft could succeed on every metric while shipping a worse brain. Two additions close
that hole; run 1's single ad-hoc replay (the notary case) was the most convincing artifact of the
whole exercise and is here made systematic.

**Held-out replay evaluation (before the gate, before cleanup):**

- preparation reserves a small stratified holdout (default 5–10 threads) with substantive inbound
  questions and real human answers; holdout threads are excluded from synthesis reads;
- after tracked edits, replay each holdout question against the pushed dev ref;
- a comparison agent scores each answer against the historical human answer on factual agreement,
  routing, and tone, and writes a short scorecard into the review brief;
- the full production replay (one representative new route on a pushed dev ref, recording run ID,
  status, turns, trace URL, resolved brain SHA, and brain diff) remains required.

**Operator review brief** (generated, local+ephemeral except where noted):

- coverage summary: threads scanned / deep-read / sampled / noise / rerouted, per cluster;
- every settings change with scope, and every skip proposal with its §5 evidence counts;
- notable durable rules with evidence strength and era flags;
- discovered contradictions and how reduction resolved them;
- holdout scorecard;
- turns and wall clock for the run;
- a sanitized subset of the brief (counts and scorecard, no opaque IDs) may be committed as the
  harvest record below.

**Committed harvest record:** generation renders the exact tracked-safe JSON candidate beside the
ephemeral brief before the gate. After approval, the record command verifies current ledger/run state
and copies that candidate byte-for-byte into the tracked diff before scratch deletion. It contains the
date, export id, thread count, date span, coverage stats, ordinal-only holdout scores, run metrics, and
kit version. This provides auditability after scratch deletion and a watermark enabling **incremental
re-harvest**: a future `--since` run exports and processes only threads newer than the last record,
turning re-runs from a full harvest into a small delta.

## Deliverables

### Kit repository

- `prepare_harvest.py` (raw v1+v2 parsing, opaque manifest, dumb clustering, coverage ledger, era
  bands, prose-reply flags, risk distribution, holdout reservation) plus fixtures and coverage
  tests, reusing `website_scout.py` patterns;
- standard JSONL manifest, coverage ledger, agent prompt, critic, and reduction templates (share
  structure with website-scout templates where possible);
- `brain_lint.py` pattern extensions (§7) and the scratch-root cleanup verifier;
- review-brief and holdout-scorecard generators; committed harvest-record format;
- default Markdown-brain structural validator;
- rewritten `brain-harvest/SKILL.md` using the pipeline above;
- concise migration note from the current skill and field-notes document.

### ReplyPen CLI/server

- v2 split support and v1+v2 fixtures (M, urgent — server is v2-only today);
- `--out` rescue on split failure + error text naming the 48h re-download window (S);
- export format in the API status projection; supported formats in `rp self doctor` (S);
- future, out of scope for v2: inbound/no-reply decision export (L — requires a cross-subsystem
  join the export lane deliberately avoids today).

## Non-goals

- No hosted LLM mining tier or private ReplyPen operator path.
- No raw correspondence, names, or identifiers committed for traceability.
- No mailbox-scoped persona writes from harvest; voice targets the reviewed tenant/project business scope.
- No automatic triage widening beyond the reviewed business scope.
- No skip rules from absence-of-evidence; presence-without-prose-reply only, per §5.
- No requirement to deep-read repetitive low-risk threads beyond the stratified sample.
- No weakening of the single human diff gate before push.
- No per-batch checkpoint files, artifact registries, or format-negotiation CI machinery.
- No inbound/no-reply decision export in v2.

## Rollout

Local-first: nothing in steps 1–3 depends on cross-repo work, because `prepare_harvest.py` parses
raw v2 itself.

1. Ship preparation, opaque manifests, coverage ledger, era bands, scratch-root layout, and lint
   extensions; build synthetic 1,000-thread fixtures shaped like the two real harvests (the real
   corpora are deleted and cannot be benchmarks).
2. Update the skill workflow and templates (single-pass stratified reads, cluster resume, early
   critic, post-approval cleanup); keep the existing manual path as fallback for one release.
3. Add structural validation, review brief, holdout evaluation, and the committed harvest record.
4. In parallel, ship `replypen-cli` v2 splitting, `--out` rescue, and format advertising.
5. Validate against the synthetic fixtures: coverage and privacy gates must pass, holdout
   scorecard must be generated, turns and wall clock reported and meaningfully under the
   40-minute baseline.
