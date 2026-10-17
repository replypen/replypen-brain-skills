#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Bounded orientation scan of a customer codebase into `scan.md`, `scan.json`, `tables.tsv`.

    uv run skills/brain-grounding-intake/scripts/scan.py [--repo NAME=PATH ...]
        [--listing NAME=FILE[:ROOT] ...] [--tables FILE] [--out DIR]

With no `--repo` and no `--listing` the `[mirrors]` table of the brain's `.replypen.toml` is
walked. With no `--tables` and a `schema.json` already in `$OUT`, the probed table names are
the table list, so the code half and the data half meet in one directory. Listing mode reads only a file of paths (the `find` output you captured from the prod
console): no file contents, so no manifests, no line counts, no word grep.

Read-only. Never reads `.env` or `.env.*` (only the names `.env.example`, `.env.dist`,
`.env.sample` are listed, never their contents). Every path in the outputs is the production
shape `/mirrors/<name>/<relative path>`; `scan.md` prints the local mapping once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"

MAX_FILES = 60000
MAX_GREP_FILES = 2000
MAX_GREP_BYTES = 256 * 1024
MAX_TABLE_FILES = 1500
MAX_TABLE_BYTES = 64 * 1024
MAX_AREA_ITEMS = 12
MAX_DEPS = 80
MAX_TALLY_DIRS = 25
MAX_TALLY_EXT = 6
APP_DEPTH = 3

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "bower_components", "storage", "cache", "var", "tmp",
    "temp", "dist", "build", "logs", "log", "uploads", "__pycache__", ".idea", ".vscode",
    "coverage", ".next", ".nuxt", ".venv", "venv", "site-packages",
}
KEEP_DOT_DIRS = {".github"}  # every other dot directory is tooling (worktrees, caches, agent trees)
CODE_EXT = {
    "php", "py", "rb", "go", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue", "java", "kt", "cs",
    "ex", "exs", "rs", "yaml", "yml", "json", "toml", "ini", "xml", "sql", "twig", "blade",
    "html", "erb", "haml", "env", "sh", "conf", "cfg", "prisma", "graphql", "proto", "ru",
}
NON_CODE_PREFIXES = ("docs/", "doc/", "tests/", "test/", "spec/", "fixtures/", "examples/")
VENDORED_SEGMENTS = {"libraries", "third_party", "lib", "plugins"}  # a manifest here is a vendored package
FRAMEWORK_CORE = ("system/",)  # CodeIgniter ships its core in the repo; never an area candidate
EXCLUDED_PREFIXES: set[str] = set()  # vendored package roots of the current repo, filled by analyse()
ASSET_EXT = {
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp", "bmp", "tiff", "woff", "woff2", "ttf",
    "eot", "otf", "pdf", "zip", "gz", "bz2", "xz", "tar", "rar", "7z", "mp3", "mp4", "mov",
    "avi", "wav", "ogg", "webm", "exe", "dll", "so", "dylib", "class", "jar", "pyc", "psd",
    "ai", "sketch", "bin", "dat", "db", "sqlite", "lock", "map",
}
ENV_OK = {".env.example", ".env.dist", ".env.sample"}
MANIFESTS = (
    "composer.json", "package.json", "Gemfile", "go.mod", "pyproject.toml", "requirements.txt",
    "pom.xml", "artisan", "manage.py", "mix.exs",
)
BACKEND_MANIFESTS = set(MANIFESTS) - {"package.json"}
VENDORS = [
    "mollie", "stripe", "adyen", "paypal", "twilio", "messagebird", "vonage", "mailgun",
    "sendgrid", "postmark", "ses", "aws-sdk", "mailchimp", "sentry", "bugsnag", "pusher",
    "algolia", "elasticsearch", "redis", "firebase", "onesignal", "exact", "yuki", "peppol",
    "google", "facebook", "intercom", "helpscout", "zendesk", "slack",
]
AREAS = ("entry", "routes", "config", "models", "mail", "jobs", "migrations", "tests", "frontend")


# --------------------------------------------------------------------- helpers

def find_brain_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (_marker(candidate)).exists():
            return candidate
    return here


def read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.10 only
        return _naive_toml(path)
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _naive_toml(path: Path) -> dict:  # pragma: no cover - 3.10 fallback
    out: dict = {}
    table = out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            table = out.setdefault(line[1:-1], {})
        elif "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out_value = value.strip().strip('"').strip("'")
            table[key.strip()] = out_value
    return out


def depth(rel: str) -> int:
    return rel.count("/")


def ext_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name[1:] else ""


def is_asset(rel: str) -> bool:
    return ext_of(rel) in ASSET_EXT


def is_code(rel: str) -> bool:
    """Name-based area rules and the vendor grep only look at source-shaped files outside docs/tests."""
    name = rel.rsplit("/", 1)[-1]
    low = rel.lower()
    if low.startswith(NON_CODE_PREFIXES) or low.startswith(FRAMEWORK_CORE):
        return False
    if any(rel.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return False
    return ext_of(rel) in CODE_EXT or name in ENV_OK or name in ("crontab", "Procfile", "artisan", "Gemfile")


def skip_name(name: str) -> bool:
    """`.env` and `.env.local` never appear; the example files do, by name only."""
    return name.startswith(".env") and name not in ENV_OK


def read_text(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def count_lines(path: Path) -> int:
    text = read_text(path, 2 * 1024 * 1024)
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


# ----------------------------------------------------------------- collecting

def walk_repo(root: Path) -> tuple[list[str], bool]:
    rels: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and (not d.startswith(".") or d in KEEP_DOT_DIRS))
        base = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if base == "." else base + "/"
        for name in sorted(filenames):
            if skip_name(name):
                continue
            rels.append(prefix + name)
            if len(rels) >= MAX_FILES:
                truncated = True
                return sorted(rels), truncated
    return sorted(rels), truncated


def read_listing(path: Path, root: str) -> tuple[list[str], bool]:
    rels: list[str] = []
    prefix = root.rstrip("/") + "/"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel = line[len(prefix):] if line.startswith(prefix) else line.lstrip("/")
        if not rel or skip_name(rel.rsplit("/", 1)[-1]):
            continue
        if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
            continue
        rels.append(rel)
    truncated = len(rels) >= MAX_FILES
    return sorted(rels)[:MAX_FILES], truncated


# ------------------------------------------------------------------ app roots

def app_dirs(rels: set[str]) -> dict[str, set[str]]:
    """Directory (relative, "" for the repo root) -> the manifest basenames found there."""
    found: dict[str, set[str]] = defaultdict(set)
    for rel in rels:
        parent, _, name = rel.rpartition("/")
        if name in MANIFESTS or name.endswith(".csproj"):
            marker = name if name in MANIFESTS else "*.csproj"
            if depth(parent) + (1 if parent else 0) <= APP_DEPTH:
                found[parent].add(marker)
        if rel.endswith("bin/console"):
            base = rel[: -len("bin/console")].rstrip("/")
            if depth(base) + (1 if base else 0) <= APP_DEPTH:
                found[base].add("bin/console")
        if rel.endswith("application/config/config.php"):
            base = rel[: -len("application/config/config.php")].rstrip("/")
            if depth(base) + (1 if base else 0) <= APP_DEPTH:
                found[base].add("application/config/config.php")
    return dict(found)


def prune_frontend(found: dict[str, set[str]]) -> tuple[list[str], dict[str, list[str]]]:
    """Drop nested pure-frontend app roots; return the kept roots and the dropped ones per parent."""
    roots = sorted(found, key=lambda d: (depth(d) if d else -1, d))
    kept: list[str] = []
    nested: dict[str, list[str]] = defaultdict(list)
    for candidate in roots:
        parents = [r for r in kept if candidate != r and (r == "" or candidate.startswith(r + "/"))]
        backend_parent = next((r for r in reversed(parents) if found[r] & BACKEND_MANIFESTS), None)
        if backend_parent is not None and found[candidate] == {"package.json"}:
            nested[backend_parent].append(candidate)
            continue
        kept.append(candidate)
    return kept, dict(nested)


# ----------------------------------------------------------------- manifests

def parse_deps(root: Path | None, app: str, markers: set[str]) -> list[str]:
    if root is None:
        return []
    base = root / app if app else root
    deps: list[str] = []

    def js(path: Path, keys: tuple[str, ...]) -> None:
        try:
            data = json.loads(read_text(path, 1024 * 1024) or "{}")
        except ValueError:
            return
        for key in keys:
            block = data.get(key)
            if isinstance(block, dict):
                deps.extend(str(name) for name in block)

    if "composer.json" in markers:
        js(base / "composer.json", ("require", "require-dev"))
    if "package.json" in markers:
        js(base / "package.json", ("dependencies",))
    if "Gemfile" in markers:
        for line in read_text(base / "Gemfile", 256 * 1024).splitlines():
            hit = re.match(r"""\s*gem\s+['"]([^'"]+)['"]""", line)
            if hit:
                deps.append(hit.group(1))
    if "go.mod" in markers:
        for line in read_text(base / "go.mod", 256 * 1024).splitlines():
            hit = re.match(r"\s*(?:require\s+)?([a-z0-9./_-]+\.[a-z]{2,}/[^\s]+)\s+v", line)
            if hit:
                deps.append(hit.group(1))
    if "pyproject.toml" in markers:
        text = read_text(base / "pyproject.toml", 256 * 1024)
        block = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
        if block:
            deps.extend(re.findall(r"""['"]\s*([A-Za-z0-9._-]+)""", block.group(1)))
    if "requirements.txt" in markers:
        for line in read_text(base / "requirements.txt", 256 * 1024).splitlines():
            hit = re.match(r"\s*([A-Za-z0-9._-]+)", line)
            if hit and not line.strip().startswith("#"):
                deps.append(hit.group(1))
    seen: list[str] = []
    for dep in deps:
        if dep not in seen:
            seen.append(dep)
    return seen[:MAX_DEPS]


def frameworks(app_rels: set[str], markers: set[str], deps: list[str]) -> list[dict]:
    low = {d.lower() for d in deps}
    out: list[dict] = []

    def add(name: str, marker: str) -> None:
        if not any(f["name"] == name for f in out):
            out.append({"name": name, "marker": marker})

    def dep(*names: str) -> str | None:
        return next((n for n in names if n in low), None)

    if "artisan" in app_rels:
        add("laravel", "artisan")
    elif dep("laravel/framework"):
        add("laravel", "composer dep laravel/framework")
    if "bin/console" in app_rels and "config/bundles.php" in app_rels:
        add("symfony", "bin/console + config/bundles.php")
    elif dep("symfony/framework-bundle"):
        add("symfony", "composer dep symfony/framework-bundle")
    ci = next((p for p in ("application/config/config.php", "system/core/CodeIgniter.php")
               if p in app_rels), None)
    if ci:
        add("codeigniter", ci)
    elif dep("codeigniter4/framework"):
        add("codeigniter", "composer dep codeigniter4/framework")
    if "Gemfile" in markers and "config/routes.rb" in app_rels:
        add("rails", "Gemfile + config/routes.rb")
    if "manage.py" in app_rels:
        add("django", "manage.py")
    for name in ("flask", "fastapi"):
        if dep(name):
            add(name, f"dep {name}")
    js_map = {"express": ("express",), "nest": ("@nestjs/core",), "next": ("next",),
              "nuxt": ("nuxt",), "vue": ("vue",), "react": ("react",), "angular": ("@angular/core",)}
    if "package.json" in markers:
        for name, candidates in js_map.items():
            hit = dep(*candidates)
            if hit:
                add(name, f"package.json dep {hit}")
    if "go.mod" in markers:
        add("go", "go.mod")
        for name, needle in (("gin", "gin-gonic/gin"), ("echo", "labstack/echo"), ("chi", "go-chi/chi")):
            if any(needle in d for d in low):
                add(name, f"go.mod require {needle}")
    if "pom.xml" in markers:
        add("spring", "pom.xml")
    if "*.csproj" in markers:
        add("dotnet", "*.csproj")
    for language, marker in (("php", "composer.json"), ("node", "package.json"),
                             ("ruby", "Gemfile"), ("go", "go.mod")):
        if marker in markers:
            add(language, marker)
    if "pyproject.toml" in markers or "requirements.txt" in markers or "manage.py" in app_rels:
        add("python", "pyproject.toml/requirements.txt/manage.py")
    php_marker = next((p for p in ("artisan", "application/config/config.php") if p in app_rels), None)
    if php_marker:
        add("php", php_marker)
    return out


# --------------------------------------------------------------------- areas

def _rule_hits(app_rels: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Area -> [(rule priority, app-relative path)], most specific rule first."""
    hits: dict[str, list[tuple[int, str]]] = {area: [] for area in AREAS}
    entry_exact = ["public/index.php", "artisan", "bin/console", "manage.py", "index.php",
                   "main.go", "src/main.ts", "app.js", "server.js", "index.js", "config.ru"]
    routes_exact = ["config/routes.yaml", "config/routes.php", "application/config/routes.php",
                    "config/routes.rb"]
    for rel in app_rels:
        if not is_code(rel):
            continue
        name = rel.rsplit("/", 1)[-1]
        low = rel.lower()
        if rel in entry_exact:
            hits["entry"].append((entry_exact.index(rel), rel))
        elif re.fullmatch(r"cmd/[^/]+/main\.go", rel):
            hits["entry"].append((len(entry_exact), rel))
        if rel in routes_exact:
            hits["routes"].append((routes_exact.index(rel), rel))
        elif re.fullmatch(r"routes/[^/]+\.php", rel) or rel.startswith("config/routes/"):
            hits["routes"].append((4, rel))
        elif name == "urls.py":
            hits["routes"].append((5, rel))
        elif ("route" in name.lower() or "router" in name.lower()) and rel.split("/")[0] in ("src", "app"):
            hits["routes"].append((6, rel))
        if name in (".env.example", ".env.dist", ".env.sample"):
            hits["config"].append((0, rel))
        elif rel.startswith("application/config/"):
            hits["config"].append((1, rel))
        elif rel.startswith("config/") and depth(rel) <= 2:
            hits["config"].append((2, rel))
        elif re.fullmatch(r"settings[^/]*\.py", name) or name == "settings.py":
            hits["config"].append((3, rel))
        elif any(word in name.lower() for word in ("feature", "flag", "setting")):
            hits["config"].append((4, rel))
        model_prefixes = ["app/Models/", "src/Entity/", "application/models/", "app/models/", "models/"]
        prefix = next((p for p in model_prefixes if rel.startswith(p)), None)
        if rel in ("prisma/schema.prisma", "db/schema.rb"):
            hits["models"].append((0, rel))
        elif prefix:
            hits["models"].append((1 + model_prefixes.index(prefix), rel))
        elif "/Entity/" in "/" + rel or "/Entities/" in "/" + rel or "/Model/" in "/" + rel:
            hits["models"].append((6, rel))  # Doctrine per-module entities (src/<Module>/Entity/)
        elif name.endswith(".model.ts"):
            hits["models"].append((7, rel))
        elif re.fullmatch(r"app/[A-Z][A-Za-z0-9]*\.php", rel):
            hits["models"].append((8, rel))
        mail_prefixes = ["app/Mail/", "app/Notifications/", "src/Mailer/"]
        mprefix = next((p for p in mail_prefixes if rel.startswith(p)), None)
        if mprefix:
            hits["mail"].append((mail_prefixes.index(mprefix), rel))
        elif re.match(r"src/.*/Mail[^/]*/", rel):
            hits["mail"].append((3, rel))
        elif rel.startswith("application/libraries/") and "mail" in low:
            hits["mail"].append((4, rel))
        elif "mail" in low or "notif" in low:
            hits["mail"].append((5, rel))
        job_prefixes = ["app/Console/", "app/Jobs/", "src/Command/", "application/controllers/cron"]
        jprefix = next((p for p in job_prefixes if rel.startswith(p)), None)
        if jprefix:
            hits["jobs"].append((job_prefixes.index(jprefix), rel))
        elif re.match(r"src/.*/Job[^/]*/", rel):
            hits["jobs"].append((4, rel))
        elif name in ("crontab", "Procfile") or name.endswith(".cron"):
            hits["jobs"].append((5, rel))
        elif rel.startswith("config/schedule") or rel.startswith("config/queue"):
            hits["jobs"].append((6, rel))
        elif any(word in low for word in ("supervisor", "sidekiq", "celery")):
            hits["jobs"].append((7, rel))
    return hits


DIR_AREAS = {
    "migrations": ["database/migrations", "migrations", "db/migrate", "application/migrations",
                   "alembic/versions", "priv/repo/migrations", "src/Migrations"],
    "tests": ["tests", "spec", "test"],
    "frontend": ["resources/js", "resources/views", "templates", "views"],
}


def _dir_hits(app_rels: list[str], area: str, extra: list[str]) -> list[tuple[int, str, int]]:
    """(priority, directory, file count) for the directory-shaped areas."""
    out: list[tuple[int, str, int]] = []
    prefixes = DIR_AREAS[area] + extra
    for index, prefix in enumerate(prefixes):
        count = sum(1 for rel in app_rels if rel.startswith(prefix + "/"))
        if count:
            out.append((index, prefix + "/", count))
    if area == "migrations":
        sql = [rel for rel in app_rels if rel.endswith(".sql") and depth(rel) <= APP_DEPTH and is_code(rel)]
        by_dir = Counter(rel.rpartition("/")[0] for rel in sql)
        for directory, count in sorted(by_dir.items()):
            path = (directory + "/") if directory else ""
            if not any(path == existing for _, existing, _ in out):
                out.append((len(prefixes), path, count))
    return out


def build_areas(name: str, app: str, app_rels: list[str], root: Path | None,
                nested_frontend: list[str]) -> dict[str, list[dict]]:
    hits = _rule_hits(app_rels)
    areas: dict[str, list[dict]] = {}
    for area in ("entry", "routes", "config", "models", "mail", "jobs"):
        chosen = sorted(set(hits[area]), key=lambda item: (item[0], depth(item[1]), len(item[1]), item[1]))
        items = []
        for _, rel in chosen[:MAX_AREA_ITEMS]:
            lines = 0
            if root is not None:
                lines = count_lines(root / (f"{app}/{rel}" if app else rel))
            items.append({"path": prod_path(name, app, rel), "lines": lines})
        if items:
            areas[area] = items
    frontend_extra = [d[len(app) + 1:] if app and d.startswith(app + "/") else d
                      for d in nested_frontend]
    for area in ("migrations", "tests", "frontend"):
        extra = [e.rstrip("/") for e in frontend_extra] if area == "frontend" else []
        chosen = sorted(_dir_hits(app_rels, area, extra),
                        key=lambda item: (item[0], len(item[1]), item[1]))
        items = [{"path": prod_path(name, app, path), "lines": count}
                 for _, path, count in chosen[:MAX_AREA_ITEMS]]
        if items:
            areas[area] = items
    return areas


def prod_path(name: str, app: str, rel: str) -> str:
    """The production shape of an app-relative path; directory entries keep their trailing slash."""
    inner = f"{app}/{rel}" if app else rel
    return f"/mirrors/{name}/{inner}"


# -------------------------------------------------------------- integrations

def integrations(root: Path | None, app: str, app_rels: list[str], deps: list[str],
                 name: str) -> list[dict]:
    dep_low = " ".join(deps).lower()
    out: list[dict] = []
    grep_counts: dict[str, list[str]] = defaultdict(list)
    if root is not None:
        pattern = re.compile("|".join(rf"\b{re.escape(v)}\b" for v in VENDORS), re.I)
        scanned = 0
        for rel in app_rels:
            if scanned >= MAX_GREP_FILES:
                break
            if is_asset(rel) or not is_code(rel):
                continue
            path = root / (f"{app}/{rel}" if app else rel)
            try:
                if path.stat().st_size > MAX_GREP_BYTES:
                    continue
            except OSError:
                continue
            text = read_text(path, MAX_GREP_BYTES)
            if not text:
                continue
            scanned += 1
            for hit in set(m.group(0).lower() for m in pattern.finditer(text)):
                grep_counts[hit].append(prod_path(name, app, rel))
    for vendor in VENDORS:
        has_dep = vendor in dep_low
        files = grep_counts.get(vendor, [])
        if has_dep or files:
            out.append({"vendor": vendor, "dep": has_dep, "files": len(files),
                        "sample": files[:3]})
    return out


# -------------------------------------------------------------------- tables

def singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def table_files(repo, areas_paths: list[str]) -> list[str]:
    """Repo-relative files under the model and migration area entries, capped."""
    picked: list[str] = []
    rels = repo["rels"]
    # An area lists at most MAX_AREA_ITEMS files; the table mapping wants the whole directory.
    dirs: list[str] = []
    for path in areas_paths:
        inner = path[len(f"/mirrors/{repo['name']}/"):]
        directory = inner if inner.endswith("/") else inner.rpartition("/")[0] + "/"
        if directory not in dirs:
            dirs.append(directory)
    for directory in dirs:
        picked.extend(rel for rel in rels if rel.startswith(directory) and is_code(rel))
    seen: list[str] = []
    for rel in picked:
        if rel not in seen and not is_asset(rel):
            seen.append(rel)
    return seen[:MAX_TABLE_FILES]


def scan_tables(repos: list[dict], names: list[str]) -> list[dict]:
    rows: list[dict] = []
    per_repo: list[tuple[dict, list[str], dict[str, str]]] = []
    for repo in repos:
        paths: list[str] = []
        for app in repo["apps"]:
            for area in ("models", "migrations"):
                paths.extend(item["path"] for item in app["areas"].get(area, []))
        files = table_files(repo, paths)
        contents: dict[str, str] = {}
        if repo["mode"] == "local":
            root = Path(repo["local_root"])
            for rel in files:
                contents[rel] = read_text(root / rel, MAX_TABLE_BYTES)
        per_repo.append((repo, files, contents))
    for table in names:
        model_files: list[str] = []
        mentions = 0
        guess = ""
        declare = re.compile(
            r"""(?:\$table\s*=\s*|ORM\\Table\(\s*name\s*[:=]\s*|table_name\s*=\s*"""
            r"""|__tablename__\s*=\s*"""
            r"""|->(?:get|get_where|from|insert|insert_batch|update|update_batch|delete|join"""
            r"""|count_all_results|count_all|truncate|replace)\(\s*)['"]""" + re.escape(table)
            + r"""['"]|\b(?:from|join|into|update)\s+`?""" + re.escape(table) + r"""`?\b""", re.I)
        word = re.compile(r"\b" + re.escape(table) + r"\b")
        keys = {_key(table), _key(singular(table)), _key(singular(table) + "s")}
        for repo, files, contents in per_repo:
            for rel in files:
                base = rel.rsplit("/", 1)[-1]
                stem = re.sub(r"\.(php|py|rb|ts|js|go)$", "", base)
                stem = re.sub(r"(_model|Model)$", "", stem)
                if not guess and _key(stem) in keys:
                    guess = f"/mirrors/{repo['name']}/{rel}"
                text = contents.get(rel, "")
                if not text:
                    continue
                if declare.search(text):
                    model_files.append(f"/mirrors/{repo['name']}/{rel}")
                if word.search(text):
                    mentions += 1
        rows.append({"table": table, "model_files": model_files, "mentions": mentions,
                     "guess": guess})
    return rows


def tables_tsv(rows: list[dict]) -> str:
    matched = [r for r in rows if r["model_files"] or r["guess"]]
    unmatched = [r for r in rows if not (r["model_files"] or r["guess"])]
    lines = ["table\tmodel_files\tmentions\tguess"]
    for row in sorted(matched, key=lambda r: r["table"]):
        lines.append(f"{row['table']}\t{';'.join(row['model_files'])}\t{row['mentions']}\t{row['guess']}")
    if unmatched:
        lines.append(f"# unmatched ({len(unmatched)})")
        for row in sorted(unmatched, key=lambda r: r["table"]):
            lines.append(f"{row['table']}\t\t{row['mentions']}\t")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- tallies

def ext_tally(app_rels: list[str]) -> dict[str, dict[str, int]]:
    per_dir: dict[str, Counter] = defaultdict(Counter)
    for rel in app_rels:
        top = rel.split("/")[0] if "/" in rel else "."
        per_dir[top][ext_of(rel) or "(none)"] += 1
    ranked = sorted(per_dir.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    return {name: dict(counter.most_common(MAX_TALLY_EXT))
            for name, counter in ranked[:MAX_TALLY_DIRS]}


# ------------------------------------------------------------------ rendering

def render_md(project: str, repos: list[dict], tables: list[dict], mode_note: str) -> str:
    total = sum(repo["files"] for repo in repos)
    out = [f"# source scan · {project} · {len(repos)} repo(s) · {total} files · {mode_note}", ""]
    for repo in repos:
        head = f"## {repo['name']} → {repo['display_root']} ({repo['mode']}, {repo['files']} files"
        head += ", truncated)" if repo["truncated"] else ")"
        out.append(head)
        if repo["mode"] == "listing":
            out.append("mode: listing (contents not read)")
        for app in repo["apps"]:
            out.append("")
            out.append(f"### app: {app['root']}")
            frames = ", ".join(f"{f['name']} ({f['marker']})" for f in app["frameworks"]) or "none detected"
            out.append(f"frameworks: {frames}")
            for area in AREAS:
                items = app["areas"].get(area)
                if items:
                    body = " · ".join(f"{i['path']} ({i['lines']})" for i in items)
                    out.append(f"- {area}: {body}")
            if app["integrations"]:
                parts = []
                for hit in app["integrations"]:
                    tail = f"{hit['files']} file" + ("s" if hit["files"] != 1 else "") + (
                        ": " + ", ".join(hit["sample"]) if hit["sample"] else "")
                    parts.append(f"{hit['vendor']} ({'dep' if hit['dep'] else '0 dep'}, {tail})")
                out.append("- integrations: " + " · ".join(parts))
            if app["deps"]:
                out.append("- deps: " + ", ".join(app["deps"][:40]))
            tally = " · ".join(
                f"{name}/ " + " ".join(f"{ext} {count}" for ext, count in exts.items())
                for name, exts in app["ext_tally"].items())
            if tally:
                out.append("- tally: " + tally)
        out.append("")
    if tables:
        matched = sum(1 for row in tables if row["model_files"] or row["guess"])
        out.append(f"tables: {len(tables)} given, {matched} matched, "
                   f"{len(tables) - matched} unmatched → tables.tsv")
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------- main

def parse_pair(value: str) -> tuple[str, str]:
    name, _, rest = value.partition("=")
    if not name or not rest:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {value!r}")
    return name, rest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded orientation scan of a customer codebase")
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--listing", action="append", default=[], metavar="NAME=FILE[:ROOT]")
    parser.add_argument("--tables", type=Path, help="one table name per line")
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def sources(args: argparse.Namespace, brain_root: Path) -> list[tuple[str, str, str | None]]:
    """(name, kind, spec) triples; kind is `repo` or `listing`."""
    out: list[tuple[str, str, str | None]] = []
    for value in args.repo:
        name, path = parse_pair(value)
        out.append((name, "repo", path))
    for value in args.listing:
        name, spec = parse_pair(value)
        out.append((name, "listing", spec))
    if out:
        return out
    mirrors = read_toml(_marker(brain_root)).get("mirrors") or {}
    for name, rel in sorted(mirrors.items()):
        out.append((name, "repo", str(brain_root / str(rel))))
    return out


def build_repo(name: str, kind: str, spec: str) -> dict:
    if kind == "repo":
        root = Path(spec).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"scan: {name}: not a directory: {root}")
        rels, truncated = walk_repo(root)
        return {"name": name, "mode": "local", "local_root": str(root),
                "display_root": str(root), "rels": rels, "truncated": truncated}
    file_part, _, listing_root = spec.partition(":")
    listing_root = listing_root or f"/mirrors/{name}"
    path = Path(file_part).expanduser()
    if not path.is_file():
        raise SystemExit(f"scan: {name}: no such listing file: {path}")
    rels, truncated = read_listing(path, listing_root)
    return {"name": name, "mode": "listing", "local_root": None,
            "display_root": listing_root, "rels": rels, "truncated": truncated}


def analyse(repo: dict) -> None:
    rels = repo["rels"]
    repo["relset"] = set(rels)
    repo["files"] = len(rels)
    found = app_dirs(repo["relset"])
    vendored = {app for app in found if set(app.split("/")) & VENDORED_SEGMENTS}
    EXCLUDED_PREFIXES.clear()
    EXCLUDED_PREFIXES.update(app + "/" for app in vendored)
    found = {app: markers for app, markers in found.items() if app not in vendored}
    repo["vendored"] = sorted(vendored)
    if not found:
        found = {"": set()}
    kept, nested = prune_frontend(found)
    root = Path(repo["local_root"]) if repo["local_root"] else None
    apps = []
    for app in kept:
        prefix = app + "/" if app else ""
        # a file belongs to the deepest app root that contains it
        deeper = [other + "/" for other in kept if other != app and other.startswith(prefix)]
        inner = [rel[len(prefix):] for rel in rels
                 if rel.startswith(prefix) and not any(rel.startswith(d) for d in deeper)]
        deps = parse_deps(root, app, found[app])
        apps.append({
            "root": f"/mirrors/{repo['name']}/{app}" if app else f"/mirrors/{repo['name']}",
            "markers": sorted(found[app]),
            "frameworks": frameworks(set(inner), found[app], deps),
            "areas": build_areas(repo["name"], app, inner, root, nested.get(app, [])),
            "integrations": integrations(root, app, inner, deps, repo["name"]),
            "deps": deps,
            "ext_tally": ext_tally(inner),
        })
    repo["apps"] = apps


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    brain_root = find_brain_root()
    project = str(read_toml(_marker(brain_root)).get("project") or brain_root.name)
    out_dir = (args.out or brain_root / ".replypen" / "grounding-intake"
               / date.today().isoformat())
    out_dir = Path(out_dir).expanduser()
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)

    specs = sources(args, brain_root)
    if not specs:
        print("scan: nothing to scan: pass --repo NAME=PATH or --listing NAME=FILE, "
              "or add a [mirrors] table to .replypen.toml", file=sys.stderr)
        return 2
    repos = []
    for name, kind, spec in specs:
        repo = build_repo(name, kind, str(spec))
        analyse(repo)
        (out_dir / "raw" / f"listing-{name}.txt").write_text(
            "\n".join(repo["rels"]) + "\n", encoding="utf-8")
        repos.append(repo)

    names: list[str] = []
    if args.tables:
        for line in args.tables.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    elif (out_dir / "schema.json").is_file():
        probed = json.loads((out_dir / "schema.json").read_text(encoding="utf-8"))
        names = [str(row["table"]) for row in (probed.get("tables") or []) if row.get("table")]
        if names:
            print(f"tables: taking the {len(names)} table names from {out_dir / 'schema.json'}")
    tables = scan_tables(repos, names) if names else []
    if tables:
        (out_dir / "tables.tsv").write_text(tables_tsv(tables), encoding="utf-8")

    modes = sorted({repo["mode"] for repo in repos})
    mode_note = "listing (contents not read)" if modes == ["listing"] else "/".join(modes)
    payload = {
        "project": project,
        "scanned_at": date.today().isoformat(),
        "repos": [{
            "name": repo["name"], "root": repo["display_root"], "mode": repo["mode"],
            "files": repo["files"], "truncated": repo["truncated"], "apps": repo["apps"],
        } for repo in repos],
        "tables": tables,
    }
    (out_dir / "scan.json").write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "scan.md").write_text(render_md(project, repos, tables, mode_note), encoding="utf-8")

    apps = sum(len(repo["apps"]) for repo in repos)
    truncated = [repo["name"] for repo in repos if repo["truncated"]]
    print(f"{project}: {len(repos)} repo(s), {sum(r['files'] for r in repos)} files, {apps} app root(s)"
          + (f", {len(tables)} tables" if tables else "")
          + (f", truncated: {', '.join(truncated)}" if truncated else ""))
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
