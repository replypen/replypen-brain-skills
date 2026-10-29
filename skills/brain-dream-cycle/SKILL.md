---
name: brain-dream-cycle
description: "Operator-driven learning pass for a ReplyPen project from a brain checkout, public rp only: mine recent runs, human feedback, sent-vs-proposed deltas and shadow verdicts, then put each lesson in its one durable home (brain, persona, triage). Use when asked to learn from recent runs or feedback, review deltas, or judge shadow-mode readiness."
---

# brain-dream-cycle - learn from recent runs

Run from a project or tenant brain checkout: gather evidence, drill only where it can justify an edit,
put the lesson in its one durable home, verify, publish.

Two dream cycles exist and share evidence, not machinery. ReplyPen's own hosted consolidation cron
groups recurring human signal (sent-vs-drafted edits, feedback, clarify answers) and commits durably on its
own when enabled — you never drive it. This skill is the
**operator-driven** pass: a human decides between brain files, persona, triage policy and triage rules.

Public-CLI only: no ReplyPen source, host shell, SSM, raw registry SQL, or operator scripts. If the
read/write you need is not exposed through `rp`, finish with a support request through
[`brain-publish`](../brain-publish/SKILL.md).

## Required context

- [docs/brain-model.md](../../docs/brain-model.md) — what belongs in a brain.
- [docs/rp-cli.md](../../docs/rp-cli.md) — command surface, esp. § Dream Evidence and § Persona And
  Triage. Commands below are the shape of the pass, not a flag reference.
- [docs/side-effects.md](../../docs/side-effects.md) — before `rp ask` or an action.
- [docs/support-boundary.md](../../docs/support-boundary.md) — when the surface you need is missing.
- [`rp-debug`](../rp-debug/SKILL.md) drilling one run · [`brain-ask`](../brain-ask/SKILL.md) verifying ·
  [`brain-publish`](../brain-publish/SKILL.md) shipping.

## Workflow

1. **Scope first**, so a wrong project/tenant fails before you read evidence: `rp auth status`,
   `git status --short --branch`, `git pull --ff-only`. Preserve local work. In a tenant checkout keep
   tenant lessons tenant-side unless they clearly generalize. If `rp dev brain status -o json` shows
   `status.brain_source.read_only == true`, stop — learning for a read-only brain alias happens only from the source project;
   consumer runs are QA evidence, not learning input. Two skill paths are used throughout:

   ```bash
   DREAM_SKILL=<absolute path to skills/brain-dream-cycle>
   LOCAL_SKILL="$(cd "$DREAM_SKILL/../local-brain-work" && pwd)"
   ```

2. **Broad evidence before drilling.**

   ```bash
   rp dev learning evidence --limit 50 -o json          # --plane feedback|deltas|shadow|triage
   rp fleet runs --kind email --days 14 --learning
   rp fleet patterns --days 30
   ```

   Weight: explicit human feedback > sent-vs-proposed deltas > repeated run patterns > journal/debug
   traces. `rp dev learning evidence` ranks feedback by sharpest criticism and deltas by strongest human
   rewrite; shadow is a verdict-neutral recent sample instead. Stop here if the corpus is empty or thin
   — report "no durable lesson" with the commands run rather than inventing a rule.

3. **Read live deltas as edits, not JSON.**

   ```bash
   uv run --no-project python "$DREAM_SKILL/scripts/sent_delta_report.py" --limit 20 \
     --annotations .replypen/dream/notes.json --conclusion .replypen/dream/conclusion.md
   ```

   One evidence call, two files under gitignored `.replypen/dream/`: **read the `.md`** (agent-facing,
   `[-removed-]`/`{+added+}`); **hand the `.html` to the human**, don't read it yourself. `--from-json -`
   reuses evidence already in hand; `--annotations` (`{"<delta-id>": "why"}`) and `--conclusion` land in
   both files so evidence and reasoning ship together. Cite delta ids and run ids in the final report.
   Both files embed raw customer mail: never commit, never paste bodies into brain files, delete when
   done.

   Reading rules the report cannot enforce:
   - Group on the server's **delta category** (`factual | tone | policy | omission | addition | other`,
     assigned at capture from the bodies, stable across projects). The **signal index** (dropped date /
     link / placeholder / confirmation question, rewritten sign-off, length shift) is regex-level
     marker, never a judgement — confirm on the body before writing anything durable.
   - One delta is an anecdote; three sharing a category or marker is a pattern.
   - **Two similarity numbers, never interchangeable.** The `polish → replaced` verdict and ordering come
     from the script's word-level metric over display text; `server similarity` is the host's
     character-level distance over its own normalization. Never average or substitute.
   - **Bodies live 14 days** (host email TTL). Older rows still arrive flagged `bodies_scrubbed` and land
     in *Aged out (description only)* — corroboration for a pattern, never sole evidence for an edit.
     Run the cycle close to the sends if you want wording.

4. **Shadow rows are a different frame.** Shadow = the draft was suppressed and the human never saw it.
   Detect it only from the wire field `shadow: true` — never infer from low similarity, a large diff, or
   the verdict. Mixed payloads are normal; the report partitions them.

   ```bash
   rp dev learning evidence --plane shadow --limit 100 -o json     # unbiased sample FIRST
   rp dev learning evidence --plane shadow --verdict divergent_facts,missed_content \
     --include-bodies --limit 100 -o json                          # narrow only after reading it
   uv run --no-project python "$DREAM_SKILL/scripts/sent_delta_report.py" --shadow --limit 100
   ```

   - Read **verdict → recurring themes → one representative run**. `served_score` and the body comparison
     support the verdict; word overlap does not define it.
   - `equivalent` and `same_outcome_details_differ` are **positive** blind evidence: a longer answer,
     different greeting or different wording is not a correction. Do not mine positive prose into persona.
   - Readiness = `(equivalent + same_outcome_details_differ) / answerable shadow rows`. Show numerator and
     denominator, exclude `not_answerable` and unjudged rows, report the served-score distribution beside
     it. Graduation is a human decision, never a threshold.
   - **As-if-done convention:** human-gated `action` proposals and `👀` reviewer to-do lines are performed
     by the reviewer before sending — judge them as executed. A `proposed` action status in a shadow trace
     is expected (nothing executes), not a false claim. The real miss is asking the **customer** to confirm
     instead of proposing the action or leaving a `👀` task.
   - **Rubric before traces.** `rp run debug` lists the prompt sections the run was actually given; a
     worker who reads only traces calls convention-correct drafts "false claims". The human's send is
     strong evidence, not ground truth — the verdict is about the customer outcome. When splitting
     classification across workers, fix the taxonomy up front and canonicalize theme slugs on merge.
   - An unpaired shadow run usually means the human has not answered yet — check the thread before
     assigning quality meaning.

   A shadow lesson needs customer-impacting `divergent_facts` or `missed_content`, a representative
   drill, and recurrence or one high-impact/high-confidence failure. Route the evidenced cause, not the
   textual delta.

5. **Drill progressively**, only for evidence that can justify an edit. Read the `rp run debug` markdown
   index first; open JSONL only for exact commands, stdout/stderr, reasoning, reply payloads, journal
   lines. One high-signal run beats five dumps.

   | Need | First | Escalate only if needed |
   |---|---|---|
   | Bad score/comment, sent-edit context | `rp dev learning evidence -o json` | `rp run debug <id>` |
   | Fleet-level recurring failure | `rp fleet runs`, `rp fleet patterns` | `rp run debug <id>` on one representative |
   | Conversation wording / sender context | `rp run thread <id>` | `rp run trace <id> -o json` |
   | Did a previous brain edit help | `rp run brain-diff <id> -o json` | diff against current brain files |

6. **Pick the one durable home.** Fix at the highest level that generalizes; tenant is the easiest level
   to oversteer.

   | Evidence says | Home |
   |---|---|
   | Product-agnostic loop/system-prompt behavior every project should have | ReplyPen host → `brain-publish` support request |
   | Facts/helpers reused by sibling variant projects (`-support`, `-staff`) | shared grounding mirror, when the project has one |
   | Product fact, terminology, routing, source-of-truth pointer, playbook | brain files |
   | Missing reusable script, action instructions or selection rules | brain files or `actions/<id>/` |
   | Truly tenant-specific policy, fact or term | tenant brain/overlay |
   | Voice, language, signature, formality, "sound more like us" | persona settings (`rp project settings behavior`) |
   | Which inbound mail deserves a draft, broad draft/no-draft guidance | triage policy (`rp project triage policy`) |
   | Deterministic draft blacklist/whitelist by sender/subject/header | triage rule (`rp project triage rules`, `skip` / `force_process`) |
   | Spam blacklist/whitelist by sender | `rp project senders block` / `allow` |
   | The fact was simply unavailable from DB, KB, website or mirrors | grounding-data gap — not a brain edit |
   | Outcome needs a mutation or reviewer operation the workflow can't express | action wiring → `brain-publish` |
   | A phone call or private decision drove the answer | human-only knowledge — nothing to write |

   **Hard rule — a deterministic blacklist/whitelist lives only in settings.** Restating the same
   selector in `AGENTS.md`, `triage.md`, a brain skill, or a brain test creates a second, weaker rule
   that silently diverges from the UI. Also keep out: raw email quotes, one-off customer facts, copied
   private data, generic ReplyPen behavior.

7. **Change settings only after reading them** (`… get -o json`, `rules ls -o json`); command shapes in
   [docs/rp-cli.md § Persona And Triage](../../docs/rp-cli.md). Tenant and mailbox scopes have their own
   `settings get/set`. Keep persona and triage short — once guidance turns into product knowledge or a
   runbook it belongs in the brain. Delete any temporary verification rule (`rp project triage rules rm
   <id>`) before finishing.

8. **Apply brain changes narrowly.** Search first, edit the smallest existing home — prefer existing
   `AGENTS.md`, `terminology.md`, `skills/*/SKILL.md`, `notes/`, scripts or bounded `actions/<id>/` over
   new top-level structure.

   ```bash
   rg -n "<customer phrase>|<internal term>|<policy name>" AGENTS.md skills notes playbooks actions
   ```

9. **Verify with the cheapest check that proves the change**, then publish.

   ```bash
   uv run "$LOCAL_SKILL/scripts/brain_test.py"
   git push origin dev/<branch>
   rp ask "<case that previously failed>" --brain-ref dev/<branch>
   rp run debug <new-run-id>
   ```

   Settings-only changes: a fresh `rp ask` against the live scope, then inspect the run; for triage rules
   prefer a prompt or a disabled create/delete that proves the contract without touching real mail.
   Publishing: brain files → commit, push, [`brain-publish`](../brain-publish/SKILL.md); settings →
   record the exact `rp` commands and the verification run id; missing surface → `brain-publish` support
   request with evidence and the desired product outcome. Mixed: publish the brain first.

## Discipline

- `journal/` is legacy run residue (one run's self-report, being removed): never evidence, never edit it.
- Never promote a single anecdote unless it is high-impact explicit human feedback.
- Never hide write policy in persona: draft/no-draft is triage, confirmed mutations are actions.
- `rp dev console database` is for verifying a brain script or project fact, never for ReplyPen
  internals; private `db.py`, raw production SQL, host scripts and support credentials are out of scope.
