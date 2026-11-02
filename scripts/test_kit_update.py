"""kit_update: an installed clone moves to the newest release tag (daily job + usage fallback); dev checkouts,
offline, busy brains and failed checkouts/installs leave a usable kit; the daily job arms itself once."""

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KIT_UPDATE = REPO / "scripts" / "kit_update.py"
INSTALL = REPO / "install.sh"

# Records every call; `add` writes the pointer + repo definition the way t3-schedule does.
FAKE_SCHEDULE = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["SCHEDULE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
base = Path.home() / ".t3/userdata/scheduled/t3-schedule"
if args[0] == "add":
    opt = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
    src = Path(opt["--project"]) / ".agents/schedules" / (opt["--name"] + ".json")
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps({"at": opt["--at"], "pre_command": opt["--pre-command"]}))
    src.with_name(opt["--name"] + ".prompt.md").write_text(Path(opt["--prompt-file"]).read_text())
    base.mkdir(parents=True, exist_ok=True)
    (base / (opt["--name"] + ".json")).write_text(json.dumps({"name": opt["--name"], "source": str(src)}))
elif args[0] == "remove":
    (base / (args[1] + ".json")).unlink()
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def skill(repo, name):
    (repo / "skills" / name).mkdir(parents=True, exist_ok=True)
    (repo / "skills" / name / "SKILL.md").write_text(f"# {name}\n")


def release(dev, tag):
    git(dev, "add", ".")
    git(dev, "commit", "-qm", tag)
    git(dev, "tag", tag)
    git(dev, "push", "-q", "origin", "main", "--tags")


def make_brain(path, clone=None):
    """A brain checkout; with `clone`, linked to it by the real install.sh."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / ".replypen.toml").write_text("")
    if clone:
        done = subprocess.run(["bash", str(clone / "install.sh"), str(path)], capture_output=True, text=True,
                              env={**os.environ, "RC_BRAIN_KIT": str(clone), "RP_KIT_SCHEDULE": "0"})
        assert done.returncode == 0, done.stderr
    return path


def setup(tmp_path):
    """origin with v0.1.9 and v0.1.10 (adds brain-new); an install clone at v0.1.9; one linked brain."""
    origin, dev, clone = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "home" / ".replypen-brain-skills"
    (tmp_path / "home").mkdir()
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(dev, "config", k, v)
    (dev / "scripts").mkdir()
    shutil.copy(KIT_UPDATE, dev / "scripts" / "kit_update.py")
    shutil.copy(INSTALL, dev / "install.sh")
    (dev / ".gitignore").write_text("/.agents/schedules/\n")
    skill(dev, "local-brain-work")
    release(dev, "v0.1.9")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "v0.1.9")
    skill(dev, "brain-new")
    release(dev, "v0.1.10")  # numeric, not lexical: v0.1.10 > v0.1.9
    brain = make_brain(tmp_path / "home" / "code" / "acme" / "replypen-brain-acme", clone)
    return dev, clone, brain


def env(tmp_path, **extra):
    e = {k: v for k, v in os.environ.items() if k not in ("CI", "RC_BRAIN_KIT_TAG", "RP_KIT_AUTOUPDATE", "RP_KIT_ROOT")}
    e.update(HOME=str(tmp_path / "home"), RP_KIT_CACHE=str(tmp_path / "cache"), RP_KIT_SCHEDULE="0",
             RP_KIT_LOCK_WAIT="1", SCHEDULE_LOG=str(tmp_path / "schedule.log"))
    return {**e, **extra}


def run(kit, tmp_path, *args, code=0, **extra):
    done = subprocess.run([sys.executable, str(kit / "scripts" / "kit_update.py"), *map(str, args)],
                          capture_output=True, text=True, env=env(tmp_path, **extra))
    assert done.returncode == code, done.stdout + done.stderr
    return done.stdout + done.stderr


def tag(clone):
    return git(clone, "describe", "--tags", "--exact-match")


def with_fake_schedule(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "t3-schedule").write_text(FAKE_SCHEDULE)
    (bin_dir / "t3-schedule").chmod(0o755)
    return {"RP_KIT_SCHEDULE": "", "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def schedule_calls(tmp_path):
    log = tmp_path / "schedule.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# --- usage fallback ------------------------------------------------------------------------------
def test_installed_clone_moves_to_newest_tag_and_relinks_the_brain(tmp_path):
    _, clone, brain = setup(tmp_path)
    err = run(clone, tmp_path, brain)
    assert "updated v0.1.9 → v0.1.10" in err
    assert tag(clone) == "v0.1.10"
    assert (brain / ".agents" / "skills" / "brain-new").is_symlink()
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert run(clone, tmp_path, brain) == ""  # checked today: no network, nothing to relink


def test_dev_checkout_pin_and_opt_out_are_untouched(tmp_path):
    dev, clone, _ = setup(tmp_path)
    git(dev, "reset", "-q", "--hard", "v0.1.9")  # on a branch: someone's working tree
    assert run(dev, tmp_path) == ""
    assert git(dev, "rev-parse", "HEAD") == git(dev, "rev-parse", "v0.1.9")
    for extra in ({"RP_KIT_AUTOUPDATE": "0"}, {"RC_BRAIN_KIT_TAG": "v0.1.9"}, {"CI": "true"}):
        assert run(clone, tmp_path, **extra) == ""
    (clone / "install.sh").write_text("# local edit\n")  # dirty tracked file
    assert run(clone, tmp_path) == ""
    assert tag(clone) == "v0.1.9"


def test_offline_warns_once_and_keeps_the_installed_tag(tmp_path):
    _, clone, _ = setup(tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert "could not check for a newer kit release" in run(clone, tmp_path)
    assert run(clone, tmp_path) == ""  # retried after an hour, not on every commit
    assert tag(clone) == "v0.1.9"


def test_commit_in_progress_defers_the_swap_and_its_own_relink(tmp_path):
    _, clone, brain = setup(tmp_path)
    other = make_brain(tmp_path / "home" / "code" / "acme" / "replypen-brain-other", clone)
    (other / ".git" / "index.lock").write_text("")  # another agent mid-commit
    run(clone, tmp_path, brain)
    assert tag(clone) == "v0.1.9"
    (other / ".git" / "index.lock").unlink()
    (brain / ".git" / "index.lock").write_text("")  # the caller's own commit (pre-commit hook)
    (tmp_path / "cache").joinpath(next(p.name for p in (tmp_path / "cache").iterdir()
                                       if p.name.startswith("tag-check"))).unlink()  # due again
    assert "updated v0.1.9 → v0.1.10" in run(clone, tmp_path, brain)  # its own commit is not "busy"
    assert not (brain / ".agents" / "skills" / "brain-new").exists()  # but no relink mid-commit
    (brain / ".git" / "index.lock").unlink()
    assert "relinked" in run(clone, tmp_path, brain)
    assert (brain / ".agents" / "skills" / "brain-new").is_symlink()


CALLER = """import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
LOADED = (ROOT / "VERSION").read_text().strip()  # an import-time default, like brain_env.DEFAULT_IMAGE
sys.path.insert(0, str(ROOT / "scripts"))
import kit_update
kit_update.maybe_update()
print("LOADED", LOADED, sys.argv[1:])
"""


def test_caller_restarts_on_the_new_release_after_a_tag_move(tmp_path):
    dev, clone, _ = setup(tmp_path)
    for version in ("old", "new"):
        (dev / "VERSION").write_text(version)
        (dev / "scripts" / "caller.py").write_text(CALLER)
        git(dev, "add", ".")
        git(dev, "commit", "-qm", version)
        git(dev, "tag", "v0.1.11" if version == "old" else "v0.1.12")
    git(dev, "push", "-q", "origin", "main", "--tags")
    git(clone, "fetch", "-q", "--tags")
    git(clone, "checkout", "-q", "v0.1.11")
    done = subprocess.run([sys.executable, str(clone / "scripts" / "caller.py"), "--x"], capture_output=True,
                          text=True, env=env(tmp_path))
    assert done.stdout == "LOADED new ['--x']\n", done.stderr
    assert "updated v0.1.11 → v0.1.12" in done.stderr


# --- the daily job -------------------------------------------------------------------------------
def test_job_updates_relinks_and_installs_into_unlinked_brains(tmp_path):
    _, clone, brain = setup(tmp_path)
    fresh = make_brain(tmp_path / "home" / "code" / "replypen-brain-fresh")  # cloned, never installed
    elsewhere = make_brain(tmp_path / "home" / "code" / "replypen-brain-dev")
    (elsewhere / ".agents" / "skills").mkdir(parents=True)
    (elsewhere / ".agents" / "skills" / "local-brain-work").symlink_to(tmp_path / "dev" / "skills" / "local-brain-work")
    out = run(clone, tmp_path, "--job")
    assert tag(clone) == "v0.1.10"
    assert "relinked replypen-brain-acme (missing skill links: brain-new)" in out
    assert "installed the kit into replypen-brain-fresh" in out
    assert (fresh / ".agents" / "skills" / "brain-new").is_symlink()
    assert os.readlink(elsewhere / ".agents" / "skills" / "local-brain-work").startswith(str(tmp_path / "dev"))
    assert out.rstrip().endswith('as your last tool call') and "thread: SETTLE" in out
    out = run(clone, tmp_path, "--job")  # second run: nothing to do
    assert "v0.1.10 is the newest release" in out and "relinked" not in out


def test_job_repairs_stale_links_and_an_outdated_hook(tmp_path):
    _, clone, brain = setup(tmp_path)
    run(clone, tmp_path, "--apply")
    hook = brain / ".git" / "hooks" / "pre-commit"
    hook.write_text(hook.read_text().replace("brain hygiene", "old hygiene"))  # an older release's hook
    (clone / "docs").mkdir()  # a release that ships docs: the brain lacks both docs links
    (brain / ".agents" / "skills" / "retired").symlink_to(clone / "skills" / "retired")  # skill gone
    out = run(clone, tmp_path, "--apply")
    assert "stale skill links: retired" in out and "outdated pre-commit hook" in out
    assert "docs links missing or stale" in out and (brain / ".claude" / "docs").resolve() == (clone / "docs").resolve()
    assert "old hygiene" not in hook.read_text()
    assert not os.path.lexists(brain / ".agents" / "skills" / "retired")


def test_job_repairs_a_missing_hook_unless_opted_out_or_user_owned(tmp_path):
    """A brain installed before pre-push existed gets it; --no-hooks sticks; a user hook or core.hooksPath
    is never flagged (install.sh leaves those alone, so flagging would relink every day)."""
    _, clone, brain = setup(tmp_path)
    run(clone, tmp_path, "--apply")
    hooks = brain / ".git" / "hooks"
    assert "managed pre-push hook" in (hooks / "pre-push").read_text()
    (hooks / "pre-push").unlink()
    out = run(clone, tmp_path, "--apply")
    assert "missing pre-push hook" in out and (hooks / "pre-push").is_file()

    (hooks / "pre-push").write_text("#!/bin/sh\necho mine\n")
    assert "pre-push" not in run(clone, tmp_path, "--apply")
    (hooks / "pre-push").unlink()
    git(brain, "config", "core.hooksPath", ".githooks")
    assert "pre-push" not in run(clone, tmp_path, "--apply")
    git(brain, "config", "--unset", "core.hooksPath")

    done = subprocess.run(["bash", str(clone / "install.sh"), "--no-hooks", str(brain)], capture_output=True,
                          text=True, env={**os.environ, "RC_BRAIN_KIT": str(clone), "RP_KIT_SCHEDULE": "0"})
    assert done.returncode == 0 and "pre-commit hook: removed" in done.stdout, done.stdout + done.stderr
    assert not (hooks / "pre-commit").exists()
    out = run(clone, tmp_path, "--apply")
    assert "hook" not in out and not (hooks / "pre-commit").exists()


def test_git_hook_env_never_reaches_the_kit_clone(tmp_path, monkeypatch):
    """A hook exports GIT_INDEX_FILE (GIT_DIR in a linked worktree): the clone's git must not use them."""
    _, clone, brain = setup(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(brain / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(brain / ".git" / "index"))
    monkeypatch.setenv("RP_KIT_ROOT", str(clone))
    sys.path.insert(0, str(clone / "scripts"))
    try:
        import kit_update
        assert kit_update._clone_tag() == "v0.1.9"
    finally:
        sys.path.remove(str(clone / "scripts"))
        sys.modules.pop("kit_update", None)


def test_failed_checkout_keeps_the_installed_tag_and_surfaces(tmp_path):
    _, clone, _ = setup(tmp_path)
    skill(clone, "brain-new")  # untracked file the new tag would overwrite: git refuses the checkout
    out = run(clone, tmp_path, "--job", code=1)
    assert tag(clone) == "v0.1.9"
    assert "FAILED: checkout v0.1.10 failed, kept v0.1.9" in out
    assert "diagnostics:" in out and out.rstrip().endswith("thread: KEEP OPEN (the user should see this)")


def test_failed_install_in_one_brain_still_relinks_the_others(tmp_path):
    _, clone, brain = setup(tmp_path)
    other = make_brain(tmp_path / "home" / "code" / "acme" / "replypen-brain-other", clone)
    (other / ".agents" / "skills" / "brain-new").mkdir()  # user content where a kit link would go
    out = run(clone, tmp_path, "--job", code=1)
    assert tag(clone) == "v0.1.10"
    assert (brain / ".agents" / "skills" / "brain-new").is_symlink()
    assert f"{other}: install.sh failed" in out and "reproduce:" in out and "thread: KEEP OPEN" in out


def test_job_waits_for_a_held_lock_and_for_a_busy_brain(tmp_path):
    _, clone, brain = setup(tmp_path)
    with open(clone / ".git" / "replypen-kit.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        out = run(clone, tmp_path, "--job")
    assert "deferred, retried next run: another kit update held the lock" in out and "thread: SETTLE" in out
    (brain / ".git" / "rebase-merge").mkdir()
    out = run(clone, tmp_path, "--job")
    assert "v0.1.10 waits: git rebase-merge in progress" in out and "thread: SETTLE" in out
    assert tag(clone) == "v0.1.9"
    since = tmp_path / "cache" / f"deferred-since-{hashlib.sha1(str(clone.resolve()).encode()).hexdigest()[:12]}"
    assert since.exists()  # tracked from the first deferral, even with no good check ever
    os.utime(since, (1, 1))  # deferred for days (a forgotten rebase) → surface it
    assert "no successful check for" in run(clone, tmp_path, "--job", code=1)


def test_bootstrap_runs_the_newest_tags_updater_when_the_installed_one_is_broken(tmp_path):
    dev, clone, _ = setup(tmp_path)
    (dev / "scripts" / "kit_update.py").write_text("this is not python\n")
    release(dev, "v0.1.11")  # a broken updater ships…
    shutil.copy(KIT_UPDATE, dev / "scripts" / "kit_update.py")
    release(dev, "v0.1.12")  # …and its fix
    git(clone, "fetch", "-q", "--tags")
    git(clone, "checkout", "-q", "v0.1.11")
    pre = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import kit_update; "
                          "print(kit_update._bootstrap())", str(REPO / "scripts")], capture_output=True, text=True,
                         env={**env(tmp_path), "RP_KIT_ROOT": str(clone)}).stdout.strip()
    done = subprocess.run(["zsh" if shutil.which("zsh") else "sh", "-c", pre], capture_output=True, text=True,
                          env=env(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert tag(clone) == "v0.1.12" and "status: updated" in done.stdout


def test_job_arms_once_and_respects_opt_outs(tmp_path):
    _, clone, brain = setup(tmp_path)
    fake = with_fake_schedule(tmp_path)
    run(clone, tmp_path, brain, **fake)  # usage path after a move (colleague on the old updater)
    adds = [c for c in schedule_calls(tmp_path) if c[0] == "add"]
    assert len(adds) == 1 and adds[0][adds[0].index("--at") + 1] == "06:45"
    pre = adds[0][adds[0].index("--pre-command") + 1]
    assert str(clone) in pre and "--apply" in pre
    run(clone, tmp_path, brain, **fake)
    run(clone, tmp_path, "--apply", **fake)
    assert len(schedule_calls(tmp_path)) == 1  # idempotent
    run(clone, tmp_path, "--no-schedule", **fake)
    assert schedule_calls(tmp_path)[-1] == ["remove", "brain-kit-refresh"]
    out = subprocess.run(["bash", str(clone / "install.sh"), str(brain)], capture_output=True, text=True,
                         env=env(tmp_path, RC_BRAIN_KIT=str(clone), **fake)).stdout
    assert "opted out" in out and len(schedule_calls(tmp_path)) == 2
    out = subprocess.run(["bash", str(clone / "install.sh"), "--schedule", str(brain)], capture_output=True,
                         text=True, env=env(tmp_path, RC_BRAIN_KIT=str(clone), **fake)).stdout
    assert "armed the daily kit refresh job" in out and schedule_calls(tmp_path)[-1][0] == "add"
