---
name: brain-ask
description: Ask a real production ReplyPen brain with `rp ask` and verify the resulting run. Use inside a brain checkout when asked whether a brain change works on prod infra, to simulate a customer support email, for a direct raw investigation, or to test a pushed `dev/*` brain ref without moving `main`. Captures the answer, run accounting, trace URL, and brain journal diff.
---

# brain-ask - ask a real prod brain

`rp ask` is the **full LLM loop** on production infrastructure — use it for last-mile validation,
usually of a pushed `dev/*` ref. It is not the debugging fast path: for a known primitive, tool parity,
a grounding script, a schema, logs, a tenant projection, local tests, mirror checks or an action dry-run,
reach for [`prod-console`](../prod-console/SKILL.md) or
[`local-brain-work`](../local-brain-work/SKILL.md) first.

Run it from the brain checkout: `rp` targets the project from the brain metadata and the logged-in OAuth
token. On tenant-enabled projects do **not** pass `--tenant` by default — `rp` uses the tenant of the
active login (`rp auth status` if unclear); only a project-pinned login must pass `--tenant <slug>`. If a
ReplyPen MCP is installed, ignore it unless the user explicitly asks for MCP.

Read [docs/side-effects.md](../../docs/side-effects.md) (an ask creates a real run) and
[docs/brain-model.md](../../docs/brain-model.md). Flags: [docs/rp-cli.md § `rp ask`](../../docs/rp-cli.md)
and `rp ask --help`.

## Workflow

1. **Require a question**; if absent, ask and stop. Default scenario is the customer-style email
   simulation; `--scenario raw` for a direct investigation, only when the full loop is what needs
   validating.

2. **Trigger and wait.** The choices that matter:
   - `--brain-ref dev/<branch>` — an already-pushed dev branch, after Local Brain Work covered the checks
     that fit the change. `main` stays live and the run is flagged `test`: no ReplyPen callback, no
     durable journal push, proposed actions/PRs are test artifacts.
   - `--file <path>` (repeatable) stages local files read-only in the run workspace. Content is sniffed
     server-side; text, images, pdf, csv, json and xlsx pass, other binaries/archives are rejected.
     Files the run generates (written to `/tmp/outbox`) come back as metadata + a download URL in the
     result's `attachments` (never inline content); `--file` inputs are not echoed there.
   - `--effort pro|max` only when deliberately escalating.

3. **Relay the result:** draft/note/actions for the email scenario, or the direct answer for
   `--scenario raw`; plus caveats, run accounting (status, turns, outcome) and the trace URL. Capture the
   printed `run_id`. On `status: error`, surface the error and stop.

   When the brain rule changes the recipient or sets a subject — for example a web-form relay,
   voicemail/PBX notification, or partner forward — also verify the result's `outbound_email.to`,
   `.cc`, `.bcc`, and `.subject` against that rule. Do not apply this check or comment on the normal
   absence of `outbound_email` for an ordinary same-thread reply.

   If the draft or note claims a state change (booked, moved, cancelled, refunded, sent, updated), or
   carries an action/preflight caveat, check `rp run events <run_id>` before reporting: preflight failed
   ⇒ no proposal and no mutation; `proposed` ⇒ pending human confirm; succeeded/failed ⇒ post-loop
   execution really happened. Decision table:
   [`local-brain-work/action-run-triage.md`](../local-brain-work/action-run-triage.md).

4. **Show what the run wrote to the brain:** `rp run brain-diff <run_id>` — journal commit SHA, message,
   changed files, diff summary. If nothing changed, say the run answered without persisting knowledge.

Full reasoning/tool trail: [`rp-debug`](../rp-debug/SKILL.md) with the captured `run_id`. If this
validated a local brain change, finish through [`brain-publish`](../brain-publish/SKILL.md).

## Simulating a customer's follow-up reply

**Mail already in? Replay it:** `rp ask --replay-run <run id> --brain-ref dev/<branch>` reruns it as a
simulation (real thread, mailbox contract, PII seed; live data) and diffs it with the original. Hand-quote
only a reply that never arrived.

**`rp ask --session <id>` does not replay the earlier email or draft.** A reused session only warm-starts
the new run with a digest of the prior turns' *command labels* ("ran: search_patient …; outcome:
Draft+Note") — never the sender's words, the offered options, or the previous draft. Prompt-mode runs
also never open the thread-coherence / sender-history prompt sections; those need a real ingested mailbox
thread. Consequences seen in prod: a bare follow-up ("Doe maar 21 september om 15u05") arrives with *no*
thread context, and the grounding step may fill the vacuum with a journal file from an unrelated
earlier run — same-evening test runs about the same patient are the classic trap — silently steering
intent.

So embed the whole prior conversation in the question, quoted oldest-first exactly as a mail client
would, with the new reply on top:

```bash
rp ask "$(cat <<'MAIL'
Doe maar 21 september 2026 om 15u05. Mvg, Thomas

> Op ma 31 aug schreef Team De Kies:
> Beste Thomas, voor een vulling bij Mia kan je kiezen uit: 1) ma 21 sep 15u05 …
>> Op ma 31 aug schreef Thomas Bollen:
>> Ik zou graag een vulling laten doen bij Mia …
MAIL
)" --subject "Re: Afspraak vulling"
```

Use `--session` only for *investigation* continuity (which scripts already ran), never for thread
history. If a run still picks up a stale journal note, report it as a grounding step artefact, not a
brain bug.

Same follow-up again after the next brain edit, or the real record no longer has the state to test
(already cancelled, thread scrubbed)? Make it a committed
[fixture case](../brain-simulate/SKILL.md#fixture-cases) on a seeded demo dossier, with deterministic
checks on the draft and the proposed actions.
