#!/usr/bin/env python3
"""Bounded read-only schema probe. Runs ON THE BOX, not on your laptop.

`collect.py` ships this file through `rp dev console bash run -`, replaces the `CONFIG = {...}`
line with its own values, and fetches the JSON it writes. Stdlib plus the injected `lib.db`
only: the box has no kit, no requests, no pip.

Every statement is one bounded query. It reads `information_schema` in pages, then samples
values for enum-like, non-PII columns only (at most 13 groups, never a text or blob column,
never `select *`). Anything that raises lands in `budget.skipped` and the probe carries on.
"""

from __future__ import annotations

CONFIG = {"db": "staging", "out": "/tmp/replypen-out/schema.json", "profile_max_rows": 3000000, "max_profile_queries": 120, "max_profile_per_table": 6, "max_count_queries": 40, "count_max_rows": 200000, "wall_seconds": 600, "rows_per_page": 5000}

import json
import re
import time
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")  # the wire proxy warns about max_execution_time on every call

from lib import db  # noqa: E402

# A column whose name matches this is never sampled, whatever its type.
PII = re.compile(
    r"mail|phone|tel|gsm|mob|name|naam|first|last|address|adres|street|straat|city|gemeente"
    r"|zip|post|iban|bic|btw|vat|password|pass|token|hash|secret|salt|ip|birth|geboorte"
    r"|comment|remark|note|text|descr|url|domain|www|voornaam|achternaam", re.I)
# Sampled before the rest, because these carry the state machine a support answer needs.
INTERESTING = re.compile(r"status|type|state|kind|active|deleted|flag|is_|online|lang|plan|mode"
                         r"|level|role", re.I)
ENUMISH_MYSQL = re.compile(r"^(enum\(|set\(|tinyint|smallint|bool|boolean|bit)")
MAX_GROUPS = 12
MIN_PROFILE_ROWS = 20
MAX_VALUE_CHARS = 40


class Probe:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.started = time.time()
        self.queries = 0
        self.skipped: list[dict] = []
        self.engine = "mysql"
        self.quote = "`"

    # ---------------------------------------------------------------- plumbing

    def q(self, name: str) -> str:
        """Quote an identifier that came out of information_schema, never user text."""
        char = self.quote
        return char + str(name).replace(char, "") + char

    def run(self, sql: str) -> list[dict]:
        self.queries += 1
        rows = db.query(sql, db=self.config["db"])
        # some servers answer with upper case information_schema column names
        return [{str(key).lower(): value for key, value in dict(row).items()}
                for row in (rows or [])]

    def safe(self, sql: str, table: str = "", column: str = "") -> list[dict] | None:
        try:
            return self.run(sql)
        except Exception as exc:  # noqa: BLE001 - one failing table must not end the probe
            self.skipped.append({"table": table, "column": column,
                                 "reason": f"{type(exc).__name__}: {exc}"[:200]})
            return None

    def paged(self, select: str, order: str, label: str) -> list[dict]:
        """One information_schema query, read in pages so no single statement is unbounded."""
        size = int(self.config["rows_per_page"])
        out: list[dict] = []
        offset = 0
        while True:
            page = self.safe(f"{select} order by {order} limit {size} offset {offset}",
                             table=label)
            if not page:
                break
            out.extend(page)
            if len(page) < size:
                break
            offset += size
            if self.out_of_time():
                self.skipped.append({"table": label, "column": "",
                                     "reason": "wall_seconds reached while paging"})
                break
        return out

    def out_of_time(self) -> bool:
        return time.time() - self.started > float(self.config["wall_seconds"])

    def one(self, sql: str) -> str:
        rows = self.safe(sql) or []
        return str(list(rows[0].values())[0]) if rows else ""

    # -------------------------------------------------------------- structure

    def detect(self) -> str:
        version = self.one("select version()")
        self.engine = "postgres" if "PostgreSQL" in version else "mysql"
        self.quote = '"' if self.engine == "postgres" else "`"
        return version

    def mysql_structure(self) -> dict:
        where = "where table_schema = database()"
        tables = [{"table": r["table_name"], "schema": "", "type": r["table_type"],
                   "engine": r["engine"], "rows": as_int(r["table_rows"]),
                   "data_length": as_int(r["data_length"]),
                   "index_length": as_int(r["index_length"]),
                   "comment": r["table_comment"] or "",
                   "create_time": str(r["create_time"] or ""),
                   "update_time": str(r["update_time"] or "")}
                  for r in self.paged(
                      "select table_name, table_type, engine, table_rows, data_length, "
                      f"index_length, table_comment, create_time, update_time "
                      f"from information_schema.tables {where}",
                      "table_name", "information_schema.tables")]
        columns = [{"table": r["table_name"], "column": r["column_name"],
                    "position": as_int(r["ordinal_position"]), "type": r["column_type"],
                    "nullable": r["is_nullable"], "default": none_str(r["column_default"]),
                    "key": r["column_key"] or "", "extra": r["extra"] or "",
                    "comment": r["column_comment"] or ""}
                   for r in self.paged(
                       "select table_name, column_name, ordinal_position, column_type, "
                       "is_nullable, column_default, column_key, extra, column_comment "
                       f"from information_schema.columns {where}",
                       "table_name, ordinal_position", "information_schema.columns")]
        kinds = {(r["table_name"], r["constraint_name"]): r["constraint_type"]
                 for r in self.paged(
                     "select table_name, constraint_name, constraint_type "
                     f"from information_schema.table_constraints {where}",
                     "table_name, constraint_name", "table_constraints")}
        keys = [{"table": r["table_name"], "constraint": r["constraint_name"],
                 "column": r["column_name"], "position": as_int(r["ordinal_position"]),
                 "referenced_table": none_str(r["referenced_table_name"]) or "",
                 "referenced_column": none_str(r["referenced_column_name"]) or "",
                 "type": kinds.get((r["table_name"], r["constraint_name"]), "")}
                for r in self.paged(
                    "select table_name, constraint_name, column_name, ordinal_position, "
                    "referenced_table_name, referenced_column_name "
                    f"from information_schema.key_column_usage {where}",
                    "table_name, constraint_name, ordinal_position", "key_column_usage")]
        indexes = [{"table": r["table_name"], "index": r["index_name"],
                    "non_unique": as_int(r["non_unique"]), "seq": as_int(r["seq_in_index"]),
                    "column": r["column_name"] or ""}
                   for r in self.paged(
                       "select table_name, index_name, non_unique, seq_in_index, column_name "
                       f"from information_schema.statistics {where}",
                       "table_name, index_name, seq_in_index", "information_schema.statistics")]
        return {"tables": tables, "columns": columns, "keys": keys, "indexes": indexes}

    def postgres_structure(self) -> dict:
        where = ("where table_schema not in ('pg_catalog', 'information_schema') "
                 "and table_schema not like 'pg_toast%'")
        tables = [{"table": r["table_name"], "schema": r["table_schema"],
                   "type": r["table_type"], "engine": "postgres", "rows": 0,
                   "data_length": 0, "index_length": 0, "comment": "",
                   "create_time": "", "update_time": ""}
                  for r in self.paged(
                      "select table_name, table_schema, table_type "
                      f"from information_schema.tables {where}",
                      "table_schema, table_name", "information_schema.tables")]
        sizes = self.safe(
            "select n.nspname as table_schema, c.relname as table_name, "
            "c.reltuples as row_estimate, pg_total_relation_size(c.oid) as total_bytes "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where c.relkind in ('r', 'p') "
            "and n.nspname not in ('pg_catalog', 'information_schema')") or []
        by_key = {(r["table_schema"], r["table_name"]): r for r in sizes}
        for table in tables:
            hit = by_key.get((table["schema"], table["table"]))
            if hit:
                table["rows"] = as_int(hit["row_estimate"])
                table["data_length"] = as_int(hit["total_bytes"])
        columns = [{"table": r["table_name"], "column": r["column_name"],
                    "position": as_int(r["ordinal_position"]),
                    "type": pg_type(r), "nullable": r["is_nullable"],
                    "default": none_str(r["column_default"]), "key": "", "extra": "",
                    "comment": "", "udt": r.get("udt_name") or ""}
                   for r in self.paged(
                       "select table_name, column_name, ordinal_position, data_type, udt_name, "
                       "character_maximum_length, is_nullable, column_default "
                       f"from information_schema.columns {where}",
                       "table_name, ordinal_position", "information_schema.columns")]
        labels: dict[str, list[str]] = {}
        for row in self.safe("select t.typname as name, e.enumlabel as label from pg_enum e "
                             "join pg_type t on t.oid = e.enumtypid "
                             "order by t.typname, e.enumsortorder") or []:
            labels.setdefault(str(row["name"]), []).append(str(row["label"]))
        for column in columns:
            if column.get("udt") in labels:
                column["enum_labels"] = labels[column["udt"]]
        kinds = {(r["table_name"], r["constraint_name"]): r["constraint_type"]
                 for r in self.paged(
                     "select table_name, constraint_name, constraint_type "
                     f"from information_schema.table_constraints {where}",
                     "table_name, constraint_name", "table_constraints")}
        referenced = {}
        for row in self.safe(
                "select constraint_name, table_name, column_name "
                "from information_schema.constraint_column_usage "
                "where table_schema not in ('pg_catalog', 'information_schema')") or []:
            referenced.setdefault(row["constraint_name"], (row["table_name"], row["column_name"]))
        keys = []
        for r in self.paged(
                "select table_name, constraint_name, column_name, ordinal_position "
                f"from information_schema.key_column_usage {where}",
                "table_name, constraint_name, ordinal_position", "key_column_usage"):
            kind = kinds.get((r["table_name"], r["constraint_name"]), "")
            target = referenced.get(r["constraint_name"], ("", "")) if kind == "FOREIGN KEY" \
                else ("", "")
            keys.append({"table": r["table_name"], "constraint": r["constraint_name"],
                         "column": r["column_name"], "position": as_int(r["ordinal_position"]),
                         "referenced_table": target[0], "referenced_column": target[1],
                         "type": kind})
        indexes = []
        for r in self.safe("select tablename as table_name, indexname as index_name, "
                           "indexdef from pg_indexes where schemaname not in "
                           "('pg_catalog', 'information_schema')") or []:
            definition = str(r["indexdef"])
            unique = 0 if " UNIQUE " in definition.upper() else 1
            inside = definition[definition.find("(") + 1:definition.rfind(")")]
            for seq, part in enumerate(inside.split(","), start=1):
                indexes.append({"table": r["table_name"], "index": r["index_name"],
                                "non_unique": unique, "seq": seq,
                                "column": part.strip().strip('"').split(" ")[0]})
        return {"tables": tables, "columns": columns, "keys": keys, "indexes": indexes}

    # ---------------------------------------------------------------- sampling

    def enumish(self, column: dict) -> bool:
        kind = str(column.get("type") or "").lower()
        name = str(column.get("column") or "")
        if PII.search(name):
            return False
        if re.search(r"(_id|id|_key|_ref|_nr|number|code)$", name, re.I) and not kind.startswith(
                ("tinyint", "smallint", "bool", "bit", "enum(", "set(")):
            return False  # an identifier, never a state machine
        if self.engine == "postgres":
            if column.get("enum_labels") or kind in ("boolean", "smallint"):
                return True
            hit = re.match(r"character varying\((\d+)\)|character\((\d+)\)", kind)
            return bool(hit and int(hit.group(1) or hit.group(2)) <= 32)
        if ENUMISH_MYSQL.match(kind):
            return True
        hit = re.match(r"char\((\d+)\)", kind)
        if hit:
            return int(hit.group(1)) <= 8
        hit = re.match(r"varchar\((\d+)\)", kind)
        return bool(hit and int(hit.group(1)) <= 32)

    def profile(self, structure: dict) -> list[dict]:
        rows_by_table = {t["table"]: t["rows"] for t in structure["tables"]}
        base = {t["table"] for t in structure["tables"] if "VIEW" not in str(t["type"]).upper()}
        ceiling = int(self.config["profile_max_rows"])
        candidates: list[tuple[int, dict]] = []
        for column in structure["columns"]:
            table = column["table"]
            if table not in base or not MIN_PROFILE_ROWS <= rows_by_table.get(table, 0) <= ceiling:
                continue  # a near-empty table would print raw rows, not a distribution
            if not self.enumish(column):
                continue
            kind = str(column.get("type") or "").lower()
            rank = 0 if (kind.startswith(("enum(", "set(")) or column.get("enum_labels")) \
                else 1 if INTERESTING.search(column["column"]) else 2
            candidates.append((rank, column))
        candidates.sort(key=lambda pair: (pair[0], pair[1]["table"], pair[1]["position"]))

        profiles: list[dict] = []
        per_table: dict[str, int] = {}
        for _, column in candidates:
            if len(profiles) >= int(self.config["max_profile_queries"]) or self.out_of_time():
                break
            table = column["table"]
            if per_table.get(table, 0) >= int(self.config["max_profile_per_table"]):
                continue
            per_table[table] = per_table.get(table, 0) + 1
            name = self.q(column["column"])
            rows = self.safe(f"select {name} as v, count(*) as c from {self.qualified(column)} "
                             f"group by {name} order by c desc limit {MAX_GROUPS + 1}",
                             table=table, column=column["column"])
            if rows is None or len(rows) > MAX_GROUPS:
                continue
            profiles.append({"table": table, "column": column["column"],
                             "values": [{"v": clip(row["v"]), "c": as_int(row["c"])}
                                        for row in rows]})
        return profiles

    def qualified(self, column: dict) -> str:
        schema = column.get("schema") or ""
        return f"{self.q(schema)}.{self.q(column['table'])}" if schema else self.q(column["table"])

    def tenant_candidates(self, structure: dict) -> list[dict]:
        seen: dict[str, set] = {}
        for column in structure["columns"]:
            name = column["column"]
            if name.endswith("_id") and name != "id":
                seen.setdefault(name, set()).add(column["table"])
        ranked = sorted(seen.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        return [{"column": name, "tables": len(tables)} for name, tables in ranked[:3]]

    def counts(self, structure: dict, tenant: str) -> list[dict]:
        if not tenant:
            return []
        has = {column["table"] for column in structure["columns"] if column["column"] == tenant}
        schema_of = {t["table"]: t.get("schema") or "" for t in structure["tables"]}
        small = [t for t in structure["tables"]
                 if t["table"] in has and "VIEW" not in str(t["type"]).upper()
                 and t["rows"] <= int(self.config["count_max_rows"])]
        stem = tenant[:-3] if tenant.endswith("_id") else tenant
        anchor = next((t["rows"] for t in structure["tables"]
                       if t["table"] in (stem, stem + "s", stem + "es")), 0)
        # tables the size of the tenant table first: those are the one-row-per-tenant candidates
        small.sort(key=lambda t: (abs(t["rows"] - anchor), t["rows"]))
        out = []
        for table in small[:int(self.config["max_count_queries"])]:
            if self.out_of_time():
                break
            name = {"table": table["table"], "schema": schema_of.get(table["table"], "")}
            rows = self.safe(f"select count(*) as c, count(distinct {self.q(tenant)}) as d "
                             f"from {self.qualified(name)}", table=table["table"], column=tenant)
            if rows:
                out.append({"table": table["table"], "rows": as_int(rows[0]["c"]),
                            "tenants": as_int(rows[0]["d"])})
        return out


def as_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def none_str(value: object) -> str | None:
    return None if value is None else str(value)


def clip(value: object) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1] + "…"


def collect(config: dict) -> dict:
    probe = Probe(config)
    version = probe.detect()
    database = probe.one("select current_database()" if probe.engine == "postgres"
                         else "select database()")
    structure = probe.postgres_structure() if probe.engine == "postgres" \
        else probe.mysql_structure()
    for column in structure["columns"]:  # sampling needs the schema of its table
        column["schema"] = next((t.get("schema") or "" for t in structure["tables"]
                                 if t["table"] == column["table"]), "")
    candidates = probe.tenant_candidates(structure)
    profiles = probe.profile(structure)
    counts = probe.counts(structure, candidates[0]["column"] if candidates else "")
    return {
        "engine": probe.engine,
        "version": version,
        "database": database,
        "db": config["db"],
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "tenant_candidates": candidates,
        "tables": structure["tables"],
        "columns": structure["columns"],
        "keys": structure["keys"],
        "indexes": structure["indexes"],
        "profiles": profiles,
        "counts": counts,
        "budget": {"queries": probe.queries,
                   "seconds": round(time.time() - probe.started, 1),
                   "skipped": probe.skipped},
    }


def pg_type(row: dict) -> str:
    """`character varying(32)` rather than the bare `character varying`."""
    kind = str(row.get("data_type") or "")
    length = row.get("character_maximum_length")
    return f"{kind}({as_int(length)})" if length else kind


def main() -> int:
    data = collect(CONFIG)
    path = CONFIG["out"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    budget = data["budget"]
    print(f"{data['engine']} {data['database']}: {len(data['tables'])} tables, "
          f"{len(data['columns'])} columns, {len(data['keys'])} key rows, "
          f"{len(data['profiles'])} profiles, {len(data['counts'])} counts, "
          f"{budget['queries']} queries in {budget['seconds']}s, "
          f"{len(budget['skipped'])} skipped -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
