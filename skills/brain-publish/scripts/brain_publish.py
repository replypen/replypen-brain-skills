#!/usr/bin/env python3
"""Publish a shared project brain in one command: local checks -> brain_git_sync -> preflight ->
edge/stable gate -> `rc dev brain publish` -> proof. Run from the brain checkout root:

    uv run --no-project python .agents/skills/brain-publish/scripts/brain_publish.py --project <p>

Stage intended edits first and pass --commit-message, or commit them yourself. The SHA published is
always the one brain_git_sync verified at origin/main. --dry-run mutates nothing (no commit, push or
promote) and preflights the current origin/main tip.

Exit: 0 live (or no-op: already live) | 1 publish or proof failed | 2 blocked, nothing published |
3 merge conflicts, nothing published (resolve, stage, rerun the same command).
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "brain-git-sync" / "scripts"))
import brain_git_sync as bgs

CHECKS = SKILLS / "local-brain-work" / "scripts"
RECOVERY = SKILLS / "brain-git-sync" / "references" / "recovery.md"
EXIT_FAILED, EXIT_BLOCKED, EXIT_CONFLICT = 1, 2, 3
WRITE_STAGES = {"publish", "proof"}  # an rc failure here means "maybe live", never "blocked"


class Stop(Exception):
    def __init__(self, code: int, stage: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code, self.stage, self.details = code, stage, details


def note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def classify(repo: Path, project: str) -> None:
    """Only a shared project brain has channels; the kit and tenant brains have other routes."""
    if (repo / "runtime" / "lib").is_dir() and (repo / "plugin.json").is_file():
        raise Stop(EXIT_BLOCKED, "classify", "this is the brain-skills kit, not a brain: run "
                   "`./refresh-brains.sh --release patch` instead")
    marker = repo / ".rootcause.toml"
    if not marker.is_file():
        raise Stop(EXIT_BLOCKED, "classify", f"no .rootcause.toml in {repo}; run from a brain checkout root")
    data = tomllib.loads(marker.read_text("utf-8"))
    if data.get("tenant"):
        raise Stop(EXIT_BLOCKED, "classify", f"tenant brain ({data['tenant']}): it runs from its own "
                   "main and has no channel to publish; brain-git-sync is its publish")
    if data.get("project") != project:
        raise Stop(EXIT_BLOCKED, "classify", f"--project {project!r} but .rootcause.toml binds "
                   f"{data.get('project')!r}; refusing to publish the wrong brain")
    if not ((repo / "skills").is_dir() or (repo / "actions").is_dir()):
        raise Stop(EXIT_BLOCKED, "classify", "no skills/ or actions/: not a shared project brain")


def docker_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def default_checks(skip_docker: bool, warnings: list[str]) -> list[str]:
    py = "uv run --no-project python"
    lint = f"{py} {shlex.quote(str(CHECKS / 'brain_lint.py'))}"
    test = f"{py} {shlex.quote(str(CHECKS / 'brain_test.py'))}"
    if not skip_docker and docker_up():
        return [lint, f"{test} --mode docker"]  # docker tier = import smoke + offline pytest in the prod image
    why = "--skip-docker-tier" if skip_docker else "docker unavailable"
    warnings.append(f"LOWER FIDELITY ({why}): import smoke + tests ran on the host (uv), not in the "
                    "workspace image; image/dependency breakage is not covered")
    return [lint, test]


class Rc:
    def __init__(self, repo: Path, project: str) -> None:
        self.repo, self.project = repo, project

    def __call__(self, *args: str, stage: str) -> dict:
        cmd = ["rc", "dev", "brain", *args, "--project", self.project, "--scope", "project",
               "-o", "json", "--raw-output"]
        try:
            proc = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Stop(EXIT_FAILED if stage in WRITE_STAGES else EXIT_BLOCKED, stage, f"{shlex.join(cmd)}: {error}")
        if proc.returncode:
            detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[-3000:]
            raise Stop(EXIT_FAILED if stage in WRITE_STAGES else EXIT_BLOCKED, stage,
                       f"{shlex.join(cmd)} failed: {detail}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise Stop(EXIT_BLOCKED, stage, f"{shlex.join(cmd)} returned non-JSON output") from None

    def channels(self, stage: str) -> dict[str, dict]:
        status = self("status", stage=stage).get("status") or {}
        source = status.get("brain_source") or {}
        if source.get("read_only"):
            raise Stop(EXIT_BLOCKED, stage, f"read-only brain alias of {source.get('project')}; "
                       "publish from the source project's checkout")
        return {c["channel"]: c for c in status.get("channels") or []}


def relation(git: bgs.Git, old: str, new: str) -> str:
    """How channel commit `old` relates to candidate `new`: equal | ancestor | ahead | diverged | unknown."""
    if old == new:
        return "equal"
    if git.run("cat-file", "-e", f"{old}^{{commit}}", check=False).returncode:
        for c in ("edge", "stable"):  # one refspec per fetch: a missing branch must not sink the other
            git.run("fetch", "--quiet", "origin", f"+refs/heads/{c}:refs/remotes/origin/{c}", check=False)
        if git.run("cat-file", "-e", f"{old}^{{commit}}", check=False).returncode:
            return "unknown"
    if bgs._is_ancestor(git, old, new):
        return "ancestor"
    return "ahead" if bgs._is_ancestor(git, new, old) else "diverged"


def forward_only(name: str, sha: str, channels: dict[str, dict], rel, stage: str = "edge-gate") -> str:
    """Never move a channel to a commit that is not a descendant of what it serves now."""
    current = (channels.get(name) or {}).get("resolved_sha") or ""
    r = rel(current, sha) if current else "missing"
    if r in {"ahead", "diverged", "unknown"}:
        raise Stop(EXIT_BLOCKED, stage, f"{name} is at {current} ({r} vs candidate {sha}); never downgrade "
                   f"or overwrite it. Human decision: reconcile {name} into main or confirm it may be replaced",
                   channel_shas={k: v.get("resolved_sha") for k, v in channels.items()})
    return r


def plan(channel: str, sha: str, channels: dict[str, dict], edge_consumers: int, rel) -> tuple[list[str], str]:
    """SKILL.md step 7 as code. Returns (channels to move in order, human-readable edge state)."""
    def guard(name: str) -> str:
        return forward_only(name, sha, channels, rel)

    guard(channel)
    if channel == "edge":
        if edge_consumers == 0:
            raise Stop(EXIT_BLOCKED, "edge-gate", "no active tenant is pinned to edge; edge cannot be "
                       "created or advanced (BRAIN_EDGE_UNUSED). Publish stable instead")
        return ["edge"], f"{edge_consumers} edge consumers"
    if edge_consumers == 0:
        return ["stable"], "no edge consumers" + ("; edge ref should be GC'd by sync" if "edge" in channels else "")
    edge = guard("edge")
    if edge == "equal":
        return ["stable"], f"{edge_consumers} edge consumers, edge already at candidate"
    return ["edge", "stable"], f"{edge_consumers} edge consumers, edge {edge} -> aligned first"


def preflight_problems(canary: dict) -> tuple[list[str], list[str]]:
    """(offenders, excused) from the server's own verdict: canary.ok is the gate; a `preexisting`
    data fault (the channel's current commit fails identically) is excused by the server, not here."""
    problems = [f"candidate-wide: {canary['error']}"] if canary.get("error") else []
    lint = canary.get("lint") or {}
    if lint and not lint.get("ok", True):
        problems.append(f"server lint: {lint.get('failures')} FAIL\n{lint.get('report', '')[:2000]}")
    excused = []
    for t in canary.get("tenants") or []:
        if t.get("status") == "ok":
            continue
        reasons = "; ".join(f"{d.get('key')} in {d.get('file')}: {d.get('reason')}" for d in t.get("degraded") or [])
        line = f"{t.get('tenant')}: {t.get('status')} {t.get('error') or reasons}".rstrip()
        (excused if t.get("preexisting") else problems).append(line)
    if not canary.get("ok", False) and not problems:
        problems.append("server canary reported not ok")
    return problems, excused


def run(args: argparse.Namespace, receipt: dict) -> int:
    started, timings = time.monotonic(), receipt.setdefault("timings_s", {})

    def lap(stage: str, t0: float) -> None:
        timings[stage] = round(time.monotonic() - t0, 1)

    if not shutil.which("rc"):
        raise Stop(EXIT_BLOCKED, "classify", "rc is not on PATH (brain-dev-upgrade)")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    repo = Path(top.stdout.strip() or ".").resolve()
    classify(repo, args.project)
    git, rc = bgs.Git(repo), Rc(repo, args.project)
    receipt.update(project=args.project, channel=args.channel, repo=str(repo))

    warnings: list[str] = receipt.setdefault("warnings", [])
    checks = [*default_checks(args.skip_docker_tier, warnings), *args.verify_command]
    receipt["verify_commands"] = checks
    for w in warnings:
        note(f"WARNING: {w}")

    t0 = time.monotonic()
    if args.dry_run:
        remote = bgs._fetch(git)
        topo = bgs.topology(git, remote)
        sha = remote
        receipt["sync"] = {"dry_run": True, "local_vs_origin_main": topo.state, "ahead": topo.ahead,
                           "behind": topo.behind, "checks_not_run": True}
        if topo.ahead or bgs.inventory(git).staged:
            warnings.append("local work is not on origin/main yet; this dry-run preflights origin/main only")
    else:
        note(f"-> brain_git_sync (checks rerun after every merge): {len(checks)} commands")
        try:
            result = bgs.synchronize(repo, commit_message=args.commit_message, verify_commands=checks,
                                     project=args.project, reporter=bgs.Reporter(False))
        except bgs.SyncError as error:
            if error.kind == "conflict":
                raise Stop(EXIT_CONFLICT, "git-sync", f"{error}. Resolve with both sides' intent, stage, "
                           f"rerun this command (recovery: {RECOVERY})", **error.details) from error
            raise Stop(EXIT_BLOCKED, "git-sync", str(error), **error.details) from error
        sha = result.final_sha
        if not result.ancestry_verified or git.out("rev-parse", "refs/remotes/origin/main") != sha:
            raise Stop(EXIT_BLOCKED, "git-sync", "sync receipt not verified at origin/main; refusing to publish")
        receipt["sync"] = {k: getattr(result, k) for k in
                           ("local_commits", "remote_commits", "committed_files", "push_attempts")}
    receipt["sha"] = sha
    lap("git_fetch" if args.dry_run else "git_sync", t0)

    t0 = time.monotonic()
    preflight_channels = ["stable", "edge"] if args.channel == "stable" else ["edge"]
    with ThreadPoolExecutor(3) as pool:
        before_f = pool.submit(rc.channels, "status")
        canaries_f = {c: pool.submit(rc, "preflight", "--sha", sha, "--channel", c, stage="preflight")
                      for c in preflight_channels}
        before = before_f.result()
        canaries = {c: f.result().get("canary") or {} for c, f in canaries_f.items()}
    edge_consumers = int(canaries["edge"].get("consumers") or 0)
    moves, edge_state = plan(args.channel, sha, before, edge_consumers, lambda o, n: relation(git, o, n))
    receipt["preflight"] = {c: {k: canaries[c].get(k) for k in ("ok", "templated", "consumers", "checked", "skipped")}
                            for c in moves}
    receipt["edge"] = edge_state
    receipt["before"] = {c: v.get("resolved_sha") for c, v in before.items()}
    for c in moves:
        problems, excused = preflight_problems(canaries[c])
        if excused:
            warnings.append(f"{c}: {len(excused)} pre-existing tenant data fault(s) excused by the server "
                            f"(already failing on the live commit): {', '.join(e.split(':')[0] for e in excused)}")
        if problems:
            raise Stop(EXIT_BLOCKED, "preflight", f"{c} preflight at {sha[:12]} would break tenants; "
                       "nothing published:\n  " + "\n  ".join(problems), offenders=problems)
    receipt["moves"] = moves
    lap("preflight_gate", t0)

    if args.dry_run:
        receipt["status"] = "dry_run"
        return 0

    t0 = time.monotonic()
    published = receipt.setdefault("published", [])
    for c in moves:
        # Re-read right before each move: another publisher may have advanced the channel during
        # preflight. Shrinks the race to rc's own sync; a strict guarantee needs a server-side CAS.
        forward_only(c, sha, rc.channels("publish-recheck"), lambda o, n: relation(git, o, n), "publish-recheck")
        note(f"-> rc dev brain publish --channel {c} --sha {sha}")
        try:
            out = rc("publish", "--channel", c, "--sha", sha, stage="publish")
        except Stop as stop:
            # rc promotes before its own proof: a failed call may still have moved the channel.
            try:
                now = (rc.channels("proof").get(c) or {}).get("resolved_sha")
            except Stop:
                now = None
            published.append({"channel": c, "attempted": True, "now": now, "changed": now == sha or None})
            stop.details["uncertain"] = (f"{c} now resolves {now or 'UNKNOWN'} after the failed publish; "
                                         "check `rc dev brain status --scope project` before retrying")
            raise
        published.append({"channel": c, "old_sha": out.get("old_sha"), "changed": out.get("old_sha") != sha})
    lap("publish", t0)

    after = rc.channels("proof")
    receipt["after"] = {c: v.get("resolved_sha") for c, v in after.items()}
    wrong = [c for c in moves if (after.get(c) or {}).get("resolved_sha") != sha
             or not (after.get(c) or {}).get("matches_origin")]
    if wrong:
        raise Stop(EXIT_FAILED, "proof", f"channel(s) {wrong} do not resolve {sha} after publish; not live")
    if edge_consumers == 0 and "edge" in after:
        warnings.append(f"edge has no consumers but is still reported at {after['edge'].get('resolved_sha')}; "
                        "server GC did not run, check `rc dev brain status`")
    receipt["status"] = "published" if any(p["changed"] for p in published) else "noop"
    timings["total"] = round(time.monotonic() - started, 1)
    return 0


def render(receipt: dict, code: int) -> str:
    sha, status = receipt.get("sha", ""), receipt.get("status")
    if status in {"published", "noop"}:
        head = (f"LIVE {receipt['project']} {', '.join(receipt['moves'])} @ {sha}" if status == "published"
                else f"NO-OP {receipt['project']}: {', '.join(receipt['moves'])} already at {sha}; nothing changed")
        moved = "; ".join(f"{p['channel']} {(p['old_sha'] or '-')[:12]} -> {sha[:12]}"
                          for p in receipt["published"] if p["changed"])
        lines = [head] + ([f"  moved: {moved}"] if moved else [])
    elif status == "dry_run":
        lines = [f"DRY-RUN {receipt['project']}: would publish {' then '.join(receipt['moves'])} @ {sha} "
                 f"(origin/main); local {receipt['sync']['local_vs_origin_main']}"]
    else:
        lines = [f"{status.upper()} at {receipt.get('stage')}: {receipt.get('error')}"]
        if receipt.get("uncertain"):
            lines.append(f"  UNCERTAIN: {receipt['uncertain']}")
        lines.append(f"  published before failure: {receipt['published']}" if receipt.get("published")
                     else "  nothing published")
    ok = status in {"published", "noop", "dry_run"}
    if ok and "local_commits" in receipt.get("sync", {}):
        s = receipt["sync"]
        lines.append(f"  git: ancestry verified at origin/main; {len(s['local_commits'])} local + "
                     f"{len(s['remote_commits'])} remote commits integrated, {len(s['committed_files'])} files "
                     f"committed, {s['push_attempts']} pushes")
    if ok:
        lines += [f"  preflight {c}: ok, {p.get('checked')} tenants compiled, {p.get('consumers')} consumers"
                  for c, p in (receipt.get("preflight") or {}).items()]
        verb = "listed, not run (dry-run)" if status == "dry_run" else "passed"
        lines.append(f"  checks {verb}: " + " | ".join(c.split("/")[-1] for c in receipt["verify_commands"]))
    if receipt.get("edge"):
        lines.append(f"  edge: {receipt['edge']}")
    lines += [f"  WARNING: {w}" for w in receipt.get("warnings", [])]
    timings = ", ".join(f"{k} {v}s" for k, v in (receipt.get("timings_s") or {}).items())
    lines.append(f"  exit {code}" + (f" | {timings}" if timings else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, help="project slug; must match .rootcause.toml")
    p.add_argument("--channel", choices=("stable", "edge"), default="stable")
    p.add_argument("--commit-message", help="commit the already-staged index with this message first")
    p.add_argument("--verify-command", action="append", default=[], metavar="CMD",
                   help="extra check on top of lint + brain_test, rerun after every merge (repeatable)")
    p.add_argument("--skip-docker-tier", action="store_true",
                   help="run brain_test on the host instead of the workspace image (loud warning)")
    p.add_argument("--dry-run", action="store_true", help="no commit/push/promote; preflight origin/main")
    p.add_argument("--json", action="store_true", help="print the receipt as one JSON object")
    args = p.parse_args(argv)
    receipt: dict = {}
    try:
        code = run(args, receipt)
    except Stop as stop:
        code = stop.code
        receipt.update(status={EXIT_FAILED: "failed", EXIT_BLOCKED: "blocked", EXIT_CONFLICT: "conflict"}[code],
                       stage=stop.stage, error=str(stop), **stop.details)
    except bgs.SyncError as error:  # git plumbing outside synchronize (dry-run fetch, relation)
        code = EXIT_BLOCKED
        receipt.update(status="blocked", stage="git", error=str(error))
    print(json.dumps(receipt, sort_keys=True) if args.json else render(receipt, code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
