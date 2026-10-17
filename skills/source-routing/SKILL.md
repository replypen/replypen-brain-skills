---
name: source-routing
description: Build a question digest, trace-backed source usage report and human-prunable document routing table for a large mirrored source; ingest reviewed preferences into a brain. Use at onboarding, after stale-source complaints, or quarterly.
---

# Source routing

Local developer workflow, not a production grounding skill. Run from the target brain after `rp auth login`.
The collector uses public read-only `rp`; inventory/propose/ingest use local files and Python 3.11+ (also
workspace-compatible). Reuses the sibling helpcenter skill's email cleaner; no LLM calls, runtime changes,
private host scripts or provider-specific assumptions.

First inspect `rp project knowledge sync get`, `rp fleet health`, and `rp project knowledge content list`
with explicit `--project` and, where applicable, `--tenant`. `/kb` is independent of the brain; absence of
local `kb/` proves nothing about production. Export the appropriate scoped content with
`rp project knowledge content export --help`. The export writes `articles/<provider>/…` plus a manifest. Use `--root EXPORT/articles --mount /kb`;
for one subtree use `--root EXPORT/articles/PROVIDER --mount /kb/PROVIDER`. Check manifest `truncated`
before claiming coverage. Do not inventory `hits.md` or prepend `articles/` to runtime paths. Older trees may use `/brain/kb`.
If no mirror exists, say so immediately: inventory existing brain notes instead and propose a source
scope without claiming uninspected files exist. Future providers only need a local text-tree export.

All artifacts and question caches belong under an ignored directory. Do not commit customer questions.
`S` below is this skill's installed `scripts/` directory; no local-control commands belong in the brain.

```bash
uv run "$S/digest.py" --project PROJECT --days 60 --out .replypen/source-routing/digest.json --markdown .replypen/source-routing/digest.md
uv run "$S/trace.py" --digest .replypen/source-routing/digest.json --out .replypen/source-routing/trace.json --markdown .replypen/source-routing/trace.md
uv run "$S/propose.py" --digest .replypen/source-routing/digest.json --trace .replypen/source-routing/trace.json \
  --root LOCAL_EXPORT/articles --mount /kb --out .replypen/source-routing/proposal.json --markdown .replypen/source-routing/proposal.md
uv run "$S/ingest.py" --reviewed .replypen/source-routing/reviewed.md --brain . --out .replypen/source-routing/reviewed.json
# After the owner has pruned the table, repeat ingest with --apply to write locally.
```

Each command has `--help`. JSON intermediates preserve coverage, topic/run membership and evidence.
Digest defaults to email/chat/analysis runs, excluding simulations. The paginated fleet export is server tenant-scoped and locally
window-filtered; `--runs` accepts that export offline. Traces are fetched once and reduced to question,
subject and source paths; transcripts never go into model context. `--refresh` bypasses the reduced cache.
`--input digest.json --topics topics.json` re-clusters offline using optional `{topic: [keywords]}` rules
in the customer's language. Unmatched questions remain visible. Otherwise lexical overlap generates
provisional clusters; review labels before forwarding. Counts are inbound run-backed turns after retry
deduplication, not all mailbox mail or unique customers. Follow-ups and vendor mail may need manual
exclusion. Retention gaps, trace errors and mail skipped before run creation must stay visible; never
present a partial text window as a complete 60-day question census.

Evidence order: structured grounding selection → successful read command → journal reference →
command reference. A read command is attempted grounding access, not proof a document supported the
answer; failed reads and search references are weaker still. Generic directory searches and dynamically
computed paths can remain unattributed. Missing evidence means unknown. Proposal keyword/body matches
are labelled heuristics and never called observed usage. No answer-similarity fallback is silently applied.

Inventory reads up to 16 KB of YAML frontmatter; source dates/version are preserved (local mtime
is not freshness). Bodies are read only for missing titles/links or weak metadata matches, up to `--body-kb` (default 4, 0 disables).
`--include`/`--exclude` are repeatable local relative-path fnmatch globs, **not** provider sync globs.
Metadata `url`/`source_url` supplies the original link; brain notes without it show their first cited web
source instead. Files without a URL remain named with their runtime path. Year-stamped/archive paths
default to `outdated`, pivot/shortcut names to `noise`, others to `?`. Dates cannot establish authority:
a human may retain old timeless advice, but current price/policy evidence wins.

The owner edits only Verdict (`use` / `outdated` / `noise` / `?`) or deletes rows. Keep the five-column
header unchanged. Ingest replaces only the marked generated section in `notes/source-routing.md`, with
per-topic prefer/avoid lines; `?` adds no preference. It refuses malformed tables/markers or an empty
replacement. It never publishes. For durable human overrides, put table rows or free-form decisions
between `<!-- source-routing:human:start -->` and `<!-- source-routing:human:end -->` **outside** the
generated block. Human table rows override the same topic/path on later imports; other prose stays intact.

Link the fixed routing note from triage and source-status. Before review, commit only a stub that says
no document decisions have been approved. After review, use [brain-publish](../brain-publish/SKILL.md):
reconcile/push the brain's `main`, then publish and verify the exact SHA via public `rp dev brain publish`.
The proposal itself remains ignored until reviewed.
