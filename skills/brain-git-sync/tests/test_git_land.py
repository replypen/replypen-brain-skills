import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import git_land


def sh(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    gitconfig = tmp_path / 'gitconfig'
    gitconfig.write_text('[user]\n\tname = Test\n\temail = t@example.org\n[init]\n\tdefaultBranch = main\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    origin, shared, other = tmp_path / 'origin.git', tmp_path / 'shared', tmp_path / 'other'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    subprocess.run(['git', 'clone', '-q', str(origin), str(shared)], check=True, capture_output=True)
    for name, text in {'mine.txt': 'base\n', 'foreign.txt': 'base\n', 'gone.txt': 'x\n', 'shared.txt': 'a\nb\n'}.items():
        (shared / name).write_text(text)
    sh(shared, 'add', '.')
    sh(shared, 'commit', '-qm', 'seed')
    sh(shared, 'push', '-q', 'origin', 'HEAD:main', 'HEAD:stable')
    subprocess.run(['git', 'clone', '-q', str(origin), str(other)], check=True, capture_output=True)
    return dict(origin=origin, shared=shared, other=other)


def remote(world, branch='main'):
    return sh(world['origin'], 'rev-parse', branch)


def run(capsys, *argv):
    code = git_land.main([*argv, '--json'])
    return code, json.loads(capsys.readouterr().out)


def foreign_push(world, name='other.txt', text='other\n'):
    (world['other'] / name).write_text(text)
    sh(world['other'], 'add', '.')
    sh(world['other'], 'commit', '-qm', 'foreign')
    sh(world['other'], 'pull', '-q', '--no-rebase', 'origin', 'main')
    sh(world['other'], 'push', '-q', 'origin', 'HEAD:main')


def test_paths_mode_lands_only_own_files_and_leaves_shared_checkout_alone(world, capsys):
    shared = world['shared']
    head = sh(shared, 'rev-parse', 'HEAD')
    (shared / 'foreign.txt').write_text('colleague WIP\n')
    (shared / 'untracked-wip.txt').write_text('colleague\n')
    (shared / 'mine.txt').write_text('mine\n')
    (shared / 'new').mkdir()
    (shared / 'new' / 'x.py').write_text('print(1)\n')
    (shared / 'gone.txt').unlink()
    sh(shared, 'add', 'foreign.txt')  # a colleague's staged hunk must stay staged and unlanded
    status = sh(shared, 'status', '--porcelain')

    code, result = run(capsys, '--repo', str(shared), '--paths', 'mine.txt', 'new', 'gone.txt',
                       '--message', 'land mine', '--also', 'stable')

    assert code == 0, result
    sha = result['sha']
    assert result['status'] == 'landed' and remote(world) == remote(world, 'stable') == sha
    assert sh(world['origin'], 'show', f'{sha}:mine.txt') == 'mine'
    assert sh(world['origin'], 'show', f'{sha}:new/x.py') == 'print(1)'
    assert sh(world['origin'], 'show', f'{sha}:foreign.txt') == 'base'
    assert 'gone.txt' not in sh(world['origin'], 'ls-tree', '--name-only', sha)
    assert sh(world['origin'], 'log', '-1', '--format=%s', sha) == 'land mine'
    # The shared checkout: same HEAD, same index/working tree, no stash, no leftover worktree.
    assert sh(shared, 'rev-parse', 'HEAD') == head
    assert sh(shared, 'status', '--porcelain') == status
    assert sh(shared, 'stash', 'list') == ''
    assert len(sh(shared, 'worktree', 'list').splitlines()) == 1


def test_commit_mode_retries_after_concurrent_push(world, capsys, monkeypatch):
    shared = world['shared']
    (shared / 'mine.txt').write_text('committed\n')
    sh(shared, 'commit', '-qam', 'my fix')
    mine = sh(shared, 'rev-parse', 'HEAD')
    real_fetch, calls = git_land.fetch, []

    def racing_fetch(repo, branches):
        real_fetch(repo, branches)
        calls.append(1)
        if len(calls) == 1:  # someone publishes between our fetch and our push
            foreign_push(world)
    monkeypatch.setattr(git_land, 'fetch', racing_fetch)

    code, result = run(capsys, '--repo', str(shared), '--commit', mine)

    assert code == 0, result
    assert result['attempts'] == 2 and remote(world) == result['sha'] != mine
    assert sh(world['origin'], 'show', f"{result['sha']}:other.txt") == 'other'
    assert sh(world['origin'], 'show', f"{result['sha']}:mine.txt") == 'committed'
    assert sh(world['origin'], 'log', '-1', '--format=%s', result['sha']) == 'my fix'
    assert remote(world, 'stable') != result['sha']  # --also not given


def test_conflicting_cherry_pick_aborts_cleanly(world, capsys):
    shared = world['shared']
    (shared / 'shared.txt').write_text('a\nmine\n')
    sh(shared, 'commit', '-qam', 'mine')
    foreign_push(world, 'shared.txt', 'a\ntheirs\n')
    before = remote(world)

    code, result = run(capsys, '--repo', str(shared), '--commit', 'HEAD')

    assert code == git_land.EXIT_CONFLICT and 'shared.txt' in result['error']
    assert remote(world) == before
    assert len(sh(shared, 'worktree', 'list').splitlines()) == 1


def test_pre_commit_hook_runs_in_worktree_and_blocks(world, capsys):
    shared = world['shared']
    hook = shared / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\ngit diff --cached | grep -q FORBIDDEN && { echo hygiene: bad >&2; exit 1; }\n'
                    'touch "$(git rev-parse --git-common-dir)/hook-ran"\n')
    hook.chmod(0o755)
    (shared / 'mine.txt').write_text('FORBIDDEN\n')
    code, result = run(capsys, '--repo', str(shared), '--paths', 'mine.txt', '--message', 'x')
    assert code == git_land.EXIT_BLOCKED and 'hygiene: bad' in result['error']
    (shared / 'mine.txt').write_text('fine\n')
    code, result = run(capsys, '--repo', str(shared), '--paths', 'mine.txt', '--message', 'x')
    assert code == 0 and (shared / '.git' / 'hook-ran').exists()


def test_refuses_secrets_and_ignored_paths(world, capsys):
    shared = world['shared']
    (shared / '.env').write_text('TOKEN=1\n')
    code, result = run(capsys, '--repo', str(shared), '--paths', '.env', '--message', 'x')
    assert code == git_land.EXIT_BLOCKED and '.env' in result['error']
    (shared / '.gitignore').write_text('scratch/\n')
    (shared / 'scratch').mkdir()
    (shared / 'scratch' / 'a.txt').write_text('a\n')
    code, result = run(capsys, '--repo', str(shared), '--paths', 'scratch/a.txt', '--message', 'x')
    assert code == git_land.EXIT_BLOCKED and 'ignored' in result['error']


def test_refresh_mirror_per_project(world, capsys, monkeypatch, tmp_path):
    bin_dir, log = tmp_path / 'bin', tmp_path / 'rc.log'
    bin_dir.mkdir()
    (bin_dir / 'rc').write_text(f'#!/bin/sh\ncase "$*" in *--help*) echo "  --accept-descendant"; exit 0;; esac\n'
                                f'echo "$*" >> {log}\ncase "$*" in *bad*) echo boom; exit 1;; esac\necho refreshed\n')
    (bin_dir / 'rc').chmod(0o755)
    (bin_dir / 'rp').symlink_to('rc')  # subprocesses resolve `rp` first
    monkeypatch.setenv('PATH', f"{bin_dir}:{__import__('os').environ['PATH']}")
    (world['shared'] / 'mine.txt').write_text('m\n')
    code, result = run(capsys, '--repo', str(world['shared']), '--paths', 'mine.txt', '--message', 'x',
                       '--refresh-mirror', 'common', '--project', 'p1', '--project', 'bad')
    assert code == git_land.EXIT_REFRESH
    assert [v['ok'] for v in result['mirrors']] == [True, False]
    lines = log.read_text().splitlines()
    assert lines[0] == f"--project p1 dev mirror refresh --repo common --expect-sha {result['sha']} --accept-descendant"


def test_noop_still_promotes_requested_stable_and_refreshes(world, capsys, monkeypatch, tmp_path):
    bin_dir, log = tmp_path / 'bin', tmp_path / 'rc.log'
    bin_dir.mkdir()
    (bin_dir / 'rc').write_text(f'#!/bin/sh\ncase "$*" in *--help*) exit 0;; esac\necho "$*" >> {log}\n')
    (bin_dir / 'rc').chmod(0o755)
    (bin_dir / 'rp').symlink_to('rc')  # subprocesses resolve `rp` first
    monkeypatch.setenv('PATH', f"{bin_dir}:{__import__('os').environ['PATH']}")
    shared = world['shared']
    (shared / 'mine.txt').write_text('new\n')
    sh(shared, 'commit', '-qam', 'mine')
    sh(shared, 'push', '-q', 'origin', 'HEAD:main')
    code, result = run(capsys, '--repo', str(shared), '--commit', 'HEAD', '--also', 'stable',
                       '--refresh-mirror', 'common', '--project', 'p1')
    assert code == 0 and result['status'] == 'noop' and result['pushed'] == ['stable'], result
    assert remote(world, 'stable') == remote(world) == result['sha']
    assert log.read_text().split() == f"--project p1 dev mirror refresh --repo common --expect-sha {result['sha']}".split()
    code, result = run(capsys, '--repo', str(shared), '--commit', 'HEAD', '--also', 'stable')
    assert code == 0 and result['status'] == 'noop' and result['pushed'] == []


def test_identical_add_already_landed_is_noop(world, capsys):
    shared, other = world['shared'], world['other']
    for repo, subject in ((shared, 'mine add'), (other, 'their add')):
        (repo / 'added.txt').write_text('identical\n')
        sh(repo, 'add', 'added.txt')
        sh(repo, 'commit', '-qm', subject)
    sh(other, 'push', '-q', 'origin', 'HEAD:main')
    code, result = run(capsys, '--repo', str(shared), '--commit', 'HEAD')
    assert code == 0 and result['status'] == 'noop', result


def test_paths_replace_directory_with_file_and_back(world, capsys):
    shared = world['shared']
    (shared / 'slot').mkdir()
    (shared / 'slot' / 'old.txt').write_text('old\n')
    sh(shared, 'add', 'slot')
    sh(shared, 'commit', '-qm', 'directory')
    sh(shared, 'push', '-q', 'origin', 'HEAD:main')
    (shared / 'slot' / 'old.txt').unlink()
    (shared / 'slot').rmdir()
    (shared / 'slot').write_text('replacement\n')
    code, result = run(capsys, '--repo', str(shared), '--paths', 'slot', '--message', 'to file')
    assert code == 0, result
    assert sh(world['origin'], 'ls-tree', '-r', '--name-only', remote(world), 'slot') == 'slot'
    assert sh(world['origin'], 'show', f"{remote(world)}:slot") == 'replacement'
    (shared / 'slot').unlink()
    (shared / 'slot').mkdir()
    (shared / 'slot' / 'new.txt').write_text('new\n')
    code, result = run(capsys, '--repo', str(shared), '--paths', 'slot', '--message', 'to dir')
    assert code == 0, result
    assert sh(world['origin'], 'ls-tree', '-r', '--name-only', remote(world), 'slot') == 'slot/new.txt'


def test_landing_survives_descendant_pushed_before_verification(world, capsys, monkeypatch):
    shared = world['shared']
    (shared / 'mine.txt').write_text('my change\n')
    real_git = git_land.git

    def racing_git(cwd, *args, check=True):
        done = real_git(cwd, *args, check=check)
        if 'push' in args and not done.returncode:  # a colleague builds on our commit before we verify
            foreign_push(world)
        return done
    monkeypatch.setattr(git_land, 'git', racing_git)
    code, result = run(capsys, '--repo', str(shared), '--paths', 'mine.txt', '--message', 'mine')
    assert code == 0 and result['status'] == 'landed', result
    assert remote(world) != result['sha']
    sh(world['origin'], 'merge-base', '--is-ancestor', result['sha'], remote(world))
    assert sh(world['origin'], 'show', f'{remote(world)}:mine.txt') == 'my change'


def test_refresh_mirror_without_project_publishes_to_every_consumer(world, capsys, monkeypatch, tmp_path):
    bin_dir, log = tmp_path / 'bin', tmp_path / 'rc.log'
    bin_dir.mkdir()
    (bin_dir / 'rp').write_text(f'#!/bin/sh\necho "$*" >> {log}\necho "LIVE  p1  stable  x"\n')
    (bin_dir / 'rp').chmod(0o755)
    monkeypatch.setenv('PATH', f"{bin_dir}:{__import__('os').environ['PATH']}")
    (world['shared'] / 'mine.txt').write_text('m\n')
    code, result = run(capsys, '--repo', str(world['shared']), '--paths', 'mine.txt', '--message', 'x',
                       '--also', 'stable', '--refresh-mirror', 'common')
    assert code == 0 and result['mirrors'][0]['ok'], result
    assert log.read_text().split() == f"dev mirror publish --repo common --sha {result['sha']} -o table".split()
