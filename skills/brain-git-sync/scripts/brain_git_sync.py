#!/usr/bin/env python3
"""Safely reconcile a brain repository's local main with origin/main.

The caller is responsible for deciding which local files are intended work and
staging only those files. This program owns the fragile synchronization steps:
fetch, merge, bounded non-force push retries, and final ancestry verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import NoReturn, Sequence


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"


EXIT_BLOCKED = 2
EXIT_CONFLICT = 3
TRANSACTION_VERSION = 1
TRANSACTION_FILE = "brain-git-sync-state.json"


class SyncError(RuntimeError):
    """A safe, actionable synchronization failure."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "blocked",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


@dataclass
class Inventory:
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class Topology:
    local_sha: str
    remote_sha: str
    ahead: int
    behind: int

    @property
    def state(self) -> str:
        if self.ahead and self.behind:
            return "diverged"
        if self.ahead:
            return "ahead"
        if self.behind:
            return "behind"
        return "equal"


@dataclass
class SyncResult:
    status: str
    final_sha: str
    initial_local_sha: str
    initial_remote_sha: str
    initial_topology: dict[str, object]
    inventory: dict[str, list[str]]
    local_commits: list[str]
    remote_commits: list[str]
    committed_files: list[str]
    observed_remote_shas: list[str]
    push_attempts: int
    ancestry_verified: bool


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_MERGE_AUTOEDIT": "no",
            "LC_ALL": "C",
        }

    def run(
        self,
        *args: str,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        try:
            process = subprocess.run(
                ["git", *args],
                cwd=self.repo,
                env=self.env,
                capture_output=True,
                text=text,
                check=False,
            )
        except OSError as error:
            raise SyncError(f"cannot run git in {self.repo}: {error}") from error
        if check and process.returncode:
            stderr = _output(process.stderr).strip()
            stdout = _output(process.stdout).strip()
            detail = stderr or stdout or f"exit {process.returncode}"
            raise SyncError(f"git {' '.join(args)} failed: {detail}")
        return process

    def out(self, *args: str) -> str:
        return _output(self.run(*args).stdout).strip()

    def git_path(self, name: str) -> Path:
        path = Path(self.out("rev-parse", "--git-path", name))
        return path if path.is_absolute() else self.repo / path


class Reporter:
    def __init__(self, json_output: bool) -> None:
        self.json_output = json_output

    def note(self, message: str) -> None:
        if not self.json_output:
            print(message, file=sys.stderr)


def _output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else value


def _split_nul(value: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in value.split(b"\0") if item]


def inventory(git: Git) -> Inventory:
    raw = git.run(
        "status", "--porcelain=v1", "-z", "--untracked-files=all", text=False
    ).stdout
    assert isinstance(raw, bytes)
    records = raw.split(b"\0")
    result = Inventory()
    index = 0
    conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        decoded = record.decode("utf-8", "surrogateescape")
        if len(decoded) < 4:
            raise SyncError(f"cannot parse git status record: {decoded!r}")
        code, path = decoded[:2], decoded[3:]
        if code[0] in {"R", "C"} and index < len(records):
            old_path = records[index].decode("utf-8", "surrogateescape")
            index += 1
            path = f"{old_path} -> {path}"
        if code == "??":
            result.untracked.append(path)
            continue
        if code in conflict_codes:
            result.conflicts.append(path)
            continue
        if code[0] != " ":
            result.staged.append(path)
        if code[1] != " ":
            result.unstaged.append(path)
    return result


def _ref_exists(git: Git, ref: str) -> bool:
    return git.run("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def _in_progress(git: Git, name: str) -> bool:
    return git.git_path(name).exists()


def _selected_project(repo: Path, explicit: str) -> str:
    """Explicit --project wins; otherwise best-effort from the checkout's committed marker
    (same pattern as sent_delta_report.py's local_scope)."""
    if explicit:
        return explicit
    marker = _marker(repo)
    if not marker.is_file():
        return ""
    try:
        data = tomllib.loads(marker.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str(data.get("project", ""))


def _brain_source(repo: Path, project: str, reporter: Reporter) -> dict[str, object] | None:
    """Best-effort `status.brain_source` from `rp dev brain status -o json`. Returns None (never
    blocking) when `rp` is unavailable, fails, or the field is absent — a server that predates
    the read-only brain alias, or a project that owns its own brain, both read as no alias."""
    cmd = [_cli(), "dev", "brain", "status", "-o", "json", "--raw-output"]
    if project:
        cmd = [cmd[0], "--project", project, *cmd[1:]]
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        reporter.note(f"brain-alias check skipped: cannot run rp dev brain status: {error}")
        return None
    if proc.returncode != 0:
        reporter.note(
            "brain-alias check skipped: rp dev brain status failed: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
        )
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        reporter.note("brain-alias check skipped: rp dev brain status returned non-JSON output")
        return None
    status = data.get("status") if isinstance(data, dict) else None
    source = status.get("brain_source") if isinstance(status, dict) else None
    return source if isinstance(source, dict) else None


def _refuse_if_read_only_alias(repo: Path, project: str, reporter: Reporter) -> None:
    """A read-only brain alias reads its source project's brain and must never write it: refuse
    before any fetch/commit/push, mirroring the laptop-side source/alias contract
    (BRAIN_READ_ONLY_ALIAS on the server side)."""
    source = _brain_source(repo, project, reporter)
    if not source or not source.get("read_only"):
        return
    owner = source.get("project") or source.get("project_id") or "its source project"
    label = project or "this project"
    raise SyncError(
        f"{label} reads {owner}'s brain (read-only brain alias); sync from the source checkout",
        details={"brain_source": source},
    )


def validate_repository(git: Git, *, allow_detached: bool = False) -> None:
    inside = git.run("rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode or _output(inside.stdout).strip() != "true":
        raise SyncError(f"not a Git worktree: {git.repo}")
    branch_result = git.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = _output(branch_result.stdout).strip()
    if branch != "main" and not (allow_detached and not branch):
        raise SyncError(f"current branch must be main (found {branch or 'detached HEAD'})")
    if "origin" not in git.out("remote").splitlines():
        raise SyncError("Git remote 'origin' is required")
    if not git.out("remote", "get-url", "origin"):
        raise SyncError("Git remote 'origin' has no URL")
    forbidden = [
        name
        for name in ("CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
        if _in_progress(git, name)
    ]
    forbidden.extend(
        name for name in ("rebase-apply", "rebase-merge") if _in_progress(git, name)
    )
    if forbidden:
        raise SyncError(f"finish the existing Git operation first: {', '.join(forbidden)}")


def _fetch(git: Git) -> str:
    git.run("fetch", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main")
    if not _ref_exists(git, "refs/remotes/origin/main"):
        raise SyncError("origin/main does not exist")
    return git.out("rev-parse", "refs/remotes/origin/main")


def topology(git: Git, remote_sha: str) -> Topology:
    local_sha = git.out("rev-parse", "HEAD")
    counts = git.out("rev-list", "--left-right", "--count", f"{local_sha}...{remote_sha}")
    try:
        ahead, behind = (int(part) for part in counts.split())
    except (ValueError, TypeError) as error:
        raise SyncError(f"cannot determine ahead/behind state: {counts!r}") from error
    return Topology(local_sha, remote_sha, ahead, behind)


def _is_ancestor(git: Git, ancestor: str, descendant: str) -> bool:
    return (
        git.run("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode
        == 0
    )


def _commit_list(git: Git, exclude: str, include: str) -> list[str]:
    process = git.run("rev-list", "--reverse", f"{exclude}..{include}", check=False)
    if process.returncode:
        return []
    return _output(process.stdout).splitlines()


def _staged_paths(git: Git) -> list[str]:
    raw = git.run("diff", "--cached", "--name-only", "-z", text=False).stdout
    assert isinstance(raw, bytes)
    return _split_nul(raw)


def _unsafe_staged_paths(paths: Sequence[str]) -> list[str]:
    unsafe: list[str] = []
    cache_parts = {"__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
    secret_names = {
        ".env",
        "accounts.yml",
        "accounts.yaml",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
    allowed_env_suffixes = (".example", ".sample", ".template")
    for raw_path in paths:
        # Rename display paths do not occur in `git diff --name-only`; keep this
        # check path-oriented so it remains package neutral.
        path = Path(raw_path)
        lower_name = path.name.lower()
        is_env_secret = lower_name.startswith(".env") and not lower_name.endswith(
            allowed_env_suffixes
        )
        if (
            lower_name in secret_names
            or is_env_secret
            or lower_name.endswith((".pem", ".key", ".p12", ".pfx"))
            or any(part in cache_parts for part in path.parts)
        ):
            unsafe.append(raw_path)
    return unsafe


def _assert_prepared_work(
    current: Inventory,
    *,
    merge_in_progress: bool,
    commit_message: str | None,
) -> None:
    if current.untracked:
        raise SyncError(
            "untracked files require an agent decision before sync: "
            + ", ".join(current.untracked),
            details={"inventory": asdict(current)},
        )
    if current.unstaged:
        raise SyncError(
            "unstaged tracked files require an agent decision before sync: "
            + ", ".join(current.unstaged),
            details={"inventory": asdict(current)},
        )
    if current.conflicts:
        kind = "conflict" if merge_in_progress else "blocked"
        raise SyncError(
            "resolve and stage every merge conflict, then rerun: "
            + ", ".join(current.conflicts),
            kind=kind,
            details={"inventory": asdict(current)},
        )
    unsafe = _unsafe_staged_paths(current.staged)
    if unsafe:
        raise SyncError(
            "refusing staged secret/cache-like paths; unstage and inspect: "
            + ", ".join(unsafe),
            details={"inventory": asdict(current)},
        )
    if current.staged and not merge_in_progress and not commit_message:
        raise SyncError(
            "staged changes require an explicit --commit-message",
            details={"inventory": asdict(current)},
        )


def _format_shas(shas: Sequence[str]) -> str:
    if not shas:
        return "none"
    shown = [sha[:12] for sha in shas[:8]]
    if len(shas) > len(shown):
        shown.append(f"+{len(shas) - len(shown)} more")
    return ", ".join(shown)


def _append_unique(items: list[str], values: Sequence[str]) -> None:
    for value in values:
        if value and value not in items:
            items.append(value)


def _transaction_path(git: Git) -> Path:
    return git.git_path(TRANSACTION_FILE)


def _protection_prefix(git: Git) -> str:
    identity = hashlib.sha256(str(_transaction_path(git).resolve()).encode()).hexdigest()[:16]
    return f"refs/brain-git-sync/{identity}"


def _protect_remote_tip(git: Git, sha: str) -> None:
    git.run("update-ref", f"{_protection_prefix(git)}/{sha}", sha)


def _load_transaction(git: Git) -> dict[str, object] | None:
    path = _transaction_path(git)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SyncError(f"cannot read sync recovery state {path}: {error}") from error
    if not isinstance(data, dict) or data.get("version") != TRANSACTION_VERSION:
        raise SyncError(f"unsupported sync recovery state in {path}; inspect it before retrying")
    return data


def _save_transaction(git: Git, transaction: dict[str, object]) -> None:
    path = _transaction_path(git)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(transaction, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SyncError(f"cannot save sync recovery state {path}: {error}") from error


def _clear_transaction(git: Git) -> None:
    prefix = _protection_prefix(git)
    try:
        refs = git.out("for-each-ref", "--format=%(refname)", prefix).splitlines()
        for ref in refs:
            git.run("update-ref", "-d", ref)
        _transaction_path(git).unlink(missing_ok=True)
    except (OSError, SyncError) as error:
        raise SyncError(f"sync succeeded but recovery state could not be cleared: {error}") from error


def _verify_env() -> dict[str, str]:
    """Verify commands must not inherit the ephemeral venv this script may itself run under
    (`uv run --no-project python brain_git_sync.py`): a bare `python3` in a verify command would
    otherwise resolve to that dependency-less interpreter and fail on imports like PyYAML."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    venv = env.pop("VIRTUAL_ENV", None)
    drop = {os.path.join(venv, "bin")} if venv else set()
    # `uv run --no-project` sets no VIRTUAL_ENV but prepends its managed interpreter's bin dir
    # (…/uv/python/cpython-3.x…/bin) — a bare `python3` there has no third-party packages.
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep)
        if p and p not in drop and f"{os.sep}uv{os.sep}python{os.sep}" not in p
    )
    return env


def _verify(repo: Path, commands: Sequence[str], reporter: Reporter) -> None:
    for command in commands:
        reporter.note(f"Verify merged tree: {command}")
        process = subprocess.run(
            command,
            cwd=repo,
            env=_verify_env(),
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode:
            output = (process.stderr.strip() or process.stdout.strip() or "no output")[-2000:]
            raise SyncError(
                f"verification failed after merge ({command!r}, exit {process.returncode}): "
                f"{output}"
            )


HYGIENE_DIR = Path(__file__).resolve().parents[2] / "local-brain-work" / "scripts"


def _knowledge_brain(repo: Path) -> bool:
    """A knowledge-only brain (no skills/ or actions/, e.g. a help-centre corpus): `.replypen.toml`
    declares `project =` and runs write its `journal/`. Source repos carry `project =` only to scope
    `rp` and have no journal, so they stay out."""
    import tomllib

    try:
        cfg = tomllib.loads(_marker(repo).read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return bool(cfg.get("project")) and not cfg.get("tenant") and (repo / "journal").is_dir()


def _hygiene_gate(repo: Path, base: str, reporter: Reporter) -> None:
    """Built-in pre-push gate (brain_hygiene.py) for every brain checkout; no --verify-command
    needed. Skipped for the kit itself and for repos that are not brains."""
    is_kit = (repo / "runtime" / "lib").is_dir() and (repo / "plugin.json").is_file()
    if is_kit or not ((repo / "skills").is_dir() or (repo / "actions").is_dir() or _knowledge_brain(repo)):
        return
    if not (HYGIENE_DIR / "brain_hygiene.py").is_file():
        raise SyncError(f"brain hygiene gate missing at {HYGIENE_DIR}; reinstall the kit "
                        "(brain-dev-upgrade)")
    sys.path.insert(0, str(HYGIENE_DIR))
    import brain_hygiene

    reporter.note("Verify merged tree: brain hygiene gate")
    findings = brain_hygiene.check(repo, brain_hygiene.bs.git_changed_paths(repo, base), base)
    blocking = [f.render() for f in findings if f.severity != "NOTICE"]
    for notice in (f.render() for f in findings if f.severity == "NOTICE"):
        reporter.note(notice)
    if blocking:
        raise SyncError("brain hygiene gate failed:\n" + "\n".join(blocking[:50]),
                        details={"hygiene": blocking})


def _merge(git: Git, target: str) -> bool:
    """Merge target if needed, returning whether HEAD changed."""
    head = git.out("rev-parse", "HEAD")
    if _is_ancestor(git, target, head):
        return False
    if _is_ancestor(git, head, target):
        git.run("merge", "--ff-only", target)
        return True
    merge = git.run("merge", "--no-edit", target, check=False)
    if merge.returncode:
        conflicts = inventory(git).conflicts
        if conflicts:
            raise SyncError(
                "merge conflicts preserved; resolve and stage them, then rerun: "
                + ", ".join(conflicts),
                kind="conflict",
                details={"inventory": asdict(inventory(git))},
            )
        detail = _output(merge.stderr).strip() or _output(merge.stdout).strip()
        raise SyncError(f"merge failed without file conflicts: {detail}")
    return True


def _synchronize(
    repo: Path,
    *,
    commit_message: str | None = None,
    max_push_attempts: int = 4,
    verify_commands: Sequence[str] = (),
    project: str = "",
    reporter: Reporter | None = None,
    allow_detached: bool = False,
    error_context: dict[str, object],
) -> SyncResult:
    reporter = reporter or Reporter(False)
    if max_push_attempts < 1:
        raise SyncError("--max-push-attempts must be at least 1")
    resolved_repo = repo.resolve()
    if not resolved_repo.is_dir():
        raise SyncError(f"repository path is not a directory: {resolved_repo}")
    git = Git(resolved_repo)
    validate_repository(git, allow_detached=allow_detached)
    _refuse_if_read_only_alias(resolved_repo, _selected_project(resolved_repo, project), reporter)

    current_inventory = inventory(git)
    reporter.note(
        "Inventory: "
        f"staged={len(current_inventory.staged)} "
        f"unstaged={len(current_inventory.unstaged)} "
        f"untracked={len(current_inventory.untracked)} "
        f"conflicts={len(current_inventory.conflicts)}"
    )
    merge_in_progress = _in_progress(git, "MERGE_HEAD")
    pending_merge_sha = git.out("rev-parse", "MERGE_HEAD") if merge_in_progress else None
    current_local_sha = git.out("rev-parse", "HEAD")

    # Capture the locally known tracking tip before fetch is allowed to replace
    # it. A remote force-update must not silently erase this computer's last
    # known origin/main history.
    known_remote_sha = (
        git.out("rev-parse", "refs/remotes/origin/main")
        if _ref_exists(git, "refs/remotes/origin/main")
        else None
    )

    # Inventory and the known tracking tip precede the first mutation.
    fetched_remote_sha = _fetch(git)
    current_topology = topology(git, fetched_remote_sha)
    reporter.note(
        f"main {current_topology.local_sha[:12]} / origin/main "
        f"{current_topology.remote_sha[:12]}: {current_topology.state} "
        f"(ahead {current_topology.ahead}, behind {current_topology.behind})"
    )

    transaction = _load_transaction(git)
    if transaction is None:
        initial_remote_sha = pending_merge_sha or fetched_remote_sha
        initial_topology = {**asdict(current_topology), "state": current_topology.state}
        transaction = {
            "version": TRANSACTION_VERSION,
            "initial_inventory": asdict(current_inventory),
            "initial_local_sha": current_local_sha,
            "initial_remote_sha": initial_remote_sha,
            "initial_topology": initial_topology,
            "prepared_local_sha": current_local_sha,
            "local_commits": _commit_list(git, initial_remote_sha, current_local_sha),
            "remote_commits": [],
            "committed_files": [],
            "observed_remote_shas": [],
            "push_attempts": 0,
            "verify_commands": list(verify_commands),
            "verified_head": None,
            "verified_commands": [],
        }
        remote_commits = transaction["remote_commits"]
        observed = transaction["observed_remote_shas"]
        assert isinstance(remote_commits, list) and isinstance(observed, list)
        for remote_tip in (known_remote_sha, pending_merge_sha, fetched_remote_sha):
            if remote_tip:
                _append_unique(observed, [remote_tip])
                _append_unique(
                    remote_commits,
                    _commit_list(git, current_local_sha, remote_tip),
                )
    else:
        if verify_commands:
            transaction["verify_commands"] = list(verify_commands)
            transaction["verified_head"] = None
            transaction["verified_commands"] = []
        observed = transaction.get("observed_remote_shas")
        remote_commits = transaction.get("remote_commits")
        if not isinstance(observed, list) or not isinstance(remote_commits, list):
            raise SyncError("sync recovery state is missing commit accounting; inspect it")
        for remote_tip in (known_remote_sha, pending_merge_sha, fetched_remote_sha):
            if remote_tip:
                _append_unique(observed, [remote_tip])
        prepared_for_classification = transaction.get("prepared_local_sha")
        if not isinstance(prepared_for_classification, str):
            raise SyncError("sync recovery state is missing prepared_local_sha")
        if current_topology.state != "equal":
            for remote_tip in (known_remote_sha, pending_merge_sha, fetched_remote_sha):
                if remote_tip:
                    _append_unique(
                        remote_commits,
                        _commit_list(git, prepared_for_classification, remote_tip),
                    )

    for protected_remote_sha in observed:
        _protect_remote_tip(git, protected_remote_sha)
    _save_transaction(git, transaction)

    initial_inventory_data = transaction.get("initial_inventory")
    initial_local_sha = transaction.get("initial_local_sha")
    initial_remote_sha = transaction.get("initial_remote_sha")
    initial_topology = transaction.get("initial_topology")
    if not (
        isinstance(initial_inventory_data, dict)
        and isinstance(initial_local_sha, str)
        and isinstance(initial_remote_sha, str)
        and isinstance(initial_topology, dict)
    ):
        raise SyncError("sync recovery state lacks initial inventory/topology evidence")
    error_context.update(
        {
            "initial_inventory": initial_inventory_data,
            "initial_local_sha": initial_local_sha,
            "initial_remote_sha": initial_remote_sha,
            "initial_topology": initial_topology,
        }
    )

    try:
        _assert_prepared_work(
            current_inventory,
            merge_in_progress=merge_in_progress,
            commit_message=commit_message,
        )
    except SyncError as error:
        raise

    committed_files = transaction.get("committed_files")
    local_commits = transaction.get("local_commits")
    observed_remote_shas = transaction.get("observed_remote_shas")
    if not (
        isinstance(committed_files, list)
        and isinstance(local_commits, list)
        and isinstance(observed_remote_shas, list)
    ):
        raise SyncError("sync recovery state has invalid result accounting")

    if merge_in_progress:
        _append_unique(committed_files, _staged_paths(git))
        git.run("commit", "--no-edit")
        reporter.note(f"Resumed merge of {(pending_merge_sha or initial_remote_sha)[:12]}")
        transaction["verified_head"] = None
    elif current_inventory.staged:
        _append_unique(committed_files, _staged_paths(git))
        git.run("commit", "-m", commit_message or "")
        committed_sha = git.out("rev-parse", "HEAD")
        _append_unique(local_commits, [committed_sha])
        transaction["prepared_local_sha"] = committed_sha
        transaction["verified_head"] = None
        reporter.note(f"Committed prepared index: {committed_sha[:12]}")
    _save_transaction(git, transaction)

    prepared_local_sha = transaction.get("prepared_local_sha")
    if not isinstance(prepared_local_sha, str):
        raise SyncError("sync recovery state is missing prepared local SHA")
    configured_verifiers = transaction.get("verify_commands")
    if not isinstance(configured_verifiers, list) or not all(
        isinstance(command, str) for command in configured_verifiers
    ):
        raise SyncError("sync recovery state has invalid verification commands")

    invocation_push_attempts = 0
    last_push_error = ""

    while True:
        pre_fetch_remote_sha = (
            git.out("rev-parse", "refs/remotes/origin/main")
            if _ref_exists(git, "refs/remotes/origin/main")
            else None
        )
        if pre_fetch_remote_sha:
            _append_unique(observed_remote_shas, [pre_fetch_remote_sha])
            _protect_remote_tip(git, pre_fetch_remote_sha)
        remote_sha = _fetch(git)
        _append_unique(observed_remote_shas, [remote_sha])
        _protect_remote_tip(git, remote_sha)
        _save_transaction(git, transaction)

        current = inventory(git)
        if any((current.staged, current.unstaged, current.untracked, current.conflicts)):
            raise SyncError(
                "worktree changed unexpectedly during sync; inspect before retrying: "
                + json.dumps(asdict(current), sort_keys=True),
                details={"inventory": asdict(current)},
            )

        state = topology(git, remote_sha)
        reporter.note(
            f"Reconcile {state.state}: local {state.local_sha[:12]}, "
            f"remote {state.remote_sha[:12]}"
        )
        if state.state != "equal":
            for remote_tip in (pre_fetch_remote_sha, remote_sha):
                if remote_tip:
                    _append_unique(
                        remote_commits,
                        _commit_list(git, prepared_local_sha, remote_tip),
                    )
            _save_transaction(git, transaction)
        if state.state in {"behind", "diverged"}:
            if _merge(git, "refs/remotes/origin/main"):
                transaction["verified_head"] = None
                _save_transaction(git, transaction)

        # Preserve every remote tip observed during this invocation, including
        # a locally known tip replaced by a non-fast-forward remote update.
        for protected_remote_sha in observed_remote_shas:
            if _merge(git, protected_remote_sha):
                transaction["verified_head"] = None
                _save_transaction(git, transaction)

        post_merge = inventory(git)
        if any(
            (post_merge.staged, post_merge.unstaged, post_merge.untracked, post_merge.conflicts)
        ):
            raise SyncError(
                "merge did not leave a clean worktree: "
                + json.dumps(asdict(post_merge), sort_keys=True)
            )

        current_head = git.out("rev-parse", "HEAD")
        verified_commands = transaction.get("verified_commands")
        already_verified = (
            transaction.get("verified_head") == current_head
            and verified_commands == configured_verifiers
        )
        if not already_verified:
            try:
                _hygiene_gate(git.repo, remote_sha, reporter)
                _verify(git.repo, configured_verifiers, reporter)
            except SyncError as error:
                raise SyncError(
                    f"{error}. Local commits/merges remain recorded; fix verification and rerun.",
                    details=error.details,
                ) from error
            transaction["verified_head"] = current_head
            transaction["verified_commands"] = list(configured_verifiers)
            _save_transaction(git, transaction)
        after_verify = inventory(git)
        if any(
            (
                after_verify.staged,
                after_verify.unstaged,
                after_verify.untracked,
                after_verify.conflicts,
            )
        ):
            raise SyncError(
                "verification changed the worktree; local commits/merges remain recorded. "
                "Inspect or clean generated files, then rerun: "
                + json.dumps(asdict(after_verify), sort_keys=True),
                details={"inventory": asdict(after_verify)},
            )

        # A freshly fetched equal candidate is still re-verified above.
        if current_head == remote_sha:
            final_sha = current_head
            break

        protected_shas = [initial_local_sha, initial_remote_sha, *observed_remote_shas]
        missing_before_push = [
            sha
            for sha in dict.fromkeys(protected_shas)
            if not _is_ancestor(git, sha, current_head)
        ]
        if missing_before_push:
            raise SyncError(
                "pre-push ancestry proof failed; refusing to publish: "
                + ", ".join(missing_before_push)
            )
        if invocation_push_attempts >= max_push_attempts:
            raise SyncError(
                f"push did not succeed after {max_push_attempts} safe attempts; "
                f"last error: {last_push_error or 'origin/main kept moving'}. "
                "Local commits/merges remain recorded; fix the blocker and rerun."
            )
        invocation_push_attempts += 1
        total_push_attempts = transaction.get("push_attempts")
        if not isinstance(total_push_attempts, int):
            raise SyncError("sync recovery state has invalid push attempt accounting")
        transaction["push_attempts"] = total_push_attempts + 1
        _save_transaction(git, transaction)
        push = git.run(
            "-c",
            "push.followTags=false",
            "push",
            "origin",
            "HEAD:refs/heads/main",
            check=False,
        )
        if push.returncode:
            last_push_error = (
                _output(push.stderr).strip() or _output(push.stdout).strip() or "unknown error"
            )[-2000:]
            reporter.note(
                f"Push attempt {invocation_push_attempts} raced or failed; refetching before retry"
            )
        else:
            reporter.note(
                f"Push attempt {invocation_push_attempts} succeeded; verifying fresh remote"
            )

    final_inventory = inventory(git)
    if any(
        (
            final_inventory.staged,
            final_inventory.unstaged,
            final_inventory.untracked,
            final_inventory.conflicts,
        )
    ):
        raise SyncError(
            "final worktree is not clean: " + json.dumps(asdict(final_inventory), sort_keys=True)
        )
    proof_shas = [initial_local_sha, initial_remote_sha, prepared_local_sha, *observed_remote_shas]
    missing = [sha for sha in dict.fromkeys(proof_shas) if not _is_ancestor(git, sha, final_sha)]
    if missing:
        raise SyncError(
            "ancestry proof failed; refusing success because commits would be lost: "
            + ", ".join(missing)
        )
    if git.out("rev-parse", "HEAD") != git.out("rev-parse", "refs/remotes/origin/main"):
        raise SyncError("final local main and freshly fetched origin/main differ")

    result = SyncResult(
        status="ok",
        final_sha=final_sha,
        initial_local_sha=initial_local_sha,
        initial_remote_sha=initial_remote_sha,
        initial_topology=initial_topology,
        inventory=initial_inventory_data,
        local_commits=local_commits,
        remote_commits=remote_commits,
        committed_files=sorted(dict.fromkeys(committed_files)),
        observed_remote_shas=observed_remote_shas,
        push_attempts=int(transaction["push_attempts"]),
        ancestry_verified=True,
    )
    _clear_transaction(git)
    reporter.note(
        f"Synced {final_sha[:12]}; local commits: {_format_shas(local_commits)}; "
        f"remote commits: {_format_shas(remote_commits)}; "
        f"committed files: {', '.join(result.committed_files) or 'none'}"
    )
    return result


def synchronize(
    repo: Path,
    *,
    commit_message: str | None = None,
    max_push_attempts: int = 4,
    verify_commands: Sequence[str] = (),
    project: str = "",
    reporter: Reporter | None = None,
    allow_detached: bool = False,
) -> SyncResult:
    """Run a sync and retain all available pre-mutation evidence on failure."""
    error_context: dict[str, object] = {}
    try:
        return _synchronize(
            repo,
            commit_message=commit_message,
            max_push_attempts=max_push_attempts,
            verify_commands=verify_commands,
            project=project,
            reporter=reporter,
            allow_detached=allow_detached,
            error_context=error_context,
        )
    except SyncError as error:
        for key, value in error_context.items():
            error.details.setdefault(key, value)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely merge local main and origin/main, push without force, and prove no "
            "pre-sync commits were lost. Stage intended work before running."
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Git worktree to synchronize (default: current directory)",
    )
    parser.add_argument(
        "--commit-message",
        help="commit the already-prepared index with this explicit message",
    )
    parser.add_argument(
        "--max-push-attempts",
        type=int,
        default=4,
        help="bounded retries when origin/main moves (default: 4)",
    )
    parser.add_argument(
        "--verify-command",
        action="append",
        default=[],
        metavar="CMD",
        help="shell command to run after each merge and before push (repeatable)",
    )
    parser.add_argument(
        "--project",
        default="",
        help=(
            "project slug for the read-only brain alias check (rp dev brain status); "
            "defaults to the checkout's .replypen.toml"
        ),
    )
    parser.add_argument(
        "--detached-head",
        action="store_true",
        help=(
            "accept a detached HEAD (a throwaway worktree, e.g. the kit release); "
            "HEAD still publishes to origin/main"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="emit one machine-readable result object"
    )
    return parser


def _fail(error: SyncError, *, json_output: bool) -> NoReturn:
    payload = {"status": error.kind, "error": str(error), **error.details}
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{error.kind}: {error}", file=sys.stderr)
    raise SystemExit(EXIT_CONFLICT if error.kind == "conflict" else EXIT_BLOCKED)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = synchronize(
            args.repo,
            commit_message=args.commit_message,
            max_push_attempts=args.max_push_attempts,
            verify_commands=args.verify_command,
            project=args.project,
            reporter=Reporter(args.json),
            allow_detached=args.detached_head,
        )
    except SyncError as error:
        _fail(error, json_output=args.json)
    payload = asdict(result)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"final={result.final_sha} local={len(result.local_commits)} "
            f"remote={len(result.remote_commits)} files={len(result.committed_files)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
