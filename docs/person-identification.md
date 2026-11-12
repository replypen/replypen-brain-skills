# Person identification: "who is this message about?"

Heuristics for brains whose answers need an existing record (a patient, a parent, a registration). They
come from simplifying one brain's identity rules (about 28 case rules down to 5 principles, same fixture
and replay results). One project's experience: treat them as starting points, not law.

> [!IMPORTANT]
> The hard boundaries stay deterministic and outside the model's discretion: tenant-scoped retrieval,
> no action or record-specific statement before every required person is identified, and a typed
> identity decline (`decline_kind`) when someone stays unresolved. Everything else is judgement.

## Write principles, not case recipes

Each production miss tempts a new rule ("a child named by first name only → …"). After a dozen, the
rules contradict each other and nobody can tell which ones still matter. Five goals cover most of it:

1. **Only who is needed.** Identify the people whose existing record the answer needs. The writer,
   incidental names, a general question and a thank-you need nobody.
2. **New or existing comes from the content, per person**, never from an empty search. Zero hits prove
   nothing unless everything was searched and the message asks for new service.
3. **Search like a front-desk employee**: every clue (name as written plus a surname from the thread,
   sender address, phone, birth date, the event or date they mention), from several angles. An empty
   search is a reason to try another angle, not to stop.
4. **Pick when confident enough**, and say how sure in the reviewer note. No pick while a plausible rival,
   a contradicting fact or a cut-off search could flip it. The human reviewer is the backstop.
5. **Nothing on a guess**: no action, no record-specific claim, a typed decline instead.

Give calibration as examples ("a near-identical sibling name is a rival"), not as rules. Keep as rules
only the **defaults the model would not derive**, e.g. "sender address on exactly one record + another
signature → that record, greet the writer by their own name". Compressing those into an example lost
them.

## Scripts lay evidence; the model decides

- Retrieval scripts return rows with a per-row reason (`name exact`, `sender address`, `same household`)
  and say when a route was cut at its cap. That is evidence.
- A verdict in script output ("strong match", a suggested id) becomes a rule the model follows, and it
  grows case logic of its own. Leave it out.
- Keep **cross-row facts** a single row cannot show: the sender address sits on exactly one row with
  no name match, two first names one letter apart, two rows with the same name and birth date (and
  whether they share contact details), a search cut at its cap. Removing those cost real behaviour.

## Placement matters as much as wording

- **Output contracts at the point of use.** A reminder printed by the script right where the decision
  happens ("no exact name: add the check line to the note") was followed. The same rule only in a
  playbook was dropped about half the time.
- **Grounding may load one file.** If a mail only pulls in, say, the new-customer playbook, a rule that
  matters for that mail ("a sibling is no new-customer signal") must live there too.
- **Rules attached to identity travel with it.** When you fold files together, check that execution
  rules hanging off an identity case (a handed-over booking is transferred, not booked again) still
  live somewhere.

## Proving a simplification

- Build a fixture set first: positives *and* must-stop controls (near-named siblings, two plausible
  records, unknown sender). See [brain-simulate fixture cases](../skills/brain-simulate/SKILL.md#fixture-cases).
- Run the baseline and the candidate from the **same base commit**, at least twice each. Read failures
  per case, not totals: a run-to-run flip, a simulation gap (a live lookup a simulation never executes)
  and a real regression look identical in a score.
- Replay the real runs behind each past miss: `rp ask --replay-run <run> --brain-ref dev/<branch>`
  (pass `--tenant` for tenant runs). Replays read live data, so compare both arms on the same day.
- Prefer invariant tests (decline kinds, nothing before identification, one live lookup) and retrieval
  behaviour tests over pins of exact wording; phrase pins freeze yesterday's text.
- Get a review from a different model family on the diff: it catches a dropped rule faster than a
  fixture run does.
- Already principle-based? Audit before rewriting: read the traces of the recent drafts and sort
  them into solid / nobody needed / thin pick / asked who. A sudden drop to zero stops can hide
  over-answering; on KampAdmin the thin picks were all "which child or booking inside one family".
- With PII masking, names reach drafts and notes as stable value tokens (`⟦pii:firstname:<hash>⟧`):
  check a greeting against the token, and a tie-break against the booking id the note links, not
  against words (a good note names the excluded cousin's booking to say it was left out).
