"""kit_fresh: an installed clone behind the newest SIGNED release re-execs from its snapshot; floating main,
unsigned tags and dev checkouts never replace the installed copy."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_kit_update import enroll, sign_tags, signer

KIT_FRESH = Path(__file__).with_name("kit_fresh.py")
KIT_UPDATE = Path(__file__).with_name("kit_update.py")
SCRIPT = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import kit_fresh
kit_fresh.ensure(__file__)
print("VERSION", "{v}", sys.argv[1:])
'''


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def write(repo, version, header=""):
    s = repo / "skills" / "demo" / "scripts"
    s.mkdir(parents=True, exist_ok=True)
    (s / "demo.py").write_text(header + SCRIPT.replace("{v}", version))


def publish(dev, message, tag=None, *flags):
    git(dev, "commit", "-qam", message)
    if tag:
        git(dev, *flags, "tag", "-m", tag, tag)
    git(dev, "push", "-q", "origin", "main", "--tags")


def setup(tmp_path):
    """Installed clone at signed v0.0.1 ("old"); origin has signed v0.0.2 ("new")."""
    origin, dev, clone = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(dev, "config", k, v)
    key, line = signer(tmp_path)
    sign_tags(dev, key)
    write(dev, "old")
    (dev / "scripts").mkdir()
    shutil.copy(KIT_FRESH, dev / "scripts" / "kit_fresh.py")
    shutil.copy(KIT_UPDATE, dev / "scripts" / "kit_update.py")
    enroll(dev / "scripts" / "kit_update.py", line)
    git(dev, "add", ".")
    publish(dev, "v0.0.1", "v0.0.1")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "v0.0.1")
    write(dev, "new")
    publish(dev, "fix", "v0.0.2")
    return dev, clone


def run(repo, tmp_path, **env):
    e = {**os.environ, "RP_KIT_CACHE": str(tmp_path / "cache"), **env}
    e.pop("RP_KIT_FRESH", None) if "RP_KIT_FRESH" not in env else None
    done = subprocess.run([sys.executable, str(repo / "skills/demo/scripts/demo.py"), "--x"],
                          capture_output=True, text=True, env=e, cwd=tmp_path)
    return done.stdout, done.stderr


def test_stale_installed_clone_runs_the_newest_signed_release(tmp_path):
    _, clone = setup(tmp_path)
    out, err = run(clone, tmp_path)
    assert "VERSION new ['--x']" in out
    assert "runs from release v0.0.2" in err
    out, _ = run(clone, tmp_path)  # cached snapshot, no refetch
    assert "VERSION new" in out


def test_floating_main_and_unsigned_tags_never_run(tmp_path):
    dev, clone = setup(tmp_path)
    git(clone, "fetch", "-q", "--tags")
    git(clone, "checkout", "-q", "v0.0.2")
    write(dev, "main-only")
    publish(dev, "unreleased")  # on main, no tag: not a release
    os.utime(clone / ".git" / "FETCH_HEAD", (1, 1))  # due for a fetch
    assert "VERSION new" in run(clone, tmp_path)[0]
    write(dev, "unsigned")
    publish(dev, "pushed with tag rights only", "v0.0.3", "-c", "tag.gpgSign=false")
    os.utime(clone / ".git" / "FETCH_HEAD", (1, 1))
    out, err = run(clone, tmp_path)
    assert "VERSION new" in out and "v0.0.3 is not an SSH-signed annotated tag" in err


def test_opt_out_and_dev_checkout_run_their_own_tree(tmp_path):
    dev, clone = setup(tmp_path)
    assert "VERSION old" in run(clone, tmp_path, RP_KIT_FRESH="0")[0]
    git(dev, "reset", "-q", "--hard", "v0.0.1")  # dev checkout on a branch, behind origin
    assert "VERSION old" in run(dev, tmp_path)[0]
    git(dev, "checkout", "-q", "--detach")  # detached local work ahead of the release is not replaced
    write(dev, "local")
    git(dev, "commit", "-qam", "wip")
    assert "VERSION local" in run(dev, tmp_path)[0]


def test_offline_warns_and_runs_installed(tmp_path):
    _, clone = setup(tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    out, err = run(clone, tmp_path)
    assert "VERSION old" in out and "could not check for a newer release" in err


def test_changed_dependency_header_runs_the_release_in_its_own_uv_env(tmp_path):
    """The installed copy's interpreter lacks a dependency the release's header adds; `uv run --script` builds it."""
    dev, clone = setup(tmp_path)
    write(dev, "dep", '# /// script\n# requires-python = ">=3.10"\n# dependencies = []\n# ///\n'
          'print("PREFIX", __import__("sys").prefix)\n')
    publish(dev, "header", "v0.0.3")
    out, err = run(clone, tmp_path)
    assert "VERSION dep ['--x']" in out, err
    assert f"PREFIX {sys.prefix}\n" not in out
