#!/usr/bin/env python3
"""One source of truth for triage guidance, per scope.

The triage prompt composes two homes: the scoped triage policy in the ReplyPen database (`rp project
triage policy`) and every brain doc tagged `include_in: [triage]`. Both at the SAME scope is a
second, unversioned copy that drifts (DentAI 2026-10: the policy said "skip a bare thanks",
triage.md said "the first thanks gets one graag gedaan"). Scopes are compared like for like only:

  * project brain     ↔ the project-level policy
  * tenant overlay    ↔ that tenant's policy (and a NOTICE: the triage step reads only the project
                        brain, so a tenant doc tagged `triage` reaches the main agent, not triage)

A project policy next to a tenant overlay's doc, or the reverse, is fine. Read-only; never fails a
publish (brain_publish.py prints it as a warning).

    python3 triage_source.py [--brain DIR]      # exit 0 always; prints findings
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
INLINE_RE = re.compile(r"^include_in:\s*\[([^\]]*)\]", re.M)
BLOCK_RE = re.compile(r"^include_in:\s*\n((?:\s+-\s*\S+\s*\n?)+)", re.M)


def _marker(root: Path) -> dict:
    for name in (".replypen.toml", ".rootcause.toml"):
        p = root / name
        if p.is_file():
            try:
                return tomllib.loads(p.read_text("utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                return {}
    return {}


def _include_in(text: str) -> list[str]:
    m = FRONT_RE.match(text)
    if not m:
        return []
    front = m.group(1) + "\n"
    if hit := INLINE_RE.search(front):
        return [t.strip().strip("'\"") for t in hit.group(1).split(",") if t.strip()]
    if hit := BLOCK_RE.search(front):
        return [ln.strip()[1:].strip().strip("'\"") for ln in hit.group(1).splitlines() if ln.strip()]
    return []


def triage_docs(root: Path) -> list[str]:
    """Tracked .md files tagged `include_in: [triage]` (the host's selection, before ignore rules)."""
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
                             capture_output=True, text=True).stdout.split("\0")
    out = []
    for rel in filter(None, tracked):
        try:
            text = (root / rel).read_text("utf-8", errors="replace")
        except OSError:
            continue
        if "triage" in _include_in(text):
            out.append(rel)
    return sorted(out)


def db_policy(project: str, tenant: str | None, root: Path) -> str | None:
    """Guidance at exactly this scope, or None when rp cannot answer (offline, no login)."""
    cli = "rp" if shutil.which("rp") else "rc" if shutil.which("rc") else None
    if cli is None:
        return None
    cmd = [cli, "--project", project]
    cmd += ["--scope", "tenant", "--tenant", tenant] if tenant else ["--scope", "project"]
    cmd += ["project", "triage", "policy", "get", "-o", "json", "--raw-output"]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=60)
        return str(json.loads(proc.stdout).get("guidance") or "").strip() if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError):
        return None


def check(root: Path) -> list[str]:
    marker = _marker(root)
    project, tenant = marker.get("project"), marker.get("tenant")
    if not project:
        return []
    docs = triage_docs(root)
    findings = []
    if tenant and docs:
        findings.append(f"{', '.join(docs)}: the triage step reads only the project brain; in a tenant "
                        "overlay this doc reaches the main agent, not triage")
    if not docs:
        return findings
    policy = db_policy(project, tenant, root)
    scope = f"tenant {tenant}" if tenant else "project"
    if policy is None:
        findings.append(f"could not read the {scope} triage policy via rp; one-source check skipped")
    elif policy:
        set_cmd = f"rp --project {project} " + (f"--scope tenant --tenant {tenant}" if tenant else "--scope project")
        findings.append(f"two triage sources at {scope} scope: the database policy ({len(policy)} chars) and "
                        f"{', '.join(docs)}. Keep one: move the policy text into the brain doc and clear it "
                        f"(`{set_cmd} project triage policy set \"\"`), or drop the doc's `triage` tag")
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", default=".", help="brain checkout (default: cwd)")
    args = ap.parse_args(argv)
    findings = check(Path(args.brain).resolve())
    for f in findings:
        print(f"NOTICE  triage-source: {f}")
    if not findings:
        print("triage-source: one source per scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
