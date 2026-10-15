"""Google Docs helpers: readable text out, Markdown in.

The Docs API speaks structural JSON with UTF-16 indices; these wrappers own that arithmetic.
``text`` flattens a document to Markdown-ish plain text (optionally with each paragraph's start index,
which raw ``batchUpdate`` requests need). ``insert_markdown`` turns a Markdown subset into ONE
``documents:batchUpdate`` (insertText + paragraph/text styles + bullets), pinned to the revision it read
so a concurrent edit fails instead of landing at stale indices. Anything else the Docs API supports
goes through ``requests`` (raw passthrough) or ``python -m lib.api post googledocs
documents/<id>:batchUpdate --json …``.

Markdown subset: ``#``/``##``/``###`` headings, paragraphs (soft-wrapped lines join), ``- ``/``* ``
bullets, ``1. `` numbered items, ``**bold**``, ``*italic*``/``_italic_``, ``[text](url)``, ``\\`` escapes.
Everything else is inserted as literal text.

    python -m lib.gdocs text <doc> [--indices]
    python -m lib.gdocs insert-markdown <doc> (--markdown TEXT | --file PATH) [--after-heading H] [--dry-run]
    python -m lib.gdocs replace-text <doc> <old> <new> [--ignore-case]
    python -m lib.gdocs requests <doc> (--json '[…]' | --json-file PATH)
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib import _google

KEY = _google.DOCS
_HEADING_PREFIX = {"TITLE": "# ", **{f"HEADING_{n}": "#" * n + " " for n in range(1, 7)}}
_ORDERED_GLYPHS = {"DECIMAL", "ZERO_DECIMAL", "UPPER_ALPHA", "ALPHA", "UPPER_ROMAN", "ROMAN"}
_BULLET_PRESET = {"ul": "BULLET_DISC_CIRCLE_SQUARE", "ol": "NUMBERED_DECIMAL_ALPHA_ROMAN"}


def _u16(s: str) -> int:
    """Docs indices count UTF-16 code units: an emoji is 2, an accented letter 1."""
    return len(s.encode("utf-16-le")) // 2


def _doc_path(doc: str, suffix: str = "") -> str:
    return f"documents/{_google.file_id(doc, KEY)}{suffix}"


def _get(doc: str) -> dict:
    return _google.client(KEY).get(_doc_path(doc))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _para_text(par: dict) -> str:
    return "".join((el.get("textRun") or {}).get("content", "") for el in par.get("elements", []))


def _ordered(lists: dict, bullet: dict) -> bool:
    levels = (((lists.get(bullet.get("listId", "")) or {}).get("listProperties") or {}).get("nestingLevels")) or []
    level = bullet.get("nestingLevel", 0) or 0
    glyph = (levels[level] if level < len(levels) else {}).get("glyphType", "")
    return glyph in _ORDERED_GLYPHS


def _render(content: list[dict], lists: dict, indices: bool) -> list[str]:
    lines: list[str] = []
    for el in content:
        if "paragraph" in el:
            par = el["paragraph"]
            body = _para_text(par).rstrip("\n").replace("\x0b", "\n")
            style = (par.get("paragraphStyle") or {}).get("namedStyleType", "")
            if (bullet := par.get("bullet")) is not None:
                prefix = "  " * (bullet.get("nestingLevel", 0) or 0) + ("1. " if _ordered(lists, bullet) else "- ")
            else:
                prefix = _HEADING_PREFIX.get(style, "") if body.strip() else ""
            line = prefix + body
            lines.append(f"[{el.get('startIndex', 0)}] {line}" if indices else line)
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                cells = [" ".join(_render(c.get("content", []), lists, False)).strip() for c in row.get("tableCells", [])]
                lines.append("| " + " | ".join(cells) + " |")
    return lines


def text(doc: str, *, indices: bool = False) -> str:
    """The document as plain text: headings as ``# ``/``## ``, bullets as ``- ``/``1. ``, tables as
    ``| a | b |`` rows. ``indices=True`` prefixes each paragraph with ``[startIndex]``, the location a
    raw ``insertText``/``deleteContentRange`` request needs."""
    d = _get(doc)
    body = "\n".join(_render((d.get("body") or {}).get("content", []), d.get("lists") or {}, indices))
    return f"title: {d.get('title', '')}\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Markdown → batchUpdate requests
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    text: str
    bold: bool = False
    italic: bool = False
    link: str = ""


@dataclass
class _Block:
    kind: str  # p | h1 | h2 | h3 | ul | ol
    spans: list[_Span] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


_ESC = {"\\" + c: chr(0xE000 + i) for i, c in enumerate("\\*_[]()#`-")}
_UNESC = {v: k[1] for k, v in _ESC.items()}
_INLINE = [
    ("link", re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")),
    ("bold", re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")),
    ("bold", re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")),
    ("italic", re.compile(r"(?<!\*)\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?!\*)")),
    ("italic", re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")),
]
_HEADING = re.compile(r"(#{1,3})\s+(.*?)\s*#*\s*$")
_UL = re.compile(r"[-*+]\s+(.*)")
_OL = re.compile(r"\d+[.)]\s+(.*)")


def _unescape(s: str) -> str:
    return "".join(_UNESC.get(ch, ch) for ch in s)


def _inline(s: str, bold: bool = False, italic: bool = False, link: str = "") -> list[_Span]:
    spans: list[_Span] = []
    while s:
        best = None
        for kind, rx in _INLINE:
            m = rx.search(s)
            if m and (best is None or m.start() < best[1].start()):
                best = (kind, m)
        if best is None:
            spans.append(_Span(_unescape(s), bold, italic, link))
            break
        kind, m = best
        if m.start():
            spans.append(_Span(_unescape(s[: m.start()]), bold, italic, link))
        if kind == "link":
            spans += _inline(m.group(1), bold, italic, _unescape(m.group(2)))
        else:
            spans += _inline(m.group(1), bold or kind == "bold", italic or kind == "italic", link)
        s = s[m.end():]
    return [sp for sp in spans if sp.text]


def parse_markdown(markdown: str) -> list[_Block]:
    """Markdown subset → blocks. Blank lines end a paragraph; soft-wrapped lines join with a space."""
    for k, v in _ESC.items():
        markdown = markdown.replace(k, v)
    blocks: list[_Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(_Block("p", _inline(" ".join(para))))
            para.clear()

    for raw in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif m := _HEADING.fullmatch(line):
            flush()
            blocks.append(_Block(f"h{len(m.group(1))}", _inline(m.group(2))))
        elif m := _UL.fullmatch(line):
            flush()
            blocks.append(_Block("ul", _inline(m.group(1))))
        elif m := _OL.fullmatch(line):
            flush()
            blocks.append(_Block("ol", _inline(m.group(1))))
        else:
            para.append(line)
    flush()
    return [b for b in blocks if b.text.strip()]


_NAMED_STYLE = {"p": "NORMAL_TEXT", "ul": "NORMAL_TEXT", "ol": "NORMAL_TEXT", "h1": "HEADING_1", "h2": "HEADING_2", "h3": "HEADING_3"}


def markdown_requests(markdown: str, index: int, *, leading_newline: bool = True) -> list[dict]:
    """The batchUpdate request list that inserts ``markdown`` at ``index``.

    ``index`` is the position of a paragraph's closing newline; the block lands as new paragraphs right
    after that paragraph. ``leading_newline=False`` instead fills an EMPTY paragraph at ``index`` (an
    empty document, or an empty last line), so no blank line is left above the insert.
    Styles are set explicitly on every inserted paragraph and run, so nothing inherits from the
    neighbour it was split from (a heading's style, a list's bullet, a bold run).
    """
    blocks = parse_markdown(markdown)
    if not blocks:
        raise ValueError("nothing to insert: the markdown is empty")
    body = "\n".join(b.text for b in blocks)
    inserted = ("\n" if leading_newline else "") + body
    reqs: list[dict] = [{"insertText": {"location": {"index": index}, "text": inserted}}]
    start = index + (1 if leading_newline else 0)
    end_all = index + _u16(inserted)

    para_ranges = []
    pos = start
    for b in blocks:
        n = _u16(b.text)
        para_ranges.append((pos, pos + n))
        pos += n + 1  # the paragraph's newline

    reqs.append({"deleteParagraphBullets": {"range": _rng(start, end_all)}})
    for b, (s, e) in zip(blocks, para_ranges):
        reqs.append({
            "updateParagraphStyle": {
                "range": _rng(s, e),
                "paragraphStyle": {"namedStyleType": _NAMED_STYLE[b.kind]},
                "fields": "namedStyleType",
            }
        })
    reqs.append({"updateTextStyle": {"range": _rng(index, end_all), "textStyle": {}, "fields": "bold,italic,link"}})
    for b, (s, _e) in zip(blocks, para_ranges):
        pos = s
        for sp in b.spans:
            n = _u16(sp.text)
            style: dict[str, Any] = {}
            if sp.bold:
                style["bold"] = True
            if sp.italic:
                style["italic"] = True
            if sp.link:
                style["link"] = {"url": sp.link}
            if style:
                reqs.append({"updateTextStyle": {"range": _rng(pos, pos + n), "textStyle": style, "fields": ",".join(style)}})
            pos += n
    i = 0
    while i < len(blocks):
        kind = blocks[i].kind
        if kind not in _BULLET_PRESET:
            i += 1
            continue
        j = i
        while j + 1 < len(blocks) and blocks[j + 1].kind == kind:
            j += 1
        reqs.append({"createParagraphBullets": {"range": _rng(para_ranges[i][0], para_ranges[j][1]), "bulletPreset": _BULLET_PRESET[kind]}})
        i = j + 1
    return reqs


def _rng(start: int, end: int) -> dict:
    return {"startIndex": start, "endIndex": end}


def _norm_heading(s: str) -> str:
    return " ".join(s.strip().lstrip("#").split()).casefold()


def _insert_point(d: dict, after_heading: str | None) -> tuple[int, bool]:
    """``(index, leading_newline)`` for ``markdown_requests``: after the named heading's paragraph,
    else at the end of the body."""
    paras = [el for el in (d.get("body") or {}).get("content", []) if "paragraph" in el]
    if not paras:
        raise ValueError("the document body has no paragraph to insert after")
    if after_heading is not None:
        want = _norm_heading(after_heading)
        for el in paras:
            style = ((el["paragraph"].get("paragraphStyle") or {}).get("namedStyleType", ""))
            if style in _HEADING_PREFIX and _norm_heading(_para_text(el["paragraph"])) == want:
                return el["endIndex"] - 1, True
        found = [
            _para_text(el["paragraph"]).strip()
            for el in paras
            if (el["paragraph"].get("paragraphStyle") or {}).get("namedStyleType", "") in _HEADING_PREFIX
        ]
        raise LookupError(f"no heading {after_heading!r}; headings are {found}")
    last = paras[-1]
    empty = _para_text(last["paragraph"]) == "\n" and "bullet" not in last["paragraph"]
    return last["endIndex"] - 1, not empty


def insert_markdown(doc: str, markdown: str, *, after_heading: str | None = None, dry_run: bool = False) -> dict:
    """Insert ``markdown`` as formatted paragraphs at the end of the body, or right after the heading
    named ``after_heading``. Returns ``{"index", "paragraphs", "requests"}`` (``requests`` only on a
    dry run, which sends nothing). Read the doc back with ``text`` to confirm the result."""
    d = _get(doc)
    index, leading = _insert_point(d, after_heading)
    reqs = markdown_requests(markdown, index, leading_newline=leading)
    out: dict[str, Any] = {"index": index, "paragraphs": len(parse_markdown(markdown))}
    if dry_run:
        out["requests"] = reqs
        return out
    payload: dict[str, Any] = {"requests": reqs}
    if d.get("revisionId"):
        payload["writeControl"] = {"requiredRevisionId": d["revisionId"]}
    _google.client(KEY).post(_doc_path(doc, ":batchUpdate"), json=payload)
    return out


def replace_text(doc: str, old: str, new: str, *, match_case: bool = True) -> dict:
    """Replace every occurrence of ``old`` (whole document); returns ``{"occurrences_changed": n}``.
    0 means nothing matched: check spelling/case before claiming the change."""
    body = requests(doc, [{"replaceAllText": {"containsText": {"text": old, "matchCase": match_case}, "replaceText": new}}])
    reply = (body.get("replies") or [{}])[0].get("replaceAllText") or {}
    return {"occurrences_changed": reply.get("occurrencesChanged", 0)}


def requests(doc: str, reqs: list[dict]) -> dict:
    """Raw escape hatch: send any ``documents.batchUpdate`` request list; returns Google's reply."""
    if not isinstance(reqs, list) or not reqs:
        raise ValueError("requests must be a non-empty JSON list of Docs API request objects")
    return _google.client(KEY).post(_doc_path(doc, ":batchUpdate"), json={"requests": reqs})


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m lib.gdocs", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("text", help="the document as plain text with headings/bullets marked")
    sp.add_argument("doc")
    sp.add_argument("--indices", action="store_true", help="prefix paragraphs with their start index")

    sp = sub.add_parser("insert-markdown", help="insert formatted Markdown at the end or after a heading")
    sp.add_argument("doc")
    sp.add_argument("--markdown", default="")
    sp.add_argument("--file", default="", help="read the Markdown from a file")
    sp.add_argument("--after-heading", default=None)
    sp.add_argument("--dry-run", action="store_true", help="print the requests, send nothing")

    sp = sub.add_parser("replace-text", help="replace every occurrence of a text")
    sp.add_argument("doc")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.add_argument("--ignore-case", action="store_true")

    sp = sub.add_parser("requests", help="raw documents.batchUpdate passthrough")
    sp.add_argument("doc")
    sp.add_argument("--json", default="")
    sp.add_argument("--json-file", default="")

    args = p.parse_args(argv)

    def md() -> str:
        return Path(args.file).read_text(encoding="utf-8") if args.file else args.markdown

    def raw() -> list:
        return json.loads(Path(args.json_file).read_text(encoding="utf-8") if args.json_file else args.json or "null")

    commands = {
        "text": lambda: text(args.doc, indices=args.indices),
        "insert-markdown": lambda: insert_markdown(args.doc, md(), after_heading=args.after_heading, dry_run=args.dry_run),
        "replace-text": lambda: replace_text(args.doc, args.old, args.new, match_case=not args.ignore_case),
        "requests": lambda: requests(args.doc, raw()),
    }
    return _google.cli(commands[args.cmd])


if __name__ == "__main__":
    raise SystemExit(_main())
