---
id: bad-follow-up
subject: "RE: Uw afspraak"
status: proposed
expect: {process: false, needs_action: false, needs_patient_text: false}
why: "follow_up_from must name a later message."
follow_up_from: m1
messages:
  - {id: m1, direction: inbound, from: "An <an@example.com>", date: 2026-10-20T19:52:00+02:00}
  - {id: m2, direction: inbound, from: "An <an@example.com>", date: 2026-10-21T16:09:00+02:00}
---

## m1

Woensdag lukt niet.

## m2

Bedankt
