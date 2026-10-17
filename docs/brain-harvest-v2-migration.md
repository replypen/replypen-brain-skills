# brain-harvest v2 migration note

What changed when [`skills/brain-harvest/SKILL.md`](../skills/brain-harvest/SKILL.md) moved from the v1
manual flow to the v2 pipeline. The design source of truth is
[`docs/specs/brain-harvest-long-horizon-v2.md`](specs/brain-harvest-long-horizon-v2.md); the field-note
record in [`docs/brain-harvest-improvements.md`](brain-harvest-improvements.md) stays as history, but
**the spec supersedes it wherever they conflict**.

## What the v2 pipeline adds

- **One deterministic pipeline command.** `skills/brain-harvest/scripts/prepare_harvest.py`
  (`preflight` / `prepare` / `verify` / `ledger apply|expand` / `review` / `record` / `cleanup`) parses the raw corpus (v1 **and** v2)
  into an opaque-ID manifest, dumb clusters with a mandatory `mixed` bucket, stratified per-cluster
  reading plans, an era-banded metadata layer, a risk-marker distribution report, a reserved holdout,
  and a machine-verified coverage ledger. The field-notes' top recommendation (a `cluster_index.py`) was
  never built; this supersedes it. All numeric knobs are tunable defaults via `--config`.
- **Local IMAP exporter feeds `prepare` directly.** `skills/brain-harvest/scripts/local_imap_harvest.py`
  now emits a `harvest_format: v1` corpus blob at `<out>/corpus/corpus.md` — the exact section shape the
  server's canonical renderer (`replypen/internal/export/harvest_render.go` `render()`) produces, with
  real sender addresses — alongside the legacy `INDEX.md` + `threads/` split (kept one deprecation
  release). Deep-IMAP harvests therefore flow through the deterministic pipeline (`prepare --corpus
  <out>/corpus/`) instead of being forced onto the manual fallback. Because the export is sent-folder
  only, every message is mailbox-authored (`direction: mailbox_first`, no external-question holdouts —
  run with `--holdout 0`); paired inbound-thread expansion stays future work.
- **Single ignored scratch root.** Everything sensitive (corpus, manifest, ledger, drafts, critic notes,
  brief, IMAP env) lives under `.replypen/harvest/<tag>/`. Cleanup is a structural delete-and-verify of
  that root — no artifact registry.
- **Coverage ledger instead of "read every thread."** Mechanical scan of all threads plus semantic
  deep-read of a stratified sample and every risk-flagged thread. The ledger proves each thread is
  `assigned` / `holdout` / `excluded_noise` exactly once, with read state and route-elsewhere
  reassignments folded back via `ledger apply`.
- **Held-out evaluation + review brief.** Preparation reserves a stratified holdout and local replay
  cases. `review` rejects holdout leakage, incomplete semantic coverage, invalid scores/replay metadata,
  and unreconciled totals; then it generates the operator brief plus an exact tracked-safe record
  candidate. Raw holdouts never enter the synthesis thread tree; content fingerprints catch copied
  replay text. After approval, `record --approved` copies that candidate byte-for-byte before cleanup.
- **Bound target and evidence provenance.** Preflight verifies `rp auth access`, normalizes read/write
  proofs, and binds exact project/tenant/mailbox/provider/export metadata into `run.json`; review fails
  if it changes. Reduction JSON carries scratch-only evidence IDs so skip occurrence counts and durable
  semantic reads reconcile mechanically without leaking identifiers into the committed record.
  Tenant scope and checkout root are explicit; applied settings require hashed immediate before/after
  read snapshots bound to the exact scope and target.
- **Structural validation for docs-only brains.** `skills/local-brain-work/scripts/brain_structure.py`
  fills the gap where `brain_test.py` exits "no tests" — links/frontmatter/reachability/lint/raw-tracked/
  raw-history, plus `--expect-clean` for the post-cleanup scratch check.
- **Privacy lint extensions.** `brain_lint.py` gained a `--scratch` mode and first-class pattern classes
  for contact details, order/invoice/tracking/account identifiers, and opaque-ID/raw-filename leakage;
  names stay HARD in the tracked diff but downgrade to SOFT in scratch scanning.

## Behavioural reversals and narrowings from v1

These are deliberate product decisions, not refactors:

- **Cleanup ordering reversed.** v1 deleted the export at the end of the session, and the earlier v2
  draft deleted it *before* review. v2 deletes sensitive scratch **only after** the operator approves the
  diff. Deleting pre-review would leave the single human gate with no evidence to check, degrading it to
  a rubber stamp, and re-fetching after the ~48h export eviction is a production operation.
- **Critic-before-reduce fixed.** The v1 skill self-contradicted: it ordered Reduce before Critic while
  also describing the critic as running on the first draft. v2 fixes the order — the critic runs on the
  **untouched first-draft set**, before reduction, so contract violations die before they get polished
  into keepers.
- **Skip-evidence narrowed (§5).** v1 routed "mail you never answer" to `effect=skip` freely. v2 permits
  skip / sender-block / hard-skip rules **only** from presence-without-prose-reply evidence that is
  repeated, unambiguous, and machine-countable (the manifest `prose_reply` flag), each surfaced
  individually in the review brief with its occurrence count. Absence-based inference is prohibited
  (unanswered inbound mail is never exported), pending a paired inbound/no-reply export that does not
  exist yet.
- **Scope matrix corrected (§6).** Harvested persona is applied at tenant scope when tenant-bound and
  project scope otherwise; mailbox settings are checked only for shadowing overrides. **Triage policy
  and hard rules are project/tenant only — no mailbox scope exists.** Mailbox-derived triage evidence necessarily
  widens, so it is applied only with explicit scope authority, otherwise carried as a pending
  recommendation. v1's "narrowest scope project → tenant → mailbox" guidance did not reflect this.
- **Cluster-level resume replaces batches.** Resume granularity is the whole cluster: a draft either
  exists complete (proposal + report, self-lint clean) or the cluster reruns. There are no per-batch
  checkpoint files — they were bookkeeping insuring against a failure neither real run hit. Reads are a
  single stratified pass, not incremental batch rounds.

## What still requires replypen-cli / server work (out of scope here)

These are tracked in the spec's CLI/server deliverables, not in this kit change. The kit is designed to
work today regardless, because `prepare_harvest.py` parses raw v2 itself:

- **v2 split support (M, urgent):** released `rp` hard-rejects corpus format v2 in its splitter, but the
  server now emits v2 only. Until `rp` splits v2, use `rp project corpus download --out <file>` and let
  `prepare_harvest.py` parse the raw bytes.
- **`--out` rescue on split failure (S):** `rp` currently discards downloaded bytes when a split fails
  and makes `--out`/`--split` mutually exclusive, so there is no client-side rescue path; the fix should
  offer `--out` on split failure and name the ~48h re-download window.
- **Format advertising (S):** the export format is not in the API status projection and `rp self doctor`
  does not advertise supported corpus formats, so clients cannot see the format before download.
- **Inbound/no-reply decision export (L, future):** would unlock absence-based skip inference (§5), but
  requires a cross-subsystem join the export lane deliberately avoids today. Explicitly not a v2
  dependency.
