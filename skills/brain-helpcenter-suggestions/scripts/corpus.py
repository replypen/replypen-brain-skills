# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pure normalisers for `brain-helpcenter-suggestions`: provider payload -> one conversation shape.

    from corpus import helpscout_conversations, trace_conversation, write_digest

Every entry point returns the `evidence.json` `conversations[]` contract (see suggestions_schema.md):
id, url, channel, tenant, created_at, subject, customer, first_message, first_raw, reply, later[],
noise, truncated, tags. Stdlib only, no I/O, no rc: collect.py owns the network, this owns the text.
"""

from __future__ import annotations

import html as html_mod
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

BODY_LIMIT, REPLY_LIMIT, LATER_LIMIT, LATER_MAX = 12000, 3000, 800, 12
MERGE_WINDOW_MIN = 60  # Beacon splits one chat; one runs thread can retrigger on the same mail
TICKET_MERGE_WINDOW_MIN = 3 * 24 * 60  # the same ticket submitted twice a couple of days apart
_ARTICLE_ID = re.compile(r"/articles?/([0-9a-f]{24}|\d+)")
_PATH_ID = re.compile(r"/([0-9a-f]{24}|\d+)-")
HS_URL = "https://secure.helpscout.net/conversation/{cid}/{number}/"
DROP_THREADS = {"note", "lineitem"}
CUSTOMER_THREADS = {"customer", "beaconchat"}

_BLOCK = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table)\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_QUOTED = re.compile(
    r"(?i)^\s*(>|on\b.{0,140}\bwrote:|op\b.{0,140}\bschreef|le\b.{0,140}\ba écrit|"
    r"-{2,}\s*original message|-{3,}\s*forwarded message|van:\s|from:\s|verzonden:\s)"
)
_SIGNATURE = re.compile(
    r"(?i)^\s*(--\s*$|__+\s*$|met vriendelijke groet|vriendelijke groet|met dank en vriendelijke|"
    r"mvg\b|m\.v\.g\b|groetjes|groeten\s*[,.]?\s*$|kind regards|best regards|met warme groet)"
)

# Turns that are chat furniture, not an answer: bot greetings, flow prompts, auto-replies.
BOILERPLATE = (
    "ik ben yumi", "i'm yumi", "closing this chat", "this conversation has been closed",
    "chat gesloten", "we're away right now", "automated message", "auto-reply", "out of office",
    "bedankt voor je bericht, we", "waar gaat je vraag over", "waar kunnen we je mee helpen",
    "how can we help you today", "antwoordt meestal binnen", "is morgen weer beschikbaar",
    "om verder te chatten", "to continue this chat",
)
GREETINGS = {"hallo", "hoi", "hey", "hi", "hello", "dag", "goeiemorgen", "goedemorgen",
             "goeiedag", "goedemiddag", "goedenavond", "bonjour", "salut", "hola"}
_NO_REPLY = re.compile(r"(?i)(noreply|no-reply|donotreply|mailer-daemon|notifications@)")
_TEST = re.compile(r"(?i)\b(test(je)?|hgh|ping)\b")
# Monitoring mail routed to the support inbox: Sentry, uptime pings, CI. Never a customer question.
_ALERT = re.compile(r"(?i)(sentry\.io/|\[sentry\]|uptimerobot|statuscake|betteruptime|pagerduty|is down\b|"
                    r"github\.com/.+/actions/runs/)")
_BOT_SENDER = re.compile(r"(?i)(operator\+[^@]*@intercom\.io|^fin@|noreply|no-reply|bot@)")
_INVITE_BODY = re.compile(r"(?i)(a new event has been scheduled|view event in calendly|invitee time zone"
                          r"|event name:.*\n.*invitee)")
_INVITE = re.compile(r"(?i)^\s*((updated |canceled |geannuleerd: )?invitation|uitnodiging)\s*:")


def clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + " …"


def words(text: Any) -> list[str]:
    return _WORD.findall(str(text or ""))


def _collapse(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(value: Any) -> str:
    """Body html -> plain text: block tags become newlines, the rest is dropped and unescaped."""
    text = _BLOCK.sub("\n", str(value or ""))
    text = html_mod.unescape(_TAG.sub(" ", text)).replace("\xa0", " ")
    return _collapse([re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()])


def strip_quoted_and_signature(text: Any) -> str:
    """Drop the quoted history tail (`Op … schreef`, `>` lines) and a short signature block."""
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if _QUOTED.match(line):
            lines = lines[:index]
            break
    for index in range(len(lines) - 1, -1, -1):
        tail = [line for line in lines[index + 1:] if line.strip()]
        # A signature is either short or made of signature-shaped lines (name, phone, address,
        # a bare url); a long prose paragraph after `Groeten` is a real message and must survive.
        if _SIGNATURE.match(lines[index]) and (
                len(tail) <= 6 or all(len(t) <= 60 or " " not in t.strip() for t in tail)):
            lines = lines[:index]
    return _collapse(lines)


def clean(value: Any, limit: int = BODY_LIMIT) -> str:
    return clip(_EMAIL.sub("[e-mail]", strip_quoted_and_signature(html_to_text(value))), limit)


def in_window(value: Any, start: datetime, end: datetime) -> bool:
    """`created_at` is provider-shaped (`…Z`, naive, or offset); unparseable falls out of scope."""
    return _moment(value) is not None and start <= _moment(value) < end  # type: ignore[operator]


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ turn semantics


def is_boilerplate(text: str) -> bool:
    """Bot greetings, flow prompts and canned closers, dropped from the conversation entirely."""
    tokens = words(text)
    # Only short turns: "out of office" inside a 50-word follow-up is context, not an auto-reply.
    if len(tokens) <= 40 and any(phrase in text.lower() for phrase in BOILERPLATE):
        return True
    if len(tokens) >= 6:
        return False
    return text.rstrip().endswith("!") or bool(tokens) and tokens[0].lower() in GREETINGS \
        and len(tokens) <= 3


def looks_like_question(text: str) -> bool:
    """The first customer turn is often a menu click or a salutation; the question has shape."""
    return "?" in text or len(words(text)) >= 8


def noise_reason(subject: Any, sender: Any, first_message: str, has_ics: bool = False) -> str | None:
    """Obvious not-KB traffic, pre-tagged so the judge does not spend its reading budget on it."""
    if not first_message.strip():
        return "empty"
    if has_ics or _INVITE.match(str(subject or "")) or _INVITE_BODY.search(first_message):
        return "calendar_invite"
    if _NO_REPLY.search(str(sender or "")):
        return "no_reply_sender"
    if len(words(first_message)) <= 6 and _TEST.search(first_message):
        return "test"
    if _ALERT.search(str(subject or "")) or _ALERT.search(first_message[:400]):
        return "alert_mail"
    return None


def build(cid, url, channel, created_at, subject, customer, tags, turns, provenance="human",
          tenant=None, has_ics=False):
    """turns = chronological [(role, text, by)]. Returns None when no customer turn survives."""
    turns = [(role, text, by) for role, text, by in turns if text and not is_boilerplate(text)]
    customer_at = [i for i, turn in enumerate(turns) if turn[0] == "customer"]
    if not customer_at:
        return None
    # The question, not the opener: chat widgets start with a placeholder or a menu click.
    ask = next((i for i in customer_at if looks_like_question(turns[i][1])), customer_at[0])
    first_message = turns[ask][1]
    reply, later = None, []
    for role, text, by in turns[ask + 1:]:
        if reply is None and role == "agent":
            reply = {"text": clip(text, REPLY_LIMIT), "by": by or None, "provenance": provenance}
        else:
            later.append({"role": role, "text": clip(text, LATER_LIMIT)})
    truncated = len(later) > LATER_MAX
    reason = noise_reason(subject, customer, first_message, has_ics)
    return {
        "id": cid, "url": url, "channel": channel, "tenant": tenant, "created_at": created_at,
        "subject": subject or None, "customer": customer or None,
        "first_message": first_message,
        "first_raw": turns[customer_at[0]][1] if ask != customer_at[0] else None,
        "reply": reply, "later": (later[:6] + later[-6:]) if truncated else later,
        "truncated": truncated, "linked_articles": [],
        "noise": {"verdict": "not_kb", "reason": reason} if reason else None,
        "tags": [str(t.get("tag") if isinstance(t, dict) else t) for t in tags or []],
    }


def merge_split_chats(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same customer, next conversation opened within MERGE_WINDOW_MIN: one conversation, first id/url."""
    out: list[dict[str, Any]] = []
    for conv in sorted(conversations, key=lambda c: str(c["created_at"])):
        prev = out[-1] if out else None
        if prev and conv.get("customer") and conv["customer"] == prev.get("customer") \
                and _gap(prev, conv) is not None and _gap(prev, conv) <= MERGE_WINDOW_MIN:
            turns = [{"role": "customer", "text": clip(conv["first_message"], LATER_LIMIT)}]
            turns += [{"role": "agent", "text": conv["reply"]["text"]}] if conv.get("reply") else []
            prev["later"] = prev["later"] + turns + conv["later"]
            prev["reply"] = prev.get("reply") or conv.get("reply")
            prev["tags"] = prev["tags"] + [f"merged:{conv['id']}"]
            continue
        out.append(conv)
    return out


def _merge_key(conv: dict[str, Any]) -> tuple[str, str, str]:
    """Chat has no sender, so the tenant keeps two organisations asking the same thing apart."""
    return (str(conv.get("customer") or ""), str(conv.get("tenant") or ""),
            " ".join(conv["first_message"].split()))


def merge_duplicates(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One inbound mail can trigger several runs: same sender + byte-identical question, one hour."""
    out: list[dict[str, Any]] = []
    for conv in sorted(conversations, key=lambda c: str(c["created_at"])):
        key = _merge_key(conv)
        twin = next((c for c in reversed(out)
                     if _merge_key(c) == key and (_gap(c, conv) or 1e9) <= (
                         TICKET_MERGE_WINDOW_MIN if conv.get("channel") == "ticket" else MERGE_WINDOW_MIN)), None)
        if twin is not None:
            twin["tags"] = twin["tags"] + [f"merged:{conv['id']}"]
            continue
        out.append(conv)
    return out


def _gap(prev: dict[str, Any], conv: dict[str, Any]) -> float | None:
    before, after = _moment(prev.get("created_at")), _moment(conv.get("created_at"))
    return None if before is None or after is None else (after - before) / timedelta(minutes=1)


def tag_duplicate_outreach(conversations: list[dict[str, Any]]) -> None:
    """The same pitch from several senders is cold outreach, never a help-centre question."""
    seen: dict[str, set[str]] = {}
    for conv in conversations:
        seen.setdefault(" ".join(conv["first_message"].split()), set()).add(
            str(conv.get("customer") or conv["id"]))
    for conv in conversations:
        if conv.get("noise") is None and len(seen[" ".join(conv["first_message"].split())]) > 1:
            conv["noise"] = {"verdict": "not_kb", "reason": "duplicate_outreach"}


_BARE_URL = re.compile(r"^\s*<?https?://\S+>?\s*$")
_ERROR_PASTE = re.compile(
    r"(?i)^\s*(traceback\b|\w*(error|exception)\s*[:(]|[a-z]\w*::\w|status\s+[45]\d\d\b|"
    r"http\s*[45]\d\d\b|\{\s*\"error\"|<!doctype|internal server error)")
REPEATED_PROMPT_MIN = 3


def tag_noise(conversations: list[dict[str, Any]], skip_tenants: list[str] | None = None) -> None:
    """Pre-tags the judge should not spend reading budget on. It may still override any of them."""
    skip = {str(t).strip() for t in skip_tenants or [] if str(t).strip()}
    repeated = Counter(" ".join(c["first_message"].split()) for c in conversations)
    for conv in conversations:
        first = conv["first_message"]
        reason = None
        if str(conv.get("tenant") or "") in skip:
            reason = "internal_tenant"  # operator probes and demos, not customers
        elif conv.get("noise"):
            continue
        elif repeated[" ".join(first.split())] >= REPEATED_PROMPT_MIN:
            reason = "repeated_prompt"
        elif _BARE_URL.match(first):
            reason = "bare_url"
        elif _ERROR_PASTE.match(first) and "?" not in first:
            reason = "error_paste"
        if reason:
            conv["noise"] = {"verdict": "not_kb", "reason": reason}


def link_articles(conversations: list[dict[str, Any]], articles: list[dict[str, Any]]) -> None:
    """Help-centre URLs the human pasted in a reply → `linked_articles` (proof the content exists)."""
    by_id: dict[str, str] = {}
    by_url: list[tuple[str, str]] = []
    for a in articles:
        url, path = str(a.get("url") or ""), str(a.get("path") or "")
        if url.startswith("http"):
            by_url.append((url.rstrip("/"), a["id"]))
        for match in (_ARTICLE_ID.search(url), _ARTICLE_ID.search(path), _PATH_ID.search(path)):
            if match:
                by_id.setdefault(match.group(1), a["id"])
                break
    for conv in conversations:
        human = conv.get("reply") and conv["reply"].get("provenance") == "human"
        texts = [conv["reply"]["text"]] + [t["text"] for t in conv["later"] if t["role"] == "agent"] \
            if human else []
        found = {by_id[m.group(1)] for text in texts for m in _ARTICLE_ID.finditer(text)
                 if m.group(1) in by_id}
        found |= {aid for url, aid in by_url if any(url in text for text in texts)}
        conv["linked_articles"] = sorted(found, key=lambda a: int(a[1:]))


# ------------------------------------------------------------------ providers


def helpscout_conversations(page_json: Any) -> list[dict[str, Any]]:
    """Accepts the raw API page (`_embedded.conversations[]`) and a flattened list of the same objects."""
    items = (((page_json.get("_embedded") or {}).get("conversations")) or []
             if isinstance(page_json, dict) else page_json or [])
    built = (_helpscout_one(c) for c in items if isinstance(c, dict))
    return [conv for conv in built if conv]


def _helpscout_one(conv: dict[str, Any]) -> dict[str, Any] | None:
    threads = (conv.get("_embedded") or {}).get("threads") or conv.get("threads") or []
    threads = sorted(threads, key=lambda t: str(t.get("createdAt") or t.get("at") or ""))
    turns, beacon = [], False
    for thread in threads:
        kind = str(thread.get("type") or "")
        if kind in DROP_THREADS:
            continue
        beacon = beacon or kind == "beaconchat"
        if kind in CUSTOMER_THREADS:
            role = "customer"
        elif kind == "message":
            role = "agent"
        else:
            continue
        by = thread.get("first") or (thread.get("createdBy") or {}).get("first")
        turns.append((role, clean(thread.get("body")), by))
    source = str((conv.get("source") or {}).get("type") or "")
    chat = str(conv.get("type") or "") == "chat" or (source.startswith("beacon") and beacon)
    primary = conv.get("primaryCustomer")
    customer = (primary.get("first") if isinstance(primary, dict) else None) or next(
        (by for role, _, by in turns if role == "customer" and by), None)
    return build(
        f"hs:{conv.get('id')}",
        HS_URL.format(cid=conv.get("id"), number=conv.get("number")),
        "chat" if chat else "email",
        str(conv.get("createdAt") or conv.get("created_at") or ""),
        conv.get("subject"), customer, conv.get("tags"), turns,
    )


def address(value: Any) -> str:
    """`Name <a@b.c>` / `a@b.c` / an opaque chat contact id -> a comparable lowercase key."""
    match = _EMAIL.search(str(value or ""))
    return (match.group(0) if match else str(value or "")).strip().strip("<>").lower()


CLARIFIER_ANSWER = "User selected:"
_CLARIFIER_PROMPT = re.compile(r"(?m)^Questions asked \(set\b")


def _fold_clarifiers(turns: list[tuple[str, str, Any]]) -> list[tuple[str, str, Any]]:
    """The clarifier form is one turn in the model's eyes, not a question and an answer.

    `User selected: doel=x` is what the customer clicked in the form the previous turn asked about:
    it belongs to that customer turn. The form itself is scaffolding: it is appended to the agent's
    answer (or is the whole turn), so cut it off rather than dropping a real answer with it.
    """
    out: list[tuple[str, str, Any]] = []
    for role, text, by in turns:
        if role == "agent":
            found = _CLARIFIER_PROMPT.search(text)
            text = text[:found.start()].rstrip() if found else text
            if not text:
                continue
        if role == "customer" and text.startswith(CLARIFIER_ANSWER):
            picked = text[len(CLARIFIER_ANSWER):].strip()
            last = next((i for i in range(len(out) - 1, -1, -1) if out[i][0] == "customer"), None)
            if last is not None and picked:
                out[last] = (out[last][0], f"{out[last][1]} [koos: {picked}]", out[last][2])
            continue
        out.append((role, text, by))
    return out


def chat_conversation(header: dict[str, Any]) -> dict[str, Any] | None:
    """A chat session: every inbound turn is the customer, every outbound one is our agent.

    No sender is carried on a chat turn, so there is nothing to attribute a human to: the reply is
    always the model's live answer (`provenance: draft`).
    """
    prior = [m for m in header.get("prior_messages") or [] if isinstance(m, dict)]
    turns = [("agent" if m.get("is_inbound") is False else "customer", clean(m.get("body")), None)
             for m in prior]
    turns.append(("customer", clean(header.get("question")), None))
    draft = clean(header.get("draft"), REPLY_LIMIT)
    if draft:
        turns.append(("agent", draft, None))
    return build(
        f"run:{str(header.get('run_id') or '')[:8]}",
        (header.get("metadata") or {}).get("run_url"), "chat",
        str(header.get("created_at") or ""), header.get("topic"), None, [],
        _fold_clarifiers(turns), "draft", tenant=header.get("tenant") or None,
    )


# ------------------------------------------------------------------ Embassy support tickets

_TICKET_TITLE = re.compile(r"(?m)^#\s*Support Ticket:\s*(.+?)\s*$")
_TICKET_SECTION = re.compile(r"(?m)^##\s+(.+?)\s*$")
_TICKET_ROW = re.compile(r"(?m)^\s*[-*]\s*([^:]{1,40}):\s*(.*)$")
# A discussion turn header: a short line with a date and (before or after it) the poster's name,
# under any markdown decoration. `26/8 - Marjan`, `### 26/08/26 Koen`, `**Sylvie 27/08/2026**`.
# Most tickets have none at all, and then the discussion is one turn.
_TICKET_NAME = r"[^\W\d_][\w.'-]*(?:\s+[^\W\d_][\w.'-]*)?"
_TICKET_DATE = r"\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?"
_TICKET_TURN = re.compile(rf"^(?:{_TICKET_NAME}\s+)?{_TICKET_DATE}(?:\s*[-–—:]?\s*{_TICKET_NAME})?$")
TICKET_TURN_MAX = 40
TICKET_NOT_KB_TYPES = {"feedback", "request"}  # feature wishes, never a help-centre gap


def _ticket_sections(question: str) -> dict[str, str]:
    """`## <name>` -> its body, for the markdown ticket the Embassy renders into `question`."""
    heads = list(_TICKET_SECTION.finditer(question))
    return {h.group(1).strip().lower(): question[h.end():(heads[i + 1].start()
                                                          if i + 1 < len(heads) else len(question))]
            for i, h in enumerate(heads)}


def _ticket_meta(body: str) -> dict[str, str]:
    return {k.strip().lower(): v.strip().strip("`").strip()
            for k, v in _TICKET_ROW.findall(body or "")}


def _first_word(value: str) -> str:
    """`low (Laag)` -> `low`; `Marjan (marjan@x.be) (is super admin)` -> `Marjan`."""
    return (words(str(value or "").partition("(")[0]) or [""])[0]


def _ticket_turns(discussion: str) -> list[str]:
    """The discussion split on dated turn headers; unsplit when there are none."""
    chunks, current = [], []
    for line in discussion.splitlines():
        bare = line.strip().strip("#*_ ").strip()
        if len(bare) <= TICKET_TURN_MAX and _TICKET_TURN.match(bare):
            chunks.append(current)
            current = []
            continue
        current.append(line)
    chunks.append(current)
    return [text for text in (clean("\n".join(c)) for c in chunks) if text]


def ticket_conversation(header: dict[str, Any]) -> dict[str, Any] | None:
    """An Embassy support ticket (`kind: analysis`): the question is a markdown ticket document.

    The admin who files a ticket is exactly the person who should have found a help-centre article,
    so the discussion is the customer turn and the agent's `draft` is the reply. Feature wishes
    (`feedback`, `request`) are pre-tagged noise: no article closes them.
    """
    question = str(header.get("question") or "")
    sections = _ticket_sections(question)
    meta = _ticket_meta(sections.get("metadata", ""))
    found = _TICKET_TITLE.search(question)
    title = found.group(1) if found else header.get("topic")
    ticket_type, priority = _first_word(meta.get("type")), _first_word(meta.get("priority"))
    tags = [f"ticket_type:{ticket_type}"] if ticket_type else []
    tags += [f"priority:{priority}"] if priority else []
    turns = [("customer", text, None) for text in _ticket_turns(sections.get("discussion", ""))]
    draft = clean(header.get("draft"), REPLY_LIMIT)
    conv = build(
        f"run:{str(header.get('run_id') or '')[:8]}",
        (header.get("metadata") or {}).get("run_url"), "ticket",
        str(header.get("created_at") or ""), title, _first_word(meta.get("created by")), tags,
        turns + ([("agent", draft, None)] if draft else []), "draft",
        tenant=str(header.get("tenant") or meta.get("tenant") or "") or None,
    )
    if conv and ticket_type in TICKET_NOT_KB_TYPES:
        conv["noise"] = conv.get("noise") or {"verdict": "not_kb", "reason": "ticket_type"}
    return conv


def trace_conversation(header: dict[str, Any],
                       mailbox_domains: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """First JSONL record of `rp run trace --stream` -> conversation (draft = the reply).

    `is_inbound` is *delivery direction*, not authorship: a sibling vendor cc'd on the thread also
    arrives inbound. Only the originating sender and our own mailbox domains are provable, the rest
    is `unknown`, and the validator refuses customer quotes from unknown turns.
    """
    if str(header.get("kind") or "") == "chat":
        return chat_conversation(header)
    if str(header.get("kind") or "") == "analysis" \
            or str(header.get("question") or "").lstrip().startswith("# Support Ticket"):
        return ticket_conversation(header)
    prior = [m for m in header.get("prior_messages") or [] if isinstance(m, dict)]
    origin = next((address(m.get("sender")) for m in prior if m.get("is_inbound") is not False), "")
    # An opaque chat contact id is the same person as the e-mail they hand over later in the widget.
    opaque = bool(origin) and "@" not in origin

    def role(message: dict[str, Any]) -> str:
        sender = address(message.get("sender"))
        if message.get("is_inbound") is False or sender.rpartition("@")[2] in mailbox_domains:
            return "agent"
        return "customer" if opaque or sender == origin else "unknown"

    turns = [(role(m), clean(m.get("body")), m.get("sender")) for m in prior]
    turns.append(("customer", clean(header.get("question")), None))
    provenance = "human" if any(r == "agent" for r, _, _ in turns) else "draft"
    draft = clean(header.get("draft"), REPLY_LIMIT)
    has_ics = any(str(a.get("filename") or a.get("mime_type") or "").lower().endswith(
        (".ics", "/calendar")) for m in prior for a in m.get("attachments") or [])
    conv = build(
        f"run:{str(header.get('run_id') or '')[:8]}",
        (header.get("metadata") or {}).get("run_url"), "email",
        str(header.get("created_at") or ""), header.get("topic"),
        next((m.get("sender") for m in prior if m.get("is_inbound") is not False), None),
        [], turns + ([("agent", draft, None)] if draft else []), provenance,
        tenant=header.get("tenant") or None, has_ics=has_ics,
    )
    if conv and conv.get("reply") and _BOT_SENDER.search(address(conv["reply"].get("by"))):
        conv["reply"]["provenance"] = "bot"  # the mailbox's AI assistant, never a seed
    return conv


# ------------------------------------------------------------------ output tiers


def _cell(value: Any, limit: int = 140) -> str:
    return clip(" ".join(str(value or "").split()), limit).replace("\t", " ") or "-"


TSV_LINE_MAX, FIRST_MAX = 200, 110


def first_sentence(text: Any) -> str:
    """The question in one line: the first sentence, or the head of it when there is no stop."""
    flat = " ".join(str(text or "").split())
    cut = re.search(r"[.?!]\s", flat)
    return clip(flat[:cut.end() - 1] if cut else flat, FIRST_MAX)


def write_tsvs(evidence: dict[str, Any]) -> tuple[str, str]:
    """The read-whole tier: one line per conversation and per article, no bodies.

    Conversation lines stay under TSV_LINE_MAX so 500 of them are ~25k tokens: that is the judge's
    pass-1 budget. Article lines carry the full keyword/alias vocabulary, which is what makes
    `wrong_title` findable, so they are not clipped.
    """
    convs = [["id", "date", "tenant", "ch", "first", "turns", "reply", "linked", "noise"]]
    for c in evidence.get("conversations") or []:
        row = [c["id"], str(c.get("created_at"))[:10], _cell(c.get("tenant"), 20), c["channel"],
               first_sentence(c["first_message"]).replace("\t", " ") or "-",
               str(len(c.get("later") or [])), (c.get("reply") or {}).get("provenance", "-"),
               ",".join(c.get("linked_articles") or []) or "-",
               (c.get("noise") or {}).get("reason", "-")]
        overflow = len("\t".join(row)) - TSV_LINE_MAX
        if overflow > 0:
            row[4] = clip(row[4], max(20, len(row[4]) - overflow - 2))
        convs.append(row)
    arts = [["id", "title", "collection", "collection_id", "audience", "keywords", "aliases",
             "dup", "path"]]
    # The same article shipped under two collections drifts apart over time (kampadmin-support:
    # `{{.uitschrijven}}` in one copy only). Name the twins so the judge diffs them on purpose.
    by_title: dict[str, list[str]] = {}
    for a in evidence.get("articles") or []:
        if a.get("home") == "kb":
            by_title.setdefault(" ".join(str(a.get("title") or "").lower().split()), []).append(a["id"])
    for a in evidence.get("articles") or []:
        twins = [i for i in by_title.get(" ".join(str(a.get("title") or "").lower().split()), [])
                 if i != a["id"]] if a.get("home") == "kb" else []
        arts.append([a["id"], _cell(a.get("title"), 100), _cell(a.get("collection"), 40),
                     _cell(a.get("collection_id"), 40), _cell(a.get("audience"), 20),
                     ", ".join(a.get("keywords") or []) or "-",
                     ", ".join(a.get("aliases") or []) or "-",
                     ",".join(twins) or "-", a.get("path") or "-"])
    return ("\n".join("\t".join(row) for row in convs) + "\n",
            "\n".join("\t".join(row) for row in arts) + "\n")


def _article_line(article: dict[str, Any]) -> str:
    bits = [article["id"], article.get("title") or "?"]
    bits += [", ".join(article[key]) for key in ("keywords", "aliases") if article.get(key)]
    bits += [str(article[key]) for key in ("summary", "collection", "audience", "path")
             if article.get(key)]
    return " · ".join(bits + (["DEPRECATED"] if article.get("deprecated") else []))


def write_digest(evidence: dict[str, Any]) -> str:
    """The drill tier: full conversation bodies + the article inventory (conversations.tsv first)."""
    window, kb = evidence.get("window") or {}, evidence.get("kb") or {}
    convs, articles = evidence.get("conversations") or [], evidence.get("articles") or []
    public = [a for a in articles if a.get("home") == "kb"]
    brain = [a for a in articles if a.get("home") == "brain"]
    coverage = " · ".join(f"{c.get('feed')}={c.get('status')}({c.get('retained')}/{c.get('scanned')})"
                          for c in evidence.get("coverage") or [])
    reasons = [f"{c['feed']}: {c['reason']}" for c in evidence.get("coverage") or [] if c.get("reason")]
    out = [f"# Help centre evidence: {evidence.get('project')}"
           + (f" / {evidence['tenant']}" if evidence.get("tenant") else ""), "",
           f"window {str(window.get('start'))[:16]} → {str(window.get('end'))[:16]} "
           f"({window.get('days')}d) · source {evidence.get('source')} · {len(convs)} conversations "
           f"· {len(public)} kb articles ({kb.get('status')}) · kb root {kb.get('root') or '-'}",
           f"coverage: {coverage or 'none'}"] + reasons + ["", "## Conversations", ""]
    for conv in convs:
        reply, noise = conv.get("reply"), conv.get("noise")
        out += [f"##### {conv['id']} | {conv['channel']} | {str(conv.get('created_at'))[:16]} | "
                f"subj={conv.get('subject') or '-'} | {conv.get('customer') or '-'} | "
                f"{'↗' if conv.get('url') else 'no-link'}"
                + (f" | linked {','.join(conv['linked_articles'])}" if conv.get('linked_articles') else '')
                + (f" | noise: {noise['reason']}" if noise else ''),
                f"[customer] {conv['first_message']}"]
        if conv.get("first_raw"):
            out.append(f"[customer|opener] {conv['first_raw']}")
        if reply:
            out.append(f"[agent|{reply.get('provenance')}|{reply.get('by') or '-'}] {reply['text']}")
        out += [f"[{turn['role']}] {turn['text']}" for turn in conv.get("later") or []]
        out += (["[…] middle turns omitted"] if conv.get("truncated") else []) + [""]
    out += [f"## Articles ({len(public)})", ""] + [_article_line(a) for a in public]
    out += ["", f"## Brain knowledge ({len(brain)})", ""]
    return "\n".join(out + [f"{a['id']} · {a.get('title') or a.get('path')}" for a in brain]) + "\n"
