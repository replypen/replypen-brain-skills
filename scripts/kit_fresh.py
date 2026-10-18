"""Run a kit script from fresh `origin/main` when the installed clone is behind it.

The shared clone (`~/.replypen-brain-skills`) sits on a release tag; fixes land on main first. A report
made with an older script silently undercounts, so analysis entry points call, before any work:

    if __name__ == "__main__":
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import kit_fresh  # re-execs from origin/main when the installed kit is behind
        kit_fresh.ensure(__file__)
        sys.exit(main())

`ensure` is a no-op in a dev checkout (HEAD on a branch, or detached but not behind main: you run your
own tree), outside git (prod, snapshots) and with `RP_KIT_FRESH=0`. Otherwise it fetches main (bounded, at most every 10 min) and,
when `skills/` or `scripts/` differ from the clone's HEAD, re-execs the same command from an immutable
snapshot `~/.cache/replypen-kit/<sha>` (`git archive`, atomic rename: safe with parallel agents).
Offline → one warning line, then the installed copy runs. Skill prose already loaded into an agent's
context is not refreshed; only the code is.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

CACHE = Path(os.environ.get("RP_KIT_CACHE", Path.home() / ".cache" / "replypen-kit"))
FETCH_EVERY = 600  # seconds
KEEP_DAYS = 7


def _git(root: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)


def _snapshot(root: Path, sha: str) -> Path:
    snap = CACHE / sha[:12]
    if snap.is_dir():
        return snap
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f".{sha[:12]}.{os.getpid()}"
    tmp.mkdir()
    archive = subprocess.run(["git", "-C", str(root), "archive", sha], capture_output=True, check=True, timeout=60)
    tar_path = tmp / ".archive.tar"
    tar_path.write_bytes(archive.stdout)
    with tarfile.open(tar_path) as tar:
        tar.extractall(tmp, filter="data")
    tar_path.unlink()
    try:
        tmp.rename(snap)
    except OSError:  # a parallel run won the race: same sha, same bytes
        shutil.rmtree(tmp, ignore_errors=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in CACHE.iterdir():
        if old != snap and old.is_dir() and old.stat().st_mtime < cutoff:
            shutil.rmtree(old, ignore_errors=True)
    return snap


def ensure(script: str) -> None:
    if os.environ.get("RP_KIT_FRESH"):
        return
    path = Path(script).resolve()
    root = path.parents[3]
    if not (root / ".git").exists() or _git(root, "symbolic-ref", "-q", "HEAD").returncode == 0:
        return
    try:
        head = root / _git(root, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip()
        if not head.exists() or time.time() - head.stat().st_mtime > FETCH_EVERY:
            if _git(root, "fetch", "-q", "--tags", "origin", "main").returncode != 0:
                raise RuntimeError("fetch failed")
        main = _git(root, "rev-parse", "--verify", "origin/main").stdout.strip()
        if not main:
            raise RuntimeError("no origin/main")
        if (_git(root, "merge-base", "--is-ancestor", "HEAD", main).returncode != 0  # ahead/diverged: local work
                or _git(root, "diff", "--quiet", "HEAD", main, "--", "skills", "scripts").returncode == 0):
            return
        snap = _snapshot(root, main)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"kit: ⚠ could not check origin/main ({exc}); running the installed copy, which may be stale",
              file=sys.stderr)
        return
    here = _git(root, "describe", "--tags").stdout.strip() or "HEAD"
    print(f"kit: {path.name} runs from origin/main {main[:8]} (installed {here} is behind; "
          f"RP_KIT_FRESH=0 runs the installed copy)", file=sys.stderr)
    target = snap / path.relative_to(root)
    os.execve(sys.executable, [sys.executable, str(target), *sys.argv[1:]], {**os.environ, "RP_KIT_FRESH": main})
