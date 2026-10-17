---
name: rp-debug
description: "Debug ONE ReplyPen production run, thread, or session from a brain checkout with `rp run debug` / `rp run thread`. Use when given a run UUID, thread/session id, a failed run, a UUID flagged by rp-fleet/rp-health, or a delivery question. Analysis-first: inspect evidence, propose the smallest fix, stop before editing brain files unless asked to implement."
---

# rp-debug — inspect one run/thread

Public `rp` only; scope comes from `.replypen.toml` + the OAuth login
([docs/support-boundary.md](../../docs/support-boundary.md)).

## Required Context

- [docs/run-trace-model.md](../../docs/run-trace-model.md) — trace concepts, reading order, and the
  debug discipline (a single failed run is evidence, not permission to oversteer the brain).
- [docs/brain-model.md](../../docs/brain-model.md).
- [docs/actions.md](../../docs/actions.md) when the trace carries action/preflight artifacts.

## Resolve the input first

- **Run UUID** → `rp run thread <uuid>` for thread/session lineage, then `rp run debug <uuid>`.
- **`rp fleet actions` row** → use its `run_id` (not the action-run `id`); `run_url` opens the same
  originating run. A null `run_id` = a direct operator action with no run trace. Read `error_class`
  first: an infra class means ReplyPen's machinery failed — report it, do not edit the brain
  ([docs/actions.md](../../docs/actions.md#failure-classes-infra-vs-domain)).
- **Thread/session id** → `rp run thread <id>`. No run at all → explain the channel/support boundary.
- **Chat share link** (`…/s/<token>`, no login needed) or a bad chat answer with "this is what I
  expected" → [brain-chat-debug](../brain-chat-debug/SKILL.md) (case file, replay on a dev ref).
- **Run link** (`…/runs/<id>?t=<token>`) → `rp run debug '<link>'` works without login; for a chat
  run, `brain-chat-debug open '<link>'` pulls the whole conversation.
- **A forwarded email** → `rp run list --from <address> --since <YYYY-MM-DD>` (`--subject` narrows;
  rows show FROM/SUBJECT). No run → `rp run thread <address>`: its threads, including mail triage
  skipped or security-blocked before any run, with the triage reason.
- **A question, not an id** → `brain-ask`.
- **Nothing usable** → ask for a run UUID, thread/session id, or question, and stop.

## Files in a chat session

The files a chat user uploaded (any turn) stay in the session, but their bytes expire 30 days after
upload; text lives as long as the session. List and stream-download them with your own login (admin
tier on that project/tenant; never an end-user chat token):

- `rp run attachments <session_id|run_id>`: list (origin, size, available). `rp run thread <id> --transcript` shows them too.
- `--download` (user uploads; `--include-generated`, `--id <attachment_id>`, `--dir`) streams to
  `.replypen/attachments/<session_id>/`, sha256-verified, never overwriting. Images come as uploaded
  when the original was kept; older ones only as the processed 1536px JPEG.
- From a record an action created, e.g. a support ticket: `rp run attachments --resource support_ticket:<uuid>`.
  This host-proven link outlives the action ledger's 14-day params scrub; a ticket's own session
  hint is unverified, never use it as the source.
- `expired` = bytes past 30 days; the text and metadata remain.

## Drill

`rp run debug <uuid>` writes `.replypen/debug/<run8>-<project>.{md,jsonl}`. Read the markdown index,
then query the JSONL by `disp` for one step's `command` / `stdout` / `reasoning`. On 401/scope errors
check `rp auth status`; on missing public data, produce a support request.

Missing tables or an unknown database short name in the trace usually means the database was never
registered — check `rp dev console database list` (authoritative;
[docs/secrets.md](../../docs/secrets.md#register-a-new-grounding-database)) before touching brain
files.

## Run header (JSONL line 1)

Line 1 is the `type=="run"` header — the run's **inputs**, not its steps; fields are top-level (no
`.run` wrapper). Every other line is `type=="event"`.

**Rule out drift first.** A run answered against the settings, brain SHA, and mirror state of *its*
moment, not today's:

```bash
F=.replypen/debug/<file>.jsonl
jq 'select(.type=="run") | {tenant_settings, tenant_settings_current, tenant_settings_drift,
                            brain_resolved, grounding_sources}' "$F"
```

`system_prompt` is only plane 1 of the assembled context — the run also gets a bootstrap/brain-plane
user turn and the thread itself. Never read it as the whole prompt.

Inspect the run header's `outbound_email` only when the applicable brain rule changes recipients or
sets a subject, such as a web-form relay, voicemail/PBX notification, or partner forward; verify
`to`/`cc`/`bcc` and `subject` against that rule. Its absence on an ordinary same-thread reply is normal
and needs no comment.

**Why didn't the model know X?** The prompt-context capture (`context_schema_version`; `0` = a
pre-1.14 run or past the 14-day retention window) carries the section map. The `on:false` sections are
the signal — a gate that stayed shut is context the run never received:

```bash
jq -r 'select(.type=="run").prompt_sections[]? | select(.on|not) | .id + "  " + .gate' "$F"
jq -r 'select(.type=="run").prompt_sections[]? | select(.id=="<id>").text' "$F"  # off sections carry no .text
jq -r 'select(.type=="run") | .bootstrap_turn, .preselected_turn' "$F"
jq -r 'select(.type=="run").manifest_blocks[]? | "\(.presence)\t\(.chars)\t\(.path)\t\(.gloss)"' "$F"
jq -r 'select(.seq>=4000000).args | .before, .after, .rejected_diff' "$F"  # draft-cleanup passes (disp C1, C2, …)
```

## Stop with

Root cause or best hypothesis · trace/file evidence · the smallest proposed brain change, naming each
file it creates or edits as a clickable absolute-path link (new file: its absolute target directory) ·
a verification plan (usually `brain-ask --brain-ref dev/<branch>`) · the publish path
(`brain-publish`).

Edit files only when the user explicitly asks to implement. Then verify with `local-brain-work` /
`brain-ask` and finish through `brain-publish`.
