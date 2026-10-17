# /// script
# requires-python = ">=3.11"
# ///
"""Render sent-vs-proposed evidence as live-edit or shadow-readiness reports.

`rp dev learning evidence --plane deltas --include-bodies` returns the two bodies that matter for a
dream cycle: what the brain proposed and what the human actually sent. JSON is unreadable for a
human reviewer and a line diff is wrong for prose — humans rewrite *inside* sentences, so a
line-granular diff paints a whole paragraph red/green and hides the actual edit.

Live rows retain the edit report. Rows carrying the evidence wire's ``shadow: true`` field use a
verdict-first blind-comparison report instead; no similarity or prose heuristic guesses shadow mode.
The script produces one self-contained HTML file that shows, per delta:

  * the run/thread identifiers needed to drill further (`rp run debug <id>`);
  * a paragraph alignment (fuzzy, so a rewritten paragraph stays paired with its original);
  * a word-level inline diff inside each aligned pair, plus a side-by-side view of the same data;
  * kept/removed/added word counts, so "polish" and "full rewrite" are distinguishable at a glance;
  * the agent's per-delta conclusion next to the evidence it was drawn from.

    sent_delta_report.py --limit 20
    sent_delta_report.py --limit 20 --out .replypen/dream/2026-07-29-sent-deltas.html
    sent_delta_report.py --from-json deltas.json --annotations notes.json --conclusion concl.md

Read-only: it shells out to `rp` for a GET, or reads a JSON file, and writes one local HTML file.

PRIVACY: the output embeds raw customer mail. Keep it under the gitignored `.replypen/` tree
(the default), never commit it, and delete it when the dream cycle is done.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


def _marker(root: Path) -> Path:
    """The brain's committed binding: `.replypen.toml`, else the pre-rebrand `.rootcause.toml`."""
    new = root / ".replypen.toml"
    return new if new.exists() or not (root / ".rootcause.toml").exists() else root / ".rootcause.toml"

# A token is one word (or one punctuation mark) plus the whitespace that follows it. Keeping
# whitespace attached matters: with whitespace as its own token every "equal" space would split an
# otherwise contiguous edit into a string of separate red/green fragments.
TOKEN_RE = re.compile(r"(\w+|[^\w\s])(\s*)", re.UNICODE)
PARA_SPLIT_RE = re.compile(r"\n\s*\n")
# Below this paragraph similarity two paragraphs are unrelated, so pairing them would produce a
# word diff that is noisier than showing them as a clean delete + insert.
PAIR_THRESHOLD = 0.34
# Where a reply's quoted history starts. Sent bodies arrive with the full quoted thread appended;
# diffing that against a proposal that never contained it would drown the real edit in green.
QUOTE_START_RE = re.compile(
    r"""^(?:>.*
      |-{2,}\s*(?:Original\s+Message|Oorspronkelijk\s+bericht|Forwarded\s+message)\s*-{2,}
      |(?:From|Van|Von)\s*:\s*.+<.+@.+>)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
# An attribution line ("Op <date> schreef <name> <addr>:") sits directly above the quote block and
# often wraps over two lines, so it is absorbed by walking backwards from the block.
ATTRIBUTION_RE = re.compile(
    r"[\w.+-]+@[\w.-]+|\b(?:schreef|wrote|geschreven|a\s+écrit|escribió|schrieb)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------- evidence loading


def load_evidence(args: argparse.Namespace) -> dict:
    if args.from_json:
        raw = sys.stdin.read() if args.from_json == "-" else Path(args.from_json).read_text("utf-8")
        return json.loads(raw)
    plane = "shadow" if getattr(args, "shadow", False) else "deltas"
    cmd = [_cli(), "dev", "learning", "evidence", "--plane", plane, "--include-bodies",
           "--limit", str(args.limit), "-o", "json", "--raw-output"]
    if args.project:
        cmd = [_cli(), "--project", args.project, *cmd[1:]]
    if args.tenant:
        cmd = [cmd[0], "--tenant", args.tenant, *cmd[1:]]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"rp failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def local_scope(cwd: Path) -> tuple[str, str]:
    """Best-effort project/tenant label from the brain checkout's committed marker."""
    marker = _marker(cwd)
    if not marker.is_file():
        return "", ""
    try:
        data = tomllib.loads(marker.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    return str(data.get("project", "")), str(data.get("tenant", ""))


# ---------------------------------------------------------------------------- diff model


@dataclass
class Row:
    """One aligned paragraph pair: equal, changed (word diff), deleted, or inserted."""

    kind: str  # equal | changed | delete | insert
    left: list[tuple[str, str]] = field(default_factory=list)   # (op, text) for the proposed side
    right: list[tuple[str, str]] = field(default_factory=list)  # (op, text) for the sent side
    inline: list[tuple[str, str]] = field(default_factory=list)  # both sides interleaved in order


@dataclass
class Diff:
    rows: list[Row]
    kept: int
    removed: int
    added: int
    quoted: str = ""  # quoted history stripped off the sent body, shown but not diffed

    @property
    def total(self) -> int:
        return self.kept + self.removed + self.added

    @property
    def similarity(self) -> float:
        return (2 * self.kept / (2 * self.kept + self.removed + self.added)) if self.total else 1.0

    @property
    def shape(self) -> str:
        """A one-word verdict a reviewer can scan without reading the diff."""
        if not self.removed and not self.added:
            return "identical"
        if self.similarity >= 0.85:
            return "polish"
        if self.similarity >= 0.5:
            return "reworked"
        if self.kept and self.similarity >= 0.2:
            return "heavy rewrite"
        return "replaced"


def split_quotes(text: str) -> tuple[str, str]:
    """Split a body into (new text, quoted history). The trailer is shown, but never diffed."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not QUOTE_START_RE.match(line.strip()):
            continue
        start = i
        while start > 0 and not lines[start - 1].strip():
            start -= 1  # blank lines between the attribution and the quote block
        if start == 0 or not ATTRIBUTION_RE.search(lines[start - 1]):
            start = i
        while start > 0 and lines[start - 1].strip() and ATTRIBUTION_RE.search(lines[start - 1]):
            start -= 1  # the attribution itself, which often wraps over two lines
        return "\n".join(lines[:start]).rstrip(), "\n".join(lines[start:]).strip()
    return text or "", ""


def paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in PARA_SPLIT_RE.split((text or "").replace("\r\n", "\n"))]
    return [p for p in parts if p]


def tokens(text: str) -> list[tuple[str, str]]:
    """Split into (word-or-punctuation, trailing whitespace) pairs."""
    return TOKEN_RE.findall(text)


def _keys(toks: list[tuple[str, str]]) -> list[str]:
    return [core.casefold() for core, _ in toks]


def _words(toks: list[tuple[str, str]]) -> int:
    return sum(1 for core, _ in toks if core.isalnum() or "_" in core)


def _join(toks: list[tuple[str, str]]) -> str:
    return "".join(core + ws for core, ws in toks)


def word_diff(a: str, b: str) -> tuple[Row, int, int, int]:
    """Word-level diff of two paragraphs; returns (row, kept, removed, added)."""
    at, bt = tokens(a), tokens(b)
    matcher = difflib.SequenceMatcher(None, _keys(at), _keys(bt), autojunk=False)
    row = Row("changed")
    kept = removed = added = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            row.left.append(("eq", _join(at[i1:i2])))
            row.right.append(("eq", _join(bt[j1:j2])))
            row.inline.append(("eq", _join(at[i1:i2])))
            kept += _words(at[i1:i2])
            continue
        if op in ("delete", "replace"):
            row.left.append(("del", _join(at[i1:i2])))
            row.inline.append(("del", _join(at[i1:i2])))
            removed += _words(at[i1:i2])
        if op in ("insert", "replace"):
            row.right.append(("ins", _join(bt[j1:j2])))
            row.inline.append(("ins", _join(bt[j1:j2])))
            added += _words(bt[j1:j2])
    return row, kept, removed, added


def align(a_paras: list[str], b_paras: list[str]) -> list[tuple[int | None, int | None]]:
    """Fuzzy paragraph alignment (Needleman-Wunsch over similarity ratios).

    Exact-match alignment would pair almost nothing in rewritten prose; this keeps a paragraph
    paired with its rewritten self so the word diff below it is meaningful, while genuinely new or
    dropped paragraphs fall out as unpaired.
    """
    n, m = len(a_paras), len(b_paras)
    ratios = [[0.0] * m for _ in range(n)]
    for i, pa in enumerate(a_paras):
        ka = _keys(tokens(pa))
        for j, pb in enumerate(b_paras):
            matcher = difflib.SequenceMatcher(None, ka, _keys(tokens(pb)), autojunk=False)
            if matcher.real_quick_ratio() < PAIR_THRESHOLD or matcher.quick_ratio() < PAIR_THRESHOLD:
                continue
            r = matcher.ratio()
            ratios[i][j] = r if r >= PAIR_THRESHOLD else 0.0

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair = dp[i - 1][j - 1] + ratios[i - 1][j - 1] if ratios[i - 1][j - 1] else -1.0
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], pair)

    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        r = ratios[i - 1][j - 1] if i > 0 and j > 0 else 0.0
        if i > 0 and j > 0 and r and abs(dp[i][j] - (dp[i - 1][j - 1] + r)) < 1e-9:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            pairs.append((None, j - 1))
            j -= 1
        else:
            pairs.append((i - 1, None))
            i -= 1
    pairs.reverse()
    return pairs


def build_diff(proposed: str, sent: str, keep_quotes: bool = False,
               quoted: str | None = None) -> Diff:
    """Diff a proposed draft against a sent body.

    quoted is the escape hatch for a PRE-CLEANED sent side (the server's `sent_body_clean`): pass the
    trailer to display and the sent text is diffed as given. Left None, split_quotes does the work —
    the fallback path for an older server, kept because brains pin kit tags and can outlive a deploy.
    """
    if quoted is None:
        quoted = ""
        if not keep_quotes:
            proposed, _ = split_quotes(proposed)
            sent, quoted = split_quotes(sent)
    a_paras, b_paras = paragraphs(proposed), paragraphs(sent)
    rows: list[Row] = []
    kept = removed = added = 0
    for ai, bj in align(a_paras, b_paras):
        if ai is not None and bj is not None:
            a, b = a_paras[ai], b_paras[bj]
            if a == b:
                rows.append(Row("equal", [("eq", a)], [("eq", b)], [("eq", a)]))
                kept += _words(tokens(a))
                continue
            row, k, r, d = word_diff(a, b)
            if not r and not d:
                # Same words, different line wrapping — sent bodies come back hard-wrapped.
                row.kind = "equal"
            rows.append(row)
            kept, removed, added = kept + k, removed + r, added + d
        elif ai is not None:
            rows.append(Row("delete", [("del", a_paras[ai])], [], [("del", a_paras[ai])]))
            removed += _words(tokens(a_paras[ai]))
        else:
            rows.append(Row("insert", [], [("ins", b_paras[bj])], [("ins", b_paras[bj])]))
            added += _words(tokens(b_paras[bj]))
    return Diff(rows, kept, removed, added, quoted)


def diff_for(item: dict, keep_quotes: bool = False) -> Diff:
    """Diff one evidence item, preferring the server's cleaned sent side.

    `sent_body_clean` is `cleanbody` — the same deterministic cleaner the pipeline runs on inbound
    mail: quoted history in every locale we see, glued Outlook `Van:/Verzonden:` header blocks,
    mobile/vendor footers, legal boilerplate. It is strictly stronger than split_quotes, which stays
    the fallback for payloads from an older server. The collapsed history block still comes from the
    local heuristic over the RAW body, so nothing that was stripped disappears from the human report.
    """
    clean = (item.get("sent_body_clean") or "").strip()
    if not clean or keep_quotes:
        return build_diff(item["proposed_body"], item["sent_body"], keep_quotes)
    return build_diff(item["proposed_body"], clean,
                      quoted=split_quotes(item["sent_body"])[1])


# ---------------------------------------------------------------------------- signals

# Cheap regex-level markers over what the human removed against what they added. They are a *sorting*
# aid for the agent — three deltas sharing a marker is worth reading as a pattern, one is an anecdote.
# None of them is a judgement; the agent still has to read the bodies before writing anything durable.
DAY_WORDS = ("maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag"
             "|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
             "|januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december")
DATE_RE = re.compile(rf"\b\d{{1,2}}[/.-]\d{{1,2}}(?:[/.-]\d{{2,4}})?\b|\b\d{{1,2}}\s?u\s?\d{{2}}\b"
                     rf"|\b\d{{1,2}}:\d{{2}}\b|\b(?:{DAY_WORDS})\b", re.IGNORECASE)
LINK_RE = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\[\[.+?\]\]|\{\{.+?\}\}|\bTODO\b|\bTBD\b|<[A-Z_]{4,}>")
GREETING_RE = re.compile(r"^(beste|hallo|hi|hey|dag|geachte|goedendag)\b", re.IGNORECASE)
SIGNOFF_RE = re.compile(r"\b(groeten|groetjes|regards|vriendelijke|hartelijk|sincerely)\b",
                        re.IGNORECASE)

SIGNAL_LABELS = {
    "date_dropped": "brain named a date/time the human took out",
    "link_dropped": "brain sent a link the human took out",
    "placeholder_leak": "draft still held an unfilled placeholder",
    "confirm_dropped": "brain asked the customer to confirm; the human did not",
    "greeting_changed": "greeting or addressee changed",
    "signoff_changed": "sign-off block rewritten",
    "human_wrote_more": "human answered with substantially more than the draft",
    "human_wrote_less": "human cut the draft down",
}

SHADOW_VERDICTS = (
    "divergent_facts",
    "missed_content",
    "same_outcome_details_differ",
    "equivalent",
    "not_answerable",
    "unjudged",
)
SHADOW_VERDICT_LABELS = {
    "divergent_facts": "Divergent facts",
    "missed_content": "Missed content",
    "same_outcome_details_differ": "Same outcome, details differ",
    "equivalent": "Equivalent",
    "not_answerable": "Not answerable",
    "unjudged": "Unjudged",
}
ANSWERABLE_SHADOW_VERDICTS = {
    "divergent_facts", "missed_content", "same_outcome_details_differ", "equivalent"
}


def signals(diff: Diff) -> list[str]:
    gone = " ".join(t for row in diff.rows for op, t in row.left if op == "del")
    new = " ".join(t for row in diff.rows for op, t in row.right if op == "ins")
    found = []
    if DATE_RE.search(gone) and not DATE_RE.search(new):
        found.append("date_dropped")
    if LINK_RE.search(gone) and not LINK_RE.search(new):
        found.append("link_dropped")
    if PLACEHOLDER_RE.search(gone):
        found.append("placeholder_leak")
    if "?" in gone and "?" not in new:
        found.append("confirm_dropped")
    changed = [row for row in diff.rows if row.kind != "equal"]
    if changed and GREETING_RE.match(_flat(changed[0].left + changed[0].right)):
        found.append("greeting_changed")
    if any(SIGNOFF_RE.search(_flat(row.left + row.right)) for row in changed):
        found.append("signoff_changed")
    if diff.added >= 2 * diff.removed and diff.added >= 20:
        found.append("human_wrote_more")
    elif diff.removed >= 2 * diff.added and diff.removed >= 20:
        found.append("human_wrote_less")
    return found


def _flat(ops: list[tuple[str, str]]) -> str:
    return re.sub(r"\s+", " ", "".join(t for _, t in ops)).strip()


def short(value: object) -> str:
    """First uuid segment — enough to name a delta in prose, cheap in tokens."""
    return str(value or "").split("-")[0] or "?"


def is_shadow(item: dict) -> bool:
    """Use only the pinned evidence-wire identity; textual similarity cannot identify shadow."""
    return item.get("shadow") is True


def shadow_verdict(item: dict) -> str:
    value = str(item.get("shadow_verdict") or "").strip()
    return value if value in SHADOW_VERDICTS[:-1] else "unjudged"


def served_score(item: dict) -> int | None:
    value = item.get("served_score")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5 else None


def _time_sort_value(value: object) -> float:
    try:
        return -datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def shadow_sort_key(item: dict) -> tuple[int, float, str]:
    verdict = shadow_verdict(item)
    return SHADOW_VERDICTS.index(verdict), _time_sort_value(item.get("sent_at")), str(item.get("id") or "")


def shadow_readiness(items: list[dict]) -> tuple[int, int]:
    verdicts = [shadow_verdict(item) for item in items]
    close = sum(v in {"equivalent", "same_outcome_details_differ"} for v in verdicts)
    answerable = sum(v in ANSWERABLE_SHADOW_VERDICTS for v in verdicts)
    return close, answerable


# ---------------------------------------------------------------------------- markdown (agent)

MD_CAP = 400  # per paragraph; the agent needs the wording, not every clause of a long block


def _clip(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= MD_CAP else flat[:MD_CAP].rstrip() + " …"


def md_ops(ops: list[tuple[str, str]]) -> str:
    out = []
    for op, text in ops:
        body = re.sub(r"\s+", " ", text)
        if op == "eq":
            out.append(body)
            continue
        # Trailing space stays outside the marker, or words run together on the next chunk.
        core, trail = body.rstrip(), body[len(body.rstrip()):]
        out.append(f"[-{core}-]{trail}" if op == "del" else f"{{+{core}+}}{trail}")
    return _clip("".join(out))


def category_of(item: dict) -> str:
    """The server's coarse delta_category. Empty for rows no describer ran on (the Embassy lane, or a
    capture whose LLM pass failed) — those keep the regex signal markers as their only grouping."""
    return str(item.get("delta_category") or "").strip()


def render_markdown(items: list[dict], diffs: list[Diff], notes: dict[str, str], scope: str,
                    conclusion: str, html_path: str, aged_out: list[dict] | None = None) -> str:
    aged_out = aged_out or []
    shapes: dict[str, int] = {}
    index: dict[str, list[str]] = {}
    categories: dict[str, list[str]] = {}
    for item, diff in zip(items, diffs):
        shapes[diff.shape] = shapes.get(diff.shape, 0) + 1
        categories.setdefault(category_of(item) or "uncategorized", []).append(short(item.get("id")))
        for name in signals(diff):
            index.setdefault(name, []).append(short(item.get("id")))

    out = [f"# Sent vs proposed — {scope}", ""]
    out.append(f"{len(items)} deltas, most-rewritten first · "
               + " · ".join(f"{n}× {s}" for s, n in sorted(shapes.items(), key=lambda kv: -kv[1])))
    if aged_out:
        out.append(f"{len(aged_out)} further delta(s) aged out — bodies scrubbed at the 14-day "
                   "retention TTL, descriptions below.")
    out.append(f"Human-facing diff report: `{html_path}`")
    if conclusion:
        out += ["", "## Conclusion so far", "", conclusion]
    if categories:
        # The server's own axis, and the one to group by FIRST: it was assigned from the bodies at
        # capture time, survives retention, and means the same thing across projects — unlike the
        # regex markers below, which are this tenant's prose at kit-release cadence.
        out += ["", "## Delta categories", "",
                "The server's capture-time classification. Group by this first; the regex signals "
                "below are a secondary axis.", "", "| n | category | deltas |", "|--:|---|---|"]
        for name, ids in sorted(categories.items(), key=lambda kv: -len(kv[1])):
            out.append(f"| {len(ids)} | {name} | {', '.join(ids)} |")
    if index:
        out += ["", "## Signal index", "",
                "Regex-level markers, not judgements — one delta is an anecdote, three sharing a "
                "marker is a pattern worth drilling.", "", "| n | signal | deltas |", "|--:|---|---|"]
        for name, ids in sorted(index.items(), key=lambda kv: -len(kv[1])):
            out.append(f"| {len(ids)} | {SIGNAL_LABELS[name]} | {', '.join(ids)} |")

    out += ["", "## Deltas", "",
            "`[-…-]` the human removed · `{+…+}` the human added · unchanged paragraphs omitted."]
    for item, diff in zip(items, diffs):
        run_id = item.get("related_run_id") or ""
        head = f"### {short(item.get('id'))} · {diff.shape} · −{diff.removed}/+{diff.added} words"
        if cat := category_of(item):
            head += f" · {cat}"
        meta = [f"run `{run_id}`" if run_id else "no related run (drill via thread)"]
        if url := item.get("run_url"):
            meta.append(f"[trace]({url})")
        # "server similarity" is NOT diff.similarity above: the server measures character-level
        # Levenshtein over its own normalized text, this script measures word-level Dice over display
        # text. Different numbers by design — never read one as the other.
        if isinstance(item.get("similarity"), (int, float)):
            meta.append(f"server similarity {item['similarity']:.0%}")
        if found := signals(diff):
            meta.append("signals: " + ", ".join(found))
        out += ["", head, "", " · ".join(meta)]
        if item.get("delta_description"):
            out.append(f"server note: {str(item['delta_description']).strip()}")
        if notes.get(str(item.get("id"))):
            out.append(f"**conclusion:** {notes[str(item['id'])]}")
        out.append("")
        skipped = 0
        for row in diff.rows:
            if row.kind == "equal":
                skipped += 1
            elif row.kind == "delete":
                out.append(f"- {_clip(_flat(row.left))}")
            elif row.kind == "insert":
                out.append(f"+ {_clip(_flat(row.right))}")
            else:
                out.append(f"~ {md_ops(row.inline)}")
        if skipped:
            out.append(f"({skipped} unchanged paragraph{'s' if skipped > 1 else ''} omitted)")

    if aged_out:
        # The durable half of the plane: retention blanks the bodies at EmailTTL but keeps the
        # description + category, so an old edit still carries its lesson — just not its wording.
        out += ["", "## Aged out (description only)", "",
                "Bodies scrubbed at the 14-day retention TTL; no diff is possible. Treat these as "
                "corroboration for a pattern, never as the sole evidence for a durable edit.", ""]
        for item in aged_out:
            bits = [f"`{short(item.get('id'))}`"]
            if cat := category_of(item):
                bits.append(cat)
            if url := item.get("run_url"):
                bits.append(f"[trace]({url})")
            out.append(f"- {' · '.join(bits)} — {str(item.get('delta_description') or '').strip() or 'no description'}")

    out += ["", "## Next", "",
            "1. Group by signal before deciding — a single delta rarely justifies a durable edit.",
            "2. Drill one representative per group: `rp run debug <run-id>`.",
            "3. Route with the durable-home table in the skill: brain file, persona settings, triage "
            "policy/rule, or action wiring. Wording-only deltas belong in persona, never in a brain file.",
            "4. Verify with `rp ask` against a `dev/*` brain ref before publishing.", ""]
    return "\n".join(out)


def _score_distribution(items: list[dict]) -> str:
    counts = {score: 0 for score in range(1, 6)}
    unscored = 0
    for item in items:
        score = served_score(item)
        if score is None:
            unscored += 1
        else:
            counts[score] += 1
    parts = [f"{score}/5: {counts[score]}" for score in range(5, 0, -1)]
    if unscored:
        parts.append(f"unscored: {unscored}")
    return " · ".join(parts)


def _shadow_groups(items: list[dict], diffs: list[Diff]) -> list[tuple[str, list[tuple[dict, Diff]]]]:
    grouped: dict[str, list[tuple[dict, Diff]]] = {verdict: [] for verdict in SHADOW_VERDICTS}
    for item, diff in zip(items, diffs, strict=True):
        grouped[shadow_verdict(item)].append((item, diff))
    return [(verdict, grouped[verdict]) for verdict in SHADOW_VERDICTS if grouped[verdict]]


def render_shadow_markdown(items: list[dict], diffs: list[Diff], notes: dict[str, str], scope: str,
                           conclusion: str, html_path: str) -> str:
    close, answerable = shadow_readiness(items)
    readiness = f"{100 * close / answerable:.0f}%" if answerable else "—"
    verdict_counts = {verdict: 0 for verdict in SHADOW_VERDICTS}
    themes: dict[str, list[dict]] = {}
    for item in items:
        verdict_counts[shadow_verdict(item)] += 1
        themes.setdefault(category_of(item) or "uncategorized", []).append(item)

    out = [f"# Shadow readiness — {scope}", "",
           f"{len(items)} blind comparisons · readiness **{readiness}** ({close}/{answerable} "
           "close/answerable; `not_answerable` and unjudged excluded)",
           f"Served scores: {_score_distribution(items)}",
           f"Human-facing shadow report: `{html_path}`"]
    if conclusion:
        out += ["", "## Conclusion so far", "", conclusion]

    out += ["", "## Verdicts", "", "| n | verdict | readiness role |", "|--:|---|---|"]
    for verdict in SHADOW_VERDICTS:
        count = verdict_counts[verdict]
        if not count:
            continue
        role = ("close" if verdict in {"equivalent", "same_outcome_details_differ"}
                else "answerable miss" if verdict in {"divergent_facts", "missed_content"}
                else "excluded")
        out.append(f"| {count} | {SHADOW_VERDICT_LABELS[verdict]} | {role} |")

    out += ["", "## Themes", "",
            "The server category is a first-pass theme, not a root-cause attribution. Read a verdict "
            "group first, then drill one representative run before routing a lesson.", "",
            "| n | theme | verdicts | deltas |", "|--:|---|---|---|"]
    for theme, members in sorted(themes.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        member_verdicts = sorted({shadow_verdict(item) for item in members},
                                 key=SHADOW_VERDICTS.index)
        out.append(f"| {len(members)} | {theme} | "
                   f"{', '.join(SHADOW_VERDICT_LABELS[v] for v in member_verdicts)} | "
                   f"{', '.join(short(item.get('id')) for item in members)} |")

    out += ["", "## Comparisons by verdict", "",
            "`[-…-]` only in ours · `{+…+}` only in human answer · unchanged paragraphs omitted."]
    for verdict, members in _shadow_groups(items, diffs):
        out += ["", f"### {SHADOW_VERDICT_LABELS[verdict]} ({len(members)})"]
        for item, diff in members:
            topic = str(item.get("topic") or "").strip()
            title = f"#### {short(item.get('id'))}"
            if topic:
                title += f" · {topic}"
            out += ["", title, ""]
            meta = []
            if run_id := item.get("related_run_id"):
                meta.append(f"run `{run_id}`")
            if url := item.get("run_url"):
                meta.append(f"[trace]({url})")
            score = served_score(item)
            meta.append(f"served score {score}/5" if score is not None else "served score —")
            if theme := category_of(item):
                meta.append(f"theme {theme}")
            if isinstance(item.get("similarity"), (int, float)):
                meta.append(f"server similarity {item['similarity']:.0%}")
            out.append(" · ".join(meta))
            if question := str(item.get("question_excerpt") or "").strip():
                out += ["", f"**Question:** {_clip(question)}"]
            if item.get("delta_description"):
                out += ["", f"**Server comparison:** {str(item['delta_description']).strip()}"]
            if notes.get(str(item.get("id"))):
                out += ["", f"**Conclusion:** {notes[str(item['id'])]}"]
            if not item.get("proposed_body") or not item.get("sent_body"):
                out += ["", "Bodies unavailable; use the verdict and description only."]
                continue
            out.append("")
            skipped = 0
            for row in diff.rows:
                if row.kind == "equal":
                    skipped += 1
                elif row.kind == "delete":
                    out.append(f"- only in ours: {_clip(_flat(row.left))}")
                elif row.kind == "insert":
                    out.append(f"+ only in human answer: {_clip(_flat(row.right))}")
                else:
                    out.append(f"~ {md_ops(row.inline)}")
            if skipped:
                out.append(f"({skipped} unchanged paragraph{'s' if skipped > 1 else ''} omitted)")

    out += ["", "## Next", "",
            "1. Start with verdict and recurring themes; low word overlap alone is not a lesson.",
            "2. Drill one representative miss: `rp run debug <run-id>`.",
            "3. Route the evidenced cause with the Shadow mode table in the skill.",
            "4. Treat unpaired runs as awaiting a human answer unless thread evidence says otherwise.", ""]
    return "\n".join(out)


def render_missing_live_markdown(items: list[dict]) -> str:
    out = ["## Missing live bodies", "",
           "These live rows were returned without bodies and cannot be diffed. Re-fetch with "
           "`--include-bodies`; do not infer a lesson from the description alone.", ""]
    for item in items:
        bits = [f"`{short(item.get('id'))}`"]
        if category := category_of(item):
            bits.append(category)
        if url := item.get("run_url"):
            bits.append(f"[trace]({url})")
        description = str(item.get("delta_description") or "").strip() or "no description"
        out.append(f"- {' · '.join(bits)} — {description}")
    return "\n".join(out)


# ---------------------------------------------------------------------------- rendering (human)

CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#5b6570;--line:#d8dee4;--card:#fff;--sub:#f6f8fa;
--del-bg:#ffe3e3;--del-fg:#8b1a1a;--ins-bg:#dcf5e3;--ins-fg:#0f5427;--accent:#0b62d6;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1418;--fg:#e6edf3;--muted:#9aa7b2;--line:#2b333c;
--card:#161c22;--sub:#11171c;--del-bg:#4a1f22;--del-fg:#ffb4b4;--ins-bg:#173a26;--ins-fg:#9ff0bd;
--accent:#68a9ff;}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0}
.wrap{max-width:1180px;margin:0 auto}
.meta{color:var(--muted);font-size:12.5px}
.foot{padding:8px 14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)}
.foot a{color:var(--accent);text-decoration:none}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);
display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--card);color:var(--fg);
border-radius:6px;cursor:pointer}
button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
.card{border:1px solid var(--line);border-radius:8px;background:var(--card);margin:0 0 20px;overflow:hidden}
.head{padding:12px 14px;background:var(--sub);border-bottom:1px solid var(--line)}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.tag{font-size:11.5px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--muted)}
.tag.shape{border-color:var(--accent);color:var(--accent)}
code,kbd,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.cmd{margin-top:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;
overflow-x:auto;white-space:pre}
.note{margin:0;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--sub);
white-space:pre-wrap}
.note b{color:var(--accent)}
.body{padding:4px 0}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td{vertical-align:top;padding:8px 14px;white-space:pre-wrap;overflow-wrap:anywhere;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
td.l{border-right:1px solid var(--line)}
tr.equal td{color:var(--muted)}
del{background:var(--del-bg);color:var(--del-fg);text-decoration:line-through;border-radius:2px}
ins{background:var(--ins-bg);color:var(--ins-fg);text-decoration:none;border-radius:2px}
.colhead td{background:var(--sub);color:var(--muted);font-size:11.5px;text-transform:uppercase;
letter-spacing:.04em;white-space:normal;border-bottom:1px solid var(--line)}
body.inline .side,body.inline .colhead{display:none}
body:not(.inline) .inlineview{display:none}
body.hide-equal tr.equal{display:none}
.empty{color:var(--muted);font-style:italic}
.quoted{border-top:1px solid var(--line);background:var(--sub)}
.quoted summary{padding:8px 14px;cursor:pointer;color:var(--muted);font-size:12.5px}
.quoted pre{margin:0;padding:0 14px 12px;white-space:pre-wrap;overflow-wrap:anywhere;
color:var(--muted);font-size:12px}
"""

SHADOW_CSS = """
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:16px 0}
.metric{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card)}
.metric b{display:block;font-size:22px}.metric span{color:var(--muted);font-size:12px}
.verdict-summary,.themes{width:100%;margin:12px 0 20px;border:1px solid var(--line)}
.verdict-summary th,.themes th{text-align:left;background:var(--sub)}
.verdict-summary th,.verdict-summary td,.themes th,.themes td{padding:7px 10px;border-bottom:1px solid var(--line)}
.verdict-group{margin:28px 0}.verdict-group>h2{font-size:18px;margin-bottom:10px}
.shadow-card summary{padding:12px 14px;background:var(--sub);cursor:pointer;font-weight:650}
.shadow-card[open] summary{border-bottom:1px solid var(--line)}
.question{margin:0;padding:10px 14px;border-bottom:1px solid var(--line)}
.question b{color:var(--accent)}.score{font-weight:650;color:var(--accent)}
.section-intro{margin:24px 0 8px}.section-intro h2{font-size:18px;margin-bottom:4px}
.live-section{border-top:3px solid var(--line);margin-top:40px;padding-top:20px}
"""

JS = """
const b=document.body;
function set(view){b.classList.toggle('inline',view==='inline');
document.querySelectorAll('[data-view]').forEach(x=>x.setAttribute('aria-pressed',x.dataset.view===view));}
document.querySelectorAll('[data-view]').forEach(x=>x.onclick=()=>set(x.dataset.view));
const eq=document.getElementById('eq');
eq.onclick=()=>{b.classList.toggle('hide-equal');eq.setAttribute('aria-pressed',b.classList.contains('hide-equal'));};
set('side');
"""


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def spans(ops: list[tuple[str, str]]) -> str:
    out = []
    for op, text in ops:
        if op == "eq":
            out.append(esc(text))
            continue
        # Keep trailing whitespace outside the marker so the highlight ends on the last word.
        body, trail = text.rstrip(), text[len(text.rstrip()):]
        tag = "del" if op == "del" else "ins"
        out.append(f"<{tag}>{esc(body)}</{tag}>{esc(trail)}" if body else esc(text))
    return "".join(out) or '<span class="empty">—</span>'


def render_rows(diff: Diff) -> str:
    side = ['<table class="side"><tr class="colhead"><td class="l">Proposed draft (brain)</td>'
            "<td>Actually sent (human)</td></tr>"]
    inline = ['<table class="inlineview">']
    for row in diff.rows:
        side.append(f'<tr class="{row.kind}"><td class="l">{spans(row.left)}</td>'
                    f"<td>{spans(row.right)}</td></tr>")
        inline.append(f'<tr class="{row.kind}"><td>{spans(row.inline)}</td></tr>')
    side.append("</table>")
    inline.append("</table>")
    return "".join(side) + "".join(inline)


def render_shadow_rows(diff: Diff) -> str:
    side = ['<table class="side"><tr class="colhead"><td class="l">Our blind proposal</td>'
            "<td>Human's independent answer</td></tr>"]
    inline = ['<table class="inlineview">']
    for row in diff.rows:
        side.append(f'<tr class="{row.kind}"><td class="l">{spans(row.left)}</td>'
                    f"<td>{spans(row.right)}</td></tr>")
        inline.append(f'<tr class="{row.kind}"><td>{spans(row.inline)}</td></tr>')
    side.append("</table>")
    inline.append("</table>")
    return "".join(side) + "".join(inline)


def fmt_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return value or "—"


def render_card(index: int, item: dict, diff: Diff, note: str) -> str:
    run_id = item.get("related_run_id") or ""
    url = item.get("run_url") or ""
    # The human report carries plain-language verdicts only — no ids, no metrics, no rp commands
    # (pinned by test). delta_category qualifies: "factual"/"tone"/"omission" reads as English. The
    # payload's numeric `similarity` deliberately does not.
    tags = [f'<span class="tag shape">{esc(diff.shape)}</span>']
    if cat := category_of(item):
        tags.append(f'<span class="tag">{esc(cat)}</span>')
    blocks = [
        f'<div class="head"><h2>#{index} · {esc(fmt_time(item.get("sent_at", "")))} · '
        f'{esc(str(item.get("sender") or "unknown sender"))}</h2>'
        f'<div class="tags">{"".join(tags)}</div></div>'
    ]
    if item.get("delta_description"):
        blocks.append(f'<p class="note"><b>Server delta note:</b> '
                      f'{esc(str(item["delta_description"]))}</p>')
    if note:
        blocks.append(f'<p class="note"><b>Conclusion:</b> {esc(note)}</p>')
    blocks.append(f'<div class="body">{render_rows(diff)}</div>')
    if diff.quoted:
        blocks.append(f"<details class=\"quoted\"><summary>Quoted history on the sent message "
                      f"({len(diff.quoted.splitlines())} lines, excluded from the diff)</summary>"
                      f"<pre>{esc(diff.quoted)}</pre></details>")
    if url:
        blocks.append(f'<div class="foot"><a href="{esc(url)}">Open the run ↗</a></div>')
    elif run_id:
        blocks.append(f'<div class="foot">run <span class="mono">{esc(short(run_id))}</span></div>')
    return f'<div class="card">{"".join(blocks)}</div>'


def render_shadow_card(index: int, item: dict, diff: Diff, note: str) -> str:
    verdict = shadow_verdict(item)
    topic = str(item.get("topic") or "").strip()
    title = topic or f"Comparison #{index}"
    score = served_score(item)
    tags = [f'<span class="tag shape">{esc(SHADOW_VERDICT_LABELS[verdict])}</span>',
            f'<span class="tag score">served score {score}/5</span>' if score is not None
            else '<span class="tag">served score —</span>']
    if theme := category_of(item):
        tags.append(f'<span class="tag">{esc(theme)}</span>')
    blocks = [
        f'<summary>{esc(title)}<div class="tags">{"".join(tags)}</div></summary>'
    ]
    if question := str(item.get("question_excerpt") or "").strip():
        blocks.append(f'<p class="question"><b>Question</b><br>{esc(question)}</p>')
    if item.get("delta_description"):
        blocks.append(f'<p class="note"><b>Server comparison:</b> '
                      f'{esc(str(item["delta_description"]))}</p>')
    if note:
        blocks.append(f'<p class="note"><b>Conclusion:</b> {esc(note)}</p>')
    if item.get("proposed_body") and item.get("sent_body"):
        blocks.append(f'<div class="body">{render_shadow_rows(diff)}</div>')
        if diff.quoted:
            blocks.append(f'<details class="quoted"><summary>Quoted history on the human answer '
                          f'({len(diff.quoted.splitlines())} lines, excluded from the comparison)'
                          f'</summary><pre>{esc(diff.quoted)}</pre></details>')
    else:
        blocks.append('<p class="note">Bodies unavailable; use the verdict and description only.</p>')
    if url := item.get("run_url"):
        blocks.append(f'<div class="foot"><a href="{esc(str(url))}">Drill into this run ↗</a></div>')
    elif run_id := item.get("related_run_id"):
        blocks.append(f'<div class="foot">run <span class="mono">{esc(short(run_id))}</span></div>')
    return f'<details class="card shadow-card"{" open" if index == 1 else ""}>{"".join(blocks)}</details>'


def render_shadow(items: list[dict], diffs: list[Diff], notes: dict[str, str], scope: str,
                  conclusion: str, live_items: list[dict] | None = None,
                  live_diffs: list[Diff] | None = None, live_aged_out: int = 0,
                  live_unavailable: list[dict] | None = None) -> str:
    live_items = live_items or []
    live_diffs = live_diffs or []
    live_unavailable = live_unavailable or []
    close, answerable = shadow_readiness(items)
    readiness = f"{100 * close / answerable:.0f}%" if answerable else "—"
    verdict_counts = {verdict: 0 for verdict in SHADOW_VERDICTS}
    themes: dict[str, list[dict]] = {}
    for item in items:
        verdict_counts[shadow_verdict(item)] += 1
        themes.setdefault(category_of(item) or "uncategorized", []).append(item)

    verdict_rows = []
    for verdict in SHADOW_VERDICTS:
        count = verdict_counts[verdict]
        if count:
            verdict_rows.append(f'<tr><td>{esc(SHADOW_VERDICT_LABELS[verdict])}</td><td>{count}</td></tr>')
    theme_rows = []
    for theme, members in sorted(themes.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        member_verdicts = sorted({shadow_verdict(item) for item in members}, key=SHADOW_VERDICTS.index)
        theme_rows.append(f'<tr><td>{esc(theme)}</td><td>{len(members)}</td><td>'
                          f'{esc(", ".join(SHADOW_VERDICT_LABELS[v] for v in member_verdicts))}</td></tr>')

    groups = []
    for verdict, members in _shadow_groups(items, diffs):
        cards = "".join(render_shadow_card(i + 1, item, diff,
                                            notes.get(str(item.get("id")), ""))
                        for i, (item, diff) in enumerate(members))
        groups.append(f'<section class="verdict-group"><h2>{esc(SHADOW_VERDICT_LABELS[verdict])} '
                      f'({len(members)})</h2>{cards}</section>')

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = (f"<h1>Shadow readiness — {esc(scope)}</h1>"
            f'<div class="meta">{len(items)} blind comparisons · generated {generated}</div>')
    if conclusion:
        head += (f'<div class="card"><p class="note" style="border:0"><b>Conclusion</b>\n'
                 f'{esc(conclusion)}</p></div>')
    controls = ('<div class="controls"><span class="meta">View</span>'
                '<button data-view="side">Side by side</button>'
                '<button data-view="inline">Inline</button>'
                '<button id="eq" aria-pressed="false">Hide unchanged paragraphs</button>'
                '<span class="meta"><del>only in ours</del> · <ins>only in human answer</ins></span></div>')
    metrics = (f'<section class="metrics"><div class="metric"><span>Readiness</span><b>{readiness}</b>'
               f'<span>{close}/{answerable} close/answerable; not-answerable and unjudged excluded</span></div>'
               f'<div class="metric"><span>Served scores</span><b>{sum(served_score(i) is not None for i in items)}/{len(items)}</b>'
               f'<span>{esc(_score_distribution(items))}</span></div></section>')
    overview = ('<div class="section-intro"><h2>Verdict</h2><div class="meta">Quality first; wording overlap is supporting evidence.</div></div>'
                '<table class="verdict-summary"><thead><tr><th>Verdict</th><th>Rows</th></tr></thead>'
                f'<tbody>{"".join(verdict_rows)}</tbody></table>'
                '<div class="section-intro"><h2>Themes</h2><div class="meta">Server categories are a first-pass theme; drill before routing a lesson.</div></div>'
                '<table class="themes"><thead><tr><th>Theme</th><th>Rows</th><th>Verdicts</th></tr></thead>'
                f'<tbody>{"".join(theme_rows)}</tbody></table>'
                '<div class="section-intro"><h2>Drill one run</h2><div class="meta">Grouped by verdict, then newest first.</div></div>')

    live = ""
    if live_items or live_aged_out or live_unavailable:
        shapes: dict[str, int] = {}
        for diff in live_diffs:
            shapes[diff.shape] = shapes.get(diff.shape, 0) + 1
        summary = " · ".join(f"{count}× {shape}" for shape, count in sorted(
            shapes.items(), key=lambda pair: -pair[1]))
        cards = "".join(render_card(i + 1, item, diff, notes.get(str(item.get("id")), ""))
                        for i, (item, diff) in enumerate(zip(live_items, live_diffs, strict=True)))
        aged = (f" · {live_aged_out} aged out (older than 14 days, wording no longer kept)"
                if live_aged_out else "")
        unavailable = ""
        if live_unavailable:
            rows = []
            for item in live_unavailable:
                description = str(item.get("delta_description") or "").strip() or "no description"
                rows.append(f'<li><code>{esc(short(item.get("id")))}</code> — '
                            f'{esc(description)}</li>')
            unavailable = ('<div class="card"><div class="head"><h2>Missing live bodies '
                           f'({len(live_unavailable)})</h2></div><p class="note">Re-fetch with '
                           '<code>--include-bodies</code>; do not infer a lesson from the description '
                           f'alone.</p><ul>{"".join(rows)}</ul></div>')
        live = (f'<section class="live-section"><h1>Sent vs proposed — live edits</h1>'
                f'<div class="meta">{len(live_items)} deltas · {esc(summary or "—")}{esc(aged)}</div>'
                '<p class="meta"><del>removed by human</del> · <ins>added by human</ins></p>'
                f'{cards}{unavailable}</section>')

    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Shadow readiness — {esc(scope)}</title><style>{CSS}{SHADOW_CSS}</style></head>"
            f'<body class="inline-off"><div class="wrap">{head}{metrics}{controls}{overview}'
            f'{"".join(groups)}{live}</div><script>{JS}</script></body></html>')


def render(items: list[dict], diffs: list[Diff], notes: dict[str, str], scope: str,
           conclusion: str, aged_out: int = 0) -> str:
    shapes: dict[str, int] = {}
    for d in diffs:
        shapes[d.shape] = shapes.get(d.shape, 0) + 1
    summary = " · ".join(f"{count}× {shape}" for shape, count in sorted(shapes.items(),
                                                                       key=lambda kv: -kv[1]))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cards = "".join(render_card(i + 1, item, diff, notes.get(str(item.get("id")), ""))
                    for i, (item, diff) in enumerate(zip(items, diffs)))
    aged = f" · {aged_out} aged out (older than 14 days, wording no longer kept)" if aged_out else ""
    head = (f"<h1>Sent vs proposed — {esc(scope)}</h1>"
            f'<div class="meta">{len(items)} deltas · {esc(summary or "—")}{esc(aged)} · generated {generated}</div>')
    if conclusion:
        head += f'<div class="card"><p class="note" style="border:0"><b>Conclusion</b>\n{esc(conclusion)}</p></div>'
    controls = ('<div class="controls"><span class="meta">View</span>'
                '<button data-view="side">Side by side</button>'
                '<button data-view="inline">Inline</button>'
                '<button id="eq" aria-pressed="false">Hide unchanged paragraphs</button>'
                '<span class="meta"><del>removed by human</del> · <ins>added by human</ins></span></div>')
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Sent vs proposed — {esc(scope)}</title><style>{CSS}</style></head>"
            f'<body class="inline-off"><div class="wrap">{head}{controls}{cards}</div>'
            f"<script>{JS}</script></body></html>")


# ---------------------------------------------------------------------------- entrypoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20, help="deltas to fetch (server cap 100)")
    parser.add_argument("--shadow", action="store_true",
                        help="fetch a recent, verdict-neutral shadow sample instead of live deltas")
    parser.add_argument("--project", default="", help="explicit project slug (all-projects token)")
    parser.add_argument("--tenant", default="", help="explicit tenant slug")
    parser.add_argument("--from-json", default="",
                        help="read `rp dev learning evidence` JSON from a file or '-' for stdin")
    parser.add_argument("--annotations", default="",
                        help='JSON map {"<delta-id>": "conclusion"} shown next to each delta')
    parser.add_argument("--conclusion", default="", help="markdown/text file shown at the top")
    parser.add_argument("--keep-quotes", action="store_true",
                        help="diff quoted reply history too (default: shown collapsed, not diffed)")
    parser.add_argument("--out", default="", help="output path (default .replypen/dream/<ts>-sent-deltas.html)")
    args = parser.parse_args(argv)

    payload = load_evidence(args)
    items = payload.get("deltas") or []
    if not items:
        print("no sent-vs-proposed deltas in this scope/window", file=sys.stderr)
        return 1
    has_shadow = any(is_shadow(item) for item in items)

    # This is intentionally the pre-shadow live path with the same inputs, ordering, renderers, and
    # messages. A pure live payload must remain byte-identical apart from its existing UTC timestamp.
    if not has_shadow:
        aged_out = [i for i in items if i.get("bodies_scrubbed") and not i.get("sent_body")]
        items = [i for i in items if i.get("proposed_body") and i.get("sent_body")]
        hollow = len(payload.get("deltas") or []) - len(items) - len(aged_out)
        if not items:
            if aged_out and not hollow:
                print(f"every delta in this window has aged out ({len(aged_out)} scrubbed at the 14-day "
                      "retention TTL); their descriptions are still in the payload — widen with --limit or "
                      "run the cycle closer to the sends", file=sys.stderr)
            else:
                print("deltas carry no bodies — re-run with --include-bodies", file=sys.stderr)
            return 1

        notes: dict[str, str] = {}
        if args.annotations:
            notes = json.loads(Path(args.annotations).read_text("utf-8"))
            unknown = set(notes) - {str(i.get("id")) for i in items}
            if unknown:
                print(f"warning: annotations for unknown delta ids: {', '.join(sorted(unknown))}",
                      file=sys.stderr)
        conclusion = Path(args.conclusion).read_text("utf-8").strip() if args.conclusion else ""

        diffs = [diff_for(i, args.keep_quotes) for i in items]
        order = sorted(range(len(items)), key=lambda k: diffs[k].similarity)
        items = [items[k] for k in order]
        diffs = [diffs[k] for k in order]

        project, tenant = args.project, args.tenant
        if not project:
            project, tenant_local = local_scope(Path.cwd())
            tenant = tenant or tenant_local
        scope = " / ".join(p for p in (project or "unknown project", tenant) if p)

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
        out = Path(args.out) if args.out else Path(".replypen/dream") / f"{stamp}-sent-deltas.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(items, diffs, notes, scope, conclusion, len(aged_out)), "utf-8")
        md = out.with_suffix(".md")
        md.write_text(render_markdown(items, diffs, notes, scope, conclusion, str(out), aged_out), "utf-8")
        if not {".replypen", ".rootcause"} & set(out.resolve().parts):
            print(f"warning: {out} is outside .replypen/ and holds raw customer mail — do not commit",
                  file=sys.stderr)
        if aged_out:
            print(f"note: {len(aged_out)} delta(s) aged out (bodies scrubbed at 14 days); their "
                  "descriptions are in the markdown report", file=sys.stderr)
        if hollow:
            print(f"note: {hollow} delta(s) skipped for missing bodies — re-fetch with --include-bodies",
                  file=sys.stderr)
        print(f"{md}\n{out}")
        return 0

    shadow_items = sorted((item for item in items if is_shadow(item)), key=shadow_sort_key)
    shadow_diffs = [diff_for(item, args.keep_quotes)
                    if item.get("proposed_body") and item.get("sent_body") else Diff([], 0, 0, 0)
                    for item in shadow_items]
    live_all = [item for item in items if not is_shadow(item)]
    live_aged_out = [item for item in live_all
                     if item.get("bodies_scrubbed") and not item.get("sent_body")]
    live_items = [item for item in live_all if item.get("proposed_body") and item.get("sent_body")]
    live_hollow = [item for item in live_all
                   if not (item.get("proposed_body") and item.get("sent_body"))
                   and not (item.get("bodies_scrubbed") and not item.get("sent_body"))]

    notes = {}
    if args.annotations:
        notes = json.loads(Path(args.annotations).read_text("utf-8"))
        unknown = set(notes) - {str(item.get("id")) for item in items}
        if unknown:
            print(f"warning: annotations for unknown delta ids: {', '.join(sorted(unknown))}",
                  file=sys.stderr)
    conclusion = Path(args.conclusion).read_text("utf-8").strip() if args.conclusion else ""

    live_diffs = [diff_for(item, args.keep_quotes) for item in live_items]
    live_order = sorted(range(len(live_items)), key=lambda k: live_diffs[k].similarity)
    live_items = [live_items[k] for k in live_order]
    live_diffs = [live_diffs[k] for k in live_order]

    project, tenant = args.project, args.tenant
    if not project:
        project, tenant_local = local_scope(Path.cwd())
        tenant = tenant or tenant_local
    scope = " / ".join(p for p in (project or "unknown project", tenant) if p)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    out = Path(args.out) if args.out else Path(".replypen/dream") / f"{stamp}-sent-deltas.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_shadow(shadow_items, shadow_diffs, notes, scope, conclusion,
                                 live_items, live_diffs, len(live_aged_out), live_hollow), "utf-8")
    md = out.with_suffix(".md")
    markdown = render_shadow_markdown(shadow_items, shadow_diffs, notes, scope, conclusion, str(out))
    live_markdown = ""
    if live_items or live_aged_out:
        live_markdown = render_markdown(
            live_items, live_diffs, notes, scope, conclusion, str(out), live_aged_out)
    elif live_hollow:
        live_markdown = f"# Sent vs proposed — {scope}"
    if live_hollow:
        live_markdown += "\n\n" + render_missing_live_markdown(live_hollow)
    if live_markdown:
        markdown += "\n\n---\n\n" + live_markdown
    md.write_text(markdown, "utf-8")
    if not {".replypen", ".rootcause"} & set(out.resolve().parts):
        print(f"warning: {out} is outside .replypen/ and holds raw customer mail — do not commit",
              file=sys.stderr)
    if live_aged_out:
        print(f"note: {len(live_aged_out)} live delta(s) aged out (bodies scrubbed at 14 days); "
              "their descriptions are in the markdown report", file=sys.stderr)
    if live_hollow:
        print(f"note: {len(live_hollow)} live delta(s) skipped for missing bodies — re-fetch with "
              "--include-bodies", file=sys.stderr)
    print(f"{md}\n{out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
