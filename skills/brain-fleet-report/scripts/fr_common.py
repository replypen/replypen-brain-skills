# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Shared, deterministic primitives for `brain-fleet-report` (collect.py, correlate.py, drill.py).

Nothing here is project-specific: the rc wrapper with its on-disk raw cache, the Brussels day
window, run/error signature normalisation, the bash-corpus clusterer, privacy reducers, coverage
records, queue recurrence and the overlay loader. Project quirks live in the brain's
`_internal/fleet-report/` overlay (config.toml + optional overlay.py), never here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"

UTC = ZoneInfo("UTC")
DEFAULT_TZ = "Europe/Brussels"
MAX_WORKERS = 8

# Runs that represent real customer work. Everything else is counted and set aside.
DEFAULT_INCLUDE_KINDS = ("email", "analysis")
# Kinds that are dev/operator traffic on every project we have seen.
DEV_KINDS = ("prompt", "chat", "mcp", "console")


# --------------------------------------------------------------------------- time


def tzinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a typo in config must not sink the run
        return ZoneInfo(DEFAULT_TZ)


def yesterday(tz: ZoneInfo) -> date:
    return datetime.now(tz).date() - timedelta(days=1)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[00:00, next 00:00) local, as UTC instants. DST-correct: the day can be 23 or 25 h."""
    start = datetime.combine(day, datetime.min.time(), tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def workdays_before(day: date, count: int) -> list[date]:
    """The `count` Mon–Fri days strictly before `day`, oldest first.

    Monday's context is therefore Tue–Fri of the previous week, which is what makes a
    Monday report readable at all (the weekend is empty on every project we have seen).
    """
    days: list[date] = []
    cursor = day - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_day(value: Any, tz: ZoneInfo) -> date | None:
    moment = parse_ts(value)
    return moment.astimezone(tz).date() if moment else None


def hhmm(value: Any, tz: ZoneInfo) -> str:
    moment = parse_ts(value)
    return moment.astimezone(tz).strftime("%H:%M") if moment else "??:??"


def stamp(value: Any, tz: ZoneInfo) -> str:
    moment = parse_ts(value)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") if moment else "?"


# ------------------------------------------------------------------------ privacy

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + " …"


def one_line(text: Any, limit: int = 160) -> str:
    return clip(re.sub(r"\s+", " ", str(text or "")), limit)


def short_name(full: str | None) -> str:
    """`Sanny Paesmans` -> `Sanny P.`"""
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


def strip_emails(text: Any, keep: Iterable[str] = ()) -> str:
    kept = {a.lower() for a in keep}
    return EMAIL_RE.sub(
        lambda m: m.group(0) if m.group(0).lower() in kept else "[e-mail]", str(text or "")
    )


# A person is a *run* of capitalised words, not a fixed pair: `Van Nuffelen Greet`, `Lies Van
# Genechten`, `Molemans Bo`. Particles glue such a run together without being capitalised.
_PARTICLES = frozenset(
    "van de den der des ten ter te het du de la le von op in 't aan aux di da dos vande vanden".split()
)
# Capitalised words that are never a surname in this corpus. Weekday/month names dominate
# (`vrijdag 21 September`), the rest are product/company vocabulary.
_NOT_A_NAME = {
    w.lower()
    for w in (
        "maandag dinsdag woensdag donderdag vrijdag zaterdag zondag "
        "monday tuesday wednesday thursday friday saturday sunday "
        "januari februari maart april mei juni juli augustus september oktober november december "
        "january february march may june july august october "
        "lundi mardi mercredi jeudi vendredi samedi dimanche "
        "AI OPG RX HTTP HTTPS SQL PDF CSV URL API CLI DSN UUID ID PMS KB MCP PII "
        "ClickDoc ClickUp DentAI KampAdmin ReplyPen RootCause UiTPAS Invisalign Sentry Engine "
        "Gmail Google Outlook Microsoft Intercom Embassy Stripe Sync "
        "Dr Dhr Mevr Mvr Mr Mrs Prof Dokter Tandarts Praktijk Agenda Afspraak Patient Patiënt "
        "Beste Geachte Bedankt Hallo Dag Goedemorgen Goedemiddag Goedenavond "
        "Error Warning Traceback None True False Null"
    ).split()
}
# Registry-derived vocabulary (tenant slugs and display names), installed by collect.py.
_KNOWN_NON_NAMES: set[str] = set()
_WORDS = re.compile(r"(\s+)")
_TRAILING = " \t.,;:!?)»”\"'…]"
_LEADING = " \t(«“\"'[…"


def learn_non_names(words: Iterable[str]) -> None:
    """Teach the reducer project vocabulary (tenant slugs/names) so it stops eating it."""
    for phrase in words:
        for token in re.split(r"[\s_/-]+", str(phrase or "")):
            if len(token) > 1:
                _KNOWN_NON_NAMES.add(token.lower())


def _capitalised(core: str) -> bool:
    return bool(core) and core[0].isupper() and not core.isupper() and any(c.islower() for c in core[1:])


def reduce_names(text: Any) -> str:
    """`Van Nuffelen Greet` -> `Van N. G.`; `Molemans Bo` -> `Molemans B.`.

    Heuristic, deliberately: any run of >=2 capitalised words (particles allowed inside) that does
    not start a sentence and contains no known non-name is collapsed to its first word plus the
    initials of the rest. It will occasionally shorten a two-word product name mid-sentence and it
    will always miss a surname that opens a sentence — those are the accepted failure modes; the
    goal is that no full patient name survives into digest.md, not perfect linguistics.
    """
    tokens = _WORDS.split(str(text or ""))
    # even indices are words, odd indices the whitespace between them
    index_of_word = [i for i in range(0, len(tokens), 2) if tokens[i]]
    cores = {i: tokens[i].strip(_TRAILING).lstrip(_LEADING) for i in index_of_word}

    def _tail(position: int) -> str:
        return tokens[index_of_word[position - 1]].rstrip(")\"'»”")

    def sentence_start(position: int) -> bool:
        """Only real sentence enders: a capital here carries no evidence of being a surname."""
        return position == 0 or _tail(position).endswith((".", "!", "?"))

    def breaks_run(position: int) -> bool:
        """`Dag Sanny, Dank u` is not a three-part name — any punctuation ends the run."""
        return position == 0 or _tail(position).endswith((".", "!", "?", ",", ";", ":", "·", "|", "—"))

    def kind(position: int) -> str:
        core = cores[index_of_word[position]]
        if not core:
            return "stop"
        if core.lower() in _NOT_A_NAME or core.lower() in _KNOWN_NON_NAMES:
            return "stop"
        if core.lower() in _PARTICLES:
            return "particle"
        return "name" if _capitalised(core) else "stop"

    position = 0
    total = len(index_of_word)
    while position < total:
        if kind(position) != "name" or sentence_start(position):
            position += 1
            continue
        start = position
        # `Van Nuffelen Greet`: a *capitalised* particle in front belongs to the name. A lowercase
        # one is the Dutch preposition (`documenten van Molemans Bo`) — leave it alone.
        if (start and kind(start - 1) == "particle" and not breaks_run(start)
                and not sentence_start(start - 1)
                and cores[index_of_word[start - 1]][:1].isupper()):
            start -= 1
        end = position
        while end + 1 < total and kind(end + 1) in {"name", "particle"} and not breaks_run(end + 1):
            end += 1
        while end > start and kind(end) == "particle":
            end -= 1
        if sum(1 for p in range(start, end + 1) if kind(p) == "name") < 2:
            position = end + 1
            continue
        for p in range(start + 1, end + 1):
            slot = index_of_word[p]
            if kind(p) == "particle":
                continue
            raw, core = tokens[slot], cores[index_of_word[p]]
            trailing = raw[len(raw.rstrip(_TRAILING)):] if raw.rstrip(_TRAILING) != raw else ""
            tokens[slot] = raw[: raw.index(core)] + core[0].upper() + "." + trailing.lstrip(".")
        position = end + 1
    return "".join(tokens)


def privacy(text: Any, limit: int = 200, keep_emails: Iterable[str] = ()) -> str:
    """The only text transform allowed on anything that reaches digest.md."""
    return one_line(reduce_names(strip_emails(text, keep_emails)), limit)


# --------------------------------------------------------------- signatures / bash

_HEX = re.compile(r"\b[0-9a-f]{7,}\b", re.I)
_NUM = re.compile(r"\b\d{2,}\b")
_QUOTED = re.compile(r"(['\"])(?:(?!\1).){3,}\1")
_PATHISH = re.compile(r"/(?:tmp|home|srv)/[^\s'\"]+")


def normalise_error(text: Any, limit: int = 140) -> str:
    """Collapse ids/paths/quoted literals so the same defect gets one stable signature."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = UUID_RE.sub("<uuid>", value)
    value = _PATHISH.sub("<path>", value)
    value = _QUOTED.sub("<q>", value)
    value = _HEX.sub("<hex>", value)
    value = _NUM.sub("<n>", value)
    return clip(value, limit)


def stderr_last_line(stderr: Any) -> str:
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def first_token(command: Any) -> str:
    """The interesting head of a compound bash command: the script, else the verb."""
    text = str(command or "")
    script = re.search(r"([\w./-]+\.py)", text)
    if script:
        return Path(script.group(1)).name
    module = re.search(r"-m\s+([\w.]+)", text)
    if module:
        return f"-m {module.group(1)}"
    token = re.split(r"\s+", text.strip(), maxsplit=1)[0] if text.strip() else ""
    return Path(token).name if token else "?"


_USAGE_UNRECOGNIZED = re.compile(r"unrecognized arguments:\s*(.+)")
_USAGE_NOT_ALLOWED = re.compile(r"argument (--[a-z0-9-]+): not allowed with argument (--[a-z0-9-]+)")
_USAGE_ERROR = re.compile(r"error:\s*(.+)")
_EXPLORE = re.compile(r"^\s*(rg|grep|ls|find|test|\[)\b")


def usage_error(command: str, output: str) -> dict[str, str] | None:
    """A CLI/argparse rejecting the agent's flags — the same signal on every project."""
    if "usage:" not in output or "error:" not in output:
        return None
    named = re.search(r"usage:\s*([\w_.-]+)", output)
    script = named.group(1) if named else first_token(command)
    combo = _USAGE_NOT_ALLOWED.search(output)
    if combo:
        detail = " + ".join(sorted([combo.group(1), combo.group(2)])) + " (mutually exclusive)"
    else:
        unknown = _USAGE_UNRECOGNIZED.search(output)
        if unknown:
            flags = sorted({f for f in unknown.group(1).split() if f.startswith("--")})
            detail = "unrecognized " + " ".join(flags or [clip(unknown.group(1), 40)])
        else:
            message = _USAGE_ERROR.search(output)
            detail = clip(message.group(1), 90) if message else "usage error"
    return {"script": script, "detail": detail, "signature": f"{script}: {normalise_error(detail, 90)}"}


BASH_BUCKETS = ("usage", "timeout", "guard", "traceback", "sql", "path_miss", "noise", "other")


def bash_bucket(command: str, exit_code: Any, stderr: str, stdout: str = "") -> str:
    """Classify one non-zero bash event. Buckets are the same on every project."""
    blob = f"{stdout}\n{stderr}"
    if exit_code == -1:
        return "timeout"
    if exit_code == 64:
        return "guard"
    if usage_error(command, blob) or exit_code == 2 and "usage:" in blob:
        return "usage"
    low = stderr.lower()
    if "traceback (most recent call last)" in low or "modulenotfounderror" in low:
        return "traceback"
    if re.search(r"does not exist|undefinedcolumn|invalid input syntax|relation \"|"
                 r"invalid enum label|ambiguous column|data-scoping|syntax error at or near", low):
        return "sql"
    if re.search(r"no such file or directory|cannot access|can't read|not found:", low):
        return "path_miss"
    if exit_code == 1 and (_EXPLORE.match(command) or not stderr.strip()):
        return "noise"
    return "other"


def bash_cluster(command: str, stderr: str) -> str:
    """Cluster key: what ran + the last stderr line, both normalised.

    `fleet patterns` clusters on the whole compound command and previews stdout, which merges
    unrelated bugs; the last stderr line is where the actual defect is.
    """
    return f"{first_token(command)} · {normalise_error(stderr_last_line(stderr), 110) or '(no stderr)'}"


# ------------------------------------------------------------------ json plumbing


def json_objects(text: str) -> list[Any]:
    """Every top-level JSON value in a blob. `rp fleet health` prints two documents."""
    decoder = json.JSONDecoder()
    found: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char not in "{[":
            index += 1
            continue
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        found.append(value)
        index += consumed
    return found


_ACTION_KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\{.*?\}(?=\s+\w+=|\s*$)|[^\s]+)')


def parse_action_lines(text: str) -> list[dict[str, Any]]:
    """Parse `rp fleet actions --format agent` ACTION lines (the only source of error_message).

    Each line is `ACTION k=v k="v with spaces" params={...}`; values are JSON-quoted.
    """
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("ACTION "):
            continue
        row: dict[str, Any] = {}
        for key, raw in _ACTION_KV.findall(line[len("ACTION "):]):
            if raw.startswith('"'):
                try:
                    row[key] = json.loads(raw)
                except json.JSONDecodeError:
                    row[key] = raw.strip('"')
            elif raw.startswith("{"):
                try:
                    row[key] = json.loads(raw)
                except json.JSONDecodeError:
                    row[key] = raw
            else:
                row[key] = raw
        if row:
            rows.append(row)
    return rows


# ----------------------------------------------------------------------- rc access


@dataclass
class Coverage:
    """One record per evidence feed, surfaced in the report so gaps never look like good news."""

    feed: str
    status: str  # complete | partial | unavailable
    fetched: int = 0
    reason: str | None = None
    observed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"feed": self.feed, "status": self.status, "fetched": self.fetched}
        if self.reason:
            out["reason"] = clip(self.reason, 200)
        out["observed_at"] = self.observed_at or datetime.now(UTC).isoformat()
        return out


def rc_error_envelope(stdout: str) -> str | None:
    """`{"error": {"code": …, "message": …}}` as rc's whole stdout → short message, else None."""
    text = (stdout or "").strip()
    if not text.startswith("{") or '"error"' not in text[:20]:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not (isinstance(doc, dict) and set(doc) == {"error"} and isinstance(doc["error"], dict)):
        return None
    err = doc["error"]
    return f"{err.get('code') or 'ERROR'}: {err.get('message') or ''}".strip()


@dataclass
class Rc:
    """rc CLI wrapper: on-disk raw cache keyed by argv, one retry, never fatal."""

    raw_dir: Path
    cwd: Path
    refresh: bool = False
    offline: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- bookkeeping ------------------------------------------------------

    def note_error(self, feed: str, key: str, message: str) -> None:
        with self._lock:
            self.errors.append({"feed": feed, "key": key, "error": clip(message, 300)})

    def note_coverage(self, record: Coverage) -> None:
        with self._lock:
            self.coverage.append(record)

    # -- raw --------------------------------------------------------------

    def _path(self, args: Sequence[str], suffix: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "-".join(args))
        if len(safe) > 110:
            import hashlib

            safe = safe[:100] + "-" + hashlib.sha1(safe.encode()).hexdigest()[:8]
        return self.raw_dir / f"{safe}.{suffix}"

    def raw(self, args: Sequence[str], suffix: str = "json", timeout: int = 300) -> str | None:
        path = self._path(args, suffix)
        if path.exists() and not self.refresh:
            return path.read_text(encoding="utf-8")
        if self.offline:
            self.note_error("offline", " ".join(args), "not in cache")
            return None
        command = [_cli(), *args]
        last = ""
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            with self._lock:
                self.calls += 1
            try:
                done = subprocess.run(
                    command, capture_output=True, text=True, timeout=timeout, cwd=self.cwd
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            # rc prints transport failures as a JSON error envelope on stdout with exit 0
            # (`{"error":{"code":"NETWORK_ERROR",…}}`). Never cache it as a payload: on
            # 2026-09-23 thirteen cached resets made 0 runs look like a quiet day.
            envelope = rc_error_envelope(done.stdout)
            if envelope:
                last = envelope
                continue
            # `fleet health` exits non-zero *and* prints a valid payload when unhealthy.
            if done.stdout.strip():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(done.stdout, encoding="utf-8")
                return done.stdout
            if done.returncode == 0:
                return ""
            last = (done.stderr or f"exit {done.returncode}").strip()
        self.note_error("rc", " ".join(args), f"{' '.join(command[:5])}: {clip(last, 200)}")
        return None

    def _spilled(self, payload: Any) -> str | None:
        """`--format agent` can spill to a file and hand back an envelope pointing at it."""
        if isinstance(payload, dict) and payload.get("spilled") and payload.get("path"):
            candidate = Path(str(payload["path"]))
            if not candidate.is_absolute():
                candidate = self.cwd / candidate
            if candidate.exists():
                return candidate.read_text(encoding="utf-8", errors="replace")
            self.note_error("rc", str(payload["path"]), "spill file missing")
        return None

    # -- typed ------------------------------------------------------------

    def json(self, *args: str, timeout: int = 300) -> Any:
        raw = self.raw([*args, "-o", "json", "--raw-output"], "json", timeout)
        if not raw:
            return None
        docs = json_objects(raw)
        if not docs:
            self.note_error("rc", " ".join(args), "no JSON in output")
            return None
        return docs[0]

    def json_docs(self, *args: str, timeout: int = 300) -> list[Any]:
        raw = self.raw([*args, "-o", "json", "--raw-output"], "json", timeout)
        return json_objects(raw) if raw else []

    def jsonl(self, *args: str, timeout: int = 300) -> list[dict[str, Any]]:
        raw = self.raw([*args, "-o", "json", "--raw-output"], "jsonl", timeout)
        if not raw:
            return []
        out = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    def agent_text(self, *args: str, timeout: int = 300) -> str:
        """`--format agent --raw-output`, following a spill envelope to its file."""
        raw = self.raw([*args, "--format", "agent", "--raw-output"], "txt", timeout)
        if not raw:
            return ""
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            docs = json_objects(raw)
            if docs:
                followed = self._spilled(docs[0])
                if followed is not None:
                    return followed
        return raw


def parallel(items: Sequence[Any], worker: Callable[[Any], Any], workers: int = MAX_WORKERS) -> list[Any]:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(worker, items))


def guarded(rc: Rc, feed: str, key_of: Callable[[Any], str], worker: Callable[[Any], Any]):
    """One unparseable item must not take the whole collection down."""

    def run(item: Any) -> Any:
        try:
            return worker(item)
        except Exception as exc:  # noqa: BLE001 - recorded as a collection error
            rc.note_error(feed, key_of(item), f"{type(exc).__name__}: {exc}")
            return None

    return run


# ------------------------------------------------------------------- brain / project


BRAIN_MARKERS = ("projection.yaml", "skills", "AGENTS.md")


def find_brain_root(start: Path | None = None) -> Path:
    """Nearest ancestor of cwd that looks like a brain checkout. Never `__file__` parents:
    the kit is installed as a symlink, so `__file__` points at the kit, not the brain."""
    cursor = (start or Path.cwd()).resolve()
    for candidate in [cursor, *cursor.parents]:
        if not (candidate / ".git").exists():
            continue
        if any((candidate / marker).exists() for marker in BRAIN_MARKERS):
            return candidate
    return cursor


def project_name(brain_root: Path, rc_status: dict[str, Any] | None = None) -> str:
    if rc_status and rc_status.get("project"):
        return str(rc_status["project"])
    toml_path = _marker(brain_root)
    if toml_path.exists():
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        if data.get("project"):
            return str(data["project"])
    name = brain_root.name
    for prefix in ("replypen-brain-", "rootcause-brain-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def auth_status(cwd: Path) -> dict[str, Any]:
    try:
        done = subprocess.run(
            [_cli(), "auth", "status", "-o", "json"], capture_output=True, text=True, timeout=60, cwd=cwd
        )
        docs = json_objects(done.stdout)
        return docs[0] if docs and isinstance(docs[0], dict) else {}
    except (OSError, subprocess.TimeoutExpired):
        return {}


def rc_version(cwd: Path) -> str:
    try:
        done = subprocess.run([_cli(), "--version"], capture_output=True, text=True, timeout=30, cwd=cwd)
        return done.stdout.strip().splitlines()[0] if done.stdout.strip() else "unknown"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "unknown"


# --------------------------------------------------------------------- overlay


HOOK_NAMES = ("classify_run", "channel_of", "drill")


@dataclass
class Overlay:
    """`<brain>/_internal/fleet-report/`: config.toml + optional overlay.py. Fail-soft by design.

    Hooks (all optional, all called defensively):
      classify_run(run) -> list[str]        extra tags for one run
      channel_of(run) -> str | None         axis key on a tenantless project
      drill(run, ctx) -> str | None         markdown detail block, appended by drill.py under
                                            "## Project follow-up"; see drill.py for the ctx keys
    """

    root: Path | None = None
    config: dict[str, Any] = field(default_factory=dict)
    module: ModuleType | None = None
    problems: list[str] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key)
        return default if value is None else value

    def hook(self, name: str) -> Callable[..., Any] | None:
        func = getattr(self.module, name, None) if self.module else None
        return func if callable(func) else None

    def call(self, name: str, *args: Any, default: Any = None) -> Any:
        func = self.hook(name)
        if func is None:
            return default
        try:
            return func(*args)
        except Exception as exc:  # noqa: BLE001 - an overlay bug must not lose the report
            problem = f"overlay.{name}: {type(exc).__name__}: {exc}"
            if problem not in self.problems:
                self.problems.append(problem)
            return default


def load_overlay(brain_root: Path, override: str | None = None) -> Overlay:
    root = Path(override).expanduser().resolve() if override else brain_root / "_internal" / "fleet-report"
    if override and not root.is_dir():
        raise ValueError(f"Overlay directory does not exist: {root}")
    overlay = Overlay(root=root if root.exists() else None)
    config_path = root / "config.toml"
    if config_path.exists():
        try:
            overlay.config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            overlay.problems.append(f"config.toml: {exc}")
    module_path = root / "overlay.py"
    if module_path.exists():
        # Never leave a __pycache__ behind: the overlay lives inside a tracked brain checkout.
        bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec = importlib.util.spec_from_file_location("fleet_report_overlay", module_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules["fleet_report_overlay"] = module
                spec.loader.exec_module(module)
                overlay.module = module
        except Exception as exc:  # noqa: BLE001 - import errors are reported, not fatal
            overlay.problems.append(f"overlay.py: {type(exc).__name__}: {exc}")
        finally:
            sys.dont_write_bytecode = bytecode
    return overlay


# ------------------------------------------------------------- owner language


DEFAULT_OWNER_LANG = "en"

# `persona.language` is free prose written by the operator ("Nederlands, altijd — …"), not a code.
# Earliest-matching name wins, so a sentence that mentions a second language later still resolves
# to the one it opens with. Bare codes are only honoured when they are the whole setting: "en" is
# also a Dutch word.
_LANGUAGE_NAMES: dict[str, tuple[str, ...]] = {
    "nl": ("nederlands", "vlaams", "dutch", "flemish", "neerlandais", "niederlandisch"),
    "en": ("english", "engels", "anglais", "englisch"),
    "fr": ("frans", "francais", "français", "french", "franzosisch"),
    "de": ("duits", "deutsch", "german", "allemand"),
    "es": ("spaans", "espanol", "español", "spanish", "espagnol"),
    "it": ("italiaans", "italiano", "italian", "italien"),
    "pt": ("portugees", "portugues", "português", "portuguese"),
    "pl": ("pools", "polski", "polish"),
    "tr": ("turks", "turkce", "türkçe", "turkish"),
    "da": ("deens", "dansk", "danish"),
    "sv": ("zweeds", "svenska", "swedish"),
    "no": ("noors", "norsk", "norwegian"),
    "fi": ("fins", "suomi", "finnish"),
}


def lang_code(value: Any) -> str:
    """Normalise an explicit language code ('NL-be', ' EN ') to two lowercase letters."""
    return str(value or "").strip().lower()[:2]


def language_code(setting: Any) -> str | None:
    """`persona.language` prose -> ISO 639-1, or None when it names no language we know.

    Empty setting means English (the host's own contract), so it resolves rather than fails."""
    text = str(setting or "").strip()
    if not text:
        return DEFAULT_OWNER_LANG
    if len(text) <= 5 and re.fullmatch(r"[A-Za-z]{2}([_-][A-Za-z]{2,3})?", text):
        return text[:2].lower()
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for code, names in _LANGUAGE_NAMES.items():
        for name in names:
            at = lowered.find(name)
            if at >= 0 and (best is None or at < best[0]):
                best = (at, code)
    return best[1] if best else None


def _persona_language(payload: Any) -> Any:
    """`persona.language` out of an `rc … settings get` envelope (resolved, provenance-wrapped)."""
    if not isinstance(payload, dict):
        return None
    for section in (payload.get("resolved"), payload.get("settings"), payload):
        persona = (section or {}).get("persona") if isinstance(section, dict) else None
        if not isinstance(persona, dict):
            continue
        value = persona.get("language")
        if isinstance(value, dict):
            value = value.get("value")
        if value:
            return value
    return None


def owner_languages(
    rc: Rc,
    members: Sequence[str],
    tenants_by_member: dict[str, Sequence[str]] | None = None,
    override: Any = None,
) -> tuple[str, dict[str, str]]:
    """Resolve the owner half's language from the host, never from a hardcoded default.

    Project language = the first member's `persona.language`; a tenant inherits it unless its own
    settings resolve differently. `override` (overlay `[owner].lang`) wins everywhere. Unreadable
    settings fail soft to English and leave a coverage record, so the gap shows in the report."""
    forced = lang_code(override)
    if forced:
        by_tenant = {slug: forced for slugs in (tenants_by_member or {}).values() for slug in slugs}
        rc.note_coverage(Coverage("owner_language", "complete", 1 + len(by_tenant),
                                  f"overlay [owner].lang={forced} overrides persona.language"))
        return forced, by_tenant

    unresolved: list[str] = []

    def resolve(label: str, *args: str) -> str | None:
        payload = rc.json(*args)
        if payload is None:
            unresolved.append(f"{label}: settings unreadable")
            return None
        setting = _persona_language(payload)
        if setting is None:
            # No resolved persona.language at this scope: a tenant then inherits the project,
            # and the project itself lands on the host's English floor.
            unresolved.append(f"{label}: no resolved persona.language")
            return None
        code = language_code(setting)
        if code is None:
            unresolved.append(f"{label}: persona.language names no known language")
        return code

    project_lang = DEFAULT_OWNER_LANG
    for member in members:
        code = resolve(member, "--project", member, "--scope", "project",
                       "project", "settings", "behavior", "get")
        if code:
            project_lang = code
            break

    by_tenant: dict[str, str] = {}
    for member, slugs in (tenants_by_member or {}).items():
        for slug in slugs:
            if not slug:
                continue
            code = resolve(f"{member}/{slug}", "--project", member, "--scope", "project",
                           "project", "tenant", "settings", "get", slug)
            by_tenant[slug] = code or project_lang

    rc.note_coverage(Coverage(
        "owner_language", "complete" if not unresolved else "partial", 1 + len(by_tenant),
        None if not unresolved else "; ".join(unresolved[:4])
        + f" — tenants fall back to the project language, the project to {DEFAULT_OWNER_LANG}",
    ))
    return project_lang, by_tenant


# ------------------------------------------------------------------- recurrence


def recurrence(
    ledger: dict[str, dict[str, Any]],
    signature: str,
    kind: str,
    title: str,
    focus_count: int,
    context_count: int,
    first_obs: str | None,
    last_obs: str | None,
) -> dict[str, Any]:
    """Fold this window's observations into prior queue dates. Absence != resolution."""
    prior = ledger.get(signature) or {}
    first_seen = min([t for t in (prior.get("first_seen"), first_obs) if t], default=first_obs)
    last_seen = max([t for t in (prior.get("last_seen"), last_obs) if t], default=last_obs)
    # State is about *this window* only: `recurring` means the focus day repeated something the
    # context days already showed. A prior ledger entry does not make it recurring — that is what
    # `known_since` is for, and conflating the two made every focus-only signature look old.
    if focus_count:
        state = "recurring" if context_count else "new"
    else:
        state = "gone"
    entry = {
        "signature": signature,
        "kind": kind,
        "title": clip(title, 160),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "focus_count": focus_count,
        "context_count": context_count,
        "state": state,
    }
    if prior.get("first_seen") and (not first_obs or prior["first_seen"] < first_obs):
        entry["known_since"] = prior["first_seen"]
    elif prior.get("known_since"):
        entry["known_since"] = prior["known_since"]
    if prior.get("disposition"):
        entry["disposition"] = prior["disposition"]
    return entry


# ------------------------------------------------------- learning-plane helpers

_FALSE_DELTA = re.compile(
    r"identical|identiek|no (?:material |substantive )?change|unchanged|"
    r"niets(?:\s+\w+){0,4}\s+(?:gewijzigd|veranderd|aangepast)|"
    r"(?:antwoord|versie|tekst|inhoud)\w*(?:\s+\w+){0,3}\s+ongewijzigd|"
    r"geen(?:\s+\w+){0,2}\s+(?:wijziging|verschil|aanpassing)(?!\s+in\b)",
    re.I,
)
_NEGATED = re.compile(r"\b(?:niet|nauwelijks|amper|bijna|not|hardly)\s+(?:\w+\s+){0,2}$", re.I)
_CONTRAST = re.compile(r"^[^.]{0,80}?\b(?:maar|behalve|echter|but|except|however)\b", re.I)


def is_false_delta(delta: dict[str, Any]) -> bool:
    """The judge itself said the two versions are equivalent — quoted-thread/render noise."""
    text = str(delta.get("delta_description") or "")
    for match in _FALSE_DELTA.finditer(text):
        if _NEGATED.search(text[: match.start()]):
            continue
        if _CONTRAST.search(text[match.end():]):
            continue
        return True
    # markdown -> plaintext renders land in `other` with a high similarity score
    category = str(delta.get("delta_category") or "").lower()
    try:
        similarity = float(delta.get("similarity"))
    except (TypeError, ValueError):
        similarity = 0.0
    return category == "other" and similarity >= 0.75


def human_outcome(run: dict[str, Any], deltas: Sequence[dict[str, Any]], mode: str) -> str:
    """Draft fate ladder. In draft-mode projects a `sent_message_id` is a placed draft, not a send."""
    if not run.get("has_draft"):
        return "no_draft"
    real = [d for d in deltas if not d.get("false_delta")]
    if any(not d.get("shadow") for d in real):
        return "edited"
    if real:
        return "shadow_compared"
    drafts = run.get("drafts") or []
    if any(d.get("sent_message_id") for d in drafts) or deltas:
        return "sent_as_proposed"
    if mode == "shadow" or any(str(d.get("status")) == "suppressed" for d in drafts):
        return "shadow_compared"
    return "not_sent_yet"


# ------------------------------------------------------------------ trace header

TRACE_HEADER_KEYS = (
    "run_id", "tenant", "trigger", "scenario", "kind", "project", "status", "created_at",
    "brain_resolved", "context_schema_version", "grounding_source_drift_count", "error",
)


def reduce_trace_header(header: dict[str, Any]) -> dict[str, Any]:
    """Allowlist only. The raw header embeds the bootstrap turn and system prompt (~170 KB)."""
    out = {key: header.get(key) for key in TRACE_HEADER_KEYS if header.get(key) is not None}
    # The head of the inbound message: the only place where vendor/sender identity survives when
    # `topic` is the model's own summary (and errored runs have no topic at all). Clipped hard —
    # it is a classification handle for `classify_run`, not a body.
    if header.get("question"):
        out["question_head"] = clip(str(header["question"]), 600)
    guards = header.get("guards")
    if isinstance(guards, dict):
        out["guards"] = {
            key: guards.get(key)
            for key in ("egress", "final", "injection_scan", "principal_scope", "output_guard", "output_judge")
            if guards.get(key) is not None
        }
    grounding = header.get("grounding_sources")
    if isinstance(grounding, dict):
        sources = grounding.get("sources") or []
        out["grounding"] = {
            "captured": grounding.get("captured"),
            "reason": grounding.get("reason"),
            "mounts": [
                {
                    "name": src.get("name"),
                    "kind": src.get("kind"),
                    "mounted": src.get("mounted"),
                    "available": src.get("available"),
                    "state": src.get("state"),
                    "drift": src.get("drift"),
                }
                for src in sources
                if isinstance(src, dict)
            ],
        }
    metadata = header.get("metadata")
    if isinstance(metadata, dict):
        out["run_url"] = metadata.get("run_url")
        out["iterations"] = metadata.get("iterations")
        if metadata.get("draft_deferral") is not None:
            out["draft_deferral"] = metadata["draft_deferral"]
    return out


def unmounted_sources(header: dict[str, Any]) -> list[str]:
    """Configured-but-not-mounted grounding sources: the cleanest 'workspace incomplete' signal."""
    mounts = ((header.get("grounding") or {}).get("mounts")) or []
    return [
        str(m.get("name"))
        for m in mounts
        if m.get("mounted") is False and m.get("available") is not None
    ]


def env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).lower() in {"1", "true", "yes"}
