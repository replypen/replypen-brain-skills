# Phone-lab evidence

```bash
rp run list --phone-lab --project <project> --limit 5 -o table
rp run debug <run_id> --phone-lab --project <project> --out-dir .replypen/debug
# A dashboard phone-lab URL supplies project + id too.
```

Uses the public API and a project developer login; tenant-pinned logins cannot read lab calls.
`--tenant <slug>` filters a project developer's listing. JSON listing keeps the raw `items` response;
it is the last N calls, not an exhaustive history export.

Read the Markdown index first: anomalies → transcript turns → full event timeline. The JSON is the
original API response. JSONL has one `{"type":"phone_run","run":{…}}` header (events excluded), then
`{"type":"event","index":0,"event":{…}}` per stored event. Index matches `.events[index]` in JSON;
unknown fields survive. Files are private (0600). Tool rows can be replaced in place: array order is
not chronological. `t` is elapsed milliseconds; tool `t` marks start, `exec_ms` duration.

The offline helper works with either format, from any brain:

```bash
Q=.agents/skills/rp-debug/scripts/phone_events.py
F=.replypen/debug/phone-<run_id>.jsonl
uv run --no-project python "$Q" "$F" --kind model --kind tool
uv run --no-project python "$Q" "$F" --after-ms 80000 --before-ms 130000
uv run --no-project python "$Q" "$F" --delegation <id>
uv run --no-project python "$Q" "$F" --kind tool --min-exec-ms 2001
uv run --no-project python "$Q" "$F" --index 24
```

Direct JSONL queries:

```bash
jq 'select(.type=="event" and .event.kind=="model") | {index,event}' "$F"
jq 'select(.type=="event" and .event.kind=="tool") | .event | {t,delegation,cmd,args,stdout,stderr,exit_code,exec_ms}' "$F"
jq 'select(.type=="event" and (.event.kind=="commentary" or .event.kind=="voicemail" or .event.kind=="note"))' "$F"
```

Model lifecycle: `seq` pairs started/terminal records; `outcome=round_1` is one-based per backend
invocation, `delegation` groups work, `name` is the configured model. Terminal `text` is a bounded,
safe answer or categorical failure; `result.tool_calls` carries bounded/redacted arguments.
These are projected debug records: routed providers, token counts and cost are absent. Old calls
have no model records; a newer CLI cannot reconstruct them. Missing/truncated evidence is not proof
that no call happened. SIP/Twilio messages, voicemail and commentary appear only where persisted.

Heuristics flag lookup filler without a created/in-flight delegation within 3 seconds, caller
“still there” checks, tool duration over 2 seconds, failed/stale/retired delivery and empty voicemail.
A lookup already queued/in flight covers repeated filler. Check IDs + caller corrections before
calling stale/retired answers bugs. Transcripts are text deltas; gaps and `latency_ms` cannot prove
measured audio silence. A successful voicemail POST is stronger evidence than captured text alone.
End/cap notices live in `note`; output caps and truncation still apply. The linked console run is
separate workspace context, not the voice transcript.

Fix ownership: brain persona/scripts for wording and lookup strategy; ReplyPen host for missing
model/delegation events, scheduling, stale delivery and capture; the project's app facade for tool
failures. Preserve the historical brain ref when comparing with today's files.
