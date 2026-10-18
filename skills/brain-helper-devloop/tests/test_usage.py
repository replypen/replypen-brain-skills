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
    rows = {H: {"path": H, "source": "acme-common", "entry": True, "idents": {"people", "subscriptions"}},
            "/brain/skills/x/scripts/idle.py": {"path": "/brain/skills/x/scripts/idle.py", "source": "brain",
                                                "entry": True, "idents": set()}}
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
