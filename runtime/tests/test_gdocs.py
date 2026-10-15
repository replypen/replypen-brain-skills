"""lib.gdocs: Docs JSON → text, Markdown → one batchUpdate, index arithmetic in UTF-16.

No network: ``lib._google.client`` is patched with a recording fake. A tiny in-memory text model
applies the generated ``insertText`` so every style/bullet range is checked against the text it
should cover (the class of bug that silently styles the wrong words).

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_gdocs.py -q
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import _google, gdocs  # noqa: E402

DOC_ID = "1DocAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
DOC_URL = f"https://docs.google.com/document/d/{DOC_ID}/edit"


def para(text, start, style="NORMAL_TEXT", bullet=None):
    p = {"elements": [{"textRun": {"content": text}}], "paragraphStyle": {"namedStyleType": style}}
    if bullet:
        p["bullet"] = bullet
    return {"startIndex": start, "endIndex": start + gdocs._u16(text), "paragraph": p}


def doc_of(*paras_spec, revision="rev-1"):
    """Build a Docs body from (text, style[, bullet]) tuples with correct UTF-16 indices."""
    content = [{"endIndex": 1, "sectionBreak": {}}]
    pos = 1
    for spec in paras_spec:
        text, style, *rest = spec
        content.append(para(text, pos, style, rest[0] if rest else None))
        pos += gdocs._u16(text)
    return {"documentId": DOC_ID, "title": "Handbook", "revisionId": revision, "body": {"content": content}, "lists": {
        "ol": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}},
        "ul": {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}]}},
    }}


def body_text(d):
    return "".join(gdocs._para_text(el["paragraph"]) for el in d["body"]["content"] if "paragraph" in el)


class Model:
    """The body as UTF-16 code units, index 1 = first character (index 0 is the section break)."""

    def __init__(self, text):
        self.units = list(text.encode("utf-16-le"))

    def insert(self, index, text):
        at = (index - 1) * 2
        self.units[at:at] = list(text.encode("utf-16-le"))

    def slice(self, rng):
        return bytes(self.units[(rng["startIndex"] - 1) * 2:(rng["endIndex"] - 1) * 2]).decode("utf-16-le")


def apply(d, reqs):
    m = Model(body_text(d))
    ins = reqs[0]["insertText"]
    m.insert(ins["location"]["index"], ins["text"])
    return m


class Fake:
    def __init__(self, doc, reply=None):
        self.doc, self.reply, self.calls = doc, reply or {"replies": [{}]}, []

    def get(self, path, *, query=None):
        self.calls.append(("GET", path, None))
        return self.doc

    def post(self, path, *, query=None, json=None):
        self.calls.append(("POST", path, json))
        return self.reply


def patched(fake):
    return mock.patch.object(_google, "client", lambda key: fake)


SAMPLE_MD = """## Opening hours

We are open **Monday to Friday**, see [the site](https://ex.be/hours).
Closed on _public holidays_.

- Visits by *appointment*
- Parking at the back

1. Call us
2. Come in
"""

SAMPLE_GOLDEN = [
    {"insertText": {"location": {"index": 6}, "text": "\nOpening hours\nWe are open Monday to Friday, see the site. Closed on public holidays.\nVisits by appointment\nParking at the back\nCall us\nCome in"}},
    {"deleteParagraphBullets": {"range": {"startIndex": 7, "endIndex": 149}}},
    {"updateParagraphStyle": {"range": {"startIndex": 7, "endIndex": 20}, "paragraphStyle": {"namedStyleType": "HEADING_2"}, "fields": "namedStyleType"}},
    {"updateParagraphStyle": {"range": {"startIndex": 21, "endIndex": 91}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
    {"updateParagraphStyle": {"range": {"startIndex": 92, "endIndex": 113}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
    {"updateParagraphStyle": {"range": {"startIndex": 114, "endIndex": 133}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
    {"updateParagraphStyle": {"range": {"startIndex": 134, "endIndex": 141}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
    {"updateParagraphStyle": {"range": {"startIndex": 142, "endIndex": 149}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}},
    {"updateTextStyle": {"range": {"startIndex": 6, "endIndex": 149}, "textStyle": {}, "fields": "bold,italic,link"}},
    {"updateTextStyle": {"range": {"startIndex": 33, "endIndex": 49}, "textStyle": {"bold": True}, "fields": "bold"}},
    {"updateTextStyle": {"range": {"startIndex": 55, "endIndex": 63}, "textStyle": {"link": {"url": "https://ex.be/hours"}}, "fields": "link"}},
    {"updateTextStyle": {"range": {"startIndex": 75, "endIndex": 90}, "textStyle": {"italic": True}, "fields": "italic"}},
    {"updateTextStyle": {"range": {"startIndex": 102, "endIndex": 113}, "textStyle": {"italic": True}, "fields": "italic"}},
    {"createParagraphBullets": {"range": {"startIndex": 92, "endIndex": 133}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}},
    {"createParagraphBullets": {"range": {"startIndex": 134, "endIndex": 149}, "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}},
]


class MarkdownRequests(unittest.TestCase):
    def test_golden_requests_for_sample_doc(self):
        d = doc_of(("Intro\n", "NORMAL_TEXT"))
        index, leading = gdocs._insert_point(d, None)
        self.assertEqual((index, leading), (6, True))
        reqs = gdocs.markdown_requests(SAMPLE_MD, index, leading_newline=leading)
        self.assertEqual(reqs, SAMPLE_GOLDEN)

    def test_ranges_cover_the_right_text_after_insert(self):
        d = doc_of(("Intro\n", "NORMAL_TEXT"), ("Prices\n", "HEADING_1"), ("Old line\n", "NORMAL_TEXT"))
        index, leading = gdocs._insert_point(d, "# prices")
        self.assertEqual(index, 13)  # the closing newline of "Prices"
        reqs = gdocs.markdown_requests(SAMPLE_MD, index, leading_newline=leading)
        m = apply(d, reqs)
        self.assertEqual(m.slice({"startIndex": 1, "endIndex": 1 + gdocs._u16(body_text(d)) + gdocs._u16(reqs[0]["insertText"]["text"])}),
                         "Intro\nPrices\nOpening hours\nWe are open Monday to Friday, see the site. Closed on public holidays.\n"
                         "Visits by appointment\nParking at the back\nCall us\nCome in\nOld line\n")
        styled = {m.slice(r["updateTextStyle"]["range"]): r["updateTextStyle"]["fields"] for r in reqs[4:] if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"]}
        self.assertEqual(styled, {"Monday to Friday": "bold", "the site": "link", "public holidays": "italic", "appointment": "italic"})
        paras = [m.slice(r["updateParagraphStyle"]["range"]) for r in reqs if "updateParagraphStyle" in r]
        self.assertEqual(paras[0], "Opening hours")
        bullets = [m.slice(r["createParagraphBullets"]["range"]) for r in reqs if "createParagraphBullets" in r]
        self.assertEqual(bullets, ["Visits by appointment\nParking at the back", "Call us\nCome in"])

    def test_utf16_offsets_for_emoji_and_accents(self):
        d = doc_of(("Café 🎉 intro\n", "NORMAL_TEXT"))
        index, leading = gdocs._insert_point(d, None)
        self.assertEqual(index, 1 + gdocs._u16("Café 🎉 intro"))  # 🎉 is 2 code units
        reqs = gdocs.markdown_requests("Één 👩‍👩‍👧 **gezin** en *crème brûlée* 🎉 [klik](https://x.be)", index, leading_newline=leading)
        m = apply(d, reqs)
        styled = [m.slice(r["updateTextStyle"]["range"]) for r in reqs if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"]]
        self.assertEqual(styled, ["gezin", "crème brûlée", "klik"])
        self.assertEqual(reqs[1]["deleteParagraphBullets"]["range"]["endIndex"],
                         index + gdocs._u16(reqs[0]["insertText"]["text"]))

    def test_empty_document_fills_the_empty_paragraph(self):
        d = doc_of(("\n", "NORMAL_TEXT"))
        index, leading = gdocs._insert_point(d, None)
        self.assertEqual((index, leading), (1, False))
        reqs = gdocs.markdown_requests("# Title\nBody", index, leading_newline=leading)
        self.assertEqual(reqs[0]["insertText"]["text"], "Title\nBody")
        m = apply(d, reqs)
        self.assertEqual(m.slice(reqs[2]["updateParagraphStyle"]["range"]), "Title")

    def test_parse_subset_and_escapes(self):
        blocks = gdocs.parse_markdown("# A\nline one\nline two\n\n* star item\n2) second\n\\*not italic\\* snake_case_name **b *bi* b**")
        self.assertEqual([(b.kind, b.text) for b in blocks], [
            ("h1", "A"), ("p", "line one line two"), ("ul", "star item"), ("ol", "second"),
            ("p", "*not italic* snake_case_name b bi b"),
        ])
        spans = blocks[-1].spans
        self.assertEqual([(s.text, s.bold, s.italic) for s in spans if s.bold or s.italic],
                         [("b ", True, False), ("bi", True, True), (" b", True, False)])

    def test_heading_not_found_lists_headings(self):
        d = doc_of(("Prices\n", "HEADING_2"), ("x\n", "NORMAL_TEXT"))
        with self.assertRaises(LookupError) as cm:
            gdocs._insert_point(d, "Hours")
        self.assertIn("Prices", str(cm.exception))

    def test_empty_markdown_refused(self):
        with self.assertRaises(ValueError):
            gdocs.markdown_requests("\n\n", 5)


class DocsCalls(unittest.TestCase):
    def test_text_flattens_headings_bullets_tables(self):
        d = doc_of(
            ("Handbook\n", "TITLE"), ("Hours\n", "HEADING_2"), ("Open daily.\n", "NORMAL_TEXT"),
            ("First\n", "NORMAL_TEXT", {"listId": "ol"}), ("Dot\n", "NORMAL_TEXT", {"listId": "ul", "nestingLevel": 0}),
        )
        d["body"]["content"].append({"startIndex": 40, "table": {"tableRows": [{"tableCells": [
            {"content": [para("Day\n", 41)]}, {"content": [para("Time\n", 45)]}]}]}})
        f = Fake(d)
        with patched(f):
            out = gdocs.text(DOC_URL)
            indexed = gdocs.text(DOC_ID, indices=True)
        self.assertEqual(out, "title: Handbook\n\n# Handbook\n## Hours\nOpen daily.\n1. First\n- Dot\n| Day | Time |\n")
        self.assertIn("[10] ## Hours", indexed)
        self.assertEqual(f.calls[0][1], f"documents/{DOC_ID}")

    def test_insert_markdown_pins_revision(self):
        d = doc_of(("Intro\n", "NORMAL_TEXT"), revision="rev-9")
        f = Fake(d)
        with patched(f):
            out = gdocs.insert_markdown(DOC_URL, "- one\n- two")
        self.assertEqual(out, {"index": 6, "paragraphs": 2})
        verb, path, payload = f.calls[1]
        self.assertEqual((verb, path), ("POST", f"documents/{DOC_ID}:batchUpdate"))
        self.assertEqual(payload["writeControl"], {"requiredRevisionId": "rev-9"})
        self.assertEqual(payload["requests"][0]["insertText"]["text"], "\none\ntwo")

    def test_insert_markdown_dry_run_sends_nothing(self):
        f = Fake(doc_of(("Intro\n", "NORMAL_TEXT")))
        with patched(f):
            out = gdocs.insert_markdown(DOC_ID, "hello", dry_run=True)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(out["requests"][0]["insertText"]["text"], "\nhello")

    def test_replace_text(self):
        f = Fake({}, {"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})
        with patched(f):
            self.assertEqual(gdocs.replace_text(DOC_ID, "2025", "2026", match_case=False), {"occurrences_changed": 3})
        self.assertEqual(f.calls[0][2], {"requests": [{"replaceAllText": {"containsText": {"text": "2025", "matchCase": False}, "replaceText": "2026"}}]})

    def test_requests_passthrough_and_validation(self):
        f = Fake({})
        raw = [{"insertText": {"location": {"index": 1}, "text": "x"}}]
        with patched(f):
            gdocs.requests(DOC_ID, raw)
            with self.assertRaises(ValueError):
                gdocs.requests(DOC_ID, [])
        self.assertEqual(f.calls[0][2], {"requests": raw})

    def test_cli_text(self):
        out = io.StringIO()
        with patched(Fake(doc_of(("Hi\n", "NORMAL_TEXT")))), redirect_stdout(out):
            self.assertEqual(gdocs._main(["text", DOC_ID]), 0)
        self.assertEqual(out.getvalue(), "title: Handbook\n\nHi\n\n")

    def test_cli_requests_json(self):
        f = Fake({}, {"replies": [{}]})
        out = io.StringIO()
        with patched(f), redirect_stdout(out):
            self.assertEqual(gdocs._main(["requests", DOC_ID, "--json", '[{"insertText":{"location":{"index":1},"text":"x"}}]']), 0)
        self.assertEqual(json.loads(out.getvalue()), {"replies": [{}]})


if __name__ == "__main__":
    unittest.main()
