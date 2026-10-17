---
name: brain-harvest
description: "Turn a mailbox's historical sent mail into durable brain knowledge plus persona/triage settings, locally from a brain checkout with the public rp CLI. Use when onboarding a mailbox's past replies, seeding or growing a brain from a sent-history export, or when drafts look foreign (font/signature probe in voice-format.md)."
---

# brain-harvest — synthesize a brain from historical sent mail

Run from inside a project or tenant brain checkout. Where
[`brain-dream-cycle`](../brain-dream-cycle/SKILL.md) mines a handful of recent runs, harvest sweeps the
full sent corpus once, partitions it deterministically, and distils durable patterns. Synthesis runs
**locally in your coding-agent session** so it uses your stronger-model subscription instead of a hosted
tier; the corpus lands on the laptop only for the session.

Public-CLI only: no ReplyPen private source, host shell, SSM, raw registry SQL, or private operator
scripts. If a needed read/write is not exposed through `rp`, finish with a support request through
[`brain-publish`](../brain-publish/SKILL.md).

**Privacy is the hard rule.** The corpus is real customer mail. Only *distilled patterns* may land in
tracked brain files — never raw thread text, credentials, patient data, addresses, payment links,
correspondent names, or the opaque IDs that index the scratch corpus. Corpus, manifests, cluster drafts,
critic notes, review brief and evidence filenames are all sensitive until reduced; they live under one
gitignored scratch root and are deleted only after the human gate. A `brain_lint.py` HARD finding blocks
a commit, full stop.

**Every harvest has two required outputs:** durable business knowledge in the brain, *and* a distilled
reply voice in persona settings. Voice goes to the harvested business scope — tenant settings for a
tenant checkout, otherwise project. Never make the mailbox the durable home for harvested voice; inspect
mailbox overrides only to confirm they do not shadow the chosen values.

## Map

- [`docs/specs/brain-harvest-long-horizon-v2.md`](../../docs/specs/brain-harvest-long-horizon-v2.md) —
  the design this pipeline implements and the **rationale authority** for the §-numbered rules below
  (§5 sent-only bias, §5a eras, §6 scope, §7 privacy, §8 ordering, §10 evaluation).
- [`docs/brain-harvest-v2-migration.md`](../../docs/brain-harvest-v2-migration.md) — what changed from
  the v1 manual flow, the deliberate reversals/narrowings, and the open `rp`/server gaps.
- [`docs/brain-model.md`](../../docs/brain-model.md) — what belongs in a brain and its layout.
- [`docs/rp-cli.md`](../../docs/rp-cli.md) — public command scope, incl. harvest/export syntax.
- [`docs/side-effects.md`](../../docs/side-effects.md) · [`docs/support-boundary.md`](../../docs/support-boundary.md).
- [`acquire.md`](acquire.md) — per-provider corpus acquisition (Gmail templates, Intercom, IMAP).
- [`voice-format.md`](voice-format.md) — the standalone draft font + signature probe.
- [`templates/`](templates/) — pipeline prompts (cluster / critic / reduction), operator report formats
  (review brief, harvest record), and two archetype brain skeletons. Read
  [`templates/README.md`](templates/README.md) first.
- [`brain-ask`](../brain-ask/SKILL.md) (production verification) · [`brain-publish`](../brain-publish/SKILL.md) (publish / support request).

## Scratch root

One gitignored root holds **everything sensitive** for the run. `.replypen/` is wholesale-gitignored in
a brain checkout, but the scripts re-verify with `git check-ignore` and refuse to write a stageable root.

```
.replypen/harvest/<tag>/     # <tag> = a local scratch label, never the export id
  corpus/ threads/ manifest.jsonl clusters.json ledger.json holdout.json run.json
  replay-cases.json diagnostics.{json,md} templates.json
  drafts/ critic/ brief/ settings-verification/
```

```bash
SKILL=<absolute path to skills/brain-harvest>
LBW=<absolute path to skills/local-brain-work>   # brain_structure.py lives here
TAG=<operator-chosen-local-run-tag>
EXPORT_ID=<actual-rc-export-id>
SCRATCH=.replypen/harvest/$TAG
```

## Workflow

Twelve steps in exactly this order (§8). ⚙ = script invocation with machine-checkable output. All state
lives in the scratch root plus the tracked working diff, so **any step is re-entrant**; the only
irreversible transition is scratch deletion in step 12, which is why it happens after approval.

### 1. ⚙ Preflight and acquire

Inventory scope and existing configuration first, so project/tenant mistakes fail early and synthesis
knows what grounding/persona/triage already exist before proposing homes. **Never infer "no grounding"
from a local repo search.** If `rp dev brain status -o json` shows `status.brain_source.read_only == true`, stop —
learning for a read-only brain alias happens only from the source project; consumer runs are QA
evidence, not learning input.

```bash
rp auth status && rp auth access && rp project mailbox ls
git status --short --branch && git pull --ff-only

rp project settings behavior get -o json
rp project triage policy get -o json
rp project triage rules ls -o json
rp dev console database list -o json   # grounding databases already wired
rp dev console capabilities            # cataloged brain scripts / tools
rp fleet health                        # mirrors mounted, and their freshness
```

Acquire the corpus per provider — see [`acquire.md`](acquire.md).

Then bind the target. The unbound form (`preflight --scratch` alone) is a local diagnostic that degrades
to WARN without `rp`; `prepare` requires the bound form:

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" preflight \
  --scratch "$SCRATCH" --project <project> --mailbox <mailbox-id> \
  [--tenant <tenant-slug>] --provider <google|microsoft|imap|intercom> --export-id "$EXPORT_ID"
```

It sniffs the acquired format, checks Git/gitignore state plus the public auth, scope, persona, triage,
grounding, capability, mirror, corpus-history, export and doctor surfaces, and writes private
`$SCRATCH/preflight.json` (target, scope matrix, results — never raw command output). A `FAIL` must be
fixed before proceeding; a bound target **fails closed** unless export metadata proves the exact target.

In a tenant checkout, route tenant-specific distillations to the tenant brain or tenant settings unless
they clearly apply to the shared project.

### 2. ⚙ Deterministic prepare, verify

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" prepare \
  --corpus "$SCRATCH/corpus/" --templates "$SCRATCH/templates/export.json" \
  --scratch "$SCRATCH" --export-id "$EXPORT_ID"
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" verify --scratch "$SCRATCH"
```

`prepare` binds the verified preflight into `run.json`, then writes the whole synthesis input set
atomically and idempotently over the same corpus bytes (see the script's module docstring for the file
list, and spec §1/§3 for why each exists). What you must know as the operator:

- IDs are **opaque and content-derived** (`H<32-hex>`), stable across full/delta overlap; raw filenames
  stay internal and indistinguishable duplicates hard-fail.
- Metadata is extracted **without LLM synthesis** — including the `prose_reply` flag (the only legitimate
  negative signal, §5) and the era band (§5a).
- Clustering is **deliberately dumb** and carries a mandatory `mixed` bucket: it is a work-partitioning
  unit, not routing truth. Generic subjects ("contact", "order", "invoice") never determine a topic.
- The **holdout** (default 8) is excluded from every synthesis read and never written under `threads/`.
  If the requested count is unavailable, `prepare` hard-fails — deliberately lower `--holdout` or acquire
  a larger corpus rather than silently shrinking the evaluation.

Two artifacts gate fan-out. Read `diagnostics.md` (counts only, no bodies/names/IDs): an unexpected or
noisy distribution is a stop-and-fix signal. Inspect `ledger.json` → `risk`: `over_cap: true` means the
forced-deep set exceeds the cap (default 15%) — prune risk markers via `--config` and re-run `prepare`,
rather than silently reintroducing read-everything.

Every numeric knob (sample cap, era bands, holdout, risk cap, prose-reply threshold) is a **tunable
default** via `--config <json>` / `--holdout N` / `--seed N`, never a hard constant.

### 3. Bounded per-cluster drafts

Fan out one coding-agent subagent per cluster with
[`templates/cluster-agent-prompt.md`](templates/cluster-agent-prompt.md), substituting the `{{…}}` slots
from `clusters.json`. That template owns the full contract (reads, delta format, era tags, §5 gate,
honesty signals, privacy, self-lint, the two output files). Orchestrator-side rules:

- Each subagent reads **only** its `sample_ids` in a single pass **plus every** `deep_read_ids` — no
  incremental batch rounds, no reading beyond the plan.
- **Resume granularity is the cluster**: a draft either exists complete (proposal + report, self-lint
  clean) or the cluster reruns. Adjacent small clusters may share one assignment; a large diverse cluster
  should be split and reduced once, not handed wholesale to one agent.
- An honest `still_yielding: true` at the cap earns **one** orchestrator-controlled follow-up — never let
  the agent silently read more:
  ```bash
  uv run --no-project python "$SKILL/scripts/prepare_harvest.py" ledger expand \
    --scratch "$SCRATCH" --cluster <cluster-id> --count <bounded-count>
  ```
- Fold reports back so coverage and route-elsewhere reassignments are recorded. `ledger apply` validates
  the strict schema, hard-fails on unknown/non-assigned/holdout IDs, and persists nothing if the merge
  would break invariants:
  ```bash
  uv run --no-project python "$SKILL/scripts/prepare_harvest.py" ledger apply \
    --scratch "$SCRATCH" "$SCRATCH"/drafts/*.report.json
  ```

### 4. Induce one candidate taxonomy

Orchestrator work over the draft proposals only, no new corpus reads: merge the per-cluster returns into
one induced tree of topics/homes that reduction will tighten every proposal against.

### 5. Early critic on the untouched first-draft set

One critic subagent over **every** `drafts/<cluster>.md` + `.report.json`, using
[`templates/critic-prompt.md`](templates/critic-prompt.md), **before any reduction** — reducing first
would hide the raw cross-cluster picture the critic needs. It judges and flags only (never edits
proposals) and writes advisory notes to `$SCRATCH/critic/critic.md`.

### 6. Per-topic reduction

[`templates/reduction-prompt.md`](templates/reduction-prompt.md) turns the critic-judged first draft into
tight final deltas against the induced taxonomy: apply the critic, resolve contradictions where evidence
reconciles and **surface** the rest, apply era supersessions (prefer recent), collapse restatements into
one delta per fact/rule, keep the brain/persona/triage split clean. Output is `critic/reduced.md` plus the
strict `critic/reduced.json` contract. Nothing tracked is written yet.

### 7. Tracked edits plus narrow settings changes

Apply the reduced deltas. Start from the matching [archetype skeleton](templates/) — product-support or
personal/mixed — and **edit** it rather than inventing new top-level structure. Search before adding:

```bash
rg -n "<customer phrase>|<internal term>|<policy name>" AGENTS.md skills notes playbooks actions terminology.md 2>/dev/null
```

**Durable home** for each reduced signal:

| Distilled signal says | Write to |
|---|---|
| Product fact, routing, terminology, source-of-truth pointer, repeatable investigation/playbook | Brain files. |
| Missing reusable script, action instructions, action selection rules | Brain files or `actions/<id>/`. |
| Voice, language, signature, formality, wording preference, "sound more like us" | Persona settings via `rp project settings behavior`. |
| Drafts look foreign: wrong font, missing signature/logo | `channel.draft_font_css` + `channel.signature_html` — see [`voice-format.md`](voice-format.md). |
| Which inbound mail should become a draft, broad draft/no-draft guidance | Triage policy via `rp project triage policy`. |
| Deterministic draft blacklist/whitelist by sender/subject/header | Triage hard rule via `rp project triage rules` (`skip` / `force_process`). |
| Shared project channel promotion, or a missing public surface | [`brain-publish`](../brain-publish/SKILL.md). |

Onboarding-shaped outputs land where the mechanical seeder points: `notes/onboarding-inbox.md` (survey
facts) and `notes/mailbox-patterns.md` (distilled patterns), plus case/terminology files. Never copy a
deterministic selector into brain prose or a brain test — apply it only through the settings surface so
the UI and CLI share one source of truth.

**Scope matrix (§6).** Re-read current values immediately before mutating, and verify the resolved source
after.

| Signal | Narrowest writable target today | Rule |
|---|---|---|
| Persona | **tenant when tenant-bound, else project** | Mandatory harvest output; never mailbox. |
| Triage policy | **tenant or project — no mailbox scope exists** | Mailbox-derived evidence necessarily widens; widen only with explicit scope authority, else a **pending recommendation**. |
| Hard rules | **tenant or project — no mailbox scope exists** | Same widening rule; deterministic evidence per §5. |
| Brain facts | tenant or project brain | Match the business scope of the fact. |

Persona synthesis is mandatory: derive concise `tone`, `language`, `formality`, `guidance` from the
recent human replies; set `signature` only on repeated evidence. Record a gap rather than inventing a
value. A harvest is not complete until the harvested mailbox *resolves* those fields from the target
tenant/project — reconcile or surface any mailbox override that shadows them.

```bash
# tenant when tenant-bound, otherwise project
rp project tenant settings set <slug> persona.tone="..." persona.language="..." persona.formality="..." persona.guidance="..."
rp project settings behavior set persona.tone="..." persona.language="..." persona.formality="..." persona.guidance="..."

rp project triage policy set "Draft customer support + billing questions; skip vendor newsletters and automated alerts."
rp project triage rules add effect=force_process match_kind=sender_domain pattern="partner.com" reason="partner mailbox — always answered"
rp project triage rules add effect=skip match_kind=subject_contains pattern="unsubscribe" reason="presence-without-prose-reply, N occurrences"
```

For tenant triage, use the explicit global `--project <project> --tenant <slug>` selectors on both reads
and the write. For every change marked `applied`, save the immediate pre/post `get -o json` responses
under `$SCRATCH/settings-verification/` and record their filenames, SHA-256 digests, timestamps, resolved
scope and exact target in `reduced.json` (post-read within five minutes); `review` hashes both and
reconciles them to preflight. Pending recommendations use `"verification": null`. A temporary rule created
only to verify the contract must be removed with `rp project triage rules rm <id>` before finishing.

**Skip-evidence gate (§5).** A sent-history corpus proves only what the mailbox *answered*, and
unanswered inbound mail is never exported — so **absence proves nothing** and no skip rule may be inferred
from a sender or subject being missing or rare. The one legitimate negative signal is
**presence-without-prose-reply**: recurring in-corpus threads with `prose_reply=false`.

- Persona, terminology, intake, routing and historical handling are supported outputs.
- `force_process` needs repeated deterministic *positive* evidence.
- `skip` / sender blocks need presence-without-prose-reply evidence that is repeated, unambiguous and
  machine-countable. Raw frequency of a subject or domain is not evidence of actionability.
- Every skip proposal is surfaced **individually** in the review brief with its occurrence count, never
  applied silently.

### 8. ⚙ Lint — scratch drafts and staged brain

```bash
# scratch: opaque IDs/raw filenames expected there and suppressed; secrets/raw-thread/payment/
# identifier/name classes still apply (names downgrade HARD→SOFT).
uv run --no-project python "$SKILL/scripts/brain_lint.py" --scratch "$SCRATCH/drafts/"

git add <brain files you wrote>
uv run --no-project python "$SKILL/scripts/brain_lint.py"                 # staged pre-commit gate
uv run --no-project python "$SKILL/scripts/brain_lint.py" --all --strict  # whole-tree; address/persona warnings fatal too
```

Secrets, raw-thread shape, payment links/IBANs, contact details, order/invoice/tracking/account
identifiers, opaque-ID or raw-filename leakage into tracked files, and `rp` command roots hard-block. (The
kit checkout is exempt from the `rp`-command rule because its local-development skills intentionally
document the CLI; a brain checkout is not.)

**A history-rewrite decision must be explicit.** If a legacy onboarding path committed raw mail (e.g. a
`notes/past-replies.md`), deleting the file leaves it in git history. Do **not** silently `git rm` —
escalate to the operator with the exact path and commit; scrubbing history is a deliberate, coordinated
rewrite. (Precedent: one such file was deleted post-onboarding and a real credential had been committed.)

### 9. Independent staged-diff review

Review the full staged diff with a fresh reviewer subagent: every claim a distilled pattern traceable to
corpus evidence, homes correct, no raw data or opaque IDs, settings changes within the §6 matrix. Fix
findings **against the still-present corpus**, before the gate.

### 10. ⚙ Review brief, sanitized replay, held-out evaluation

Commit the staged edits to a local `dev/<branch>` (a WIP commit is fine — the mandatory gate guards the
push to `main`/publish, not dev refs), push, and replay every reserved question against that ref:

```bash
git checkout -b dev/<branch> && git commit -m "wip: harvest draft"
git push origin dev/<branch>
rp ask "<held-out inbound question>" --brain-ref dev/<branch>
rp run debug <run-id>
```

Have a comparison agent score each answer against its paired historical answer (0–4: factual agreement,
routing, tone), then run one distinct representative full production replay.
[`templates/review-brief.md`](templates/review-brief.md) owns the `evaluation.json` / `metrics.json`
schemas and what the generator validates.

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" review \
  --scratch "$SCRATCH" \
  --agent-report "$SCRATCH"/drafts/*.report.json \
  --reduction "$SCRATCH/critic/reduced.json" \
  --evaluation "$SCRATCH/brief/evaluation.json" \
  --metrics "$SCRATCH/brief/metrics.json" \
  --harvest-date "$(date +%F)" --kit-version <installed-kit-version>
```

Then the structural validator (lint already ran in step 8) — link/route targets resolve, skill frontmatter
valid, routed case files reachable, no raw-harvest path tracked now or in history:

```bash
uv run --no-project python "$LBW/scripts/brain_structure.py" --skip lint
```

### 11. Mandatory operator diff approval

Pause for the single human gate **with the local evidence brief still present**. The operator consults
`$SCRATCH/brief/review-brief.md`, the exact `record-candidate.json`, and — via opaque IDs — the local
corpus, then approves each settings change and each skip proposal individually. Not optional, never a
rubber stamp; which is exactly why cleanup happens after it.

### 12. ⚙ Cleanup, then publish

Only **after** approval, promote the already-reviewed candidate into the tracked diff, then delete all
sensitive scratch and prove it is gone:

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" record \
  --scratch "$SCRATCH" --out "notes/harvest-records/$(date +%F).json" --approved
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" cleanup --scratch "$SCRATCH" --yes
uv run --no-project python "$LBW/scripts/brain_structure.py" --expect-clean
```

`record` writes the approved candidate byte-for-byte and refuses without `--approved`; `cleanup` refuses
without `--yes`. Then finalize the commit including the generated
[harvest record](templates/harvest-record.md), fast-forward to `main` via
[`brain-git-sync`](../brain-git-sync/SKILL.md), and run the full
[`brain-publish`](../brain-publish/SKILL.md) flow (dev-ref replay, server sync, channel promote,
exact-SHA verification). Settings-only changes: record the exact `rp` commands and the verification run
id. The harvest record is the watermark for a future incremental `--since` re-harvest.

## Fallback: the v1 manual path (one release only)

Only for a corpus `prepare_harvest.py` genuinely cannot parse (a `harvest_format` outside v1/v2/v3): fall
back to `rp project corpus download --split` into a gitignored dir, read its `INDEX.md`, cluster from
thread metadata, then the same fan-out → critic-first → reduce → homes → lint → verify → publish → delete
sequence. Privacy, critic-before-reduce and post-approval cleanup still apply; only the deterministic
manifest/ledger/holdout machinery is unavailable. Never use it to bypass rejected v3 structure or bad
diagnostics — reacquire or fix the exporter instead.
