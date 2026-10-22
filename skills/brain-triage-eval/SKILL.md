---
name: brain-triage-eval
description: "Prove a triage change before it ships: keep a linted corpus of synthetic multi-message threads with expected process/skip in the brain (`_internal/triage/cases/*.md`), run it through ReplyPen's real triage decision at a dev/* brain ref with `rp project triage eval --brain-ref dev/BRANCH --baseline HEAD`, and read false skips first. Use when shortening or rewording triage.md / the triage policy, when a run drafted for noise or skipped a real message, or when writing/reviewing triage cases and their labels."
---

# brain-triage-eval — a labelled case corpus for the reply-or-skip gate

Triage is the cheap gate before the main agent: it decides whether a mail is worth a run. Its wording
lives in two places that compose into one prompt: the scoped **triage policy** (`rp project triage
policy get`, editable by the owner) and every brain doc tagged `include_in: [triage]` (by convention
`triage.md`). Edit either and you can lose a patient mail silently. This skill makes that measurable.

Keep **one** home per scope. A project with a brain puts its triage rules in `triage.md` and leaves
the project policy empty; the policy stays the home for brain-less projects and for tenant- or
mailbox-specific rules (the triage step reads only the project brain, never a tenant overlay).
`triage_source.py` (run by `brain_publish.py`) flags both homes at the same scope.

## The command

```bash
# from the brain checkout (project comes from .replypen.toml)
git push origin HEAD:dev/shorter-triage            # the candidate wording
rp project triage eval --brain-ref dev/shorter-triage --baseline HEAD --repeat 2
rp project triage eval --only akkoord-op-voorgesteld-moment,bedankt-na-sociale-afsluiting   # a subset
rp --tenant <slug> project triage eval              # tenant policy, rules and persona language
rp project triage eval -o json                      # raw attempts + provenance
```

What runs, server-side: the real prefilter (live hard rules), the live policy text, the triage docs
**of `--brain-ref`**, persona language, the email-type gate and the project's model. Cases come from
`--cases-ref` (default `HEAD`, the synced main); `--baseline` pins the candidate to the baseline's
exact cases commit, so a branch can never grade itself against its own edited answer key. Nothing is
ingested, drafted, sent or executed; each case costs one cheap model call per `--repeat`.

Read the output worst first: **false_skip** (a mail that needed work was dropped) > **flaky** (repeats
disagree) > **failed_open** (classifier error, not a judgment) > **extra_process** (a needless draft a
human discards). One green run is not proof: use `--repeat 2` or `3` for a wording decision.

Every report says what it does **not** cover. Most important: a follow-up on an already-handled thread
gets only the host's narrow **follow-up check** (skip a pure courtesy close, process everything else;
a delivered file always processes). Model those as `follow_up_from` cases. Whatever passes goes to the
main agent, which decides with the same `triage.md` (`include_in: [triage, agent]`); check that half
with [`brain-simulate`](../brain-simulate/SKILL.md) or a real `rp ask --simulation` thread.

## Case format

One file per case in `_internal/triage/cases/<id>.md` (run-hidden: `/_internal/` must be in
`.replypenignore`, or a run could read the answer key).

```markdown
---
id: akkoord-op-voorgesteld-moment          # = file name, [a-z0-9-]
subject: "Re: Herinnering afspraak"        # the thread subject
status: proposed                           # proposed | approved (owner sign-off; see Labels and review)
expect: {process: true, needs_action: true, needs_patient_text: true}
why: "The practice proposed a slot and asked to confirm; the acceptance still has to be booked."
tags: [acceptance, same-last-text]
# channel: whatsapp                        # optional: the thread's source channel (conversation, whatsapp, ticket, …); omit for email
# follow_up_from: m4                       # optional: first message of the current turn on an already-handled thread (follow-up check)
messages:                                  # oldest first; the LAST one is the current turn (inbound)
  - {id: m1, direction: outbound, from: "Praktijk <info@praktijk.example>", date: 2026-10-06T11:40:00+02:00}
  - id: m2
    direction: inbound
    from: "An Vermeulen <an@example.com>"
    date: 2026-10-06T12:05:00+02:00
    headers: {auto-submitted: auto-replied}  # optional, lowercase names, as the provider stores them
    attachments: [media]                     # optional: media | calendar | document
    html: true                               # optional: the body section is the HTML part
    spam: true                               # optional, inbound only
---

## m1

Mogen we de afspraak naar dinsdag 14/10 om 16:00 verplaatsen?

## m2

Bedankt, akkoord!
```

- `process` must equal `needs_action OR needs_patient_text`. Keep the two apart: an acceptance of a
  proposed slot needs an action even when no extra text to the patient is owed; a first "duidelijk,
  bedankt" may need only text.
- A chat case (`channel: conversation|whatsapp|sms|…`) gets the host's live-chat rules (openers with no
  question of their own are skipped; the next message is). Pair each opener with its real-question
  sibling, plus an email control with the same text.
- Write the bodies as the provider stores them (quoted history included when a mail client would
  quote it). The classifier sees subject, sender and body text of every message, not attachments.
- A follow-up case (`follow_up_from: <id>`) marks every message before that id "earlier turn" and the
  rest "new this turn", like production's cut line. Only a `closure` verdict skips; a `document`
  attachment on a new message processes without asking the model. Production often lacks the
  mailbox's own reply (sent from another system), so include follow-ups with no outbound message.
- **Synthetic only.** Invent names, addresses (`example.com`, `*.example`), phone numbers and ids;
  never paste a real mail. Lint checks structure, not de-identification: that stays your review.
- What makes a useful corpus: the same last text under different histories, courtesy ladders (first
  thanks / reciprocal closing / third social turn), a thanks that hides a new question or correction,
  confirmations/cancellations/choices after a reminder, delivered documents, relays that carry a real
  message (voicemail, web forms) and machine notifications that must stay skipped. Twenty sharp cases
  beat fifty near-duplicates; the server caps a corpus at 60.

## Lint

`brain_hygiene.py` (the pre-push gate) runs the `triage-cases` rule whenever a case changes; by hand:

```bash
uv run --no-project --with pyyaml python "$KIT/skills/local-brain-work/scripts/triage_cases.py"
```

It mirrors the host parser exactly (unknown fields, ids, RFC 3339 dates in order, last message
inbound, one `## <id>` section per message, `expect` consistency, caps). The host refuses the whole
corpus when one file is invalid.

## Labels and review

`status` records sign-off only; the eval runs every case either way. New cases start `proposed`; the
project owner reviews the expected outcome and the `why` (not the prose) and flips `approved`. When the
labels follow a brief or decision the owner already approved, approve them yourself in the same change
and say so in the commit; never invent the owner's policy. Disagreement on a label is a policy question:
write it down in the case's `why`, don't tune wording until it is settled.

## Related

[`brain-simulate`](../brain-simulate/SKILL.md) (whole-agent replay) ·
[`brain-dream-cycle`](../brain-dream-cycle/SKILL.md) (triage setting proposals from real feedback) ·
[`brain-publish`](../brain-publish/SKILL.md) (ship the winning wording).
