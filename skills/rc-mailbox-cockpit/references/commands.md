# Commands and local artifacts

All commands run with `uv run --no-project python <skill>/scripts/cockpit.py`. Arguments are paths/IDs, not shell snippets. Output contains artifact paths and coverage only. Failures redact upstream stdout/stderr.

```sh
cockpit.py collect --project <project> --mailbox-id <google-uuid> \
  --gws-script <google-workspace-core>/scripts/gws.py \
  --gws-config <account-wrapper>/workspace.json \
  --out <new-private-directory> --days 7 --max-threads 60
cockpit.py detail --bundle <directory> --thread-id <gmail-hex-id>
cockpit.py validate --bundle <directory> --findings <findings.json>
cockpit.py apply --bundle <directory> --findings <findings.json> \
  --thread-id <gmail-hex-id> --allow-draft-corrections
cockpit.py render --bundle <directory> --findings <findings.json> --out <report.html>
```

`collect` paginates recent INBOX thread stubs and all draft stubs (default maximum 20 pages each), then interleaves two inbox threads with one older draft thread. Only `--max-threads` full threads are read. Standalone compose drafts are excluded. Coverage separates recent-inbox and older-draft discovered/checked counts, then reports discovered-but-unchecked count; when listing is capped, **additional unlisted threads are unknown**. Gmail estimates are not exact totals. Increase the bound deliberately if the uncovered backlog warrants it.

A non-DRAFT inbound followed by a non-self SENT reply is answered. SENT self-addressed mails are ReplyPen notes. A spoofed own From without SENT remains inbound and is flagged for review. Human clients' X-Mailer headers do not prove automation. Outbound messages with genuine automation/list headers or uncertain authorship remain uncertain, not automatically answered. The model still checks actual authorship when the distinction affects an action.

- `inventory.json`: complete discovered inbox/older-draft IDs and pagination flags; no message bodies.
- `index.json`: bounded snippets, subjects, state, health, IDs, Gmail links, coverage. Read first.
- `threads/<id>.json`: Gmail + provider-filtered rc lineage. Read only selected threads.
- `details/<id>.json`: refreshed thread, latest run body/notes, full MIME drafts, fingerprint. Read before review/correction; correction eligibility expires after four hours. Stale findings can still render with a single refresh-before-acting notice.
- `config.json`: machine-local adapter/project target. Do not publish.
- `corrections/<id>.json`: original draft, hash, evidence, write/readback state; `.txt` is the full replacement body.

These files contain private mail; output directories should be outside source control. Files are mode 600, newly created directories mode 700. Do not commit them, attach entire bundles to tickets, or repeat raw content in the final response. The public kit needs no private RootCause checkout or host access. `rc run debug` is available for a narrow trace drill, with files kept in the same private evidence area.

Immediately before a write, the audit records `write_attempted`. If the update/readback raises, that status means outcome unknown: read Gmail before any retry.

Missing original run bodies (including after raw-data retention expires) refuse correction; the current Gmail body cannot prove untouched AI origin, and no trace fallback is used.

`apply` does not run automatically during rendering. It revalidates findings and current provider/run, lists current drafts to prove uniqueness, fetches the draft and thread again, and uses GWS `gmail-draft-update --expect-message`. GWS preserves headers/thread/files and verifies them on readback. Plain text must match `draft_markdown` exactly: no whitespace/line-ending normalization that could conceal human editing. HTML-only, multiple plain parts, unsupported encodings, edited drafts and changed threads are refused. A write failure may have happened after Gmail accepted the write; inspect the audit and read Gmail before retrying. Never blind-retry mutations.
