# Run Trace Model

`rp run debug <id>` writes a markdown index plus JSONL event log for one production run. Read the
markdown index first; use JSONL only for targeted drill-down.
Use `rp run thread <run-id>` when you need that run's provider thread, session, and neighboring runs.

| Trace concept | What it means | Where to look |
|---|---|---|
| Run lifecycle | Trigger, run row, assembly, loop, post-loop work, callback/result. | Header, status, outcome, flags. |
| Mode vs scenario | Email simulation/draft flow vs raw/direct answer. | Header `kind`, question, outcome. |
| Workspace | One per-run container with `/brain`, `/tenant`, `/mirrors`, `/kb` read-only and `/tmp` writable. | Timeline bash commands and file paths. |
| Warm start | Prior same-session trail injected into main loop and grounding. | "Warm start" and prior-context sections. |
| Grounding pre-step | Cheap fail-open file-selection loop before main prompt. | "Grounding pre-step" and `P*` JSONL events. |
| System prompt | Per-mode preamble, capabilities, policies, action catalog, stable run clock. | "System prompt"; full text in JSONL header. |
| Tenant projection | Deterministic compiled `/brain` view from shared brain plus tenant profile values. | "Projection inputs" and `brain_resolved`. |
| Data scoping | Per-run DB credentials/views limit what grounding scripts can see. | Commands using `lib.db`; DB errors. |
| PII masking | Declared DB columns may be tokenized before the model and detokenized later for delivery. | "Data shielded from the model" when the public bundle exposes it; otherwise escalate if needed. |
| Mirrors/KB | External source/KB snapshots mounted read-only and refreshed independently. | Files read under `/mirrors`/`/kb`, egress, `rp fleet health`. |
| Egress | Outbound HTTP goes through production allowlist. | Egress section, blocked-egress flags, command output. |
| Actions/preflight | Run proposes actions only; schema/preflight can block proposals in-loop. | Outcome actions, action/preflight timeline labels. |
| Terminal outcome | Reply/raw answer, decline, error, journal/action/PR proposals; byte-free attachment delivery/drop results. | Outcome, `Attachments` header line, reply event `args.attachments`, flags, metadata. |
| Post-loop | Journal commit, action rows, source PRs, blocked-egress notes, callback delivery. | `rp run brain-diff <id>`, `rp run thread <id>`, `rp fleet health`. |

## Reading Order

1. Header: project, status, kind, test-run marker, tenant, `brain_ref`, `brain_resolved`.
2. Outcome: what draft/raw answer/action proposal the run produced.
3. Flags: failures, blocked egress, missing callback, aborted grounding; plus heaviness read off
   turn count and wall-clock duration.
4. Projection and warm-start sections: what context existed before the main loop.
5. Grounding pre-step: which files were selected or discarded before the main loop.
6. Timeline: non-search main-loop steps; drill JSONL by `disp` for full command/stdout/stderr.
7. Files read and egress: what the model actually inspected and which external calls were blocked.

## Debug Discipline

A single failed run is evidence, not permission to oversteer the brain. For `rp-debug`, inspect,
state the best hypothesis, cite trace/file evidence, propose the smallest useful brain change, and stop
before editing unless the user asked for implementation.
