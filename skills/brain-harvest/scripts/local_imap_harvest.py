# /// script
# requires-python = ">=3.11"
# ///
"""Local IMAP sent-history exporter for brain-harvest.

Reads IMAP credentials from a local env file written by `rp project mailbox imap-env`, connects to the mailbox,
exports a capped sent-folder corpus, and writes:

  <out>/corpus/corpus.md   # v1 harvest blob, parseable by prepare_harvest.py
  <out>/INDEX.md           # human-readable index (backward-compat, one release)
  <out>/threads/*.md       # per-thread split (backward-compat, one release)

The `corpus/corpus.md` blob carries `harvest_format: v1` front-matter and the exact section shape the
server's canonical renderer emits (replypen/internal/export/harvest_render.go `render()`), so
`prepare_harvest.py` parses it directly instead of routing deep-IMAP harvests to the manual fallback.
The blob lives in its own `corpus/` subdir precisely so `prepare --corpus <out>/corpus/` never trips over
the non-front-mattered `INDEX.md` left at the top level for one deprecation release.

This v1 is intentionally conservative: it exports sent-folder messages grouped by RFC thread root or
normalized subject. It does not deep-expand every referenced inbound message across folders yet, so every
rendered message is mailbox-authored (expect `direction: mailbox_first` downstream, and no external-question
holdouts — a sent-only corpus cannot fill the default holdout reserve).
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import imaplib
import os
import re
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REQUIRED_ENV = {
    "RC_MAILBOX_ID",
    "RC_IMAP_EMAIL",
    "RC_IMAP_USERNAME",
    "RC_IMAP_PASSWORD",
    "RC_IMAP_HOST",
    "RC_IMAP_PORT",
    "RC_IMAP_TLS",
}

DEFAULT_SENT_NAMES = ("Sent", "Sent Items", "Sent Mail", "[Gmail]/Sent Mail", "Verzonden", "Verzonden items")


@dataclass(frozen=True)
class ParsedMessage:
    uid: str
    message_id: str
    thread_key: str
    subject: str
    date: datetime | None
    sender: str
    recipients: tuple[str, ...]
    text: str
    attachments: tuple[str, ...] = ()


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        values[key.strip()] = val
    missing = sorted(k for k in REQUIRED_ENV if not values.get(k))
    if missing:
        raise SystemExit(f"env file is missing required keys: {', '.join(missing)}")
    return values


def replypen_root(path: Path) -> Path | None:
    parts = path.parts
    for i, part in enumerate(parts):
        if part in (".replypen", ".rootcause"):
            return Path(*parts[: i + 1])
    return None


def ensure_replypen_gitignore(path: Path) -> None:
    root = replypen_root(path)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def connect(env: dict[str, str]) -> imaplib.IMAP4:
    host = env["RC_IMAP_HOST"]
    port = int(env["RC_IMAP_PORT"])
    tls = env.get("RC_IMAP_TLS", "implicit")
    timeout = int(env.get("RC_IMAP_TIMEOUT_SECONDS", "30"))
    if tls == "implicit":
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    else:
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        if tls == "starttls":
            conn.starttls(ssl.create_default_context())
        elif tls != "none":
            raise SystemExit(f"unsupported RC_IMAP_TLS={tls!r}")
    typ, data = conn.login(env["RC_IMAP_USERNAME"], env["RC_IMAP_PASSWORD"])
    if typ != "OK":
        raise SystemExit(f"IMAP login failed: {_safe_status(data)}")
    return conn


def _safe_status(data: object) -> str:
    text = repr(data)
    return text[:160]


def imap_quote(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def decode_folder_name(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if ' "/" ' in text:
        return text.rsplit(' "/" ', 1)[-1].strip().strip('"')
    if ' "." ' in text:
        return text.rsplit(' "." ', 1)[-1].strip().strip('"')
    return text.split()[-1].strip('"') if text.split() else text


def choose_sent_folder(conn: imaplib.IMAP4, requested: str | None) -> str:
    if requested:
        return requested
    typ, data = conn.list()
    candidates: list[str] = []
    if typ == "OK":
        for item in data or []:
            if isinstance(item, bytes):
                line = item.decode("utf-8", errors="replace")
                name = decode_folder_name(item)
                if "\\Sent" in line:
                    return name
                candidates.append(name)
    lower = {c.lower(): c for c in candidates}
    for name in DEFAULT_SENT_NAMES:
        if name.lower() in lower:
            return lower[name.lower()]
    return "Sent"


def search_uids(conn: imaplib.IMAP4, folder: str, max_messages: int) -> tuple[list[str], bool]:
    """Return the (most-recent) capped UID list plus whether the cap dropped older messages."""
    typ, _ = conn.select(imap_quote(folder), readonly=True)
    if typ != "OK":
        raise SystemExit(f"could not select sent folder {folder!r}")
    typ, data = conn.uid("search", None, "ALL")
    if typ != "OK":
        raise SystemExit("IMAP UID SEARCH failed")
    raw = b" ".join(part for part in data or [] if isinstance(part, bytes))
    uids = [u.decode("ascii", errors="ignore") for u in raw.split() if u]
    return uids[-max_messages:], len(uids) > max_messages


def fetch_message(conn: imaplib.IMAP4, uid: str) -> bytes | None:
    typ, data = conn.uid("fetch", uid, "(RFC822)")
    if typ != "OK":
        return None
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def normalize_message_id(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    match = re.search(r"<[^>]+>", value)
    return match.group(0) if match else value.split()[-1]


def thread_key_for(msg: email.message.EmailMessage, subject: str, uid: str) -> str:
    refs = [normalize_message_id(x) for x in msg.get_all("references", [])]
    in_reply_to = normalize_message_id(msg.get("in-reply-to", ""))
    mid = normalize_message_id(msg.get("message-id", ""))
    if refs and refs[0]:
        return refs[0]
    if in_reply_to:
        return in_reply_to
    if mid:
        return mid
    return "subject:" + normalize_subject(subject) + ":" + uid


def normalize_subject(subject: str) -> str:
    s = subject.strip()
    while True:
        new = re.sub(r"(?i)^\s*(re|fw|fwd)\s*:\s*", "", s).strip()
        if new == s:
            return s.lower()
        s = new


def header_addr_list(value: str) -> tuple[str, ...]:
    return tuple(addr.lower() for _, addr in email.utils.getaddresses([value]) if addr)


def message_text(msg: email.message.EmailMessage, max_chars: int) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = (part.get_content_disposition() or "").lower()
            if disp == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    parts.append(part.get_content())
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    elif msg.get_content_type() == "text/plain":
        try:
            parts.append(msg.get_content())
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[truncated]"
    return text


def message_attachments(msg: email.message.EmailMessage) -> tuple[str, ...]:
    """Attachment filenames only — we never export attachment bytes (mirrors harvest_render.go)."""
    names: list[str] = []
    if not msg.is_multipart():
        return ()
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disp == "attachment" or (filename and disp != "inline"):
            name = (filename or "attachment").strip()
            if name:
                names.append(re.sub(r"\s+", " ", name))
    return tuple(names)


def parse_message(uid: str, raw: bytes, max_chars: int) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(msg.get("subject", "")).strip() or "(no subject)"
    date: datetime | None = None
    if msg.get("date"):
        try:
            date = email.utils.parsedate_to_datetime(str(msg.get("date")))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            date = date.astimezone(timezone.utc)
        except Exception:
            date = None
    sender = (header_addr_list(str(msg.get("from", ""))) or ("",))[0]
    recipients = header_addr_list(", ".join(msg.get_all("to", []) + msg.get_all("cc", [])))
    mid = normalize_message_id(str(msg.get("message-id", "")))
    return ParsedMessage(
        uid=uid,
        message_id=mid,
        thread_key=thread_key_for(msg, subject, uid),
        subject=subject,
        date=date,
        sender=sender,
        recipients=recipients,
        text=message_text(msg, max_chars),
        attachments=message_attachments(msg),
    )


def slugify(text: str) -> str:
    text = normalize_subject(text)
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text or "thread")[:64].strip("-") or "thread"


def domain(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def date_str(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "-"


def month_str(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m") if dt else "unknown"


def escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# --- v1 corpus blob (parseable by prepare_harvest.py) ---------------------------------------
# The shape below is a byte-for-byte match of replypen/internal/export/harvest_render.go `render()`
# (HarvestFormatVersion "v1"), the reference renderer whose output prepare_harvest.py's
# parse_front_matter/split_sections/parse_section consume. Keep them in lockstep on any change.

HARVEST_FORMAT_VERSION = "v1"


def render_date(dt: datetime | None) -> str:
    # A message with no parseable Date still needs a yyyy-mm-dd or prepare_harvest's MSG_RE drops it.
    # Mirror Go's zero-time formatting ("0001-01-01") so the block round-trips as a real message.
    return dt.strftime("%Y-%m-%d") if dt else "0001-01-01"


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def thread_participants(group: list[ParsedMessage]) -> str:
    """Unique sender addresses in first-seen order (harvest_render.go threadParticipants)."""
    ordered: list[str] = []
    for msg in group:
        addr = msg.sender
        if addr and addr not in ordered:
            ordered.append(addr)
    return ", ".join(ordered)


def thread_span(group: list[ParsedMessage]) -> str:
    dates = [m.date for m in group if m.date]
    if not dates:
        return ""
    lo, hi = min(dates), max(dates)
    if lo == hi:
        return render_date(lo)
    return f"{render_date(lo)} → {render_date(hi)}"


def render_message_block(msg: ParsedMessage, mailbox: str) -> str:
    body = (msg.text or "").strip()
    if not body and not msg.attachments:
        return ""  # empty body + no attachments: skip so no dangling header (mirrors Go)
    frm = msg.sender or one_line(mailbox) or "unknown@local"
    out = f"\n**{frm} ({render_date(msg.date)}):**\n"
    if body:
        out += body + "\n"
    for name in msg.attachments:
        out += f"_[attachment: {name}]_\n"
    return out


def render_corpus_blob(mailbox: str, harvested_at: str, cleaned: bool, truncated: bool,
                       groups: list[list[ParsedMessage]]) -> str:
    parts = [
        "---\n"
        f"harvest_format: {HARVEST_FORMAT_VERSION}\n"
        f"mailbox: {one_line(mailbox)}\n"
        f"harvested_at: {harvested_at}\n"
        f"threads: {len(groups)}\n"
        f"cleaned: {'true' if cleaned else 'false'}\n"
        f"truncated: {'true' if truncated else 'false'}\n"
        "---\n"
    ]
    for idx, group in enumerate(groups, start=1):
        subject = one_line(group[0].subject) or "(no subject)"
        parts.append(f"\n## {subject} — #{idx}\n")
        participants = thread_participants(group)
        if participants:
            parts.append(f"**Participants:** {participants}\n")
        span = thread_span(group)
        if span:
            parts.append(f"**Span:** {span}\n")
        for msg in group:
            parts.append(render_message_block(msg, mailbox))
    return "".join(parts)


def group_messages(messages: Iterable[ParsedMessage]) -> list[list[ParsedMessage]]:
    groups: dict[str, list[ParsedMessage]] = {}
    for msg in messages:
        groups.setdefault(msg.thread_key, []).append(msg)
    out = list(groups.values())
    for group in out:
        group.sort(key=lambda m: (m.date or datetime.min.replace(tzinfo=timezone.utc), m.uid))
    out.sort(key=lambda g: (g[-1].date or datetime.min.replace(tzinfo=timezone.utc), g[-1].uid), reverse=True)
    return out


def write_output(out: Path, env: dict[str, str], folder: str, messages: list[ParsedMessage],
                 *, cleaned: bool = False, truncated: bool = False) -> None:
    ensure_replypen_gitignore(out)
    threads = out / "threads"
    threads.mkdir(parents=True, exist_ok=True)
    groups = group_messages(messages)
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()

    index_lines = [
        f"# Local IMAP harvest {env['RC_MAILBOX_ID']}",
        "",
        f"- mailbox: {env.get('RC_IMAP_EMAIL', '')}",
        f"- folder: {folder}",
        f"- exported_at: {now}",
        f"- messages: {len(messages)}",
        f"- threads: {len(groups)}",
        "- limitation: sent-folder messages only; inbound thread expansion is not included in v1.",
        "",
        "| date | participants | subject | msgs | file |",
        "|---|---|---|---|---|",
    ]

    for idx, group in enumerate(groups, start=1):
        first = group[0]
        last = group[-1]
        name = f"{month_str(last.date)}--{slugify(first.subject)}--{idx}.md"
        rel = f"threads/{name}"
        people = sorted({domain(first.sender)} | {domain(a) for msg in group for a in msg.recipients})
        people = [p for p in people if p]
        index_lines.append(
            f"| {date_str(last.date)} | {escape_cell(', '.join(people) or '-')} | "
            f"{escape_cell(first.subject)} | {len(group)} | {rel} |"
        )
        body = [
            "---",
            f"mailbox_id: {env['RC_MAILBOX_ID']}",
            f"thread: {idx}",
            f"source: imap-sent-local-v1",
            "---",
            "",
            f"# {first.subject}",
            "",
            f"- thread_key: `{first.thread_key}`",
            f"- messages: {len(group)}",
            f"- participants: {', '.join(people) or '-'}",
            "",
        ]
        for msg in group:
            body.extend(
                [
                    "---",
                    f"date: {date_str(msg.date)}",
                    f"from: {msg.sender or '-'}",
                    f"to: {', '.join(msg.recipients) or '-'}",
                    f"message_id: {msg.message_id or '-'}",
                    "",
                    msg.text or "[no text/plain body exported]",
                    "",
                ]
            )
        (threads / name).write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    out.mkdir(parents=True, exist_ok=True)
    (out / "INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    # The prepare-ready v1 blob. Its own subdir keeps `prepare --corpus <out>/corpus/` from scanning
    # the non-front-mattered INDEX.md (a top-level *.md) alongside it.
    corpus_dir = out / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    blob = render_corpus_blob(
        env.get("RC_IMAP_EMAIL", ""),
        now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        cleaned,
        truncated,
        groups,
    )
    (corpus_dir / "corpus.md").write_text(blob, encoding="utf-8")


def run_export(args: argparse.Namespace) -> int:
    env_path = Path(args.env)
    out = Path(args.out)
    env = parse_env(env_path)
    log(f"connecting to IMAP host for {env.get('RC_IMAP_EMAIL', '(mailbox)')}")
    conn = connect(env)
    try:
        folder = choose_sent_folder(conn, args.folder)
        log(f"selected sent folder: {folder}")
        uids, capped = search_uids(conn, folder, args.max_messages)
        log(f"fetching {len(uids)} sent messages (cap {args.max_messages})")
        parsed: list[ParsedMessage] = []
        for i, uid in enumerate(uids, start=1):
            raw = fetch_message(conn, uid)
            if raw is None:
                log(f"warning: skipped uid {uid} (fetch failed)")
                continue
            parsed.append(parse_message(uid, raw, args.max_body_chars))
            if i % 25 == 0 or i == len(uids):
                log(f"fetched {i}/{len(uids)}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    # cleaned=False: raw text/plain bodies are exported without stripping quoted history.
    body_truncated = any((m.text or "").rstrip().endswith("[truncated]") for m in parsed)
    write_output(out, env, folder, parsed, cleaned=False, truncated=capped or body_truncated)
    log(f"wrote {len(parsed)} messages -> {out} (corpus blob: {out / 'corpus' / 'corpus.md'})")
    print(out)
    return 0


def _fixture_message(uid: str, subject: str, date: str, body: str) -> tuple[str, bytes]:
    raw = (
        f"From: support@example.test\r\n"
        f"To: customer@example.org\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: <m{uid}@example.test>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode("utf-8")
    return uid, raw


def selftest() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = {
            "RC_MAILBOX_ID": "mb-test",
            "RC_IMAP_EMAIL": "support@example.test",
            "RC_IMAP_USERNAME": "user",
            "RC_IMAP_PASSWORD": "secret",
            "RC_IMAP_HOST": "imap.example.test",
            "RC_IMAP_PORT": "993",
            "RC_IMAP_TLS": "implicit",
        }
        messages = [
            parse_message(*_fixture_message("1", "Re: Invoice question", "Tue, 1 Apr 2025 10:00:00 +0000", "Thanks, here is the invoice."), max_chars=1000),
            parse_message(*_fixture_message("2", "Another subject", "Wed, 2 Apr 2025 10:00:00 +0000", "Second answer."), max_chars=1000),
        ]
        out = root / ".replypen" / "exports" / "selftest"
        write_output(out, env, "Sent", messages, cleaned=False, truncated=False)
        index = (out / "INDEX.md").read_text(encoding="utf-8")
        if "Invoice question" not in index or "threads:" not in index:
            print("selftest failed: INDEX.md missing expected content", file=sys.stderr)
            return 1
        if not (root / ".replypen" / ".gitignore").exists():
            print("selftest failed: .replypen/.gitignore missing", file=sys.stderr)
            return 1
        thread_files = list((out / "threads").glob("*.md"))
        if len(thread_files) != 2:
            print(f"selftest failed: thread file count {len(thread_files)}", file=sys.stderr)
            return 1
        blob = (out / "corpus" / "corpus.md").read_text(encoding="utf-8")
        if not blob.startswith("---\nharvest_format: v1\n") or "## " not in blob:
            print("selftest failed: corpus blob missing v1 front-matter/sections", file=sys.stderr)
            return 1
        if "**support@example.test (2025-04-01):**" not in blob:
            print("selftest failed: corpus blob missing rendered message block", file=sys.stderr)
            return 1
    print("selftest ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a capped local IMAP sent-history corpus.")
    parser.add_argument("--env", required=False, help="env file from `rp project mailbox imap-env`")
    parser.add_argument("--out", required=False, help="output dir, usually .replypen/exports/<run-id>/")
    parser.add_argument("--folder", help="sent folder name override")
    parser.add_argument("--max-messages", type=int, default=200, help="max sent messages to fetch (default 200)")
    parser.add_argument("--max-body-chars", type=int, default=16000, help="max text chars per message")
    parser.add_argument("--selftest", action="store_true", help="run fixture-based selftest and exit")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.env or not args.out:
        parser.error("--env and --out are required unless --selftest is used")
    if args.max_messages < 1 or args.max_messages > 5000:
        parser.error("--max-messages must be between 1 and 5000")
    if args.max_body_chars < 1000 or args.max_body_chars > 200000:
        parser.error("--max-body-chars must be between 1000 and 200000")
    return run_export(args)


if __name__ == "__main__":
    raise SystemExit(main())
