# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Commits that overlap the window, and which of them *could* explain a new failure signature.

    uv run skills/brain-fleet-report/scripts/correlate.py --date 2026-09-04

Correlation only, never causation: a commit is a candidate when it landed in the 12 hours before a
signature's first occurrence. A commit *after* the first failure cannot explain onset — at most
expansion or recovery. The LLM decides; this script only narrows the search.

collect.py imports `run()`; the CLI entry point re-reads an existing evidence.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fr_common import (  # noqa: E402
    UTC,
    DEFAULT_TZ,
    clip,
    day_bounds,
    find_brain_root,
    load_overlay,
    one_line,
    parse_ts,
    stamp,
    tzinfo,
)

# New layout first; the pre-rebrand checkout names still resolve until a laptop is migrated.
ORG_DIRS = (Path.home() / "code" / "replypen", Path.home() / "code" / "rootcause-org")
BRAIN_PREFIXES = ("replypen-brain-", "rootcause-brain-")
HOST_PATHS = (
    "internal/run", "internal/agent", "internal/brain", "internal/actionexec", "internal/api/runs",
    "runtime/", "internal/placement", "internal/workspace",
)
ONSET_WINDOW = timedelta(hours=12)


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def default_repos(brain_root: Path, members: Sequence[str]) -> list[dict[str, Any]]:
    """Brain checkout + sibling tenant overlays + the host, if they exist on this laptop."""
    repos: list[dict[str, Any]] = [{"id": brain_root.name, "path": str(brain_root), "plane": "brain"}]
    seen = {brain_root.resolve()}
    for org_dir in ORG_DIRS:
        for member in members:
            for prefix in BRAIN_PREFIXES:
                sibling = org_dir / f"{prefix}{member}"
                if sibling.exists() and sibling.resolve() not in seen:
                    seen.add(sibling.resolve())
                    repos.append({"id": sibling.name, "path": str(sibling), "plane": "brain"})
                for overlay in sorted(org_dir.glob(f"{prefix}{member}-tenant-*")):
                    if overlay.resolve() in seen:
                        continue
                    seen.add(overlay.resolve())
                    repos.append({"id": overlay.name, "path": str(overlay), "plane": "tenant_brain"})
    host = next((h for org_dir in ORG_DIRS for h in (org_dir / "replypen", org_dir / "rootcause")
                 if (h / "internal").is_dir()), None)
    if host is not None:
        repos.append({"id": "replypen", "path": str(host), "plane": "host",
                      "relevant_paths": list(HOST_PATHS)})
    return repos


def commits(repo: Path, since: datetime, until: datetime, relevant: Sequence[str]) -> list[dict[str, Any]]:
    """`git log --name-status`, journal-only churn dropped, host filtered to the relevant planes."""
    raw = git(
        repo, "log", "--no-merges", "--name-status", "--date=iso-strict",
        f"--since={since.isoformat()}", f"--until={until.isoformat()}",
        "--pretty=format:%x1e%H%x1f%aI%x1f%an%x1f%s",
    )
    out: list[dict[str, Any]] = []
    for chunk in raw.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, body = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        sha, when, author, subject = parts[0], parts[1], parts[2], parts[3]
        files = [line.split("\t", 1)[1].strip() for line in body.splitlines()
                 if "\t" in line and line.strip()]
        if files and all(f.startswith("journal/") for f in files):
            continue  # server-written journal commits are not changes to the brain
        if not files and subject.lower().startswith("journal"):
            continue
        if relevant and files and not any(f.startswith(p) for f in files for p in relevant):
            continue
        out.append({
            "sha": sha[:9], "at": when, "author": author, "subject": one_line(subject, 120),
            "files": files[:12], "file_count": len(files),
        })
    return out


def candidates(clusters: Sequence[dict[str, Any]], repo_commits: dict[str, list[dict[str, Any]]]
               ) -> list[dict[str, Any]]:
    """For every `new` signature: commits inside [first_seen - 12h, first_seen]."""
    found: list[dict[str, Any]] = []
    for cluster in clusters:
        rec = cluster.get("recurrence") or {}
        if rec.get("state") != "new":
            continue
        first = parse_ts(rec.get("first_seen"))
        if not first:
            continue
        hits = []
        text = f"{cluster.get('title', '')} {cluster.get('detail', '')}"
        for repo_id, rows in repo_commits.items():
            for row in rows:
                when = parse_ts(row["at"])
                if not when or not (first - ONSET_WINDOW <= when <= first):
                    continue
                named = named_in(row["files"], text)
                hits.append({"repo": repo_id, "sha": row["sha"], "at": row["at"],
                             "subject": row["subject"], "paths_named_in_error": named})
        if hits:
            # a commit that touches a file the error names is a far better lead than a neighbour
            hits.sort(key=lambda h: (not h["paths_named_in_error"], h["at"]), reverse=False)
            hits.sort(key=lambda h: not h["paths_named_in_error"])
            found.append({
                "signature": cluster["signature"], "title": cluster["title"],
                "first_seen": rec.get("first_seen"), "focus_count": cluster.get("focus_count", 0),
                "candidates": hits[:8],
            })
    found.sort(key=lambda item: -item["focus_count"])
    return found


def render(repo_commits: dict[str, list[dict[str, Any]]], found: Sequence[dict[str, Any]],
           deploy_state: dict[str, Any], repos: Sequence[dict[str, Any]], tz,
           max_commits: int = 25, max_signatures: int = 25, max_hits: int = 8) -> str:
    """Same view at two densities: `max_commits` caps the per-repo list (0 = counts only)."""
    lines = ["## Commits in the window (correlation only — a commit after the first failure "
             "cannot explain onset)"]
    for repo in repos:
        rows = repo_commits.get(repo["id"], [])
        if not rows:
            continue
        lines.append(f"\n### {repo['id']} ({repo['plane']}) — {len(rows)} commits")
        for row in rows[:max_commits]:
            lines.append(f"- `{row['sha']}` {stamp(row['at'], tz)} {row['author'].split()[0]}: {row['subject']}"
                         + (f" · {row['file_count']} files" if row["file_count"] > 1 else ""))
        if len(rows) > max_commits:
            lines.append(f"- … {len(rows) - max_commits} more in commits.md")
    if not any(repo_commits.values()):
        lines.append("- no non-journal commits in the window on the repos found locally")

    lines.append("\n### Onset candidates for NEW signatures")
    if not found:
        lines.append("- none (no new signature has a commit in the 12 h before its first occurrence)")
    elif len(found) > max_signatures:
        lines.append(f"- (showing {max_signatures} of {len(found)} new signatures; rest in commits.md)")
    for item in found[:max_signatures]:
        lines.append(f"- **{clip(item['title'], 140)}** ({item.get('focus_count', 0)}× on D) — "
                     f"first seen {stamp(item['first_seen'], tz)}")
        for hit in item["candidates"][:max_hits]:
            named = f" · touches {', '.join(hit['paths_named_in_error'][:3])}" if hit["paths_named_in_error"] else ""
            lines.append(f"  - {hit['repo']} `{hit['sha']}` {stamp(hit['at'], tz)}: {hit['subject']}{named}")

    for member, state in (deploy_state or {}).items():
        if not isinstance(state, dict) or not state:
            continue
        lines.extend(deploy_lines(member, state, tz))
        unpromoted = state.get("unpromoted")
        if unpromoted:
            lines.append(f"- unpromoted local host commits: {one_line(json.dumps(unpromoted, default=str), 300)}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ deployed state

DRIFT_HOURS = 24


def deploy_lines(member: str, state: dict[str, Any], tz) -> list[str]:
    """Explicit per-channel publish state with a loud drift flag.

    `rp fleet deploy-state` reports the managed brain cache (`brain`: on-box main vs origin/main)
    and every channel (`channels[]`: resolved vs origin/main, `state` current|behind_main|…) plus
    the promotion history. A stuck sync or a forgotten promote used to hide inside a 200-char
    JSON blob; now it is one flagged line per condition so dashboard and brain logic cannot
    silently diverge (DentAI 2026-09-07: a fix sat unpublished for a day behind a
    "manual reconcile required" cache)."""
    brain = state.get("brain") if isinstance(state.get("brain"), dict) else {}
    host = state.get("host") if isinstance(state.get("host"), dict) else {}
    lines = [f"\n### Deployed state · {member}"]
    if host:
        lines.append(f"- host release `{host.get('release', '?')}` up {float(host.get('uptime_hours') or 0):.1f} h")
    if not brain:
        if not host:
            lines.append(f"- {one_line(json.dumps(state, default=str), 200)}")
        return lines
    flags: list[str] = []
    main_sha = str(brain.get("main_sha") or "")[:12]
    origin_sha = str(brain.get("origin_sha") or "")[:12]
    cache_state = str(brain.get("state") or "?")
    lines.append(f"- managed cache main `{main_sha or '?'}` vs origin/main `{origin_sha or '?'}` · {cache_state}"
                 + (f" · synced {stamp(brain['synced_at'], tz)}" if brain.get("synced_at") else ""))
    if cache_state not in ("current", "behind") or brain.get("dirty") or (brain.get("ahead") or 0) > 0:
        flags.append(f"managed cache `{cache_state}`"
                     + (" with unpublished on-box commits" if (brain.get("ahead") or 0) > 0 else "")
                     + (" and a dirty tree" if brain.get("dirty") else "")
                     + " — `rp dev brain sync` will demand a manual reconcile; see brain-publish")
    latest_promotion: dict[str, datetime] = {}
    for promo in brain.get("promotions") or []:
        if not isinstance(promo, dict):
            continue
        at = parse_ts(promo.get("finished_at") or promo.get("created_at"))
        channel = str(promo.get("channel") or "")
        if at and channel and (channel not in latest_promotion or at > latest_promotion[channel]):
            latest_promotion[channel] = at
    now = datetime.now(UTC)
    for channel in brain.get("channels") or []:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("channel") or "?")
        resolved = str(channel.get("resolved_sha") or "")[:12]
        ch_state = str(channel.get("state") or "?")
        promoted = latest_promotion.get(name)
        lines.append(f"- channel `{name}` @ `{resolved or '?'}` · {ch_state}"
                     + (f" · last promoted {stamp(promoted, tz)}" if promoted else ""))
        if ch_state == "current":
            continue
        age_h = (now - promoted).total_seconds() / 3600 if promoted else None
        if ch_state != "behind_main" or age_h is None or age_h >= DRIFT_HOURS:
            since = f"for {age_h:.0f} h" if age_h is not None else "with no promotion on record"
            flags.append(f"channel `{name}` is `{ch_state}` {since} (origin/main `{origin_sha}`, "
                         f"channel `{resolved}`) — publish or consciously park it")
    for flag in flags:
        lines.append(f"- ⚠ PUBLISH DRIFT: {flag}")
    return lines


# Every repo has a SKILL.md and a script.py; matching those links every commit to every error.
UBIQUITOUS = {"skill", "readme", "agents", "index", "main", "script", "config", "init", "setup",
              "schema", "test", "utils", "common", "__init__"}


def named_in(files: Sequence[str], blob: str) -> list[str]:
    """Files whose basename is distinctive enough that seeing it in an error means something."""
    out = []
    for path in files:
        base = str(path).split("/")[-1]
        stem = base.rsplit(".", 1)[0].lower()
        if len(stem) >= 5 and stem not in UBIQUITOUS and base in blob:
            out.append(path)
    return out


def implicated(repo_commits: dict[str, list[dict[str, Any]]], clusters: Sequence[dict[str, Any]]
               ) -> list[dict[str, Any]]:
    """Commits touching a file whose name appears in an error signature we are actually reporting."""
    blob = " ".join(f"{c.get('title', '')} {c.get('detail', '')}" for c in clusters)
    hits: list[dict[str, Any]] = []
    for repo_id, rows in repo_commits.items():
        for row in rows:
            named = named_in(row["files"], blob)
            if named:
                hits.append({"repo": repo_id, **row, "paths_named_in_error": named[:3]})
    hits.sort(key=lambda h: h["at"], reverse=True)
    return hits


def render_brief(repo_commits: dict[str, list[dict[str, Any]]], found: Sequence[dict[str, Any]],
                 named: Sequence[dict[str, Any]], deploy_state: dict[str, Any], tz) -> str:
    """Digest density: counts per repo, then only the commits that could actually explain something.

    The full per-repo log lives in commits.md — pasting 200 subjects into the digest buries the
    handful that correlate with a failure.
    """
    lines = ["## Commits in the window (correlation only — a commit after the first failure "
             "cannot explain onset; full log in commits.md)"]
    counts = [f"{repo_id} {len(rows)}" for repo_id, rows in repo_commits.items() if rows]
    lines.append("- " + (" · ".join(counts) if counts else
                         "no non-journal commits in the window on the repos found locally"))

    lines.append("\n### Onset candidates for NEW signatures")
    # A commit that touches a file the error names is a lead. A commit that merely landed in the
    # same 12 hours is a coincidence — repeated once per signature it produced ~25 identical digest
    # lines and zero decisions, so the coincidences collapse into one count.
    strong_items = [i for i in found if any(h["paths_named_in_error"] for h in i["candidates"])]
    weak = len(found) - len(strong_items)
    if not strong_items:
        lines.append("- none (no new signature has a commit touching a file it names)")
    for item in strong_items[:10]:
        lines.append(f"- **{clip(item['title'], 140)}** ({item.get('focus_count', 0)}× on D) — "
                     f"first seen {stamp(item['first_seen'], tz)}")
        for hit in [h for h in item["candidates"] if h["paths_named_in_error"]][:3]:
            lines.append(f"  - {hit['repo']} `{hit['sha']}` {stamp(hit['at'], tz)}: {hit['subject']} · "
                         f"touches {', '.join(hit['paths_named_in_error'][:3])}")
    if len(strong_items) > 10:
        lines.append(f"- … {len(strong_items) - 10} more with a file-level hit in commits.md")
    if weak:
        lines.append(f"- {weak} new signature(s): commits landed in the 12 h window but none touched "
                     f"a file the error names — coincidence unless you have another reason "
                     f"(per-signature detail in commits.md)")

    if named:
        lines.append("\n### Commits touching a file named in an error signature")
        for hit in named[:12]:
            lines.append(f"- {hit['repo']} `{hit['sha']}` {stamp(hit['at'], tz)} "
                         f"{hit['author'].split()[0]}: {hit['subject']} · "
                         f"touches {', '.join(hit['paths_named_in_error'])}")
        if len(named) > 12:
            lines.append(f"- … {len(named) - 12} more in commits.md")

    for member, state in (deploy_state or {}).items():
        if isinstance(state, dict) and state:
            lines.extend(deploy_lines(member, state, tz))
    return "\n".join(lines) + "\n"


def run(brain_root: Path, members: Sequence[str], overlay, clusters: Sequence[dict[str, Any]],
        days: Sequence[date], tz, deploy_state: dict[str, Any]
        ) -> tuple[str, str, list[dict[str, Any]]]:
    repos = [dict(r) for r in overlay.get("repos", [])] or default_repos(brain_root, members)
    since, _ = day_bounds(days[0], tz)
    _, until = day_bounds(days[-1], tz)
    until = min(until, datetime.now(UTC) + timedelta(hours=1))
    repo_commits: dict[str, list[dict[str, Any]]] = {}
    for repo in repos:
        path = Path(str(repo.get("path", ""))).expanduser()
        if not (path / ".git").exists():
            continue
        repo_commits[str(repo.get("id") or path.name)] = commits(
            path, since, until, [str(p) for p in (repo.get("relevant_paths") or [])]
        )
    found = candidates(clusters, repo_commits)
    full = render(repo_commits, found, deploy_state, repos, tz, max_commits=40)
    brief = render_brief(repo_commits, found, implicated(repo_commits, clusters), deploy_state, tz)
    return full, brief, found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="focus day of an existing report directory")
    parser.add_argument("--overlay", help="shared overlay directory (default: brain _internal/fleet-report)")
    parser.add_argument("--report-id")
    args = parser.parse_args()
    brain_root = find_brain_root()
    overlay = load_overlay(brain_root, args.overlay)
    tz = tzinfo(str(overlay.get("timezone", DEFAULT_TZ)))
    report_id = args.report_id or str(overlay.get("report_id") or brain_root.name.replace("replypen-brain-", "").replace("rootcause-brain-", ""))
    out_dir = brain_root / ".replypen" / "fleet-report" / report_id / args.date
    evidence = json.loads((out_dir / "evidence.json").read_text(encoding="utf-8"))
    days = [date.fromisoformat(d) for d in evidence["window"]["context_days"]] + [
        date.fromisoformat(evidence["window"]["focus"])]
    markdown, _brief, found = run(brain_root, evidence["projects"], overlay, evidence["clusters"],
                                  days, tz, evidence.get("deploy_state") or {})
    (out_dir / "commits.md").write_text(markdown, encoding="utf-8")
    print(f"{len(found)} onset candidate signature(s) → {out_dir / 'commits.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
