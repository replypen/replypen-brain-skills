"""Shared run-dump renderer — the ONE index + JSONL formatter, brain-side.

A run-dump turns ONE agent run into two files: a concise markdown **index** (sized for an agent or a
hurried human deciding WHERE to look) and a jq-queryable **JSONL** event log (the drill-down target,
one untruncated JSON object per tool call). This module is the single source of truth for that
rendering, imported by public `rp`/Local Brain Work consumers so their output stays stable:

  * the project-dev path — `skills/local-brain-work/scripts/brain_dump.py` here, fed by the public-API
    trace bundle (`rp run trace <id> -o json`);

The producer normalizes its source to the **bundle dict** (`{"run": {...}, "events": [...]}`) and hands
it here. The server
ships raw truth; this module decorates it — the `disp`/`label`/`P1,P2…` computation, anomaly flags,
and "files read" extraction are presentation, computed here, never by the server.

It lives in `replypen-runtime` (not the prod run image, which never imports it) so both consumers
pull identical bytes via the existing git-tag pin — the same anti-drift mechanism `lib` uses. It is
**pure stdlib** and is NOT imported by any grounding/run path.

Bundle contract (what this renderer reads — see the server spec for the producing endpoint):

    bundle = {
      "run": {
        "run_id", "project", "status", "kind", "trigger", "brain_ref", "error",
        "brain_resolved", "tenant", "tenant_settings",
        "thread_id", "session_id", "topic", "question",
        "warm_start_digest", "grounding_seed", "system_prompt",   # untrimmed
        "created_at", "finished_at",                              # ISO str or datetime
        "model",
        "draft": "<full body or null>",
        "notes": [{"key", "body"}],
        "proposed_actions": [{"slug", "status", "params", ...}],
        "pii_masks": [{"label","source","count", ...}],              # optional, if API exposes it
        "metadata": {...} | null,                                 # run_url/trace_url/…
        "egress": [{"host","port","scheme","url","bytes_out","decision","at"}],  # per-ROW (see note)
      },
      "events": [{"seq","tool","args","command","stdout","stderr","exit_code","status",
                  "duration_ms","at","reasoning","model"}],
    }

> Note on egress: this renderer needs the per-ROW egress shape (it does the by-host aggregation and
> reads `decision`/`at` for the blocked-egress flags), which REFINES the illustrative aggregated
> `[{host,count,blocked}]` sketch in the server spec. Whichever fetch backend builds the bundle must
> supply per-row egress under `run.egress`.
"""

from ._render import decorate, emit_jsonl, files_read, flags, render_index

__all__ = ["render_index", "emit_jsonl", "decorate", "flags", "files_read"]
