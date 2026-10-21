---
id: live-chat-opener
subject: "Chat"
status: proposed
channel: conversation
expect: {process: false, needs_action: false, needs_patient_text: false}
why: "A chat greeting alone; the real question follows as its own message"
messages:
  - {id: m1, direction: inbound, from: "a@example.com", date: 2026-10-06T08:10:00+02:00}
---

## m1

Hello team
