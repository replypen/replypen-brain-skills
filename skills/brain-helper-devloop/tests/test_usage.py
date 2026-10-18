"""Offline tests for execution.py + usage.py: synthetic commands and runs only (no rp, no PII)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import usage
from execution import normalise_sql, sites, sql_strings, sql_tables

H = "/mirrors/acme-common/skills/records/scripts/widget_check.py"


def kinds(command):
    return [(s["path"], s["kind"], s["loop"]) for s in sites(command)]


def test_reads_help_and_launch_are_separated():
    assert kinds(f"cat {H}; sed -n 1,40p {H} | head; python {H} --help; python3 {H} --id 4 > /tmp/o.txt") == [
        (H, "read", False), (H, "read", False), (H, "help", False), (H, "launch", False)]


def test_subprocess_in_python_heredoc_loop_counts_once_as_loop():
    command = (f"python - <<'PY'\nimport subprocess\nfor pid in ids:\n"
               f"    subprocess.run(['python', '{H}', '--person', pid], capture_output=True)\nPY")
    assert kinds(command) == [("subprocess", "import", False), (H, "launch", True)]


def test_heredoc_written_to_py_file_is_python_but_cat_body_is_not():
    written = f"cat > /tmp/x.py <<'PY'\nimport subprocess\nsubprocess.run(['python','{H}'])\nPY\npython /tmp/x.py"
    assert (H, "launch", False) in kinds(written)
    assert ("/tmp/x.py", "launch", False) in kinds(written)
    assert kinds(f"cat <<'EOF'\npython {H} --id 1\nEOF") == []


def test_subprocess_reader_is_a_read_and_import_only_command_is_kept():
    assert kinds(f"python - <<'PY'\nimport subprocess, sys\nsubprocess.run(['cat', '{H}'])\n"
                 f"subprocess.run([sys.executable, '{H}', '--id', '1'])\nPY")[2:] == [(H, "read", False), (H, "launch", False)]
    assert usage.KEEP_CMD.search("python -c 'from widget_check import check; check(1)'")
    assert kinds("python -c 'from widget_check import check; check(1)'") == [("widget_check", "import", False)]


def test_psql_heredoc_sql():
    assert sql_strings("psql <<'SQL'\nSELECT * FROM people;\nselect 1 from coupons\nSQL") == [
        "SELECT * FROM people", "select 1 from coupons"]


def test_shell_loop_and_python_c():
    assert kinds(f"for id in 1 2; do python {H} --id $id; done") == [(H, "launch", True)]
    assert kinds(f"python -c \"import subprocess; subprocess.run(['python', '{H}'])\"") == [
        ("subprocess", "import", False), (H, "launch", False)]


def test_sql_extraction_and_normalisation():
    command = ("python - <<'PY'\nfrom lib import db\n"
               "db.query(\"select id from people p join subscriptions s on s.parent_id=p.id "
               "where p.email ilike %s and s.id in (1, 2, 3)\", ['%x%'])\n"
               "print('selected from the list')\nPY\npsql -c \"SELECT * FROM coupons WHERE id = 'ab'\"")
    found = sql_strings(command)
    assert len(found) == 2
    norm = normalise_sql(found[0])
    assert norm == "select id from people p join subscriptions s on s.parent_id=p.id where p.email ilike ? and s.id in (?)"
    assert sql_tables(norm) == ["people", "subscriptions"]
    assert sql_tables(normalise_sql("with x as (select 1 from a) select * from x join b on true")) == ["a", "b"]


def run(rid, kind="chat", simulation=False):
    return {"run_id": rid, "kind": kind, "simulation": simulation, "created_at": "2026-10-16T10:00:00Z",
            "status": "done", "health": {"bash_total": 1}}


def rec(rid, cmds, email="admin@customer.be", tenant="t1"):
    return {"schema": usage.SCHEMA, "run_id": rid, "terminal": True,
            "bash": [{"seq": seq, "exit": ex, "ms": 1000, "cmd": cmd, "tb": []} for seq, ex, cmd in cmds],
            "trace": {"tenant": tenant, "email": email, "brain": "", "question": "q"}}


def test_aggregate_cohorts_inventory_and_discovery():
    rows = {H: {"path": H, "source": "acme-common", "entry": True, "tables": {"people", "subscriptions"}},
            "/brain/skills/x/scripts/idle.py": {"path": "/brain/skills/x/scripts/idle.py", "source": "brain",
                                                "entry": True, "tables": set()}}
    sql = "python - <<'PY'\nfrom lib import db\ndb.query('select * from people where id=%s', [1])\nPY"
    runs = [run("r1"), run("r2"), run("r3", kind="prompt"), run("r4")]
    records = {
        "r1": rec("r1", [(-1, 2, f"sed -n 1,9p {H}"), (3, 0, f"python {H} --id 1"), (4, 0, sql)]),
        "r2": rec("r2", [(2, 2, f"cd /mirrors/acme-common/skills/records/scripts && python widget_check.py"),
                         (5, 0, sql)], tenant="t2"),
        "r3": rec("r3", [(1, 0, f"python {H}")], email=None),
        "r4": rec("r4", [(1, 0, f"python {H}"), (2, 0, sql)], email="me@acme.be"),
    }
    helpers, discovery = usage.aggregate(runs, records, rows, ["acme.be"], None, "https://x")
    top = helpers[0]
    assert top["path"] == H
    assert (top["customer_runs"], top["test_runs"], top["tenants"]) == (2, 2, 2)
    assert (top["launches"], top["exit_2"], top["exit_nonzero"], top["prestep_reads"]) == (4, 1, 0, 1)
    idle = next(h for h in helpers if h["path"].endswith("idle.py"))
    assert idle["launches"] == idle["customer_runs"] == 0
    assert discovery[0]["tables"] == ["people"] and discovery[0]["customer_runs"] == 2
    assert discovery[0]["maybe_covered_by"] == [H]
    md = usage.render({"project": "acme", "tenant": None, "generated_at": "now", "base_url": "https://x",
                       "mirrors": ["acme-common"], "coverage": {"since": "a", "until": None, "pages": 1},
                       "helpers": helpers, "discovery": discovery})
    assert "`widget_check.py`" in md and "Not observed in window: 1" in md and "**people**" in md


def test_input_rerenders_offline(tmp_path):
    report = {"project": "acme", "tenant": None, "generated_at": "now", "base_url": "https://x", "mirrors": [],
              "coverage": {"since": "a", "until": None}, "helpers": [], "discovery": []}
    (tmp_path / "in.json").write_text(json.dumps(report))
    assert usage.main(["--input", str(tmp_path / "in.json"), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "usage.md").read_text().startswith("# Helper usage — acme")


def test_scrubbed_events_fall_back_to_kept_command_and_count_blind():
    events = "\n".join(json.dumps(e) for e in [
        {"tool": "bash", "seq": 1, "command": "", "kept": {"v": 1, "command": "python /brain/skills/x/scripts/a.py --email ⟨str⟩"}},
        {"tool": "bash", "seq": 2, "command": "", "kept": None},
        {"tool": "bash", "seq": 3, "command": "python /brain/skills/x/scripts/b.py"},
    ])
    scrub: dict = {}
    cmds = [b["cmd"] for b in usage.reduce_events(events, scrub)]
    assert cmds == ["python /brain/skills/x/scripts/a.py --email ⟨str⟩", "python /brain/skills/x/scripts/b.py"]
    assert scrub == {"kept": 1, "blind": 1}


def test_heredoc_traceback_is_not_blamed_on_a_chained_helper():
    heredoc = ["Traceback (most recent call last):", '  File "<stdin>", line 3, in <module>', "KeyError: 'groups'"]
    deep = ["  File \"psycopg/cursor.py\", line 225, in fetchall", "psycopg.DataError: date too large"]
    assert usage.foreign_traceback(heredoc, H)
    assert not usage.foreign_traceback(deep, H)
    assert not usage.foreign_traceback(heredoc + [f'  File "{H}", line 9'], H)


def test_vocab_reduced_path_rejoins_its_unique_inventory_helper():
    rows = {p: {} for p in ("/brain/skills/databases/scripts/backup_status.py",
                            "/brain/skills/databases/scripts/restore_report.py",
                            "/brain/skills/platform-api/scripts/platform_api_read.py")}
    resolve = usage.resolver(rows)
    assert resolve("/brain/skills/databases/scripts/⟨vocab⟩_status.py", "path").endswith("backup_status.py")
    assert resolve("/brain/skills/platform-⟨vocab⟩/scripts/platform_⟨vocab⟩_read.py", "path").endswith("api_read.py")
    assert resolve("/brain/skills/databases/scripts/⟨vocab⟩.py", "path") == "/brain/skills/databases/scripts/⟨vocab⟩.py"


def test_coverage_needs_a_queried_table_and_skips_mirror_one_offs(tmp_path):
    (tmp_path / "skills/x/scripts").mkdir(parents=True)
    (tmp_path / "skills/x/scripts/lookup.py").write_text(
        'import argparse\nSQL = "select id from people p join subscriptions s on s.person_id = p.id"\n')
    (tmp_path / "skills/x/scripts/prose.py").write_text('"""Mentions people in prose only."""\nimport argparse\n')
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/one_off.py").write_text('import argparse\nq = "select * from people"\n')
    (tmp_path / "scripts/cataloged.py").write_text('# name: cataloged\nimport argparse\nq = "select * from people"\n')
    rows, _ = usage.inventory(tmp_path / "brain-missing", {"acme": tmp_path})
    m = "/mirrors/acme/"
    assert rows[m + "skills/x/scripts/lookup.py"]["tables"] >= {"people", "subscriptions"}
    assert "people" not in rows[m + "skills/x/scripts/prose.py"]["tables"]
    assert not rows[m + "scripts/one_off.py"]["helper"] and rows[m + "scripts/cataloged.py"]["helper"]
    sql = "python - <<'PY'\nfrom lib import db\ndb.query('select * from people where id=%s', [1])\nPY"
    _, discovery = usage.aggregate([run("r1")], {"r1": rec("r1", [(1, 0, sql)])}, rows, [], None, "https://x")
    covered = [p.rsplit("/", 1)[-1] for p in discovery[0]["maybe_covered_by"]]
    assert covered == ["cataloged.py", "lookup.py"]  # most specific first; one_off.py and prose.py never


def test_bare_help_is_split_from_help_chained_to_a_launch():
    rows = {H: {"path": H, "source": "acme-common", "entry": True, "tables": set()}}
    records = {"r1": rec("r1", [(1, 0, f"python {H} --help"), (2, 0, f"python {H} --help; python {H} --id 1")])}
    helpers, _ = usage.aggregate([run("r1")], records, rows, [], None, "https://x")
    assert (helpers[0]["help_calls"], helpers[0]["help_chained"], helpers[0]["launches"]) == (1, 1, 1)
    md = usage.render({"project": "acme", "tenant": None, "generated_at": "now", "base_url": "https://x",
                       "mirrors": [], "coverage": {"since": "a", "until": None}, "helpers": helpers, "discovery": []})
    assert "| 1 (+1 chained) |" in md


def test_exit_is_charged_to_the_last_launch_site_or_the_helper_stderr_names():
    a, b = "/brain/skills/x/scripts/a.py", "/brain/skills/x/scripts/b.py"
    rows = {p: {"path": p, "source": "brain", "entry": True, "tables": set()} for p in (a, b)}
    records = {"r1": rec("r1", [(1, 2, f"python {a} --id 1; python {b} --id 1"),
                                (2, 1, f"python {a} --id 2 && python {b} --id 2")])}
    records["r1"]["bash"][1]["err"] = "usage: a.py [-h] --id ID\na.py: error: bad id"
    helpers, _ = usage.aggregate([run("r1")], records, rows, [], None, "https://x")
    got = {h["path"]: (h["exit_2"], h["exit_nonzero"]) for h in helpers}
    assert got == {a: (0, 1), b: (1, 0)}


def test_undeclared_mirror_warns_with_the_line_to_add(tmp_path):
    brain = tmp_path / "acme-org/brain-acme"
    (tmp_path / "other-org/acme-app/.git").mkdir(parents=True)
    brain.mkdir(parents=True)
    path = "/mirrors/acme-app/.agents/scripts/lookup.py"
    helpers, _ = usage.aggregate([run("r1")], {"r1": rec("r1", [(1, 0, f"python {path} --id 1")])}, {}, [], None, "x")
    undeclared = usage.undeclared_mirrors(helpers, {"declared": tmp_path})
    assert undeclared == {"acme-app": 1}
    hint = usage.mirror_hint(brain, "acme-app")
    assert hint == '"acme-app" = "../../other-org/acme-app"'
    md = usage.render({"project": "acme", "tenant": None, "generated_at": "now", "base_url": "x", "mirrors": [],
                       "mirror_hints": {"acme-app": hint},
                       "coverage": {"since": "a", "until": None, "undeclared_mirrors": undeclared},
                       "helpers": helpers, "discovery": []})
    assert "> [!WARNING]" in md and hint in md and "`/mirrors/acme-app/…` 1×" in md


def test_import_resolves_to_helpers_only_not_a_mirror_app_module():
    rows = {"/mirrors/app/apps/leave/requests.py": {"helper": False},
            "/mirrors/app/skills/x/scripts/widget_check.py": {"helper": True}}
    resolve = usage.resolver(rows)
    assert resolve("requests", "import") is None
    assert resolve("widget_check", "import").endswith("widget_check.py")
