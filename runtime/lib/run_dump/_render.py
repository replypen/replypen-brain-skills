"""Implementation of the shared run-dump renderer. See the package docstring for the bundle contract.

The renderer is data-source agnostic: public `rp run trace <id>` hands it a normalized **bundle dict**,
and this module owns only formatting/decoration.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime
from typing import Iterable

PUBLIC_BASE_URL = "https://app.replypen.com"
PROJECTION_BRANCH_KEYS = (
    "newpatient_method",
    "existingpatient_method",
    "reschedule_method",
    "booking_hygienist_dentist_interaction",
)


# ---------------------------------------------------------------- shared formatting helpers


def _egr_blocked(e: dict) -> bool:
    """True if an egress entry represents a blocked call, across both shapes: the operator path's raw
    `egress_log` rows (`decision == "block"`) and the public-API `/trace` bundle's per-host aggregate
    (`blocked == true`)."""
    return e.get("decision") == "block" or e.get("blocked") is True


def _egr_count(e: dict) -> int:
    """Call count for an entry: explicit on the aggregated API shape, one per raw operator row."""
    return int(e.get("count") or 1)


def _fence(text: str, lang: str = "") -> str:
    """Fence content, bumping the backtick run so embedded ``` can't break out."""
    body = (text or "").rstrip("\n")
    if not body:
        return "_(empty)_"
    longest = max((len(m) for m in _backtick_runs(body)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{body}\n{ticks}"


def _backtick_runs(s: str):
    run = 0
    for ch in s:
        if ch == "`":
            run += 1
        elif run:
            yield "`" * run
            run = 0
    if run:
        yield "`" * run


def _dur(ms) -> str:
    ms = int(ms or 0)
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m{(ms % 60_000) // 1000:02d}s"


def _gist(text: str, limit: int = 100) -> str:
    """First sentence-ish of a reasoning blob, single line, truncated."""
    line = " ".join((text or "").split())
    for sep in (". ", "; "):
        idx = line.find(sep)
        if 0 < idx < limit:
            return line[: idx + 1]
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _cell(text: str, limit: int = 70) -> str:
    """One markdown table cell: single line, pipes escaped, truncated."""
    line = " ".join((text or "").split()).replace("|", "\\|")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _as_dt(v):
    """A timestamp as a datetime, accepting either a datetime (operator path) or an ISO string (the
    JSON API bundle). Returns it unchanged on a non-ISO string so str() still prints something."""
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


def _tenant_settings_record(raw) -> tuple[dict, str | None]:
    """Return (record, error). The API/operator bundle may carry tenant_settings as the raw JSON string
    stamped on the run row, or as a decoded dict in synthetic tests/future callers."""
    if raw in (None, ""):
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, str(exc)
        if isinstance(data, dict):
            return data, None
        return {}, "tenant_settings JSON is not an object"
    return {}, f"unexpected tenant_settings type {type(raw).__name__}"


def _projection_input_lines(run: dict) -> list[str]:
    """Concise, non-exhaustive projection provenance for the index. The full operator-scoped snapshot
    stays in JSONL; the index surfaces just enough to answer "what /brain did prod mount?"."""
    if not (run.get("brain_resolved") or run.get("tenant") or run.get("tenant_settings")):
        return []

    lines = ["", "## Projection inputs", ""]
    if run.get("brain_resolved"):
        lines.append(f"- **Brain resolved:** `{run['brain_resolved']}`")
    if run.get("tenant"):
        lines.append(f"- **Tenant:** `{run['tenant']}`")

    rec, err = _tenant_settings_record(run.get("tenant_settings"))
    if err:
        lines.append(f"- **Tenant settings:** unparseable snapshot ({_cell(err, 80)}); see JSONL header")
        return lines
    if not rec:
        return lines

    meta = []
    for key in ("source", "synced_at", "version"):
        if rec.get(key):
            meta.append(f"{key} `{rec[key]}`")
    if meta:
        lines.append("- **Tenant settings:** " + " · ".join(meta))

    settings = rec.get("settings") if isinstance(rec.get("settings"), dict) else {}
    selectors = [(k, settings[k]) for k in PROJECTION_BRANCH_KEYS if k in settings]
    if selectors:
        rendered = " · ".join(f"{k}=`{v}`" for k, v in selectors)
        lines.append(f"- **Branch selectors:** {rendered}")
    else:
        lines.append("- **Branch selectors:** none present in snapshot")
    return lines


def _pii_mask_lines(run: dict) -> list[str]:
    masks = run.get("pii_masks") or run.get("shielded_data") or run.get("data_shielded")
    if not isinstance(masks, list) or not masks:
        return []
    lines = ["", "## Data shielded from the model", ""]
    for item in masks[:20]:
        if not isinstance(item, dict):
            lines.append(f"- `{_cell(str(item), 120)}`")
            continue
        label = item.get("label") or item.get("token") or item.get("kind") or "masked"
        source = item.get("source") or item.get("table") or item.get("column") or ""
        count = item.get("count") or item.get("rows") or item.get("occurrences")
        bits = [f"`{label}`"]
        if source:
            bits.append(str(source))
        if count is not None:
            bits.append(f"{count} occurrence(s)")
        lines.append("- " + " · ".join(bits))
    if len(masks) > 20:
        lines.append(f"- ... {len(masks) - 20} more in JSONL header")
    return lines


# Nearly every workspace command opens with `cd <dir> && ` — strip it wherever we classify or
# gist a command, so the anchored patterns below see the command that actually does the work.
_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*&&\s*")

# The wrappers every workspace python call repeats — noise in a 100-char glimpse of the actual code.
_PY_UNWRAP = [
    _CD_PREFIX,
    re.compile(r"^\s*(?:uv run\s+)?python3?\s+-c\s+[\"']"),       # python -c "
]
_PY_SKIP_LINE = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|sys\.path\.insert)")


def _code_gist(cmd: str, limit: int = 100) -> str:
    """First chars of the code that matters: strip the cd/python -c wrapper, import/sys.path
    boilerplate, and a trailing closing quote — the agent's narration comments stay."""
    for rx in _PY_UNWRAP:
        cmd = rx.sub("", cmd, count=1)
    cmd = re.sub(r"[\"']\s*$", "", cmd)
    kept = [ln.strip() for ln in cmd.splitlines()
            if ln.strip() and not _PY_SKIP_LINE.match(ln)]
    return _cell("; ".join(kept), limit)


_LABEL_RULES = [
    # An action preflight is read-only and predicts what an action would do — its own intent, ahead of
    # the generic "python", so the timeline shows an action being validated in-loop.
    (re.compile(r"actions/[A-Za-z0-9._\-]+/preflight\.(?:py|sh|rb)\b"), "check action"),
    (re.compile(r"\blib\.db\b|\bpsql\b|\bSELECT\b", re.I), "db query"),
    (re.compile(r"\bstripe\b", re.I), "stripe"),
    (re.compile(r"\bcloudwatch\b|\baws logs\b|\blib\.logs\b", re.I), "cloudwatch"),
    (re.compile(r"\bcurl\b|\blib\.http\b|\bwget\b|\brequests\b"), "http"),
    (re.compile(r"^\s*(cat|head|tail|less|sed -n)\b"), "read file"),
    (re.compile(r"^\s*(rg|grep|find|ls|tree|fd)\b"), "search files"),
    (re.compile(r"\bpython3?\b|<<\s*'?EOF"), "python"),
]


def _label(cmd: str) -> str:
    cmd = _CD_PREFIX.sub("", cmd or "", count=1)  # `cd /mirrors/x && rg …` is an rg, not a "cd"
    for rx, label in _LABEL_RULES:
        if rx.search(cmd):
            return label
    # The agent often narrates with leading `# …` comment lines — skip them for the fallback label.
    for line in (cmd or "").splitlines():
        words = line.strip().split()
        if words and not words[0].startswith("#"):
            return words[0][:20]
    return "bash"


# The standing systemPromptBody (internal/agent/prompt.go) is identical for every run — pure
# boilerplate in the index. Detected by its fixed first/last sentences and collapsed to a marker so the
# index shows only the DYNAMIC parts at a glance: the per-mode preamble before it, and the capability
# paragraphs + action catalog after it. The full text stays in the JSONL header.
_BODY_START = "You are an autonomous agent with exactly two tools"
_BODY_END = "Never end your turn without it."


def _trim_system_prompt(sp: str) -> str:
    """Elide the static systemPromptBody span, keeping the preamble + gated paragraphs + catalog in
    full. Falls back to the verbatim prompt if the markers aren't found (the constant changed)."""
    i = sp.find(_BODY_START)
    if i == -1:
        return sp
    j = sp.find(_BODY_END, i)
    if j == -1:
        return sp
    j += len(_BODY_END)
    return (sp[:i].rstrip()
            + f"\n\n[+ standing systemPromptBody, {j - i} chars — see JSONL]\n\n"
            + sp[j:].lstrip()).strip()


# ---------------------------------------------------------------- event decoration

# /brain, /mirrors, /kb paths mentioned in commands — the bridge to "what did the agent read".
_PATH_RX = re.compile(r"(?<![\w-])(/(?:brain|mirrors|kb)/[A-Za-z0-9._/@%+-]*[A-Za-z0-9_])")


def decorate(events: list[dict]) -> list[dict]:
    """Add display fields in place: disp (P1…/1…), label, command, gist. Returns the same list.

    Deterministic and idempotent — `render_index`/`emit_jsonl` call it themselves, so a caller need
    not (re-running it just recomputes the same values)."""
    p = n = 0
    in_grounding = True
    for e in events:
        # The grounding step records in a negative seq band, but the sign alone is not the signal:
        # runs recorded before the MaxRunEventSeq fix seeded the main loop's seq from the grounding
        # band's max, so their main-loop rows are negative too. The reliable boundary is the pass's
        # terminal row (submit_selection / grounding_aborted) — everything after it is the main loop.
        grounding = in_grounding and e["seq"] < 0
        if not grounding or e["tool"] in ("submit_selection", "grounding_aborted"):
            in_grounding = False
        if grounding:
            p += 1
            e["disp"] = f"P{p}"
        else:
            n += 1
            e["disp"] = str(n)
        e["grounding"] = grounding
        args = e.get("args") or {}
        if e["tool"] == "bash":
            # Bundle carries the bash input either as the top-level `command` (the /trace API shape) or
            # inside `args` (the operator DB shape) — accept both.
            e["command"] = args.get("command") or e.get("command") or ""
            e["label"] = _label(e["command"])
        elif e["tool"] == "reply":
            e["command"] = " ".join(
                f"{k}={'yes' if v else 'no'}" if isinstance(v, bool)
                else f"{k}={len(v)}" if isinstance(v, list)
                else f"{k}={v}"
                for k, v in args.items()
            )
            e["label"] = "reply"
        elif e["tool"] == "grounding_aborted":  # the grounding step died — args carry the reason
            e["command"] = args.get("aborted", "")
            e["label"] = "aborted"
        else:  # submit_selection — the grounding step's terminal row
            sel = args.get("selected") or []
            e["command"] = "skip (judged trivial)" if args.get("skip") else f"selected {len(sel)} docs"
            e["label"] = "submit_selection"
        e["gist"] = _gist(e.get("reasoning") or "")
    return events


def _selected_docs(events: list[dict]) -> dict | None:
    """{'skip': bool, 'summary': str, 'docs': [(ref, reason)]} from the submit_selection event; None =
    no such event (the pass never reached its terminal tool — budget hit or grounding_aborted)."""
    for e in events:
        if e["tool"] == "submit_selection":
            args = e.get("args") or {}
            docs = []
            for s in args.get("selected") or []:
                ref = s.get("path", "")
                if s.get("start") or s.get("end"):
                    ref = f"{ref}:{s.get('start', 0)}-{s.get('end', 0)}"
                docs.append((ref, s.get("reason", "")))
            return {"skip": bool(args.get("skip")), "summary": (args.get("summary") or "").strip(), "docs": docs}
    return None


# grep-family exit 1 means NO MATCH, not failure — flagging it as an error is noise.
_GREP_RX = re.compile(r"^\s*(rg|grep|egrep|fgrep)\b")


def _benign_grep_miss(e: dict) -> bool:
    return (e.get("exit_code") == 1 and e.get("status") in ("ok", "error")
            and not (e.get("stderr") or "").strip()
            and bool(_GREP_RX.match(_CD_PREFIX.sub("", e.get("command") or "", count=1))))


def _has_result(run: dict) -> bool:
    """True when the run produced a callback (the bundle carries draft / notes / actions / metadata).
    The bundle pre-flattens the old `result` object, so 'no callback' = none of those present."""
    return bool(run.get("draft") or run.get("notes") or _proposed_actions(run) or run.get("metadata"))


def flags(bundle: dict) -> list[str]:
    """Attention-directing anomalies: where 'why did he do that' questions are likely to live.

    Public entry over the bundle; calls `decorate` itself so a standalone `flags(bundle)` works."""
    run = bundle["run"]
    events = decorate(bundle["events"])
    egress = run.get("egress") or []
    out: list[str] = []
    if run.get("error"):
        out.append(f"run errored: `{run['error']}`")
    if not _has_result(run):
        out.append("no stored callback — the run never produced one")
    if any(e["grounding"] for e in events) and _selected_docs(events) is None:
        aborted = next((e for e in events if e["tool"] == "grounding_aborted"), None)
        out.append(
            f"grounding step aborted: `{aborted['command']}` — its work was discarded" if aborted
            else "grounding step ran but never reached submit_selection — "
                 "its work was discarded (main loop started unseeded)")

    for e in events:
        if _benign_grep_miss(e):
            pass
        elif e["status"] != "ok":
            out.append(f"[{e['disp']}] {e['status']}" + (f" (exit {e['exit_code']})" if e["exit_code"] else ""))
        elif e["exit_code"]:
            out.append(f"[{e['disp']}] exit {e['exit_code']}")
        if "EGRESS_BLOCKED" in (e.get("stdout") or "") + (e.get("stderr") or ""):
            out.append(f"[{e['disp']}] output mentions EGRESS_BLOCKED")
        if len(e.get("stdout") or "") > 20_000:
            out.append(f"[{e['disp']}] large stdout ({len(e['stdout']) // 1024} KB)")

    # Repeated identical commands — possible flailing / retry loop.
    seen: dict[str, list[str]] = {}
    for e in events:
        if e["tool"] == "bash" and e["command"].strip():
            seen.setdefault(" ".join(e["command"].split()), []).append(e["disp"])
    for cmd, steps in seen.items():
        if len(steps) > 1:
            out.append(f"[{', '.join(steps)}] identical command ran {len(steps)}×: `{_cell(cmd, 60)}`")

    # Slow turns: a turn way above the median — where the run's wall clock actually went. The 10s
    # floor keeps a fast run's jitter (2ms vs 20ms) from flagging.
    durations = [e["duration_ms"] for e in events if e.get("duration_ms")]
    if len(durations) >= 4:
        median = statistics.median(durations)
        for e in events:
            d = e.get("duration_ms")
            if d and median and d > 4 * median and d > 10_000:
                out.append(f"[{e['disp']}] slow turn {_dur(d)} ({d / median:.0f}× median turn)")

    for e in (e for e in egress if _egr_blocked(e)):
        # Operator rows carry port + timestamp; the aggregated API shape carries neither — render what
        # the entry has so both sources produce a sensible (if not byte-identical) blocked line.
        loc = f"{e['host']}:{e.get('port', '')}" if "port" in e else e.get("host", "?")
        at = e.get("at")
        when = f" at {_as_dt(at)}" if at else (f" ({_egr_count(e)}× blocked)" if e.get("count") else "")
        out.append(f"egress BLOCKED: `{loc}`{when}")
    return out


def files_read(events: list[dict]) -> list[str]:
    """Sorted FILE paths only — directory mentions (ls/find targets) are noise here."""
    files: set[str] = set()
    for e in events:
        if e["tool"] != "bash":
            continue
        for m in _PATH_RX.findall(e["command"]):
            if "." in m.rsplit("/", 1)[-1]:  # no extension → almost certainly a directory
                files.add(m)
    return sorted(files)


def _run_url(run: dict) -> str:
    """The human run-page link (merged trace + feedback + retry): prefer the one minted at callback
    time (metadata.run_url / metadata.trace_url); else mint locally when RUN_SIGNING_KEY is in the env
    (same recipe as internal/runpage/token.go). In the Local Brain Work context the key is absent → ""."""
    meta = run.get("metadata") or {}
    if meta.get("run_url"):
        return meta["run_url"]
    if meta.get("trace_url"):
        return meta["trace_url"]
    import base64, hashlib, hmac, os

    key = os.environ.get("RUN_SIGNING_KEY")
    if not key:
        return ""
    tok = base64.urlsafe_b64encode(
        hmac.new(key.encode(), str(run["run_id"]).encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    base = os.environ.get("PUBLIC_BASE_URL", PUBLIC_BASE_URL).rstrip("/")
    return f"{base}/runs/{run['run_id']}?t={tok}"


def _jsonl_name(run: dict) -> str:
    """The drill-down file's name, derived from the run — `<run8>-<project>.jsonl`."""
    return f"{str(run['run_id'])[:8]}-{run['project']}.jsonl"


# ---------------------------------------------------------------- index (the concise file)


def _attachment_summary(events: list[dict]) -> str | None:
    """Byte-free terminal reply attachment result; None means the run has no reply event."""
    reply = next((e for e in reversed(events) if e.get("tool") == "reply"), None)
    if reply is None:
        return None
    attachments = (reply.get("args") or {}).get("attachments") or []
    shipped = sum(a.get("status") == "shipped" for a in attachments if isinstance(a, dict))
    details = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        state = a.get("status") or "unknown"
        if a.get("drop_reason"):
            state += f": {a['drop_reason']}"
        details.append(
            f"`{a.get('filename') or '?'}` · {int(a.get('size_bytes') or 0)} bytes · "
            f"`{a.get('mime_type') or 'unknown MIME'}` · `{a.get('path') or '?'}` · {state}"
        )
    summary = f"{len(attachments)} declared · {shipped} shipped · {len(attachments) - shipped} dropped"
    return summary + (" — " + "; ".join(details) if details else "")


def render_index(bundle: dict) -> str:
    """The markdown index for a run bundle. Calls `decorate` itself, so a bare bundle is enough."""
    run = bundle["run"]
    events = decorate(bundle["events"])
    egress = run.get("egress") or []
    jsonl_name = _jsonl_name(run)

    meta = run.get("metadata") or {}
    main = [e for e in events if not e["grounding"]]
    pre = [e for e in events if e["grounding"]]
    model = meta.get("model") or next((e["model"] for e in events if e.get("model")), "")
    blocked = sum(_egr_count(e) for e in egress if _egr_blocked(e))
    created, finished = _as_dt(run.get("created_at")), _as_dt(run.get("finished_at"))
    dur = ""
    if finished and created and not isinstance(finished, str) and not isinstance(created, str):
        dur = _dur((finished - created).total_seconds() * 1000)
    run_url = _run_url(run)

    L = [
        f"# Run {str(run['run_id'])[:8]} — {run['project']} · {run['status']} · {run['kind']}",
        "",
        f"- **Run ID:** `{run['run_id']}`",
    ]
    # A test run (dev-ref / explicit test trigger) must be visibly distinguishable from a main run.
    if run.get("brain_ref") or run.get("trigger") == "test":
        L.append(f"- **Test run** · brain_ref `{run.get('brain_ref') or '(main)'}` · "
                 f"trigger `{run.get('trigger') or '?'}` — side-effect-free (no callback, no journal push)")
    if run_url:
        L.append(f"- **Run page (human view):** {run_url}")
    L += [
        f"- **Thread / Session:** `{run.get('thread_id')}` / `{run.get('session_id')}`",
        f"- **Created / Finished:** {created} / {finished or '(unfinished)'}"
        + (f" · {dur}" if dur else ""),
        f"- **Model:** `{model or '?'}`",
        f"- **Steps:** {len(main)} main" + (f" + {len(pre)} grounding" if pre else "")
        + f" · **Egress:** {len(egress)}" + (f" ({blocked} blocked)" if blocked else ""),
    ]
    attachments = _attachment_summary(events)
    if attachments is not None:
        L.append(f"- **Attachments:** {attachments}")
    L += [f"- **Events (full, queryable):** `{jsonl_name}` — one JSON object per event; "
          f"jq it (see Drill down at the bottom).", ""]
    if run.get("topic"):
        L += ["## Topic", "", run["topic"], ""]
    L += _projection_input_lines(run)
    L += _pii_mask_lines(run)
    L += [
        "## Question",
        "",
        _fence(run.get("question") or "") if run.get("question") else "_(none captured)_",
        "",
        "## Outcome",
        "",
    ]
    L += _render_outcome(run)

    # Warm inputs (continued session): the two blocks we injected at run START — the digest fed to the
    # main loop + the seed fed to the grounding step. Operator-only context, never the sender's words.
    # Both '' on a cold/first turn, so the section appears only when something seeded the run.
    digest, seed = (run.get("warm_start_digest") or ""), (run.get("grounding_seed") or "")
    if digest or seed:
        L += ["", "## Warm start — continued session", ""]
        if digest:
            L += ["**Prior-investigations digest** (fed to the main loop):", "", _fence(digest), ""]
        if seed:
            L += ["**Grounding seed** (look-here / dead-ends, fed to the grounding step):", "", _fence(seed), ""]

    # Prior turns the brain was replayed this run: the conversation-so-far (verbatim webhook Messages)
    # and our earlier internal notes (verbatim webhook Notes, never sent to the sender). Both absent on
    # a first turn / a run predating the change, so the whole section appears only when one is present.
    prior_msgs, prior_notes = run.get("prior_messages") or [], run.get("prior_notes") or []
    if prior_msgs or prior_notes:
        L += ["", "## Prior context given to the brain", ""]
        if prior_msgs:
            L += ["### Conversation so far", ""]
            for m in prior_msgs:  # oldest-first as stored — do NOT re-sort
                direction = "inbound" if m.get("is_inbound") else "outbound"
                who = m.get("sender") or "?"
                when = m.get("sent_at") or "?"
                L += [f"- **{direction}** · {who} · {when}", "", _fence(m.get("body") or ""), ""]
        if prior_notes:
            L += ["### Earlier internal notes (never sent)", ""]
            for n in prior_notes:
                body = (n.get("body_markdown") or "").strip() or (n.get("body_html") or "")
                when = n.get("created_at") or "?"
                L += [f"- {when}", "", _fence(body), ""]

    # The exact main-loop system prompt, TRIMMED: the dynamic parts (per-mode preamble, the capability
    # paragraphs actually present, the per-run action catalog) in full; the large static
    # systemPromptBody collapsed to a marker. The untrimmed text is in the .jsonl run header.
    sysp = (run.get("system_prompt") or "").strip()
    if sysp:
        L += ["", "## System prompt (as sent to the engine)", "",
              "The literal system prompt the main loop received — per-mode preamble + the capability "
              "paragraphs present + this run's action catalog; the standing boilerplate is elided "
              "(untrimmed text in the .jsonl run header).",
              "", _fence(_trim_system_prompt(sysp)), ""]

    if pre:
        L += ["", f"## Grounding step — P1–P{len(pre)}", ""]
        sel = _selected_docs(events)
        if sel and sel.get("summary"):
            L += [f"> {sel['summary']}", ""]  # the model's one-line rationale (why these files — or why none)
        if sel is None:
            L.append("_no submit_selection recorded — the grounding step ended without selecting "
                     "(budget hit or aborted); the main loop started unseeded_")
        elif sel["docs"]:
            L.append("Selected docs (baked into the main prompt):")
            L += [f"- `{ref}` — {reason}" for ref, reason in sel["docs"]]
        elif sel["skip"]:
            L.append("_judged trivial — nothing pre-selected_")
        else:
            L.append("_submitted, but every selected ref failed host-side validation — "
                     "the main loop started unseeded_")
        # The grounding step is cheap retrieval — one concatenated command block reads faster than a
        # table. Failures keep their P-number so the Flags section stays cross-referencable.
        cmds = [e["command"] + (f"  # {e['disp']}: exit {e['exit_code']}" if e["exit_code"] else "")
                for e in pre if e["tool"] == "bash" and e["command"].strip()]
        if cmds:
            L += ["", _fence("\n".join(cmds), "sh")]

    # Search/read steps are dropped — "Files the run read" already summarizes them; the table
    # focuses on the code the agent executed and what came back.
    L += ["", "## Timeline — main-loop steps (search/read steps omitted — see Files the run read)", ""]
    main_rows = [e for e in events
                 if not e["grounding"] and e["label"] not in ("search files", "read file")]
    if main_rows:
        L += ["| # | label | code | exit | dur | output | reasoning gist |",
              "|---|---|---|---|---|---|---|"]
        for e in main_rows:
            # A failed step gets 3× the output budget — the traceback is what explains the retry.
            failed = e["exit_code"] != 0 or e["status"] != "ok"
            out = _cell(e.get("stdout") or e.get("stderr") or "", 300 if failed else 100)
            L.append(
                f"| {e['disp']} | {e['label']} | `{_code_gist(e['command'])}` | {e['exit_code']} "
                f"| {_dur(e['duration_ms'])} | {f'`{out}`' if out else ''} | {_cell(e['gist'], 90)} |"
            )
    else:
        L.append("_(no run_events recorded — the run died before its first tool call; check the app logs)_")

    L += ["", "## Flags", ""]
    flag_lines = flags(bundle)
    L += [f"- {f}" for f in flag_lines] or ["_(none)_"]

    files = files_read(events)
    if files:
        L += ["", "## Files the run read", ""]
        L += [f"- `{path}`" for path in files]

    if egress:
        L += ["", "## Egress (by host)", ""]
        hosts: dict[tuple, int] = {}
        for e in egress:
            dec = "block" if _egr_blocked(e) else (e.get("decision") or "allow")
            key = (e.get("host", "?"), dec)
            hosts[key] = hosts.get(key, 0) + _egr_count(e)
        L += [f"- `{host}` — {n}× {dec}" for (host, dec), n in sorted(hosts.items())]

    L += ["", f"## Drill down — `{jsonl_name}`", "",
          "One JSON object per line: line 1 is the run header, the rest are events keyed by "
          "`disp` (the `#` column above). Pull the FULL command/output/reasoning with jq:", "",
          _fence("\n".join([
              f'jq -r \'select(.disp=="23").command\' {jsonl_name}   # full code of step 23',
              f'jq -r \'select(.disp=="23").stdout\'  {jsonl_name}   # its output / traceback',
              f'jq -r \'select(.disp=="23").stdout | .[0:2000]\' {jsonl_name}   # windowed when flagged large',
              f'jq -r \'select(.exit_code != null and .exit_code != 0).disp\' {jsonl_name}   # failed steps',
              f'jq -r \'select(.command // "" | contains("invoice")).disp\' {jsonl_name}   # steps touching X (also .stdout)',
              f'jq -r \'select(.reasoning) | .disp + " " + .reasoning\' {jsonl_name}   # reasoning per step',
          ]), "sh")]

    L.append("")
    return "\n".join(L)


def _render_outcome(run: dict) -> list[str]:
    if not _has_result(run):
        return ["_(no stored callback — run errored or never produced one)_"]
    out = []
    body = run.get("draft") or ""
    if body:
        lines = body.strip().splitlines()
        gist = "\n".join(lines[:8]) + (f"\n… ({len(lines)} lines total — full text in the .jsonl run header)" if len(lines) > 8 else "")
        out += [f"**Draft** ({len(lines)} lines):", "", _fence(gist), ""]
    else:
        out += ["**Draft:** none", ""]
    for n in run.get("notes") or []:
        nbody = n.get("body") or ""
        key = f" `{n['key']}`" if n.get("key") else ""
        out += [f"**Note**{key}:", "", _fence(_gist(nbody, 400)), ""]
    actions = _proposed_actions(run)
    if actions:
        out += [f"**Proposed actions** ({len(actions)}):", ""]
        for action in actions:
            out.append(f"- {_action_summary(action)}")
        out.append("")
    return out


def _proposed_actions(run: dict) -> list[dict]:
    """The public API uses `proposed_actions`; tolerate the older callback-ish `actions` key too."""
    actions = run.get("proposed_actions")
    if actions is None:
        actions = run.get("actions")
    return actions if isinstance(actions, list) else []


def _action_summary(action: dict) -> str:
    if not isinstance(action, dict):
        return _cell(str(action), 240)
    name = next((action.get(k) for k in ("slug", "action_id", "id", "name") if action.get(k)), "action")
    parts = [f"`{name}`"]
    if action.get("status"):
        parts.append(str(action["status"]))
    if action.get("id") and action.get("id") != name:
        parts.append(f"id `{action['id']}`")
    params = action.get("params")
    if params is None:
        params = action.get("parameters")
    if isinstance(params, dict) and params:
        parts.append("params " + ", ".join(_param_bits(params)))
    elif params not in (None, "", {}):
        parts.append(f"params `{_cell(json.dumps(params, ensure_ascii=False, default=str), 180)}`")
    return " · ".join(parts)


def _param_bits(params: dict) -> list[str]:
    out = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            rendered = str(value)
        out.append(f"`{key}={_cell(rendered, 120)}`")
    return out


# ---------------------------------------------------------------- event log (the JSONL drill-down)


def _iso(dt):
    if dt is None:
        return None
    return dt.isoformat() if hasattr(dt, "isoformat") else dt


def emit_jsonl(bundle: dict) -> Iterable[str]:
    """Yield the JSONL lines (no trailing newline) for a run bundle: a `{type:"run"}` header (run
    metadata + draft/note bodies + egress) followed by one `{type:"event"}` line per tool call —
    every field FULL and untruncated, so jq can pull a step's whole command/output/reasoning. `disp`
    mirrors the index's `#` column (main steps `1,2,…`; grounding steps `P1,P2,…`). Filter on
    `.type` to keep the header out of event-space queries.

    Calls `decorate` itself; join the lines with "\\n" (+ a trailing newline) to write the file."""
    run = bundle["run"]
    events = decorate(bundle["events"])
    egress = run.get("egress") or []
    meta = run.get("metadata") or {}
    model = meta.get("model") or next((e["model"] for e in events if e.get("model")), "")
    header = {
        "type": "run",
        "run_id": str(run["run_id"]),
        "project": run["project"],
        "status": run["status"],
        "kind": run["kind"],
        # NEW vs the legacy operator dump: test-run provenance so a dev-ref run is self-describing.
        "trigger": run.get("trigger"),
        "brain_ref": run.get("brain_ref"),
        "brain_resolved": run.get("brain_resolved"),
        "tenant": run.get("tenant"),
        "tenant_settings": run.get("tenant_settings"),
        "error": run.get("error"),
        "thread_id": str(run["thread_id"]) if run.get("thread_id") else None,
        "session_id": run.get("session_id"),
        "topic": run.get("topic"),
        "question": run.get("question"),
        "warm_start_digest": run.get("warm_start_digest") or None,
        "grounding_seed": run.get("grounding_seed") or None,
        # The EXACT main-loop system prompt, untrimmed — the full-detail view. None on a run that
        # predates the column or died before the stamp.
        "system_prompt": run.get("system_prompt") or None,
        "created_at": _iso(_as_dt(run.get("created_at"))),
        "finished_at": _iso(_as_dt(run.get("finished_at"))),
        "model": model or None,
        "draft": run.get("draft") or None,
        "notes": [{"key": n.get("key"), "body": n.get("body") or ""} for n in run.get("notes") or []],
        "proposed_actions": run.get("proposed_actions") if "proposed_actions" in run else None,
        "pii_masks": run.get("pii_masks") if "pii_masks" in run else None,
        "metadata": meta or None,
        "egress": [{"host": g.get("host"), "port": g.get("port"), "scheme": g.get("scheme"),
                    "url": g.get("url"), "bytes_out": g.get("bytes_out"),
                    "decision": g.get("decision"), "at": _iso(_as_dt(g.get("at")))}
                   for g in egress],
    }
    yield json.dumps(header, ensure_ascii=False, default=str)
    for e in events:
        line = {
            "type": "event",
            "disp": e["disp"],
            "seq": e["seq"],
            "grounding": e["grounding"],
            "tool": e["tool"],
            "label": e["label"],
            "command": e["command"],          # raw, unwrapped (bash: verbatim; reply/etc: arg summary)
            "stdout": e.get("stdout") or None,
            "stderr": e.get("stderr") or None,
            "exit_code": e.get("exit_code"),
            "status": e.get("status"),
            "duration_ms": e.get("duration_ms"),
            "at": _iso(_as_dt(e.get("at"))),
            "reasoning": e.get("reasoning") or None,
            "model": e.get("model"),
        }
        # Bash's full input is `command`; the other tools carry their structured input in `args`
        # (reply's draft/note/journal, submit_selection's picked docs, grounding_aborted's reason).
        if e["tool"] != "bash":
            line["args"] = e.get("args") or {}
        yield json.dumps(line, ensure_ascii=False, default=str)
