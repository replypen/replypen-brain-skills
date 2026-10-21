"""The run's own conversation, read by code: what did WE last write to this person?

The host stages a machine-readable duplicate of the conversation the main model sees at
`/tmp/rc-conversation.jsonl` (schema `rootcause.conversation.v1`, rewritten before every turn). One
JSON object per line; the rows this module reads are `{"type": "message", "role": "sender"|"mailbox",
"phase": "prior"|"current", "sender": ..., "sent_at": ..., "body": ...}` with a PII-masked `body`.
`mailbox` = written by the support side (a human staff member or a sent ReplyPen draft).

Use it when a script must check the model against what staff already told the customer, e.g. an
action preflight comparing its preview with an amount a staff member offered. **The expected value
comes from the staff text, extracted by code — never from a model-written parameter**, which would
only check the model against itself.

**Unavailable is not "nothing said".** The file exists only inside a run's workspace (and a mid-loop
preflight there); it is absent in a hosted action container at execution time and in a bare
`rp dev console`. A missing or unparseable file raises `ConversationUnavailable`, so a caller can never
mistake it for "staff promised nothing".

    from lib import conversation
    try:
        last = conversation.last_outbound()        # None = no outbound message in this thread
    except conversation.ConversationUnavailable:
        last = None                                # stay silent: we could not look
    if last:
        conversation.euro_amounts(last["body"])    # [7500, 1000]

Tests/local runs point at a fixture with ``RC_CONVERSATION_PATH``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

DEFAULT_PATH = "/tmp/rc-conversation.jsonl"
ENV_VAR = "RC_CONVERSATION_PATH"


class ConversationUnavailable(RuntimeError):
    """The conversation file is absent or unreadable: nothing can be concluded from it."""


def _path(path: str | None) -> str:
    return path or os.environ.get(ENV_VAR, "").strip() or DEFAULT_PATH


def messages(path: str | None = None) -> list[dict[str, Any]]:
    """Every `type:"message"` row in conversation order. Raises `ConversationUnavailable`."""
    target = _path(path)
    try:
        with open(target, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise ConversationUnavailable(f"{target}: {type(exc).__name__}") from exc
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ConversationUnavailable(f"{target}:{number}: not JSON") from exc
        if not isinstance(row, dict):
            raise ConversationUnavailable(f"{target}:{number}: not an object")
        rows.append(row)
    if not rows or rows[0].get("type") != "manifest":
        raise ConversationUnavailable(f"{target}: no manifest row")
    return [row for row in rows if row.get("type") == "message"]


# Mail clients quote the previous message under an attribution line ("Op ma 20 okt 2026 om 10:02
# schreef X <x@y>:", sometimes wrapped over two lines), an Outlook header block (Van:/Verzonden:) or a
# separator. An attribution line must END in ":" and carry an address, a year or a time: staff prose
# like "Op je vorige mail schreef ik al dat …" is new text, not a quote header.
_QUOTE_HEADER = re.compile(
    r"^[ \t]*(?:(?:Op|On|Le|Am)\b[^\n]*(?:\n[^\n]*)?\b(?:schreef|wrote|a écrit|schrieb)\b[^\n]*(?:\n[^\n]*)?:[ \t]*$"
    r"|(?:Van|From):[^\n]*\n(?:[^\n]*\n)?(?:Verzonden|Sent|Datum|Date):"
    r"|-{2,}\s*(?:Oorspronkelijk bericht|Original Message|Message d'origine)\s*-{2,}"
    r"|_{10,}[ \t]*$)",
    re.I | re.M,
)
_ATTRIBUTION_EVIDENCE = re.compile(r"@|\b\d{4}\b|\b\d{1,2}:\d{2}\b")


def strip_quoted(body: str) -> str:
    """The new text of one mail body: drops the quoted history tail and every `>` line."""
    text = str(body or "")
    for match in _QUOTE_HEADER.finditer(text):
        header = match.group(0)
        if not re.match(r"\s*(?:Op|On|Le|Am)\b", header, re.I) or _ATTRIBUTION_EVIDENCE.search(header):
            text = text[: match.start()]
            break
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()


def last_outbound(path: str | None = None) -> dict[str, Any] | None:
    """The latest `mailbox` message with its quoted history stripped (`body`; `raw_body` keeps the
    original), or None when the thread has no outbound message. Raises `ConversationUnavailable`."""
    for row in reversed(messages(path)):
        if row.get("role") == "mailbox":
            out = dict(row)
            out["raw_body"] = str(row.get("body") or "")
            out["body"] = strip_quoted(out["raw_body"])
            return out
    return None


# "€75", "€ 1.234,50", "€1,234.50", "75 euro", "75,00 EUR", "€75,-", "75€". The whole numeric token
# is captured (never a prefix: "€1,234.50" must not read as €1,23); `_cents` takes a trailing 1-2 digit
# group as the decimals and every other separator as thousands grouping.
_NUM = r"\d{1,3}(?:[.,\u00a0]\d{3})+(?:[.,]\d{1,2}|,-)?|\d+(?:[.,]\d{1,2}|,-)?"
_END = r"(?![.,]?\d)"
_EURO = re.compile(
    rf"(?:€|\bEUR\b)\s?(?P<a>{_NUM}){_END}|(?<![\d.,])(?P<b>{_NUM}){_END}\s?(?:€|\beuros?\b|\bEUR\b)",
    re.I,
)


def _cents(raw: str) -> int | None:
    match = re.fullmatch(r"(?P<whole>\d[\d.,\u00a0]*?)(?:[.,](?P<fraction>\d{1,2}))?", raw.removesuffix(",-"))
    if not match:
        return None
    return int(re.sub(r"\D", "", match["whole"])) * 100 + int(((match["fraction"] or "") + "00")[:2])


def euro_spans(text: str) -> list[tuple[int, int, int]]:
    """Every euro amount in `text` as `(cents, start, end)` character offsets, in text order."""
    out = []
    for match in _EURO.finditer(str(text or "")):
        cents = _cents(match.group("a") or match.group("b"))
        if cents is not None:
            out.append((cents, match.start(), match.end()))
    return out


def euro_amounts(text: str) -> list[int]:
    """Every euro amount in `text` in integer cents, in text order."""
    return [cents for cents, _, _ in euro_spans(text)]


__all__ = [
    "ConversationUnavailable",
    "euro_amounts",
    "euro_spans",
    "last_outbound",
    "messages",
    "strip_quoted",
]
