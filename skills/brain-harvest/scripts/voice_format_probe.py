# /// script
# requires-python = ">=3.11"
# ///
"""Propose `channel.draft_font_css` + `channel.signature_html` from a mailbox's OWN sent mail.

Gmail exposes neither the composer font nor the signature over its API, so both are recovered from
the HTML of mail the mailbox already sent: Gmail stamps every paragraph of a non-default font as
`<div class="gmail_default" style="font-family:…">` and wraps the signature in
`class="gmail_signature"` / `data-smartmail="gmail_signature"`. A signature logo is already hosted by
Google (`https://ci3.googleusercontent.com/mail-sig/…`) and that URL is stable across sends, so it is
reused verbatim — never rehosted.

Input is a JSON array / NDJSON / object-with-`items` of message rows, each carrying the raw
`body_html` (aliases: `body`, `html`). Output is ONE JSON proposal on stdout — a *proposal*, never an
applied setting: an operator reads `signature_text`, then applies with `rp … settings set`.

    voice_format_probe.py --print-sql --mailbox info@example.com   # the row export query
    voice_format_probe.py --messages rows.json --out proposal.json
    voice_format_probe.py --messages - --no-network                # skip image HEAD checks

There is no public `rp` surface for a message's raw HTML today (the inbox API returns cleaned text
only), so the rows come from a ReplyPen-side export of the query `--print-sql` emits; external brain
developers request it through `brain-publish`. Rows are raw customer mail: keep them under the
gitignored scratch root and delete them with the rest of scratch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from html import unescape
from pathlib import Path
from typing import Any

DEFAULT_SCAN = 30
# Server-side cap on channel.signature_html; a longer proposal is rejected at set time, so flag it here.
MAX_SIGNATURE_BYTES = 16 * 1024
# A signature must be near-universal in the sample before it is the mailbox's signature rather than one
# staffer's ad-hoc sign-off.
SIGNATURE_MIN_SHARE = 0.60
# A font-size is only part of the voice when Gmail stamps it as consistently as the font-family itself.
FONT_SIZE_MIN_SHARE = 0.80

GMAIL_DEFAULT_RE = re.compile(
    r"""<div\b[^>]*class="[^"]*\bgmail_default\b[^"]*"[^>]*?style="([^"]*)\"""",
    re.IGNORECASE,
)
# Same div, attributes in the other order.
GMAIL_DEFAULT_ALT_RE = re.compile(
    r"""<div\b[^>]*style="([^"]*)"[^>]*class="[^"]*\bgmail_default\b[^"]*\"""",
    re.IGNORECASE,
)
SIGNATURE_OPEN_RE = re.compile(
    r"""<div\b[^>]*(?:class="[^"]*\bgmail_signature\b[^"]*"|data-smartmail="gmail_signature")[^>]*>""",
    re.IGNORECASE,
)
DIV_TAG_RE = re.compile(r"<(/?)div\b[^>]*>", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc="([^"]+)\"""", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
BETWEEN_TAGS_RE = re.compile(r">\s+<")

# `not exists (drafts…)` is load-bearing: on a mailbox in send mode our OWN sent replies are outbound
# rows too, and learning the font/signature from them would just feed our default back to us.
SQL_TEMPLATE = """select msg.date, msg.body_html
from messages msg join mailboxes mb on mb.id = msg.mailbox_id
where mb.email_address = '{mailbox}'
  and msg.direction = 'outbound' and msg.is_draft = false
  and msg.body_html is not null and msg.body_html <> ''
  and not exists (select 1 from drafts d where d.sent_message_id = msg.id)
order by msg.date desc limit {limit}"""


# ── extraction ────────────────────────────────────────────────────────────────────────────────────


def font_declarations(html: str) -> list[str]:
    """Every `gmail_default` style fragment in one message, in document order."""
    return [m.group(1).strip() for m in GMAIL_DEFAULT_RE.finditer(html)] + [
        m.group(1).strip() for m in GMAIL_DEFAULT_ALT_RE.finditer(html)
    ]


def parse_declarations(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in style.split(";"):
        prop, sep, value = part.partition(":")
        if sep:
            out[prop.strip().lower()] = WS_RE.sub(" ", value.strip().lower())
    return out


def signature_html(html: str) -> str:
    """innerHTML of the first gmail_signature block.

    Gmail nests the signature dozens of divs deep, so the closing tag is found by counting div
    depth; a regex pairing `<div>`…`</div>` would stop at the first inner close.
    """
    open_match = SIGNATURE_OPEN_RE.search(html)
    if not open_match:
        return ""
    start = open_match.end()
    depth = 1
    for tag in DIV_TAG_RE.finditer(html, start):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[start : tag.start()].strip()
    return html[start:].strip()  # truncated mail: take the tail rather than dropping the signature


def normalise(fragment: str) -> str:
    """Collapse whitespace runs so two sends of the same signature compare equal.

    Gmail re-indents the block per send, so inter-tag whitespace is noise, not content.
    """
    return BETWEEN_TAGS_RE.sub("><", WS_RE.sub(" ", fragment)).strip()


def image_urls(fragment: str) -> list[str]:
    seen: list[str] = []
    for m in IMG_SRC_RE.finditer(fragment):
        url = unescape(m.group(1)).strip()
        if url and url not in seen:
            seen.append(url)
    return seen


def to_text(fragment: str) -> str:
    """Plain-text rendering of a signature, for the operator's review step."""
    text = re.sub(r"<br\b[^>]*>|</div>|</p>|</tr>", "\n", fragment, flags=re.IGNORECASE)
    text = re.sub(r"<img\b[^>]*>", "[image]", text, flags=re.IGNORECASE)
    text = unescape(TAG_RE.sub("", text))
    lines = [WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


# ── proposal ──────────────────────────────────────────────────────────────────────────────────────


def propose(messages: list[str]) -> dict[str, Any]:
    scanned = len(messages)
    font_votes: Counter[str] = Counter()  # per message: its dominant gmail_default style, or ""
    size_votes: Counter[str] = Counter()
    signatures: Counter[str] = Counter()

    for html in messages:
        decls = [parse_declarations(s) for s in font_declarations(html)]
        families = Counter(d["font-family"] for d in decls if d.get("font-family"))
        font_votes[families.most_common(1)[0][0] if families else ""] += 1
        sizes = Counter(d["font-size"] for d in decls if d.get("font-size"))
        if sizes:
            size_votes[sizes.most_common(1)[0][0]] += 1

        sig = normalise(signature_html(html))
        if sig:
            signatures[sig] += 1

    font_css = ""
    font_family, font_count = (font_votes.most_common(1) or [("", 0)])[0]
    if font_family and font_count > scanned / 2:
        font_css = f"font-family:{font_family}"
        size, size_count = (size_votes.most_common(1) or [("", 0)])[0]
        if size and size_count >= FONT_SIZE_MIN_SHARE * font_count:
            font_css += f";font-size:{size}"

    sig_html, sig_count = (signatures.most_common(1) or [("", 0)])[0]
    sig_share = sig_count / scanned if scanned else 0.0
    if sig_share < SIGNATURE_MIN_SHARE:
        sig_html = ""

    images = image_urls(sig_html) if sig_html else []
    proposal: dict[str, Any] = {
        "scanned": scanned,
        "draft_font_css": font_css,
        "font_share": round(font_count / scanned, 2) if scanned and font_family else 0.0,
        "signature_html": sig_html,
        "signature_text": to_text(sig_html) if sig_html else "",
        "signature_share": round(sig_share, 2),
        "signature_bytes": len(sig_html.encode("utf-8")),
        "signature_variants": len(signatures),
        "image_urls": images,
        "checks": [],
    }
    return proposal


def run_checks(proposal: dict[str, Any], *, network: bool) -> None:
    checks: list[dict[str, Any]] = []
    scanned = proposal["scanned"]
    if scanned < 10:
        checks.append({"check": "sample", "status": "warn", "detail": f"only {scanned} messages scanned"})
    if not proposal["draft_font_css"]:
        checks.append({"check": "font", "status": "warn", "detail": "no dominant gmail_default font — leave draft_font_css unset"})
    elif proposal["font_share"] < 0.7:
        checks.append({"check": "font", "status": "warn", "detail": f"font stamped on only {proposal['font_share']:.0%} of mails — thin majority"})
    if not proposal["signature_html"]:
        checks.append({
            "check": "signature",
            "status": "warn",
            "detail": f"top signature in {proposal['signature_share']:.0%} of mails, below the {SIGNATURE_MIN_SHARE:.0%} bar",
        })
    if proposal["signature_bytes"] > MAX_SIGNATURE_BYTES:
        checks.append({
            "check": "size",
            "status": "fail",
            "detail": f"signature_html is {proposal['signature_bytes']} bytes, over the {MAX_SIGNATURE_BYTES}-byte server cap",
        })
    for url in proposal["image_urls"]:
        checks.append(head(url) if network else {"check": "image", "status": "skipped", "url": url})
    proposal["checks"] = checks


def head(url: str) -> dict[str, Any]:
    """HEAD one signature image. A logo that 404s would render as a broken image in every draft."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "replypen-voice-format-probe"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001 — a probe never fails the proposal
        return {"check": "image", "status": "error", "url": url, "detail": type(exc).__name__}
    return {"check": "image", "status": "ok" if code < 400 else "fail", "url": url, "http_status": code}


# ── input ─────────────────────────────────────────────────────────────────────────────────────────


def load_rows(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        parsed = parsed.get("items") or parsed.get("rows") or [parsed]
    return list(parsed)


def bodies(rows: list[dict[str, Any]], limit: int) -> list[str]:
    out: list[str] = []
    for row in rows:
        html = row.get("body_html") or row.get("body") or row.get("html") or ""
        if isinstance(html, str) and html.strip():
            out.append(html)
        if len(out) >= limit:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--messages", help="JSON/NDJSON file of message rows, or - for stdin")
    ap.add_argument("--print-sql", action="store_true", help="print the row-export query and exit")
    ap.add_argument("--mailbox", default="<mailbox-address>", help="mailbox address for --print-sql")
    ap.add_argument("--limit", type=int, default=DEFAULT_SCAN, help=f"messages to scan (default {DEFAULT_SCAN})")
    ap.add_argument("--out", help="write the proposal here instead of stdout")
    ap.add_argument("--no-network", action="store_true", help="skip the image HEAD checks")
    args = ap.parse_args(argv)

    if args.print_sql:
        print(SQL_TEMPLATE.format(mailbox=args.mailbox, limit=args.limit))
        return 0
    if not args.messages:
        ap.error("--messages is required (or use --print-sql)")

    raw = sys.stdin.read() if args.messages == "-" else Path(args.messages).read_text("utf-8")
    messages = bodies(load_rows(raw), args.limit)
    if not messages:
        print("no message rows with body_html", file=sys.stderr)
        return 1

    proposal = propose(messages)
    run_checks(proposal, network=not args.no_network)
    rendered = json.dumps(proposal, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(rendered + "\n", "utf-8")
    else:
        print(rendered)
    return 1 if any(c["status"] == "fail" for c in proposal["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
