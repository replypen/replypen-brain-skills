"""Credential-free mailbox access for trusted chat and MCP runs.

The host owns credentials, scope checks and all provider work. This module only calls the per-run
``http://rc-broker.internal/mail/*`` surface; when the capability is absent it fails with one clear
sentence. Every write places a draft for human review — there is no send operation: never claim a
mail was sent; when asked to send, offer to place a draft instead. Every payload carries the mailbox
``slug`` and ``address``; name the mailbox address you searched or drafted in when answering.

``search`` returns the broker's whole envelope (``threads``, ``warnings``, ``since_days``, ``truncated``):
a per-mailbox failure arrives as a warning, so an empty ``threads`` list is only "no matches" when
``warnings`` is empty too.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib import _http_audit

BASE_URL = "http://rc-broker.internal/mail"
TIMEOUT = (10.0, 120.0)


class MailError(RuntimeError):
    pass


class MailUnavailable(MailError):
    pass


def mailboxes() -> list[dict]:
    return _request("GET", "/mailboxes").get("mailboxes", [])


def search(with_email: str | None = None, *, subject: str | None = None, inbox: bool = False,
           since_days: int = 14, limit: int = 20, mailbox: str | None = None) -> dict:
    """Find threads by exact correspondent, subject substring and/or the real inbox (combinable)."""
    if not (with_email or subject or inbox):
        raise ValueError("search needs at least one of with_email, subject or inbox")
    body: dict = {"since_days": since_days, "limit": limit}
    if with_email:
        body["with"] = with_email
    if subject:
        body["subject"] = subject
    if inbox:
        body["inbox"] = True
    if mailbox:
        body["mailbox"] = mailbox
    return _request("POST", "/search", body)


def thread(ref: str) -> dict:
    return _request("GET", "/thread", params={"ref": ref})


def draft(*, body_markdown: str, to=(), cc=(), bcc=(), subject: str = "", reply: str = "",
          mailbox: str | None = None, key: str | None = None) -> dict:
    body = {
        "body_markdown": body_markdown,
        "to": list(to), "cc": list(cc), "bcc": list(bcc),
        "subject": subject, "reply": reply,
    }
    if mailbox:
        body["mailbox"] = mailbox
    if key:
        body["key"] = key
    return _request("POST", "/draft", body)


def drafts() -> list[dict]:
    return _request("GET", "/drafts").get("drafts", [])


def _request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    import requests

    try:
        response = _http_audit.request(
            method, BASE_URL + path, json_body=body, params=params, timeout=TIMEOUT,
            endpoint_template="/mail" + path,
        )
    except requests.exceptions.RequestException as exc:
        raise MailUnavailable("Mailbox access is not enabled for this run") from exc
    if not 200 <= response.status_code < 300:
        sentence = response.text.strip() or f"Mailbox request failed (HTTP {response.status_code})"
        raise MailError(f"HTTP {response.status_code}: {sentence}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MailError("Mailbox broker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MailError("Mailbox broker returned an invalid response")
    return payload


def _since_days(raw: str) -> int:
    raw = raw.strip().lower()
    if raw.endswith("d"):
        raw = raw[:-1]
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use days, for example 30d") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lib.mail")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mailboxes")
    find = sub.add_parser("search")
    find.add_argument("--with", dest="with_email", help="exact correspondent address")
    find.add_argument("--subject", help="subject substring (max 120 characters)")
    find.add_argument("--inbox", action="store_true", help="recent threads in the real inbox")
    find.add_argument("--since", type=_since_days, default=14)
    find.add_argument("--limit", type=int, default=20)
    find.add_argument("--mailbox")
    get = sub.add_parser("thread")
    get.add_argument("ref")
    place = sub.add_parser("draft")
    place.add_argument("--to", action="append", default=[])
    place.add_argument("--cc", action="append", default=[])
    place.add_argument("--bcc", action="append", default=[])
    place.add_argument("--subject", default="")
    place.add_argument("--reply", default="")
    place.add_argument("--body-file", required=True)
    place.add_argument("--mailbox")
    place.add_argument("--key")
    sub.add_parser("drafts")
    return parser


def _main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "mailboxes":
            value = mailboxes()
        elif args.command == "search":
            if not (args.with_email or args.subject or args.inbox):
                parser.error("search needs --with, --subject or --inbox")
            value = search(args.with_email, subject=args.subject, inbox=args.inbox, since_days=args.since,
                           limit=args.limit, mailbox=args.mailbox)
        elif args.command == "thread":
            value = thread(args.ref)
        elif args.command == "draft":
            value = draft(body_markdown=Path(args.body_file).read_text(encoding="utf-8"), to=args.to,
                          cc=args.cc, bcc=args.bcc, subject=args.subject, reply=args.reply,
                          mailbox=args.mailbox, key=args.key)
        else:
            value = drafts()
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        return 0
    except (MailError, OSError) as exc:
        parser.exit(1, f"mail: {exc}\n")


if __name__ == "__main__":
    _main()
