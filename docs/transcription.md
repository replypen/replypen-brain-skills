# On-demand transcription (`lib.transcribe`)

Chat recordings (long audio, screen captures) are NOT transcribed when a run starts. The run prompt
names each untranscribed recording with a one-line note and its path; the agent transcribes when the
answer depends on it:

```bash
python -m lib.transcribe /tmp/attachments/2-schermopname.webm \
  --instructions-file /tmp/timeline.txt --keywords "Kampweek,Inschrijving"
```

- **Input.** The recording path the prompt names. Beside it the host stages
  `<path>.attachment.json` (`attachment_id`, `media_mode`, `recorded_at`, `media_staged`); a large
  recording's media itself is absent — the sidecar is enough, the host reads the bytes.
- **Output.** `<path>.transcript.md` (overwritten) + stdout: the transcript, or its head and the path
  when long. Screen recordings: one `[mm:ss] Speaker N:` / `[mm:ss] SCREEN:` timeline with a UTC start
  header — line it up against the project's own audit trail.
- **Hints.** The project/tenant recognition hints and language always apply. `--instructions` /
  `--keywords` are added on top: pass what only this run knows (a timeline of what the user did in the
  recording window, names, the question). The brain's own recipe decides what to pass.
- **Cost + cache.** Same arguments again ⇒ served from cache. Different instructions ⇒ a new
  transcription that replaces the stored one (later turns see the latest). One transcription per
  conversation at a time.
- **Long recordings.** One call waits up to ~9 min. Exit code 3 = still transcribing: run the SAME
  command again — it joins the running job, nothing is billed twice.
- **Exact text at a moment (screen only).** `--at 03:12 [--at 04:01] --instructions "the order id"`
  reads full-resolution stills of those moments instead of transcribing: up to 6, seconds, never cached,
  the transcript file untouched. Use it when an id/amount matters or the timeline says UNREADABLE — the
  video transcript reads frames at a fixed token budget, a still reads far smaller text.
- **Errors.** Exit 2 with the host's sentence: no sidecar (wrong path), attachment not in this
  conversation, not a recording, transcription unavailable in this run (only chat runs mount it).

Function form: `transcribe.transcribe(path, instructions=..., keywords=[...]) -> Result`, and
`transcribe.read_frames(path, ["03:12"], instructions=...) -> str`
(`transcript`, `mode`, `duration_seconds`, `cached`, `path`); raises `TranscribeError` /
`TranscribePending` / `TranscribeUnavailable`.
