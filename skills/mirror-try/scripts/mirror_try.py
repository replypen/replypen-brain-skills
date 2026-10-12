#!/usr/bin/env python3
"""Compare a read-only mirror helper at two revisions in the scoped production console."""
import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import zlib

# All kit skills ship together; retain the fleet's established privacy reducer.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'brain-fleet-report/scripts'))
from prototype_format import reduced


BUDGET_REMEDY = 'commit your files and use --ref <branch> --base <sha>, or --diff --only <path>'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def snapshot(repo, sha, paths):
    entries = tree(repo, sha)
    selected = set()
    for path in paths:
        safe_path(path)
        selected.update(name for name in entries if name == path or name.startswith(path.rstrip('/') + '/'))
    return stage(Path(repo), sha, selected)


def console_command(files, argv, repo_name="helper", mirror=None, borrowed=None):
    """mirror: the live checkout (`/mirrors/<repo>`) that unstaged imports resolve from; borrowed:
    {path: sha256 of the base bytes} for those imports, verified on the box before the helper runs."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", repo_name):
        raise ValueError("Invalid scratch repository name")
    # No shell interpolation of filenames, source, or helper arguments. Scratch is removed on exit.
    source = 'import base64,json,os,pathlib,subprocess,tempfile,zlib\n'
    for name in files:
        safe_path(name)
    payload = base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()
    source += 'files = json.loads(zlib.decompress(base64.b64decode(' + repr(payload) + ')))\n'
    source += 'argv = ' + repr(argv) + '\n'
    source += 'prefix = ' + repr(repo_name + '-') + '\n'
    dirs = sorted({str(PurePosixPath(n).parent) for n in files if n.endswith('.py')}) if mirror else []
    source += 'mirror, dirs, borrowed = ' + repr((mirror, dirs, borrowed or {})) + '\n'
    # Unique scratch per invocation (/tmp/try/<repo>-<pid>-*) so parallel runs never collide.
    source += '''pathlib.Path("/tmp/try").mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix=prefix+str(os.getpid())+"-", dir="/tmp/try") as root:
 for name,data in files.items():
  path=pathlib.Path(root)/name
  path.parent.mkdir(parents=True,exist_ok=True)
  path.write_bytes(base64.b64decode(data))
  path.chmod(0o700)
 import hashlib
 stale=[n for n,h in sorted(borrowed.items()) if not os.path.isfile(os.path.join(mirror,n)) or hashlib.sha256(open(os.path.join(mirror,n),"rb").read()).hexdigest()!=h]
 if stale: print("mirror-try: live mirror differs from base for %d borrowed import(s): %s; unchanged imports come from the live copy" % (len(stale), ", ".join(stale[:5])), file=__import__("sys").stderr)
 env=dict(os.environ)
 live=[os.path.join(root,d) for d in dirs]+([os.path.join(mirror,d) for d in dirs]+[mirror] if mirror else [])
 env["PYTHONPATH"]=os.pathsep.join([root,*live,env.get("PYTHONPATH", "")])
 result=subprocess.run(argv,cwd=root,env=env)
 raise SystemExit(result.returncode)
'''
    if len(source.encode()) > 120_000:
        raise ValueError(f'Compressed payload {len(source.encode()) // 1000} KB exceeds the 120 KB console budget; ' + BUDGET_REMEDY)
    return 'python - <<\'REVIEW_HELPER\'\n' + source + '\nREVIEW_HELPER'


def run_version(files, argv, scope, sha, repo_name="helper", **live):
    cmd = ['rc', 'dev', 'console', 'bash', 'run', *scope, '--raw-output', '-o', 'json',
           '--timeout', '120', '--', '-']
    done = subprocess.run(cmd, input=console_command(files, argv, repo_name, **live), text=True,
                          capture_output=True, timeout=180)
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('Console transport failed: ' + reduced(done.stderr)) from exc
    if 'exit_code' not in result or not result.get('run_id') or result.get('timed_out') or result.get('stdout_truncated') or result.get('stderr_truncated'):
        raise ValueError('No complete helper result: ' + reduced(json.dumps(result)))
    return dict(stdout=reduced(result.get('stdout', '')), stderr=reduced(result.get('stderr', '')),
                exit_code=result['exit_code'], sha=sha, run_id=result['run_id'], seq=result.get('seq', 0))


def safe_path(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or not path.parts or '..' in path.parts
            or any(p.startswith('.') for p in path.parts) or 'actions' in path.parts):
        raise ValueError('Stage relative grounding paths only; no hidden files or actions: ' + name)
    return path


def tree(repo, ref):
    entries = {}
    for entry in subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-rz', ref]).split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            entries[name.decode()] = meta.split()[0].decode()
    return entries


def under(name, paths):
    return any(name == p or name.startswith(p.rstrip('/') + '/') for p in paths)


def stage(repo, ref, seeds, *, working=False, only=(), via=None):
    """Bounded closure of static sibling/root Python imports, plus explicit data paths.

    working: read working-tree bytes; with only, just for files under those paths (rest from ref).
    via: filled with {file: the file that imported it} for the over-budget report."""
    via = {} if via is None else via
    entries = tree(repo, ref)
    if working:
        for name in git(repo, 'ls-files').splitlines():
            if not only or under(name, only):
                entries.setdefault(name, '100644')
    files, pending = {}, list(seeds)
    while pending:
        name = pending.pop()
        path = safe_path(name)
        if name in files or name not in entries:
            continue  # deletions/new helper absent in the baseline are meaningful evidence
        if entries[name] not in ('100644', '100755'):
            raise ValueError('Symlinks/submodules are not supported: ' + name)
        if working and (not only or under(name, only)):
            local = repo / name
            if local.is_symlink() or any(p.is_symlink() for p in local.parents if p != repo.parent):
                raise ValueError('Symlinks are not supported: ' + name)
            if not local.exists():
                continue
            data = local.read_bytes()
        else:
            data = subprocess.check_output(['git', '-C', str(repo), 'show', f'{ref}:{name}'])
        files[name] = base64.b64encode(data).decode()
        if path.suffix != '.py':
            continue
        for parent in path.parents:
            init = str(parent / '__init__.py')
            if init in entries and init != name:
                pending.append(init)
                via.setdefault(init, name)
        try:
            parsed = ast.parse(data, filename=name)
        except SyntaxError:
            continue  # Let the actual interpreter report this revision's syntax failure.
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                modules = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or '', node.level)]
                modules += [('.'.join(filter(None, [node.module, alias.name])), node.level)
                            for alias in node.names if alias.name != '*']
            else:
                continue
            for module, level in modules:
                parent = path.parent
                for _ in range(max(0, level - 1)):
                    parent = parent.parent
                roots = [parent] if level else [path.parent, PurePosixPath('.')]
                for base in roots:
                    target = base.joinpath(*module.split('.')) if module else base
                    candidates = [str(target) + '.py', str(target / '__init__.py')]
                    found = next((c for c in candidates if c in entries), None)
                    if found:
                        pending.append(found)
                        via.setdefault(found, name)
                        break
    return files


def offenders(files, via):
    """Staged files by size, each with the import path that pulled it in."""
    lines = []
    for name in sorted(files, key=lambda n: -len(files[n])):
        chain, cur = [], via.get(name)
        while cur and cur not in chain:
            chain.append(cur)
            cur = via.get(cur)
        lines.append(f'  {PurePosixPath(name).name} {len(base64.b64decode(files[name])) // 1000} KB'
                     + ''.join(' ← ' + PurePosixPath(c).stem for c in chain))
    return '\n'.join(lines)


def compare(repo, ref, diff, base, argv, project, tenant=None, principal_kind=None,
            principal_id=None, paths=(), only=(), mirror=None, stage_all=False, no_base=False):
    repo = repo.expanduser().resolve()
    if not argv or len(argv) < 2 or argv[0] not in ('python', 'python3') or not argv[1].endswith('.py'):
        raise ValueError('Command must be python relative/helper.py [args]; no shell or inline code')
    safe_path(argv[1])
    if bool(principal_kind) != bool(principal_id):
        raise ValueError('Supply both principal flags')
    if only and not diff:
        raise ValueError('--only applies to --diff')
    for path in only:
        safe_path(path)
    base_sha = git(repo, 'rev-parse', '--verify', (base or ('HEAD' if diff else 'origin/main')) + '^{commit}')
    after_sha = git(repo, 'rev-parse', '--verify', (ref or 'HEAD') + '^{commit}')
    changed = git(repo, 'diff', '--name-only', '--no-renames', base_sha, *([] if diff else [after_sha])).splitlines()
    if only:
        changed = [p for p in changed if under(p, only)]
    if not changed:
        raise ValueError('No changed files to compare' + (' under --only ' + ', '.join(only) if only else '')
                         + ': save your edit (new files: git add), or compare a commit with --ref')
    grounding_changes = [p for p in changed if not any(
        part.startswith('.') or part == 'actions' for part in PurePosixPath(p).parts)]
    changed_seeds = [p for p in grounding_changes if '/tests/' not in '/' + p and not p.startswith('tests/')]
    if not stage_all:  # changed code outside the helper's closure is not shipped; data next to it is
        home = str(PurePosixPath(argv[1]).parent)
        changed_seeds = [p for p in changed_seeds if not p.endswith('.py') and (home == '.' or under(p, [home]))]
    seeds = {argv[1], *paths, *changed_seeds}
    via = {}
    before_files = stage(repo, base_sha, seeds, via=via)
    after_files = stage(repo, after_sha, seeds, working=diff, only=only, via=via)
    if argv[1] not in after_files:
        raise ValueError('Helper missing from staged tree')
    live = {}
    if not stage_all:  # ship the helper + changed bytes; unchanged imports come from the live mirror
        trees = {base_sha: tree(repo, base_sha), after_sha: tree(repo, after_sha)}

        def blob(files, sha, n, working=False):  # a closure may not reach n on the other side
            if n in files:
                return files[n]
            if working and (not only or under(n, only)) and (repo / n).is_file():
                return base64.b64encode((repo / n).read_bytes()).decode()
            if n in trees[sha]:
                return base64.b64encode(subprocess.check_output(['git', '-C', str(repo), 'show', f'{sha}:{n}'])).decode()
        same = {n: b for n in sorted(set(before_files) | set(after_files)) if n != argv[1]
                and (b := blob(before_files, base_sha, n)) is not None and b == blob(after_files, after_sha, n, diff)}
        live = dict(mirror=mirror or f'/mirrors/{repo.name}', borrowed={
            n: hashlib.sha256(base64.b64decode(b)).hexdigest() for n, b in same.items()})
        before_files = {n: d for n, d in before_files.items() if n not in same}
        after_files = {n: d for n, d in after_files.items() if n not in same}
    scope = ['--project', project, '--tenant', tenant] if tenant else ['--project', project, '--scope', 'project']
    if principal_kind:
        scope += ['--principal-kind', principal_kind, '--principal-id', principal_id]
    if diff:
        after_sha = 'worktree:' + hashlib.sha256(json.dumps(after_files, sort_keys=True).encode()).hexdigest()
    # Resolve and validate both payloads before either production call.
    name = re.sub('[^A-Za-z0-9_-]', '-', repo.name)
    payload = {}
    for label, files in (() if no_base else (('before', before_files),)) + (('after', after_files),):
        try:
            payload[label] = len(console_command(files, argv, name, **live).encode())
        except ValueError as exc:
            raise ValueError(f'{exc}\n{label} staged {len(files)} file(s), by size ← importer:\n'
                             + offenders(files, via)) from None
    return dict(repo=str(repo), base=base_sha, ref=after_sha, files=len(changed),
                staged_before=sorted(before_files), staged_after=sorted(after_files),
                borrowed=sorted(live.get('borrowed', {})), payload=payload,
                command=reduced(shlex.join(argv)),
                before=None if no_base else run_version(before_files, argv, scope, base_sha, name, **live),
                after=run_version(after_files, argv, scope, after_sha, name, **live))


def side_by_side(result):
    if result['before'] is None:
        value = result['after']
        return '\n'.join(['no base run (new helper)', 'exit: ' + str(value['exit_code']), 'stdout:',
                          *value['stdout'].splitlines(), 'stderr:', *value['stderr'].splitlines()])
    columns = []
    for label in ('before', 'after'):
        value = result[label]
        columns.append([label.upper(), 'exit: ' + str(value['exit_code']), 'stdout:',
                        *value['stdout'].splitlines(), 'stderr:', *value['stderr'].splitlines()])
    width = max(len(line) for line in columns[0])
    return '\n'.join((columns[0][i] if i < len(columns[0]) else '').ljust(width) + ' | ' +
                     (columns[1][i] if i < len(columns[1]) else '')
                     for i in range(max(map(len, columns))))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--ref')
    choice.add_argument('--diff', action='store_true', help='Tracked working-tree changes against HEAD')
    p.add_argument('--base', help='Override base commit (default origin/main; HEAD with --diff)')
    p.add_argument('--project', required=True)
    p.add_argument('--tenant')
    p.add_argument('--principal-kind')
    p.add_argument('--principal-id', '--principal', dest='principal_id')
    p.add_argument('--path', action='append', default=[], help='Additional data/import file')
    p.add_argument('--only', action='append', default=[],
                   help='With --diff: stage working-tree changes only under PATH (repeatable)')
    p.add_argument('--cmd', required=True, help='Quoted python relative/helper.py [args]')
    p.add_argument('--json', action='store_true')
    p.add_argument('--mirror', help='Live mirror path on the box (default /mirrors/<repo dir name>)')
    p.add_argument('--stage-all', action='store_true', help='Ship the whole import closure (old behaviour)')
    p.add_argument('--no-base', action='store_true', help='New helper: skip the base run')
    p.add_argument('--read-only-reviewed', action='store_true', required=True,
                   help='Helper/imports inspected: no sends, actions, or customer writes')
    args = p.parse_args()
    try:
        result = compare(args.repo, args.ref, args.diff, args.base, shlex.split(args.cmd),
                         args.project, args.tenant, args.principal_kind, args.principal_id, args.path, args.only,
                         args.mirror, args.stage_all, args.no_base)
    except ValueError as exc:  # usage/budget/transport: one actionable message, no traceback
        print('mirror-try: ' + str(exc), file=sys.stderr)
        return 2
    print('payload ' + ' · '.join(f'{k} {v // 1000} KB' for k, v in result['payload'].items())
          + f' of 120 KB; staged {len(result["staged_after"])}, borrowed {len(result["borrowed"])} from the live mirror',
          file=sys.stderr)
    print(json.dumps(result, indent=2) if args.json else side_by_side(result))
    return 0 if result['after']['exit_code'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
