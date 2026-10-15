# /// script
# requires-python = ">=3.11"
# ///
"""Check that the absolute container paths a brain cites (`/brain/…`, `/tenant/…`, `/mirrors/<repo>/…`,
`/kb/…`, `/skills/…`) resolve in a production run. A stale path wastes agent turns or misleads the model.

Local first, production only for the rest:
  * a mount this checkout IS (`/brain` in a brain, `/tenant` in an overlay, `/mirrors/<self>` in a
    mirror) → the working tree, so new unpublished files pass; gitignored and run-hidden (root
    `.rcignore`/`.replypenignore`, host-hidden names like `.env`) files count as missing; in-repo
    symlinks resolve like the host's run view. Authoritative: WARN.
  * a mount a sibling checkout serves (`.rootcause.toml [mirrors]`, an overlay's project brain, a
    mirror's consumer brains) → that checkout; a miss there is re-asked in production (checkouts lag).
  * everything else (`/kb`, `/tenant` from a project brain, undeclared mirrors) → one batched
    `rc dev console paths` call per tenant. Missing on every tenant → WARN; on some → NOTICE.
  * `/skills` is scenario/feature-gated and not mounted in the console → listed as unverified.
Placeholders (`<x>`, `{x}`, `$X`, globs, `...`) and bare mount roots are skipped; `journal/`, `tests/`,
run-hidden and tooling dirs are not scanned. Never FAILs (exit 1 only with --strict and a WARN).

    uv run --no-project python brain_paths.py               # changed files vs origin/main
    uv run --no-project python brain_paths.py --all         # every scanned file
    uv run --no-project python brain_paths.py --local       # no rc call (brain_hygiene runs this)
    uv run --no-project python brain_paths.py --all --tenants yes_events,lbv   # or --tenants all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_structure as bs  # noqa: E402

MOUNTS = ("brain", "tenant", "mirrors", "kb", "skills")
PATH_RE = re.compile(r"(?<![\w.~/$>})\]-])/(?:" + "|".join(MOUNTS) + r")(?:/[^\s`\"'()\[\],;|]*)?")
PLACEHOLDER_RE = re.compile(r"[<>{}$*?\\…%]|\.\.\.|/\.\.?(?:/|$)|/\.git(?:/|$)|^/mirrors/[A-Z_]+(?:/|$)")
# A citation that says the target may be absent ("…/cases/ when present") is a NOTICE, never a WARN.
CONDITIONAL_RE = re.compile(r"\b(if|when) (present|it exists|available|any)\b|\boptional\b|\bindien\b|"
                            r"\bals (die|het) bestaat\b", re.I)
SCAN_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".sh", ".json", ".txt", ".toml", ".rb", ".sql"}
SKIP_DIRS = ("journal/", "tests/", "_internal/", ".agents/", ".claude/", ".github/")


@dataclass
class Ref:
    path: str
    sites: list[tuple[str, int]] = field(default_factory=list)  # (file, line)
    conditional: bool = False  # every citing line hedges ("when present")
    verdict: str = ""        # ok | missing | partial | unverified
    detail: str = ""


def normalize(raw: str) -> str | None:
    """The container path a citation means, or None for placeholders/bare roots."""
    p = raw.split("#", 1)[0]
    p = p.rstrip(".:!*_~")                         # sentence end, **bold**
    p = re.sub(r":(?:\d+(?:[:-]\d+)?|[A-Za-z_]\w*)$", "", p).rstrip("/")  # :12, :10-18, :method
    if PLACEHOLDER_RE.search(p) or p.count("/") < 2:
        return None
    return p


def extract(root: Path, files: list[str]) -> dict[str, Ref]:
    refs: dict[str, Ref] = {}
    hedged: dict[str, bool] = {}
    for rel in files:
        try:
            text = (root / rel).read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in PATH_RE.finditer(line):
                if (p := normalize(m.group(0))) is not None and p not in HOST_GENERATED:
                    refs.setdefault(p, Ref(p)).sites.append((rel, lineno))
                    hedged[p] = hedged.get(p, True) and bool(CONDITIONAL_RE.search(line))
    for p, ref in refs.items():
        ref.conditional = hedged[p]
    return refs


# ── what each local checkout ships into a run ──────────────────────────────────────────────────────
def _git_lines(root: Path, *args: str) -> list[str]:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return [p for p in proc.stdout.split("\0") if p] if proc.returncode == 0 else []


# rootcause internal/treeview visible.go + bootstrap.go: names every run view drops, whatever the rules say.
HOST_HIDDEN_NAMES = {".git", ".gitignore", ".replypenignore", "__pycache__", "node_modules", ".venv",
                     "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
DOTENV_TEMPLATES = (".sample", ".example", ".template", ".dist", ".defaults")
# Written into /brain by the host at run time (assembler run context, projection profile): never
# committed, always present when it applies.
HOST_GENERATED = {"/brain/run_context.json", "/brain/tenant_profile.json"}


def _host_hidden(rel: str) -> bool:
    if rel == ".rcignore":
        return True
    return any(part in HOST_HIDDEN_NAMES or ((part == ".env" or part.startswith(".env."))
                                             and not part.endswith(DOTENV_TEMPLATES))
               for part in rel.split("/"))


class Checkout:
    """What one local checkout ships into a run view: tracked + untracked-not-gitignored files, minus
    the root `.replypenignore`/`.rcignore` rules and the host's always-hidden names; in-repo symlinks
    resolve to a visible target (the host drops an alias whose target is hidden)."""

    def __init__(self, root: Path):
        self.root = root
        self._files: set[str] | None = None
        self._dirs: set[str] = set()

    def files(self) -> set[str]:
        if self._files is None:
            shipped = set(_git_lines(self.root, "ls-files", "-z", "-c", "-o", "--exclude-standard"))
            hidden: set[str] = set()
            for control in (".replypenignore", ".rcignore"):
                if (self.root / control).is_file():
                    hidden.update(_git_lines(self.root, "ls-files", "-z", "-c", "-o", "-i",
                                             f"--exclude-from={self.root / control}"))
            self._files = {p for p in shipped - hidden
                           if not _host_hidden(p) and os.path.lexists(self.root / p)}
            for p in self._files:
                parts = p.split("/")
                self._dirs.update("/".join(parts[:k]) for k in range(1, len(parts)))
        return self._files

    def has(self, rel: str) -> bool:
        files = self.files()
        parts = rel.split("/") if rel else []
        for i in range(len(parts)):
            head = "/".join(parts[: i + 1])
            if not (self.root / head).is_symlink():
                continue
            if head not in files:                     # the link entry itself is hidden/untracked
                return False
            real = (self.root / head).resolve()
            try:
                target = real.relative_to(self.root.resolve()).as_posix()
            except ValueError:                        # escaping link: the host refuses it
                return False
            if not real.exists() or target == head:
                return False
            return self.has("/".join([target, *parts[i + 1:]]))
        return rel == "" or rel in files or rel in self._dirs

    def why_not(self, rel: str) -> str:
        """Why `has(rel)` is False, in the words a doc fixer needs."""
        if (self.root / rel).exists():
            return f"{self.root.name}: on disk but gitignored or run-hidden (.rcignore/.replypenignore/host rules)"
        return f"{self.root.name}: absent"


def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def _siblings(root: Path) -> list[Path]:
    """Checkouts beside this one (`../x`, `../../org/x`), the layout brain mirrors already assume."""
    if root.parent == root.parent.parent:
        return []
    return sorted({c.parent.resolve() for c in [*root.parent.glob("*/.rootcause.toml"),
                                                *root.parent.parent.glob("*/*/.rootcause.toml")]}
                  - {root})


def _declared_mirrors(brain: Path) -> dict[str, Path]:
    mirrors = _toml(brain / ".rootcause.toml").get("mirrors")
    if not isinstance(mirrors, dict):
        return {}
    return {name: (brain / rel).resolve() for name, rel in mirrors.items()
            if isinstance(rel, str) and (brain / rel).resolve().is_dir()}


@dataclass
class Layout:
    kind: str                                   # brain | tenant | mirror
    project: str | None
    tenant: str | None
    own: str                                    # mount prefix this checkout is authoritative for
    local: dict[str, list[Checkout]]            # mount prefix → checkouts that serve it
    consumers: list[str] = field(default_factory=list)  # a mirror's consumer projects (remote scope)


def layout(root: Path) -> Layout:
    cfg = _toml(root / ".rootcause.toml")
    project, tenant = cfg.get("project"), cfg.get("tenant")
    local: dict[str, list[Checkout]] = {}

    def serve(prefix: str, path: Path) -> None:
        local.setdefault(prefix, []).append(Checkout(path))

    def serve_brain(brain: Path) -> None:
        serve("/brain", brain)
        for name, path in _declared_mirrors(brain).items():
            if path != root:
                serve(f"/mirrors/{name}", path)

    if tenant:
        kind, own = "tenant", "/tenant"
        for sib in _siblings(root):
            sc = _toml(sib / ".rootcause.toml")
            if sc.get("project") == project and not sc.get("tenant"):
                serve_brain(sib)
    else:
        consumers = [(sib, name) for sib in _siblings(root)
                     for name, path in _declared_mirrors(sib).items() if path == root]
        if consumers:
            kind, own = "mirror", f"/mirrors/{consumers[0][1]}"
            for sib, _name in consumers:
                serve_brain(sib)
        else:
            kind, own = "brain", "/brain"
            for name, path in _declared_mirrors(root).items():
                serve(f"/mirrors/{name}", path)
    local[own] = [Checkout(root)]
    lay = Layout(kind, project, tenant, own, local)
    if kind == "mirror":
        lay.consumers = sorted({p for sib, _n in consumers
                                if (p := _toml(sib / ".rootcause.toml").get("project"))})
    return lay


def _mount_of(path: str) -> tuple[str, str]:
    """(mount prefix, rel) — `/mirrors/<name>` is its own prefix."""
    parts = path.split("/")
    n = 3 if parts[1] == "mirrors" else 2
    return "/".join(parts[:n]), "/".join(parts[n:])


def resolve_local(lay: Layout, refs: dict[str, Ref]) -> list[Ref]:
    """Settle what local checkouts can; return the refs production must answer."""
    remote: list[Ref] = []
    for ref in refs.values():
        prefix, rel = _mount_of(ref.path)
        if prefix.startswith("/skills"):
            ref.verdict, ref.detail = "unverified", "/skills is scenario-gated and not mounted in the console"
        elif prefix == lay.own:
            ok = lay.local[prefix][0].has(rel)
            ref.verdict = "ok" if ok else "missing"
            ref.detail = "" if ok else lay.local[prefix][0].why_not(rel)
        elif prefix in lay.local and any(c.has(rel) for c in lay.local[prefix]):
            ref.verdict = "ok"
        else:
            if prefix in lay.local:
                ref.detail = "local " + ", ".join(c.why_not(rel) for c in lay.local[prefix])
            remote.append(ref)
    return remote


# ── production: one batched `rc dev console paths` per tenant ──────────────────────────────────────
def resolve_remote(root: Path, refs: list[Ref], tenants: str | None,
                   projects: list[str | None], any_ok: bool = False) -> str:
    """Fill verdicts from production; return a one-line note on what was asked. Each project is asked
    once (a tenant-required project with no tenant given: every tenant). `any_ok` (a mirror, whose
    consumers mount different things): present anywhere is enough."""
    def ask(project: str | None, tenant_arg: str | None) -> tuple[dict | None, str]:
        cmd = ["rc", "dev", "console", "paths", "-o", "json", "--raw-output", "-"]
        if tenant_arg:
            cmd[4:4] = ["--tenants", tenant_arg]
        if project:
            cmd[1:1] = ["--project", project]
        try:
            proc = subprocess.run(cmd, cwd=root, input="\n".join(r.path for r in refs),
                                  capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"rc failed: {exc}"
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None, (proc.stderr or proc.stdout).strip()[:300]
        if not isinstance(data, dict) or "results" not in data:  # rc's {"error": {code, message}}
            err = data.get("error") if isinstance(data, dict) else None
            return None, (f"{err.get('code')}: {err.get('message')}" if isinstance(err, dict)
                          else proc.stdout.strip()[:300])
        return data, ""

    kinds: dict[str, dict[str, str]] = {}   # path → scope label → kind
    answered: list[str] = []
    problems: list[str] = []
    for project in projects:
        data, err = ask(project, tenants)
        if data is None and tenants is None and "TENANT_REQUIRED" in err:
            data, err = ask(project, "all")
        if data is None:
            if "unknown command" in err:
                err = "this rc has no `dev console paths`; upgrade rc (brain-dev-upgrade)"
            problems.append(f"{project + ': ' if project else ''}{err}")
            continue
        prefix = f"{project}/" if len(projects) > 1 else ""
        for res in data.get("results") or []:
            label = prefix + (res.get("tenant") or "-")
            if res.get("error"):
                problems.append(f"{label}: {res['error'][:80]}")
                continue
            answered.append(label)
            for e in res.get("entries") or []:
                kinds.setdefault(e["path"], {})[label] = e["kind"]

    for r in refs:
        seen = kinds.get(r.path, {})
        missing = sorted(t for t, k in seen.items() if k == "missing")
        local_note = f"{r.detail}; " if r.detail else ""
        if not seen:
            r.verdict, r.detail = "unverified", "production check failed (see summary)"
        elif not missing or (any_ok and len(missing) < len(seen)):
            r.verdict, r.detail = "ok", ""
        elif problems and len(missing) == len(seen):  # a failed scope may hold it
            r.verdict = "unverified"
            r.detail = f"missing on {len(missing)}/{len(seen)} answering tenant(s); some failed"
        elif len(missing) == len(seen):
            r.verdict = "missing"
            r.detail = local_note + ("missing in production" if len(seen) == 1
                                     else f"missing on all {len(seen)} tenant(s)")
        else:
            shown = ", ".join(missing[:8]) + (f" +{len(missing) - 8} more" if len(missing) > 8 else "")
            r.verdict, r.detail = "partial", f"missing on {len(missing)}/{len(seen)} tenants: {shown}"
    note = f"production: {len(refs)} path(s) on {len(answered)} tenant(s)"
    note += f" ({', '.join(answered)})" if len(answered) <= 5 else ""
    if problems:
        note += f"; failed: {'; '.join(problems[:4])}" + (f" +{len(problems) - 4}" if len(problems) > 4 else "")
    return note


# ── driver ──────────────────────────────────────────────────────────────────────────────────────────
def scan_files(root: Path, only: list[str] | None) -> list[str]:
    visible = Checkout(root).files()
    files = visible if only is None else set(only) & visible
    return sorted(p for p in files if Path(p).suffix.lower() in SCAN_SUFFIXES
                  and not p.startswith(SKIP_DIRS) and "/tests/" not in p)


def check(root: Path, files: list[str] | None = None, *, remote: bool = True,
          tenants: str | None = None, project: str | None = None) -> tuple[Layout, list[Ref], str]:
    lay = layout(root)
    refs = extract(root, scan_files(root, files))
    pending = resolve_local(lay, refs)
    note = ""
    if pending and remote:
        if lay.tenant and tenants is None:
            tenants = lay.tenant
        projects = [project] if project else (lay.consumers or [None]) if lay.kind == "mirror" else [None]
        note = resolve_remote(root, pending, tenants, projects, any_ok=lay.kind == "mirror")
    else:
        for r in pending:
            r.verdict = "unverified"
            r.detail = (r.detail + "; " if r.detail else "") + "needs a production check (drop --local)"
    return lay, sorted(refs.values(), key=lambda r: r.path), note


LEVEL = {"missing": "WARN", "partial": "NOTICE", "unverified": "NOTICE"}


def level(ref: Ref) -> str:
    return "NOTICE" if ref.verdict == "missing" and ref.conditional else LEVEL.get(ref.verdict, "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_paths.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="checkout root (default: cwd's git toplevel)")
    p.add_argument("--base", default="origin/main", help="scope to files changed vs this ref")
    p.add_argument("--all", action="store_true", help="scan every run-visible file")
    p.add_argument("--local", action="store_true", help="no production call; unresolved → NOTICE")
    p.add_argument("--tenants", help="production tenants: a,b or all (default: rc scope; overlay: "
                                      "its tenant; tenant-required project: all)")
    p.add_argument("--project", help="rc project for the production check (default: .rootcause.toml)")
    p.add_argument("--strict", action="store_true", help="exit 1 on any WARN")
    p.add_argument("-o", "--output", choices=("text", "json"), default="text")
    p.add_argument("files", nargs="*", help="only these files (relative to the root)")
    args = p.parse_args(argv)
    try:
        root = bs.git_toplevel(Path(args.root).resolve() if args.root else Path.cwd())
    except bs.StructureError as exc:
        print(f"brain-paths: {exc}", file=sys.stderr)
        return 2
    files = args.files or (None if args.all else bs.git_changed_paths(root, args.base))
    lay, refs, note = check(root, files, remote=not args.local, tenants=args.tenants,
                            project=args.project)
    if args.output == "json":
        print(json.dumps({"kind": lay.kind, "own_mount": lay.own, "note": note,
                          "paths": [{"path": r.path, "verdict": r.verdict, "detail": r.detail,
                                     "level": level(r),
                                     "sites": [f"{f}:{n}" for f, n in r.sites]} for r in refs]},
                         indent=2))
    else:
        gated = sorted({r.path.split("/")[2] for r in refs if r.path.startswith("/skills/")})
        for r in refs:
            if level(r) and not r.path.startswith("/skills/"):
                site = ", ".join(f"{f}:{n}" for f, n in r.sites[:3])
                site += f" +{len(r.sites) - 3}" if len(r.sites) > 3 else ""
                print(f"{level(r)} {r.path}: {r.detail or r.verdict}  [{site}]")
        if gated:
            print(f"NOTICE /skills: {', '.join(gated)} unverified (scenario/feature-gated; not "
                  "mounted in the console)")
        counts = {v: sum(r.verdict == v for r in refs) for v in ("ok", "missing", "partial", "unverified")}
        scope = "all" if files is None else f"{len(files)} file(s)"
        print(f"brain paths: {lay.kind} ({lay.own}), {len(refs)} path(s) in {scope}: "
              + ", ".join(f"{n} {v}" for v, n in counts.items()) + (f" — {note}" if note else ""))
    return int(args.strict and any(level(r) == "WARN" for r in refs))


if __name__ == "__main__":
    raise SystemExit(main())
