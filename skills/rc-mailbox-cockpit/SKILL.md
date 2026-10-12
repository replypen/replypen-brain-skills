---
name: rc-mailbox-cockpit
description: "Review a ReplyPen Google mailbox from a local developer machine: independently fact-check complex unsent drafts, surface neglected inbox work, and create a concise Gmail-linked daily cockpit. Optional guarded corrections change draft text only; this is not a production brain skill."
---

Build one small, actionable mailbox page. Gmail is the inbox source for this MVP; public `rc` supplies per-thread run evidence. A successful ReplyPen run is evidence, never proof its answer is correct.

Use the **skill-creator** workflow when modifying this skill. Runtime use needs authenticated `rc`, `uv`, and a configured [google-workspace-core](https://github.com/pjmuller/skills/tree/main/skills/google-workspace-core) adapter with its account wrapper. Read that wrapper's policy. Keep account, credential paths, mailbox UUID and schedules machine-local, outside this kit.

1. Run `scripts/cockpit.py collect`; read **only `index.json`** first. [Commands](references/commands.md) describe exact inputs, bounds, and artifacts. The collector verifies mailbox UUID/provider/email against GWS identity; Intercom runs sharing an email address do not qualify.
2. Leave genuinely answered, no-inbound and confident simple cases alone. Use the summaries to choose uncertain or consequential cases for review, including no-run/out-of-scope mail and stale unanswered drafts. Health flags, notes and missing grounding help prioritize; keywords or a green run cannot decide answer quality. Respect the displayed uncovered counts.
3. For selected threads run `detail`, then dispatch a dedicated **Opus 5.5, high effort, in-harness subagent** per complex case using [the reviewer recipe](references/reviewer.md). Claude coordinates these. A Codex coordinator must hand a bounded set to a Claude T3 worker that fans out Opus checks, using the environment's thread-management skill; never silently substitute another model. If Opus is unavailable, report that limit and leave corrections pending.
4. Merge only evidence-supported reviewer findings. Confirmed/simple/answered cases remain untouched; a correct unsent draft may still need an owner action. Validate findings, optionally apply demonstrably necessary corrections **only when the user authorized draft editing and `--allow-draft-corrections` is supplied**, then render the page to the user's artifact directory (normally `~/Downloads`). Expired evidence may still render with one refresh-before-acting notice; draft corrections always require fresh validation. Show action, reason and Gmail link; put evidence behind expansion. No raw mail bodies or tokenized run URLs in the report.

Reviewers perform read-only grounding. No `rc ask`, retry, process-thread, sending, deletion, archive, labels, or customer actions. Draft correction requires exact current AI body match, a unique draft, unchanged thread/run/message, and independent evidence. Preserve recipients, attachments and quoted text; the helper saves the original and reads back. Gmail cannot atomically lock thread and draft together: a residual race exists, never describe it as race-free.

Schedule only when requested, using machine-local definitions. Never pause a shared versioned schedule to represent one colleague's holiday. Handoffs describe setup and verification on that colleague's computer, never borrow their credentials or execute there.
