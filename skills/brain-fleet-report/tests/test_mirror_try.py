import base64
from pathlib import Path
import subprocess
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mirror-try/scripts'))
import mirror_try as mirror


def commit(repo):
    mirror.git(repo, 'add', '.')
    mirror.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'fixture')
    return mirror.git(repo, 'rev-parse', 'HEAD')


def test_revision_import_closure_and_working_diff(tmp_path):
    mirror.git(tmp_path, 'init', '-q')
    folder = tmp_path / 'helpers'
    folder.mkdir()
    (folder / 'main.py').write_text('from sibling import result\nprint(result)\n')
    (folder / 'sibling.py').write_text('from package import result\n')
    package = tmp_path / 'package'
    package.mkdir()
    (package / '__init__.py').write_text('from .value import result\n')
    (package / 'value.py').write_text('result = "before"\n')
    before = commit(tmp_path)
    (package / 'value.py').write_text('result = "after"\n')
    for working, expected in [(False, 'before'), (True, 'after')]:
        files = mirror.stage(tmp_path, before, ['helpers/main.py'], working=working)
        assert set(files) == {'helpers/main.py', 'helpers/sibling.py', 'package/__init__.py', 'package/value.py'}
        done = subprocess.run(['sh', '-c', mirror.console_command(files, ['python', 'helpers/main.py'])],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == expected
    (folder / 'sibling.py').unlink()
    (folder / 'sibling.py').symlink_to(package / 'value.py')
    with pytest.raises(ValueError, match='Symlinks'):
        mirror.stage(tmp_path, before, ['helpers/main.py'], working=True)


def test_compare_stages_changed_dependency_both_sides_before_call(tmp_path, monkeypatch):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('import dependency\nprint(dependency.value)\n')
    (tmp_path / 'dependency.py').write_text('value=1\n')
    before = commit(tmp_path)
    (tmp_path / 'dependency.py').write_text('value=2\n')
    after = commit(tmp_path)
    seen = []
    def run(files, argv, scope, sha, name, **live):
        seen.append((base64.b64decode(files['dependency.py']), scope, sha))
        return {'exit_code': 0}
    monkeypatch.setattr(mirror, 'run_version', run)
    mirror.compare(tmp_path, after, False, before, ['python', 'helper.py'], 'project', 'tenant', 'parent', '42')
    assert [s[0] for s in seen] == [b'value=1\n', b'value=2\n']
    assert seen[0][1] == seen[1][1] == ['--project', 'project', '--tenant', 'tenant', '--principal-kind', 'parent', '--principal-id', '42']
    (tmp_path / 'actions').mkdir()
    (tmp_path / 'actions/write.py').write_text('print(1)')
    bad = commit(tmp_path)
    seen.clear()
    with pytest.raises(ValueError, match='actions'):
        mirror.compare(tmp_path, bad, False, before, ['python', 'helper.py'], 'project', paths=['actions/write.py'])
    assert not seen


def test_large_staging_is_compressed_and_paths_are_guarded():
    source = b'# padding\n' * 15000 + b'print("large payload ran")\n'
    files = {'helper.py': base64.b64encode(source).decode()}
    command = mirror.console_command(files, ['python', 'helper.py'])
    assert len(command.encode()) < 120_000
    result = subprocess.run(['sh', '-c', command], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == 'large payload ran'
    for path in ('../outside.py', '/absolute.py', '.env', 'nested/actions/write.py'):
        with pytest.raises(ValueError, match='grounding'):
            mirror.console_command({path: ''}, ['python', 'helper.py'])


def test_hidden_and_action_changes_are_omitted_and_broken_base_runs(tmp_path, monkeypatch):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('def (:')
    before = commit(tmp_path)
    (tmp_path / 'helper.py').write_text('print("fixed")\n')
    (tmp_path / '.gitignore').write_text('cache/\n')
    (tmp_path / 'actions').mkdir()
    (tmp_path / 'actions' / 'write.py').write_text('raise Exception("must never run")')
    after = commit(tmp_path)
    def run(files, argv, scope, sha, name, **live):
        assert set(files) == {'helper.py'}
        done = subprocess.run(['sh', '-c', mirror.console_command(files, argv, **live)], capture_output=True, text=True)
        return {'exit_code': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr}
    monkeypatch.setattr(mirror, 'run_version', run)
    result = mirror.compare(tmp_path, after, False, before, ['python', 'helper.py'], 'project')
    assert result['before']['exit_code'] != 0 and 'SyntaxError' in result['before']['stderr']
    assert result['after']['exit_code'] == 0 and result['after']['stdout'] == 'fixed\n'


def test_changed_only_staging_borrows_unchanged_imports_from_live_mirror(tmp_path, monkeypatch):
    repo, live = tmp_path / 'repo', tmp_path / 'live'
    repo.mkdir()
    mirror.git(repo, 'init', '-q')
    (repo / 'helper.py').write_text('import big, small\nprint(big.value, small.value)\n')
    (repo / 'big.py').write_text('value = "big"\n' + '# padding\n' * 30000)
    (repo / 'small.py').write_text('value = 1\n')
    before = commit(repo)
    subprocess.run(['cp', '-R', str(repo), str(live)], check=True)
    (repo / 'small.py').write_text('value = 2\n')
    after = commit(repo)
    outputs = []
    def run(files, argv, scope, sha, name, **live_args):
        done = subprocess.run(['sh', '-c', mirror.console_command(files, argv, name, **live_args)],
                              capture_output=True, text=True)
        outputs.append((sorted(files), done.stdout, done.stderr))
        return {'exit_code': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr}
    monkeypatch.setattr(mirror, 'run_version', run)
    result = mirror.compare(repo, after, False, before, ['python', 'helper.py'], 'p', mirror=str(live))
    assert result['borrowed'] == ['big.py'] and result['staged_after'] == ['helper.py', 'small.py']
    assert [o[1] for o in outputs] == ['big 1\n', 'big 2\n'] and not any(o[2] for o in outputs)
    assert result['payload']['after'] < 5_000
    (live / 'big.py').write_text('value = "drifted"\n')  # live mirror no longer at base: say so loudly
    outputs.clear()
    mirror.compare(repo, after, False, before, ['python', 'helper.py'], 'p', mirror=str(live))
    assert 'live mirror differs from base for 1 borrowed import(s): big.py' in outputs[0][2]
    outputs.clear()
    result = mirror.compare(repo, after, False, before, ['python', 'helper.py'], 'p', mirror=str(live), no_base=True)
    assert result['before'] is None and len(outputs) == 1 and 'no base run' in mirror.side_by_side(result)


def test_over_budget_names_each_file_and_its_importer(tmp_path):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('import middle\n')
    (tmp_path / 'middle.py').write_text('import heavy\n')
    (tmp_path / 'heavy.py').write_text(''.join(f'x{i} = "{i * 7919 % 100003:x}{i}"\n' for i in range(40000)))
    before = commit(tmp_path)
    (tmp_path / 'helper.py').write_text('import middle  # changed\n')
    after = commit(tmp_path)
    with pytest.raises(ValueError, match=r'heavy\.py \d+ KB ← middle ← helper'):
        mirror.compare(tmp_path, after, False, before, ['python', 'helper.py'], 'p', stage_all=True)


def test_unchanged_import_only_the_base_helper_reaches_is_borrowed(tmp_path, monkeypatch):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('import old\n')
    (tmp_path / 'old.py').write_text('x = 1\n')
    (tmp_path / 'new.py').write_text('x = 2\n')
    before = commit(tmp_path)
    (tmp_path / 'helper.py').write_text('import new\n')
    after = commit(tmp_path)
    monkeypatch.setattr(mirror, 'run_version', lambda *a, **k: {'exit_code': 0})
    result = mirror.compare(tmp_path, after, False, before, ['python', 'helper.py'], 'p')
    assert result['staged_before'] == result['staged_after'] == ['helper.py']
    assert result['borrowed'] == ['new.py', 'old.py']


def test_cli_error_is_one_line_naming_the_only_path(tmp_path):
    mirror.git(tmp_path, 'init', '-q')
    (tmp_path / 'helper.py').write_text('print(1)\n')
    (tmp_path / 'other.py').write_text('x = 1\n')
    commit(tmp_path)
    (tmp_path / 'other.py').write_text('x = 2\n')  # a sibling's edit; ours is unchanged
    done = subprocess.run([sys.executable, mirror.__file__, '--repo', str(tmp_path), '--diff', '--only', 'helper.py',
                           '--project', 'p', '--read-only-reviewed', '--cmd', 'python helper.py'],
                          capture_output=True, text=True)
    assert done.returncode == 2 and 'Traceback' not in done.stderr
    assert done.stderr.startswith('mirror-try: No changed files to compare under --only helper.py')


def test_as_label_resolves_through_the_mirror_roster(tmp_path, monkeypatch):
    mirror_dir = tmp_path / 'mirror'
    (mirror_dir / '_internal').mkdir(parents=True)
    (mirror_dir / '_internal' / 'scopecheck-proj.toml').write_text(
        '[[audience]]\nlabel = "regular-admin"\nkind = "ka_admin"\nid = "u-1"\n')
    brain = tmp_path / 'brain'
    brain.mkdir()
    (brain / '.rootcause.toml').write_text('project = "proj"\n[mirrors]\n"m" = "../mirror"\n')
    monkeypatch.chdir(brain)
    seen = {}

    def fake_compare(*args):
        seen['principal'] = args[7:9]
        raise ValueError('stop')
    monkeypatch.setattr(mirror, 'compare', fake_compare)
    base = ['mirror_try.py', '--repo', str(mirror_dir), '--diff', '--project', 'proj',
            '--read-only-reviewed', '--cmd', 'python h.py']
    monkeypatch.setattr(sys, 'argv', [*base, '--as', 'regular-admin'])
    assert mirror.main() == 2 and seen['principal'] == ('ka_admin', 'u-1')
    monkeypatch.setattr(sys, 'argv', [*base, '--as', 'nobody'])
    assert mirror.main() == 2  # unknown label: one message listing known labels, no traceback
    monkeypatch.setattr(sys, 'argv', [*base, '--as', 'regular-admin', '--principal-id', 'x'])
    with pytest.raises(SystemExit):
        mirror.main()
