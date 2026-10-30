"""kit_update: an installed clone moves to the newest release tag once a day; dev checkouts and offline don't break."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

KIT_UPDATE = Path(__file__).with_name("kit_update.py")
INSTALL = """#!/usr/bin/env bash
for s in "$RC_BRAIN_KIT"/skills/*; do ln -sfn "$s" "$1/.agents/skills/$(basename "$s")"; done
echo "$RC_BRAIN_KIT" >"$1/installed-by"
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def skill(repo, name):
    (repo / "skills" / name).mkdir(parents=True, exist_ok=True)
    (repo / "skills" / name / "SKILL.md").write_text(f"# {name}\n")


def setup(tmp_path):
    origin, dev, clone = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(dev, "config", k, v)
    (dev / "scripts").mkdir()
    shutil.copy(KIT_UPDATE, dev / "scripts" / "kit_update.py")
    (dev / "install.sh").write_text(INSTALL)
    skill(dev, "local-brain-work")
    git(dev, "add", ".")
    git(dev, "commit", "-qm", "v0.1.9")
    git(dev, "tag", "v0.1.9")
    git(dev, "push", "-q", "origin", "main", "--tags")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "v0.1.9")
    skill(dev, "brain-new")
    git(dev, "add", ".")
    git(dev, "commit", "-qm", "v0.1.10")
    git(dev, "tag", "v0.1.10")  # numeric, not lexical: v0.1.10 > v0.1.9
    git(dev, "push", "-q", "origin", "main", "--tags")
    brain = tmp_path / "brain"
    (brain / ".agents" / "skills").mkdir(parents=True)
    (brain / ".agents" / "skills" / "local-brain-work").symlink_to(clone / "skills" / "local-brain-work")
    return dev, clone, brain


def run(kit, tmp_path, brain=None, **env):
    e = {**os.environ, "RP_KIT_CACHE": str(tmp_path / "cache"), **env}
    for k in ("CI", "RC_BRAIN_KIT_TAG", "RP_KIT_AUTOUPDATE"):
        if k not in env:
            e.pop(k, None)
    args = [sys.executable, str(kit / "scripts" / "kit_update.py"), *([str(brain)] if brain else [])]
    done = subprocess.run(args, capture_output=True, text=True, env=e)
    assert done.returncode == 0, done.stderr
    return done.stderr


def test_installed_clone_moves_to_newest_tag_and_relinks_the_brain(tmp_path):
    _, clone, brain = setup(tmp_path)
    err = run(clone, tmp_path, brain)
    assert "updated v0.1.9 → v0.1.10" in err
    assert git(clone, "describe", "--tags", "--exact-match") == "v0.1.10"
    assert (brain / ".agents" / "skills" / "brain-new").is_symlink()
    assert (brain / "installed-by").read_text().strip() == str(clone)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert run(clone, tmp_path, brain) == ""  # checked today: no network, nothing to relink


def test_dev_checkout_pin_and_opt_out_are_untouched(tmp_path):
    dev, clone, _ = setup(tmp_path)
    git(dev, "reset", "-q", "--hard", "v0.1.9")  # on a branch: someone's working tree
    assert run(dev, tmp_path) == ""
    assert git(dev, "rev-parse", "HEAD") == git(dev, "rev-parse", "v0.1.9")
    for env in ({"RP_KIT_AUTOUPDATE": "0"}, {"RC_BRAIN_KIT_TAG": "v0.1.9"}, {"CI": "true"}):
        assert run(clone, tmp_path, **env) == ""
    (clone / "install.sh").write_text("# local edit\n")  # dirty tracked file
    assert run(clone, tmp_path) == ""
    assert git(clone, "describe", "--tags", "--exact-match") == "v0.1.9"


def test_offline_warns_once_and_keeps_the_installed_tag(tmp_path):
    _, clone, _ = setup(tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert "could not check for a newer kit release" in run(clone, tmp_path)
    assert run(clone, tmp_path) == ""  # retried after an hour, not on every commit
    assert git(clone, "describe", "--tags", "--exact-match") == "v0.1.9"


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
    e = {k: v for k, v in os.environ.items() if k not in ("CI", "RC_BRAIN_KIT_TAG", "RP_KIT_AUTOUPDATE")}
    done = subprocess.run([sys.executable, str(clone / "scripts" / "caller.py"), "--x"], capture_output=True,
                          text=True, env={**e, "RP_KIT_CACHE": str(tmp_path / "cache")})
    assert done.stdout == "LOADED new ['--x']\n", done.stderr
    assert "updated v0.1.11 → v0.1.12" in done.stderr
