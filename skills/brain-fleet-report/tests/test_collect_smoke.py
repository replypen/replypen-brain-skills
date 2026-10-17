"""Unit tests for the pure functions in fr_common/collect.

    uv run --with pytest pytest skills/brain-fleet-report/tests -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collect  # noqa: E402
import drill  # noqa: E402
import fr_common as fr  # noqa: E402

TZ = fr.tzinfo("Europe/Brussels")


# ------------------------------------------------------------------ window math


def test_context_is_four_workdays_and_monday_reaches_back_to_tuesday():
    friday = date(2026, 9, 4)
    assert fr.workdays_before(friday, 4) == [
        date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)
    ]
    monday = date(2026, 9, 7)
    assert fr.workdays_before(monday, 4) == [
        date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)
    ]


def test_day_bounds_are_dst_correct():
    summer = fr.day_bounds(date(2026, 7, 1), TZ)
    assert (summer[1] - summer[0]).total_seconds() == 24 * 3600
    assert summer[0].isoformat() == "2026-06-30T22:00:00+00:00"
    winter = fr.day_bounds(date(2026, 12, 1), TZ)
    assert winter[0].isoformat() == "2026-11-30T23:00:00+00:00"
    # the day the clocks go back is 25 hours long
    long_day = fr.day_bounds(date(2026, 10, 25), TZ)
    assert (long_day[1] - long_day[0]).total_seconds() == 25 * 3600


def test_local_day_uses_the_project_timezone():
    # 22:30 UTC is already the next day in Brussels (summer time)
    assert fr.local_day("2026-09-02T22:30:00Z", TZ) == date(2026, 9, 3)


# --------------------------------------------------------------- bash clustering


def test_bash_buckets():
    assert fr.bash_bucket("search_availability.py --from x", -1, "") == "timeout"
    assert fr.bash_bucket("cat /brain/AGENTS.md", 64, "error: already in your context") == "guard"
    assert fr.bash_bucket(
        "python x.py --weekdays vrijdag", 2, "usage: x.py\nx.py: error: unrecognized arguments: --weekdays"
    ) == "usage"
    assert fr.bash_bucket("psql", 1, 'ERROR: column "activity_name" does not exist') == "sql"
    assert fr.bash_bucket("python -m lib.api get", 1, "Traceback (most recent call last):\n  File") == "traceback"
    assert fr.bash_bucket("cat /skills/agenda/SKILL.md", 1, "cat: /skills: No such file or directory") == "path_miss"
    assert fr.bash_bucket("rg needle /brain", 1, "") == "noise"


def test_cluster_key_is_stderr_last_line_plus_script_not_the_compound_command():
    left = fr.bash_cluster(
        "cd /tmp && python /brain/skills/records/scripts/avo_urls.py --q 'Ada D' | head",
        "boom\nERROR: column s.activity_name does not exist at 1234",
    )
    right = fr.bash_cluster(
        "python /brain/skills/records/scripts/avo_urls.py --q 'Bob X'",
        'ERROR: column s.activity_name does not exist at 9999',
    )
    assert left == right
    assert left.startswith("avo_urls.py · ")


def test_signature_normalisation_collapses_ids_paths_and_quotes():
    a = fr.normalise_error(
        'resolve symlink ".claude/skills": lstat /srv/brain/rc-worktrees/ab12cd34/.agents: no such file')
    b = fr.normalise_error(
        'resolve symlink ".claude/skills": lstat /srv/brain/rc-worktrees/ff99ee88/.agents: no such file')
    assert a == b
    assert fr.normalise_error("run 6129cd0e-1111-2222-3333-444444444444 failed") == \
        fr.normalise_error("run aaaaaaaa-1111-2222-3333-444444444444 failed")


def test_usage_error_extracts_the_flag_conflict():
    problem = fr.usage_error(
        "python search_availability.py --find-at-least 3 --exact-range",
        "usage: search_availability.py [-h]\nsearch_availability.py: error: argument --find-at-least: "
        "not allowed with argument --exact-range",
    )
    assert problem and problem["script"] == "search_availability.py"
    assert "--exact-range" in problem["detail"] and "--find-at-least" in problem["detail"]


# -------------------------------------------------------------- json / agent text


def test_json_objects_reads_two_documents_from_one_stdout():
    text = '{"mirrors": []}\n{"error":{"code":"USAGE","message":"unhealthy"}}\n'
    docs = fr.json_objects(text)
    assert len(docs) == 2 and docs[1]["error"]["code"] == "USAGE"


def test_parse_action_lines_recovers_error_message():
    line = (
        'ACTION id=4bf15b8f run_id="19247e8a" action_id="prepare_appointment_cancellation" '
        'status="failed" duration_ms=3375 params={"appointment_id":"Steven J. (10692742)"} '
        'error_class="ActionError" error_message="de live oorspronkelijke reden verschilt van de cache"'
    )
    rows = fr.parse_action_lines("noise\n" + line + "\n")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["error_message"].startswith("de live oorspronkelijke")
    assert row["params"]["appointment_id"].startswith("Steven")


# ------------------------------------------------------------------- privacy


def test_privacy_reduces_names_and_drops_emails():
    reduced = fr.privacy("Mail van Melissa Demuytere vanaf melissa@gmail.com over Ada Derammelaere")
    assert "Melissa D." in reduced and "Ada D." in reduced
    assert "@" not in reduced and "Demuytere" not in reduced


def test_name_reducer_handles_particles_and_three_word_names():
    # a capitalised particle belongs to the name, a lowercase one is the Dutch preposition
    assert fr.reduce_names("Scan van patiënt Van Nuffelen Greet") == "Scan van patiënt Van N. G."
    assert fr.reduce_names("Patiëntdocumenten van Molemans Bo") == "Patiëntdocumenten van Molemans B."
    assert fr.reduce_names("Bevestiging recall bij Lies Van Genechten") == "Bevestiging recall bij Lies Van G."


def test_name_reducer_leaves_dates_products_and_sentence_starts_alone():
    assert fr.reduce_names("Bevestiging mondonderzoek 22 September 2026").endswith("22 September 2026")
    assert "ClickDoc must contain" in fr.reduce_names("De actie faalde: ClickDoc must contain one label")
    # a name that opens the string has no left context to disambiguate — accepted miss
    assert fr.reduce_names("Molemans Bo stuurde een mail") == "Molemans Bo stuurde een mail"
    # a run broken by punctuation does not swallow the next word
    assert fr.reduce_names("expected Dorien Vanbrabant: Mondonderzoek").endswith("Dorien V.: Mondonderzoek")


def test_name_reducer_spares_learned_project_vocabulary():
    assert fr.reduce_names("gestuurd naar Sint Joris") == "gestuurd naar Sint J."
    fr.learn_non_names(["sint-joris", "De Kies"])
    assert fr.reduce_names("gestuurd naar Sint Joris") == "gestuurd naar Sint Joris"
    assert fr.reduce_names("gestuurd naar De Kies") == "gestuurd naar De Kies"


# ------------------------------------------------------------------ kpi contract


def _run(**kwargs):
    base = {"run_id": "a" * 8, "created_at": "2026-09-04T09:00:00Z", "kind": "email",
            "has_draft": True, "outcome": "answered", "category": "ok", "health": {}}
    return {**base, **kwargs}


def test_kpis_contract_keys_and_counting():
    cfg = {"include_kinds": ("email",), "dev_tenants": {"demo"}, "noise_topics": ("reminder",),
           "noise_senders": ()}
    runs = [
        _run(run_id="r1", human_outcome="edited"),
        _run(run_id="r2", kind="prompt"),
        _run(run_id="r3", tenant="demo"),
        _run(run_id="r4", topic="Automatische reminder"),
        _run(run_id="r5", category="workspace_failure", error="boom", human_outcome="no_draft"),
    ]
    for run in runs:
        run["excluded_reason"] = collect.exclusion_reason(run, cfg)
    block = collect.count_day(date(2026, 9, 4), runs, [], [], [], [], TZ)
    assert set(block) == {"date", *collect.KPI_KEYS}
    assert block["runs"] == 5 and block["counted"] == 2 and block["excluded"] == 3
    assert block["edited"] == 1 and block["run_errors"] == 1
    assert [r["excluded_reason"] for r in runs] == [None, "kind:prompt", "dev_tenant:demo",
                                                    "noise_topic", None]


def test_flagged_runs_are_detected_from_health_and_category():
    assert collect.is_flagged(_run(category="workspace_failure"))
    assert collect.is_flagged(_run(health={"bash_err_real_count": 2}))
    assert collect.is_flagged(_run(outcome="error"))
    assert not collect.is_flagged(_run())


# --------------------------------------------------------------- recurrence


def test_recurrence_state_and_known_since():
    ledger = {"run_error:x": {"first_seen": "2026-08-20T10:00:00+00:00"}}
    fresh = fr.recurrence({}, "run_error:y", "run_error", "t", 3, 0,
                          "2026-09-04T08:00:00+00:00", "2026-09-04T09:00:00+00:00")
    assert fresh["state"] == "new"
    known = fr.recurrence(ledger, "run_error:x", "run_error", "t", 1, 2,
                          "2026-09-03T08:00:00+00:00", "2026-09-04T09:00:00+00:00")
    assert known["state"] == "recurring" and known["known_since"] == "2026-08-20T10:00:00+00:00"
    gone = fr.recurrence(ledger, "run_error:x", "run_error", "t", 0, 4,
                         "2026-09-01T08:00:00+00:00", "2026-09-03T09:00:00+00:00")
    assert gone["state"] == "gone"


# --------------------------------------------------------------- learning plane


def test_false_delta_detection_keeps_negations_and_contrasts():
    assert fr.is_false_delta({"delta_description": "The versions are identical."})
    assert not fr.is_false_delta({"delta_description": "The answer is niet identiek aan het origineel."})
    assert not fr.is_false_delta(
        {"delta_description": "De tekst is ongewijzigd, maar het tijdstip verschilt."})
    assert fr.is_false_delta({"delta_category": "other", "similarity": 0.83,
                              "delta_description": "markdown rendered to plaintext"})


def test_human_outcome_ladder():
    assert fr.human_outcome({"has_draft": False}, [], "live") == "no_draft"
    assert fr.human_outcome({"has_draft": True}, [{"shadow": False}], "live") == "edited"
    assert fr.human_outcome({"has_draft": True}, [{"shadow": True}], "shadow") == "shadow_compared"
    assert fr.human_outcome({"has_draft": True, "drafts": [{"sent_message_id": "x"}]}, [], "live") \
        == "sent_as_proposed"
    assert fr.human_outcome({"has_draft": True, "drafts": []}, [], "live") == "not_sent_yet"


# ------------------------------------------------------------------- trace header


def test_trace_header_allowlist_drops_the_bootstrap_prompt():
    header = {
        "run_id": "r1", "tenant": "lbv", "trigger": "inbound", "bootstrap_turn": "x" * 5000,
        "system_prompt": "y" * 5000, "question": "z" * 500,
        "guards": {"final": {"outcome": "replied"}, "secret": 1},
        "grounding_sources": {"captured": True, "sources": [
            {"name": "pro-backup-backend", "kind": "mirror", "mounted": False, "available": False,
             "state": "ok", "details": {"huge": "x" * 1000}},
            {"name": "project", "kind": "kb", "mounted": True, "available": True, "state": "ok"},
        ]},
        "metadata": {"run_url": "https://example/runs/r1", "iterations": 4},
    }
    reduced = fr.reduce_trace_header(header)
    assert "bootstrap_turn" not in reduced and "system_prompt" not in reduced
    assert reduced["tenant"] == "lbv" and reduced["run_url"].endswith("/r1")
    assert set(reduced["guards"]) == {"final"}
    assert fr.unmounted_sources(reduced) == ["pro-backup-backend"]


# ------------------------------------------------------------------ brain root


def test_find_brain_root_prefers_the_nearest_checkout(tmp_path):
    brain = tmp_path / "replypen-brain-demo"
    (brain / ".git").mkdir(parents=True)
    (brain / "projection.yaml").write_text("x")
    nested = brain / "skills" / "records"
    nested.mkdir(parents=True)
    assert fr.find_brain_root(nested) == brain
    assert fr.project_name(brain) == "demo"


def test_overlay_hooks_are_fail_soft(tmp_path):
    root = tmp_path / "_internal" / "fleet-report"
    root.mkdir(parents=True)
    (root / "config.toml").write_text('report_id = "kampadmin"\ndev_tenants = ["demo"]\n')
    (root / "overlay.py").write_text(
        "def channel_of(run):\n    return 'chat'\n\ndef classify_run(run):\n    raise ValueError('boom')\n"
    )
    overlay = fr.load_overlay(tmp_path)
    assert overlay.get("report_id") == "kampadmin"
    assert overlay.call("channel_of", {}) == "chat"
    assert overlay.call("classify_run", {}, default=[]) == []
    assert overlay.problems and "classify_run" in overlay.problems[0]
    assert overlay.call("drill", {}, {}) is None  # slice C hook, absent here


# -------------------------------------------------------------- recurrence state


def _rec(ledger, focus, context, first="2026-09-04T10:00:00Z"):
    return fr.recurrence(ledger, "sig", "run_error", "boom", focus, context, first, first)


def test_recurrence_state_is_about_this_window_not_the_ledger():
    assert _rec({}, 2, 0)["state"] == "new"
    assert _rec({}, 2, 3)["state"] == "recurring"
    assert _rec({}, 0, 3)["state"] == "gone"
    # a prior ledger entry records `known_since`; it must NOT turn a focus-only signature into
    # `recurring` (the bug that made every new failure look like old news)
    prior = {"sig": {"first_seen": "2026-08-01T00:00:00Z", "signature": "sig"}}
    entry = _rec(prior, 2, 0)
    assert entry["state"] == "new"
    assert entry["known_since"] == "2026-08-01T00:00:00Z"


# ------------------------------------------------------ digest run-list filtering


def _digest_evidence(runs, **extra):
    base = {
        "report_id": "t", "projects": ["t"], "timezone": "Europe/Brussels",
        "window": {"focus": "2026-09-04", "context_days": ["2026-09-03"]},
        "generated_at": "2026-09-05T00:00:00Z", "coverage": [], "excluded": {},
        "kpis": {"focus": collect.empty_kpis(date(2026, 9, 4)),
                 "context_days": [collect.empty_kpis(date(2026, 9, 3))], "per_axis": []},
        "clusters": [], "runs": runs, "deltas": [], "feedback": [], "draft_fate": {},
        "watch": [], "errors": [],
    }
    base.update(extra)
    return base


def _digest_run(**kwargs):
    base = {"run_id": "b" * 32, "created_at": "2026-09-04T09:00:00Z", "day": "2026-09-04",
            "axis_key": "acme", "topic": "iets", "human_outcome": "no_draft", "flagged": False}
    base.update(kwargs)
    return base


def test_excluded_runs_are_counted_not_listed_unless_they_errored():
    quiet = _digest_run(run_id="1" * 32, excluded_reason="kind:chat")
    loud = _digest_run(run_id="2" * 32, excluded_reason="kind:prompt", error="run: boom")
    real = _digest_run(run_id="3" * 32)
    digest = collect.build_digest(_digest_evidence([quiet, loud, real]), TZ)
    body = digest.split("## Focus-period runs")[1]
    assert "11111111" not in body
    assert "22222222" in body and "excl:kind:prompt" in body
    assert "33333333" in body
    assert "2 excluded as noise, of which 1 still shown" in digest


def test_clusters_active_on_the_focus_day_rank_above_context_only_ones():
    def cluster(kind, sig, focus, context, state):
        return {"kind": kind, "signature": sig, "title": sig, "detail": "",
                "focus_count": focus, "context_count": context, "run_ids": [], "run8s": [],
                "recurrence": {"focus_count": focus, "context_count": context, "state": state}}

    old_error = cluster("run_error", "run_error:old", 0, 9, "gone")
    fresh = cluster("action_failure", "action_failure:new", 1, 0, "new")
    ordered = collect.rank([old_error, fresh])
    assert ordered[0] is fresh
    digest = collect.build_digest(_digest_evidence([], clusters=[old_error, fresh]), TZ)
    assert digest.index("action_failure:new") < digest.index("run_error:old")
    assert "Not seen on D" in digest


# ------------------------------------------------------------ drill trace summary


def test_summarise_steps_flattens_a_trace_and_marks_the_grounding_pre_pass():
    records = [
        {"seq": -1000000, "tool": "bash", "command": "cat /brain/AGENTS.md", "exit_code": 0,
         "stdout": "# AGENTS\nlots of text", "at": "2026-09-04T08:14:06Z"},
        {"seq": 6, "tool": "bash", "command": "python search_availability.py --weekdays vrijdag",
         "exit_code": 2, "stderr": "usage: search_availability.py\nerror: unrecognized arguments: --weekdays",
         "at": "2026-09-04T08:15:00Z"},
        {"seq": 13, "tool": "action", "args": {"action_id": "create_placeholder_appointment"},
         "exit_code": 1, "stdout": '{"ok": false, "error": "permission denied for table users"}',
         "at": "2026-09-04T08:17:41Z"},
        {"note": "not a tool call"},
    ]
    steps = drill.summarise_steps(records)
    assert [s["seq"] for s in steps] == [-1000000, 6, 13]
    assert steps[0]["grounding"] is True and steps[1]["grounding"] is False
    assert steps[1]["stderr"].startswith("usage: search_availability.py")
    # json stdout collapses to its keys instead of dumping the payload
    assert "ok=false" in steps[2]["stdout"] and "permission denied" in steps[2]["stdout"]
    assert steps[2]["command"].startswith('{"action_id"')
    lines = drill.step_lines(steps)
    assert any("**exit 2**" in line for line in lines)
    assert lines[0].startswith("- `g` bash")


# ------------------------------------------------- overlay noise tags / clusters


def test_overlay_noise_tag_excludes_the_run(tmp_path):
    root = tmp_path / "_internal" / "fleet-report"
    root.mkdir(parents=True)
    (root / "config.toml").write_text('report_id = "demo"\n')
    (root / "overlay.py").write_text(
        "def classify_run(run):\n"
        "    q = str(((run.get('trace') or {}).get('question_head') or '')).lower()\n"
        "    return ['noise:vendor'] if 'sentry' in q else ['deferral']\n"
    )
    overlay = fr.load_overlay(tmp_path)
    cfg = {"include_kinds": ("email",), "dev_tenants": set(), "noise_topics": (), "noise_senders": ()}

    vendor = _run(run_id="v1", trace={"question_head": "New alert from Sentry"})
    real = _run(run_id="r1", trace={"question_head": "Hi, my form is broken"})
    for run in (vendor, real):
        run["tags"] = [str(t) for t in (overlay.call("classify_run", run, default=[]) or [])]
        noise_tag = next((t for t in run["tags"] if t.startswith("noise:")), None)
        run["excluded_reason"] = collect.exclusion_reason(run, cfg) or noise_tag

    assert vendor["excluded_reason"] == "noise:vendor"
    assert real["excluded_reason"] is None and real["tags"] == ["deferral"]
    block = collect.count_day(date(2026, 9, 4), [vendor, real], [], [], [], [], TZ)
    assert block["counted"] == 1 and block["excluded"] == 1


def test_trace_header_keeps_a_clipped_question_head_for_classification():
    reduced = fr.reduce_trace_header({"run_id": "r", "question": "Sentry issue " + "x" * 900})
    assert reduced["question_head"].startswith("Sentry issue")
    assert len(reduced["question_head"]) <= 602


def test_excluded_runs_do_not_feed_cluster_counts():
    counted = _run(run_id="c" * 32, error="boom on the same signature")
    counted["day"] = "2026-09-04"
    noisy = _run(run_id="n" * 32, error="boom on the same signature")
    noisy["day"] = "2026-09-04"
    noisy["excluded_reason"] = "noise_topic"
    clusters = collect.build_clusters([counted, noisy], [], [], [], {}, "2026-09-04", TZ)
    cluster = next(c for c in clusters if c["kind"] == "run_error")
    # one denominator: only the counted run is a hit, the excluded one is visible but separate
    assert cluster["focus_count"] == 1 and cluster["excluded_count"] == 1
    assert cluster["run_ids"] == ["c" * 32, "n" * 32]
    assert cluster["excluded_run_ids"] == ["n" * 32]
    # recurrence facts are echoed onto the cluster so a report copies instead of transcribing
    assert cluster["state"] == cluster["recurrence"]["state"] == "new"
    assert cluster["first_seen"] == cluster["recurrence"]["first_seen"]


def test_action_funnel_statuses_boundaries_and_execution_day():
    now = fr.parse_ts('2026-09-10T22:00:00Z')
    actions = [
        {'action_id': 'change', 'status': status, 'proposed_at': '2026-09-09T09:59:59Z'}
        for status in ('failed', 'superseded', 'canceled', 'executing', 'proposed')
    ] + [
        {'action_id': 'change', 'status': 'proposed', 'proposed_at': '2026-09-09T10:00:00Z'},
        # Exactly 120 seconds remains auto; 121 is reviewer-confirmed.
        {'action_id': 'change', 'status': 'succeeded', 'proposed_at': '2026-09-09T12:00:00Z',
         'executed_at': '2026-09-09T12:02:00Z'},
        {'action_id': 'change', 'status': 'succeeded', 'proposed_at': '2026-09-09T12:00:00Z',
         'executed_at': '2026-09-09T12:02:01Z'},
        # Proposal in context, execution in focus: counts once, in focus (Brussels midnight).
        {'action_id': 'later', 'status': 'succeeded', 'proposed_at': '2026-09-08T20:00:00Z',
         'executed_at': '2026-09-08T22:00:00Z'},
        {'action_id': 'context', 'status': 'canceled', 'proposed_at': '2026-09-08T12:00:00Z'},
        {'action_id': 'outside', 'status': 'failed', 'proposed_at': '2026-09-07T12:00:00Z'},
    ]
    window = {'focus': '2026-09-09', 'context_days': ['2026-09-08']}
    funnel = collect.build_action_funnel(actions, [], window, tz=TZ, now=now)
    row = funnel['focus']['rows'][0]
    assert row == dict(action_id='change', proposed_total=8, succeeded=2, failed=1,
                       superseded=1, canceled=1, executing=1, pending=1, stale=1,
                       human_confirmed=1, auto=1, acceptance_rate=0.2)
    assert funnel['focus']['total']['proposed_total'] == 9
    assert funnel['focus']['total']['acceptance_rate'] == 0.33
    assert funnel['context']['total']['proposed_total'] == 1
    assert funnel['context']['rows'][0]['action_id'] == 'context'
    legacy = collect.count_day(date(2026, 9, 9), [], actions, [], [], [], TZ)
    assert funnel['focus']['total']['succeeded'] == legacy['actions_ok']
    assert row['pending'] + row['stale'] == legacy['actions_proposed']
    empty = collect.build_action_funnel([], [], window, tz=TZ, now=now)
    assert empty['focus']['rows'] == []
    assert empty['focus']['total']['acceptance_rate'] is None
    weekly = collect.build_action_funnel(actions, [], {**window, 'focus_days': ['2026-09-08', '2026-09-09'],
                                                      'context_days': []}, tz=TZ, now=now)
    assert weekly['focus']['total']['proposed_total'] == 10


def test_action_funnel_axes_join_run_id_not_tenant_uuid():
    runs = [{'run_id': 'r1', 'axis': 'tenant', 'axis_key': 'clinic', 'member': 'one'},
            {'run_id': 'r2', 'axis': 'channel', 'axis_key': 'email', 'member': 'two'}]
    actions = [{'run_id': run['run_id'], 'member': run['member'], 'tenant_id': 'opaque-uuid',
                'action_id': 'change', 'status': 'succeeded', 'proposed_at': '2026-09-09T12:00:00Z',
                'executed_at': '2026-09-09T12:00:01Z'} for run in runs]
    actions.append({**actions[0], 'run_id': 'uncollected'})
    funnel = collect.build_action_funnel(actions, runs, {'focus': '2026-09-09'}, tz=TZ)
    axes = {(a['axis'], a['key']): a['total'] for a in funnel['per_axis']}
    assert axes[('tenant', 'clinic')]['proposed_total'] == 1
    assert axes[('channel', 'email')]['auto'] == 1
    assert axes[('member', 'one')]['proposed_total'] == 2
    assert funnel['focus']['total']['proposed_total'] == 3
    assert funnel['focus']['total']['acceptance_rate'] is None


# ------------------------------------------------------------- owner language


class _StubRc:
    """Just the two Rc surfaces `owner_languages` uses."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.coverage = []
        self.asked = []

    def json(self, *args):
        self.asked.append(args)
        return self.payloads.get(args[-1])

    def note_coverage(self, record):
        self.coverage.append(record)


def _settings(language):
    return {"resolved": {"persona": {"language": {"source": "project", "value": language}}}}


def test_language_code_reads_persona_prose_not_a_hardcoded_default():
    assert fr.language_code("Nederlands, altijd — concept én note, ook wanneer brain Engels is") == "nl"
    assert fr.language_code("Always English, also when the brain is Dutch/Nederlands") == "en"
    assert fr.language_code("Français, toujours") == "fr"
    assert fr.language_code("nl-BE") == "nl"
    # empty is the host's own "⇒ English"; unreadable prose resolves to nothing, never to Dutch
    assert fr.language_code("") == "en"
    assert fr.language_code("Antwoord in de taal van de klant") is None


def test_owner_languages_resolve_tenant_then_project_then_english():
    rc = _StubRc({
        "get": _settings("Nederlands, altijd"),   # project call ends in "get"
        "de-kies": _settings("Nederlands, altijd"),
        "orthodusart": _settings("Français, toujours"),
    })
    project, by_tenant = fr.owner_languages(
        rc, ["dentai"], {"dentai": ["de-kies", "orthodusart", "molaar"]}
    )
    assert project == "nl"
    # a tenant whose settings are unreadable inherits the project language, not a literal "nl"
    assert by_tenant == {"de-kies": "nl", "orthodusart": "fr", "molaar": "nl"}
    assert rc.coverage[-1].status == "partial" and "molaar" in (rc.coverage[-1].reason or "")

    blind = _StubRc({})
    assert fr.owner_languages(blind, ["dentai"], {}) == ("en", {})
    assert blind.coverage[-1].status == "partial"


def test_overlay_lang_is_an_explicit_override():
    rc = _StubRc({"get": _settings("Nederlands, altijd")})
    project, by_tenant = fr.owner_languages(
        rc, ["dentai"], {"dentai": ["de-kies"]}, override="DE"
    )
    assert (project, by_tenant) == ("de", {"de-kies": "de"})
    assert not rc.asked  # the override never calls the host


def test_digest_names_the_owner_language_per_tenant():
    line = collect.owner_language_line(
        {"owner_lang": "nl", "owner_lang_by_tenant": {"de-kies": "nl", "orthodusart": "fr"}}
    )
    assert line.startswith("Owner language: nl (project)") and "orthodusart: fr" in line
    assert "de-kies" not in line
