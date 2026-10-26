---
id: archive-receipt
subject: "Ticket Received - 1234"
status: proposed
expect: {process: false, needs_action: false, needs_patient_text: false, archive: true}
why: "A helpdesk receipt for our own mailing."
messages:
  - {id: m1, direction: inbound, from: "desk@example.com", date: 2026-10-06T08:10:00Z, headers: {auto-submitted: auto-generated}}
---

## m1

Your ticket 1234 was received.
