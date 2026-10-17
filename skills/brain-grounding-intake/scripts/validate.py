#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Validate one grounding-intake output directory, then assemble `intake.json`.

    uv run skills/brain-grounding-intake/scripts/validate.py OUT_DIR

Exit 0 plus a one-line summary and the assembled `intake.json` (all `render.py` reads), or
exit 1 plus one actionable line per problem, each prefixed with the file to fix
(`benchmark.tsv: Q3: ...`) so the next edit is a small targeted rewrite. `warn:` lines are
steering and never fail.

Both evidence halves are optional on their own, at least one must be there: a project can have
a readable database and no readable code, or the reverse. Every locator you write is held
against the evidence that is present: `/mirrors/...` against the listings, `db:table.column`
against `schema.json`, `brain:path` against the brain checkout on disk.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake_common import (  # noqa: E402
    check_markdown_hygiene, frontmatter_description, read_tsv, split_list,
)
from scan import _marker, find_brain_root  # noqa: E402


STATUSES = ("grounded", "ambiguous", "missing", "knowledge", "human")
GROUPS = ("architecture", "where", "data", "settings", "queues", "conventions", "ownership")
MAX_DEVQUESTIONS = 40
WARN_DEVQUESTIONS = 25
MAX_PROPOSAL_FILES = 24
MAX_PROPOSAL_LINES = 150
MAX_LINE_CHARS = 400
MAX_HEADLINE_LINES = 3
Q_COLUMNS = ("id", "date", "channel", "question", "url")
B_COLUMNS = ("id", "cluster", "status", "where", "note")
D_COLUMNS = ("id", "group", "question", "proposal", "evidence", "impact")
TABLE_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]+$")


# ------------------------------------------------------------------ the evidence

class Listings:
    """The `raw/listing-<name>.txt` files, one per repo in scan.json."""

    def __init__(self, out: Path, repos: list[dict], errors: list[str]) -> None:
        self.files: dict[str, set[str]] = {}
        self.dirs: dict[str, set[str]] = {}
        for repo in repos:
            name = str(repo.get("name") or "")
            path = out / "raw" / f"listing-{name}.txt"
            if not path.is_file():
                errors.append(f"raw/listing-{name}.txt: missing: scan.json lists repo {name!r} "
                              "but its listing is not there; rerun scan.py")
                continue
            rels = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()}
            self.files[name] = rels
            dirs: set[str] = set()
            for rel in rels:
                parts = rel.split("/")[:-1]
                for index in range(1, len(parts) + 1):
                    dirs.add("/".join(parts[:index]))
            self.dirs[name] = dirs

    def check(self, locator: str) -> str | None:
        """None when the locator resolves, else the reason it does not."""
        if not locator.startswith("/mirrors/"):
            return f"{locator!r} is not a /mirrors/<repo>/<path> locator"
        rest = locator[len("/mirrors/"):]
        name, _, rel = rest.partition("/")
        if name not in self.files:
            return f"{locator!r} names repo {name!r}, which scan.json does not know"
        rel = rel.split(":", 1)[0].rstrip("/")
        if not rel:
            return f"{locator!r} points at the repo root, not at a file"
        if rel in self.files[name] or rel in self.dirs[name]:
            return None
        return f"{locator!r} is not in raw/listing-{name}.txt"

    def is_dir(self, locator: str) -> bool:
        rest = locator[len("/mirrors/"):] if locator.startswith("/mirrors/") else ""
        name, _, rel = rest.partition("/")
        rel = rel.split(":", 1)[0].rstrip("/")
        return name in self.dirs and rel not in self.files.get(name, ()) and rel in self.dirs[name]


class Schema:
    """Every table and column the probe actually saw."""

    def __init__(self, data: dict) -> None:
        self.tables = {str(table["table"]) for table in data.get("tables") or []}
        self.columns: dict[str, set[str]] = {}
        for column in data.get("columns") or []:
            self.columns.setdefault(str(column["table"]), set()).add(str(column["column"]))
        self.column_names = {name for names in self.columns.values() for name in names}

    def check(self, locator: str) -> str | None:
        """None when `table` or `table.column` resolves, else the reason it does not."""
        table, _, column = locator.partition(".")
        table, column = table.strip(), column.strip()
        if not table:
            return f"{locator!r} names no table"
        if table not in self.tables:
            return f"{locator!r} names table {table!r}, which is not in schema.json"
        if column and column not in self.columns.get(table, set()):
            return f"{locator!r}: table {table!r} has no column {column!r}"
        return None


class Locators:
    """The three locator kinds, each held against the evidence that is present."""

    def __init__(self, listings: Listings, schema: Schema | None, brain_root: Path | None,
                 had_scan: bool) -> None:
        self.listings = listings
        self.schema = schema
        self.brain_root = brain_root
        self.had_scan = had_scan

    def kind(self, locator: str) -> str:
        if locator.startswith("db:"):
            return "db"
        if locator.startswith("brain:"):
            return "brain"
        return "mirrors"

    def check(self, locator: str) -> tuple[str | None, str | None]:
        """(error, warning); either may be None."""
        if locator.startswith("db:"):
            rest = locator[3:].strip()
            if not rest:
                return f"{locator!r} names no table", None
            if self.schema is None:
                return None, (f"{locator!r} could not be checked: this intake has no schema.json")
            problem = self.schema.check(rest)
            return (f"{locator!r}: {problem}" if problem else None), None
        if locator.startswith("brain:"):
            rel = locator[6:].strip()
            if not rel:
                return f"{locator!r} names no file", None
            if self.brain_root is None:
                return None, (f"{locator!r} could not be checked: no .replypen.toml above "
                              "the output directory, so the brain root is unknown")
            if not (self.brain_root / rel).exists():
                return f"{locator!r} is not a file of the brain checkout", None
            return None, None
        if not self.had_scan:
            return None, (f"{locator!r} could not be checked: this intake has no scan.json")
        return self.listings.check(locator), None


# ---------------------------------------------------------------------- checks

def check_benchmark(rows: list[dict], questions: list[dict], locators: Locators,
                    errors: list[str], warnings: list[str]) -> None:
    known = {row["id"] for row in questions}
    seen: set[str] = set()
    for row in rows:
        qid, where = row["id"], split_list(row["where"])
        if qid not in known:
            errors.append(f"benchmark.tsv: {qid}: unknown question id, not in questions.tsv")
        elif qid in seen:
            errors.append(f"benchmark.tsv: {qid}: listed twice, one row per question")
        seen.add(qid)
        if not row["cluster"]:
            errors.append(f"benchmark.tsv: {qid}: empty cluster, every row needs a type slug")
        status = row["status"]
        if status not in STATUSES:
            errors.append(f"benchmark.tsv: {qid}: status {status!r} is not one of "
                          f"{', '.join(STATUSES)}")
            continue
        if status == "grounded" and len(where) < 1:
            errors.append(f"benchmark.tsv: {qid}: status grounded needs at least one locator "
                          "in `where`")
        if status == "ambiguous" and len(where) < 2:
            errors.append(f"benchmark.tsv: {qid}: status ambiguous needs two or more locators "
                          f"in `where`, got {len(where)}")
        if status in ("missing", "human") and where:
            errors.append(f"benchmark.tsv: {qid}: status {status} takes no locators, "
                          f"got {len(where)}")
        if status == "knowledge":
            other = [item for item in where if locators.kind(item) != "brain"]
            if other:
                errors.append(f"benchmark.tsv: {qid}: status knowledge takes brain: locators "
                              f"only, got {other[0]!r}; if a run must look it up, it is not "
                              "knowledge")
        for locator in where:
            problem, warning = locators.check(locator)
            if problem:
                errors.append(f"benchmark.tsv: {qid}: {problem}")
            if warning:
                warnings.append(f"benchmark.tsv: {qid}: {warning}")
            if not problem and status == "grounded" and locators.listings.is_dir(locator):
                warnings.append(f"benchmark.tsv: {qid}: {locator} is a directory, not one "
                                "grounding place; point at the file")
        if status in ("ambiguous", "missing") and not row["note"]:
            warnings.append(f"benchmark.tsv: {qid}: {status} with an empty note; say what "
                            "exactly is unknown, that sentence becomes the dev question")
    for row in questions:
        if row["id"] not in seen:
            errors.append(f"benchmark.tsv: {row['id']}: missing, every question needs a row")


def check_devquestions(rows: list[dict], benchmark: list[dict], locators: Locators,
                       errors: list[str], warnings: list[str]) -> None:
    if len(rows) > MAX_DEVQUESTIONS:
        errors.append(f"devquestions.tsv: {len(rows)} questions, the cap is {MAX_DEVQUESTIONS}")
    if len(rows) > WARN_DEVQUESTIONS:
        warnings.append(f"devquestions.tsv: {len(rows)} questions is a lot to ask a dev; "
                        f"under {WARN_DEVQUESTIONS} gets answered")
    clusters = {row["cluster"] for row in benchmark if row["cluster"]}
    seen: set[str] = set()
    cited: set[str] = set()
    for row in rows:
        did = row["id"] or f"line {row['_line']}"
        if did in seen:
            errors.append(f"devquestions.tsv: {did}: listed twice")
        seen.add(did)
        if row["group"] not in GROUPS:
            errors.append(f"devquestions.tsv: {did}: group {row['group']!r} is not one of "
                          f"{', '.join(GROUPS)}")
        if not row["question"]:
            errors.append(f"devquestions.tsv: {did}: empty question")
        if not row["impact"]:
            errors.append(f"devquestions.tsv: {did}: empty impact, say in one sentence what a "
                          "run gets wrong today without the answer")
        evidence = split_list(row["evidence"])
        if not evidence:
            errors.append(f"devquestions.tsv: {did}: empty evidence, cite the benchmark cluster "
                          "or a locator this question is about")
        for item in evidence:
            if item in clusters:
                continue
            problem, warning = locators.check(item)
            if problem:
                errors.append(f"devquestions.tsv: {did}: {problem}, and it is no benchmark "
                              "cluster either")
            if warning:
                warnings.append(f"devquestions.tsv: {did}: {warning}")
        cited.update(evidence)
    open_clusters: dict[str, set[str]] = {}
    for row in benchmark:
        if row["status"] in ("ambiguous", "missing"):
            open_clusters.setdefault(row["cluster"], set()).update(split_list(row["where"]))
    for cluster, cluster_locators in sorted(open_clusters.items()):
        if cluster not in cited and not (cluster_locators & cited):
            errors.append(f"devquestions.tsv: cluster {cluster!r}: ambiguous or missing in "
                          "benchmark.tsv but no dev question cites it")


def check_proposal(directory: Path, locators: Locators, had_scan: bool, db: str,
                   errors: list[str], warnings: list[str]) -> dict[str, str]:
    """Every `*.md` under proposal/, keyed by its relative path (`codebase/INDEX.md`)."""
    proposal: dict[str, str] = {}
    if not directory.is_dir():
        errors.append("proposal/: missing, write the proposed brain files there "
                      "(codebase/INDEX.md plus one file per area, databases/<db>.md)")
        return proposal
    files = sorted(directory.rglob("*.md"))
    if not files:
        errors.append("proposal/: no markdown file at all, it is the point of this intake")
    if had_scan and not (directory / "codebase" / "INDEX.md").is_file():
        errors.append("proposal/codebase/INDEX.md: missing, it routes customer symptoms to the "
                      "area files")
    if locators.schema is not None and db and not (directory / "databases" / f"{db}.md").is_file():
        errors.append(f"proposal/databases/{db}.md: missing, it is the map a run reads first "
                      "for this database")
    if len(files) > MAX_PROPOSAL_FILES:
        errors.append(f"proposal/: {len(files)} files, the cap is {MAX_PROPOSAL_FILES}")
    for path in files:
        rel = path.relative_to(directory).as_posix()
        label = f"proposal/{rel}"
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        proposal[rel] = text
        if not frontmatter_description(text):
            errors.append(f"{label}: line 1: needs YAML frontmatter with a `description:` line")
        check_markdown_hygiene(label, text, errors, MAX_PROPOSAL_LINES, MAX_LINE_CHARS, warnings)
        check_proposal_tokens(label, rel, text, locators, errors, warnings)
    return proposal


def check_proposal_tokens(label: str, rel: str, text: str, locators: Locators,
                          errors: list[str], warnings: list[str]) -> None:
    """Backtick tokens: mirror paths everywhere, tables everywhere, bare names in databases/."""
    in_databases = rel.startswith("databases/")
    paths = 0
    named_tables = 0
    for raw in re.findall(r"`([^`]+)`", text):
        token = raw.strip()
        if token.startswith("/mirrors/"):
            paths += 1
            problem = locators.listings.check(token) if locators.had_scan else None
            if problem:
                errors.append(f"{label}: {problem}")
            continue
        if locators.schema is None:
            continue
        if TABLE_COLUMN.match(token):
            table = token.partition(".")[0]
            if not in_databases and table not in locators.schema.tables:
                continue  # a file name such as `booking.md` or `m_agenda.php`
            named_tables += 1
            problem = locators.schema.check(token)
            if problem:
                errors.append(f"{label}: {problem}")
        elif in_databases and IDENTIFIER.match(token):
            if token in locators.schema.tables:
                named_tables += 1
            elif token not in locators.schema.column_names:
                errors.append(f"{label}: `{token}` is not a table or column in schema.json; "
                              "name a real one or drop the backticks")
    if rel.startswith("codebase/") and not paths:
        warnings.append(f"{label}: no /mirrors/ path at all; an area file that names no file "
                        "is a guess the dev cannot check")
    if in_databases and not named_tables:
        warnings.append(f"{label}: no table named in backticks; a map that names no table "
                        "is a guess the developer cannot check")


# -------------------------------------------------------------------- assembly

def assemble(out: Path, scan: dict | None, schema_data: dict | None, context: str,
             questions: list[dict], benchmark: list[dict], devquestions: list[dict],
             proposal: dict[str, str], headline: list[str]) -> dict:
    tally = {status: 0 for status in STATUSES}
    for row in benchmark:
        if row["status"] in tally:
            tally[row["status"]] += 1
    source = schema_data or scan or {}
    return {
        "project": (scan or {}).get("project") or (schema_data or {}).get("project")
        or out.parent.name,
        "db": str(source.get("db") or source.get("database") or ""),
        "engine": str((schema_data or {}).get("engine") or ""),
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "context": context,
        "scan": scan,
        "schema": schema_data,
        "questions": [{key: row[key] for key in Q_COLUMNS} for row in questions],
        "benchmark": [{"id": row["id"], "cluster": row["cluster"], "status": row["status"],
                       "where": split_list(row["where"]), "note": row["note"]}
                      for row in benchmark],
        "devquestions": [{"id": row["id"], "group": row["group"], "question": row["question"],
                          "proposal": row["proposal"], "evidence": split_list(row["evidence"]),
                          "impact": row["impact"]}
                         for row in devquestions],
        "proposal": proposal,
        "headline": headline,
        "tally": tally,
    }


def load_json(path: Path, errors: list[str]) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        errors.append(f"{path.name}: not valid JSON: {exc}")
        return None


def brain_root_of(out: Path) -> Path | None:
    root = find_brain_root(out.resolve())
    return root if (_marker(root)).exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a grounding-intake output directory")
    parser.add_argument("out", type=Path, help="the intake output directory")
    args = parser.parse_args(argv)
    out: Path = args.out

    errors: list[str] = []
    warnings: list[str] = []
    if not out.is_dir():
        print(f"validate: not a directory: {out}", file=sys.stderr)
        return 2
    for name in ("questions.tsv", "benchmark.tsv", "devquestions.tsv"):
        if not (out / name).is_file():
            errors.append(f"{name}: missing")
    had_scan = (out / "scan.json").is_file()
    had_schema = (out / "schema.json").is_file()
    if not had_scan and not had_schema:
        errors.append("scan.json / schema.json: neither is there; an intake needs at least one "
                      "readable half, run scan.py or collect.py first")
    if errors:
        return report(errors, out)

    scan = load_json(out / "scan.json", errors) if had_scan else None
    schema_data = load_json(out / "schema.json", errors) if had_schema else None
    if errors:
        return report(errors, out)
    schema = Schema(schema_data) if schema_data is not None else None
    if schema is not None and not schema.tables:
        return report(["schema.json: no tables; rerun collect.py"], out)

    listings = Listings(out, (scan or {}).get("repos") or [], errors)
    locators = Locators(listings, schema, brain_root_of(out), had_scan)
    questions = read_tsv(out / "questions.tsv", Q_COLUMNS, errors)
    benchmark = read_tsv(out / "benchmark.tsv", B_COLUMNS, errors)
    devquestions = read_tsv(out / "devquestions.tsv", D_COLUMNS, errors)
    if errors:
        return report(errors, out)

    check_benchmark(benchmark, questions, locators, errors, warnings)
    check_devquestions(devquestions, benchmark, locators, errors, warnings)
    db = str((schema_data or {}).get("db") or (schema_data or {}).get("database") or "")
    proposal = check_proposal(out / "proposal", locators, had_scan, db, errors, warnings)

    headline: list[str] = []
    headline_file = out / "headline.txt"
    if headline_file.is_file():
        headline = [line.strip() for line in
                    headline_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(headline) > MAX_HEADLINE_LINES:
            errors.append(f"headline.txt: {len(headline)} lines, the cap is {MAX_HEADLINE_LINES}")
    context_file = out / "context.md"
    context = context_file.read_text(encoding="utf-8") if context_file.is_file() else ""
    if not context:
        warnings.append("context.md: missing; run context.py so the report shows what a "
                        "production run receives before it reads anything")
    if errors:
        return report(errors, out)

    data = assemble(out, scan, schema_data, context, questions, benchmark, devquestions,
                    proposal, headline)
    target = out / "intake.json"
    target.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    for warning in warnings:
        print(f"  warn: {warning}", file=sys.stderr)
    tally = data["tally"]
    print(f"OK: {len(questions)} questions ({tally['grounded']} grounded, "
          f"{tally['ambiguous']} ambiguous, {tally['missing']} missing, "
          f"{tally['knowledge']} knowledge, {tally['human']} human) · "
          f"{len(devquestions)} dev questions · {len(proposal)} proposal files")
    print(f"wrote {target}")
    return 0


def report(errors: list[str], out: Path) -> int:
    print(f"{len(errors)} error(s) in {out}:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
