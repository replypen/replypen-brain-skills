"""End to end checks for the grounding-intake engine.

    cd skills/brain-grounding-intake && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import types
from html import escape
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FIXTURES = SKILL / "fixtures"


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          text=True, capture_output=True, check=False)


def load(script: str, name: str):
    """Import one script in process, for the pieces that must be tested without a network."""
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------- mini repo

def mini_repo(root: Path) -> None:
    """Two apps in one checkout: a CodeIgniter legacy app and a Laravel app."""
    write(root / "legacy/index.php", "<?php require 'application/bootstrap.php';\n")
    write(root / "legacy/application/config/config.php", "<?php $config = [];\n")
    write(root / "legacy/application/config/routes.php", "<?php $route['default'] = 'home';\n")
    write(root / "legacy/application/models/Salon_model.php",
          "<?php class Salon_model { protected $table = 'users'; }\n")
    write(root / "legacy/application/controllers/cron/Reminders.php", "<?php class Reminders {}\n")
    write(root / "app/composer.json", json.dumps(
        {"require": {"laravel/framework": "^10.0", "mollie/mollie-api-php": "^2.0"}}) + "\n")
    write(root / "app/artisan", "#!/usr/bin/env php\n")
    write(root / "app/routes/web.php", "<?php Route::get('/', fn () => view('home'));\n")
    write(root / "app/app/Models/Treatment.php",
          "<?php class Treatment { protected $table = 'treatments'; }\n")
    write(root / "app/app/Mail/ReminderMail.php", "<?php class ReminderMail {}\n")
    write(root / "app/app/Console/Kernel.php", "<?php class Kernel {}\n")
    write(root / "app/database/migrations/2020_01_01_create_treatments.php",
          "<?php // create treatments\n")
    write(root / "app/.env.example", "APP_KEY=\n")
    write(root / "app/.env", "APP_KEY=base64:supersecretvalue\n")
    write(root / "app/vendor/foo/bar.php", "<?php // vendored\n")


def tables_file(tmp: Path) -> Path:
    path = tmp / "tables.txt"
    path.write_text("# the tables the dev named\nusers\ntreatments\nghost\n", encoding="utf-8")
    return path


def app_named(scan: dict, suffix: str) -> dict:
    return next(app for app in scan["repos"][0]["apps"] if app["root"].endswith(suffix))


def framework_names(app: dict) -> set[str]:
    return {f["name"] for f in app["frameworks"]}


def test_scan_local(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    mini_repo(repo)
    out = tmp_path / "out"
    done = run("scan.py", "--repo", f"app={repo}", "--tables", str(tables_file(tmp_path)),
               "--out", str(out))
    assert done.returncode == 0, done.stderr

    scan = json.loads((out / "scan.json").read_text())
    assert scan["repos"][0]["mode"] == "local"
    assert len(scan["repos"][0]["apps"]) == 2
    assert "laravel" in framework_names(app_named(scan, "/app"))
    assert "codeigniter" in framework_names(app_named(scan, "/legacy"))

    listing = (out / "raw" / "listing-app.txt").read_text()
    assert "app/.env.example" in listing
    assert "app/.env\n" not in listing
    assert "vendor/" not in listing
    assert ".env" not in (out / "scan.md").read_text().replace(".env.example", "")

    mollie = next(hit for hit in app_named(scan, "/app")["integrations"]
                  if hit["vendor"] == "mollie")
    assert mollie["dep"] is True

    rows = {line.split("\t")[0]: line.split("\t")
            for line in (out / "tables.tsv").read_text().splitlines()[1:]
            if not line.startswith("#")}
    assert "Salon_model.php" in rows["users"][1]
    assert "Treatment.php" in rows["treatments"][1]
    assert "ghost" in rows and rows["ghost"][1] == ""
    assert "# unmatched (1)" in (out / "tables.tsv").read_text()


def test_scan_listing(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    mini_repo(repo)
    rels = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file())
    rels = [rel for rel in rels if "/vendor/" not in rel and not rel.endswith("/.env")]
    listing = tmp_path / "find.txt"
    listing.write_text("\n".join(f"/mirrors/app/{rel}" for rel in rels) + "\n", encoding="utf-8")

    out = tmp_path / "out"
    done = run("scan.py", "--listing", f"app={listing}", "--tables", str(tables_file(tmp_path)),
               "--out", str(out))
    assert done.returncode == 0, done.stderr

    scan = json.loads((out / "scan.json").read_text())
    repo_record = scan["repos"][0]
    assert repo_record["mode"] == "listing"
    assert repo_record["root"] == "/mirrors/app"
    assert "mode: listing (contents not read)" in (out / "scan.md").read_text()
    assert "laravel" in framework_names(app_named(scan, "/app"))
    assert "codeigniter" in framework_names(app_named(scan, "/legacy"))
    # no contents were read, so no line counts, no deps and no declared tables
    assert all(item["lines"] == 0
               for item in app_named(scan, "/app")["areas"]["entry"])
    assert app_named(scan, "/app")["deps"] == []
    assert all(not row["model_files"] for row in scan["tables"])
    assert next(row for row in scan["tables"] if row["table"] == "treatments")["guess"].endswith(
        "app/app/Models/Treatment.php")


def test_scan_takes_tables_from_a_probed_schema(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    mini_repo(repo)
    out = tmp_path / "out"
    out.mkdir()
    shutil.copy(FIXTURES / "schema.json", out / "schema.json")
    done = run("scan.py", "--repo", f"app={repo}", "--out", str(out))
    assert done.returncode == 0, done.stderr
    assert "taking the 7 table names" in done.stdout
    tables = {row["table"] for row in json.loads((out / "scan.json").read_text())["tables"]}
    assert {"agenda", "remarks", "settings"} <= tables


# --------------------------------------------------------------------- fixture

def copy_fixture(tmp_path: Path) -> Path:
    """The fixture inside a fake brain checkout, so `brain:` locators resolve."""
    write(tmp_path / ".replypen.toml", 'project = "salonx"\n')
    write(tmp_path / "skills" / "tenant-model.md",
          "---\ndescription: who sees what\n---\n\nOne salon sees one agenda.\n")
    out = tmp_path / ".replypen" / "grounding-intake" / "2026-09-08"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / "out", out)
    return out


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name}: fixture no longer contains {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_validate_and_render_fixture(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    done = run("validate.py", str(out))
    assert done.returncode == 0, done.stderr
    assert "OK: 8 questions" in done.stdout

    data = json.loads((out / "intake.json").read_text())
    assert data["project"] == "salonx"
    assert data["db"] == "staging"
    assert data["engine"] == "mysql"
    assert data["tally"] == {"grounded": 2, "ambiguous": 2, "missing": 2, "knowledge": 1,
                             "human": 1}
    assert set(data["proposal"]) == {"codebase/INDEX.md", "codebase/booking.md",
                                     "databases/staging.md"}
    assert data["scan"] and data["schema"]
    assert "what a run of salonx receives" in data["context"]
    assert all(row["impact"] for row in data["devquestions"])

    done = run("render.py", str(out / "intake.json"))
    assert done.returncode == 0, done.stderr
    html = (out / "report.html").read_text()
    for question in data["devquestions"]:
        assert escape(question["question"], quote=True) in html
        assert escape(question["impact"], quote=True) in html
    assert "Copy all as Markdown" in html
    assert 'type="radio"' in html
    assert "<link" not in html
    assert "<script src" not in html
    # the page loads nothing from the network; run links are the only external URLs
    assert 'src="http' not in html and "@import" not in html


def test_render_from_sample_intake_json(tmp_path: Path) -> None:
    target = tmp_path / "intake.json"
    shutil.copy(FIXTURES / "intake.json", target)
    done = run("render.py", str(target))
    assert done.returncode == 0, done.stderr
    html = (tmp_path / "report.html").read_text()
    assert "salonx" in html
    assert 'type="radio"' in html


def test_validate_rejects_unknown_evidence(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    edit(out / "benchmark.tsv", "/mirrors/app/app/app/Models/Booking.php:isOnlineBookable",
         "/mirrors/app/app/app/Models/Ghost.php")
    edit(out / "benchmark.tsv", "db:agenda.status", "db:agenda.ghost")
    edit(out / "proposal" / "databases" / "staging.md", "`users.active`", "`users.ghost`")

    done = run("validate.py", str(out))
    assert done.returncode == 1
    for expected in (
        "benchmark.tsv: Q1:",
        "is not in raw/listing-app.txt",
        "has no column 'ghost'",
        "proposal/databases/staging.md",
    ):
        assert expected in done.stderr, done.stderr
    assert not (out / "intake.json").exists()


def test_validate_rejects_a_thin_questionnaire(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    rows = [line for line in (out / "devquestions.tsv").read_text().splitlines()
            if not line.startswith("D7")]  # the only question citing the `exports` cluster
    rows = [line.rsplit("\t", 1)[0] + "\t" if line.startswith("D2") else line for line in rows]
    (out / "devquestions.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    done = run("validate.py", str(out))
    assert done.returncode == 1
    for expected in (
        "devquestions.tsv: D2: empty impact",
        "devquestions.tsv: cluster 'exports'",
    ):
        assert expected in done.stderr, done.stderr


def test_validate_rejects_bad_proposal_and_knowledge_lookup(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    edit(out / "benchmark.tsv", "knowledge\tbrain:skills/tenant-model.md",
         "knowledge\tbrain:skills/tenant-model.md;db:users")
    staging = out / "proposal" / "databases" / "staging.md"
    staging.write_text(staging.read_text(encoding="utf-8")
                       + "\n```sql\nselect * from agenda;\n```\n", encoding="utf-8")

    done = run("validate.py", str(out))
    assert done.returncode == 1
    for expected in (
        "status knowledge takes brain: locators only",
        "proposal/databases/staging.md",
        "fenced code block",
    ):
        assert expected in done.stderr, done.stderr


def test_validate_needs_one_readable_half(tmp_path: Path) -> None:
    out = copy_fixture(tmp_path)
    (out / "scan.json").unlink()
    (out / "schema.json").unlink()
    done = run("validate.py", str(out))
    assert done.returncode == 1
    assert "neither is there" in done.stderr


# ------------------------------------------------------------------- questions

def test_questions_from_line_list(tmp_path: Path) -> None:
    source = tmp_path / "asked.txt"
    source.write_text("# what the owner remembers\n"
                      "2026-08-02 Waarom ziet mijn klant geen vrije slots?\n"
                      "\n"
                      "Kan ik de herinnering later laten vertrekken?\n", encoding="utf-8")
    out = tmp_path / "out"
    done = run("questions.py", "--from", str(source), "--out", str(out))
    assert done.returncode == 0, done.stderr
    rows = (out / "questions.tsv").read_text().splitlines()
    assert rows[0] == "id\tdate\tchannel\tquestion\turl"
    assert rows[1].split("\t")[:3] == ["Q1", "2026-08-02", "manual"]
    assert rows[2].split("\t")[1] == ""
    assert len(rows) == 3


def test_questions_from_evidence_json(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps({"conversations": [
        {"id": "C2", "created_at": "2026-08-09T10:00:00Z", "channel": "chat",
         "first_message": "Waar   vind ik de factuur?", "url": "https://example.test/2"},
        {"id": "C1", "created_at": "2026-08-01T10:00:00Z", "channel": "email",
         "subject": "reminder", "first_raw": "De herinnering kwam niet aan."},
        {"id": "C3", "created_at": "2026-08-11T10:00:00Z", "channel": "email",
         "first_message": "out of office", "noise": True},
    ]}), encoding="utf-8")
    out = tmp_path / "out"
    done = run("questions.py", "--from", str(source), "--out", str(out))
    assert done.returncode == 0, done.stderr
    rows = [line.split("\t") for line in (out / "questions.tsv").read_text().splitlines()[1:]]
    assert [row[0] for row in rows] == ["Q1", "Q2"]
    assert rows[0][3] == "De herinnering kwam niet aan."
    assert rows[1][3] == "Waar vind ik de factuur?"
    assert rows[1][4] == "https://example.test/2"


# --------------------------------------------------------------------- collect

def test_collect_reduce(tmp_path: Path) -> None:
    out = tmp_path / "out"
    done = run("collect.py", "--from", str(FIXTURES / "schema.json"), "--out", str(out))
    assert done.returncode == 0, done.stderr
    text = (out / "schema.md").read_text()
    lines = {line.split(" · ")[0]: line for line in text.splitlines()}

    assert "tenant candidates: user_id (6 tables)" in text
    assert "settings" in lines["settings"].split(" · ")[3]
    assert lines["logging"].split(" · ")[3] == "log"
    assert lines["reminder_jobs"].split(" · ")[3] == "log"
    assert "soft-delete:deleted" in lines["agenda"]
    assert "enum:status=open (70%)" in lines["agenda"]
    assert "ts:time_added" in lines["remarks"]
    assert "exact rows" in lines["settings"]  # counted, not estimated
    assert "views (1): v_agenda_today" in text
    assert "budget: 41 queries" in text


# ----------------------------------------------------------------------- probe

TABLES = [
    {"table_name": "users", "table_type": "BASE TABLE", "engine": "InnoDB", "table_rows": 100,
     "data_length": 1024, "index_length": 512, "table_comment": "", "create_time": None,
     "update_time": None},
    {"table_name": "agenda", "table_type": "BASE TABLE", "engine": "InnoDB", "table_rows": 500,
     "data_length": 4096, "index_length": 1024, "table_comment": "", "create_time": None,
     "update_time": None},
]
COLUMNS = [
    ("users", "id", 1, "int(11)"), ("users", "email", 2, "varchar(32)"),
    ("users", "active", 3, "tinyint(1)"),
    ("agenda", "id", 1, "int(11)"), ("agenda", "user_id", 2, "int(11)"),
    ("agenda", "status", 3, "varchar(16)"), ("agenda", "deleted", 4, "tinyint(1)"),
]


def fake_query(sql: str, db: str | None = None) -> list[dict]:
    low = sql.lower()
    if "version()" in low:
        return [{"version()": "5.6.51-log"}]
    if "information_schema.tables" in low:
        return list(TABLES)
    if "information_schema.columns" in low:
        return [{"table_name": t, "column_name": c, "ordinal_position": p, "column_type": k,
                 "is_nullable": "YES", "column_default": None, "column_key": "", "extra": "",
                 "column_comment": ""} for t, c, p, k in COLUMNS]
    if "key_column_usage" in low:
        return [{"table_name": "agenda", "constraint_name": "fk_agenda_user",
                 "column_name": "user_id", "ordinal_position": 1,
                 "referenced_table_name": "users", "referenced_column_name": "id"}]
    if "table_constraints" in low:
        return [{"table_name": "agenda", "constraint_name": "fk_agenda_user",
                 "constraint_type": "FOREIGN KEY"}]
    if "information_schema.statistics" in low:
        return [{"table_name": "agenda", "index_name": "PRIMARY", "non_unique": 0,
                 "seq_in_index": 1, "column_name": "id"}]
    if "count(distinct" in low:
        return [{"c": 500, "d": 42}]
    if "group by" in low:
        if "`email`" in sql:
            raise AssertionError("a PII column was sampled")
        if "`status`" in sql:  # 13 groups is one too many to be an enum
            return [{"v": f"s{n}", "c": 100 - n} for n in range(13)]
        return [{"v": "0", "c": 480}, {"v": "1", "c": 20}]
    if "database()" in low:
        return [{"database()": "salonx_staging"}]
    raise AssertionError(f"unexpected query: {sql}")


def load_probe():
    lib = types.ModuleType("lib")
    database = types.ModuleType("lib.db")
    database.query = fake_query
    lib.db = database
    sys.modules["lib"] = lib
    sys.modules["lib.db"] = database
    return load("probe.py", "probe_under_test")


def test_probe_offline() -> None:
    probe = load_probe()
    data = probe.collect(dict(probe.CONFIG))
    assert set(data) == {"engine", "version", "database", "db", "collected_at",
                         "tenant_candidates", "tables", "columns", "keys", "indexes",
                         "profiles", "counts", "budget"}
    assert data["engine"] == "mysql"
    assert data["tenant_candidates"][0] == {"column": "user_id", "tables": 1}
    assert {(p["table"], p["column"]) for p in data["profiles"]} == {
        ("agenda", "deleted"), ("users", "active")}  # email is PII, status has 13 groups
    assert data["counts"] == [{"table": "agenda", "rows": 500, "tenants": 42}]
    assert data["budget"]["queries"] > 0

    capped = probe.collect({**probe.CONFIG, "max_profile_per_table": 1})
    assert [(p["table"], p["column"]) for p in capped["profiles"]] == [("users", "active")]


# ----------------------------------------------------------------------- query

def test_query_guards() -> None:
    query = load("query.py", "query_under_test")
    ok = [
        "select id, status from agenda where user_id = 4 limit 20",
        "with recent as (select id from agenda limit 10) select * from recent limit 10",
        "select table_name from information_schema.tables where table_schema = 'salonx'",
        "show create table agenda",
        "describe agenda",
    ]
    for sql in ok:
        assert query.guard(sql) == (None, None), sql
    bad = {
        "update agenda set status = 'open' where id = 1": "not read-only",
        "select id from agenda limit 10; select 1 limit 1": "more than one statement",
        "select id from agenda": "no LIMIT",
        "select id from agenda limit 5000": "over the cap",
        "select * from agenda limit 100": "select *",
        "select id from agenda limit 10 for update": "for update",
    }
    for sql, expected in bad.items():
        refusal, _ = query.guard(sql)
        assert refusal and expected in refusal, (sql, refusal)

    refusal, warning = query.guard("select id from agenda", limit_ok=True)
    assert refusal is None and warning and "--limit-ok" in warning


def test_query_dry_run(tmp_path: Path) -> None:
    done = run("query.py", "--db", "staging", "--dry-run",
               "select id from agenda where user_id = 4 limit 5")
    assert done.returncode == 0, done.stderr
    assert "from lib import db" in done.stdout
    assert "limit 5" in done.stdout
    assert not (tmp_path / "drills.log").exists()

    refused = run("query.py", "--db", "staging", "--dry-run", "delete from agenda")
    assert refused.returncode == 2
    assert "refused" in refused.stderr


def test_query_tsv() -> None:
    query = load("query.py", "query_under_test")
    rows = [{"id": 1, "status": "open"}, {"id": 2, "status": None}]
    assert query.as_tsv(rows).splitlines() == ["id\tstatus", "1\topen", "2\tNone"]
    assert query.as_tsv([]) == ""


# --------------------------------------------------------------------- context

def test_context_markdown() -> None:
    context = load("context.py", "context_under_test")
    listed = [{"id": "SALONX_STAGING_DSN", "description": "SalonX staging, restore of prod"}]
    capabilities = {
        "databases": [{"name": "staging", "env": "SALONX_STAGING_DSN", "pii_masked": False,
                       "scoped": True, "description": "SalonX staging, restore of prod"}],
    }
    repos = [{"name": "app", "default_branch": "main", "description": "Laravel monolith"}]
    files = [("skills/tenant-model.md", 24), ("AGENTS.md", 12)]
    text = context.build("salonx", listed, capabilities, repos, files, [])
    assert "# what a run of salonx receives" in text
    assert "- `staging` (tenant scoped): SalonX staging, restore of prod" in text
    assert "- `/mirrors/app` (main): Laravel monolith" in text
    assert "- skills: 1 files, 24 lines" in text
    assert "  - AGENTS.md (12)" in text


def test_context_survives_missing_keys() -> None:
    context = load("context.py", "context_under_test")
    text = context.build("salonx", None, {"databases": []}, [], [],
                         ["`rp project database ls` was not available."])
    assert "was not available" in text
    assert "- none: a run has no database" in text
    assert "- none: a run reads no source code" in text
    assert "no tracked markdown yet" in text
