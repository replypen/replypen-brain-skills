# Acquiring a harvest corpus (step 1 detail)

Provider branches for `brain-harvest` step 1. Command syntax and semantics live in
[`docs/rp-cli.md`](../../docs/rp-cli.md); side effects in
[`docs/side-effects.md`](../../docs/side-effects.md). This file only carries what the operator must
*decide* per provider.

Reuse a fresh export if one exists (`rp project corpus ls -o json`) — `download` starts the ~48h
server-side eviction window, and re-fetching after it is a production operation.

## Hosted providers (Gmail / Microsoft / Intercom)

```bash
rp project corpus ls -o json
rp project mailbox harvest <mailbox-id> --max-threads 1000
rp project corpus get <export-id>                  # poll until terminal
rp project corpus download <export-id> --out "$SCRATCH/corpus/corpus.md"
```

Use `--out` and let `prepare_harvest.py` parse the raw bytes: it reads corpus **v1, v2 and v3**, so
`--split` is a convenience, not a dependency. Today it is also the only working path — the server emits
v2 and released `rp` hard-rejects v2 in its splitter (see
[the migration note's CLI gaps](../../docs/brain-harvest-v2-migration.md)). An existing `--split`
directory from an older export can be passed to `prepare` instead.

## Gmail: canned responses as a second export

```bash
mkdir -p "$SCRATCH/templates"
rp project mailbox templates <mailbox-id>
rp project corpus get <templates-export-id>
rp project corpus download <templates-export-id> --out "$SCRATCH/templates/export.json"
```

Authored, high-signal voice and stock-phrasing reference (`templates/v1` JSON). It is **not interaction
evidence**: it never satisfies thread coverage, occurrence, era, skip, or durable-rule gates. Non-Gmail
harvests skip this; `prepare` still emits an empty normalized `templates.json` so later stages have one
stable path.

## Intercom

Read-only export; it neither enables inbox processing nor writes to Intercom. The corpus is already
structurally typed (`inbound/contact` / `outbound/human_admin`) and `prepare` **rejects** unstructured or
misordered v3 rather than guessing — bots, automation, system events and unverified admins are dropped
server-side and counted in the diagnostics. Read `diagnostics.md` (step 2) before trusting the mix. Never
mix Help Center/KB content into a conversation harvest.

## IMAP

Hosted IMAP harvest is a **shallow smoke path only** (the server caps rendered refs, currently 100).
Before any deep/local run, prove both public surfaces exist:

```bash
rp project mailbox imap-env --help
test -f "$SKILL/scripts/local_imap_harvest.py"
```

If either is missing, stop with an implementation/ops gap via
[`brain-publish`](../brain-publish/SKILL.md) — do **not** reveal credentials, scrape private stores, or
invent env-file handling. When both exist:

```bash
rp project mailbox imap-env <mailbox-id> --out "$SCRATCH/imap.env"
git check-ignore "$SCRATCH/imap.env"
uv run "$SKILL/scripts/local_imap_harvest.py" --env "$SCRATCH/imap.env" --out "$SCRATCH/imap-export/"
```

The env file is secret material: never print or commit it; step 12 cleanup removes it with the rest of
scratch.

The exporter feeds `prepare` directly — it writes a `harvest_format: v1` blob at
`$SCRATCH/imap-export/corpus/corpus.md` (own subdir, so `prepare` never trips over the legacy
non-front-mattered `INDEX.md` still written at top level for one deprecation release):

```bash
uv run --no-project python "$SKILL/scripts/prepare_harvest.py" prepare \
  --corpus "$SCRATCH/imap-export/corpus/" --scratch "$SCRATCH" --export-id "$EXPORT_ID"
```

**Sent-folder only**, so every message is mailbox-authored: expect `direction: mailbox_first` and no
external-question holdouts — pass `--holdout 0`. This corpus proves only what the mailbox *sent*; paired
inbound-thread expansion is future work.
