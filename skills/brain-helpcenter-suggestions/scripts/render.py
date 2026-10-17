#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "pyyaml>=6"]
# ///
"""`uv run render.py DIR/suggestions.json` → `report.html` next to it.

Validates first; on any error nothing is written. Chrome is English, quotes and
proposed edits stay in the customer's language. Everything is escaped: the agent
never supplies HTML.

Each card reads WHY → evidence → edit, with the edit as the visual focus, and
carries two views: `rendered` (the owner reads it) and `markdown` (the bot block
an agent applies, see `bot_block`).
"""

from __future__ import annotations

import argparse
import sys
from html import escape
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import WEIGHT, load, rank, summary, validation_errors  # noqa: E402

KIND_COLOR = {
    "new": ("#e8f5ee", "#2f6f4f"), "rewrite": ("#fbf1e3", "#7a4a12"),
    "retitle": ("#eef2fa", "#33518c"), "add_alias": ("#f2ecfb", "#5b3a8a"),
    "merge": ("#e6f4f5", "#1f6b73"), "delete": ("#fdeceb", "#b42318"),
}
VERDICT_LABEL = {"answered": "answered", "partial": "partial", "missing": "missing",
                 "wrong_title": "wrong title", "recipe": "recipe", "not_kb": "not KB",
                 "uncertain": "uncertain"}
SCORE_TIP = ("Score = the gap weights of every conversation on this topic added up "
             "(missing 3, partial 2, recipe 2, wrong title 1, uncertain 1). Higher means "
             "more customers wrote in about it, or wrote in about a bigger hole.")
# One static sentence per tile: what it means and why it matters.
TILES = [
    ("scanned", "scanned", "Every conversation in this window that was read and classified."),
    ("howto", "how-to", "Conversations the help centre could own, so everything except the not-KB ones."),
    ("answered", "answered", "The help centre already answers this. Nothing to do, and it is the number you want to grow."),
    ("partial", "partial", "An article covers the question but leaves a piece open, so the customer still writes in. Usually the cheapest edit."),
    ("missing", "missing", "No article covers this at all. These are the gaps that cost you the most replies."),
    ("wrong_title", "wrong title", "The answer exists but customers cannot find it. The cheapest fix is putting their words in the title or aliases."),
    ("recipe", "recipe", "The customer asked for something they could have done themselves in the product. The help centre should teach that path."),
    ("not_kb", "not KB", "Account specifics, bugs, or noise. No help centre edit follows from these."),
    ("uncertain", "uncertain", "Too little context to judge. Kept visible so a repeating pattern still shows up."),
]
BOT_INSTRUCTION = (
    "Apply this edit to the help centre. Use the MCP or API connection available in your project, "
    "or the rp CLI (`rp project knowledge article apply --from this.md --dry-run`, then without "
    "`--dry-run`; `rp project knowledge article get --provider <p> --id <id>` shows the current "
    "article; recipes in the kit skill `brain-helpcenter-publish`). The body is markdown in the "
    "help-centre canon: '- ' bullets, **bold**, ATX headings, one line per paragraph, no tables, "
    "no strikethrough, no raw HTML. Anchors are verbatim lines of the current article."
)
# Providers rc cannot write: say so up front, and name the path that does work.
BOT_INSTRUCTION_BY_PROVIDER = {
    "knowledgeowl": (
        "Apply this edit to the help centre. rc cannot write KnowledgeOwl (`UNSUPPORTED_PROVIDER`): use "
        "the KnowledgeOwl API/MCP connection available in your project. In the KampAdmin repo that is the "
        "`knowledge-owl` skill: `get <id> --format html --output /tmp/a.html`, splice the edit at the "
        "anchor in that HTML, `update <id> --html-file /tmp/a.html --version-note '...'` (dry run), then "
        "re-run with `--apply` and the printed guards. `id` below is KnowledgeOwl's article id; `url` "
        "carries the slug. The body is markdown in the help-centre canon: '- ' bullets, **bold**, ATX "
        "headings, one line per paragraph, no tables, no strikethrough, no raw HTML. Anchors are verbatim "
        "lines of the current article."
    ),
}
OP_BY_KIND = {"new": "create", "rewrite": "update", "retitle": "update",
              "add_alias": "update", "merge": "manual", "delete": "manual"}
MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#101828;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 20px 80px}
a{color:#33518c}h1{margin:0;font-size:26px}h3{margin:2px 0 6px;font-size:18px}p{margin:0}section{margin-top:40px}
h2,.tile span,th,.editwrap b,.badge,.tabs button{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085;font-weight:700}
h2{margin:0 0 14px}.stamp,.score,.targets,.footer{color:#667085;font-size:13px}.footer{margin-top:40px}
.card,.tile,.lede,table{background:#fff;border:1px solid #e4e7ec;border-radius:12px}
.lede{margin-top:20px;border-left:4px solid #33518c;padding:18px 20px;font-size:18px}
.tiles,.top{display:flex;flex-wrap:wrap;gap:10px}.top{align-items:center;gap:8px}
.tile{padding:12px 16px;min-width:104px;cursor:pointer}.tile b{display:block;font-size:24px}
.tile>summary{list-style:none;cursor:pointer}.tile>summary::-webkit-details-marker{display:none}
.tile p{font-size:12.5px;line-height:1.45;color:#344054;text-transform:none;letter-spacing:0;
 font-weight:400;margin-top:8px;max-width:240px}
details.tile[open]{background:#fff;box-shadow:0 1px 3px rgba(16,24,40,.08)}
.card{padding:18px 20px;margin-bottom:12px}
.rank{flex:0 0 26px;height:26px;border-radius:50%;background:#33518c;color:#fff;display:flex;
 align-items:center;justify-content:center;font-size:12px;font-weight:700}
.badge{border-radius:999px;padding:3px 9px;letter-spacing:.06em}.route{background:#fdf3e7;color:#b54708}
.warn{background:#fdeceb;color:#b42318}
.why{font-size:15.5px;color:#101828;margin:10px 0 2px}
.quote{border-left:2px solid #e4e7ec;padding-left:12px;margin:8px 0;color:#344054}
.quote a,summary{font-size:12px;font-weight:700;text-decoration:none;color:#33518c}
.quote a{margin-left:6px;white-space:nowrap}.quote .who{color:#667085;font-weight:700;font-size:11px}
.editwrap{border:1px solid #d3d9e2;border-radius:10px;margin:14px 0 4px;background:#fbfcfd}
.editwrap>.head{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid #e4e7ec}
.editwrap>.head b{flex:1}
.tabs{display:inline-flex;border:1px solid #d3d9e2;border-radius:6px;overflow:hidden}
.tabs button{font-family:inherit;border:0;background:#fff;color:#667085;padding:3px 9px;cursor:pointer}
.tabs button.on{background:#33518c;color:#fff}
.view{padding:12px 14px}.view[hidden]{display:none}
.blk{margin:0 0 10px}.blk:last-child{margin-bottom:0}
.blk>b{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#667085;margin-bottom:4px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13.5px/1.5 ui-monospace,Menlo,monospace}
pre.old,pre.ctx{background:#f2f4f7;color:#98a2b3;border-radius:8px;padding:10px 12px}
pre.old{text-decoration:line-through}
.ctx .anchorline{color:#475467;font-weight:700}
pre.bot{background:#f6f7f9;border-radius:8px;padding:12px 14px;color:#344054}
.newtext{background:#fff;border:1px solid #b7d5c3;border-left:4px solid #2f6f4f;border-radius:8px;padding:12px 14px}
.newtext .md>*:first-child{margin-top:0}.newtext .md>*:last-child{margin-bottom:0}
.md{font-size:14.5px}.md h1,.md h2,.md h3{font-size:15px;margin:12px 0 4px;text-transform:none;letter-spacing:0;color:#101828}
.md ul,.md ol{margin:6px 0;padding-left:22px}.md p{margin:6px 0}.md code{background:#f2f4f7;padding:1px 4px;border-radius:4px}
.chip{display:inline-block;background:#f2ecfb;color:#5b3a8a;border-radius:999px;padding:3px 10px;margin:0 6px 6px 0;font-size:13px}
.strike{text-decoration:line-through;color:#98a2b3}
.newtext .title{font-weight:700;font-size:15.5px}
.arrow{color:#98a2b3;margin:2px 0;font-size:12px;text-align:center}
.offline{color:#b54708;font-size:12px;margin:0 0 6px}
details.more{margin-top:12px;border-top:1px solid #e4e7ec;padding-top:10px}
details.more summary{cursor:pointer;font-size:13px}
details.more .note{color:#667085;font-size:12.5px;margin:4px 0 8px;font-weight:400}
table{width:100%;border-collapse:collapse;font-size:13px;border-radius:0}
th,td{text-align:left;padding:7px 8px;border-top:1px solid #e4e7ec;vertical-align:top}th{border-top:none}
button.copy{font:inherit;font-size:11px;font-weight:700;border:1px solid #d3d9e2;background:#fff;color:#33518c;
 border-radius:6px;padding:3px 9px;cursor:pointer}
"""

JS = """
var mdOk = !!(window.marked && window.marked.parse);
document.querySelectorAll('template.mdsrc').forEach(function(t){
  var host = document.getElementById(t.dataset.into);
  if (!host) return;
  var src = t.content.textContent;
  if (mdOk) { host.innerHTML = window.marked.parse(src); return; }
  var note = document.createElement('p'); note.className = 'offline';
  note.textContent = 'offline: showing markdown';
  var pre = document.createElement('pre'); pre.textContent = src;
  host.appendChild(note); host.appendChild(pre);
});
document.querySelectorAll('pre.bot').forEach(function(pre){
  var t = document.getElementById(pre.dataset.src);
  if (t) pre.textContent = t.content.textContent;
});
document.querySelectorAll('.tabs button').forEach(function(b){ b.onclick = function(){
  var card = b.closest('.editwrap'), view = b.dataset.view;
  card.querySelectorAll('.view').forEach(function(p){ p.hidden = (p.dataset.view !== view); });
  card.querySelectorAll('.tabs button').forEach(function(t){ t.classList.toggle('on', t === b); });
};});
function srcOf(el){ return el.content ? el.content.textContent : el.textContent; }
document.querySelectorAll('button.copy').forEach(function(b){ b.onclick = function(){
  var target = document.getElementById(b.dataset.target);
  var plain = srcOf(document.getElementById(b.dataset.md));
  var html = b.dataset.html === '1' ? target.innerHTML : plain;
  var done = function(){ b.textContent = 'copied \\u2713';
    setTimeout(function(){ b.textContent = 'copy'; }, 1500); };
  var plainOnly = function(){ navigator.clipboard.writeText(plain).then(done, function(){}); };
  if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
    navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([html], {type: 'text/html'}),
      'text/plain': new Blob([plain], {type: 'text/plain'})})]).then(done, plainOnly);
  } else { plainOnly(); }
};});
"""


def _tiles(ev, sug) -> dict:
    """Tile numbers. `summary()` gains `recipe` and `per_tenant` in this same round; stand in for
    both while it does not, so an older schema.py still renders."""
    counts: dict[str, int] = {}
    for c in sug.classification:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    try:
        tiles = dict(summary(ev, sug))
    except KeyError:  # summary() predates the `recipe` verdict
        topics: dict[str, set] = {}
        for c in sug.classification:
            if c.verdict in WEIGHT:
                for t in c.topics:
                    topics.setdefault(t, set()).add(c.conversation_id)
        tiles = {
            "scanned": len(ev.conversations),
            "howto": sum(n for v, n in counts.items() if v != "not_kb"),
            "top_topics": sorted(((t, len(ids)) for t, ids in topics.items()), key=lambda kv: (-kv[1], kv[0]))[:8],
        }
    for verdict in VERDICT_LABEL:
        tiles.setdefault(verdict, counts.get(verdict, 0))
    if "per_tenant" not in tiles:
        seen: dict[str, list[int]] = {}
        by_id = {c.id: c for c in ev.conversations}
        for c in sug.classification:
            tenant = getattr(by_id.get(c.conversation_id), "tenant", None)
            if tenant:
                row = seen.setdefault(tenant, [0, 0])
                row[0] += 1
                row[1] += 1 if c.verdict in WEIGHT else 0
        tiles["per_tenant"] = [(t, n, g) for t, (n, g) in sorted(seen.items())]
    return tiles


def e(text: str | None) -> str:
    return escape(text or "", quote=True)


def _first_name(who: str | None) -> str:
    return (who or "").strip().split(" ")[0] or "the agent"


def _worst(classifications) -> str:
    gaps = [c.verdict for c in classifications if c.verdict in WEIGHT]
    return VERDICT_LABEL[max(gaps, key=lambda v: WEIGHT[v])] if gaps else "no gap"


def _coverage(ev) -> str:
    """One line per feed, reason included: 'complete (63/121)' alone reads contradictory."""
    parts = []
    for f in ev.coverage:
        head = f"{f.feed.replace('_', ' ')}: {f.status}"
        if f.scanned or f.retained:
            head += f", {f.retained} of {f.scanned} kept"
        if f.reason:
            head += f", {f.reason}"
        parts.append(head)
    return " · ".join(parts) or "no coverage recorded"


# --------------------------------------------------------------- article body

def _body_lines(articles_dir: Path | None, aid: str | None) -> list[str]:
    """Lines of `raw/articles/<aid>.md` after the frontmatter, or [] when unavailable."""
    if not articles_dir or not aid:
        return []
    path = Path(articles_dir) / f"{aid}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[text.find("\n", end + 1) + 1:]
    return [line.rstrip() for line in text.split("\n")]


def _anchor_context(lines: list[str], anchor: str) -> tuple[list[str], list[str]]:
    """Up to two non-empty lines before and after the anchor line, for orientation."""
    target = anchor.strip()
    idx = next((i for i, line in enumerate(lines) if line.strip() == target), None)
    if idx is None:
        return [], []
    before = [line for line in lines[max(0, idx - 4):idx] if line.strip()][-2:]
    after = [line for line in lines[idx + 1:idx + 5] if line.strip()][:2]
    return before, after


# ------------------------------------------------------------------ bot block

class _Dumper(yaml.SafeDumper):
    """safe_dump, except multi-line strings become `|` blocks so the bot reads them verbatim."""


_Dumper.add_representer(
    str,
    lambda d, data: d.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None),
)


def bot_block(s, article, kb, destination=None) -> str:
    """The markdown tab: instruction paragraph + YAML front matter + body, for an agent.

    `article` is the article the op acts on (the one created into, updated, merged away, deleted);
    `destination` is the surviving article of a merge/delete, named in `why` because those two ops
    stay manual.
    """
    op = OP_BY_KIND[s.kind]
    data: dict[str, object] = {"replypen": "helpcenter/v1", "op": op}
    provider = (article.provider if article else None) or kb.provider
    if provider:
        data["provider"] = provider
    if kb.base_url:
        data["base_url"] = kb.base_url
    if article:
        if article.provider_id:
            data["id"] = article.provider_id
        if article.number:
            data["number"] = article.number
        if article.url:
            data["url"] = article.url
    if op == "create" or s.kind == "retitle" or op == "manual":
        data["title"] = s.title
    if op == "create":
        if article and article.collection_id:
            data["collection_id"] = article.collection_id
        if article and article.parent_type:
            data["parent_type"] = article.parent_type
    if article and article.locale:
        data["locale"] = article.locale
    if article and article.audience:
        data["audience"] = article.audience
    elif op == "create":
        data["audience"] = "customer"
    if op == "create":
        data["status"] = "draft"
    if s.aliases and s.kind in ("add_alias", "new"):
        data["aliases"] = list(s.aliases)
    why = s.why
    if s.kind in ("merge", "delete") and destination:
        verb = "Merge into" if s.kind == "merge" else "Delete, redirect to"
        handle = destination.provider_id or destination.id
        why = f"{why} {verb}: {destination.title} ({handle})."
    data["why"] = why
    if op == "update" and s.edit:
        for key in ("old", "after", "before"):
            value = getattr(s.edit, key, None)
            if value is not None:
                # a replaced block keeps its trailing newline so it dumps as a `|` literal;
                # an anchor line stays a one-liner
                data["anchor"] = {key: value + "\n" if key == "old" and not value.endswith("\n") else value}
                break

    front = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000).rstrip("\n")
    if op == "create" and "collection_id" not in data:
        front = front.replace("\nstatus:", "\n# collection_id: <pick one, the collection this article belongs to>\nstatus:", 1)
    body = "" if s.kind in ("retitle", "add_alias") else (s.text or "")
    instruction = BOT_INSTRUCTION_BY_PROVIDER.get(str(data.get("provider") or ""), BOT_INSTRUCTION)
    return f"{instruction}\n\n---\n{front}\n---\n{body.rstrip()}\n"


# ------------------------------------------------------------------ edit views

def _md(host_id: str, text: str) -> str:
    """A markdown target plus its never-executed source template."""
    return (f'<div class="md" id="{host_id}"></div>'
            f'<template class="mdsrc" data-into="{host_id}">{e(text)}</template>')


def _edit_view(s, arts, articles_dir) -> str:
    """The rendered edit, by kind. The owner has to see exactly what changes and where."""
    target = arts.get(s.target_articles[0]) if s.target_articles else None
    dest = arts.get(s.destination) if s.destination else None
    out: list[str] = []
    if s.kind == "rewrite" and s.edit and s.edit.old:
        out.append(f'<div class="blk"><b>Current text{_where(target)}</b><pre class="old">{e(s.edit.old.rstrip())}</pre></div>')
        out.append('<p class="arrow">becomes</p>')
        out.append(f'<div class="blk"><b>Replacement</b><div class="newtext">{_md(f"ed-body-{s.id}", s.text or "")}</div></div>')
    elif s.kind == "rewrite" and s.edit and (s.edit.after or s.edit.before):
        anchor = s.edit.after or s.edit.before or ""
        lines = _body_lines(articles_dir, target.id if target else None)
        ctx_before, ctx_after = _anchor_context(lines, anchor)
        where = "after" if s.edit.after else "before"
        marked = f'<span class="anchorline">{e(anchor)}</span>'
        head = [e(l) for l in ctx_before] + ([marked] if where == "after" else [])
        tail = ([marked] if where == "before" else []) + [e(l) for l in ctx_after]
        out.append(f'<div class="blk"><b>Insert {where} the bold line{_where(target)}</b>'
                   + (f'<pre class="ctx">{chr(10).join(head)}</pre>' if head else ""))
        out.append(f'<div class="newtext">{_md(f"ed-body-{s.id}", s.text or "")}</div>')
        out.append((f'<pre class="ctx">{chr(10).join(tail)}</pre>' if tail else "") + "</div>")
    elif s.kind in ("new", "merge"):
        label = "New article" if s.kind == "new" else "Merged article text"
        out.append(f'<div class="blk"><b>{label}</b><div class="newtext">{_md(f"ed-body-{s.id}", s.text or "")}</div></div>')
        if s.kind == "merge" and dest:
            out.append(f'<div class="blk"><b>Keep as the surviving article</b>'
                       f'<p>{e(dest.title)} ({e(dest.id)})</p></div>')
    elif s.kind == "retitle":
        old = target.title if target else ""
        out.append(f'<div class="blk"><b>Title today</b><p class="strike">{e(old)}</p>'
                   f'<p class="arrow">becomes</p><div class="newtext">'
                   f'<p class="title">{e(s.title)}</p></div></div>')
    elif s.kind == "add_alias":
        chips = "".join(f'<span class="chip">{e(a)}</span>' for a in s.aliases)
        out.append(f'<div class="blk"><b>Add these search words{_where(target)}</b>{chips or "<p>none given</p>"}</div>')
    elif s.kind == "delete":
        out.append('<div class="blk"><b>Delete this article</b>'
                   f'<p class="strike">{e(target.title if target else "")}</p>'
                   + (f'<p>Point readers to {e(dest.title)} ({e(dest.id)}).</p>' if dest else "") + "</div>")
    if s.text and s.kind in ("retitle", "add_alias", "delete"):
        out.append(f'<div class="blk"><b>Note</b><div class="md-plain">{_md(f"ed-body-{s.id}", s.text)}</div></div>')
    return "".join(out) or "<p>No edit body given.</p>"


def _where(article) -> str:
    return f" in {e(article.title)}" if article else ""


def _kb_link(article) -> str:
    if not article:
        return ""
    return (f'<a href="{e(article.url)}">{e(article.title)}</a>' if article.url else e(article.title))


# ----------------------------------------------------------------------- card

def _card(pos: int, s, sc: int, arts, by_topic, convs, ev, articles_dir, multi_tenant: bool) -> str:
    bg, fg = KIND_COLOR[s.kind]
    topic_convs = by_topic.get(s.topic, [])
    links = " · ".join(_kb_link(arts[a]) for a in s.target_articles if a in arts)
    badges = f'<span class="badge" style="background:{bg};color:{fg}">{e(s.kind.replace("_", " "))}</span>'
    if "contradiction" in s.flags:
        badges += '<span class="badge warn">contradiction</span>'
    if s.route == "brain":
        badges += '<span class="badge route">brain</span>'
    parts = [
        f'<div class="card"><div class="top"><span class="rank">{pos}</span>{badges}</div>',
        f"<h3>{e(s.title)}</h3>",
        f'<p class="score" title="{e(SCORE_TIP)}">{len(topic_convs)} conversation'
        f'{"s" if len(topic_convs) != 1 else ""} · {e(_worst(topic_convs))} · score {sc} · topic {e(s.topic)}</p>',
        f'<p class="targets">Article: {links}</p>' if links else "",
        f'<p class="why">{e(s.why)}</p>',
    ]
    for q in s.evidence:
        conv = convs.get(q.conversation_id)
        link = f'<a href="{e(conv.url)}">conversation ↗</a>' if conv and conv.url else ""
        who = (f'<span class="who">{e(conv.tenant)} </span>'
               if multi_tenant and conv and conv.tenant else "")
        parts.append(f'<div class="quote">{who}“{e(q.quote)}”{link}</div>')

    target = arts.get(s.target_articles[0]) if s.target_articles else None
    dest = arts.get(s.destination) if s.destination else None
    # `new` has no target of its own; a sibling article, when the agent named one, carries the collection
    bot = bot_block(s, (dest or target) if s.kind == "new" else target, ev.kb, destination=dest)
    parts.append(
        '<div class="editwrap"><div class="head"><b>Proposed edit</b>'
        f'<span class="tabs"><button class="tab on" data-view="rendered">rendered</button>'
        f'<button class="tab" data-view="md">markdown</button></span></div>'
        f'<div class="view" data-view="rendered"><div id="ed-{s.id}">'
        f'{_edit_view(s, arts, articles_dir)}</div>'
        f'<p style="margin-top:10px"><button class="copy" data-target="ed-{s.id}" '
        f'data-md="md-{s.id}" data-html="1">copy</button></p></div>'
        f'<div class="view" data-view="md" hidden><pre class="bot" data-src="md-{s.id}"></pre>'
        f'<p style="margin-top:10px"><button class="copy" data-target="md-{s.id}" '
        f'data-md="md-{s.id}">copy</button></p></div>'
        f'<template id="md-{s.id}">{e(bot)}</template></div>'
    )
    if s.seed_reply and s.seed_reply in convs:
        reply = convs[s.seed_reply].reply
        parts.append(
            f'<details class="more"><summary>How {e(_first_name(reply.by if reply else None))} '
            "answered this in the ticket</summary>"
            '<p class="note">The human\'s actual reply, reused as starting text.</p>'
            f'<pre class="ctx">{e(reply.text if reply else "")}</pre></details>'
        )
    return "".join(parts) + "</div>"


# -------------------------------------------------------------------- sections

def _tiles_section(tiles) -> str:
    cells = []
    for key, label, explain in TILES:
        cells.append(
            f'<details class="tile"><summary><b>{tiles.get(key, 0)}</b><span>{e(label)}</span></summary>'
            f"<p>{e(explain)}</p></details>"
        )
    return ('<section><h2>This window</h2><p class="stamp" style="margin-bottom:10px">'
            "Click a tile for what it means.</p>"
            f'<div class="tiles">{"".join(cells)}</div></section>')


def _per_tenant_section(rows) -> str:
    body = "".join(f"<tr><td>{e(t)}</td><td>{n}</td><td>{g}</td></tr>" for t, n, g in rows)
    return ("<section><h2>Per tenant</h2><table><tr><th>tenant</th><th>conversations</th>"
            f"<th>gaps</th></tr>{body}</table></section>")


def _classification_table(sug, convs_by_id, arts, multi_tenant: bool) -> str:
    rows = []
    for c in sorted(sug.classification, key=lambda c: convs_by_id[c.conversation_id].created_at):
        conv = convs_by_id[c.conversation_id]
        cid = f'<a href="{e(conv.url)}">{e(conv.id)}</a>' if conv.url else e(conv.id)
        tenant = f"<td>{e(conv.tenant)}</td>" if multi_tenant else ""
        rows.append(
            f"<tr><td>{cid}</td><td>{e(conv.created_at[:16])}</td>{tenant}<td>{e(conv.channel)}</td>"
            f"<td>{e(VERDICT_LABEL[c.verdict])}</td><td>{e(', '.join(c.topics))}</td>"
            f"<td>{e(', '.join(arts[a].title for a in c.article_ids if a in arts))}</td>"
            f"<td>{e(conv.noise.reason if conv.noise else '')}</td></tr>"
        )
    head = ("<tr><th>conversation</th><th>date</th>"
            + ("<th>tenant</th>" if multi_tenant else "")
            + "<th>channel</th><th>verdict</th><th>topics</th><th>articles</th><th>noise hint</th></tr>")
    return (
        f'<section><h2>Detail</h2><details class="more"><summary>Classification of all '
        f"{len(sug.classification)} conversations</summary><table>{head}"
        f"{''.join(rows)}</table></details></section>"
    )


def render_html(sug, ev, articles_dir: Path | None = None) -> str:
    convs_by_id = {c.id: c for c in ev.conversations}
    arts = {a.id: a for a in ev.articles}
    by_topic: dict[str, list] = {}
    for c in sug.classification:
        for t in c.topics:  # a conversation counts once per topic it lists
            by_topic.setdefault(t, []).append(c)
    tiles, cov = _tiles(ev, sug), _coverage(ev)
    per_tenant = tiles.get("per_tenant") or []
    multi_tenant = len({c.tenant for c in ev.conversations if c.tenant}) > 1
    scope = f"{ev.project} · {ev.tenant}" if ev.tenant else ev.project

    dates = sorted(c.created_at[:10] for c in ev.conversations if c.created_at)
    # The window is what was asked; the conversations are what the sources still held.
    span = f"conversations from {e(dates[0])} to {e(dates[-1])}" if dates else "no conversations"
    body = [
        f"<h1>{e(scope)}: help centre suggestions</h1>",
        f'<p class="stamp">{ev.window.days}-day window ending {e(ev.window.end[:10])}, {span} · '
        f"source {e(ev.source)} · KB {e(ev.kb.status)} ({ev.kb.articles} articles, {e(ev.kb.scope)} scope"
        f"{', ' + e(ev.kb.root) if ev.kb.root else ''})<br>{e(cov)}</p>",
        f'<div class="lede">{e(sug.headline)}</div>' if sug.headline else "",
        _tiles_section(tiles),
    ]
    if len(per_tenant) > 1:
        body.append(_per_tenant_section(per_tenant))
    if tiles["top_topics"]:
        rows = "".join(f"<tr><td>{e(t)}</td><td>{n}</td></tr>" for t, n in tiles["top_topics"])
        body.append("<section><h2>Top unanswered topics</h2><table><tr><th>topic</th>"
                    f"<th>conversations</th></tr>{rows}</table></section>")
    cards = "".join(
        _card(pos, s, sc, arts, by_topic, convs_by_id, ev, articles_dir, multi_tenant)
        for pos, s, sc in rank(sug.suggestions, sug.classification)
    )
    body.append(f"<section><h2>Suggested edits ({len(sug.suggestions)})</h2>{cards or '<p>No suggestions.</p>'}</section>")
    body.append(_classification_table(sug, convs_by_id, arts, multi_tenant))
    # learnings.md is for the skill's maintainers, not the owner: it stays out of the report.
    body.append(
        '<div class="footer">'
        + f"<p>Coverage: {e(cov)}. KB: {e(ev.kb.status)}, {ev.kb.articles} articles, "
        f"{ev.kb.brain_docs} brain documents. Collected {e(ev.collected_at)} from {e(ev.source)}.</p></div>"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(scope)}: help centre suggestions</title><style>{CSS}</style>"
        f'<script src="{MARKED_CDN}" onerror="window.markedFailed=true"></script></head>'
        f'<body><div class="wrap">{"".join(body)}</div><script>{JS}</script></body></html>'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Render suggestions.json into report.html")
    parser.add_argument("suggestions", type=Path)
    parser.add_argument("--evidence", type=Path, help="default: evidence.json next to suggestions.json")
    parser.add_argument("--articles", type=Path, help="default: raw/articles next to suggestions.json")
    args = parser.parse_args()
    out_dir = args.suggestions.parent

    def nearby(name: str) -> Path:  # the fixture keeps the agent files one level below evidence.json
        here = out_dir / name
        return here if here.exists() else out_dir.parent / name

    evidence = args.evidence or nearby("evidence.json")
    articles = args.articles or nearby("raw/articles")

    errors = validation_errors(args.suggestions, evidence)
    if errors:
        print(f"{len(errors)} error(s) in {args.suggestions}, nothing rendered:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    sug, ev = load(args.suggestions, evidence)
    path = out_dir / "report.html"
    path.write_text(render_html(sug, ev, articles), encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
