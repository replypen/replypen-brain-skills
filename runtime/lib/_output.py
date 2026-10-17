"""Shared rendering for the lib CLIs (``python -m lib.db`` / ``python -m lib.cloudwatch``).

Row sets come back as ``list[dict]`` from ``db.query`` and ``cloudwatch.insights``; the CLIs
render them as CSV (default — compact, the LLM parses it cheaply), JSON, or a fixed-width table.
Large output spills to a file under ``/tmp`` before the host's bash output spill boundary, then prints
a small structural preview so the model keeps row/column shape without re-printing the full file.

Pure stdlib so it imports without the DB/AWS drivers — keeps the modules (and their tests)
loadable on a host that has neither.
"""

import csv
import io
import json
import os
import tempfile

# Above this many bytes, write to a file and print a compact preview. Kept below the host's 6000-char
# bash spill threshold so row-aware previews pass through intact instead of being head/tail shredded.
SPILL_BYTES = 5_000
PREVIEW_CHARS = 4_000
PREVIEW_ROWS = 20

_SPILL_EXT = {"csv": ".csv", "json": ".json", "table": ".txt"}
_TRUNCATED = "…(truncated)…"


def render(rows: list[dict], fmt: str = "csv") -> str:
    """Render rows as ``csv`` | ``json`` | ``table``. Non-scalar cells are JSON-encoded for CSV/table.

    Rows that are mapping-LIKE rather than real dicts (``db.columns`` returns ``Column`` objects, which
    are str subclasses) are normalized first, so JSON keeps the object shape instead of collapsing to
    a bare string list.
    """
    rows = [r if isinstance(r, dict) else dict(r) for r in rows]
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str)
    if not rows:
        return ""
    cols = list(rows[0].keys())
    if fmt == "table":
        return _table(rows, cols)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: _cell(r.get(c)) for c in cols})
    return buf.getvalue()


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _table(rows: list[dict], cols: list[str]) -> str:
    widths = {c: len(c) for c in cols}
    cells = []
    for r in rows:
        row = {c: _cell(r.get(c)) for c in cols}
        for c in cols:
            widths[c] = max(widths[c], len(row[c]))
        cells.append(row)
    line = lambda row: "  ".join(row[c].ljust(widths[c]) for c in cols)
    head = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    return "\n".join([head, sep, *(line(r) for r in cells)])


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Return ``text`` capped to ``max_bytes`` UTF-8 bytes, preserving valid text."""
    if _byte_len(text) <= max_bytes:
        return text
    marker_bytes = _TRUNCATED.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", "ignore")
    keep = max_bytes - len(marker_bytes)
    prefix = text.encode("utf-8", "replace")[:keep].decode("utf-8", "ignore").rstrip()
    return f"{prefix}{_TRUNCATED}"


def _spill(text: str, label: str, ext: str) -> tuple[str, int]:
    out_dir = os.path.join(tempfile.gettempdir(), "replypen-out")
    os.makedirs(out_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"{label}-", suffix=ext, dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path, _byte_len(text)


def _query_hint(fmt: str, path: str) -> str:
    common = f"rg PATTERN {path}; sed -n '1,80p' {path}; wc -l {path}"
    if fmt == "json":
        specific = f"jq '.[] | select(...)' {path}"
    elif fmt == "csv":
        specific = (
            f'awk -F\',\' \'NR==1||$1=="x"\' {path}; '
            f"python -c 'import csv,sys; print(next(csv.DictReader(open(sys.argv[1]))))' {path}"
        )
    else:
        specific = f"sed -n '1,80p' {path}"
    return f"query it: {specific}; {common} (do not cat/re-print the whole file)"


def _preview_sample(rows: list[dict], fmt: str, max_bytes: int) -> str:
    if max_bytes <= 0 or not rows:
        return ""
    k = min(PREVIEW_ROWS, len(rows))
    while k > 1:
        sample = render(rows[:k], fmt)
        if _byte_len(sample) <= max_bytes:
            return sample
        k -= 1
    return _truncate_bytes(render(rows[:1], fmt), max_bytes)


def _structural_preview(rows: list[dict], fmt: str, path: str, byte_count: int) -> str:
    row_count = len(rows)
    cols = list(rows[0].keys()) if rows else []
    col_text = _truncate_bytes(", ".join(cols), 900)
    meta = [
        f"{row_count} rows × {len(cols)} cols — full result saved to remote workspace file {path} ({byte_count} bytes)",
        f"fetch locally: rp dev console file get {path} --out ./result{_SPILL_EXT.get(fmt, '.txt')}",
        f"columns: {col_text}" if cols else "columns: (none)",
    ]
    hint = _query_hint(fmt, path)
    overhead = "\n".join([*meta, "", "", hint])
    sample_budget = PREVIEW_CHARS - _byte_len(overhead) - 64
    sample = _preview_sample(rows, fmt, sample_budget)
    preview = "\n".join([*meta, sample, hint])
    return _truncate_bytes(preview, PREVIEW_CHARS)


def emit_rows(rows: list[dict], fmt: str = "csv", label: str = "out") -> None:
    """Print rendered rows, or spill full output with a structural preview when it is large."""
    rendered = render(rows, fmt)
    if _byte_len(rendered) <= SPILL_BYTES:
        print(rendered)
        return
    path, byte_count = _spill(rendered, label=label, ext=_SPILL_EXT.get(fmt, ".txt"))
    print(_structural_preview(rows, fmt, path, byte_count))


def emit(text: str, label: str = "out") -> None:
    """Print ``text``, or spill to a temp file and print its path when it exceeds ``SPILL_BYTES``."""
    if _byte_len(text) <= SPILL_BYTES:
        print(text)
        return
    path, byte_count = _spill(text, label=label, ext=".txt")
    print(
        f"[result {byte_count} bytes — spilled to remote workspace file {path}; "
        f"fetch locally: rp dev console file get {path} --out ./result.txt; read it with the fs tools or `sed -n`]"
    )
