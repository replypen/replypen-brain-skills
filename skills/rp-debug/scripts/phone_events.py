#!/usr/bin/env python3
"""Select phone-lab evidence without loading a whole dump into an agent's context."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def records(path: Path):
    with path.open() as stream:
        if path.suffix == ".json":
            for index, event in enumerate(json.load(stream)["events"]):
                yield {"type": "event", "index": index, "event": event}
        else:
            for line in stream:
                if line.strip():
                    record = json.loads(line)
                    if record.get("type") == "event":
                        yield record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="phone-*.jsonl (or an older .json dump)")
    parser.add_argument("--kind", action="append", help="repeat to select several kinds")
    parser.add_argument("--index", type=int, help="original event-array index")
    parser.add_argument("--delegation", help="exact delegation id")
    parser.add_argument("--after-ms", type=int, default=0)
    parser.add_argument("--before-ms", type=int)
    parser.add_argument("--min-exec-ms", type=int)
    args = parser.parse_args(argv)
    try:
        for record in records(args.dump):
            event = record["event"]
            if args.kind and event.get("kind") not in args.kind:
                continue
            if args.index is not None and record["index"] != args.index:
                continue
            if args.delegation is not None and event.get("delegation") != args.delegation:
                continue
            if event.get("t", 0) < args.after_ms:
                continue
            if args.before_ms is not None and event.get("t", 0) > args.before_ms:
                continue
            if args.min_exec_ms is not None and event.get("exec_ms", -1) < args.min_exec_ms:
                continue
            print(json.dumps(record, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"phone_events: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
