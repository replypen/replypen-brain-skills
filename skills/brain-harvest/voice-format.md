# Voice format — draft font + signature block

Standalone probe, no scratch root or corpus needed. Persona settings carry *words*; the visual shell of
a draft is two channel knobs (`channel.draft_font_css`, `channel.signature_html`). Gmail exposes neither
the staff composer font nor the signature over its API, so both are recovered from the mailbox's own
sent HTML. Run at onboarding, and whenever drafts "look off" — default Arial, missing logo.

```bash
# 1. the row-export query. Raw message HTML is not on a public rp surface yet: ReplyPen runs it —
#    from a brain checkout, request the export through brain-publish.
uv run --no-project python "$SKILL/scripts/voice_format_probe.py" --print-sql \
  --mailbox info@example.com --limit 30
# 2. the proposal (JSON rows in, one JSON proposal out; HEADs each signature image)
uv run --no-project python "$SKILL/scripts/voice_format_probe.py" \
  --messages "$SCRATCH/voice/rows.json" --out "$SCRATCH/voice/proposal.json"
```

The query skips our own sent replies (`drafts.sent_message_id`): on a send-mode mailbox those are
outbound rows too, and learning from them feeds our own default straight back to us.

**Never apply silently.** Read `signature_text` (plain-text rendering) with the operator and check
`checks`: image HEADs `200`, `signature_share` ≥ 60%, byte count under the 16 KB server cap. A `warn` on
font share means the mailbox is inconsistent — prefer unset over a wrong font. Signature image URLs are
Google-hosted (`ci3.googleusercontent.com/mail-sig/…`), stable across sends and reused verbatim; never
rehost them.

Apply at the scope whose staff share the signature: **mailbox** when a project has several mailboxes with
different staff (the usual multi-tenant case), otherwise tenant or project. Unlike persona, this one knob
legitimately lives at mailbox scope.

```bash
rp project mailbox ls -o json      # mailbox settings are keyed by UUID, not address
rp --project <project> project mailbox settings set <mailbox-id> \
  channel.draft_font_css="font-family:verdana,sans-serif" \
  channel.signature_html="$(jq -r .signature_html "$SCRATCH/voice/proposal.json")"
```

Side effect: with `channel.signature_html` set the agent stops writing its own sign-off. Reduce
`persona.signature` to empty, or keep it only as tone guidance ("warm, first-name close") — a persona
signature left in place duplicates the harvested block.
