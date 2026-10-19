---
id: headers-list
subject: "x"
status: proposed
expect: {process: true, needs_action: false, needs_patient_text: true}
why: "w"

messages:
  - {id: m1, direction: inbound, from: "a@example.com", date: 2026-10-06T08:10:00Z, headers: [x-mailer]}
---

## m1

hi
