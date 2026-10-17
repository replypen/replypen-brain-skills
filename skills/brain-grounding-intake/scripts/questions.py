#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""One window of real customer questions, reduced to `questions.tsv` in the intake dir.

    uv run skills/brain-grounding-intake/scripts/questions.py [--days 60] [--out DIR]
    uv run skills/brain-grounding-intake/scripts/questions.py --from FILE [--out DIR]

`--days` runs the `brain-helpcenter-suggestions` collector (read-only `rp`) into
`$OUT/raw/helpcenter/` and reduces its `evidence.json`. `--from` takes either such an
`evidence.json` or a plain text file with one question per line.

Writes into the SAME `$OUT` that `scan.py` wrote, so both evidence halves share a directory.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan import find_brain_root  # noqa: E402

MAX_QUESTIONS = 300
MAX_LEN = 200
COLLECTOR = (Path(__file__).resolve().parents[2] / "brain-helpcenter-suggestions"
             / "scripts" / "collect.py")


def clip(text: str, limit: int = MAX_LEN) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def from_evidence(data: dict) -> list[dict]:
    """Non-noise conversations of a helpcenter `evidence.json`, oldest first."""
    rows = []
    for conv in data.get("conversations") or []:
        if conv.get("noise"):
            continue
        question = clip(conv.get("first_message") or conv.get("first_raw") or conv.get("subject") or "")
        if not question:
            continue
        created = str(conv.get("created_at") or "")
        rows.append({"date": created[:10], "channel": conv.get("channel") or "email",
                     "question": question, "url": conv.get("url") or "", "_sort": created})
    rows.sort(key=lambda row: row["_sort"])
    return rows


def from_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        when = ""
        hit = re.match(r"(\d{4}-\d{2}-\d{2})\s+(.*)", line)
        if hit:
            when, line = hit.group(1), hit.group(2)
        question = clip(line)
        if question:
            rows.append({"date": when, "channel": "manual", "question": question, "url": "",
                         "_sort": when})
    return rows


def run_collector(brain_root: Path, days: int, raw: Path) -> Path:
    if not COLLECTOR.is_file():
        print(f"questions: the helpcenter collector is missing ({COLLECTOR}). "
              "Reinstall the kit with brain-dev-upgrade, or pass --from FILE.", file=sys.stderr)
        raise SystemExit(2)
    proc = subprocess.run(
        ["uv", "run", str(COLLECTOR), "--days", str(days), "--out", str(raw)],
        cwd=str(brain_root), text=True, capture_output=True, check=False)
    sys.stderr.write(proc.stderr)
    if proc.returncode not in (0, 1):
        print("questions: the helpcenter collector found no usable source. "
              "Pass --from FILE with a hand list of questions instead.", file=sys.stderr)
        raise SystemExit(2)
    return raw / "evidence.json"


def write_tsv(path: Path, rows: list[dict]) -> None:
    lines = ["id\tdate\tchannel\tquestion\turl"]
    for index, row in enumerate(rows, start=1):
        lines.append(f"Q{index}\t{row['date']}\t{row['channel']}\t{row['question']}\t{row['url']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reduce one window of customer questions to questions.tsv")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--from", dest="source", type=Path,
                        help="a helpcenter evidence.json, or one question per line")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    brain_root = find_brain_root()
    out_dir = Path(args.out or brain_root / ".replypen" / "grounding-intake"
                   / date.today().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.source:
        text = args.source.read_text(encoding="utf-8", errors="replace")
        data = None
        try:
            parsed = json.loads(text)
            data = parsed if isinstance(parsed, dict) and "conversations" in parsed else None
        except ValueError:
            data = None
        rows = from_evidence(data) if data is not None else from_lines(text)
        source = f"{args.source} ({'evidence.json' if data is not None else 'line list'})"
    else:
        evidence = run_collector(brain_root, args.days, out_dir / "raw" / "helpcenter")
        if not evidence.is_file():
            print(f"questions: the collector wrote no {evidence}", file=sys.stderr)
            return 2
        rows = from_evidence(json.loads(evidence.read_text(encoding="utf-8")))
        source = f"last {args.days} days of runs"

    dropped = max(0, len(rows) - MAX_QUESTIONS)
    if dropped:
        rows = rows[-MAX_QUESTIONS:]
    write_tsv(out_dir / "questions.tsv", rows)
    tail = f", kept the {MAX_QUESTIONS} most recent of {len(rows) + dropped}" if dropped else ""
    print(f"{len(rows)} questions (source {source}{tail}) → questions.tsv")
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
