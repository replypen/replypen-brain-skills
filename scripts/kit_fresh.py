"""Run a kit script from the newest verified release tag when the installed clone is behind it.

The shared clone (`~/.replypen-brain-skills`) moves to a new release once a day (kit_update.py). A report
made with an older script silently undercounts, so analysis entry points call, before any work:

    if __name__ == "__main__":
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        import kit_fresh  # re-execs from the newest verified release when the installed kit is behind
        kit_fresh.ensure(__file__)
        sys.exit(main())

`ensure` is a no-op in a dev checkout (HEAD on a branch, or detached but not behind the newest tag: you
run your own tree), outside git (prod, snapshots) and with `RP_KIT_FRESH=0`. Otherwise it fetches tags
(bounded, at most every 10 min) and, when `skills/` or `scripts/` differ from the clone's HEAD, re-execs
the same command from an immutable snapshot `~/.cache/replypen-kit/<sha>` (`git archive`, atomic rename:
safe with parallel agents). Never floating `main`: only a tag `kit_update.verify_tag` accepts (signed by a
pinned release key) runs. Offline or an unverified newest tag → one warning line, then the installed copy
runs. Skill prose already loaded into an agent's context is not refreshed; only the code is.
"""

from __future__ import annotations

import os
import re
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
    import kit_update  # this clone's own signing pin decides what may run (sibling module, stdlib only)

    try:
        head = root / _git(root, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip()
        if not head.exists() or time.time() - head.stat().st_mtime > FETCH_EVERY:
            if _git(root, "fetch", "-q", "--tags", "origin").returncode != 0:
                raise RuntimeError("fetch failed")
        tags = [t for t in _git(root, "tag", "-l", "v*").stdout.split() if kit_update.TAG.fullmatch(t)]
        if not tags:
            raise RuntimeError("no release tags")
        tag = max(tags, key=kit_update._key)
        commit = kit_update.verify_tag(tag, root)  # the newest or nothing, as in kit_update._move
        if (_git(root, "merge-base", "--is-ancestor", "HEAD", commit).returncode != 0  # ahead/diverged: local work
                or _git(root, "diff", "--quiet", "HEAD", commit, "--", "skills", "scripts").returncode == 0):
            return
        snap = _snapshot(root, commit)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:  # kit_update.Untrusted included
        print(f"kit: ⚠ could not check for a newer release ({exc}); running the installed copy, which may be "
              "stale", file=sys.stderr)
        return
    here = _git(root, "describe", "--tags").stdout.strip() or "HEAD"
    print(f"kit: {path.name} runs from release {tag} {commit[:8]} (installed {here} is behind; "
          f"RP_KIT_FRESH=0 runs the installed copy)", file=sys.stderr)
    target = snap / path.relative_to(root)
    env = {**os.environ, "RP_KIT_FRESH": commit}
    # This interpreter's environment was built from the INSTALLED script's dependency header; main's
    # may add a package (ModuleNotFoundError), so a changed header gets its own `uv run --script` env.
    uv = shutil.which("uv")
    if uv and _script_header(target) != _script_header(path):
        os.execve(uv, [uv, "run", "--script", str(target), *sys.argv[1:]], env)
    os.execve(sys.executable, [sys.executable, str(target), *sys.argv[1:]], env)


def _script_header(path: Path) -> str:
    """The PEP 723 `# /// script` block (the inline dependency header `uv run` builds the env from)."""
    match = re.search(r"(?ms)^# /// script\s*$(.*?)^# ///\s*$", path.read_text("utf-8", errors="replace"))
    return match.group(1) if match else ""
