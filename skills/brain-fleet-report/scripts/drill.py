# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tier 1 of `brain-fleet-report`: everything about ONE run, or ONE cluster, on demand.

    uv run skills/brain-fleet-report/scripts/drill.py --run f89d3d89
    uv run skills/brain-fleet-report/scripts/drill.py --run f89d3d89,a1b2c3d4,9e8f7a6b
    uv run skills/brain-fleet-report/scripts/drill.py --cluster action_failure:create_placeholder
    uv run skills/brain-fleet-report/scripts/drill.py --delta f89d3d89
    uv run skills/brain-fleet-report/scripts/drill.py --feedback

digest.md gives one line per run on purpose. When that line is not enough, this writes
`<report>/<date>/details/run-<run8>.md` (or `cluster-<slug>.md`, `delta-<run8>.md`,
`feedback.md`) with the question, the
draft, the notes, every action, the trace step by step, the human edit, the feedback, and the
clusters the run belongs to. The brain's overlay may append a project-specific follow-up.

Read-only, and cache-first: every rc call reuses collect.py's `raw/` directory, so drilling a run
that was already collected costs nothing. Never `rp ask`.

Two reading rules for everything this writes: every excerpt is **post-reduction** (first names
shortened, e-mail addresses dropped) — mangled-looking text is the privacy reducer, not corruption,
and `raw/` holds the original; and any feed rc could not deliver prints a loud `!!` line rather than
looking like "the run did nothing".

Overlay contract — `drill(run, ctx) -> str | None` in `<brain>/_internal/fleet-report/overlay.py`
returns markdown appended under "## Project follow-up" (fail-soft; an exception is reported, not
fatal). `ctx` is a plain dict:

  run_id project tenant    ids for this run (`tenant` is "" on a tenantless project)
  rc rc_args              `fr_common.Rc` (disk-cached, read-only) + base args incl. --project/--tenant
  show thread actions     `rp run show` / `run thread` payloads, `run actions` items
  trace                   raw trace-stream records, index 0 is the header
  steps                   summarise_steps() output; `step["record"]` is the UNCLIPPED record —
                          `step["stdout"]/["stderr"]` are display summaries, never parse those
  deltas feedback         this run's rows from evidence.json
  tz brain_root privacy fr   timezone, brain checkout, the privacy reducer, the fr_common module
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fr_common import (  # noqa: E402
    DEFAULT_TZ,
    Rc,
    clip,
    find_brain_root,
    first_token,
    hhmm,
    learn_non_names,
    load_overlay,
    one_line,
    privacy,
    project_name,
    reduce_trace_header,
    stamp,
    stderr_last_line,
    tzinfo,
)

MAX_CLUSTER_RUNS = 12
REDUCED = "(names reduced — the original is in `raw/`)"
BODY_CHARS = 1500


def run8(run_id: Any) -> str:
    text = str(run_id or "").strip()
    return text[:8] if text and text != "None" else "no-run"


def rc_failures(rc: Rc, seen: int = 0) -> list[str]:
    """Loud, in the markdown: a feed rc could not deliver must never read as an empty result."""
    return [f"!! {item.get('feed') or 'rc'} unavailable: {clip(item.get('key'), 90)} — "
            f"{clip(item.get('error'), 200)}"
            for item in rc.errors[seen:]]


# --------------------------------------------------------------------- locating


def report_dir(brain_root: Path, report_id: str, wanted: str | None) -> Path:
    """The report directory to read and write into. Latest existing day when `--date` is omitted."""
    root = brain_root / ".replypen" / "fleet-report" / report_id
    if wanted:
        return root / wanted
    days = sorted(p for p in root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]") if p.is_dir())
    return days[-1] if days else root / date.today().isoformat()


def load_evidence(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "evidence.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_run(evidence: dict[str, Any], needle: str) -> dict[str, Any]:
    """Accept a run8 prefix or a full uuid; the evidence tells us the member and tenant."""
    needle = needle.strip()
    for run in evidence.get("runs") or []:
        run_id = str(run.get("run_id") or "")
        if run_id == needle or run_id.startswith(needle):
            return dict(run)
    return {"run_id": needle}


# ------------------------------------------------------------------ trace steps


def _stdout_fields(text: Any, limit: int) -> str:
    """JSON stdout collapses to its keys; anything else is clipped prose. Traces are huge."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw[0] in "{[":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return one_line(
                " ".join(f"{key}={clip(json.dumps(value, ensure_ascii=False, default=str), 60)}"
                         for key, value in list(payload.items())[:8]),
                limit,
            )
        if isinstance(payload, list):
            head = clip(json.dumps(payload[0], ensure_ascii=False, default=str), 120) if payload else ""
            return one_line(f"[{len(payload)} rows] {head}", limit)
    return one_line(raw, limit)


def summarise_steps(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One flat row per tool call. `seq < 0` is the grounding pre-pass the host runs for the model."""
    steps: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not record.get("tool"):
            continue
        command = record.get("command")
        if command is None and record.get("args") is not None:
            command = json.dumps(record["args"], ensure_ascii=False, default=str)
        try:
            seq = int(record.get("seq"))
        except (TypeError, ValueError):
            seq = 0
        steps.append({
            # the untouched record, so an overlay can parse the FULL stdout the digest cannot carry
            "record": record,
            "seq": seq,
            "at": record.get("at"),
            "tool": str(record.get("tool")),
            "status": record.get("status"),
            "exit_code": record.get("exit_code"),
            "grounding": seq < 0,
            "command": one_line(command, 200),
            "stdout": _stdout_fields(record.get("stdout"), 300),
            "stderr": one_line(record.get("stderr"), 300),
        })
    steps.sort(key=lambda step: (step["seq"], str(step["at"])))
    return steps


def step_lines(steps: Sequence[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for step in steps:
        marker = "g" if step["grounding"] else str(step["seq"])
        exit_code = step["exit_code"]
        flag = "" if exit_code in (0, None) else f" **exit {exit_code}**"
        out.append(f"- `{marker}` {step['tool']}{flag}: `{privacy(step['command'], 200)}`")
        if step["stdout"]:
            out.append(f"    - out: {privacy(step['stdout'], 300)}")
        if step["stderr"]:
            out.append(f"    - err: {privacy(step['stderr'], 300)}")
    return out


# ----------------------------------------------------------------------- fetch


def fetch_run(rc: Rc, base: list[str], run_id: str) -> dict[str, Any]:
    trace = rc.jsonl(*base, "run", "trace", run_id, "--stream", timeout=600)
    header = trace[0] if trace and isinstance(trace[0], dict) else {}
    return {
        "show": rc.json(*base, "run", "show", run_id) or {},
        "actions": ((rc.json(*base, "run", "actions", run_id) or {}).get("items")) or [],
        "thread": rc.json(*base, "run", "thread", run_id) or {},
        "trace": trace,
        "header": header,
        "steps": summarise_steps(trace[1:] if trace else []),
    }


# ---------------------------------------------------------------------- render


def render_run(run: dict[str, Any], data: dict[str, Any], evidence: dict[str, Any], tz,
               follow_up: str | None, failures: Sequence[str] = ()) -> str:
    run_id = str(run.get("run_id"))
    short = run_id[:8]
    show, header = data["show"], data["header"]
    reduced = reduce_trace_header(header) if header else {}
    topic = header.get("topic") or run.get("topic")

    out = [
        f"# Run {short} · {privacy(topic, 120) or '(no topic)'}",
        f"{stamp(show.get('created_at') or run.get('created_at'), tz)} · "
        f"{run.get('member') or reduced.get('project') or '?'} · "
        f"tenant {header.get('tenant') or run.get('tenant') or '-'} · "
        f"kind {show.get('kind') or run.get('kind') or '?'} · "
        f"status {show.get('status') or '?'}/{show.get('outcome') or '?'} · "
        f"turns {show.get('turns') or '?'} · bash {show.get('bash_total') or 0}",
    ]
    out += list(failures)
    if show.get("run_url"):
        out.append(f"{show['run_url']}")
    if reduced.get("trigger") or reduced.get("brain_resolved"):
        out.append(f"trigger {reduced.get('trigger') or '-'} · brain {reduced.get('brain_resolved') or '-'}")
    mounts = (reduced.get("grounding") or {}).get("mounts") or []
    if mounts:
        out.append("mounts: " + ", ".join(
            f"{m.get('name')}{'' if m.get('mounted') else ' NOT-MOUNTED'}" for m in mounts))

    error = show.get("error") or header.get("error") or run.get("error")
    if error:
        out += ["", "## Error", f"```\n{clip(str(error), 1500)}\n```"]

    question = header.get("question") or show.get("question")
    if question:
        out += ["", f"## Question {REDUCED}", "```", clip(privacy(question, 1500), 1500), "```"]

    draft = header.get("draft") or show.get("draft_markdown")
    if draft:
        out += ["", f"## Draft {REDUCED}", "```", clip(privacy(draft, 2000), 2000), "```"]
        if "⟦pii:" in str(draft):
            out.append("`⟦pii:…⟧` is **server-side** masking in the stored draft, applied "
                       "inconsistently — not a placeholder the agent shipped.")
        if "[[✏️" in str(draft):
            out.append("**Unresolved `[[✏️]]` placeholder in the draft.**")

    notes = header.get("notes") or show.get("notes") or []
    if notes:
        out += ["", "## Notes"]
        for note in notes:
            body = note.get("body") if isinstance(note, dict) else note
            out.append(f"- {privacy(body, 600)}")

    metadata = show.get("metadata") or header.get("metadata") or {}
    if isinstance(metadata, dict) and metadata:
        keep = {k: v for k, v in metadata.items() if k != "run_url"}
        if keep:
            out += ["", "## Metadata", "- " + one_line(json.dumps(keep, ensure_ascii=False, default=str), 400)]

    if data["actions"]:
        out += ["", f"## Actions ({len(data['actions'])})"]
        for action in data["actions"]:
            out.append(
                f"- **{action.get('action_id')}** {action.get('status')}"
                + (f" · {action.get('error_class')}" if action.get("error_class") else "")
                + (f" · {privacy(action.get('error_message'), 300)}" if action.get("error_message") else "")
                + (f" · {hhmm(action.get('completed_at') or action.get('created_at'), tz)}")
            )
            if action.get("hint"):
                out.append(f"    - hint: {one_line(action['hint'], 200)}")
            if action.get("params"):
                out.append(f"    - params: {privacy(json.dumps(action['params'], ensure_ascii=False, default=str), 400)}")

    thread = data["thread"] or {}
    thread_runs = [r for r in (thread.get("runs") or []) if isinstance(r, dict)]
    if thread_runs:
        out += ["", "## Thread / attribution"]
        for entry in (thread.get("threads") or []):
            if isinstance(entry, dict):
                out.append(f"- thread {entry.get('provider') or '-'} · outcome {entry.get('outcome') or '-'}"
                           + (f" · {privacy(entry.get('why_no_draft'), 200)}" if entry.get("why_no_draft") else ""))
        for entry in thread_runs:
            drafts = (entry.get("attribution") or {}).get("drafts") or []
            mine = "→ " if str(entry.get("run_id")) == run_id else "  "
            out.append(f"- {mine}{str(entry.get('run_id'))[:8]} {entry.get('outcome') or '-'} · "
                       + (", ".join(f"draft {d.get('status')}"
                                    + (f" msg {str(d.get('sent_message_id'))[:16]}" if d.get("sent_message_id") else "")
                                    for d in drafts if isinstance(d, dict)) or "no drafts"))

    steps = data["steps"]
    grounding = [s for s in steps if s["grounding"]]
    agent = [s for s in steps if not s["grounding"]]
    out += ["", f"## Trace ({len(agent)} agent steps, {len(grounding)} grounding pre-pass steps)"]
    if grounding:
        out.append("### Grounding pre-pass (host-run, `seq < 0`)")
        out += step_lines(grounding)
    if agent:
        out.append("### Agent steps")
        out += step_lines(agent)

    deltas = [d for d in (evidence.get("deltas") or []) if str(d.get("related_run_id")) == run_id]
    if deltas:
        out += ["", f"## Human edit ({len(deltas)}) {REDUCED}"]
        for delta in deltas:
            out.append(
                f"- {delta.get('day')} · {'shadow' if delta.get('shadow') else 'live'} · "
                f"{delta.get('delta_category') or '-'} · sim {delta.get('similarity')} · "
                f"verdict {delta.get('shadow_verdict') or '-'}"
            )
            out.append(f"    - why: {privacy(delta.get('delta_description'), 800)}")
            out.append(f"    - proposed: {privacy(delta.get('proposed_body'), 1200)}")
            out.append(f"    - sent: {privacy(delta.get('sent_body_clean') or delta.get('sent_body'), 1200)}")

    scores = [f for f in (evidence.get("feedback") or []) if str(f.get("run_id")) == run_id]
    if scores:
        out += ["", "## Feedback"]
        for item in scores:
            out.append(f"- score {item.get('score', '-')} @ "
                       f"{stamp(item.get('comment_set_at') or item.get('score_set_at'), tz)}: "
                       f"{privacy(item.get('comment'), 600) or '(no comment)'}")

    memberships = [c for c in (evidence.get("clusters") or []) if run_id in (c.get("run_ids") or [])]
    if memberships:
        out += ["", "## Cluster memberships"]
        for cluster in memberships:
            rec = cluster.get("recurrence") or {}
            out.append(f"- `{cluster['signature']}` — {privacy(cluster.get('title'), 140)} "
                       f"(focus {rec.get('focus_count')}× / context {rec.get('context_count')}× · "
                       f"{rec.get('state')})")

    if follow_up:
        out += ["", "## Project follow-up", follow_up.rstrip()]
    return "\n".join(out) + "\n"


def _fate_split(runs: list[dict[str, Any]], field: str) -> str:
    counts = Counter(str(r.get(field) or "-") for r in runs)
    return ", ".join(f"{key} {n}" for key, n in counts.most_common()) or "-"


def render_cluster(cluster: dict[str, Any], evidence: dict[str, Any], examples: dict[str, str], tz,
                   failures: Sequence[str] = ()) -> str:
    rec = cluster.get("recurrence") or {}
    runs_by_id = {str(r.get("run_id")): r for r in (evidence.get("runs") or [])}
    run_ids = list(cluster.get("run_ids") or [])
    excluded = set(cluster.get("excluded_run_ids") or [])
    known = [runs_by_id.get(r) or {} for r in run_ids]
    days = Counter(str((runs_by_id.get(r) or {}).get("day") or "?") for r in run_ids)

    out = [
        f"# Cluster `{cluster['signature']}`",
        privacy(cluster.get("title"), 300),
        "",
        f"focus {rec.get('focus_count')}× / context {rec.get('context_count')}× · {rec.get('state')} · "
        f"first {stamp(rec.get('first_seen'), tz)} · last {stamp(rec.get('last_seen'), tz)}"
        + (f" · known since {stamp(rec.get('known_since'), tz)[:10]}" if rec.get("known_since") else "")
        + (f" · +{cluster['excluded_count']} hit(s) on runs excluded as noise (not counted)"
           if cluster.get("excluded_count") else ""),
    ]
    out += list(failures)
    if cluster.get("detail"):
        out.append(f"detail: {privacy(cluster['detail'], 400)}")
    out += ["", "## Distribution",
            "- days: " + (" · ".join(f"{day} {count}×" for day, count in sorted(days.items())) or "-"),
            f"- axis: {_fate_split(known, 'axis_key')}",
            f"- draft fate: {_fate_split(known, 'human_outcome')}",
            f"- member: {_fate_split(known, 'member')}"]

    out += ["", f"## Runs ({len(run_ids)}) — one representative excerpt each {REDUCED}"]
    for run_id in run_ids:
        run = runs_by_id.get(run_id) or {}
        out.append(
            f"- {run.get('day') or '?'} {hhmm(run.get('created_at'), tz)} `{run_id[:8]}` · "
            f"{run.get('axis_key') or '-'} · {privacy(run.get('topic'), 70) or '-'} · "
            f"{run.get('human_outcome') or run.get('outcome') or '-'}"
            + (f" · excl:{run.get('excluded_reason')}" if run_id in excluded else "")
        )
        example = examples.get(run_id) or run.get("error")
        if example:
            for line in str(example).splitlines()[:6]:
                out.append(f"    | {privacy(line, 220)}")
        else:
            out.append("    | (no excerpt recovered — trace not cached or step not identified)")

    # Every URL, not the first six: a truncated list silently loses runs you were told to check.
    urls = [str(runs_by_id.get(r, {}).get("run_url") or "") for r in run_ids]
    urls = [u for u in urls if u] or [str(u) for u in (cluster.get("run_urls") or [])]
    if urls:
        out += ["", f"## Run URLs ({len(urls)} of {len(run_ids)} runs)", *[f"- {url}" for url in urls]]
    return "\n".join(out) + "\n"


def _placeholder_excerpt(text: str, width: int = 260) -> str:
    """The sentence a `[[✏️ …]]` deferral actually shipped — the reason the cluster exists."""
    marker = text.find("[[✏️")
    if marker < 0:
        return ""
    start = max(0, marker - width // 2)
    return ("…" if start else "") + text[start:marker + width] + "…"


def cluster_examples(rc: Rc, evidence: dict[str, Any], cluster: dict[str, Any]) -> dict[str, str]:
    """One representative excerpt per run, chosen by what the cluster is about.

    A list of 17 run ids with no content is what forces a second, manual drill; the whole point of
    a cluster page is that you can judge it without opening each run.
    """
    runs_by_id = {str(r.get("run_id")): r for r in (evidence.get("runs") or [])}
    actions_by_run: dict[str, list[dict[str, Any]]] = {}
    for action in evidence.get("actions") or []:
        actions_by_run.setdefault(str(action.get("run_id")), []).append(action)
    signature = str(cluster.get("signature") or "")
    kind = str(cluster.get("kind") or "")
    out: dict[str, str] = {}

    for run_id in (cluster.get("run_ids") or [])[:MAX_CLUSTER_RUNS]:
        run = runs_by_id.get(run_id) or {}
        member = run.get("member") or (evidence.get("projects") or ["?"])[0]
        base = ["--project", str(member), "--scope", "project"]

        if kind in ("action_failure", "action_stale"):
            rows = [a for a in actions_by_run.get(run_id, [])
                    if str(a.get("action_id") or "") in signature] or actions_by_run.get(run_id, [])
            message = next((str(a.get("error_message") or a.get("error_class") or "")
                            for a in rows if a.get("error_message") or a.get("error_class")), "")
            params = next((json.dumps(a.get("params"), ensure_ascii=False, default=str)
                           for a in rows if a.get("params")), "")
            if message or params:
                out[run_id] = "\n".join(x for x in (message, f"params: {clip(params, 300)}" if params else "") if x)
                continue

        if "draft_placeholder" in signature or "deferral" in signature:
            draft = str((rc.json(*base, "run", "show", run_id) or {}).get("draft_markdown") or "")
            excerpt = _placeholder_excerpt(draft)
            if excerpt:
                out[run_id] = excerpt
                continue

        if run.get("error"):
            out[run_id] = str(run["error"])
            continue

        records = rc.jsonl(*base, "run", "trace", run_id, "--stream", timeout=600)
        failing = [s for s in summarise_steps(records[1:] if records else [])
                   if s["exit_code"] not in (0, None)]
        if not failing:
            continue
        # A run usually fails more than once; pick the step this cluster is actually about (the
        # script named in the signature) and, among those, one that actually said something —
        # an empty-stderr match yields a useless excerpt while the real traceback sits next to it.
        named = [s for s in failing if first_token(s["command"]) in signature]
        chosen = next((s for s in named if s["stderr"]),
                      next((s for s in failing if s["stderr"]), (named or failing)[-1]))
        stderr = str(chosen["stderr"] or "")
        tail = "\n".join(stderr.splitlines()[-4:]) if stderr else ""
        out[run_id] = (f"$ {chosen['command']}\n" + (tail or chosen["stdout"]
                       or f"exit {chosen['exit_code']}")).strip()
    return out


# ------------------------------------------------------------ delta / feedback


def render_delta(run_id: str, evidence: dict[str, Any], tz) -> str:
    """Proposed vs sent, in full. The single richest signal in the report and grep-only until now."""
    deltas = [d for d in (evidence.get("deltas") or [])
              if str(d.get("related_run_id") or "").startswith(run_id)]
    runs_by_id = {str(r.get("run_id")): r for r in (evidence.get("runs") or [])}
    run = next((r for k, r in runs_by_id.items() if k.startswith(run_id)), {})
    out = [f"# Human edit · run `{run_id[:8]}`",
           f"{run.get('day') or '?'} · {run.get('member') or '?'} · {run.get('axis_key') or '-'} · "
           f"{privacy(run.get('topic'), 100) or '(no topic)'}", ""]
    if not deltas:
        out.append("No delta rows for this run in evidence.json (the reviewer sent it unchanged, "
                   "the edit was dropped as cosmetic, or the delta arrived after collection).")
        return "\n".join(out) + "\n"
    for delta in deltas:
        out += [
            f"## {delta.get('day')} · {'shadow' if delta.get('shadow') else 'live'} · "
            f"{delta.get('delta_category') or '-'} · sim {delta.get('similarity')} · "
            f"verdict {delta.get('shadow_verdict') or '-'}",
            f"why: {privacy(delta.get('delta_description'), 1200) or '-'}",
            "",
            f"### Proposed (the agent) {REDUCED}",
            "```", clip(privacy(delta.get("proposed_body"), BODY_CHARS), BODY_CHARS), "```",
            f"### Sent (the human) {REDUCED}",
            "```",
            clip(privacy(delta.get("sent_body_clean") or delta.get("sent_body"), BODY_CHARS), BODY_CHARS),
            "```",
        ]
    return "\n".join(out) + "\n"


def render_feedback(evidence: dict[str, Any], tz) -> str:
    """Every score and comment in the window, joined to the run that carries it."""
    rows = evidence.get("feedback") or []
    meta = {str(r.get("run_id")): r for r in (evidence.get("runs") or [])}
    focus = str((evidence.get("window") or {}).get("focus") or "")

    def when(item: dict[str, Any]) -> str:
        moment = item.get("comment_set_at") or item.get("score_set_at")
        return stamp(moment, tz) if moment else "?"

    out = [f"# Feedback · {evidence.get('report_id')} · window ending {focus}",
           f"{len(rows)} row(s). Comments are near-100% actionable; scores are ambiguous. {REDUCED}",
           ""]
    for item in sorted(rows, key=when, reverse=True):
        run = meta.get(str(item.get("run_id"))) or {}
        out.append(
            f"- {when(item)} `{run8(item.get('run_id'))}` · "
            f"{run.get('member') or item.get('member') or '?'}/{run.get('kind') or '?'} · "
            f"{run.get('axis_key') or '-'} · score {item.get('score', '-')}"
        )
        out.append(f"    - topic: {privacy(run.get('topic'), 120) or '-'}")
        out.append(f"    - comment: {privacy(item.get('comment'), 800) or '(no comment)'}")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------ main


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "cluster"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", help="run uuid or run8, comma-separated for a batch")
    target.add_argument("--cluster", help="cluster signature (or a prefix) or a title substring")
    target.add_argument("--delta", help="run uuid or run8: proposed vs sent body in full")
    target.add_argument("--feedback", action="store_true",
                        help="every score/comment in the window, joined to its run")
    parser.add_argument("--date", help="report day to read/write (default: the latest collected)")
    parser.add_argument("--overlay", help="shared overlay directory (default: brain _internal/fleet-report)")
    parser.add_argument("--report-id")
    parser.add_argument("--refresh", action="store_true", help="ignore the raw cache")
    parser.add_argument("--offline", action="store_true", help="raw cache only, never call rc")
    args = parser.parse_args()

    brain_root = find_brain_root()
    overlay = load_overlay(brain_root, args.overlay)
    tz = tzinfo(str(overlay.get("timezone", DEFAULT_TZ)))
    report_id = args.report_id or str(overlay.get("report_id") or project_name(brain_root))
    out_dir = report_dir(brain_root, report_id, args.date)
    evidence = load_evidence(out_dir)
    if not evidence:
        print(f"warning: no evidence.json in {out_dir} — running against rc only", file=sys.stderr)
    learn_non_names(
        [str(t.get("slug") or "") for rows in (evidence.get("tenants") or {}).values() for t in rows]
        + [str(t.get("name") or "") for rows in (evidence.get("tenants") or {}).values() for t in rows]
    )
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "details").mkdir(parents=True, exist_ok=True)
    rc = Rc(raw_dir=out_dir / "raw", cwd=brain_root, refresh=args.refresh, offline=args.offline)

    if args.feedback:
        path = out_dir / "details" / "feedback.md"
        path.write_text(render_feedback(evidence, tz), encoding="utf-8")
        print(f"{len(evidence.get('feedback') or [])} feedback row(s) → {path}")
        return 0

    if args.delta:
        needle = args.delta.strip()
        path = out_dir / "details" / f"delta-{needle[:8]}.md"
        path.write_text(render_delta(needle, evidence, tz), encoding="utf-8")
        print(f"delta {needle[:8]} → {path}")
        return 0

    if args.cluster:
        needle = args.cluster.strip()
        low = needle.lower()
        # The signature is the machine key; the title is what the digest shows. Match either, so a
        # copied (privacy-mangled, truncated) title still lands on the right cluster.
        matches = [c for c in (evidence.get("clusters") or [])
                   if str(c.get("signature", "")).startswith(needle)
                   or low in str(c.get("signature", "")).lower()
                   or low in str(c.get("title", "")).lower()]
        if not matches:
            print(f"no cluster matching {needle!r} in {out_dir / 'evidence.json'} "
                  "(match is on the `sig=` key from digest.md, or a title substring)", file=sys.stderr)
            return 1
        cluster = max(matches, key=lambda c: (c.get("focus_count", 0), c.get("context_count", 0)))
        seen = len(rc.errors)
        examples = cluster_examples(rc, evidence, cluster)
        path = out_dir / "details" / f"cluster-{slug(cluster['signature'])}.md"
        path.write_text(
            render_cluster(cluster, evidence, examples, tz, rc_failures(rc, seen)), encoding="utf-8")
        print(f"{cluster['signature']} · {len(cluster.get('run_ids') or [])} runs · "
              f"{len(examples)} excerpts · {rc.calls} rc calls · {len(rc.errors) - seen} failures → {path}")
        return 0

    needles = [n.strip() for n in str(args.run).split(",") if n.strip()]
    written: list[Path] = []
    for needle in needles:
        path = drill_one(rc, evidence, overlay, brain_root, out_dir, tz, needle)
        if path is None:
            return 1
        written.append(path)
    if len(written) > 1:
        print(f"{len(written)} runs · {rc.calls} rc calls → {out_dir / 'details'}")
    return 0


def drill_one(rc: Rc, evidence: dict[str, Any], overlay, brain_root: Path, out_dir: Path, tz,
              needle: str) -> Path | None:
    seen = len(rc.errors)
    run = resolve_run(evidence, needle)
    run_id = str(run.get("run_id"))
    member = str(run.get("member") or (evidence.get("projects") or [project_name(brain_root)])[0])
    base = ["--project", member, "--scope", "project"]
    data = fetch_run(rc, base, run_id)
    if not data["show"] and not data["trace"]:
        print(f"run {run_id} not found (rc errors: {rc.errors})", file=sys.stderr)
        return None

    run_id = str(data["show"].get("run_id") or data["header"].get("run_id") or run_id)
    run["run_id"] = run_id

    # Persist original source text for offline quote validation (never the privacy-reduced Markdown).
    source_path = out_dir / "details" / f"evidence-{run_id}.json"
    source_path.write_text(json.dumps({
        "question": data["header"].get("question") or data["show"].get("question"),
        "proposed": data["header"].get("draft") or data["show"].get("draft_markdown"),
    }, ensure_ascii=False), encoding="utf-8")
    from evidence_excerpts import evidence_excerpts
    excerpt_path = out_dir / "details" / f"excerpts-{run_id}.json"
    excerpt_path.write_text(json.dumps(evidence_excerpts(run_id, evidence=evidence, out_dir=out_dir), ensure_ascii=False, indent=2), encoding="utf-8")

    tenant = str(data["header"].get("tenant") or run.get("tenant") or "")
    ctx = {
        "run_id": run_id,
        "project": member,
        "tenant": tenant,
        "tz": tz,
        "rc": rc,
        "rc_args": ["--project", member] + (["--tenant", tenant] if tenant else ["--scope", "project"]),
        "show": data["show"],
        "thread": data["thread"],
        "trace": data["trace"],
        "steps": data["steps"],
        "actions": data["actions"],
        "deltas": [d for d in (evidence.get("deltas") or []) if str(d.get("related_run_id")) == run_id],
        "feedback": [f for f in (evidence.get("feedback") or []) if str(f.get("run_id")) == run_id],
        "brain_root": brain_root,
        "privacy": privacy,
        "fr": sys.modules["fr_common"],
    }
    follow_up = overlay.call("drill", {**run, **data["show"]}, ctx)
    if overlay.problems:
        print("overlay problems: " + "; ".join(overlay.problems), file=sys.stderr)

    markdown = render_run(run, data, evidence, tz, str(follow_up) if follow_up else None,
                          rc_failures(rc, seen))
    path = out_dir / "details" / f"run-{run_id[:8]}.md"
    path.write_text(markdown, encoding="utf-8")
    print(f"{run_id} · {len(data['steps'])} steps · {len(data['actions'])} actions · "
          f"follow-up {'yes' if follow_up else 'no'} · {rc.calls} rc calls · "
          f"{len(rc.errors) - seen} failures · {markdown.count(chr(10))} lines → {path}")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
