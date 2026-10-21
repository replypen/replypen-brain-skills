---
id: bad-channel
subject: "Chat"
status: proposed
channel: Live Chat
expect: {process: false, needs_action: false, needs_patient_text: false}
why: "channel must be a lowercase source channel"
messages:
  - {id: m1, direction: inbound, from: "a@example.com", date: 2026-10-06T08:10:00+02:00}
---

## m1

Hello team
