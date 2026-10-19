---
id: akkoord-op-voorstel
subject: "Re: Uw afspraak verplaatsen"
status: proposed
expect:
  process: true
  needs_action: true
  needs_patient_text: false
why: "De patiënt aanvaardt het nieuwe moment dat de praktijk voorstelde; de verplaatsing moet nog geboekt worden."
tags: [acceptance, reschedule]
messages:
  - id: m1
    direction: inbound
    from: "Jan Peeters <jan.peeters@example.com>"
    date: 2026-10-06T08:10:00+02:00
  - id: m2
    direction: outbound
    from: "Tandartspraktijk Voorbeeld <info@praktijk.example>"
    date: 2026-10-06T09:00:00+02:00
  - id: m3
    direction: inbound
    from: "Jan Peeters <jan.peeters@example.com>"
    date: 2026-10-06T11:12:00+02:00
    headers: {x-mailer: "iPhone Mail"}
---

## m1

Kan mijn afspraak van donderdag verplaatst worden?

## m2

Wij kunnen u dinsdag 14/10 om 10u voorstellen. Mogen wij dit zo inplannen?

## m3

Bedankt, akkoord!
