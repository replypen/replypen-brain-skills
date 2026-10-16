"""Google Sheets helpers: the few reads/writes where a wrapper beats a raw ``lib.api`` call.

Each one earns its place by a footgun it removes: formulas vs displayed values, header-aware lookup,
RAW input (customer text never becomes a formula), a before/after receipt on every write, and exact
range replacement with no stale tail rows. Anything else the Sheets API supports goes raw:
``python -m lib.api post|put googlesheets <path> --json …`` (writes are allowed on the manifest's
``write_endpoints`` when the broker confirms the file is shared as Editor).

``sheet`` is a Sheets URL or the bare spreadsheet id. ``rng`` is A1 notation (``Tab!A1:C10``, or a
bare tab name for the whole tab).

    python -m lib.gsheets values <sheet> "<range>" [--formulas]
    python -m lib.gsheets find-rows <sheet> <tab> <column> <value>
    python -m lib.gsheets update <sheet> "<range>" --rows '[["a",1]]' [--user-entered]
    python -m lib.gsheets append <sheet> <tab> --rows '[["a",1]]' [--user-entered]
    python -m lib.gsheets replace-range <sheet> "<range>" --rows '[["a",1]]' [--user-entered]
    python -m lib.gsheets add-tab <sheet> "<title>"
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lib import _google

KEY = _google.SHEETS
_INPUT_OPTIONS = ("RAW", "USER_ENTERED")
_COLUMN_LETTERS = re.compile(r"[A-Za-z]{1,3}")


def _path(sheet: str, suffix: str = "") -> str:
    return f"spreadsheets/{_google.file_id(sheet, KEY)}{suffix}"


def _a1(rng: str) -> str:
    # ``:`` stays raw so ``…/values/<range>:append`` keeps its verb suffix; ``/``, ``#``, ``?`` and
    # spaces in tab names must not break the path.
    return quote(rng, safe="!:'$")


def _tab(tab: str) -> str:
    """A tab name as an A1 range; quoted so names with spaces, digits or ``!`` resolve."""
    if tab.startswith("'") and tab.endswith("'"):
        return tab
    return "'" + tab.replace("'", "''") + "'"


def _input_option(value: str) -> str:
    value = value.upper()
    if value not in _INPUT_OPTIONS:
        raise ValueError(f"input_option must be one of {_INPUT_OPTIONS}")
    return value


def _cells(rows: list[list[Any]]) -> list[list[Any]]:
    # Google SKIPS a null cell (the old value survives); "" clears it.
    return [["" if c is None else c for c in row] for row in rows]


def values(sheet: str, rng: str, *, formulas: bool = False) -> list[list[Any]]:
    """Cell values as a 2D list (trailing empty rows/cells trimmed, as Google returns them).

    ``formulas=True`` returns the formula text (``=SUM(B2:B9)``) instead of the computed value; the
    default shows what a person sees in the sheet.
    """
    body = _google.client(KEY).get(
        _path(sheet, f"/values/{_a1(rng)}"),
        query={"valueRenderOption": "FORMULA" if formulas else "FORMATTED_VALUE"},
    )
    return body.get("values", [])


def find_rows(sheet: str, tab: str, column: str, value: str, *, header_row: int = 1) -> dict:
    """Rows of ``tab`` whose ``column`` equals ``value`` (trimmed, case-insensitive).

    ``column`` is a header name (matched case-insensitively) or a column letter. Returns
    ``{"header": [...], "matches": [{"row": <sheet row number>, "values": [...], "record": {...}}]}``;
    ``row`` is the 1-based sheet row, ready for an ``update`` range like ``Tab!A<row>``.
    """
    grid = values(sheet, _tab(tab))
    if len(grid) < header_row:
        return {"header": [], "matches": []}
    header = [str(h) for h in grid[header_row - 1]]
    idx = _column_index(header, column)
    # A missing short header name ("SKU", "Tel") also parses as a column letter: past every value it
    # can only be a typo (Sheets trims trailing blanks, so single letters A-Z stay allowed).
    if len(column.strip()) > 1 and idx >= max(len(row) for row in grid):
        raise LookupError(f"no column {column!r}; header is {header}")
    want = str(value).strip().casefold()
    matches = []
    for offset, row in enumerate(grid[header_row:], start=header_row + 1):
        cell = row[idx] if idx < len(row) else ""
        if str(cell).strip().casefold() == want:
            record = {h: (row[i] if i < len(row) else "") for i, h in enumerate(header) if h}
            matches.append({"row": offset, "values": row, "record": record})
    return {"header": header, "matches": matches}


def _column_index(header: list[str], column: str) -> int:
    wanted = column.strip().casefold()
    for i, h in enumerate(header):
        if h.strip().casefold() == wanted:
            return i
    if _COLUMN_LETTERS.fullmatch(column.strip()):
        n = 0
        for ch in column.strip().upper():
            n = n * 26 + (ord(ch) - 64)
        return n - 1
    raise LookupError(f"no column {column!r}; header is {header}")


def update(sheet: str, rng: str, rows: list[list[Any]], *, input_option: str = "RAW") -> dict:
    """Overwrite ``rng`` from its top-left with ``rows``; returns ``{"range", "before", "after"}``.

    RAW stores text exactly as given (``=1+1`` stays text). Pass ``input_option="USER_ENTERED"`` only
    when the user wants Sheets to parse input like typing (formulas, dates, ``12%``). Cells of ``rng``
    outside ``rows`` are left untouched: use ``replace_range`` for an exact replacement. ``None``
    cells are sent as ``""`` and clear the cell (Google would otherwise skip them).
    """
    before = values(sheet, rng)
    body = _google.client(KEY).put(
        _path(sheet, f"/values/{_a1(rng)}"),
        query={
            "valueInputOption": _input_option(input_option),
            "includeValuesInResponse": "true",
            "responseValueRenderOption": "FORMATTED_VALUE",
        },
        json={"majorDimension": "ROWS", "values": _cells(rows)},
    )
    return {
        "range": body.get("updatedRange", rng),
        "before": before,
        "after": (body.get("updatedData") or {}).get("values", []),
    }


def append(sheet: str, tab: str, rows: list[list[Any]], *, input_option: str = "RAW") -> dict:
    """Append ``rows`` below the table on ``tab`` (new rows inserted, nothing overwritten).

    Returns ``{"range": "<where they landed>", "rows": n, "after": [...]}``.
    """
    body = _google.client(KEY).post(
        _path(sheet, f"/values/{_a1(_tab(tab))}:append"),
        query={
            "valueInputOption": _input_option(input_option),
            "insertDataOption": "INSERT_ROWS",
            "includeValuesInResponse": "true",
        },
        json={"majorDimension": "ROWS", "values": rows},
    )
    updates = body.get("updates") or {}
    return {
        "range": updates.get("updatedRange", ""),
        "rows": updates.get("updatedRows", 0),
        "after": (updates.get("updatedData") or {}).get("values", []),
    }


def replace_range(sheet: str, rng: str, rows: list[list[Any]], *, input_option: str = "RAW") -> dict:
    """Make ``rng`` hold exactly ``rows``: write them and blank whatever the old content had beyond
    them (extra rows, wider columns), in ONE ``values:batchUpdate``. Returns ``{"range", "before",
    "after"}``. The range must be tall/wide enough for the larger of old and new content (an open
    range like ``Tab!A2:F`` always is). ``None`` cells are cleared like ``""``, never skipped."""
    before = values(sheet, rng)
    rows = _cells(rows)
    height = max(len(before), len(rows))
    width = max([len(r) for r in before + rows] or [0])
    padded = [
        list(rows[i] if i < len(rows) else []) + [""] * (width - len(rows[i] if i < len(rows) else []))
        for i in range(height)
    ]
    body = _google.client(KEY).post(
        _path(sheet, "/values:batchUpdate"),
        json={
            "valueInputOption": _input_option(input_option),
            "includeValuesInResponse": True,
            "responseValueRenderOption": "FORMATTED_VALUE",
            "data": [{"range": rng, "majorDimension": "ROWS", "values": padded}],
        },
    )
    resp = (body.get("responses") or [{}])[0]
    return {
        "range": resp.get("updatedRange", rng),
        "before": before,
        "after": (resp.get("updatedData") or {}).get("values", []),
    }


def add_tab(sheet: str, title: str) -> dict:
    """Add a tab; returns its ``{"sheetId", "title", "index"}`` (``sheetId`` is the gid that
    formatting requests in a raw ``:batchUpdate`` need)."""
    body = _google.client(KEY).post(
        _path(sheet, ":batchUpdate"),
        json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    )
    props = ((body.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
    return {k: props.get(k) for k in ("sheetId", "title", "index")}


def _rows_arg(args) -> list[list[Any]]:
    raw = Path(args.rows_file).read_text(encoding="utf-8") if args.rows_file else args.rows
    rows = json.loads(raw or "null")
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        raise ValueError("--rows must be a JSON list of row lists, e.g. '[[\"a\", 1]]'")
    return rows


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m lib.gsheets", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("values", help="read a range as a 2D list")
    sp.add_argument("sheet")
    sp.add_argument("range")
    sp.add_argument("--formulas", action="store_true", help="formula text instead of computed values")

    sp = sub.add_parser("find-rows", help="rows where a column equals a value (header-aware)")
    for a in ("sheet", "tab", "column", "value"):
        sp.add_argument(a)

    for name, target in (("update", "range"), ("append", "tab"), ("replace-range", "range")):
        sp = sub.add_parser(name)
        sp.add_argument("sheet")
        sp.add_argument(target)
        sp.add_argument("--rows", default="", help="JSON list of rows")
        sp.add_argument("--rows-file", default="", help="read the rows JSON from a file")
        sp.add_argument("--user-entered", action="store_true", help="parse input like typing (formulas, dates)")

    sp = sub.add_parser("add-tab")
    sp.add_argument("sheet")
    sp.add_argument("title")

    args = p.parse_args(argv)
    opt = "USER_ENTERED" if getattr(args, "user_entered", False) else "RAW"
    commands = {
        "values": lambda: values(args.sheet, args.range, formulas=args.formulas),
        "find-rows": lambda: find_rows(args.sheet, args.tab, args.column, args.value),
        "update": lambda: update(args.sheet, args.range, _rows_arg(args), input_option=opt),
        "append": lambda: append(args.sheet, args.tab, _rows_arg(args), input_option=opt),
        "replace-range": lambda: replace_range(args.sheet, args.range, _rows_arg(args), input_option=opt),
        "add-tab": lambda: add_tab(args.sheet, args.title),
    }
    return _google.cli(commands[args.cmd])


if __name__ == "__main__":
    raise SystemExit(_main())
