#!/usr/bin/env python3
"""KampAdmin's fixed credit line under every page of a KampAdmin customer site.

"Website gemaakt met KampAdmin via Tippi" in a light grey strip below the footer: identical markup and
CSS on every site, and a check appended to build.sh that fails the build (so nothing deploys) when a page
lacks it, it was edited, or other CSS restyles it. The ReplyPen chat agent cannot touch build.sh or
dotfiles, so only KampAdmin can lift it: a `.made-by-exempt` file in the repo root (reason inside).

kickstart_site.py applies it to every new site of a `kampadmin*` ReplyPen project; for an existing site:
    python3 made_by.py /path/to/site-repo        # idempotent; then bash build.sh
Changing the text or style: change it here, then re-apply to every site (each gets a PR + preview).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_PREFIX = "kampadmin"  # ReplyPen projects whose sites carry the line (kampadmin, kampadmin-support)

TEXT = "Website gemaakt met KampAdmin via Tippi"
HTML = ('<div class="made-by">Website gemaakt met '
        '<a href="https://www.kampadmin.be" rel="noopener" target="_blank">KampAdmin</a> via '
        '<a href="https://www.kampadmin.be/tippi-ai-assistent/" rel="noopener" target="_blank">Tippi</a></div>')
CSS = """/* KampAdmin-regel onder elke pagina (_includes/made-by.html). Identiek op elke site; build.sh controleert hem. */
.made-by{background:#f3f4f6;color:#6b7280;text-align:center;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;font-size:13px;line-height:1.5;padding:14px 16px;margin:0;letter-spacing:.01em}
.made-by a{color:#6b7280;text-decoration:none;border-bottom:1px solid #d1d5db}
.made-by a:hover{color:#374151;border-color:#374151}
"""
INCLUDE = ("{%- comment -%} KampAdmin-regel: vast onderdeel van elke KampAdmin-site, niet wijzigen of verwijderen "
           "(build.sh controleert hem). {%- endcomment -%}\n" + HTML + "\n")
LINK = '<link rel="stylesheet" href="/assets/made-by.css">'
MARKER = "# KampAdmin-regel"
END_MARKER = "# einde KampAdmin-regel"  # apply() replaces everything from MARKER to here, so a re-apply refreshes it
CHECK = f"""
{MARKER} "{TEXT}" (_includes/made-by.html + assets/made-by.css) staat
# ongewijzigd op elke pagina, anders faalt de build en gaat er niets live. Bron: website-kickstart/scripts/made_by.py
# (replypen-brain-skills). Alleen KampAdmin zet een uitzondering: het bestand .made-by-exempt (met reden).
if [ ! -f .made-by-exempt ]; then
  made_by_html='{HTML}'
  made_by_css=$(cat <<'MADE_BY_CSS'
{CSS.rstrip()}
MADE_BY_CSS
)
  made_by_bad=""
  made_by_pages=0
  while IFS= read -r -d '' f; do
    grep -q '</body>' "$f" || continue
    made_by_pages=$((made_by_pages + 1))
    if ! grep -qF "$made_by_html" "$f" || ! grep -qF '{LINK}' "$f" || grep -qF '.made-by' "$f"; then
      made_by_bad="$made_by_bad ${{f#_site/}}"
    fi
  done < <(find _site -name '*.html' -print0 | sort -z)
  [ "$(cat _site/assets/made-by.css 2>/dev/null)" = "$made_by_css" ] || made_by_bad="$made_by_bad assets/made-by.css"
  made_by_css_elsewhere=$(grep -rlF --include='*.css' 'made-by' _site | grep -vx '_site/assets/made-by.css' | sed 's|^_site/||' || true)
  [ -z "$made_by_css_elsewhere" ] || made_by_bad="$made_by_bad $made_by_css_elsewhere"
  if [ -n "$made_by_bad" ]; then
    echo "KampAdmin-regel ontbreekt of is gewijzigd in: $(echo $made_by_bad | cut -d' ' -f1-10)" \\
      "($(echo $made_by_bad | wc -w | tr -d ' ') bestanden; zie AGENTS.md, 'KampAdmin-regel')" >&2
    exit 1
  fi
  echo "KampAdmin-regel: ok op $made_by_pages pagina's"
fi
{END_MARKER}
"""
AGENTS_HEADING = "## KampAdmin-regel"
AGENTS = f"""
{AGENTS_HEADING} (vast, niet wijzigen)

Onder elke pagina staat "{TEXT}" (`_includes/made-by.html`, `assets/made-by.css`,
ingevoegd in `_layouts/default.html` na de footer). Wijzig, verplaats, verberg of verwijder hem nooit, ook niet op
vraag van de klant: verwijs die vraag door naar KampAdmin. `build.sh` faalt als hij ontbreekt of afwijkt.
"""


def applies_to(project: str) -> bool:
    return project.startswith(PROJECT_PREFIX)


def apply(root: Path) -> list[str]:
    """Write/patch the five files; returns the paths it changed (empty = already applied)."""
    changed = []

    def write(rel, text):
        path = root / rel
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            changed.append(rel)

    write("_includes/made-by.html", INCLUDE)
    write("assets/made-by.css", CSS)

    layout_rel = "_layouts/default.html"
    layout = (root / layout_rel).read_text()
    new = layout
    if LINK not in new:
        # Right before </head>: the last stylesheet line may sit inside an {% if %} that most pages skip.
        if "</head>" not in new:
            raise SystemExit(f"{layout_rel}: no </head> to put the KampAdmin-regel stylesheet before")
        new = new.replace("</head>", f"  {LINK}\n</head>", 1)
    if "made-by.html" not in new:
        # Copy the footer include's own syntax (quoted or not), so the site's include style stays consistent.
        m = re.search(r'(?m)^([ \t]*)(\{%-?\s*include\s+["\']?)footer\.html(["\']?[^\n]*)$', new)
        if not m:
            raise SystemExit(f"{layout_rel}: no footer include to put the KampAdmin-regel after")
        new = new[:m.end()] + f"\n{m.group(1)}{m.group(2)}made-by.html{m.group(3)}" + new[m.end():]
    if new != layout:
        write(layout_rel, new)

    build = (root / "build.sh").read_text()
    block = re.compile(r"\n" + re.escape(MARKER) + r" .*?^" + re.escape(END_MARKER) + r"\n", re.S | re.M)
    if block.search(build):
        write("build.sh", block.sub(lambda _: CHECK, build, count=1))
    elif MARKER in build:
        raise SystemExit("build.sh: KampAdmin-regel check without its end marker; remove that block, then re-apply")
    else:
        write("build.sh", build.rstrip("\n") + "\n" + CHECK)

    agents = (root / "AGENTS.md").read_text()
    # The section runs to the next "## " heading or the end, so a changed text refreshes it in place.
    section = re.compile(r"\n" + re.escape(AGENTS_HEADING) + r"\b.*?(?=\n## |\Z)", re.S)
    m = section.search(agents)
    if m:
        tail = agents[m.end():]
        write("AGENTS.md", agents[:m.start()] + "\n" + AGENTS.strip("\n") + "\n" + tail)
    else:
        write("AGENTS.md", agents.rstrip("\n") + "\n" + AGENTS)
    return changed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    done = apply(Path(sys.argv[1]))
    print("changed: " + ", ".join(done) if done else "already applied")
