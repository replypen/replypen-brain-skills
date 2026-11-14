"""Keep the installed kit clone on the newest release tag without anyone remembering to.

The shared clone (`~/.replypen-brain-skills`) sits on whatever tag `install.sh` last saw. Only
`refresh-brains.sh` (the releaser's laptop) re-points it, so colleagues ran months-old skills
(2026-10-30: v0.13.8 and v0.3.27 against v0.14.24). Two triggers, one engine. Stdlib only: the
managed pre-commit hook runs it under a bare `python3`.

- **Daily job** (`brain-kit-refresh`, 06:45 local, t3-schedule: launchd on macOS, systemd user timer on
  WSL). Its `--pre-command` (`--bootstrap`) fetches tags and runs THE NEWEST VERIFIED TAG's copy of this
  file with `--apply`, so a release that fixes the updater's apply pass lands on its own. `--apply`
  moves the clone, relinks every linked brain and installs the kit into unlinked brain checkouts it
  discovers. The job's cheap-model thread runs `--job`: same pass, then `thread: SETTLE` unless a failure
  needs the user. `install.sh` and the usage path arm the job (`_ensure_schedule`, idempotent).
- **Usage fallback** `maybe_update(brain)` from paths agents already run (managed pre-commit hook,
  `brain_run.py`/`brain_test.py`, `brain_git_sync.py`): the same tag check at most once a day (hourly
  after a failure), the caller's brain relinked when stale, the job armed when missing. Never raises.

Safety: one lock (`<clone>/.git/replypen-kit.lock`) around tag move, relink and arming, with
eligibility rechecked inside it. No tag move while another linked brain is mid git operation (commit,
rebase, merge): agents work there. No relink of a brain mid-commit. A failed checkout restores the
installed tag.

Trust: only a release tag SSH-signed by a key in ALLOWED_SIGNERS (this installed file, never the fetched
release; no env override) is checked out or executed (`verify_tag`). The newest tag failing that blocks
the update, fail closed: the installed release keeps working and the run reports FAILED with the fix.

No-op for a developer checkout (HEAD on a branch, a git worktree, dirty tracked files, HEAD not exactly at
a release tag), an explicit `RC_BRAIN_KIT_TAG` pin, `CI`, and `RP_KIT_AUTOUPDATE=0`. Job opt-out:
`install.sh --no-schedule` (or `t3-schedule remove brain-kit-refresh`); `RP_KIT_SCHEDULE=0` skips arming.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("RP_KIT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
CACHE = Path(os.environ.get("RP_KIT_CACHE", Path.home() / ".cache" / "replypen-kit"))
CHECK_EVERY = 86400  # seconds between remote tag checks (usage path)
RETRY_EVERY = 3600  # after a failed or deferred check
STALE_AFTER = 3 * 86400  # deferred this long (offline, a stuck rebase) → the job surfaces it
LOCK_WAIT = float(os.environ.get("RP_KIT_LOCK_WAIT", "120"))  # the job waits; usage callers never do
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
JOB, JOB_AT = "brain-kit-refresh", "06:45"  # after skills-refresh
BUSY = ("index.lock", "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
BRAIN_ROOTS = ("code", "src", "dev", "projects", "Projects")
BRAIN_NAMES = ("*brain*", "*-replypen-*")  # replypen-brain-*, rootcause-brain-*, kampadmin-replypen-common
HOOKS = ("pre-commit", "pre-push")  # install.sh writes both; `--no-hooks` leaves NO_HOOKS in the git dir
NO_HOOKS = "replypen-kit-no-hooks"
# A git hook exports these (GIT_INDEX_FILE always, GIT_DIR in a linked worktree): inherited, the kit clone's
# own git commands would read, or write, the committing brain's index instead.
GIT_REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
KEEP_OPEN = "thread: KEEP OPEN (the user should see this)"
SETTLE = ('thread: SETTLE (nothing for the user): call the T3 MCP tool t3_thread_organize with '
          '{"action":"settle"} as your last tool call')


def _env(**extra: str) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if k not in GIT_REPO_ENV}, **extra}


def _git(*args: str, cwd: Path = ROOT, timeout: int = 15) -> subprocess.CompletedProcess:
    env = _env(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout, env=env)


def _key(tag: str) -> tuple[int, int, int]:
    m = TAG.fullmatch(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else (-1, -1, -1)


def _last(text: str, default: str) -> str:
    return (text.strip().splitlines() or [default])[-1]


# --- release trust -------------------------------------------------------------------------------
# allowed_signers line(s) of the release key(s); several = entries joined by a literal \n. Mirrored in
# install.sh (check-release-coherence.sh keeps both byte-equal). Rotation: RELEASING.md "Signed releases".
ALLOWED_SIGNERS = 'pjmuller namespaces="git" ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDlzBlNIEQq+VIuJRLVILwIikQ3B0uytO32FlNsNfT1GalT3yD+GB5cgSrXFmkz3updjSkTdSOkL45k3C3BZgvYfDvr1lKdaKLxhrL1WA0fux0R94DwGa+pYR1B4tox/5HD/01h8XyM9OXLVW9ZRJzMfDhIK+tk7a8VBX9A2NgbrQIFURzSbvjoSnVsbY/okj3QYuTwKjzC5Ah6PWh1LMyUqqF/TQKAozVeJiBhSOOpDTuVk/76BTCu/LsR9XIPtmb6jbRwE3HfeAZ0+CuigoZwy0eYZ2M3P0viGLIurygfIogMc+RMGChUlJcaXzKhA+t69xDt+5DYImSbCyo19ttZTZeQx8VYMK5MzC9pbSVfJgpLG+C4m22iJwRenH1ARjqAuctwIKUOa42LHxuu6fvdCMSmu+2F3vrDnUxdlFd8/nFEIDh74qhvVyobhWqMJKCqT5rBlBeALr9DdJ4cifhZdyEc8BtUdPvME1CTKGcOHAQad6smNmSLkcYSrYxCzK+hCki9Gz25gOOLbR6Hdl/Hf3TXWLIdtBsA3aj3GUmb9GchFTGLd0RN22VgOOQBtrsB+VvI4jndwIfEDTemqlWqI+zcscIJQhIxyLOktewjSpPFFFYMew5zjRk66MiG7rGtEDv7j4LmGabz5fls96XHLFApnMpZ59otjiIv5Tclfw==\nbynov namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO5NM0sSx4R8Yo+eF3eCYq+rvoBIsNn6+a0top59nKAC'
MIN_VERIFYING_RELEASE = "v0.14.35"  # first release whose updater verifies: never run an older one
FIX = ('fix: release tags must be SSH-signed by a key in ALLOWED_SIGNERS (scripts/kit_update.py; RELEASING.md '
       '"Signed releases"). A tag nobody released → delete it on GitHub and tell the kit owner; never disable the check')


class Untrusted(RuntimeError):
    """A release tag this kit must not check out or execute."""


def verify_tag(tag: str, repo: Path = ROOT) -> str:
    """The commit `tag` names, once it is an annotated tag of exactly that name over a commit, SSH-signed by
    a pinned key. Raises Untrusted: the caller keeps the installed release (fail closed, never bricks)."""
    if not TAG.fullmatch(tag):
        raise Untrusted(f"{tag!r} is not a release tag name")
    m = re.search(r"(\d+)\.(\d+)", _git("--version", cwd=repo).stdout)
    if not m or (int(m[1]), int(m[2])) < (2, 34) or not shutil.which("ssh-keygen"):
        raise Untrusted("checking release signatures needs Git >= 2.34 and OpenSSH ssh-keygen: install or update them")
    obj = _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", cwd=repo).stdout.strip()
    body = _git("cat-file", "tag", obj, cwd=repo).stdout if obj else ""
    head = body.split("\n", 3)
    if (len(head) < 4 or head[1] != "type commit" or head[2] != f"tag {tag}"
            or "-----BEGIN SSH SIGNATURE-----" not in body
            or any(f"-----BEGIN {kind}-----" in body for kind in ("PGP SIGNATURE", "SIGNED MESSAGE"))):
        raise Untrusted(f"{tag} is not an SSH-signed annotated tag named {tag}; {FIX}")
    with tempfile.NamedTemporaryFile("w", prefix="replypen-kit-signers.") as trust:
        trust.write(ALLOWED_SIGNERS + "\n")
        trust.flush()
        done = _git("-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen", "-c", "gpg.minTrustLevel=fully",
                    "-c", f"gpg.ssh.allowedSignersFile={trust.name}", "verify-tag", obj, cwd=repo, timeout=30)
    if done.returncode:
        raise Untrusted(f"{tag} is not signed by a pinned release key ({_last(done.stderr, 'verify-tag failed')}); {FIX}")
    return _git("rev-parse", "--verify", f"{obj}^{{commit}}", cwd=repo).stdout.strip()


def _clone_tag() -> str | None:
    """The release tag an install clone sits on, or None for anything that is not one."""
    if not (ROOT / ".git").is_dir():  # snapshot, plugin copy, or a `git worktree` (.git is a file)
        return None
    if _git("symbolic-ref", "-q", "HEAD").returncode == 0:  # on a branch: someone's working tree
        return None
    if _git("diff", "--quiet", "HEAD").returncode != 0:
        return None
    tags = [t for t in _git("tag", "--points-at", "HEAD").stdout.split() if TAG.fullmatch(t)]
    return max(tags, key=_key) if tags else None


def _installed_tag() -> str | None:
    if os.environ.get("RP_KIT_AUTOUPDATE") == "0" or os.environ.get("RC_BRAIN_KIT_TAG") or os.environ.get("CI"):
        return None
    return _clone_tag()


def _latest_remote_tag() -> str:
    out = _git("ls-remote", "--tags", "--refs", "origin", "v*", timeout=10)
    if out.returncode:
        raise RuntimeError(_last(out.stderr, "git ls-remote failed"))
    tags = [t for t in (line.rsplit("/", 1)[-1] for line in out.stdout.splitlines()) if TAG.fullmatch(t)]
    if not tags:
        raise RuntimeError("origin lists no release tags")
    return max(tags, key=_key)


@contextlib.contextmanager
def _lock(wait: float = 0.0):
    """Exclusive lock in the clone's own git dir (independent of RP_KIT_CACHE); yields False when another
    updater still holds it after `wait` seconds."""
    import fcntl

    with open(ROOT / ".git" / "replypen-kit.lock", "a") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.5)
        yield True


# --- stamps (CACHE, per clone) -------------------------------------------------------------------
def _stamp(kind: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"{kind}-{hashlib.sha1(str(ROOT).encode()).hexdigest()[:12]}"


def _age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _due() -> bool:
    stamp = _stamp("tag-check")
    age = _age(stamp)
    if age is None:
        return True
    with contextlib.suppress(OSError):
        return age >= (CHECK_EVERY if stamp.read_text().strip() == "ok" else RETRY_EVERY)
    return True


def _record(status: str) -> None:
    ok = status in ("unchanged", "updated")
    _stamp("tag-check").write_text(("ok" if ok else status) + "\n")
    since = _stamp("deferred-since")  # first deferral since the last good check (also on a fresh install)
    if ok:
        since.unlink(missing_ok=True)
    elif not since.exists():
        since.write_text(time.strftime("%F %T") + "\n")


# --- brains --------------------------------------------------------------------------------------
def _git_dir(brain: Path) -> Path | None:
    dot = brain / ".git"
    if dot.is_dir():
        return dot
    with contextlib.suppress(OSError, ValueError):
        text = dot.read_text().strip()
        if text.startswith("gitdir:"):
            return (brain / text.split(":", 1)[1].strip()).resolve()
    return None


def _busy(brain: Path) -> str | None:
    """An in-progress git operation in this brain (commit, rebase, merge…): an agent is working there."""
    gd = _git_dir(brain)
    return next((f"git {name}" for name in BUSY if gd and (gd / name).exists()), None)


def _anchor(brain: Path) -> Path:
    return brain / ".agents" / "skills" / "local-brain-work"


def _linked(brain: Path) -> bool:
    anchor = _anchor(brain)
    try:
        return anchor.is_symlink() and anchor.resolve().parent.parent == ROOT
    except OSError:
        return False


def _is_brain(d: Path) -> bool:
    if (d / "scripts" / "kit_update.py").is_file():  # a kit checkout, not a brain
        return False
    markers = (d / "skills").is_dir() or (d / "playbooks").is_dir() or any(
        (d / f).is_file() for f in ("projection.yaml", ".replypen.toml", ".rootcause.toml"))
    return markers and (d / ".git").is_dir()  # checkouts only; agent worktrees relink when used


def _registry() -> Path:
    return ROOT / ".git" / "replypen-kit-brains"


def _remember(brain: Path) -> None:
    with contextlib.suppress(OSError):
        known = _registry().read_text().splitlines() if _registry().exists() else []
        if str(brain) not in known:
            with open(_registry(), "a") as f:
                f.write(f"{brain}\n")


def _brains() -> list[Path]:
    """Brains install.sh/usage recorded, plus brain checkouts under the usual code roots (≤3 deep)."""
    found: dict[Path, None] = {}
    with contextlib.suppress(OSError):
        for line in _registry().read_text().splitlines():
            found[Path(line)] = None
    home = Path.home()
    for root in BRAIN_ROOTS:
        base = home / root
        if not base.is_dir():
            continue
        for depth in range(3):
            for name in BRAIN_NAMES:
                with contextlib.suppress(OSError):
                    for d in base.glob("*/" * depth + name):
                        found[d] = None
    out: dict[Path, None] = {}
    for d in found:
        with contextlib.suppress(OSError):
            d = d.resolve()
            if d != ROOT and d.is_dir() and _is_brain(d):
                out[d] = None
    return sorted(out)


_HOOK: dict[str, str | None] = {}


def _expected_hook(name: str) -> str | None:
    """What this kit's install.sh writes as hook `name`; None when it writes no such hook (an older
    install.sh prints its pre-commit body for any name: the mark tells them apart)."""
    if name not in _HOOK:
        done = subprocess.run(["bash", str(ROOT / "install.sh"), "--print-hook", name], capture_output=True,
                              text=True, timeout=30, env=_env(RC_BRAIN_KIT=str(ROOT)))
        ok = done.returncode == 0 and f"managed {name} hook" in done.stdout
        _HOOK[name] = done.stdout if ok else None
    return _HOOK[name]


def _kit_link(path: Path) -> bool:
    try:
        target = os.readlink(path)
    except OSError:
        return False
    kits = {str(ROOT), str(Path.home() / ".replypen-brain-skills"), str(Path.home() / ".rootcause-brain-skills")}
    return any(target.startswith(f"{k}/skills/") for k in kits)


def _relink_reasons(brain: Path) -> list[str]:
    """Why a linked brain needs this kit's install.sh again: missing or dangling skill links, the docs
    link, or a managed git hook that is missing or differs from what install.sh writes today."""
    skills = brain / ".agents" / "skills"
    reasons = []
    shipped = sorted(d.name for d in (ROOT / "skills").iterdir() if (d / "SKILL.md").is_file())
    if missing := [n for n in shipped if not (skills / n).is_symlink()]:
        reasons.append("missing skill links: " + ", ".join(missing))
    dirs = [skills] + ([brain / ".claude" / "skills"] if (brain / ".claude" / "skills").is_dir()
                       and not (brain / ".claude" / "skills").is_symlink() else [])
    stale = sorted({p.name for d in dirs for p in d.iterdir() if _kit_link(p) and not p.exists()})
    if stale:
        reasons.append("stale skill links: " + ", ".join(stale))
    if (ROOT / "docs").is_dir():
        docs = brain / ".agents" / "docs"
        alias = brain / ".claude" / "docs"
        try:
            ok = docs.is_symlink() and docs.resolve() == (ROOT / "docs").resolve() and alias.is_symlink() \
                and alias.resolve() == docs.resolve()
        except OSError:
            ok = False
        if not ok:
            reasons.append("docs links missing or stale")
    gd = _git_dir(brain)
    common = gd
    with contextlib.suppress(OSError):
        if gd and (gd / "commondir").is_file():
            common = (gd / (gd / "commondir").read_text().strip()).resolve()
    if common is None or (common / NO_HOOKS).exists():
        return reasons
    hooks_path = _git("config", "core.hooksPath", cwd=brain).stdout.strip()
    for name in HOOKS:
        if not (expected := _expected_hook(name)):
            continue
        hook = common / "hooks" / name
        try:
            text = hook.read_text() if hook.is_file() else None
        except (OSError, UnicodeDecodeError):
            continue
        if text is None and not hooks_path and not os.path.lexists(hook):  # install.sh leaves a user setup be
            reasons.append(f"missing {name} hook")
        elif text and f"-brain-skills managed {name} hook" in text and text != expected:
            reasons.append(f"outdated {name} hook")
    return reasons


def _install(brain: Path) -> str | None:
    """Run this kit's install.sh on a brain; returns the error, if any."""
    try:
        done = subprocess.run(["bash", str(ROOT / "install.sh"), str(brain)], capture_output=True, text=True,
                              timeout=120, env=_env(RC_BRAIN_KIT=str(ROOT), RP_KIT_SCHEDULE="0"))
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return _last(done.stderr, f"install.sh exit {done.returncode}") if done.returncode else None


# --- tag move ------------------------------------------------------------------------------------
def _move(installed: str, skip: Path | None = None) -> tuple[str, str]:
    """(status, detail): unchanged/updated (detail = tag) or deferred/failed (detail = reason)."""
    try:
        latest = _latest_remote_tag()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return "deferred", f"could not list release tags ({exc}); offline?"
    if _key(latest) <= _key(installed):
        return "unchanged", installed
    try:  # fetch first (refs/objects only): the busy check must sit right before the swap
        if _git("fetch", "-q", "--tags", "origin", timeout=120).returncode:
            return "deferred", f"could not fetch {latest}; offline?"
    except (OSError, subprocess.SubprocessError) as exc:
        return "deferred", f"could not fetch {latest} ({exc}); offline?"
    try:  # the newest tag or nothing: an older signed tag is never picked instead (fail closed)
        commit = verify_tag(latest)
    except Untrusted as exc:
        return "failed", f"{latest} not installed, kept {installed}: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:
        return "deferred", f"could not verify {latest} ({exc})"
    # Not a lock on the brains' own git operations (holding their index.lock would block commits if we
    # died): a commit starting in the sub-second checkout below still races; the quiet hour covers it.
    for brain in _brains():
        if brain != skip and _linked(brain) and (why := _busy(brain)):
            return "deferred", f"{latest} waits: {why} in progress in {brain}"
    try:
        done = _git("checkout", "-q", commit, timeout=60)  # the verified commit, not the mutable ref
        if done.returncode or _git("rev-parse", "HEAD").stdout.strip() != commit:
            _git("checkout", "-q", "-f", installed, timeout=60)  # tree was clean: restore the working tag
            return "failed", f"checkout {latest} failed, kept {installed}: {_last(done.stderr, 'unknown error')}"
    except (OSError, subprocess.SubprocessError) as exc:
        _git("checkout", "-q", "-f", installed, timeout=60)
        return "failed", f"checkout {latest} failed, kept {installed}: {exc}"
    return "updated", latest


# --- the daily job -------------------------------------------------------------------------------
def _bootstrap() -> str:
    """The job's pre-command (t3-schedule runs it before T3 starts): `bootstrap`."""
    return f"python3 {shlex.quote(str(ROOT / 'scripts' / 'kit_update.py'))} --bootstrap"


def bootstrap() -> int:
    """Run the NEWEST VERIFIED TAG's updater with `--apply` (a release may fix this one's apply pass), else
    this one. The installed file verifies: a fetched updater runs only after this pin accepted its tag.
    (Until v0.14.35 this was a shell line that ran the newest tag's updater unverified.)"""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _git("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30", "fetch", "-q", "--tags", "origin",
             timeout=180)
    installed = _clone_tag()
    newest = max((t for t in _git("tag", "-l", "v*").stdout.split() if TAG.fullmatch(t)), key=_key, default=None)
    job = ROOT / ".git" / "replypen-kit-job.py"
    try:
        floor = max(_key(MIN_VERIFYING_RELEASE), _key(installed or ""))
        if not newest or _key(newest) < floor:  # downgrade guard: never an older, non-verifying updater
            raise Untrusted(f"newest local tag {newest} is older than {installed or MIN_VERIFYING_RELEASE}")
        commit = verify_tag(newest)
        code = _git("show", f"{commit}:scripts/kit_update.py")
        if code.returncode or not code.stdout:
            raise Untrusted(f"{newest} ships no scripts/kit_update.py")
        job.write_text(code.stdout)
    except (Untrusted, OSError, subprocess.SubprocessError) as exc:
        print(f"kit: bootstrap: running the installed updater ({exc})", file=sys.stderr)
        return apply()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, str(job), "--apply"], _env(RP_KIT_ROOT=str(ROOT)))


def _job_prompt() -> str:
    cmd = f"python3 {shlex.quote(str(ROOT / 'scripts' / 'kit_update.py'))} --job"
    return (f"Run exactly one command: `{cmd}`. Then end the turn with its complete output as your answer, "
            "nothing else.\nIts last line decides this thread's lifecycle: `thread: SETTLE` → call the T3 MCP "
            'tool `t3_thread_organize` with {"action":"settle"} as your last tool call; anything else → just '
            "answer. Never retry, never fix anything yourself.\n")


def _opt_out() -> Path:
    return ROOT / ".git" / "replypen-kit-no-schedule"


def _ensure_schedule(verbose: bool = False, opt_in: bool = False) -> str | None:
    """Arm (or refresh) this machine's daily job. Idempotent: an armed, current job costs two file reads.
    Respects opt-outs: the marker, RP_KIT_SCHEDULE=0, and a job the user removed (its definition stays
    in the clone after `t3-schedule remove`)."""
    if opt_in:
        _opt_out().unlink(missing_ok=True)
    if os.environ.get("RP_KIT_SCHEDULE") == "0" or os.environ.get("CI"):
        return None
    say = (lambda msg: f"daily kit refresh job: {msg}") if verbose else (lambda msg: None)
    if _opt_out().exists():
        return say("opted out (re-enable: install.sh --schedule)")
    if not _clone_tag():
        return say(f"skipped ({ROOT} is not an install clone at a release tag)")
    exe = shutil.which("t3-schedule")
    if not exe:
        return say("skipped: t3-schedule not on PATH (the usage-triggered update still runs). Fix: run "
                   "~/.agents/skills/t3-schedule/scripts/install if it exists (links into ~/.local/bin; put that "
                   "on PATH), else `pnpm dlx skills add pjmuller/skills -s t3-schedule -g -y`, then its "
                   "scripts/install and `scripts/install --check` (doc: https://github.com/pjmuller/skills/blob/"
                   "main/skills/t3-schedule/SKILL.md; WSL: linux-wsl.md next to it); then rerun install.sh")
    pointer = Path.home() / ".t3" / "userdata" / "scheduled" / "t3-schedule" / f"{JOB}.json"
    definition = ROOT / ".agents" / "schedules" / f"{JOB}.json"
    at = JOB_AT
    if pointer.exists():
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
            src = Path(json.loads(pointer.read_text())["source"])
            spec = json.loads(src.read_text())
            at = spec.get("at") or JOB_AT  # keep a `t3-schedule set-at` choice
            if (spec.get("pre_command") == _bootstrap()
                    and src.with_name(f"{JOB}.prompt.md").read_text().strip() == _job_prompt().strip()):
                return say(f"armed ({JOB}, {at})")
    elif definition.exists() and not opt_in:
        return say("not armed (removed with t3-schedule remove; re-enable: install.sh --schedule)")
    fail = _stamp("schedule-fail")
    if not verbose and (age := _age(fail)) is not None and age < CHECK_EVERY:
        return None
    prompt = CACHE / "brain-kit-refresh.prompt.md"
    prompt.write_text(_job_prompt())
    cmd = [exe, "add", "--name", JOB, "--at", at, "--project", str(ROOT), "--model", "haiku,luna",
           "--allow-small-model", "--pre-command", _bootstrap(), "--no-notify", "--title", f"⏰ {JOB} {{date}}",
           "--force", "--prompt-file", str(prompt)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        err = _last(done.stderr or done.stdout, f"exit {done.returncode}") if done.returncode else None
    except (OSError, subprocess.SubprocessError) as exc:
        err = str(exc)
    if err:
        fail.write_text(err + "\n")
        return f"kit: ⚠ could not arm the daily kit refresh job ({err}); retried tomorrow"
    fail.unlink(missing_ok=True)
    return f"kit: armed the daily kit refresh job ({JOB}, {at} local; opt out: install.sh --no-schedule)"


def _disarm() -> str:
    _opt_out().write_text(time.strftime("%F") + "\n")
    pointer = Path.home() / ".t3" / "userdata" / "scheduled" / "t3-schedule" / f"{JOB}.json"
    exe = shutil.which("t3-schedule")
    if pointer.exists() and exe:
        subprocess.run([exe, "remove", JOB], capture_output=True, text=True, timeout=60)
    return "daily kit refresh job: opted out (re-enable: install.sh --schedule)"


def apply(job: bool = False) -> int:
    """One full pass for the timer: tag move, relink every linked brain, install into unlinked brain
    checkouts, keep the job armed. Prints a report; exit 0 = unchanged/updated/deferred, 1 = failed."""
    lines: list[str] = []
    failed = False
    env_pin = next((k for k in ("RP_KIT_AUTOUPDATE", "RC_BRAIN_KIT_TAG", "CI") if os.environ.get(k)
                    and not (k == "RP_KIT_AUTOUPDATE" and os.environ[k] != "0")), None)
    installed = None if env_pin else _clone_tag()
    if env_pin:
        status, detail = "skipped", f"updates disabled by {env_pin} in this environment"
    elif not installed:
        status, detail = "failed", f"{ROOT} is not an install clone at a release tag (on a branch, dirty or untagged)"
    else:
        with _lock(LOCK_WAIT) as held:
            installed = _clone_tag()  # recheck inside the lock: another updater may have moved it
            if not held:
                status, detail = "deferred", "another kit update held the lock"
                _record(status)  # a hung updater holding the lock must still escalate after days
            elif not installed:
                status, detail = "failed", f"{ROOT} left its release tag while waiting for the lock"
            else:
                status, detail = _move(installed)
                _record(status)
                brains = _brains()
                for brain in brains:
                    linked, unlinked = _linked(brain), not os.path.lexists(_anchor(brain))
                    if not (linked or unlinked):
                        continue  # linked to another kit (a developer checkout) or user content
                    if linked:
                        _remember(brain)
                    reasons = _relink_reasons(brain) if linked else ["not installed yet"]
                    if not reasons:
                        continue
                    if why := _busy(brain):
                        lines.append(f"kit: {brain.name}: relink waits ({why} in progress)")
                    elif err := _install(brain):
                        failed = True
                        lines.append(f"kit: ⚠ {brain}: install.sh failed: {err}\n     reproduce: "
                                     f"RC_BRAIN_KIT={shlex.quote(str(ROOT))} bash "
                                     f"{shlex.quote(str(ROOT / 'install.sh'))} {shlex.quote(str(brain))}")
                    else:
                        lines.append(f"kit: {'relinked' if linked else 'installed the kit into'} {brain.name} "
                                     f"({'; '.join(reasons)})")
                lines.append(f"kit: {sum(map(_linked, brains))} brain checkout(s) linked to this kit")
                if msg := _ensure_schedule():
                    lines.append(msg)
    if status == "deferred" and (age := _age(_stamp("deferred-since"))) is not None and age > STALE_AFTER:
        status, detail = "failed", f"no successful check for {int(age // 86400)} days: {detail}"
    head = {"updated": f"kit: updated {installed} → {detail} ({ROOT}); new agent sessions load the new skills",
            "unchanged": f"kit: {detail} is the newest release ({ROOT})",
            "deferred": f"kit: deferred, retried next run: {detail}",
            "skipped": f"kit: skipped: {detail}",
            "failed": f"kit: FAILED: {detail}"}[status]
    print("\n".join([head, *lines]))
    failed = failed or status == "failed"
    if failed:
        state = _git("status", "--short", "--branch").stdout.strip().replace("\n", " · ")
        print(f"diagnostics: {ROOT} at {_git('describe', '--tags', '--always').stdout.strip()} · {state[:400]}")
        print(f"rerun: python3 {shlex.quote(str(ROOT / 'scripts' / 'kit_update.py'))} --apply")
    print(f"status: {'failed' if failed else status}")
    if job:
        print(KEEP_OPEN if failed else SETTLE)
    return 1 if failed else 0


def maybe_update(brain: Path | str | None = None, *, reexec: bool = True) -> None:
    """Usage fallback, best effort, never raises: an update problem must not block the caller's real work.
    After a tag move the calling process restarts (`reexec`): modules it already imported, and defaults it
    derived from them (the workspace image tag), belong to the previous release."""
    try:
        if not _installed_tag():
            return
        here = Path(brain).resolve() if brain else None
        new = relinked = None
        with _lock() as held:
            installed = _installed_tag()  # recheck inside the lock
            if not held or not installed:
                return
            if _due():
                status, detail = _move(installed, skip=here)  # the caller's own commit is not "busy"
                _record(status)
                if status == "updated":
                    new = detail
                elif status == "failed" or detail.endswith("offline?"):
                    print(f"kit: ⚠ could not check for a newer kit release ({detail}); "
                          f"keeping {installed} (retry in an hour)", file=sys.stderr)
            if here and _linked(here):
                _remember(here)
                if not _busy(here) and _relink_reasons(here):  # never rewrite links/hook mid-commit
                    err = _install(here)
                    relinked = not err
                    if err:
                        print(f"kit: ⚠ could not relink {here.name} to the kit ({err})", file=sys.stderr)
            armed = _ensure_schedule()
        if armed:
            print(armed, file=sys.stderr)
        if new:
            print(f"kit: updated {installed} → {new} ({ROOT}); start a fresh agent session to reload "
                  "changed skill text", file=sys.stderr)
            if reexec:
                argv = getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv]
                sys.stdout.flush()
                sys.stderr.flush()
                os.execve(sys.executable, [sys.executable, *argv[1:]], {**os.environ, "RP_KIT_AUTOUPDATE": "0"})
        elif relinked:
            print(f"kit: relinked {here.name} to the kit", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — see docstring
        print(f"kit: ⚠ update check skipped ({exc})", file=sys.stderr)


USAGE = """usage: kit_update.py [BRAIN]             usage fallback (what the hook and brain scripts call)
       kit_update.py --apply              timer pass: tag move, relink/install brains; exit 1 = failed
       kit_update.py --job                same, then the T3 job thread's `thread:` line
       kit_update.py --bootstrap          the job's pre-command: --apply with the newest verified tag's updater
       kit_update.py --verify-tag TAG [REPO]  check TAG against this file's signing pin; print its commit
       kit_update.py --schedule [--opt-in] arm/refresh the daily job (respects opt-outs unless --opt-in)
       kit_update.py --no-schedule        opt out: disarm the daily job and stay disarmed"""

if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--apply"]:
        sys.exit(apply())
    if args[:1] == ["--bootstrap"]:
        sys.exit(bootstrap())
    if args[:1] == ["--verify-tag"] and len(args) in (2, 3):
        try:
            print(verify_tag(args[1], Path(args[2]).resolve() if len(args) == 3 else ROOT))
        except Untrusted as exc:
            sys.exit(f"kit: {exc}")
        sys.exit(0)
    if args[:1] == ["--job"]:
        sys.exit(apply(job=True))
    if args[:1] == ["--schedule"]:
        with _lock() as held:
            print(_ensure_schedule(verbose=True, opt_in="--opt-in" in args) if held
                  else "daily kit refresh job: another kit update is running; the updater arms it later")
        sys.exit(0)
    if args[:1] == ["--no-schedule"]:
        print(_disarm())
        sys.exit(0)
    if args[:1] in (["-h"], ["--help"]):
        print(USAGE)
        sys.exit(0)
    maybe_update(args[0] if args else None, reexec=False)
