# Side Effects

Diagnosis is read-only by default. Test-run creation and action execution are explicit exceptions.

| Surface | Side effect |
|---|---|
| `brain_run.py`, `brain_test.py`, `rp run list/show/events/trace/debug/brain-diff/thread`, `rp fleet runs/patterns/health`, `rp status` | Read-only. |
| `rp project mailbox harvest <id>` | Creates a **production** export job: a heavy provider sweep of the mailbox's sent history into a stored cleaned Markdown corpus body. Bills provider/API usage. |
| `rp project corpus download <id>` | Marks the export **consumed** (starts server-side eviction grace) and lands the raw mail corpus on local disk. Read-only server-side, but raw customer mail now exists on the laptop. |
| `rp project mailbox imap-env <id> --out ...` | Writes mailbox IMAP/SMTP credential material to a local env file. Must be under a gitignored path, never printed, and deleted after the session. |
| `uv run scripts/local_imap_harvest.py ...` | Connects to the IMAP server from the laptop and writes a raw local corpus. Must be gitignored, treated as customer mail, and deleted after synthesis. |
| `rp ask` against `main` | Creates a real production run. It may create a draft, journal/test artifacts, and proposed actions. |
| `rp ask --brain-ref dev/<branch>` | Creates a test run against a pushed dev ref. It does not post a callback or durable journal push; proposed actions/PRs are test artifacts. |
| `rp ask --replay-run <run>` | Creates a **simulation** rerun of a stored email run or chat turn on live data: no action executes (auto actions become display-only test proposals), nothing is placed in a mailbox, no journal, no callback. The original run and thread are untouched. |
| `rp run feedback <id>` | Records score/comment feedback for consolidation. With `--processed`/`--unprocessed`/`--resolution-note` it instead mutates the existing feedback row's operator triage state (project-admin tier). |
| `rp run retry <id>` | Creates a replacement production run, optionally at a different model tier. |
| `rp dev brain promote --channel <channel> --sha <sha>` | Moves a shared project brain channel for all its runs/tenants. Project-maintainer only; exact-SHA, serialized, idempotent, and audited. |
| `rp dev brain publish --channel <channel> --sha <sha>` | The same channel move, preceded by a cache sync and followed by verification; non-zero exit on mismatch. Same authority and audit. |
| `rp dev brain preflight --sha <sha>` | Read-only dry run of a promotion: renders each tenant's projection at the candidate SHA. Moves nothing. |
| Action proposal | LLM proposes only. No customer mutation. |
| Action confirmation or `rp dev console action run` | Real mutation path. Human/product-gated, outside the LLM loop. |
| `brain_action.py --commit` | Local real write to whatever `./.env.action` points at. Use only against local/staging or intentionally safe targets. |

When reporting results, distinguish "the draft says it happened" from the action lifecycle:
preflight blocked, action proposed, action confirmed, or action execution succeeded/failed.
