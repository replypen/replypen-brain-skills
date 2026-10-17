#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Ship `probe.py` to the box, fetch its JSON, reduce it to the read-whole `schema.md`.

    uv run skills/brain-grounding-intake/scripts/collect.py --db staging [--out DIR]
    uv run skills/brain-grounding-intake/scripts/collect.py --from schema.json --out DIR

`rp dev console database query` refuses MySQL today, so the schema is read by a Python script
running on the box through `rp dev console bash run -`, which writes JSON under
`/tmp/replypen-out/` that `rp dev console file get` fetches. `--from` skips the console and
only reduces a JSON you already have; `--dry-run` prints the remote command and stops.

Read-only: information_schema, then bounded value samples. Nothing is written anywhere but
`/tmp` on the box and `$OUT` on your laptop.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan import _marker, find_brain_root, read_toml  # noqa: E402


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


PROBE = Path(__file__).resolve().parent / "probe.py"
REMOTE_DIR = "/tmp/replypen-out"
LOG_NAME = re.compile(r"log|logging|history|jobs|queue|tracker|statistic|temp|cache|session"
                      r"|_deleted|archive", re.I)
LOG_COLUMNS = ("already_sent", "processed_at", "sent_at")
TIME_COLUMNS = ("created_at", "updated_at", "time_added", "date_added", "timestamp",
                "datum", "date", "time")
SOFT_DELETE = ("deleted", "deleted_at", "is_deleted", "removed", "archived", "active")
TIMESTAMP_STYLES = ("created_at", "updated_at", "time_added", "date_added", "timestamp")
LOOKUP_ROWS = 200
MAX_ENUM_VALUES = 3


# ----------------------------------------------------------------- the console

def remote_command(db: str, out: str, overrides: dict) -> str:
    """The probe with its CONFIG line rewritten, wrapped in a heredoc for `bash run -`."""
    source = PROBE.read_text(encoding="utf-8")
    config = {"db": db, "out": out, "profile_max_rows": 3000000, "max_profile_queries": 120,
              "max_profile_per_table": 6, "max_count_queries": 40, "count_max_rows": 200000,
              "wall_seconds": 600, "rows_per_page": 5000}
    config.update({key: value for key, value in overrides.items() if value is not None})
    line = "CONFIG = " + json.dumps(config)
    source = re.sub(r"^CONFIG = \{.*\}$", lambda _: line, source, count=1, flags=re.M)
    if line not in source:
        raise SystemExit("collect: probe.py has no single-line CONFIG = {...} to replace")
    return f"mkdir -p {REMOTE_DIR}\npython3 - <<'RCPROBE'\n{source}\nRCPROBE\n"


def console_bash(brain_root: Path, command: str, timeout: int, label: str = "collect",
                 timeout_hint: str = "Rerun with a larger --timeout, or a smaller "
                                     "--profile-max-rows.") -> str:
    """Run one bash payload on the box, return its stdout. `query.py` reuses this."""
    proc = subprocess.run(
        [_cli(), "-o", "json", "dev", "console", "bash", "run", "--timeout", str(timeout), "-"],
        input=command, cwd=str(brain_root), text=True, capture_output=True, check=False)
    try:
        result = json.loads(proc.stdout or "{}")
    except ValueError:
        result = {}
    if proc.returncode != 0 and not result:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"{label}: `rp dev console bash run` failed (exit {proc.returncode})")
    if result.get("timed_out"):
        raise SystemExit(f"{label}: hit the {timeout}s console timeout. {timeout_hint}")
    if result.get("exit_code"):
        sys.stderr.write(str(result.get("stderr") or ""))
        raise SystemExit(f"{label}: the remote script exited {result['exit_code']} on the box")
    return str(result.get("stdout") or "")


def console_get(brain_root: Path, remote: str, local: Path) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([_cli(), "dev", "console", "file", "get", remote, "--out", str(local)],
                          cwd=str(brain_root), text=True, capture_output=True, check=False)
    if proc.returncode != 0 or not local.is_file():
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"collect: could not fetch {remote} from the box")


# ------------------------------------------------------------------- reduction

def index_facts(schema: dict) -> dict:
    columns: dict[str, list[dict]] = {}
    for column in schema.get("columns") or []:
        columns.setdefault(column["table"], []).append(column)
    profiles: dict[str, list[dict]] = {}
    for profile in schema.get("profiles") or []:
        profiles.setdefault(profile["table"], []).append(profile)
    counts = {row["table"]: row for row in schema.get("counts") or []}
    unique: dict[tuple[str, str], bool] = {}
    per_index: dict[tuple[str, str], list[dict]] = {}
    for entry in schema.get("indexes") or []:
        per_index.setdefault((entry["table"], entry["index"]), []).append(entry)
    for (table, _), parts in per_index.items():
        if len(parts) == 1 and not parts[0].get("non_unique"):
            unique[(table, parts[0]["column"])] = True
    fk_out: dict[str, int] = {}
    fk_in: dict[str, int] = {}
    for key in schema.get("keys") or []:
        if key.get("referenced_table"):
            fk_out[key["table"]] = fk_out.get(key["table"], 0) + 1
            fk_in[key["referenced_table"]] = fk_in.get(key["referenced_table"], 0) + 1
    return {"columns": columns, "profiles": profiles, "counts": counts, "unique": unique,
            "fk_out": fk_out, "fk_in": fk_in}


def guess_role(table: dict, names: list[str], count: dict | None,
               tenant: str, unique: dict, rows: int) -> str:
    name = table["table"]
    if LOG_NAME.search(name):
        return "log"
    if any(column in names for column in LOG_COLUMNS) and any(c in names for c in TIME_COLUMNS):
        return "log"
    if tenant and tenant in names:
        if unique.get((name, tenant)):
            return "settings"
        if count and count["rows"] and count["rows"] == count["tenants"]:
            return "settings"
    id_columns = [column for column in names if column.endswith("_id")]
    if 2 <= len(names) <= 3 and len(id_columns) == len(names):
        return "link"
    if rows <= LOOKUP_ROWS:
        return "lookup"
    return "core"


def table_flags(table: dict, names: list[str], facts: dict) -> list[str]:
    name = table["table"]
    flags = []
    soft = next((column for column in SOFT_DELETE if column in names), "")
    if soft:
        flags.append(f"soft-delete:{soft}")
    style = next((column for column in TIMESTAMP_STYLES if column in names), "none")
    flags.append(f"ts:{style}")
    out, into = facts["fk_out"].get(name, 0), facts["fk_in"].get(name, 0)
    if out or into:
        flags.append(f"fk-out:{out} fk-in:{into}")
    for profile in facts["profiles"].get(name, []):
        total = sum(item["c"] for item in profile["values"]) or 1
        shown = profile["values"][:MAX_ENUM_VALUES]
        parts = ", ".join(f"{item['v']} ({round(100 * item['c'] / total)}%)" for item in shown)
        flags.append(f"enum:{profile['column']}={parts}")
    if table.get("comment"):
        flags.append(f"comment:{table['comment'][:80]}")
    return flags


def table_facts(schema: dict) -> list[dict]:
    """One row per base table, sorted by size, with the role guess and the flags."""
    facts = index_facts(schema)
    candidates = schema.get("tenant_candidates") or []
    tenant = candidates[0]["column"] if candidates else ""
    rows = []
    for table in schema.get("tables") or []:
        if "VIEW" in str(table.get("type") or "").upper():
            continue
        name = table["table"]
        names = [column["column"] for column in facts["columns"].get(name, [])]
        count = facts["counts"].get(name)
        row_count = count["rows"] if count else int(table.get("rows") or 0)
        rows.append({
            "table": name,
            "rows": row_count,
            "exact": bool(count),
            "mb": round((int(table.get("data_length") or 0)
                         + int(table.get("index_length") or 0)) / 1048576, 1),
            "role": guess_role(table, names, count, tenant, facts["unique"], row_count),
            "tenant": tenant if tenant and tenant in names else "",
            "flags": table_flags(table, names, facts),
        })
    rows.sort(key=lambda row: (-row["rows"], row["table"]))
    return rows


def schema_markdown(schema: dict, rows: list[dict]) -> str:
    candidates = schema.get("tenant_candidates") or []
    tenant = candidates[0]["column"] if candidates else ""
    keys = [key for key in schema.get("keys") or [] if key.get("referenced_table")]
    views = [table["table"] for table in schema.get("tables") or []
             if "VIEW" in str(table.get("type") or "").upper()]
    out = [
        f"# schema {schema.get('db') or schema.get('database')} "
        f"({schema.get('engine')} {schema.get('version')})",
        "",
        f"database {schema.get('database')} · {len(rows)} base tables · "
        f"{len(schema.get('columns') or [])} columns · {len(keys)} foreign key columns · "
        f"views {len(views)} · collected {schema.get('collected_at')}",
    ]
    if candidates:
        out.append("tenant candidates: " + " · ".join(
            f"{item['column']} ({item['tables']} tables)" for item in candidates))
    if tenant:
        without = [row["table"] for row in rows if not row["tenant"]]
        out.append(f"tables without {tenant} ({len(without)}): "
                   + (", ".join(without) if without else "none"))
    out += ["", "Per table: rows · size · role · tenant column · flags. "
                "Roles are a guess from names, indexes and counts, not a contract.", ""]
    for row in rows:
        exact = " exact" if row["exact"] else ""
        parts = [f"{row['table']}", f"{row['rows']}{exact} rows", f"{row['mb']} MB",
                 row["role"], row["tenant"] or "no tenant col", *row["flags"]]
        out.append(" · ".join(parts))
    if views:
        out += ["", f"views ({len(views)}): " + ", ".join(views)]
    budget = schema.get("budget") or {}
    out += ["", f"budget: {budget.get('queries', 0)} queries in {budget.get('seconds', 0)}s, "
                f"{len(budget.get('skipped') or [])} skipped"]
    for item in (budget.get("skipped") or [])[:20]:
        where = ".".join(part for part in (item.get("table"), item.get("column")) if part)
        out.append(f"skipped {where or '(unknown)'}: {item.get('reason')}")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect and reduce one grounding schema")
    parser.add_argument("--db", help="the short database key, as `rp dev console database list`")
    parser.add_argument("--out", type=Path,
                        help="output directory, default .replypen/grounding-intake/<date>")
    parser.add_argument("--from", dest="source", type=Path,
                        help="an already fetched schema.json; skips the console")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--profile-max-rows", type=int, dest="profile_max_rows")
    parser.add_argument("--dry-run", action="store_true", help="print the remote command and stop")
    args = parser.parse_args(argv)

    brain_root = find_brain_root()
    out_dir = Path(args.out or brain_root / ".replypen" / "grounding-intake"
                   / date.today().isoformat()).expanduser()

    if args.dry_run:
        db = args.db or "staging"
        print(remote_command(db, f"{REMOTE_DIR}/schema-{db}.json",
                             {"profile_max_rows": args.profile_max_rows}))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "schema.json"
    if args.source:
        target.write_text(args.source.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        if not args.db:
            print("collect: pass --db <key> (see `rp dev console database list`), "
                  "or --from schema.json", file=sys.stderr)
            return 2
        remote = f"{REMOTE_DIR}/schema-{args.db}.json"
        print(console_bash(brain_root, remote_command(args.db, remote,
                                                      {"profile_max_rows": args.profile_max_rows}),
                           args.timeout).strip())
        console_get(brain_root, remote, target)

    schema = json.loads(target.read_text(encoding="utf-8"))
    schema.setdefault("project", str(read_toml(_marker(brain_root)).get("project")
                                     or brain_root.name))
    target.write_text(json.dumps(schema, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    rows = table_facts(schema)
    (out_dir / "schema.md").write_text(schema_markdown(schema, rows), encoding="utf-8")
    counted = sum(1 for row in rows if row["exact"])
    print(f"{len(rows)} tables ({counted} counted exactly), "
          f"{len(schema.get('profiles') or [])} value profiles -> schema.md")
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
