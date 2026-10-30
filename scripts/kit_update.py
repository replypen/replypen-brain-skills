"""Keep the installed kit clone on the newest release tag without anyone remembering to.

The shared clone (`~/.replypen-brain-skills`) sits on whatever tag `install.sh` last saw. Only
`refresh-brains.sh` (the releaser's laptop) re-points it, so colleagues ran months-old skills
(2026-10-30: v0.13.8 and v0.3.27 against v0.14.24). Paths agents already run call `maybe_update`:
the managed pre-commit hook (`brain_hygiene.py --staged`), `brain_run.py`/`brain_test.py` and
`brain_git_sync.py`. Stdlib only: the hook runs it under a bare `python3`.

`maybe_update(brain)`:
  1. at most once a day (once an hour after a failed check) lists the remote `vX.Y.Z` tags with a
     bounded `git ls-remote`; a newer tag → fetch + detached checkout of it. Offline/slow GitHub →
     one warning line, the installed copy keeps working;
  2. every call (local, no network): a brain linked to this kit that misses a link for one of its
     skills re-runs this kit's own `install.sh` on that brain (new skills, refreshed hook).

After a tag move the caller re-execs itself (once: the child runs with `RP_KIT_AUTOUPDATE=0`).
No-op for a developer checkout (HEAD on a branch, a git worktree, dirty tracked files, HEAD not
exactly at a release tag), an explicit `RC_BRAIN_KIT_TAG` pin, `CI`, and `RP_KIT_AUTOUPDATE=0`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("RP_KIT_CACHE", Path.home() / ".cache" / "replypen-kit"))
CHECK_EVERY = 86400  # seconds between remote tag checks
RETRY_EVERY = 3600  # after a failed check (offline)
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _key(tag: str) -> tuple[int, int, int]:
    m = TAG.fullmatch(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else (-1, -1, -1)


def _installed_tag() -> str | None:
    """The release tag an install clone sits on, or None for anything that is not one."""
    if os.environ.get("RP_KIT_AUTOUPDATE") == "0" or os.environ.get("RC_BRAIN_KIT_TAG") or os.environ.get("CI"):
        return None
    if not (ROOT / ".git").is_dir():  # snapshot, plugin copy, or a `git worktree` (.git is a file)
        return None
    if _git("symbolic-ref", "-q", "HEAD").returncode == 0:  # on a branch: someone's working tree
        return None
    if _git("diff", "--quiet", "HEAD").returncode != 0:
        return None
    tags = [t for t in _git("tag", "--points-at", "HEAD").stdout.split() if TAG.fullmatch(t)]
    return max(tags, key=_key) if tags else None


def _latest_remote_tag() -> str:
    out = _git("ls-remote", "--tags", "--refs", "origin", "v*", timeout=10)
    if out.returncode:
        raise RuntimeError((out.stderr.strip().splitlines() or ["git ls-remote failed"])[-1])
    tags = [line.rsplit("/", 1)[-1] for line in out.stdout.splitlines()]
    tags = [t for t in tags if TAG.fullmatch(t)]
    if not tags:
        raise RuntimeError("origin lists no release tags")
    return max(tags, key=_key)


@contextlib.contextmanager
def _lock(path: Path):
    """Non-blocking exclusive lock; yields False when another agent holds it."""
    import fcntl

    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def _update_tag(installed: str) -> str | None:
    """Move the clone to the newest remote tag when due; returns the new tag, if any."""
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = CACHE / f"tag-check-{hashlib.sha1(str(ROOT).encode()).hexdigest()[:12]}"
    with contextlib.suppress(OSError):
        age = time.time() - stamp.stat().st_mtime
        if age < (RETRY_EVERY if stamp.read_text().strip() == "fail" else CHECK_EVERY):
            return None
    with _lock(stamp.with_suffix(".lock")) as held:
        if not held:
            return None
        try:
            latest = _latest_remote_tag()
            if _key(latest) <= _key(installed):
                stamp.write_text("ok\n")
                return None
            if _git("fetch", "-q", "--tags", "origin", timeout=120).returncode:
                raise RuntimeError(f"could not fetch {latest}")
            done = _git("checkout", "-q", latest, timeout=60)
            if done.returncode:
                raise RuntimeError(f"checkout {latest} failed: {done.stderr.strip()}")
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            stamp.write_text("fail\n")
            print(f"kit: ⚠ could not check for a newer kit release ({exc}); "
                  f"keeping {installed} (retry in an hour)", file=sys.stderr)
            return None
        stamp.write_text("ok\n")
        return latest


def _relink(brain: Path) -> bool:
    """Re-run this kit's install.sh on a brain linked to it that misses one of its skills."""
    skills = brain / ".agents" / "skills"
    anchor = skills / "local-brain-work"
    try:
        if not anchor.is_symlink() or anchor.resolve().parent.parent != ROOT.resolve():
            return False
    except OSError:
        return False
    shipped = {d.name for d in (ROOT / "skills").iterdir() if (d / "SKILL.md").is_file()}
    if all((skills / name).is_symlink() for name in shipped):
        return False
    done = subprocess.run(["bash", str(ROOT / "install.sh"), str(brain)], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "RC_BRAIN_KIT": str(ROOT)})
    if done.returncode:
        last = (done.stderr.strip().splitlines() or ["install.sh failed"])[-1]
        print(f"kit: ⚠ could not relink {brain.name} to the kit ({last})", file=sys.stderr)
        return False
    return True


def maybe_update(brain: Path | str | None = None, *, reexec: bool = True) -> None:
    """Best effort, never raises: an update problem must not block the caller's real work. After a
    tag move the calling process restarts (`reexec`): modules it already imported, and defaults it
    derived from them (the workspace image tag), belong to the previous release."""
    try:
        installed = _installed_tag()
        if not installed:
            return
        new = _update_tag(installed)
        relinked = _relink(Path(brain).resolve()) if brain else False
        if new:
            print(f"kit: updated {installed} → {new} ({ROOT}); start a fresh agent session to reload "
                  "changed skill text", file=sys.stderr)
            if reexec:
                argv = getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv]
                sys.stdout.flush()
                sys.stderr.flush()
                os.execve(sys.executable, [sys.executable, *argv[1:]], {**os.environ, "RP_KIT_AUTOUPDATE": "0"})
        elif relinked:
            print(f"kit: linked new kit skills into {Path(brain).name}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — see docstring
        print(f"kit: ⚠ update check skipped ({exc})", file=sys.stderr)


if __name__ == "__main__":
    maybe_update(sys.argv[1] if len(sys.argv) > 1 else None, reexec=False)
