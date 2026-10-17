---
name: rp-health
description: "Check whether a ReplyPen project is quietly unhealthy — `rp fleet health` reports stale/failing source mirrors, failed brain boot checks, and dead-lettered runs. Use for periodic sweeps, a CI/cron gate, or an open 'is anything broken?' question; hand flagged run UUIDs to rp-debug."
---

# rp-health — project health sweep

Read-only. Public `rp` only, scoped by the OAuth login and brain metadata
([docs/support-boundary.md](../../docs/support-boundary.md)). Ignore an installed ReplyPen MCP
unless the user asks for it.

```bash
rp fleet health              # default 24h dead-letter window; pass through an explicit --hours <n>
```

`--all` fans out across every project (all-projects token) and is non-zero if *any* project is
unhealthy. **Non-zero exit is the verdict, not a tool failure** — that is what makes this
cron/CI-usable. Read the output and report what is unhealthy.

Relay plainly:

- **Mirrors** — name each source mirror that failed sync or went stale, and for how long
  ([docs/mirrors.md](../../docs/mirrors.md)).
- **Brain boot** — a failed boot check means the mounted brain does not start.
- **Dead-lettered runs** — surface the UUIDs; these are urgent, a draft never reached the customer.

Hand flagged UUIDs to [`rp-debug`](../rp-debug/SKILL.md). If a finding leads to a brain edit, finish
through `brain-publish`.
