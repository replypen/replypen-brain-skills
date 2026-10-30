"""On-demand transcription of a chat recording (long audio, screen capture) over the host broker.

The host no longer transcribes recordings at run start. The run prompt names each untranscribed
recording by its staged path; beside it sits ``<path>.attachment.json`` (the attachment id + recorder
facts). This lib reads that sidecar and asks the broker mount
``POST http://rc-broker.internal/transcribe/`` to transcribe it. The host always applies the project's
own recognition hints (context, keywords, language); what you pass is ADDED on top — use it for what
only this run knows (e.g. a timeline of what the user did in the recording window, names involved).

    from lib import transcribe
    res = transcribe.transcribe("/tmp/attachments/2-schermopname.webm",
                                instructions=timeline, keywords=["Kampweek"])
    print(res.path)                  # /tmp/attachments/2-schermopname.webm.transcript.md

CLI (prints the transcript, or its head + the path when long):
    python -m lib.transcribe <path> [--instructions TEXT | --instructions-file FILE]
                                    [--keywords a,b] [--language nl] [--full]

Exact text at a moment of a SCREEN recording (an id, an amount, a line marked UNREADABLE): ``--at``
reads full-resolution frames instead of transcribing — up to 6 moments, ``mm:ss`` or seconds, with
``--instructions`` saying what to read. Seconds, never cached, and the transcript file is untouched:
    python -m lib.transcribe <path> --at 03:12 [--at 04:01] [--instructions "the order id"]

A long recording takes minutes. One call waits up to ~9 minutes; if the host is still working it
exits 3 with "still transcribing" — run the SAME command again: it joins that transcription instead
of starting (and paying for) a second one. The same arguments later are served from cache; different
instructions re-transcribe and replace the transcript.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from lib import _http_audit

BROKER_URL = "http://rc-broker.internal/transcribe/"

SIDECAR_SUFFIX = ".attachment.json"
TRANSCRIPT_SUFFIX = ".transcript.md"

# The host holds a call open up to its own ceiling (just under the bash tool timeout) and answers
# 202 "running" after that; the read timeout only needs to outlast that wait.
DEFAULT_WAIT_SECONDS = 540
CONNECT_TIMEOUT = 10.0
READ_SLACK = 30.0

# Printed in full up to this size; above it the CLI prints the head and points at the file.
PRINT_FULL_CHARS = 12000
HEAD_LINES = 40


class TranscribeError(RuntimeError):
    """The host refused or failed — carries its own sentence."""


class TranscribeUnavailable(TranscribeError):
    """No transcribe mount in this run (not a chat run, or transcription is off)."""


class TranscribePending(TranscribeError):
    """The host is still transcribing; call again with the same arguments to keep waiting."""


@dataclass(frozen=True)
class Result:
    transcript: str
    mode: str
    duration_seconds: float | None
    cached: bool
    path: str  # where the transcript was written


def paths_for(path: str | os.PathLike) -> tuple[Path, Path]:
    """(recording path, sidecar path) for the recording path, its sidecar, or its transcript file."""
    p = str(path)
    for suffix in (SIDECAR_SUFFIX, TRANSCRIPT_SUFFIX):
        if p.endswith(suffix):
            p = p[: -len(suffix)]
            break
    return Path(p), Path(p + SIDECAR_SUFFIX)


def read_sidecar(path: str | os.PathLike) -> dict:
    media, sidecar = paths_for(path)
    if not sidecar.is_file():
        raise TranscribeError(
            f"no recording sidecar at {sidecar} — pass the recording path the conversation names "
            f"(e.g. /tmp/attachments/2-schermopname.webm), even when the media itself is not staged"
        )
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise TranscribeError(f"unreadable recording sidecar {sidecar}: {exc}") from exc
    if not isinstance(meta, dict) or not str(meta.get("attachment_id") or "").strip():
        raise TranscribeError(f"recording sidecar {sidecar} has no attachment_id")
    return meta


def transcribe(
    path: str | os.PathLike,
    *,
    instructions: str = "",
    keywords: list[str] | tuple[str, ...] = (),
    language: str = "",
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
) -> Result:
    """Transcribe one chat recording and write ``<path>.transcript.md`` (overwriting)."""
    media, _ = paths_for(path)
    meta = read_sidecar(path)
    body = {
        "attachment_id": str(meta["attachment_id"]).strip(),
        "instructions": (instructions or "").strip(),
        "keywords": [k.strip() for k in keywords if k and k.strip()],
        "language": (language or "").strip(),
        "wait_seconds": int(wait_seconds),
    }
    payload = _post(body)
    transcript = str(payload.get("transcript") or "")
    out = _write_transcript(media, transcript)
    duration = payload.get("duration_seconds")
    return Result(
        transcript=transcript,
        mode=str(payload.get("mode") or meta.get("media_mode") or ""),
        duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        cached=bool(payload.get("cached")),
        path=str(out),
    )


MAX_FRAMES = 6  # host cap per frame read
FRAME_WAIT_SECONDS = 120


def parse_moment(value: str | float) -> float:
    """``192``, ``192.5``, ``03:12`` or ``1:03:12`` → seconds from the recording's start."""
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().strip("[]")
        try:
            parts = [float(p) for p in text.split(":")]
        except ValueError as exc:
            raise TranscribeError(f"--at {value!r}: use mm:ss (e.g. 03:12) or seconds") from exc
        if not parts or len(parts) > 3:
            raise TranscribeError(f"--at {value!r}: use mm:ss (e.g. 03:12) or seconds")
        seconds = 0.0
        for p in parts:
            seconds = seconds * 60 + p
    if seconds < 0:
        raise TranscribeError(f"--at {value!r} is before the recording starts")
    return seconds


def read_frames(path: str | os.PathLike, at: list | tuple, *, instructions: str = "") -> str:
    """Read the exact on-screen text at these moments of a screen recording (full-resolution stills)."""
    moments = [parse_moment(a) for a in at]
    if not moments or len(moments) > MAX_FRAMES:
        raise TranscribeError(f"pass 1 to {MAX_FRAMES} moments with --at")
    meta = read_sidecar(path)
    body = {
        "attachment_id": str(meta["attachment_id"]).strip(),
        "instructions": (instructions or "").strip(),
        "keywords": [],
        "at_seconds": moments,
        "wait_seconds": FRAME_WAIT_SECONDS,
    }
    return str(_post(body).get("transcript") or "")


def _post(body: dict) -> dict:
    import requests

    try:
        resp = _http_audit.request(
            "POST",
            BROKER_URL,
            json_body=body,
            timeout=(CONNECT_TIMEOUT, body["wait_seconds"] + READ_SLACK),
            endpoint_template="/transcribe/",
            # Instructions and keywords can quote customer data: keep them out of the audit line.
            known_secrets=(body["instructions"], *body["keywords"]),
        )
    except requests.exceptions.ConnectionError as exc:
        raise TranscribeUnavailable("On-demand transcription is not available in this run") from exc
    except requests.exceptions.Timeout as exc:
        raise TranscribePending(
            "still transcribing (the host did not answer in time); run the same command again to keep waiting"
        ) from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    message = str(payload.get("message") or "").strip()
    if resp.status_code == 200:
        return payload
    if resp.status_code == 202:
        raise TranscribePending(message or "still transcribing; run the same command again to keep waiting")
    if resp.status_code == 404 and not message:
        raise TranscribeUnavailable("On-demand transcription is not available in this run")
    raise TranscribeError(message or f"transcription failed (HTTP {resp.status_code})")


def _write_transcript(media: Path, transcript: str) -> Path:
    target = Path(str(media) + TRANSCRIPT_SUFFIX)
    text = transcript.rstrip() + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target
    except OSError:
        # A read-only staging dir must not lose a transcript that was already paid for.
        fallback = Path(os.environ.get("TMPDIR") or "/tmp") / "transcripts" / target.name
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(text, encoding="utf-8")
        return fallback


def render(res: Result, *, full: bool = False) -> str:
    """CLI output: the transcript itself when it fits, else its head plus where the rest lives."""
    meta = [f"mode={res.mode}", "cached" if res.cached else "fresh"]
    if res.duration_seconds is not None:
        total = int(round(res.duration_seconds))
        meta.append(f"duration={total // 60:02d}:{total % 60:02d}")
    header = f"Transcript written to {res.path} ({', '.join(meta)})"
    if full or len(res.transcript) <= PRINT_FULL_CHARS:
        return f"{header}\n\n{res.transcript.rstrip()}"
    lines = res.transcript.splitlines()
    head = "\n".join(lines[:HEAD_LINES])
    return (
        f"{header}\n\n{head}\n\n… {len(lines) - HEAD_LINES} more lines. Read or grep the full transcript "
        f"at {res.path} (e.g. rg -n 'SCREEN' {res.path})."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.transcribe", description=__doc__.splitlines()[0])
    parser.add_argument("path", help="the recording path the conversation names (its sidecar sits beside it)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--instructions", default="", help="extra context for the transcriber")
    group.add_argument("--instructions-file", help="read the extra context from a file ('-' = stdin)")
    parser.add_argument("--keywords", default="", help="comma-separated names/product terms")
    parser.add_argument("--language", default="", help="override the spoken-language hint (e.g. nl)")
    parser.add_argument("--full", action="store_true", help="print the whole transcript, however long")
    parser.add_argument("--at", action="append", default=[], metavar="MM:SS",
                        help="read the exact on-screen text at this moment instead (repeatable, up to 6)")
    args = parser.parse_args(argv)

    instructions = args.instructions
    if args.instructions_file:
        src = sys.stdin if args.instructions_file == "-" else open(args.instructions_file, encoding="utf-8")
        with src:
            instructions = src.read()
    keywords = [k for k in args.keywords.split(",") if k.strip()]
    if args.at:
        try:
            print(read_frames(args.path, args.at, instructions=instructions).rstrip())
        except TranscribeError as exc:
            print(f"lib.transcribe: {exc}", file=sys.stderr)
            return 2
        return 0
    try:
        res = transcribe(args.path, instructions=instructions, keywords=keywords, language=args.language)
    except TranscribePending as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except TranscribeError as exc:
        print(f"lib.transcribe: {exc}", file=sys.stderr)
        return 2
    print(render(res, full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
