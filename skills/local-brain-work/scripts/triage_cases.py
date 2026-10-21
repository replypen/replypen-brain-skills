# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Lint a brain's triage case corpus (`_internal/triage/cases/*.md`) exactly as ReplyPen parses it.

Mirror of replypen `internal/triageeval/cases.go` (ParseCase/LoadCorpus): keep regexes, caps and
messages in sync; both repos carry the same `testdata/{valid,invalid}` fixtures. The host refuses the
WHOLE corpus when one file is invalid, so every finding here is a FAIL, except one NOTICE: cases a run
can read (the host hides `_internal/` only when `.replypenignore`/`.rcignore` lists it), since the
main agent would then see the answer key.

    uv run --no-project --with pyyaml python triage_cases.py [--brain DIR]   # prints findings, exit 1 on FAIL
    uv run --no-project --with pyyaml python triage_cases.py --json           # machine output for brain_hygiene

Checks structure only (schema, ids, order, expectations, bodies), never prose style or whether the
text is truly de-identified: that stays a human review.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from email.utils import parseaddr
from pathlib import Path

CASES_DIR = "_internal/triage/cases"
MAX_CASES = 60
MAX_MESSAGES = 30
MAX_BODY_BYTES = 20 << 10
MAX_CASE_BYTES = 96 << 10
STATUSES = ("proposed", "approved")
ATTACHMENTS = ("media", "calendar", "document")
DIRECTIONS = ("inbound", "outbound")

CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
MESSAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CHANNEL_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
SECTION_RE = re.compile(r"^## ([a-z0-9][a-z0-9-]{0,31})[ \t]*$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

TOP_KEYS = {"id", "subject", "status", "expect", "why", "tags", "channel", "messages"}
EXPECT_KEYS = {"process", "needs_action", "needs_patient_text"}
MESSAGE_KEYS = {"id", "direction", "from", "to", "date", "html", "spam", "headers", "attachments"}


class CaseError(ValueError):
    pass


# yaml.v3 semantics for the host's typed fields: string fields take any scalar's source text, bool
# fields take the YAML 1.2 core booleans plus the 1.1 compatibility words, null is the zero value.
TRUE = {"true", "True", "TRUE", "y", "Y", "yes", "Yes", "YES", "on", "On", "ON"}
FALSE = {"false", "False", "FALSE", "n", "N", "no", "No", "NO", "off", "Off", "OFF"}
NULL = {"", "~", "null", "Null", "NULL"}


def _loader():
    import yaml

    class Strict(yaml.BaseLoader):
        """Every scalar stays its source text (like yaml.v3 into a string field); duplicate keys fail
        (yaml.v3 refuses them)."""

        def construct_mapping(self, node, deep=False):
            seen: set[str] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise yaml.constructor.ConstructorError(None, None, f"mapping key {key!r} already defined",
                                                            key_node.start_mark)
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    return yaml, Strict


def _null(v: object) -> bool:
    return v is None or (isinstance(v, str) and v in NULL)


def _str(where: str, v: object) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise CaseError(f"{where}: must be a string")
    return "" if v in NULL else v


def _bool(where: str, v: object) -> bool | None:
    """None when absent or null (the host's nil *bool)."""
    if _null(v):
        return None
    if not isinstance(v, str):
        raise CaseError(f"{where}: {v!r} is not a boolean")
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    raise CaseError(f"{where}: {v!r} is not a boolean")


def _list(where: str, v: object) -> list:
    if _null(v):
        return []
    if not isinstance(v, list):
        raise CaseError(f"{where}: must be a list")
    return v


def _rfc3339(v: str) -> float | None:
    if not RFC3339_RE.match(v):
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:  # 2026-02-30T…: well-formed but not a calendar date
        return None


def _address_ok(raw: object) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    _, addr = parseaddr(raw.strip())
    addr = addr or raw.strip()
    at = addr.rfind("@")
    if at <= 0 or at == len(addr) - 1:
        return False
    return bool(addr[:at].split("+", 1)[0])


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise CaseError("missing `---` YAML frontmatter")
    body = text[4:]
    end = body.find("\n---\n")
    if end >= 0:
        return body[: end + 1], body[end + 5:]
    if body.endswith("\n---"):
        return body[:-4], ""
    raise CaseError("missing `---` YAML frontmatter")


def _unknown(where: str, data: dict, allowed: set[str]) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise CaseError(f"{where}: unknown field {extra[0]!r}")


def parse_case(rel: str, raw: bytes) -> dict:
    """Validate one case; returns the parsed frontmatter plus `bodies`. Raises CaseError."""
    if len(raw) > MAX_CASE_BYTES:
        raise CaseError(f"file exceeds {MAX_CASE_BYTES} bytes")
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    front, rest = _split_frontmatter(text)
    yaml, loader = _loader()
    try:
        data = yaml.load(front, Loader=loader)  # noqa: S506 — BaseLoader subclass, no object tags
    except yaml.YAMLError as exc:
        raise CaseError(f"frontmatter: {exc}") from None
    if not isinstance(data, dict):
        raise CaseError("frontmatter: not a mapping")
    _unknown("frontmatter", data, TOP_KEYS)

    cid = _str("id", data.get("id"))
    if not CASE_ID_RE.match(cid):
        raise CaseError(f"id {cid!r} must match {CASE_ID_RE.pattern}")
    stem = Path(rel).name.removesuffix(".md")
    if cid != stem:
        raise CaseError(f"id {cid!r} must equal the file name {stem!r}")
    if not _str("subject", data.get("subject")).strip():
        raise CaseError("subject is required")
    status = _str("status", data.get("status"))
    if status not in STATUSES:
        raise CaseError(f"status {status!r} must be proposed or approved")
    exp = data.get("expect")
    if exp is not None and not isinstance(exp, dict):
        raise CaseError("expect: must be a mapping")
    exp = exp or {}
    _unknown("expect", exp, EXPECT_KEYS)
    vals = {k: _bool(f"expect.{k}", exp.get(k)) for k in EXPECT_KEYS}
    if any(v is None for v in vals.values()):
        raise CaseError("expect needs process, needs_action and needs_patient_text (true|false)")
    if vals["process"] != (vals["needs_action"] or vals["needs_patient_text"]):
        raise CaseError("expect.process must be true exactly when needs_action or needs_patient_text is true")
    if not _str("why", data.get("why")).strip():
        raise CaseError("why is required: the human rationale behind the expected outcome")
    channel = _str("channel", data.get("channel"))
    if channel and not CHANNEL_RE.match(channel):
        raise CaseError(f"channel {channel!r} must match {CHANNEL_RE.pattern} (a source channel such as conversation, whatsapp or ticket)")
    for tag in _list("tags", data.get("tags")):
        tag = _str("tags", tag)
        if not TAG_RE.match(tag):
            raise CaseError(f"tag {tag!r} must match {TAG_RE.pattern}")

    msgs = _list("messages", data.get("messages"))
    if not 1 <= len(msgs) <= MAX_MESSAGES:
        raise CaseError(f"messages: want 1..{MAX_MESSAGES}, got {len(msgs)}")
    seen: set[str] = set()
    prev: float | None = None
    for i, m in enumerate(msgs):
        where = f"messages[{i}]"
        if not isinstance(m, dict):
            raise CaseError(f"{where}: not a mapping")
        _unknown(where, m, MESSAGE_KEYS)
        mid = _str(f"{where}.id", m.get("id"))
        if not MESSAGE_ID_RE.match(mid):
            raise CaseError(f"{where}: id {mid!r} must match {MESSAGE_ID_RE.pattern}")
        if mid in seen:
            raise CaseError(f"{where}: duplicate message id {mid!r}")
        seen.add(mid)
        m["id"] = mid
        direction = _str(f"{where}.direction", m.get("direction"))
        if direction not in DIRECTIONS:
            raise CaseError(f"{where}: direction {direction!r} must be inbound or outbound")
        m["direction"] = direction
        frm = _str(f"{where}.from", m.get("from"))
        if not _address_ok(frm):
            raise CaseError(f"{where}: from {frm!r} is not an email address")
        for to in _list(f"{where}.to", m.get("to")):
            to = _str(f"{where}.to", to)
            if not _address_ok(to):
                raise CaseError(f"{where}: to {to!r} is not an email address")
        date = _str(f"{where}.date", m.get("date"))
        stamp = _rfc3339(date)
        if stamp is None:
            raise CaseError(f"{where}: date {date!r} must be RFC 3339 (2026-10-06T09:00:00+02:00)")
        if prev is not None and stamp < prev:
            raise CaseError(f"{where}: date goes back in time; list messages oldest first")
        prev = stamp
        _bool(f"{where}.html", m.get("html"))
        if _bool(f"{where}.spam", m.get("spam")) and direction != "inbound":
            raise CaseError(f"{where}: spam applies to inbound messages only")
        headers = m.get("headers")
        if not _null(headers):
            if not isinstance(headers, dict):
                raise CaseError(f"{where}.headers: cannot unmarshal into map[string]string (must be a mapping)")
            for key, val in headers.items():
                _str(f"{where}.headers.{key}", val)
                if not key or key != key.strip().lower():
                    raise CaseError(f"{where}: header name {key!r} must be lowercase")
        for att in _list(f"{where}.attachments", m.get("attachments")):
            att = _str(f"{where}.attachments", att)
            if att not in ATTACHMENTS:
                raise CaseError(f"{where}: attachment {att!r} must be media, calendar or document")
    if msgs[-1]["direction"] != "inbound":
        raise CaseError("the last message is the current turn and must be inbound")

    ids = [m["id"] for m in msgs]
    bodies: dict[str, list[str]] = {}
    order: list[str] = []
    cur: list[str] | None = None
    for line in rest.split("\n"):
        hit = SECTION_RE.match(line)
        if hit and hit.group(1) in ids:
            if hit.group(1) in bodies:
                raise CaseError(f"duplicate body section `## {hit.group(1)}`")
            cur = bodies[hit.group(1)] = []
            order.append(hit.group(1))
            continue
        if cur is None:
            if line.strip():
                raise CaseError(f"text before the first `## <message id>` section: {line[:60]!r}")
            continue
        cur.append(line)
    for i, mid in enumerate(ids):
        if mid not in bodies:
            raise CaseError(f"message {mid!r} has no `## {mid}` body section")
        if order[i] != mid:
            raise CaseError(f"body sections must follow message order (found `## {order[i]}` where `## {mid}` belongs)")
        body = "\n".join(bodies[mid]).strip("\n")
        if len(body.encode()) > MAX_BODY_BYTES:
            raise CaseError(f"message {mid!r} body exceeds {MAX_BODY_BYTES} bytes")
    return data


def case_files(root: Path) -> list[str]:
    d = root / CASES_DIR
    if not d.is_dir():
        return []
    return sorted(f"{CASES_DIR}/{p.name}" for p in d.iterdir()
                  if p.is_file() and p.suffix == ".md" and p.name.lower() != "readme.md")


def run_visible(root: Path, rels: list[str]) -> list[str]:
    """Tracked case files no root ignore control hides from a run (host `treeview` semantics)."""
    hidden: set[str] = set()
    for control in (".replypenignore", ".rcignore"):
        if (root / control).is_file():
            proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "-c", "-i",
                                   f"--exclude-from={root / control}"], capture_output=True, text=True)
            hidden.update(p for p in proc.stdout.split("\0") if p)
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", CASES_DIR],
                             capture_output=True, text=True).stdout.split("\0")
    return [r for r in rels if r in tracked and r not in hidden]


def lint(root: Path) -> list[dict]:
    """Findings as {path, line, level, message}."""
    files = case_files(root)
    out: list[dict] = []
    if not files:
        return out
    if len(files) > MAX_CASES:
        out.append({"path": CASES_DIR, "line": None, "level": "FAIL",
                    "message": f"{len(files)} cases; the eval caps at {MAX_CASES}"})
    for rel in files:
        try:
            parse_case(rel, (root / rel).read_bytes())
        except CaseError as exc:
            out.append({"path": rel, "line": None, "level": "FAIL", "message": str(exc)})
    if run_visible(root, files):
        out.append({"path": CASES_DIR, "line": None, "level": "NOTICE",
                    "message": "cases are run-visible; add `/_internal/` to .replypenignore so a run never reads the answer key"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", default=".", help="brain checkout (default: cwd)")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    args = ap.parse_args(argv)
    findings = lint(Path(args.brain).resolve())
    if args.json:
        print(json.dumps(findings))
    else:
        for f in findings:
            print(f"{f['level']}  {f['path']}: {f['message']}")
        n = len(case_files(Path(args.brain).resolve()))
        print(f"triage cases: {n} file(s), {sum(f['level'] == 'FAIL' for f in findings)} FAIL")
    return int(any(f["level"] == "FAIL" for f in findings))


if __name__ == "__main__":
    sys.exit(main())
