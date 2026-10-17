# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""Collect one focus period (+ 4 workdays of context) of fleet evidence for `brain-fleet-report`.

    uv run skills/brain-fleet-report/scripts/collect.py --date 2026-09-04

Writes `<brain>/.replypen/fleet-report/<report_id>/<date>/`:
  manifest.json  feeds + coverage + window        kpis.json  the A<->B counter contract
  evidence.json  everything, local only           digest.md  privacy-reduced, this is what the LLM reads
  runs.jsonl     one reduced record per run       commits.md correlate.py output
  raw/           rc responses, cache keyed by argv

Read-only: every rc call is a list/show/trace. Never `rp ask`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fr_common import (  # noqa: E402
    DEFAULT_INCLUDE_KINDS,
    DEFAULT_TZ,
    UTC,
    Coverage,
    Overlay,
    Rc,
    auth_status,
    bash_bucket,
    bash_cluster,
    clip,
    day_bounds,
    find_brain_root,
    guarded,
    hhmm,
    human_outcome,
    is_false_delta,
    learn_non_names,
    load_overlay,
    local_day,
    normalise_error,
    one_line,
    owner_languages,
    parallel,
    parse_action_lines,
    parse_ts,
    privacy,
    project_name,
    rc_version,
    recurrence,
    reduce_trace_header,
    stamp,
    stderr_last_line,
    tzinfo,
    unmounted_sources,
    usage_error,
    workdays_before,
    yesterday,
)

KPI_KEYS = (
    "runs", "counted", "excluded", "drafts", "sent_as_proposed", "edited", "shadow_compared",
    "not_sent_yet", "no_draft", "actions_ok", "actions_failed", "actions_proposed", "run_errors",
    "bash_real_errors", "usage_errors", "deltas_live", "deltas_shadow", "feedback",
)
OUTCOME_KEYS = ("sent_as_proposed", "edited", "shadow_compared", "not_sent_yet", "no_draft")
NOISE_BUCKETS = {"noise"}


# --------------------------------------------------------------------- selection


def run_kind(run: dict[str, Any]) -> str:
    return str(run.get("kind") or "")


def exclusion_reason(run: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Why this run is not customer work. `None` means it counts."""
    kind = run_kind(run)
    if kind not in cfg["include_kinds"]:
        return f"kind:{kind or 'unknown'}"
    if run.get("simulation"):
        return "simulation"
    tenant = str(run.get("tenant") or "")
    if tenant and tenant in cfg["dev_tenants"]:
        return f"dev_tenant:{tenant}"
    topic = str(run.get("topic") or "").lower()
    if any(marker in topic for marker in cfg["noise_topics"]):
        return "noise_topic"
    sender = str(run.get("sender") or run.get("from") or "").lower()
    if sender and any(marker in sender for marker in cfg["noise_senders"]):
        return "noise_sender"
    principal = str(((run.get("trace") or {}).get("guards") or {}).get("principal_scope") or "")
    if "mcp_operator" in principal:
        return "mcp_operator"
    return None


def is_flagged(run: dict[str, Any]) -> bool:
    """Worth a trace/show even when it is not counted: something actually went wrong."""
    health = run.get("health") or {}
    return bool(
        str(run.get("category") or "ok") not in {"ok", ""}
        or str(run.get("outcome") or "") in {"failed", "error", "stuck", "interrupted"}
        or (health.get("bash_err_real_count") or 0)
        or health.get("is_fallback")
        or health.get("grounding_discarded")
        or (health.get("blocked_egress") or 0)
    )


# ------------------------------------------------------------------- collection


def paged_runs(rc: Rc, base: Sequence[str], start: datetime, end: datetime, max_pages: int = 15
               ) -> tuple[list[dict[str, Any]], bool]:
    """`run list` is newest-first with a `next_before` cursor and no date filter."""
    collected: list[dict[str, Any]] = []
    before: str | None = None
    complete = False
    for _ in range(max_pages):
        args = [*base, "run", "list", "--limit", "100"]
        if before:
            args += ["--before", before]
        payload = rc.json(*args)
        if payload is None:
            # rp failed (network reset, auth): an empty list is NOT a quiet day.
            raise RuntimeError(f"run list failed for {' '.join(base)} (see collect errors)")
        runs = (payload or {}).get("runs") or []
        if not runs:
            complete = True
            break
        oldest = None
        for run in runs:
            created = parse_ts(run.get("created_at"))
            if created is None:
                continue
            oldest = created if oldest is None else min(oldest, created)
            if start <= created < end:
                collected.append(run)
        before = (payload or {}).get("next_before")
        if not before:
            complete = True
            break
        if oldest is not None and oldest < start:
            complete = True
            break
    return collected, complete


def collect_member(
    rc: Rc, member: str, window: tuple[datetime, datetime], cfg: dict[str, Any], overlay: Overlay
) -> dict[str, Any]:
    start, end = window
    base = ["--project", member, "--scope", "project"]
    data: dict[str, Any] = {"member": member}

    tenants = rc.json(*base, "project", "tenant", "ls") or []
    tenants = [t for t in tenants if isinstance(t, dict)]
    mailboxes = rc.json(*base, "project", "mailbox", "ls") or {}
    mailboxes = mailboxes.get("mailboxes", []) if isinstance(mailboxes, dict) else mailboxes
    data["tenants"] = tenants
    data["mailboxes"] = [m for m in (mailboxes or []) if isinstance(m, dict)]

    runs, complete = paged_runs(rc, base, start, end)
    rc.note_coverage(Coverage(f"{member}/runs", "complete" if complete else "partial",
                              len(runs), None if complete else "page cap reached"))
    # console runs are a separate plane: counts only, one page.
    payload = rc.json(*base, "run", "list", "--kind", "console", "--limit", "100")
    console = [r for r in ((payload or {}).get("runs") or [])
               if (parse_ts(r.get("created_at")) or end) >= start
               and (parse_ts(r.get("created_at")) or end) < end]
    data["console_runs"] = console

    by_id = {str(r.get("run_id")): r for r in runs}
    for run in runs:
        run["member"] = member

    # tenant attribution: no run listing carries a tenant, so fan out the tenant-scoped list.
    if tenants:
        def tenant_runs(tenant: dict[str, Any]) -> tuple[str, list[str]]:
            slug = str(tenant.get("slug") or tenant.get("id") or "")
            scoped, _ok = paged_runs(rc, ["--project", member, "--tenant", slug], start, end)
            return slug, [str(r.get("run_id")) for r in scoped]

        results = parallel(tenants, guarded(rc, "tenant_runs", lambda t: str(t.get("slug")), tenant_runs))
        attributed = 0
        for item in results:
            if not item:
                continue
            slug, ids = item
            for run_id in ids:
                if run_id in by_id:
                    by_id[run_id]["tenant"] = slug
                    attributed += 1
        missing = sum(1 for r in runs if not r.get("tenant"))
        rc.note_coverage(Coverage(f"{member}/tenant_attribution",
                                  "complete" if not missing else "partial", attributed,
                                  None if not missing else f"{missing} runs unattributed"))

    data["runs"] = runs

    # actions: JSON rows carry params/status, the agent text carries error_message.
    actions = rc.json(*base, "fleet", "actions", "--days", "14") or {}
    items = [a for a in (actions.get("items") or []) if isinstance(a, dict)]
    text_rows = {r.get("id"): r for r in parse_action_lines(rc.agent_text(*base, "fleet", "actions", "--days", "14"))}
    truncated = 0
    for item in items:
        extra = text_rows.get(str(item.get("id")))
        if not extra:
            continue
        message = extra.get("error_message")
        if message:
            item["error_message"] = message
            truncated += 1 if str(message).endswith("…") else 0
        if extra.get("error_class"):
            item["error_class"] = extra["error_class"]
    data["actions"] = items
    rc.note_coverage(Coverage(f"{member}/actions", "complete" if text_rows or not items else "partial",
                              len(items),
                              f"{truncated} error messages server-truncated" if truncated else None))

    # bash/http corpus: one call is the whole week, far cheaper than N traces.
    patterns = rc.json(*base, "fleet", "patterns", "--days", str(cfg["pattern_days"]), timeout=900) or {}
    data["events"] = [e for e in (patterns.get("events") or []) if isinstance(e, dict)]
    data["http"] = [h for h in (patterns.get("http") or []) if isinstance(h, dict)]
    data["egress"] = [g for g in (patterns.get("egress") or []) if isinstance(g, dict)]
    rc.note_coverage(Coverage(f"{member}/patterns", "complete" if patterns else "unavailable",
                              len(data["events"]), None if patterns else "no payload"))

    health_docs = rc.json_docs(*base, "fleet", "health")
    data["health"] = next((d for d in health_docs if isinstance(d, dict) and "mirrors" in d
                           or isinstance(d, dict) and "mailboxes" in d), {})
    rc.note_coverage(Coverage(f"{member}/health", "complete" if data["health"] else "unavailable", 1,
                              "24h window only" if data["health"] else "no payload"))
    data["deploy_state"] = rc.json(*base, "fleet", "deploy-state") or {}

    # Read-only brain alias: a member whose `status.brain_source.read_only` is true reads another
    # project's brain and is never a learning source (absent `brain_source` ⇒ owns its own brain).
    # Runs/actions/health above still feed the report as QA evidence; only the learning planes
    # below are member-scoped knowledge input.
    status_doc = rc.json(*base, "dev", "brain", "status") or {}
    status_body = status_doc.get("status") if isinstance(status_doc, dict) else None
    brain_source = status_body.get("brain_source") if isinstance(status_body, dict) else None
    is_consumer = bool(isinstance(brain_source, dict) and brain_source.get("read_only"))
    data["share_role"] = "consumer" if is_consumer else "owner"

    # learning planes: project-scope returns every plane regardless of --plane.
    days = max(cfg["lookback_days"], 3)
    learning = ({} if is_consumer else
                rc.json(*base, "dev", "learning", "evidence", "--days", str(days),
                        "--limit", "100", "--include-bodies") or {})
    for plane in ("deltas", "feedback", "triage"):
        rows = [r for r in (learning.get(plane) or []) if isinstance(r, dict)]
        data[plane] = rows
        reason = ("excluded: read-only brain alias, QA only — learning happens from the source project"
                   if is_consumer else
                   "server caps at 100 rows, no cursor" if len(rows) >= 100 else None)
        rc.note_coverage(Coverage(f"{member}/{plane}", "partial" if len(rows) >= 100 else "complete",
                                  len(rows), reason))
    # open feedback often predates the window; a 30-day pass keeps it visible.
    wide = ({} if is_consumer else
            rc.json(*base, "dev", "learning", "evidence", "--days", "30", "--limit", "100") or {})
    data["feedback_30d"] = [r for r in (wide.get("feedback") or []) if isinstance(r, dict)]
    return data


def enrich_runs(rc: Rc, member: str, runs: list[dict[str, Any]], focus: date, tz, cfg: dict[str, Any]) -> None:
    """`run show` for anything interesting, `run thread` for draft fate, traces for focus+flagged."""
    base = ["--project", member, "--scope", "project"]

    def want_show(run: dict[str, Any]) -> bool:
        return is_flagged(run) or local_day(run.get("created_at"), tz) in cfg["focus_days"]

    def want_thread(run: dict[str, Any]) -> bool:
        # Every focus run, draft or not: a lost/errored run is only judgeable against its thread
        # (the customer may have been answered five hours later by a re-processed run).
        return local_day(run.get("created_at"), tz) in cfg["focus_days"] or bool(
            (run.get("learning") or {}).get("sent_delta") or (run.get("learning") or {}).get("feedback")
        )

    shows = [r for r in runs if want_show(r)]
    threads = [r for r in runs if want_thread(r)]
    traced = [r for r in runs if is_flagged(r)]
    seen = {run_key(r) for r in traced}
    room = max(0, cfg["max_traces"] - len(traced))
    traced += [r for r in runs
               if local_day(r.get("created_at"), tz) in cfg["focus_days"] and run_key(r) not in seen][:room]

    def do_show(run: dict[str, Any]) -> None:
        payload = rc.json(*base, "run", "show", str(run.get("run_id"))) or {}
        run["error"] = payload.get("error") or (payload.get("health") or {}).get("error_head")
        run["notes"] = [
            {"key": n.get("key"), "body": clip(n.get("body"), 800)}
            for n in (payload.get("notes") or []) if isinstance(n, dict) and n.get("body")
        ]
        run["question"] = payload.get("question")
        run["draft_markdown"] = payload.get("draft_markdown")
        run["draft_markdown_len"] = len(str(payload.get("draft_markdown") or ""))
        run["draft_deferral"] = "[[✏️" in str(payload.get("draft_markdown") or "")
        run["proposed_actions"] = [a.get("slug") or a.get("action_id")
                                   for a in (payload.get("proposed_actions") or []) if isinstance(a, dict)]
        run["run_url"] = payload.get("run_url") or (payload.get("metadata") or {}).get("run_url")

    def do_thread(run: dict[str, Any]) -> None:
        payload = rc.json(*base, "run", "thread", str(run.get("run_id"))) or {}
        mine = parse_ts(run.get("created_at"))
        later: list[tuple[datetime, dict[str, Any]]] = []
        for entry in payload.get("runs") or []:
            if str(entry.get("run_id")) == str(run.get("run_id")):
                run["drafts"] = ((entry.get("attribution") or {}).get("drafts")) or []
                continue
            when = parse_ts(entry.get("created_at"))
            drafts = ((entry.get("attribution") or {}).get("drafts")) or []
            if when and mine and when > mine and (
                drafts or str(entry.get("outcome") or "") in RECOVERED_OUTCOMES
            ):
                later.append((when, entry))
        if later:
            # "the thread got an answer after this run failed" — the single fact that stops a
            # lost run from being reported as a customer who never heard back.
            when, entry = min(later, key=lambda item: item[0])
            run["recovered_at"] = when.isoformat()
            run["recovered_by"] = str(entry.get("run_id"))
        for thread in payload.get("threads") or []:
            if isinstance(thread, dict):
                run.setdefault("provider", thread.get("provider"))
                if thread.get("tenant") and not run.get("tenant"):
                    run["tenant"] = thread["tenant"]
                run.setdefault("thread_outcome", thread.get("outcome"))

    def do_trace(run: dict[str, Any]) -> None:
        records = rc.jsonl(*base, "run", "trace", str(run.get("run_id")), "--stream", timeout=600)
        if records:
            run["question"] = records[0].get("question") or run.get("question")
            run["draft_markdown"] = records[0].get("draft") or run.get("draft_markdown")
            run["trace"] = reduce_trace_header(records[0])
            if not run.get("tenant") and run["trace"].get("tenant"):
                run["tenant"] = run["trace"]["tenant"]

    parallel(shows, guarded(rc, "run_show", run_key, do_show))
    parallel(threads, guarded(rc, "run_thread", run_key, do_thread))
    parallel(traced[: cfg["max_traces"]], guarded(rc, "run_trace", run_key, do_trace))
    rc.note_coverage(Coverage(f"{member}/traces",
                              "complete" if len(traced) <= cfg["max_traces"] else "partial",
                              min(len(traced), cfg["max_traces"]),
                              None if len(traced) <= cfg["max_traces"] else f"capped at {cfg['max_traces']}"))


def run_key(run: dict[str, Any]) -> str:
    return str(run.get("run_id"))


# ------------------------------------------------------------------------- axes


def channel_of(run: dict[str, Any], mailboxes: Sequence[dict[str, Any]], overlay: Overlay) -> str:
    """Tenantless projects group by channel; the overlay owns anything smarter than this."""
    custom = overlay.call("channel_of", run)
    if custom:
        return str(custom)
    provider = run.get("provider") or (run.get("trace") or {}).get("scenario")
    live = [m for m in mailboxes if str(m.get("mode")) != "off"]
    if provider:
        match = [m for m in live if str(m.get("provider")) == str(provider)]
        if len(match) == 1:
            return str(match[0].get("slug") or provider)
        if match:
            return str(provider)
    thread_id = str(run.get("thread_id") or "")
    if thread_id.isdigit():
        intercom = [m for m in live if str(m.get("provider")) == "intercom"]
        if intercom:
            return str(intercom[0].get("slug"))
    if len(live) == 1:
        return str(live[0].get("slug") or "default")
    return "default"


# ------------------------------------------------------------------------- stats


def empty_kpis(day: date) -> dict[str, Any]:
    return {"date": day.isoformat(), **{key: 0 for key in KPI_KEYS}}


def count_day(
    day: date,
    runs: Sequence[dict[str, Any]],
    actions: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    deltas: Sequence[dict[str, Any]],
    feedback: Sequence[dict[str, Any]],
    tz,
) -> dict[str, Any]:
    block = empty_kpis(day)
    day_runs = [r for r in runs if local_day(r.get("created_at"), tz) == day]
    counted = [r for r in day_runs if not r.get("excluded_reason")]
    block["runs"] = len(day_runs)
    block["counted"] = len(counted)
    block["excluded"] = len(day_runs) - len(counted)
    block["drafts"] = sum(1 for r in counted if r.get("has_draft"))
    outcomes = Counter(str(r.get("human_outcome")) for r in counted)
    for key in OUTCOME_KEYS:
        block[key] = outcomes.get(key, 0)
    block["run_errors"] = sum(1 for r in day_runs if r.get("error") or is_flagged(r) and
                              str(r.get("category") or "ok") not in {"ok", ""})
    run_ids = {run_key(r) for r in day_runs}
    day_events = [e for e in events if str(e.get("run_id")) in run_ids]
    block["bash_real_errors"] = sum(1 for e in day_events
                                    if e.get("bucket") and e["bucket"] not in NOISE_BUCKETS | {"usage"})
    block["usage_errors"] = sum(1 for e in day_events if e.get("bucket") == "usage")
    for action in actions:
        when = local_day(action.get("executed_at") or action.get("proposed_at"), tz)
        if when != day:
            continue
        status = str(action.get("status"))
        if status == "succeeded":
            block["actions_ok"] += 1
        elif status == "failed":
            block["actions_failed"] += 1
        elif status == "proposed":
            block["actions_proposed"] += 1
    for delta in deltas:
        if local_day(delta.get("sent_at") or delta.get("received_at"), tz) != day or delta.get("false_delta"):
            continue
        block["deltas_shadow" if delta.get("shadow") else "deltas_live"] += 1
    block["feedback"] = sum(
        1 for f in feedback
        if local_day(f.get("comment_set_at") or f.get("score_set_at"), tz) == day
    )
    return block


ACTION_FUNNEL_RULE = {"reviewer_confirmed_after_s": 120, "stale_after_h": 36}
ACTION_FUNNEL_COUNTS = (
    "proposed_total", "succeeded", "failed", "superseded", "canceled", "executing",
    "pending", "stale", "human_confirmed", "auto",
)


def build_action_funnel(actions, runs, window, *, tz, now=None, axes=None):
    """Status snapshot, bucketed exactly like count_day; axes cover the focus period only."""
    now = now or datetime.now(UTC)
    focus = set(window.get("focus_days") or [window["focus"]])
    context = set(window.get("context_days") or [])

    def table(items, days):
        rows = {}
        for action in items:
            day = local_day(action.get("executed_at") or action.get("proposed_at"), tz)
            if day is None or day.isoformat() not in days:
                continue
            key = str(action.get("action_id") or "unknown")
            row = rows.setdefault(key, {"action_id": key, **dict.fromkeys(ACTION_FUNNEL_COUNTS, 0)})
            row["proposed_total"] += 1
            status = action.get("status")
            proposed = parse_ts(action.get("proposed_at"))
            executed = parse_ts(action.get("executed_at"))
            if status == "proposed":
                stale = proposed and now - proposed > timedelta(hours=ACTION_FUNNEL_RULE["stale_after_h"])
                row["stale" if stale else "pending"] += 1
            elif status in {"succeeded", "failed", "superseded", "canceled", "executing"}:
                row[status] += 1
            if status == "succeeded":
                human = proposed and executed and (executed - proposed).total_seconds() > ACTION_FUNNEL_RULE["reviewer_confirmed_after_s"]
                row["human_confirmed" if human else "auto"] += 1
        total = {"action_id": "total", **{k: sum(r[k] for r in rows.values()) for k in ACTION_FUNNEL_COUNTS}}
        ordered = sorted(rows.values(), key=lambda r: (-r["proposed_total"], r["action_id"]))
        for row in [*ordered, total]:
            denominator = sum(row[k] for k in ("human_confirmed", "failed", "superseded", "canceled", "stale"))
            row["acceptance_rate"] = round(row["human_confirmed"] / denominator, 2) if denominator else None
        return {"rows": ordered, "total": total}

    groups = defaultdict(set)
    for run in runs:
        groups[(run.get("axis", "channel"), run.get("axis_key") or "?")].add(run_key(run))
    members = {str(r.get("member")) for r in runs}
    if axes is None:
        axes = [{"axis": axis, "key": key} for axis, key in sorted(groups)]
        if len(members) > 1:
            axes += [{"axis": "member", "key": member} for member in sorted(members)]
    per_axis = []
    for axis in axes:
        kind, key = axis["axis"], axis["key"]
        items = [a for a in actions if (str(a.get("member")) == key if kind == "member"
                 else str(a.get("run_id")) in groups[(kind, key)])]
        per_axis.append({"axis": kind, "key": key, **table(items, focus)})
    return {"focus": table(actions, focus), "context": table(actions, context),
            "per_axis": per_axis, "rule": dict(ACTION_FUNNEL_RULE)}


def action_funnel_digest(funnel):
    rule = funnel["rule"]
    out = ["", "## Action funnel", "",
           f"Focus period · reviewer-confirmed heuristic: execution > {rule['reviewer_confirmed_after_s']} s "
           f"after proposal; stale > {rule['stale_after_h']} h at collection. "
           "Bucket: executed_at or proposed_at; acceptance excludes auto and pending.", ""]
    if not funnel["focus"]["rows"]:
        return out + ["No actions in the focus period."]
    fields = ("proposed_total", "human_confirmed", "auto", "failed", "superseded", "canceled", "executing", "pending", "stale", "acceptance_rate")
    out += ["| action_id | proposed | ✓ human | ✓ auto | ✗ failed | superseded | canceled | executing | pending | stale | acceptance |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in [*funnel["focus"]["rows"], funnel["focus"]["total"]]:
        values = ["—" if row[k] is None else str(row[k]) for k in fields]
        out.append("| " + str(row["action_id"]).replace("|", "\\|") + " | " + " | ".join(values) + " |")
    for axis in funnel["per_axis"]:
        row = axis["total"]
        if row["proposed_total"]:
            out.append(f"- {axis['axis']} `{axis['key']}`: " + ", ".join(
                f"{k}={row[k]}" for k in fields if row[k] or k == "acceptance_rate"))
    return out


# ------------------------------------------------------------------------ digest


RECOVERED_OUTCOMES = {"answered", "replied", "drafted", "sent"}

KIND_ORDER = {"run_error": 0, "action_failure": 1, "capture_gap": 2, "sql": 3, "usage": 4}


def focus_dates(ev):
    return set(ev['window'].get('focus_days') or [ev['window']['focus']])


def count_period(days, runs, actions, events, deltas, feedback, tz):
    if isinstance(days, date):
        days = [days]
    blocks = [count_day(day, runs, actions, events, deltas, feedback, tz) for day in days]
    return {'date': max(days).isoformat(), **{k: sum(b[k] for b in blocks) for k in KPI_KEYS}}


def rank(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Yesterday first. A signature that did not fire on D is history, however ugly it looks."""

    def key(cluster: dict[str, Any]) -> tuple:
        state = (cluster.get("recurrence") or {}).get("state")
        return (
            0 if cluster["focus_count"] else 1,      # active on D beats everything
            -cluster["focus_count"],
            0 if state == "new" else 1,              # new before recurring at equal volume
            KIND_ORDER.get(cluster["kind"], 5),
            -cluster["context_count"],
        )

    return sorted(clusters, key=key)


def run8(run_id: Any) -> str:
    """Embassy deltas carry no run id at all (`related_run_missing`) — say so, don't print `None`."""
    text = str(run_id or "").strip()
    return text[:8] if text and text != "None" else "no-run"


def exclusion_lines(ev: dict[str, Any]) -> list[str]:
    """The excluded arithmetic, once, with both scopes named.

    The window total and the focus-day total are different numbers and used to be printed three
    times with three different denominators. Say which is which.
    """
    excluded = ev["excluded"]
    if not excluded:
        return []
    focus_iso = ev["window"]["focus"]
    window_days = len(ev["window"]["context_days"]) + len(focus_dates(ev))
    runs = ev["runs"]
    focus_runs = [r for r in runs if r.get("day") in focus_dates(ev)]
    focus_excluded = sum(1 for r in focus_runs if r.get("excluded_reason"))
    detail = ", ".join(f"{k} {v}" for k, v in sorted(excluded.items(), key=lambda i: -i[1]))
    total = sum(excluded.values())
    return [
        f"Excluded as non-customer traffic — window ({window_days} days): {detail} = {total} "
        f"(`kind:console` runs are a separate plane and are not in the {len(runs)} runs below)",
        f"Excluded — focus period {min(focus_dates(ev))} → {max(focus_dates(ev))}: {focus_excluded} of {len(focus_runs)} runs "
        f"({len(focus_runs) - focus_excluded} counted; that is the denominator for every cluster)",
    ]


def coverage_warnings(ev: dict[str, Any]) -> list[str]:
    """Shout when a filter deleted a whole member.

    A default `include_kinds` that drops 41 of 42 runs is not coverage, it is a missing channel —
    and the only way it used to surface was counting rows in evidence.json by hand.
    """
    out: list[str] = []
    per_member: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in ev["runs"]:
        per_member[str(run.get("member") or "?")].append(run)
    for member, group in sorted(per_member.items()):
        counted = sum(1 for r in group if not r.get("excluded_reason"))
        if len(group) >= 5 and counted / len(group) < 0.2:
            reasons = Counter(r["excluded_reason"] for r in group if r.get("excluded_reason"))
            top = ", ".join(f"{k} {v}" for k, v in reasons.most_common(3))
            out.append(
                f"⚠ coverage: {member}: {counted} of {len(group)} runs counted in the window "
                f"({top}) — check `include_kinds`/noise filters before concluding anything "
                f"about this member"
            )
    return out


def owner_language_line(ev: dict[str, Any]) -> str:
    """The judging LLM writes the owner half in these languages — it must never guess Dutch."""
    project_lang = str(ev.get("owner_lang") or "en")
    by_tenant = ev.get("owner_lang_by_tenant") or {}
    differing = [f"{slug}: {code}" for slug, code in sorted(by_tenant.items()) if code != project_lang]
    tail = (" · " + ", ".join(differing[:8])) if differing else (
        f" · {len(by_tenant)} tenant(s) idem" if by_tenant else "")
    return f"Owner language: {project_lang} (project){tail} — write every owner field in it."


def build_digest(ev: dict[str, Any], tz) -> str:
    focus = date.fromisoformat(ev["window"]["focus"])
    kpis = ev["kpis"]
    out: list[str] = [
        f"# Fleet digest · {ev['report_id']} · {min(focus_dates(ev))} → {max(focus_dates(ev))}",
        f"Focus {min(focus_dates(ev))} → {max(focus_dates(ev))} · context {', '.join(ev['window']['context_days'])} "
        f"({ev['timezone']}) · generated {ev['generated_at'][:16]}",
        "",
        "**Copy `kpis.json` verbatim into report.json — never retype counters.**",
        "",
    ]
    coverage = ev["coverage"]
    degraded = [c for c in coverage if c["status"] != "complete"]
    out.append(
        f"Coverage: {len(coverage) - len(degraded)}/{len(coverage)} feeds complete"
        + (" · " + "; ".join(f"{c['feed']}={c['status']}({c.get('reason', '')})" for c in degraded[:8])
           if degraded else "")
    )
    if ev.get("overlay_problems"):
        out.append("Overlay problems: " + "; ".join(ev["overlay_problems"][:3]))
    out.append(owner_language_line(ev))
    out += exclusion_lines(ev)
    out += coverage_warnings(ev)
    out.append("")

    def kpi_line(block: dict[str, Any]) -> str:
        return (
            f"{block['date']}: {block['counted']} counted (+{block['excluded']} excl) · "
            f"{block['drafts']} drafts · sent {block['sent_as_proposed']} / edited {block['edited']} / "
            f"shadow {block['shadow_compared']} / unsent {block['not_sent_yet']} / no-draft {block['no_draft']} · "
            f"actions {block['actions_ok']}✓ {block['actions_failed']}✗ {block['actions_proposed']}• · "
            f"errors {block['run_errors']} run / {block['bash_real_errors']} bash / {block['usage_errors']} usage · "
            f"deltas {block['deltas_live']}L/{block['deltas_shadow']}S · feedback {block['feedback']}"
        )

    out += ["## KPIs", "- FOCUS " + kpi_line(kpis["focus"])]
    # A context day where every counter is 0 is five words of information, not a KPI line.
    quiet_days = [b["date"] for b in kpis["context_days"] if not any(b[k] for k in KPI_KEYS)]
    out += [f"- ctx   {kpi_line(block)}" for block in kpis["context_days"]
            if block["date"] not in quiet_days]
    if quiet_days:
        out.append(f"- ctx   {', '.join(quiet_days)}: no runs at all")
    axis_rows = kpis["per_axis"]
    if len(axis_rows) == 1:
        # One tenant/channel means the axis table restates the FOCUS line; one sentence is enough.
        row = axis_rows[0]
        out.append(f"- axis  single {row['axis']} `{row['key']}`"
                   f"{' (' + row['mode'] + ')' if row.get('mode') else ''} — "
                   f"identical to the focus totals, no per-axis table")
    elif axis_rows:
        out.append("")
        out.append(f"| {axis_rows[0]['axis']} | counted | drafts | sent | edited | unsent | act ✓/✗/• | errors |")
        out.append("|---|---|---|---|---|---|---|---|")
        for row in axis_rows[:25]:
            out.append(
                f"| {row['name']}{' (' + row['mode'] + ')' if row.get('mode') else ''} | {row['counted']} | "
                f"{row['drafts']} | {row['sent_as_proposed']} | {row['edited']} | {row['not_sent_yet']} | "
                f"{row['actions_ok']}/{row['actions_failed']}/{row['actions_proposed']} | "
                f"{row['run_errors']}+{row['bash_real_errors']} |"
            )

    if kpis.get("action_funnel"):
        out += action_funnel_digest(kpis["action_funnel"])

    out += ["", "## Clusters (ranked; recurrence vs the context days)"]
    clusters = rank(ev["clusters"])
    active = [c for c in clusters if c["focus_count"]]
    gone = [c for c in clusters if not c["focus_count"]]
    # `gone` action failures are the long tail of one-off ClickDoc/PMS refusals: they crowd out
    # everything actionable, so they get a hard cap and a count.
    gone_actions = [c for c in gone if c["kind"] == "action_failure"]
    gone_rest = [c for c in gone if c["kind"] != "action_failure"]
    shown = active[:30] + gone_actions[:10] + gone_rest[:15]
    if not shown:
        out.append("- none")

    def cluster_line(cluster: dict[str, Any]) -> None:
        rec = cluster["recurrence"]
        out.append(
            # `sig=` inline, verbatim: the bold title is privacy-reduced and truncated, so it is
            # not an identifier — the signature is what a finding and `drill --cluster` need.
            f"- sig={cluster['signature']} · **{privacy(cluster['title'], 160)}** — "
            f"focus {rec['focus_count']}× / context {rec['context_count']}× · {rec['state']}"
            + (f" · +{cluster['excluded_count']} excl" if cluster.get("excluded_count") else "")
            + (f" · known since {stamp(rec['known_since'], tz)[:10]}" if rec.get("known_since") else "")
            + (f" · first {stamp(rec['first_seen'], tz)}" if rec.get("first_seen") else "")
            + " · runs " + ", ".join(cluster["run8s"][:6])
        )
        if cluster.get("detail"):
            out.append(f"  - {privacy(cluster['detail'], 240)}")

    for cluster in active[:30]:
        cluster_line(cluster)
    if len(active) > 30:
        out.append(f"- … {len(active) - 30} more clusters active on D in evidence.json")
    if gone:
        out.append(f"\n**Not seen on D** (context days only — absence is not resolution):")
        for cluster in gone_actions[:10] + gone_rest[:15]:
            cluster_line(cluster)
        hidden_actions = max(0, len(gone_actions) - 10)
        hidden_rest = max(0, len(gone_rest) - 15)
        if hidden_actions or hidden_rest:
            out.append(f"- … {hidden_actions} more action-failure and {hidden_rest} other context-only "
                       f"signatures in evidence.json")

    # Runs excluded as non-customer traffic are already counted on the KPI line; listing them again
    # buries the real ones. A run-level ERROR still earns its line, marked `excl`.
    def listable(run: dict[str, Any]) -> bool:
        return not run.get("excluded_reason") or bool(run.get("error"))

    focus_all = [r for r in ev["runs"] if r.get("day") in focus_dates(ev)]
    focus_runs = [r for r in focus_all if listable(r)]
    focus_excluded = sum(1 for r in focus_all if r.get("excluded_reason"))
    shown_excluded = sum(1 for r in focus_runs if r.get("excluded_reason"))
    out += ["", f"## Focus-period runs ({len(focus_runs)} shown of {len(focus_all)} collected; "
                f"{focus_excluded} excluded as noise, of which {shown_excluded} still shown "
                f"because the run itself errored — marked `excl:`)"]
    for run in sorted(focus_runs, key=lambda r: str(r.get("created_at")))[:60]:
        out.append(run_line(run, tz))
    if len(focus_runs) > 60:
        out.append(f"- … {len(focus_runs) - 60} more in runs.jsonl")
    ctx_all = [r for r in ev["runs"] if r.get("day") not in focus_dates(ev) and r.get("flagged")]
    flagged_ctx = [r for r in ctx_all if listable(r)]
    if flagged_ctx:
        out += ["", f"## Context-day flagged runs ({len(flagged_ctx)} of {len(ctx_all)}; "
                    f"{len(ctx_all) - len(flagged_ctx)} excluded as noise)"]
        for run in sorted(flagged_ctx, key=lambda r: str(r.get("created_at")))[:25]:
            out.append(run_line(run, tz, with_date=True))
        if len(flagged_ctx) > 25:
            out.append(f"- … {len(flagged_ctx) - 25} more in runs.jsonl")

    out += ["", *delta_section(ev, tz)]
    out += ["", *feedback_section(ev, tz)]

    if ev["draft_fate"]:
        out += ["", "## Draft fate (draft-mode projects: a placed draft is not proof of a send)"]
        for key, counts in ev["draft_fate"].items():
            out.append(f"- {key}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    if ev.get("watch"):
        out += ["", "## Watch items"]
        out += [f"- {privacy(item, 220)}" for item in ev["watch"][:20]]

    commits = ev.get("commits_md")
    if commits:
        out += ["", commits.rstrip()]

    if ev["errors"]:
        out += ["", f"## Collection errors ({len(ev['errors'])})"]
        out += [f"- {e['feed']} {clip(e['key'], 60)}: {clip(e['error'], 160)}" for e in ev["errors"][:15]]
    return "\n".join(out) + "\n"


def delta_section(ev: dict[str, Any], tz) -> list[str]:
    """One line per real human edit, focus period first, with the tenant/channel it happened on.

    A delta only becomes actionable once you know *who* edited: `factual, sim 0.28` says nothing,
    `de-kies · factual · sim 0.28` points at a tenant brain.
    """
    focus_iso = ev["window"]["focus"]
    axis_of = {run8(r.get("run_id")): r.get("axis_key") for r in ev["runs"]}
    day_of = {run8(r.get("run_id")): r.get("day") for r in ev["runs"]}
    deltas = [d for d in ev["deltas"] if not d.get("false_delta")]
    live = [d for d in deltas if not d.get("shadow")]
    shadow = [d for d in deltas if d.get("shadow")]
    # equivalent shadow verdicts say "the human agreed" — a count, not a line each.
    interesting = live + [d for d in shadow if str(d.get("shadow_verdict") or "") != "equivalent"]

    def sort_key(delta: dict[str, Any]) -> tuple:
        try:
            similarity = float(delta.get("similarity"))
        except (TypeError, ValueError):
            similarity = 1.0
        return (delta.get("day") not in focus_dates(ev), bool(delta.get("shadow")), similarity)

    bodies_left = 8  # the bodies are the bulk of the section; the rest is one line each
    out = [f"## Human edits ({len(live)} live, {len(shadow)} shadow of which "
           f"{len(interesting) - len(live)} non-equivalent, "
           f"{len(ev['deltas']) - len(deltas)} cosmetic dropped)"]
    for delta in sorted(interesting, key=sort_key)[:25]:
        focus_day = delta.get("day") in focus_dates(ev)
        key = run8(delta.get("related_run_id"))
        # A delta is dated by the *edit*, not by the run: three FOCUS deltas can point at runs that
        # are not in the focus-run list at all.
        run_day = day_of.get(key)
        out.append(
            f"- {'**FOCUS** ' if focus_day else ''}{delta.get('day', '?')} {key}"
            + (f" (run {run_day})" if run_day and run_day != delta.get("day") else "") + " · "
            f"{axis_of.get(key) or delta.get('channel') or '-'} · "
            f"{'shadow' if delta.get('shadow') else 'live'} · {delta.get('delta_category') or '-'} · "
            f"sim {_sim(delta.get('similarity'))} · {privacy(delta.get('delta_description'), 260)}"
        )
        # The bodies are the expensive part of the digest; only yesterday's *live* edits (the ones
        # you can still act on) are worth carrying them.
        if focus_day and not delta.get("shadow") and bodies_left > 0:
            bodies_left -= 1
            out.append(f"  - proposed: {privacy(delta.get('proposed_body'), 160)}")
            out.append(f"  - sent:     {privacy(delta.get('sent_body_clean') or delta.get('sent_body'), 160)}")
    return out


def feedback_section(ev: dict[str, Any], tz) -> list[str]:
    """Feedback verbatim, dated. Yesterday's first; anything older than the window is a backlog."""
    if not ev["feedback"]:
        return []
    focus_iso = ev["window"]["focus"]
    window = set(ev["window"]["context_days"]) | focus_dates(ev)
    oldest = min(window)

    def when(item: dict[str, Any]) -> str:
        moment = local_day(item.get("comment_set_at") or item.get("score_set_at"), tz)
        return moment.isoformat() if moment else "?"

    dated = [(when(item), item) for item in ev["feedback"]]
    focus = [row for row in dated if row[0] in focus_dates(ev)]
    recent = [row for row in dated if row[0] not in focus_dates(ev) and row[0] >= oldest]
    older = sorted([row for row in dated if row[0] < oldest], key=lambda row: row[0], reverse=True)

    # Which member and which run kind a score sits on is what turns a list of comments into a
    # finding ("every score-1 is on one plane"); joining it by hand used to be the work.
    meta_of = {run8(r.get("run_id")): (str(r.get("member") or "?"), str(r.get("kind") or "?"))
               for r in ev["runs"]}

    def line(row: tuple[str, dict[str, Any]]) -> str:
        day, item = row
        key = run8(item.get("run_id"))
        member, kind = meta_of.get(key, (str(item.get("member") or "?"), "?"))
        return (f"- {day} {key} · {member}/{kind} · score {item.get('score', '-')} · "
                f"{privacy(item.get('comment'), 300) or '(no comment)'}")

    out = [f"## Feedback verbatim ({len(ev['feedback'])})"]
    out += [line(row) for row in focus]
    out += [line(row) for row in sorted(recent, key=lambda row: row[0], reverse=True)]
    if older:
        out.append(f"\n**Still open (older than the window; {len(older)} total)**")
        out += [line(row) for row in older[:10]]
        if len(older) > 10:
            out.append(f"- … {len(older) - 10} more in evidence.json")
    return out


def _sim(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def run_line(run: dict[str, Any], tz, with_date: bool = False) -> str:
    bits = [
        (f"{run['day']} " if with_date else "") + hhmm(run.get("created_at"), tz),
        run8(run.get("run_id")),
        run.get("axis_key") or "-",
        privacy(run.get("topic"), 70) or "-",
        str(run.get("human_outcome") or run.get("outcome") or "-"),
    ]
    if run.get("excluded_reason"):
        bits.append(f"excl:{run['excluded_reason']}")
    if run.get("error"):
        bits.append("ERROR " + privacy(run["error"], 180))
    # A lost or errored run whose thread got an answer later is not a customer left hanging.
    if run.get("recovered_at") and (run.get("error")
                                   or str(run.get("human_outcome") or "") in {"no_draft", "-"}
                                   or str(run.get("outcome") or "") in {"failed", "error", "stuck"}):
        bits.append(f"↻ recovered {hhmm(run['recovered_at'], tz)} by {run8(run.get('recovered_by'))}")
    if run.get("bash_errors"):
        bits.append("bash " + ", ".join(f"{k}×{v}" for k, v in sorted(run["bash_errors"].items())))
    if run.get("unmounted"):
        bits.append("unmounted: " + ",".join(run["unmounted"][:4]))
    if run.get("draft_deferral"):
        bits.append("✏️ placeholder in draft")
    if run.get("actions"):
        bits.append(" ".join(run["actions"]))
    if run.get("notes_flag"):
        bits.append("👀note")
    return "- " + " · ".join(b for b in bits if b)


# --------------------------------------------------------------------------- main


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="focus period YYYY-MM-DD (default: yesterday, project tz)")
    parser.add_argument("--project", action="append", default=[], help="member project (repeatable)")
    parser.add_argument("--overlay", help="shared overlay directory (default: brain _internal/fleet-report)")
    parser.add_argument("--report-id", help="override the overlay report id")
    parser.add_argument("--days", type=int, default=1, help="calendar days in focus, ending on --date")
    parser.add_argument("--context-days", type=int, default=4, help="workdays of context before D")
    parser.add_argument("--max-traces", type=int, default=80)
    parser.add_argument("--refresh", action="store_true", help="ignore the raw cache")
    parser.add_argument("--offline", action="store_true", help="raw cache only, never call rc")
    parser.add_argument("--no-correlate", action="store_true")
    parser.add_argument("--prune-raw", action="store_true",
                        help="delete raw/ after a successful run (the cache is 10-100x the report)")
    parser.add_argument("--out-dir")
    parser.add_argument("--dsn", help="local review queue database; default operator SSM tunnel")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    brain_root = find_brain_root()
    overlay = load_overlay(brain_root, args.overlay)
    tz = tzinfo(str(overlay.get("timezone", DEFAULT_TZ)))
    status = auth_status(brain_root)

    members: list[str] = list(args.project)
    if not members:
        members = [str(m.get("project")) for m in overlay.get("members", []) if m.get("project")]
    if not members:
        members = [project_name(brain_root, status)]
    report_id = args.report_id or str(overlay.get("report_id") or members[0])

    focus = date.fromisoformat(args.date) if args.date else yesterday(tz)
    if args.days < 1:
        raise SystemExit('--days must be positive')
    focus_days = [focus - timedelta(days=i) for i in reversed(range(args.days))]
    context = workdays_before(focus_days[0], max(0, args.context_days))
    days = [*context, *focus_days]
    start, _ = day_bounds(days[0], tz)
    _, end = day_bounds(focus, tz)

    report_root = brain_root / ".replypen" / "fleet-report" / report_id
    out_dir = Path(args.out_dir) if args.out_dir else report_root / (focus.isoformat() if args.days == 1 else f"{focus.isoformat()}-{args.days}d")
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    rc = Rc(raw_dir=out_dir / "raw", cwd=brain_root, refresh=args.refresh, offline=args.offline)

    cfg = {
        "include_kinds": tuple(overlay.get("include_kinds", DEFAULT_INCLUDE_KINDS)),
        "dev_tenants": set(overlay.get("dev_tenants", [])),
        "noise_topics": tuple(t.lower() for t in overlay.get("noise_topics", [])),
        "noise_senders": tuple(s.lower() for s in overlay.get("noise_senders", [])),
        "pattern_days": max(7, (datetime.now(tz).date() - days[0]).days + 1),
        "lookback_days": (datetime.now(tz).date() - days[0]).days + 1,
        "max_traces": args.max_traces,
        "focus_days": set(focus_days),
    }

    members_data = [
        collect_member(rc, member, (start, end), cfg, overlay) for member in members
    ]

    # Teach the name reducer the project vocabulary first: `De Kies` and `Sint-Joris` are
    # practices, not patients, and must survive privacy().
    learn_non_names(
        [str(t.get("slug") or "") for data in members_data for t in data["tenants"]]
        + [str(t.get("name") or "") for data in members_data for t in data["tenants"]]
        + [str(report_id), *members]
        # `known_non_names` lets a project spare its own vocabulary (`Power Filter`, `Dynamic Forms`)
        + [str(w) for w in overlay.get("known_non_names", [])]
    )

    runs: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    feedback: list[dict[str, Any]] = []
    triage: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    http: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    watch: list[str] = []

    for data in members_data:
        member = data["member"]
        enrich_runs(rc, member, data["runs"], focus, tz, cfg)
        mailboxes = data["mailboxes"]
        tenants = {str(t.get("slug") or t.get("id")): t for t in data["tenants"]}
        mailbox_mode = {}
        for box in mailboxes:
            mailbox_mode.setdefault(str(box.get("tenant") or ""), str(box.get("mode") or ""))
            expires = parse_ts(box.get("subscription_expires_at"))
            if expires and expires < datetime.now(UTC) + timedelta(days=5):
                watch.append(f"{member}: mailbox {box.get('slug')} watch expires "
                             f"{str(box.get('subscription_expires_at'))[:16]} — silent run loss if it lapses")
        for box in mailboxes:
            if str(box.get("status")) not in {"active", ""} and str(box.get("mode")) != "off":
                watch.append(f"{member}: mailbox {box.get('slug')} status={box.get('status')}")

        # bash corpus, reduced to the runs we actually collected
        run_ids = {run_key(r) for r in data["runs"]}
        for event in data["events"]:
            if str(event.get("run_id")) not in run_ids:
                continue
            exit_code = event.get("exit_code")
            if not isinstance(exit_code, int) or exit_code == 0:
                continue
            command = str((event.get("args") or {}).get("command") or "")
            stderr = str(event.get("stderr") or "")
            event["bucket"] = bash_bucket(command, exit_code, stderr, str(event.get("stdout") or "")[:2000])
            event["cluster"] = bash_cluster(command, stderr)
            event["member"] = member
            events.append(event)
        for row in data["http"]:
            if int(row.get("status_code") or 0) >= 400 and str(row.get("run_id")) in run_ids:
                row["member"] = member
                http.append(row)

        for run in data["runs"]:
            run["member"] = member
            run["day"] = (local_day(run.get("created_at"), tz) or focus).isoformat()
            run["flagged"] = is_flagged(run)
            # An overlay tag that starts with `noise:` is an exclusion the kit cannot express
            # in config.toml (vendor identity, sender heuristics, …). It counts under its own
            # tag name in `excluded`, exactly like a built-in reason.
            run["tags"] = [str(t) for t in (overlay.call("classify_run", run, default=[]) or [])]
            noise_tag = next((t for t in run["tags"] if t.startswith("noise:")), None)
            run["excluded_reason"] = exclusion_reason(run, cfg) or noise_tag
            trace = run.get("trace") or {}
            if trace:
                run["unmounted"] = unmounted_sources(trace)
            tenant = str(run.get("tenant") or "")
            if tenants:
                run["axis"] = "tenant"
                run["axis_key"] = tenant or "?"
                run["axis_name"] = (tenants.get(tenant) or {}).get("name") or tenant or "?"
                run["mode"] = mailbox_mode.get(tenant, "")
            else:
                run["axis"] = "channel"
                run["axis_key"] = channel_of(run, mailboxes, overlay)
                run["axis_name"] = run["axis_key"]
                run["mode"] = mailbox_mode.get("", "")
            runs.append(run)

        for row in data["actions"]:
            row["member"] = member
            actions.append(row)
        for row in data["deltas"]:
            row["member"] = member
            row["false_delta"] = is_false_delta(row)
            row["day"] = (local_day(row.get("sent_at") or row.get("received_at"), tz) or focus).isoformat()
            deltas.append(row)
        seen_feedback = {str(f.get("run_id")) for f in data["feedback"]}
        for row in data["feedback"] + [f for f in data["feedback_30d"]
                                       if str(f.get("run_id")) not in seen_feedback]:
            row["member"] = member
            feedback.append(row)
        for row in data["triage"]:
            row["member"] = member
            triage.append(row)
        if data["health"]:
            health[member] = data["health"]

    # join per-run derived facts
    events_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_run[str(event.get("run_id"))].append(event)
    deltas_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for delta in deltas:
        deltas_by_run[str(delta.get("related_run_id"))].append(delta)
    actions_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        if action.get("run_id"):
            actions_by_run[str(action["run_id"])].append(action)

    marks = {"succeeded": "✓", "failed": "✗", "proposed": "•", "canceled": "⊘", "superseded": "↷"}
    for run in runs:
        key = run_key(run)
        buckets = Counter(e["bucket"] for e in events_by_run.get(key, []))
        run["bash_errors"] = {k: v for k, v in buckets.items() if k not in NOISE_BUCKETS}
        run["human_outcome"] = human_outcome(run, deltas_by_run.get(key, []), str(run.get("mode") or ""))
        run["actions"] = [f"{marks.get(str(a.get('status')), '?')}{a.get('action_id')}"
                          for a in actions_by_run.get(key, [])]
        run["notes_flag"] = any("👀" in str(n.get("body")) for n in run.get("notes") or [])

    # Queue rows are the only disposition/recurrence memory. Offline reads a marked cache.
    from prior import prior_findings, prior_table
    prior_path = out_dir / "prior.json"
    if args.offline:
        prior = json.loads(prior_path.read_text())
    else:
        prior = prior_findings(projects=members, dsn=args.dsn)
        prior_path.write_text(json.dumps(prior, default=str, ensure_ascii=False))
    ledger = {}
    for signature, entry in prior.items():
        first = [str(r['first_seen']) for r in entry['rows'] if r.get('first_seen')]
        last = [str(r['last_seen']) for r in entry['rows'] if r.get('last_seen')]
        ledger[signature] = {'first_seen': min(first) if first else None,
                             'last_seen': max(last) if last else None}

    focus_iso = focus.isoformat()
    focus_isos = {d.isoformat() for d in focus_days}
    clusters = build_clusters(runs, actions, events, http, ledger, focus_isos, tz)

    # ---------------- kpis
    kpis = {
        "focus": count_period(focus_days, runs, actions, events, deltas, feedback, tz),
        "context_days": [count_day(day, runs, actions, events, deltas, feedback, tz) for day in context],
        "per_axis": per_axis_blocks(runs, actions, events, deltas, feedback, focus_days, tz, len(members) > 1),
    }

    kpis["action_funnel"] = build_action_funnel(
        actions, runs, {"focus": focus_iso, "focus_days": sorted(focus_isos),
                        "context_days": [d.isoformat() for d in context]},
        tz=tz, axes=kpis["per_axis"],
    )

    draft_fate: dict[str, dict[str, int]] = {}
    for run in runs:
        if run.get("day") not in focus_isos or run.get("excluded_reason"):
            continue
        counts = draft_fate.setdefault(run.get("axis_key") or "-", {})
        counts[str(run.get("human_outcome"))] = counts.get(str(run.get("human_outcome")), 0) + 1

    excluded_counter = Counter(r["excluded_reason"] for r in runs if r.get("excluded_reason"))
    console_total = sum(len(d["console_runs"]) for d in members_data)
    if console_total:
        excluded_counter["kind:console"] = console_total

    # The owner half is written in the language the HOST resolves for the project/tenant
    # (`persona.language`); the overlay's `[owner].lang` is an explicit override, never a default.
    # Both travel through the manifest so the judging LLM and the validator key off the same codes.
    owner_lang, owner_lang_by_tenant = owner_languages(
        rc, members,
        {d["member"]: [str(t.get("slug") or t.get("id") or "") for t in d["tenants"]] for d in members_data},
        override=(overlay.get("owner", {}) or {}).get("lang"),
    )
    coverage = [c.as_dict() for c in rc.coverage]
    raw_dir = out_dir / "raw"
    raw_bytes = sum(f.stat().st_size for f in raw_dir.rglob("*") if f.is_file())
    manifest = {
        "report_id": report_id,
        "projects": members,
        "window": {"focus": focus_iso, "focus_days": sorted(focus_isos), "context_days": [d.isoformat() for d in context]},
        "generated_at": datetime.now(UTC).isoformat(),
        "rc_version": rc_version(brain_root),
        "coverage": coverage,
        "excluded": dict(excluded_counter),
        "owner_lang": owner_lang,
        "owner_lang_by_tenant": owner_lang_by_tenant,
        "raw": {
            "path": str(raw_dir.relative_to(out_dir)),
            "files": sum(1 for f in raw_dir.rglob("*") if f.is_file()),
            "mb": round(raw_bytes / 1_048_576, 1),
            "pruned": bool(args.prune_raw),
        },
    }

    evidence = {
        "report_id": report_id,
        "projects": members,
        "timezone": str(tz),
        "window": manifest["window"],
        "generated_at": manifest["generated_at"],
        "auth": {k: status.get(k) for k in ("base_url", "logged_in", "login_all_projects", "profile")},
        "coverage": coverage,
        "excluded": dict(excluded_counter),
        "overlay_problems": overlay.problems,
        "owner_lang": owner_lang,
        "owner_lang_by_tenant": owner_lang_by_tenant,
        "kpis": kpis,
        "clusters": clusters,
        "runs": runs,
        "actions": actions,
        "deltas": deltas,
        "feedback": feedback,
        "triage": triage,
        "http_errors": http[:200],
        "health": health,
        "deploy_state": {d["member"]: d["deploy_state"] for d in members_data},
        "mailboxes": {d["member"]: d["mailboxes"] for d in members_data},
        "tenants": {d["member"]: d["tenants"] for d in members_data},
        "draft_fate": draft_fate,
        "watch": watch,
        "errors": rc.errors,
    }

    if not args.no_correlate:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import correlate  # noqa: PLC0415

            full, brief, found = correlate.run(
                brain_root, members, overlay, clusters, days, tz, evidence["deploy_state"]
            )
            evidence["commits_md"], evidence["commit_candidates"] = brief, found
            (out_dir / "commits.md").write_text(full, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - correlation is a nice-to-have
            rc.note_error("correlate", "git", f"{type(exc).__name__}: {exc}")

    digest = build_digest(evidence, tz)
    digest += "\n" + prior_table(prior)
    if args.offline:
        digest += "\nOffline: prior table is a cached Postgres snapshot; refresh before publishing.\n"
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out_dir / "kpis.json").write_text(json.dumps(kpis, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out_dir / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / "digest.md").write_text(digest, encoding="utf-8")
    with (out_dir / "runs.jsonl").open("w", encoding="utf-8") as handle:
        for run in sorted(runs, key=lambda r: str(r.get("created_at"))):
            handle.write(json.dumps(reduce_run(run), ensure_ascii=False, default=str) + "\n")

    if args.prune_raw:
        # The cache is the expensive part on disk (`fleet patterns` alone is tens of MB a week).
        # Everything downstream reads evidence.json, so dropping it is safe once we got here.
        shutil.rmtree(raw_dir, ignore_errors=True)

    focus_kpi = kpis["focus"]
    print(
        f"{report_id} {focus_iso} · {len(members)} project(s) · {len(runs)} runs "
        f"({focus_kpi['counted']} counted in focus) · {len(clusters)} clusters · "
        f"{len(deltas)} deltas · {len(feedback)} feedback · {rc.calls} rc calls · "
        f"{len(rc.errors)} collect errors · {digest.count(chr(10))} digest lines · "
        f"raw {manifest['raw']['mb']} MB{' (pruned)' if args.prune_raw else ''} · "
        f"{time.monotonic() - started:.1f}s → {out_dir}"
    )
    return 0


REDUCED_RUN_KEYS = (
    "run_id", "member", "day", "created_at", "kind", "outcome", "status", "category", "topic",
    "tenant", "axis", "axis_key", "axis_name", "mode", "excluded_reason", "flagged", "human_outcome",
    "has_draft", "error", "bash_errors", "unmounted", "draft_deferral", "actions", "tags",
    "recovered_at", "recovered_by",
    "run_url", "duration_ms", "session_id", "thread_id", "local_thread_id",
)


def reduce_run(run: dict[str, Any]) -> dict[str, Any]:
    out = {key: run.get(key) for key in REDUCED_RUN_KEYS if run.get(key) is not None}
    out["health"] = run.get("health") or {}
    if run.get("notes"):
        out["notes"] = [one_line(n.get("body"), 300) for n in run["notes"]]
    return out


def per_axis_blocks(runs, actions, events, deltas, feedback, focus, tz, by_member: bool) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[(run.get("axis", "channel"), run.get("axis_key") or "?")].append(run)
    run_ids_by_key = {key: {run_key(r) for r in group} for key, group in groups.items()}
    for (axis, key), group in sorted(groups.items(), key=lambda item: -len(item[1])):
        ids = run_ids_by_key[(axis, key)]
        block = count_period(
            focus, group,
            [a for a in actions if str(a.get("run_id")) in ids],
            [e for e in events if str(e.get("run_id")) in ids],
            [d for d in deltas if str(d.get("related_run_id")) in ids],
            [f for f in feedback if str(f.get("run_id")) in ids],
            tz,
        )
        block.pop("date", None)
        row = {"axis": axis, "key": key, "name": group[0].get("axis_name") or key, **block}
        if group[0].get("mode"):
            row["mode"] = group[0]["mode"]
        if any(block[key] for key in KPI_KEYS):
            blocks.append(row)
    if by_member:
        members: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            members[str(run.get("member"))].append(run)
        for member, group in members.items():
            ids = {run_key(r) for r in group}
            block = count_period(
                focus, group,
                [a for a in actions if str(a.get("member")) == member],
                [e for e in events if str(e.get("run_id")) in ids],
                [d for d in deltas if str(d.get("member")) == member],
                [f for f in feedback if str(f.get("member")) == member],
                tz,
            )
            block.pop("date", None)
            blocks.insert(0, {"axis": "member", "key": member, "name": member, **block})
    return blocks


# ---------------------------------------------------------------------- clusters


def build_clusters(runs, actions, events, http, ledger, focus_iso: str, tz) -> list[dict[str, Any]]:
    day_of = {run_key(r): r.get("day") for r in runs}
    url_of = {run_key(r): r.get("run_url") for r in runs}
    # A run excluded as non-customer traffic is not part of the denominator, so it must not be
    # part of any numerator either: it contributes no cluster count. It stays visible (a run-level
    # ERROR on an excluded run is still a fact), but flagged and counted separately.
    excluded_of = {run_key(r): r.get("excluded_reason") for r in runs if r.get("excluded_reason")}
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def add(kind: str, signature: str, title: str, run_id: Any, when: Any, detail: str = "") -> None:
        key = (kind, signature)
        entry = buckets.setdefault(key, {
            "kind": kind, "signature": f"{kind}:{signature}", "title": title,
            "runs": [], "excluded_runs": [], "focus_count": 0, "context_count": 0,
            "excluded_count": 0, "first": None, "last": None, "detail": detail,
        })
        day = day_of.get(str(run_id)) or (local_day(when, tz) or "").__str__()
        excluded = str(run_id) in excluded_of
        if excluded:
            entry["excluded_count"] += 1
            if run_id and str(run_id) not in entry["excluded_runs"]:
                entry["excluded_runs"].append(str(run_id))
        else:
            if day in ({focus_iso} if isinstance(focus_iso, str) else focus_iso):
                entry["focus_count"] += 1
            else:
                entry["context_count"] += 1
            if run_id and str(run_id) not in entry["runs"]:
                entry["runs"].append(str(run_id))
        moment = parse_ts(when)
        iso = moment.isoformat() if moment else None
        if iso and not excluded:
            entry["first"] = min([t for t in (entry["first"], iso) if t], default=iso)
            entry["last"] = max([t for t in (entry["last"], iso) if t], default=iso)

    for run in runs:
        if run.get("error"):
            add("run_error", normalise_error(run["error"], 120), one_line(run["error"], 160),
                run.get("run_id"), run.get("created_at"))
        for name in run.get("unmounted") or []:
            add("capture_gap", f"unmounted:{name}", f"grounding source `{name}` not mounted at run time",
                run.get("run_id"), run.get("created_at"))
        if run.get("draft_deferral"):
            add("capture_gap", "draft_placeholder", "draft shipped with an unresolved [[✏️]] placeholder",
                run.get("run_id"), run.get("created_at"))

    for action in actions:
        status = str(action.get("status"))
        when = action.get("executed_at") or action.get("proposed_at")
        if status == "failed":
            message = str(action.get("error_message") or action.get("error_class") or "failed")
            add("action_failure", f"{action.get('action_id')}:{normalise_error(message, 90)}",
                f"action `{action.get('action_id')}` failed: {one_line(message, 120)}",
                action.get("run_id"), when)
        elif status == "proposed":
            proposed = parse_ts(action.get("proposed_at"))
            if proposed and (datetime.now(UTC) - proposed) > timedelta(hours=36):
                add("action_stale", str(action.get("action_id")),
                    f"action `{action.get('action_id')}` proposed and never confirmed",
                    action.get("run_id"), when)

    for event in events:
        bucket = event.get("bucket")
        if bucket in NOISE_BUCKETS:
            continue
        command = str((event.get("args") or {}).get("command") or "")
        stderr = str(event.get("stderr") or "")
        if bucket == "usage":
            problem = usage_error(command, f"{event.get('stdout') or ''}\n{stderr}")
            signature = problem["signature"] if problem else event["cluster"]
            title = f"usage: {signature}"
            add("usage", signature, title, event.get("run_id"), event.get("at"))
        else:
            add(bucket or "other", event["cluster"], f"{bucket}: {event['cluster']}",
                event.get("run_id"), event.get("at"),
                detail=one_line(stderr_last_line(stderr), 200))

    http_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in http:
        http_groups[(str(row.get("host")), int(row.get("status_code") or 0))].append(row)
    for (host, code), rows in http_groups.items():
        for row in rows:
            add("http", f"{host}:{code}", f"HTTP {code} from {host} ({len(rows)}× in window)",
                row.get("run_id"), row.get("at"))

    clusters = []
    for entry in buckets.values():
        rec = recurrence(ledger, entry["signature"], entry["kind"], entry["title"],
                         entry["focus_count"], entry["context_count"], entry["first"], entry["last"])
        ids = entry["runs"] + entry["excluded_runs"]
        clusters.append({
            "kind": entry["kind"],
            "signature": entry["signature"],
            "title": entry["title"],
            "detail": entry["detail"],
            "focus_count": entry["focus_count"],
            "context_count": entry["context_count"],
            "excluded_count": entry["excluded_count"],
            # `first_seen`/`last_seen`/`state` are echoed out of `recurrence` so a report can copy
            # them instead of transcribing them out of digest prose.
            "first_seen": rec.get("first_seen"),
            "last_seen": rec.get("last_seen"),
            "state": rec.get("state"),
            "run_ids": ids[:40],
            "run8s": [run8(r) for r in ids[:40]],
            "excluded_run_ids": entry["excluded_runs"][:40],
            "run_urls": [url_of.get(r) for r in ids[:12] if url_of.get(r)],
            "recurrence": rec,
        })
    return clusters


if __name__ == "__main__":
    raise SystemExit(main())
