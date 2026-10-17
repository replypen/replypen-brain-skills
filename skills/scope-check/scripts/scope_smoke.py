#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Run the same brain commands as several real identities and report a pass/fail matrix.

The closest-to-production test tier: each command runs in a REAL production sandbox
(`rp dev console bash run`) bound to one principal, so it sees exactly the projection a hosted run
for that person sees. A helper that only ever ran as the tenant-wide operator passes here and fails
for a parent — that is the whole point.

    scope_smoke.py --project kampadmin --tenant acme \
        --as regular-admin=kampadmin_person:<uuid> \
        --cmd "python skills/camps/scripts/camp_overview.py --camp-id 42"

    scope_smoke.py --config _internal/scopecheck.toml          # audiences + commands + expectations

A command FAILS for an audience when it exits non-zero, times out, misses one of its `expect`
substrings, or prints a failure marker (`does not exist`, `Traceback`, `QueryError` by default).
Exit code is non-zero if any cell failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scope_common import (  # noqa: E402
    Audience,
    RcError,
    audiences_from_specs,
    parse_audience,
    rc_json_raw,
    scope_flags,
    unsupported_flag,
)

# Output that means "this blew up" even when the process exited 0 — a helper that swallows its own
# exception and prints the traceback is still broken for that audience.
DEFAULT_FAIL_MARKERS = ("does not exist", "Traceback", "QueryError")


@dataclass(frozen=True)
class Command:
    name: str
    cmd: str
    expect: tuple[str, ...] = ()


@dataclass
class Result:
    audience: str
    command: str
    ok: bool
    exit_code: int
    reason: str = ""
    detail: str = ""
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)


def load_config(path: Path) -> dict:
    """Parse `scopecheck.toml`. See `scopecheck.example.toml` for the shape."""
    data = tomllib.loads(path.read_text())
    audiences = [Audience("no-principal")]
    for entry in data.get("audience") or []:
        label = str(entry.get("label") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        external_id = str(entry.get("id") or entry.get("external_id") or "").strip()
        if not label:
            raise ValueError("every [[audience]] needs a label")
        if not kind or not external_id:
            raise ValueError(f"audience {label!r}: both kind and id are required")
        audiences.append(Audience(label, kind, external_id))
    commands = []
    for entry in data.get("command") or []:
        cmd = str(entry.get("cmd") or "").strip()
        if not cmd:
            raise ValueError("every [[command]] needs a cmd")
        expect = tuple(str(e) for e in (entry.get("expect") or []))
        commands.append(Command(str(entry.get("name") or cmd), cmd, expect))
    return {
        "project": data.get("project"),
        "tenant": data.get("tenant"),
        "timeout": data.get("timeout"),
        "fail_markers": tuple(data.get("fail_on") or DEFAULT_FAIL_MARKERS),
        "audiences": audiences,
        "commands": commands,
    }


def first_error_line(result: Result) -> str:
    """The one line worth putting in the matrix: the first stderr line, else the last stdout line."""
    for stream in (result.stderr, result.stdout):
        for line in stream.splitlines():
            if line.strip():
                return line.strip()
    return ""


def evaluate(payload: dict, command: Command, fail_markers) -> tuple[bool, str, str]:
    """(ok, reason, detail) for one finished console run."""
    stdout = payload.get("stdout") or ""
    stderr = payload.get("stderr") or ""
    combined = f"{stdout}\n{stderr}"
    if payload.get("timed_out"):
        return False, "timed out", ""
    exit_code = int(payload.get("exit_code") or 0)
    if exit_code != 0:
        return False, f"exit {exit_code}", ""
    for marker in fail_markers:
        if marker in combined:
            return False, f"output contains {marker!r}", marker
    for want in command.expect:
        if want not in combined:
            return False, f"missing expected {want!r}", want
    return True, "", ""


def run_cell(
    *, project, tenant, audience: Audience, command: Command, timeout: int, fail_markers
) -> Result:
    args = [
        "dev",
        "console",
        "bash",
        "run",
        *scope_flags(project, tenant),
        *audience.flags(),
        "--timeout",
        str(timeout),
        "--",
        command.cmd,
    ]
    try:
        payload, done = rc_json_raw(args, timeout=timeout + 60)
    except RcError as exc:
        return Result(audience.label, command.name, False, 1, "rc failed", str(exc), stderr=exc.stderr)
    if payload is None:
        # No payload at all ⇒ the CLI itself refused (bad scope, missing principal flags on an older
        # `rp`, transport error) — not a verdict about the brain command.
        err = RcError(done.stderr or done.stdout, stderr=done.stderr, returncode=done.returncode)
        reason = (
            "rc has no --principal-kind on `dev console bash run` yet — upgrade rp"
            if unsupported_flag(err, "--principal-kind")
            else "rc failed"
        )
        return Result(
            audience.label, command.name, False, done.returncode or 1, reason,
            (done.stderr or done.stdout).strip(), stderr=done.stderr or "",
        )
    ok, reason, detail = evaluate(payload, command, fail_markers)
    return Result(
        audience.label,
        command.name,
        ok,
        int(payload.get("exit_code") or 0),
        reason,
        detail,
        stdout=payload.get("stdout") or "",
        stderr=payload.get("stderr") or "",
    )


def render_matrix(audiences: list[Audience], commands: list[Command], results: list[Result]) -> str:
    by_key = {(r.audience, r.command): r for r in results}
    width = max([len(c.name) for c in commands] + [7])
    lines = ["| " + "command".ljust(width) + " | " + " | ".join(a.label for a in audiences) + " |"]
    lines.append("|" + "-" * (width + 2) + "|" + "|".join("-" * (len(a.label) + 2) for a in audiences) + "|")
    for command in commands:
        cells = []
        for aud in audiences:
            r = by_key.get((aud.label, command.name))
            cells.append(("✅" if r and r.ok else "❌").ljust(len(aud.label)))
        lines.append("| " + command.name.ljust(width) + " | " + " | ".join(cells) + " |")
    failures = [r for r in results if not r.ok]
    if failures:
        lines.append("")
        lines.append(f"{len(failures)} failing cell(s):")
        for r in failures:
            lines.append(f"  ❌ {r.audience} × {r.command}: {r.reason}")
            line = first_error_line(r)
            if line:
                lines.append(f"       {line}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--project")
    p.add_argument("--tenant")
    p.add_argument("--as", dest="audience", action="append", metavar="LABEL=KIND:EXTERNAL_ID")
    p.add_argument("--cmd", action="append", metavar="COMMAND", help="repeatable bash command")
    p.add_argument("--config", help="scopecheck.toml with audiences, commands and expectations")
    p.add_argument("--fail-on", action="append", metavar="SUBSTRING", help="extra failure marker")
    p.add_argument("--timeout", type=int, help="per-command timeout in seconds (default 120)")
    p.add_argument("--json", action="store_true", help="machine-readable results on stdout")
    args = p.parse_args(argv)

    cfg = {"audiences": None, "commands": [], "fail_markers": DEFAULT_FAIL_MARKERS}
    if args.config:
        try:
            cfg = load_config(Path(args.config))
        except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
            p.error(f"--config {args.config}: {exc}")

    project = args.project or cfg.get("project")
    tenant = args.tenant or cfg.get("tenant")
    timeout = args.timeout or cfg.get("timeout") or 120
    fail_markers = tuple(cfg.get("fail_markers") or DEFAULT_FAIL_MARKERS) + tuple(args.fail_on or [])

    audiences = list(cfg.get("audiences") or [])
    try:
        if args.audience:
            extra = [parse_audience(spec) for spec in args.audience]
            known = {a.label for a in audiences}
            audiences = (audiences or audiences_from_specs(None)) + [
                a for a in extra if a.label not in known
            ]
        elif not audiences:
            audiences = audiences_from_specs(None)
    except ValueError as exc:
        p.error(str(exc))

    commands = list(cfg.get("commands") or [])
    commands += [Command(cmd, cmd) for cmd in (args.cmd or [])]
    if not commands:
        p.error("no commands: pass --cmd or a --config with [[command]] entries")

    results = [
        run_cell(
            project=project,
            tenant=tenant,
            audience=aud,
            command=command,
            timeout=timeout,
            fail_markers=fail_markers,
        )
        for aud in audiences
        for command in commands
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "project": project,
                    "tenant": tenant,
                    "audiences": [a.label for a in audiences],
                    "results": [
                        {
                            "audience": r.audience,
                            "command": r.command,
                            "ok": r.ok,
                            "exit_code": r.exit_code,
                            "reason": r.reason,
                            "first_error_line": first_error_line(r) if not r.ok else "",
                        }
                        for r in results
                    ],
                    "failed": sum(1 for r in results if not r.ok),
                },
                indent=2,
            )
        )
    else:
        print(render_matrix(audiences, commands, results))
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
