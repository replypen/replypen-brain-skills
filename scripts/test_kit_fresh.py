"""kit_fresh: an installed clone behind origin/main re-execs from a main snapshot; dev checkouts don't."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

KIT_FRESH = Path(__file__).with_name("kit_fresh.py")
SCRIPT = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import kit_fresh
kit_fresh.ensure(__file__)
print("VERSION", "{v}", sys.argv[1:])
'''


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def write(repo, version):
    s = repo / "skills" / "demo" / "scripts"
    s.mkdir(parents=True, exist_ok=True)
    (s / "demo.py").write_text(SCRIPT.replace("{v}", version))
    (repo / "scripts").mkdir(exist_ok=True)
    shutil.copy(KIT_FRESH, repo / "scripts" / "kit_fresh.py")


def setup(tmp_path):
    origin, dev, clone = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(dev, "config", k, v)
    write(dev, "old")
    git(dev, "add", ".")
    git(dev, "commit", "-qm", "v1")
    git(dev, "tag", "v1")
    git(dev, "push", "-q", "origin", "main", "--tags")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "v1")
    write(dev, "new")
    git(dev, "commit", "-qam", "fix")
    git(dev, "push", "-q", "origin", "main")
    return dev, clone


def run(repo, tmp_path, **env):
    e = {**os.environ, "RP_KIT_CACHE": str(tmp_path / "cache"), **env}
    e.pop("RP_KIT_FRESH", None) if "RP_KIT_FRESH" not in env else None
    done = subprocess.run([sys.executable, str(repo / "skills/demo/scripts/demo.py"), "--x"],
                          capture_output=True, text=True, env=e, cwd=tmp_path)
    return done.stdout, done.stderr


def test_stale_installed_clone_runs_main(tmp_path):
    _, clone = setup(tmp_path)
    out, err = run(clone, tmp_path)
    assert "VERSION new ['--x']" in out
    assert "runs from origin/main" in err
    out, _ = run(clone, tmp_path)  # cached snapshot, no refetch
    assert "VERSION new" in out


def test_opt_out_and_dev_checkout_run_their_own_tree(tmp_path):
    dev, clone = setup(tmp_path)
    assert "VERSION old" in run(clone, tmp_path, RP_KIT_FRESH="0")[0]
    git(dev, "reset", "-q", "--hard", "v1")  # dev checkout on a branch, behind origin
    assert "VERSION old" in run(dev, tmp_path)[0]
    git(dev, "checkout", "-q", "--detach")  # detached local work ahead of main is not replaced
    write(dev, "local")
    git(dev, "commit", "-qam", "wip")
    assert "VERSION local" in run(dev, tmp_path)[0]


def test_offline_warns_and_runs_installed(tmp_path):
    _, clone = setup(tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    out, err = run(clone, tmp_path)
    assert "VERSION old" in out and "could not check origin/main" in err
