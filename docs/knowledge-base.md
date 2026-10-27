# Knowledge Base Traversal

Use this when a brain has an external knowledge source mounted at `/kb`, or when a brain commits a
`knowledge/` directory. It is for read-only discovery from `rp dev console bash`; durable routing belongs in the
brain's own skills/playbooks after you learn which articles matter.

A third-party public help center you don't sync to `/kb` can be committed as a snapshot under
`/brain/knowledge/` and refreshed monthly with [`brain-kb-mirror`](../skills/brain-kb-mirror/SKILL.md).

## Mounts And Inventory

Hosted runs may have three relevant read-only trees:

| Path | Meaning |
|---|---|
| `/brain` | Committed brain ref. |
| `/kb` | Synced external knowledge-base snapshot, if configured (periodic; force with `rp project knowledge sync run --wait`). |
| `/brain/knowledge` | Committed project knowledge directory, if that brain uses one. |

Probe before assuming a shape:

```bash
rp dev console bash run 'find /kb -maxdepth 3 -type d -print | sed -n "1,120p"'
rp dev console bash run 'find /kb -type f | sed -n "1,80p"'
rp dev console bash run 'test -d /brain/knowledge && find /brain/knowledge -type f | sed -n "1,80p" || true'
```

## Body Search

Use `rg` for broad discovery, then `sed` or `python` for one article:

```bash
rp dev console bash run 'rg -n -i "refund|invoice|payment|receipt" /kb /brain/knowledge -g "*.md" 2>/dev/null | sed -n "1,60p"'
rp dev console bash run 'sed -n "1,180p" /kb/provider/collection/article.md'
```

Search customer words first, then product vocabulary from the brain's terminology/source docs. Keep
results capped; KB snapshots can be large.

## Frontmatter Index

Many KB exports write YAML frontmatter above the article body. Inventory the real keys before filtering:

```bash
rp dev console bash run 'python - <<'"'"'PY'"'"'
from collections import Counter
from pathlib import Path

keys = Counter()
for root in (Path("/kb"), Path("/brain/knowledge")):
    if not root.exists():
        continue
    for p in root.rglob("*.md"):
        text = p.read_text(errors="replace")
        if not text.startswith("---\n"):
            continue
        frontmatter = text.split("---", 2)[1]
        for line in frontmatter.splitlines():
            if ":" in line and not line.startswith((" ", "-")):
                keys[line.split(":", 1)[0]] += 1

for key, count in keys.most_common():
    print(f"{count:4} {key}")
PY'
```

Build a title index:

```bash
rp dev console bash run 'python - <<'"'"'PY'"'"'
from pathlib import Path

for root in (Path("/kb"), Path("/brain/knowledge")):
    if not root.exists():
        continue
    for p in sorted(root.rglob("*.md")):
        text = p.read_text(errors="replace")
        title = ""
        if text.startswith("---\n"):
            frontmatter = text.split("---", 2)[1]
            for line in frontmatter.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("\"'")
                    break
        if not title:
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        if title:
            print(f"{p}\t{title}")
PY'
```

Filter by common attributes such as `collection`, `status`, `keywords`, `summary`, `url`, or by path:

```bash
rp dev console bash run 'python - <<'"'"'PY'"'"'
from pathlib import Path

terms = ("billing", "payment", "refund")
for root in (Path("/kb"), Path("/brain/knowledge")):
    if not root.exists():
        continue
    for p in sorted(root.rglob("*.md")):
        text = p.read_text(errors="replace")
        frontmatter = text.split("---", 2)[1] if text.startswith("---\n") else ""
        haystack = f"{p}\n{frontmatter}".lower()
        if any(term in haystack for term in terms):
            title = next(
                (line.split(":", 1)[1].strip().strip("\"'")
                 for line in frontmatter.splitlines()
                 if line.startswith("title:")),
                "",
            )
            print(f"{p}\t{title}")
PY'
```

## Bulk Copy To The Laptop

For offline analysis of many articles, download the tree instead of paging through `bash run` output:

```bash
rp dev console file get /kb/<provider> ./kb-<provider>
```

## Choosing Evidence

Use KB articles for stable customer-facing guidance: setup steps, policy explanations, documented
limits, and public links. Use brain scripts and `rp dev console database` for tenant-specific live state. Use source
mirrors for implementation truth. When an article answers the question, cite its frontmatter `url` if
present; when it conflicts with live data or source, report both and prefer live/source evidence.

Screenshot alt texts that mention arrows or red boxes mark where users needed pointing: the starting
set for in-app "where to click" guidance ([`brain-ui-targets`](../skills/brain-ui-targets/SKILL.md)).

## Quoting Notes

`rp dev console bash run` takes one command string. For complex Python snippets, use a quoted heredoc as shown
(stdin reaches the remote interpreter; re-verified 2026-10-27). If
the local shell says `unmatched "` or similar, the command did not reach ReplyPen; reduce quoting or
turn repeated logic into a committed brain script.
