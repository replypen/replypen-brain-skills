import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import helper_prototype as helper
from prototype_format import reduced, prototype_markdown
from publish import payload
from report_schema import Prototype


def sample():
    return Prototype(repo='/tmp/repo', branch='review/test-helper', files=1, command='python skills/helper.py',
                     before=dict(stdout='old', stderr='failure', exit_code=2, sha='a'*40, run_id='before'),
                     after=dict(stdout='new', stderr='', exit_code=0, sha='b'*40, run_id='after'))


def test_publish_prototype_only_on_technical_row():
    f = SimpleNamespace(prototype=sample(), prompt=None, title='Test', title_nl='Test', kind='script_error',
                        severity='medium', root_cause=None, scope=SimpleNamespace(axis_value=lambda: ''),
                        text_en='Problem', text_nl='Owner task', ask_nl='Check?', ask_for=None,
                        evidence=SimpleNamespace(entries=[]), options=[])
    tech = payload(f, 'technical', [])
    assert tech['options'][0]['label'] == 'Merge branch'
    assert '## Before' in tech['body'] and '## After' in tech['body']
    assert 'repo@review/test-helper (1 files)' in tech['body']
    owner = payload(f, 'owner', [])
    assert owner['body'] == 'Owner task' and not owner['options']


def test_capture_protocol_separates_helper_failure_from_transport(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(
        dict(exit_code=2, stdout='old', stderr='error', run_id='run')), stderr='', returncode=4))
    assert helper.run_version({}, ['python', 'missing.py'], [], 'a'*40)['exit_code'] == 2
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='{"error":"denied"}', stderr='', returncode=1))
    with pytest.raises(ValueError, match='No complete'):
        helper.run_version({}, ['python', 'missing.py'], [], 'a'*40)


def test_staging_executes_exact_bytes_and_literal_arguments():
    import base64
    files = {'ARGV-DATA.bin': 'ARGV', 'skills/helper.py': base64.b64encode(b'import sys; print(sys.argv[1])').decode()}
    command = helper.console_command(files, ['python', 'skills/helper.py', '$(touch /tmp/not-executed)'])
    done = subprocess.run(['sh', '-c', command], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == '$(touch /tmp/not-executed)'


def test_privacy_fences_and_failed_after():
    assert 'person@example.com' not in reduced('contact person@example.com')
    assert 'secretvalue' not in reduced('token=secretvalue')
    p = sample()
    p.before.stdout = '```\n<script>alert(1)</script>\n```'
    assert '````text' in prototype_markdown(p)
    raw = p.model_dump(); raw['after']['exit_code'] = 1
    with pytest.raises(ValueError, match='AFTER'):
        Prototype.model_validate(raw)


def test_capture_ships_changed_files_and_borrows_a_large_unchanged_import(tmp_path, monkeypatch):
    import base64, os
    import mirror_try
    remote, repo, live = tmp_path / 'remote.git', tmp_path / 'mirror', tmp_path / 'live'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    ident = ['-c', 'user.name=T', '-c', 'user.email=t@example.org']
    (repo / 'skills').mkdir()
    (repo / 'skills/helper.py').write_text('import ka\nprint(ka.VALUE, 1)\n')
    # ~200 KB of incompressible source: alone over the 120 KiB console limit, like the mirror's ka.py.
    (repo / 'skills/ka.py').write_text(f'VALUE = "ka"\nBLOB = "{base64.b64encode(os.urandom(150_000)).decode()}"\n')
    helper.git(repo, 'add', '.'); helper.git(repo, *ident, 'commit', '-qm', 'base')
    helper.git(repo, 'remote', 'add', 'origin', str(remote)); helper.git(repo, 'push', '-q', 'origin', 'main')
    subprocess.run(['cp', '-R', str(repo), str(live)], check=True)
    state = helper.prepare(repo, 'borrow-ka', tmp_path / 'state.json')
    wt = Path(state['worktree'])
    (wt / 'skills/helper.py').write_text('import ka\nprint(ka.VALUE, 2)\n')
    helper.git(wt, *ident, 'commit', '-qam', 'change')
    runs = []
    def run(files, argv, scope, sha, name, **live_args):
        done = subprocess.run(['sh', '-c', mirror_try.console_command(files, argv, name, **live_args)],
                              capture_output=True, text=True)
        runs.append(sorted(files))
        if done.returncode == mirror_try.STALE_EXIT:  # as run_version parses the box's marker
            raise mirror_try.StaleBorrowed(json.loads(done.stderr.split(mirror_try.STALE_MARKER)[1]))
        return dict(stdout=done.stdout, stderr=done.stderr, exit_code=done.returncode, sha=sha, run_id='r')
    monkeypatch.setattr(mirror_try, 'run_version', run)
    argv = ['python', 'skills/helper.py']
    proto = helper.capture(state, 'p', 't', None, None, [], argv, live=str(live))
    assert runs == [['skills/helper.py']] * 2
    assert (proto.before.stdout.strip(), proto.after.stdout.strip()) == ('ka 1', 'ka 2')
    assert helper.git(repo, 'ls-remote', 'origin', 'refs/heads/review/borrow-ka').split()[0] == helper.git(wt, 'rev-parse', 'HEAD')
    assert helper.live_root(repo) == '/mirrors/mirror'
    (live / 'skills/ka.py').write_text('VALUE = "drifted"\n')  # live differs from base: full staging, too big
    with pytest.raises(ValueError, match=r'[\d,]+ bytes exceeds the console limit of 122,880 bytes[\s\S]*ka\.py \d+ KB ← helper'):
        helper.capture(state, 'p', 't', None, None, [], argv, live=str(live))
