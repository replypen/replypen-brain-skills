#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""One guarded read-only drill against a grounding database, from your laptop.

    uv run skills/brain-grounding-intake/scripts/query.py --db staging "select ... limit 20"
    uv run skills/brain-grounding-intake/scripts/query.py --db staging --file drill.sql
    echo "select 1 limit 1" | uv run .../query.py --db staging

The statement is checked here first, then shipped to the workspace through `rp dev console bash
run -` and run with the injected `lib.db`. Rows come back as TSV, and every statement is
appended to `$OUT/drills.log`, so the intake carries the audit trail of what you touched.

Guards, all before anything is sent: one statement, read-only verbs only, a `LIMIT` of at most
200 on any select that is not reading `information_schema` (`select *` at most 30).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import console_bash  # noqa: E402
from scan import find_brain_root  # noqa: E402

READ_VERBS = ("select", "with", "explain", "show", "describe", "desc")
CATALOGS = ("information_schema", "pg_stat", "pg_stats", "pg_class", "pg_catalog",
            "performance_schema")
FORBIDDEN = ("into outfile", "into dumpfile", "for update", "lock ")
MAX_LIMIT = 200
MAX_STAR_LIMIT = 30
LIMIT = re.compile(r"\blimit\s+(\d+)", re.I)
STAR = re.compile(r"\bselect\s+\*", re.I)


def guard(sql: str, limit_ok: bool = False) -> tuple[str | None, str | None]:
    """(refusal, warning). A refusal means nothing is sent to the box."""
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        return "empty statement", None
    if ";" in statement:
        return "more than one statement; send one select at a time", None
    low = statement.lower()
    verb = low.split(None, 1)[0]
    if verb not in READ_VERBS:
        return (f"{verb!r} is not read-only; this drill runs select, with, explain, show and "
                "describe only"), None
    for bad in FORBIDDEN:
        if bad in low:
            return f"the statement contains {bad.strip()!r}, which is not a read", None
    if verb not in ("select", "with"):
        return None, None
    if any(catalog in low for catalog in CATALOGS):
        return None, None
    hit = LIMIT.search(low)
    if not hit:
        if limit_ok:
            return None, "no LIMIT, running it anyway because --limit-ok was passed"
        return "no LIMIT; add `limit 200` or less, or pass --limit-ok deliberately", None
    limit = int(hit.group(1))
    if limit > MAX_LIMIT:
        if limit_ok:
            return None, f"LIMIT {limit} is over {MAX_LIMIT}, running it anyway (--limit-ok)"
        return f"LIMIT {limit} is over the cap of {MAX_LIMIT}; narrow it or pass --limit-ok", None
    if STAR.search(low) and limit > MAX_STAR_LIMIT:
        if limit_ok:
            return None, f"select * with LIMIT {limit}, running it anyway (--limit-ok)"
        return (f"`select *` with LIMIT {limit}; name the columns, or keep the limit at "
                f"{MAX_STAR_LIMIT} or less"), None
    return None, None


def remote_command(sql: str, db: str) -> str:
    """The python heredoc `rp dev console bash run -` executes on the box."""
    payload = json.dumps({"sql": sql, "db": db})
    return ("python3 - <<'RCQUERY'\n"
            "import json\n"
            "from lib import db\n"
            f"args = json.loads({payload!r})\n"
            "rows = db.query(args['sql'], db=args['db'])\n"
            "print(json.dumps([dict(row) for row in rows], default=str))\n"
            "RCQUERY\n")


def as_tsv(rows: list[dict]) -> str:
    if not rows:
        return ""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    out = ["\t".join(columns)]
    for row in rows:
        out.append("\t".join(str(row.get(column, "")).replace("\t", " ").replace("\n", " ")
                             for column in columns))
    return "\n".join(out)


def log(out_dir: Path, db: str, sql: str, rows: int | str, seconds: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    line = f"{stamp}\t{db}\t{rows}\t{seconds:.1f}\t{' '.join(sql.split())}\n"
    with (out_dir / "drills.log").open("a", encoding="utf-8") as handle:
        handle.write(line)


def read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.file:
        return args.file.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One guarded read-only drill on a grounding database")
    parser.add_argument("sql", nargs="?", help="the statement; or --file, or stdin")
    parser.add_argument("--db", required=True, help="the short database key, as `rp project database ls`")
    parser.add_argument("--file", type=Path, help="read the statement from a file")
    parser.add_argument("--out", type=Path,
                        help="output directory, default .replypen/grounding-intake/<date>")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--limit-ok", action="store_true", dest="limit_ok",
                        help="run a select without a small LIMIT, deliberately")
    parser.add_argument("--dry-run", action="store_true", help="print the remote command and stop")
    args = parser.parse_args(argv)

    sql = read_sql(args).strip().rstrip(";").strip()
    refusal, warning = guard(sql, args.limit_ok)
    if refusal:
        print(f"query: refused: {refusal}", file=sys.stderr)
        return 2
    if warning:
        print(f"query: warn: {warning}", file=sys.stderr)
    if args.dry_run:
        print(remote_command(sql, args.db))
        return 0

    brain_root = find_brain_root()
    out_dir = Path(args.out or brain_root / ".replypen" / "grounding-intake"
                   / date.today().isoformat()).expanduser()
    started = time.monotonic()
    try:
        stdout = console_bash(brain_root, remote_command(sql, args.db), args.timeout, label="query",
                              timeout_hint="A drill that does not return in time is the query's "
                                           "problem, not the limit's: add a filter or a smaller "
                                           "LIMIT, or read information_schema instead.")
    except SystemExit:
        log(out_dir, args.db, sql, "failed", time.monotonic() - started)  # failures are audit trail too
        raise
    seconds = time.monotonic() - started
    try:
        rows = json.loads(stdout.strip() or "[]")
    except ValueError:
        print(stdout, file=sys.stderr)
        print("query: the box returned no JSON; see its output above", file=sys.stderr)
        return 1

    text = as_tsv(rows)
    if text:
        print(text)
    log(out_dir, args.db, sql, len(rows), seconds)
    print(f"{len(rows)} rows · {seconds:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
