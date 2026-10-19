---
id: yaml-coercions
subject: 2026
status: approved
expect: {process: yes, needs_action: on, needs_patient_text: NO}
why: "yaml.v3 bool words and numeric scalars into string fields"
tags: [yes, 3]
messages:
  - {id: m1, direction: inbound, from: "a@example.com", date: 2026-10-06T08:10:00.5+02:00, html: False, headers: {x-priority: 1}}
---

## m1

hi
