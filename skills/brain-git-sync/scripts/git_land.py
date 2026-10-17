#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Land your own change from a SHARED checkout onto origin/main without touching that checkout.

Parallel agents share one checkout: committing, amending or stashing there sweeps in (or rewrites)
someone else's work. This tool never touches the source checkout's index, HEAD or stash. It builds a
throwaway detached worktree at a freshly fetched origin/<branch>, brings in ONLY your change (listed
paths' working-tree bytes, or one cherry-picked commit), commits with the repo's hooks, pushes
non-force (optionally the same commit to more branches, e.g. a mirror's `stable`, also when main already
has it), verifies each remote ref contains it, and removes the worktree on every exit. A push lost to a
concurrent publisher is retried from the new tip; nothing is ever force-pushed or rewritten. Optionally
refreshes the mirror per project with `rp dev mirror refresh --expect-sha`.

    uv run --no-project python git_land.py --repo ~/code/x/mirror --paths a.py docs/b.md \
      --message 'fix: …' --also stable --refresh-mirror mirror --project p1 --project p2
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from brain_git_sync import _unsafe_staged_paths  # noqa: E402  (one secret/cache rule set)


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

EXIT_FAILED, EXIT_BLOCKED, EXIT_CONFLICT, EXIT_REFRESH = 1, 2, 3, 4


class LandError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.code = code


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(['git', '-C', str(cwd), *args], text=True, capture_output=True)
    if check and done.returncode:
        raise LandError(f'git {" ".join(args)} failed: {(done.stderr or done.stdout).strip()}')
    return done


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def fetch(repo: Path, branches: list[str]) -> None:
    git(repo, 'fetch', '-q', 'origin', *[f'+refs/heads/{b}:refs/remotes/origin/{b}' for b in branches
                                         if git(repo, 'ls-remote', '--exit-code', '--heads', 'origin', b,
                                                check=False).returncode == 0])


def remote_sha(repo: Path, branch: str) -> str:
    line = out(repo, 'ls-remote', 'origin', f'refs/heads/{branch}')
    return line.split()[0] if line else ''


def expand_paths(repo: Path, specs: list[str], base: str) -> list[str]:
    """Repo-relative files under each spec: present in the working tree (tracked or untracked, not
    ignored) or tracked at base (so a deleted file lands as a deletion)."""
    files: set[str] = set()
    for spec in specs:
        path = Path(spec)
        rel = path.resolve().relative_to(repo) if path.is_absolute() else PurePosixPath(spec)
        rel = PurePosixPath(*PurePosixPath(rel).parts)
        if not rel.parts or '..' in rel.parts or rel.parts[0] == '.git':
            raise LandError(f'path must be inside the repository: {spec}', EXIT_BLOCKED)
        found = set(out(repo, 'ls-files', '-co', '--exclude-standard', '--', str(rel)).splitlines())
        found |= set(out(repo, 'ls-tree', '-r', '--name-only', base, '--', str(rel)).splitlines())
        if not found:
            ignored = git(repo, 'check-ignore', '-q', str(rel), check=False).returncode == 0
            raise LandError(f'{spec}: ' + ('ignored by .gitignore; refusing to land it' if ignored
                                           else 'matches no file in the checkout or at origin'), EXIT_BLOCKED)
        files |= found
    return sorted(files)


def copy_paths(repo: Path, wt: Path, files: list[str]) -> None:
    touched = []
    for name in files:
        src, dst = repo / name, wt / name
        existed = dst.is_symlink() or dst.exists()
        if dst.is_dir() and not dst.is_symlink():  # a directory at origin replaced by a file (or deleted)
            shutil.rmtree(dst)
        elif existed:
            dst.unlink()
        if src.is_symlink() or src.is_file():
            for parent in reversed(PurePosixPath(name).parents[:-1]):  # a file at origin replaced by a directory
                if (wt / parent).is_symlink() or (wt / parent).is_file():
                    (wt / parent).unlink()
                    touched.append(str(parent))
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                dst.symlink_to(os.readlink(src))
            else:
                shutil.copy2(src, dst)
        if existed or dst.is_symlink() or dst.exists():
            touched.append(name)
    if touched:
        git(wt, 'add', '-A', '--', *touched)


def cherry_pick(wt: Path, sha: str) -> None:
    # --no-commit + `commit -C` (below) so the pre-commit hook runs; a plain cherry-pick skips it.
    done = git(wt, 'cherry-pick', '--no-commit', sha, check=False)
    if done.returncode:
        conflicts = out(wt, 'diff', '--name-only', '--diff-filter=U').split()
        raise LandError(f'cherry-pick {sha[:12]} conflicts with origin in: ' + (', '.join(conflicts) or
                        (done.stderr or done.stdout).strip()) + '; nothing landed', EXIT_CONFLICT)


def attempt(repo: Path, args: argparse.Namespace, branches: list[str]) -> dict:
    fetch(repo, branches)
    target = branches[0]
    base = out(repo, 'rev-parse', f'refs/remotes/origin/{target}')
    files = expand_paths(repo, args.paths, base) if args.paths else out(
        repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', '--root', args.commit).splitlines()
    unsafe = _unsafe_staged_paths(files)
    if unsafe:
        raise LandError('refusing secret/cache paths: ' + ', '.join(unsafe), EXIT_BLOCKED)
    tmp = Path(tempfile.mkdtemp(prefix='git-land-'))
    wt = tmp / 'wt'
    try:
        git(repo, 'worktree', 'add', '-q', '--detach', str(wt), base)
        if args.paths:
            copy_paths(repo, wt, files)
        else:
            cherry_pick(wt, args.commit)
        if git(wt, 'diff', '--cached', '--quiet', check=False).returncode == 0:
            sha, stat = base, ''  # origin already has these bytes; still promote any --also branch
        else:
            message = ['-m', args.message] if args.message else ['-C', args.commit]
            done = git(wt, 'commit', '-q', *message, check=False)  # hooks run: never --no-verify
            if done.returncode:
                raise LandError('commit refused (pre-commit hook?): ' + (done.stderr or done.stdout).strip(),
                                EXIT_BLOCKED)
            sha = out(wt, 'rev-parse', 'HEAD')
            stat = out(wt, 'show', '--stat', '--format=%h %s', 'HEAD')
        tips = {target: base}
        for extra in branches[1:]:
            tips[extra] = git(repo, 'rev-parse', '-q', '--verify', f'refs/remotes/origin/{extra}',
                              check=False).stdout.strip()
            if tips[extra] and git(wt, 'merge-base', '--is-ancestor', tips[extra], sha, check=False).returncode:
                raise LandError(f'origin/{extra} is not an ancestor of origin/{target}; promote it '
                                f'separately (refusing to force it)', EXIT_BLOCKED)
        pushing = [b for b in branches if tips[b] != sha]
        status = 'landed' if sha != base else 'noop'
        if not pushing or args.dry_run:
            return dict(status='dry-run' if args.dry_run and pushing else status, sha=sha, base=base,
                        files=files, pushed=[], stat=stat)
        push = git(wt, '-c', 'push.followTags=false', 'push', '--atomic', '--porcelain', 'origin',
                   *[f'HEAD:refs/heads/{b}' for b in pushing], check=False)
        if push.returncode:
            return dict(status='rejected', sha=sha, base=base, tips={b: tips[b] for b in pushing},
                        detail=(push.stderr or push.stdout).strip())
        # Verify by ancestry, not equality: a colleague may already have built on top of our push.
        fetch(repo, pushing)  # also keeps the source checkout's remote-tracking refs honest
        lost = [b for b in pushing if git(repo, 'merge-base', '--is-ancestor', sha, f'refs/remotes/origin/{b}',
                                          check=False).returncode]
        if lost:
            raise LandError(f'pushed {sha} but origin/{", origin/".join(lost)} no longer contains it')
        return dict(status=status, sha=sha, base=base, files=files, pushed=pushing, stat=stat)
    finally:
        git(repo, 'worktree', 'remove', '--force', str(wt), check=False)
        git(repo, 'worktree', 'prune', check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def land(repo: Path, args: argparse.Namespace) -> dict:
    branches = [args.push_to, *[b for b in args.also if b != args.push_to]]
    for n in range(1, args.max_attempts + 1):
        result = attempt(repo, args, branches)
        result['attempts'] = n
        if result['status'] != 'rejected':
            return result
        if all(remote_sha(repo, b) == tip for b, tip in result['tips'].items()):
            raise LandError('push rejected, and not by a concurrent publish: ' + result['detail'])
        print(f'git-land: origin moved during attempt {n}; retrying from the new tip',
              file=sys.stderr)
    raise LandError(f'origin kept moving; gave up after {args.max_attempts} attempts')


def refresh(repo: Path, name: str, projects: list[str], sha: str, dry_run: bool) -> list[dict]:
    helped = subprocess.run([_cli(), 'dev', 'mirror', 'refresh', '--help'], capture_output=True, text=True)
    extra = ['--accept-descendant'] if '--accept-descendant' in helped.stdout + helped.stderr else []
    verdicts = []
    for project in projects:
        cmd = [_cli(), '--project', project, 'dev', 'mirror', 'refresh', '--repo', name,
               '--expect-sha', sha, *extra]
        if dry_run:
            verdicts.append(dict(project=project, ok=True, detail='dry-run: ' + ' '.join(cmd)))
            continue
        # cwd=repo: --accept-descendant checks ancestry with git in the current directory.
        done = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        tail = (done.stdout + done.stderr).strip().splitlines()
        verdicts.append(dict(project=project, ok=done.returncode == 0, detail=(tail or [''])[-1]))
    return verdicts


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--repo', type=Path, required=True, help='the (shared) source checkout')
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument('--paths', nargs='+', help='repo-relative files/dirs: land their working-tree bytes')
    what.add_argument('--commit', help='land this one commit (cherry-pick; reuses its message)')
    p.add_argument('--message', help='commit message (required with --paths)')
    p.add_argument('--push-to', default='main', help='branch the worktree starts from and pushes to')
    p.add_argument('--also', action='append', default=[], help='also push the same commit here (e.g. stable)')
    p.add_argument('--refresh-mirror', metavar='NAME', help='rc mirror name to refresh per --project')
    p.add_argument('--project', action='append', default=[])
    p.add_argument('--max-attempts', type=int, default=3)
    p.add_argument('--dry-run', action='store_true', help='build + commit in the worktree, push nothing')
    p.add_argument('--json', action='store_true')
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.paths and not args.message:
        parser().error('--paths needs --message')
    if bool(args.refresh_mirror) != bool(args.project):
        parser().error('--refresh-mirror and --project go together')
    try:
        repo = Path(out(args.repo.expanduser(), 'rev-parse', '--show-toplevel')).resolve()
        if args.commit:
            args.commit = out(repo, 'rev-parse', '--verify', args.commit + '^{commit}')
            if len(out(repo, 'rev-list', '--parents', '-n1', args.commit).split()) > 2:
                raise LandError('merge commits are not landed; land the paths instead', EXIT_BLOCKED)
        result = land(repo, args)
        code = 0
        if args.refresh_mirror and result['status'] in ('landed', 'noop', 'dry-run'):
            result['mirrors'] = refresh(repo, args.refresh_mirror, args.project, result['sha'], args.dry_run)
            code = 0 if all(v['ok'] for v in result['mirrors']) else EXIT_REFRESH
    except LandError as exc:
        result, code = dict(status='failed', error=str(exc)), exc.code
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if code and 'error' in result:
            print('git-land: ' + result['error'], file=sys.stderr)
        else:
            print(f"{result['status']} {result['sha']} (attempts {result['attempts']}; pushed "
                  f"{', '.join(result['pushed']) or 'nothing'})")
            if result.get('stat'):
                print(result['stat'])
            for v in result.get('mirrors', []):
                print(f"  mirror {args.refresh_mirror} @ {v['project']}: {'ok' if v['ok'] else 'FAILED'} {v['detail']}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
