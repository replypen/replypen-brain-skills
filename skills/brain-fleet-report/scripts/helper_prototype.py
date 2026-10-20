#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2"]
# ///
"""Prepare a review branch; compare committed helper bytes in the guarded production console."""
import argparse
import json
import shutil
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mirror-try/scripts"))
from mirror_try import console_command, execute, plan, run_version, summary  # noqa: F401 (re-exported)
from prototype_format import prototype_markdown
from report_schema import Prototype


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')
    path.chmod(0o600)


def prepare(repo, slug, out):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError('Use a lowercase signature slug')
    repo = repo.expanduser().resolve()
    if out.exists():
        raise ValueError('State file already exists; resume it instead')
    git(repo, 'fetch', 'origin', 'main')
    base = git(repo, 'rev-parse', 'origin/main')
    branch = 'review/' + slug
    worktree = Path(tempfile.mkdtemp(prefix='helper-prototype-')) / 'worktree'
    git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)
    state = dict(repo=str(repo), branch=branch, base=base, worktree=str(worktree))
    save(out, state)
    return state


def live_root(repo):
    """Where the box mounts this checkout: a brain at /brain, a source mirror at /mirrors/<dir>."""
    is_brain = (repo / '.replypen.toml').is_file() or (repo / '.rootcause.toml').is_file() \
        or re.match(r'(replypen|rootcause)-brain-', repo.name)
    return '/brain' if is_brain else f'/mirrors/{repo.name}'


def capture(state, project, tenant, principal_kind, principal_id, paths, argv, live=None, stage_all=False):
    repo, wt = Path(state['repo']), Path(state['worktree'])
    if git(wt, 'branch', '--show-current') != state['branch'] or git(wt, 'status', '--porcelain'):
        raise ValueError('Commit the helper and tests on the prepared branch first; worktree must be clean')
    after = git(wt, 'rev-parse', 'HEAD')
    git(wt, 'merge-base', '--is-ancestor', state['base'], after)
    changed = git(wt, 'diff', '--name-only', state['base'], after).splitlines()
    if not changed:
        raise ValueError('Prototype has no changes')
    if any(p.startswith('actions/') for p in changed):
        raise ValueError('Action changes are outside helper prototypes')
    if not argv or argv[0] != 'python' or len(argv) < 2 or not argv[1].endswith('.py'):
        raise ValueError('Command must be python relative/helper.py [args]; no shell or inline code')
    script = PurePosixPath(argv[1])
    if script.is_absolute() or '..' in script.parts or script.parts[0] == 'actions':
        raise ValueError('Helper must be a relative grounding Python path')
    # Ship only the helper and changed bytes; unchanged imports are borrowed from the live checkout
    # after the box verifies their sha256 against base (a mismatch stages those files from git instead).
    staged = plan(repo, after, False, state['base'], argv, project, tenant, principal_kind, principal_id,
                  paths, mirror=live or live_root(repo), stage_all=stage_all)
    unstaged = [p for p in changed if p.endswith('.py') and '/tests/' not in '/' + p and not p.startswith('tests/')
                and p not in staged['files']['after']]
    if unstaged:
        raise ValueError('Changed Python outside the helper closure; add it with --path: ' + ', '.join(unstaged))
    runs = execute(staged)
    print('helper-prototype: ' + summary(dict(payload=staged['payload'], staged_after=staged['files']['after'],
                                              borrowed=staged['live'].get('borrowed', {}), restaged=staged['restaged']),
                                         'checkout'), file=sys.stderr)
    prototype = Prototype(repo=str(repo), branch=state['branch'], before=runs['before'], after=runs['after'],
                          files=len(changed), command=shlex.join([_cli(), 'dev', 'console', 'bash', 'run', *staged['scope']]) + ' · staged ' + shlex.join(argv))
    # Publishing is explicit-ref only; never main or a production channel.
    git(wt, 'push', 'origin', f'{after}:refs/heads/{state["branch"]}')
    if git(wt, 'ls-remote', 'origin', f'refs/heads/{state["branch"]}').split()[0] != after:
        raise ValueError('Remote branch changed; recapture before publishing the card')
    return prototype


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='operation', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--repo', type=Path, required=True)
    prep.add_argument('--slug', required=True)
    prep.add_argument('--state', type=Path, required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--state', type=Path, required=True)
    cap.add_argument('--project', required=True)
    cap.add_argument('--tenant')
    cap.add_argument('--principal-kind')
    cap.add_argument('--principal-id')
    cap.add_argument('--path', action='append', default=[], help='Extra data/import file (the helper and its static imports are found)')
    cap.add_argument('--out', type=Path, required=True)
    cap.add_argument('--live', help='Live checkout on the box (default /brain for a brain, else /mirrors/<repo dir>)')
    cap.add_argument('--stage-all', action='store_true', help='Ship the whole import closure (no borrowing)')
    cap.add_argument('--read-only-reviewed', action='store_true', required=True,
                     help='Confirm helper/imports were inspected: no sends, actions, or customer writes')
    cap.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.operation == 'prepare':
        print(json.dumps(prepare(args.repo, args.slug, args.state), indent=2))
        return 0
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        result = capture(json.loads(args.state.read_text()), args.project, args.tenant,
                         args.principal_kind, args.principal_id, args.path, argv, args.live, args.stage_all)
    except ValueError as exc:  # usage/budget/transport: one actionable message, no traceback
        print('helper-prototype: ' + str(exc), file=sys.stderr)
        return 2
    save(args.out, result.model_dump())
    print(prototype_markdown(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
