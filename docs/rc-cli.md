# `rc` CLI

`rc` is the public CLI over RootCause's JSON API. In a brain checkout, it uses `.rootcause.toml` plus
your `rc auth login` OAuth token to scope commands to the project/tenant. Use it instead of private
RootCause source or host tooling.

If a capability is not exposed through `rc`/API, use [`brain-publish`](../skills/brain-publish/SKILL.md)
to prepare a support request.

## Commands

```bash
rc ask "<customer-style question>"
rc ask "<direct investigation>" --scenario raw
rc ask "<q>" --brain-ref dev/<branch>
rc ask "<q>" --effort pro
rc run show <id>
rc run events <id>
rc run brain-diff <id>
rc run debug <id>
rc run trace <id> -o json
rc dev learning evidence --limit 50 -o json
rc run list [--limit N] [--kind email|prompt|mcp|analysis] [--category ok|timeout|...]
rc run feedback list [--score negative|positive|any] [--kind chat] [--since YYYY-MM-DD]
rc status
rc fleet runs [--days N] [--kind ...]
rc fleet patterns [--days N]
rc fleet health [--hours N]
rc run thread <id>
rc project mailbox ls
rc project mailbox harvest <mailbox-id> [--max-threads N] [--clean=true] [--wait]
rc project mailbox imap-env <mailbox-id> --out .rootcause/imap/<mailbox-id>.env
rc project corpus ls [-o json]
rc project corpus get <export-id>
rc project corpus download <export-id> [--out <file>] [--split <dir>]
rc project settings runtime get
rc project settings runtime set max_run_usd=5 default_tier=pro
rc project settings behavior get
rc project settings behavior set persona.guidance="..."
rc project triage policy get
rc project triage policy set "..."
rc project triage rules ls
rc project triage rules add effect=skip match_kind=subject_contains pattern="newsletter"
rc project triage rules add effect=force_process match_kind=sender_domain pattern="example.com"
rc project senders ls
rc project senders block "spam.example.com" --reason "known spam sender"
rc project senders allow "partner.example.com" --reason "trusted sender"
rc dev brain status --project <project> --scope project
rc dev brain sync --project <project>
rc dev brain preflight --project <project> --scope project --sha <exact-full-40-character-sha>
rc dev brain publish --project <project> --scope project --channel stable --sha <exact-full-40-character-sha>
rc dev brain promote --project <project> --scope project --channel stable --sha <exact-full-40-character-sha>
rc project env keys
rc project env pull
rc project env diff
printf %s "$SECRET_VALUE" | rc project env set key=FOO_API_TOKEN
rc project env rm FOO_API_TOKEN
rc project env reveal FOO_API_TOKEN
rc project database ls
rc project database get <DSN_ENV>
rc project database set <DSN_ENV> description="..."
rc project database controls get <DSN_ENV>
rc project connection ls
rc project connection add integration_key=<key> label=<label> tier=write token=<token>
rc project knowledge article apply --from <file.md> [--dry-run] [--publish]
rc project knowledge article get --provider <provider> --id <article-id>
rc auth login
rc auth status
rc self update
```

API-backed commands support `-o json` for scripting.

## Upgrade

```bash
rc self update            # non-Homebrew installs; Homebrew installs print the brew command
rc self update --check    # report only
brew update && brew upgrade rc  # Homebrew install
```

Cloud sandboxes (Claude Code cloud, Codex cloud) cannot reach GitHub release assets of unattached repos
(403 behind the GitHub proxy). Install there with the hosted bootstrap and let each session self-update:

```bash
curl -fsSL https://app.replypen.com/install/cloud.sh | bash   # rc (latest) + uv + pnpm; RC_CLOUD_SKIP_UV/PNPM=1 opt out
rc self update                                                 # falls back to the release mirror when GitHub is blocked
```
`RC_RELEASE_MIRROR=<url>` forces the mirror; `rc self update --check` prints `source: github|mirror`.

The current CLI no longer has the top-level `rc upgrade` command. If an older installed client does not
recognize `rc self update`, run its legacy `rc upgrade` once (or use Homebrew) to reach the current
command surface.

## Auth And Scope

`rc` is OAuth-only. `rc auth login` stores tokens under `~/.config/rootcause/tokens.json` with mode `0600`.
There is no API key file for this kit.

In a brain checkout, `.rootcause.toml` carries the non-secret project binding. Legacy `base_url` values
in brain/profile/token files no longer steer commands. Resolution:

```text
explicit --profile <name>          -> that profile's stored token
inside a brain + project token     -> profile named by .rootcause.toml project
inside a brain + no project token  -> default profile + .rootcause.toml project as ?project=
outside any brain                  -> default profile / built-in default (https://app.replypen.com)
base_url: ROOTCAUSE_BASE_URL > built-in production (https://app.replypen.com)
```

Use `rc auth status` to confirm profile and login-bound project/tenant; use `rc auth access` to inspect
token capabilities. On tenant-enabled projects, a tenant-pinned token scopes commands automatically.
A project-pinned login must pass `--tenant <slug>` to workspace-producing commands such as `rc ask`.
The global `--tenant` flag scopes supported runtime and collection commands; it is not the identifier
argument for editing a tenant record. Tenant settings and projection profiles take the slug positionally:

```bash
rc project tenant settings get <slug>
rc project tenant settings set <slug> persona.tone=direct
rc project tenant profile get <slug>
rc project tenant profile set <slug> key=value
```

Unsupported project/tenant selectors fail locally instead of being silently ignored. Per-key
`rc project env set/rm/reveal` only reaches a tenant env when the OAuth token itself is tenant-bound;
`--tenant` does not retarget those writes.

## Principal Scoping

A project may narrow runs below the tenant to one asserted end-user identity — a **principal** (e.g. a
parent or a staff leader on a family-admin product). The run's database views then already encode that
requester: hidden tables raise `lib.db.HiddenTableError`, per-audience columns are simply absent from
the view, and `RC_PRINCIPAL_SCOPED=1` / `RC_PRINCIPAL_KIND` mark the run. Brain scripts must tolerate
all three (lib.db auto-heals a SELECT naming a policy-hidden column) and must **never re-filter by ids
taken from the message body** — identity is host-asserted, not prompt-inferred.

`runctx.is_principal_scoped()` is the readable form of that mark (and carries `principal.kind` /
`external_id` with it) — [what kind of run is this](run-context.md).

The host also tells each sandbox what its projection contains, so a helper can ask instead of crashing.
Three env vars, all JSON keyed by the exact DSN env-var name, all best-effort (absent/malformed changes
nothing):

| Env var | Meaning | `lib.db` surface |
|---|---|---|
| `RC_DB_VISIBLE_TABLES` | the minted view names per scoped DSN (absent for flat/passthrough DSNs) | `visible_tables(db=None)` — `None` = unknown, never "nothing" |
| `RC_DB_HIDDEN_TABLES` | tables the requester's access policy removed | `hidden_tables(db=None)`, and the warning `tables()` emits |
| `RC_DB_EXCLUDED_COLUMNS` | per-table hidden/allowlisted columns | the `query` auto-heal + the warning `columns()` emits |

`is_visible("form_fields")` collapses the first two into `True` / `False` / `None`. Querying a hidden
table raises `HiddenTableError` (a `RuntimeError` subclass with `.table` / `.db`) instead of a bare
`UndefinedTable`; a relation in neither list is reported as genuinely absent from the projection, which
is the typo case. Rule for helpers: **degrade, never fail** — a hidden enrichment table means fewer
columns in the answer, not an error, and its name is never echoed to the requester.

Replicating a real per-principal session end to end — the schema/visibility matrix and the per-audience
smoke matrix — is the [`scope-check`](../skills/scope-check/SKILL.md) skill.

What you can do from a brain checkout:

```bash
# The one real per-identity preview: mints the same scoped views a run gets, reports
# per-table counts, sample rows, compiled predicates, and unconstrained_tables.
rc project database preview <DSN_ENV> --tenant <slug> \
  --principal-kind <kind> --principal-id <uuid> -o json
# Omit the principal flags for the tenant-wide view; --table <name> narrows to one view.
```

The scoping rules themselves (`scope_manifest` / `principal_manifest` on the project row) are
operator-owned data — not editable through public `rc`. Propose changes via a RootCause support
request. Practices that keep proposed rules readable and safe:

- Derive per-kind **typed claims** once (`uuid[]`, `allow_empty`) and keep every predicate a
  membership test (`col = ANY(:my_ids)`) instead of repeating subqueries per table.
- Have each kind derive every claim (the other kind's as empty sets): an empty array matches no row
  naturally, so predicates need no `:principal_kind = '…'` guards.
- Whole table irrelevant/sensitive to end users → `principal_hidden`; per-audience column →
  `principal_exclude_columns` ({kind → [cols]}, `"*"` = every asserted kind). Exclusion beats
  masking — the column is gone, not tokenized.
- Verify every kind with `preview` after a change; the engine fails closed (a missing claim refuses
  the run) rather than silently widening, so an error here is the guardrail working.

## `rc ask`

`rc ask` triggers a real production run and waits by default.

- Default scenario is email simulation: draft/note/actions/PRs as a reviewer would see them.
- `--scenario raw` returns a direct investigation answer.
- `--brain-ref dev/<branch>` tests a pushed dev branch on production infrastructure without moving
  live refs. It creates a test run: no callback, no durable journal push, and proposals are test
  artifacts.
- `--effort pro|max` is an explicit stronger-tier retry.

Use [docs/side-effects.md](side-effects.md) when reporting what a run did.

## Debug A Run

```bash
rc run debug <id>
```

This writes `.rootcause/debug/<run8>-<project>.{md,jsonl}` by default. Read the markdown index first, then query JSONL
with `jq`. Use [`rc-debug`](../skills/rc-debug/SKILL.md) for the analysis-first workflow and
[docs/run-trace-model.md](run-trace-model.md) for the mental model.

A pasted run link works with **no login and no profile** — the `?t=` token is the credential:
`rc run debug 'https://app.replypen.com/runs/<id>?t=<token>'` writes the same two artifacts. A chat
share link goes through `rc run session 'https://app.replypen.com/s/<token>'`, which writes one
`.rootcause/debug/session-<id>.md` (transcript + the runs behind it, each with its drill command).
To debug a bad chat answer end to end (case file, replay on a dev ref, compare), use
[`brain-chat-debug`](../skills/brain-chat-debug/SKILL.md).

Refused (`UNKNOWN_RUN`/404, or not logged in)? Since rc 1.27.2 the error carries a `hint:` line (a
`hint` field in `-o json`) — a bare id you cannot see is indistinguishable from no login, so rerun
with the full link, or `--share-token <token>` (`rc run debug|thread|trace` all take it).

## Dream Evidence

```bash
rc dev learning evidence --limit 50 -o json
rc dev learning evidence --plane deltas --include-bodies -o json
rc fleet runs --days 14 --learning=sent_delta --format agent
```

Use this for a local dream-cycle pass from a brain checkout. It returns ranked planes — human
feedback on runs, sent-vs-proposed deltas, and triage skips/corrections — selectable with `--plane
feedback|deltas|triage`. Raw `proposed_body`/`sent_body` are omitted unless `--include-bodies` is
passed. `rc fleet runs --learning[=<signal>]` reaches the same candidates from the run index, but
carries only the pointer (`LRN:` flag), never the bodies.

Close the loop on feedback you acted on:

```bash
rc run feedback <run-id> --processed --resolution-note "brain runbook updated"
rc run feedback <run-id> --unprocessed
```

Mark handled feedback processed so the next fleet review or dream cycle can skip it; the optional
`--resolution-note` records what was done. Both need project-admin authority (tenant admins are
denied), and processed rows render greyed-out on the web Feedback tab.

Render the delta plane with
[`sent_delta_report.py`](../skills/brain-dream-cycle/scripts/sent_delta_report.py) rather than reading
JSON. One call, two files under the gitignored `.rootcause/dream/`: a token-lean `.md` for the agent
(signal index, `[-removed-]`/`{+added+}` bodies, run ids) and a `.html` for the human (side-by-side
word-level diff). Both embed raw customer mail.

Two gaps worth knowing: the endpoint has no date window, only `--limit` over a divergence ranking; and
delta rows carry no `run_url`, so a report cannot link to the run page the way `rc fleet actions` rows
can. Minting a link would cost one API call per delta.

Drill only the runs that justify an edit with `rc run debug <id>`. Use
[`brain-dream-cycle`](../skills/brain-dream-cycle/SKILL.md) for the full brain/persona/triage decision
workflow.

## Mailbox Harvest And Exports

```bash
rc project mailbox ls -o json
rc project mailbox harvest <mailbox-id> --max-threads 1000
rc project corpus ls -o json
rc project corpus get <export-id>
rc project corpus download <export-id> --split .rootcause/exports/<export-id>/
```

`rc project mailbox harvest` triggers a **production** provider sweep of a mailbox's sent history into a stored,
cleaned Markdown corpus; `--wait` polls to completion, `--max-threads` caps the sweep, `--clean=true`
(default) strips quoting/signatures. `rc project corpus ls` answers "has this mailbox been harvested?"
(id, kind, status, thread_count, truncated, created/completed); `rc project corpus get` shows one
export's status. `rc project corpus download` fetches the corpus and marks it consumed — `--split
<dir>` materializes it as
`<dir>/INDEX.md` + `<dir>/threads/<yyyy-mm>--<slug>--<idx>.md`. The default split dir
`.rootcause/exports/<export-id>/` sits under the wholesale-gitignored `.rootcause/` and MUST stay
gitignored: it is raw customer mail, never brain content.

Hosted IMAP harvest is shallow/smoke-only when the server applies the IMAP cap (currently 100 refs) and
returns a warning. Deep IMAP uses `rc project mailbox imap-env` to write a gitignored secret env file, then the
`brain-harvest` script `$SKILL/scripts/local_imap_harvest.py` to produce the
`.rootcause/exports/...` tree. The exporter v1 reads the sent folder with safe caps and groups messages
locally; it does not full-expand every referenced inbound message across folders yet.

Use [`brain-harvest`](../skills/brain-harvest/SKILL.md) for the full acquire → cluster → distil →
verify → publish → delete workflow. See [docs/side-effects.md](side-effects.md) for the harvest/download
side effects before running either.

## Persona And Triage

```bash
rc project settings behavior get -o json
rc project settings behavior set persona.tone="..." persona.guidance="..."
rc project triage policy get -o json
rc project triage policy set "..."
rc project triage rules ls -o json
rc project triage rules add effect=skip match_kind=subject_contains pattern="newsletter" reason="marketing mail"
rc project triage rules add effect=force_process match_kind=sender_domain pattern="example.com" reason="VIP support domain"
rc project senders ls -o json
rc project senders block "spam.example.com" --reason "known spam sender"
rc project senders allow "partner.example.com" --reason "trusted sender"
```

Persona settings own voice, language, formality, signature, and wording preferences. Triage policy/rules
own whether inbound mail deserves a draft; sender settings own spam allow/block rules. Deterministic
blacklists/whitelists belong on these settings surfaces, never in brain prose or brain tests. Product
facts and runbooks belong in the brain.

## Env

```bash
rc project env keys
rc project env pull
rc project env diff
printf %s "$SECRET_VALUE" | rc project env set key=FOO_API_TOKEN
rc project env rm FOO_API_TOKEN
rc project env reveal FOO_API_TOKEN
```

`rc project env pull` writes the production grounding `.env` to the brain root with mode `0600`; values
are not printed. `rc project env set` adds or rotates one grounding secret, reading the value from STDIN
by default and never echoing it. `rc project env reveal` is the deliberate exception: it prints one live value for copy/pipe use
and is audited. The file is gitignored and contains real secrets.

There is no `rc project database add` (rc >= 1.18.1 documents this in `rc project database --help`): a
database exists because a sealed `<PROJECT>_<DBKEY>_DSN` grounding key exists. `rc project database ls`
lists the DSNs the project has *configured* (description, scope manifest, or PII columns), so a freshly
sealed DSN appears there only after `rc project database set <DSN_ENV> description=…`. Use `rc dev
console database list` to check whether a DSN actually exists and connects. Recipe:
[docs/secrets.md](secrets.md#register-a-new-grounding-database).

For the full choose-the-store flow, tenant behavior, and action write-plane rules, read
[docs/secrets.md](secrets.md).

## Brain Cache And Channels

```bash
rc dev brain status --project <project> --scope project -o json
rc dev brain sync --project <project>
rc dev brain render --project <project> --tenant <slug> [--path AGENTS.md]... [--all] [--sha <sha> | --channel stable|edge] [-o json]
rc dev brain preflight --project <project> --scope project --sha <exact-full-40-character-sha> [--channel stable|edge] -o json
rc dev brain publish --project <project> --scope project --channel stable --sha <exact-full-40-character-sha> -o json
rc dev brain promote --project <project> --scope project --channel stable --sha <exact-full-40-character-sha> -o json
rc dev console bash list
```

Use after pushing a brain commit. `status` fetches `origin/main` and reports mounted SHA, origin SHA,
staleness, sync time, and resolved `stable`/`edge` channel SHAs with origin comparison/provenance.
`sync` fast-forwards the managed `main` cache when safe and expires warm console workspaces; the next
`rc dev console bash run` remounts the refreshed `/brain`.

`publish` (rc >= 1.16.5) is the preferred one-shot: sync → promote → verify one exact SHA on one
channel, non-zero exit on mismatch, `-o json` receipt. `preflight` dry-runs a promotion and reports
which tenants' projections the candidate commit would break — run it before moving `stable`.

`render` (rc >= 1.17.0) prints one tenant's **compiled** projection — placeholders filled, `rc:branch`
regions collapsed — exactly as `/brain` mounts it, with a header (sha, channel, tenant,
filled/branches/degraded counts and degradations); `-o json` returns the raw files. It is the way to
eyeball a projection; `preflight` only says pass/fail. Requires a project-level login
(`brain:promote` scope).

Pass `--project <project>` explicitly (older clients mis-resolve an implicit project onto retired flat
routes and 404), and `--scope project` for anything channel-related: in a tenant context these
commands otherwise answer about the tenant overlay brain, not the project channels.

For a shared project brain, `promote` moves only the named channel to the exact reachable SHA. It
requires a project-level maintainer login; tenant-scoped tokens are denied because the channel is
shared by every tenant. Tenant brains use `main` and have no promotion route. The JSON result includes
`project`, `channel`, `old_sha`, `new_sha`, `changed`, and `idempotent`. Reject or stop on unknown SHAs, unsafe
channels, wrong-project authorization, or push failure.

Never infer channel freshness from `main` being current. Confirm the intended channel resolves the
exact SHA at `.status.channels[] | select(.channel == "stable") | .resolved_sha` (substitute `edge` as
needed), or create a safe normal `rc ask` without `--brain-ref` and confirm its `brain_resolved` trace.
Each channel entry also reports `origin_sha` (= `origin/<channel>`, not `origin/main`), `main_sha`
(= `origin/main`), `matches_origin` (channel ref equals its pushed branch), `matches_main` (channel at the
`main` tip), `state`, and `provenance`. If sync reports manual reconcile, use `brain-publish` with the status output.

## DB And Bash Examples

Discover before querying:

```bash
rc auth status -o json
rc dev console database list -o json   # each db carries a `writable` column: true when a <X>_WRITE_DSN is sealed
rc dev console database schema <db> --table <table> -o json
rc dev console database schema <db> --table <table> -o json |
  jq -r '.. | objects | select(has("name") and has("type")) | [.name,.type] | @tsv'
```

Then run narrow SQL. Queries are read-only by default; `--write` uses the action write plane where a
`<X>_WRITE_DSN` is sealed (scope `console:db:write`) and COMMITs, returning `rows_affected`:

```bash
rc -o json dev console database query <db> "select count(*) as row_count from <table>" --format json --out - | jq '.rows[0]'
rc dev console database query <db> "select id::text, created_at from <table> order by created_at desc" --all --format csv --out ./rows.csv
rc dev console database query <db> "update <table> set <col>=<val> where id=<id> returning id" --write -o json | jq '.rows_affected'
```

If a query fails with an unknown column, go back to `rc dev console database schema`; do not keep guessing names.
Use repeated `--param key=value` instead of quoting values into SQL. `--all` is required for a complete
result beyond the inline cap and streams one server-side cursor from a single repeatable-read transaction;
it does not require `ORDER BY` for completeness, though ordered output still needs one. A truncated inline
result exits 3 unless explicitly allowed. For a deterministic
script, use [`rc-script-wrapper`](../skills/rc-script-wrapper/SKILL.md): its `lib.rc_client` wrapper
keeps column order/duplicates and turns CLI exit codes into typed exceptions. Fetch a previewed remote
workspace artifact with `rc dev console file get <remote-path> --out <local-path>`.

Use `rc dev console bash run` for workspace files and mounted context:

```bash
rc dev console bash run 'find /brain -maxdepth 2 -type f | sed -n "1,80p"'
rc dev console bash run 'find /kb -maxdepth 3 -type d -print | sed -n "1,120p"'
rc dev console bash run 'rg -n -i "invoice|payment|refund" /kb /brain/knowledge -g "*.md" 2>/dev/null | sed -n "1,60p"'
```

For KB title/frontmatter indexes, see [knowledge-base.md](knowledge-base.md).

## Help Centre Articles

Writing back to the project's public help centre (Help Scout Docs, Intercom, KnowledgeOwl). A real `apply` needs scope `knowledge:write`; `--dry-run`/`get` work with read.
The input is a `replypen: helpcenter/v1` markdown block — produced by
[`brain-helpcenter-suggestions`](../skills/brain-helpcenter-suggestions/SKILL.md), applied by
[`brain-helpcenter-publish`](../skills/brain-helpcenter-publish/SKILL.md). Requires `rc` >= 1.26.0 and
a one-time write grant:

```bash
rc project connection add integration_key=helpscout_docs label=help-center tier=write token=<token>
rc project connection ls                                  # the row shows `tier: write`
```

`integration_key` is `helpscout_docs` (Docs API key) or `intercom` (token with Articles write).
Write-tier connections are host-only: they are never injected into a run workspace.

```bash
rc project knowledge article apply --from ./article.md --dry-run   # prints op/changes + the provider request
rc project knowledge article apply --from ./article.md             # real write; create/update lands as a draft
rc project knowledge article apply --from ./article.md --publish   # change or publish the LIVE article
rc project knowledge article get --provider helpscout --id <article-id>     # read back as a fresh `op: update` block
rc --tenant <slug> project knowledge article apply --from ./article.md   # one help centre per tenant
```

`apply` is a **side effect** ([side-effects.md](side-effects.md)); `--dry-run` and `get` are read-only.
A no-op block reports `changed: false` and sends nothing. Every real write lands an
`helpcenter.article.update` row in `rc fleet actions` and queues a `/kb` resync.

Prefer `rc dev console database` and `rc dev console bash` for debugging, tool parity, and "does this script/query work?" loops.
They run the production primitive directly and return faster than `rc ask`, which adds the LLM wrapper
and is best for full-loop behavior checks or ambiguous investigations.

## Author -> Verify Loop

Before changing a brain or action blindly:

1. Find real cases with `rc run list`, `rc fleet runs`, or `rc fleet patterns`.
2. Inspect evidence with `rc run events <id>` or `rc-debug`.
3. Edit the brain from evidence.
4. Verify locally with `local-brain-work`.
5. Verify production behavior with `rc ask --brain-ref dev/<branch>` when needed.
6. Finish with `brain-publish`.
