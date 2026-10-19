---
id: last-outbound
subject: "x"
status: proposed
expect: {process: false, needs_action: false, needs_patient_text: false}
why: "w"
messages:
  - {id: m1, direction: inbound, from: "a@example.com", date: 2026-10-06T08:10:00Z}
  - {id: m2, direction: outbound, from: "p@praktijk.example", date: 2026-10-06T09:10:00Z}
---

## m1

hi

## m2

reply
