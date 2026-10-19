---
id: html-voicemail
subject: "New voicemail from +32470000000"
status: approved
expect: {process: true, needs_action: true, needs_patient_text: true}
why: "Voicemail with a patient question."
messages:
  - id: vm
    direction: inbound
    from: "PBX <voicemail@pbx.example>"
    date: 2026-10-06T08:10:00Z
    html: true
    attachments: [media]
---

## vm

<p>You have a new voice mail.</p>
<p>--- Voicemail transcript ---</p>
<p>## not-a-section heading stays body</p>
