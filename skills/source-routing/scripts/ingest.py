# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Ingest an explicitly reviewed table into notes/source-routing.md; preserve human sections."""
import argparse
import html
from pathlib import Path
import re
from common import table, write

START = '<!-- source-routing:generated:start -->'
END = '<!-- source-routing:generated:end -->'
HUMAN_START = '<!-- source-routing:human:start -->'
HUMAN_END = '<!-- source-routing:human:end -->'


def parse(text):
    rows = []
    active = False
    for line in text.splitlines():
        if line.strip().lower() == '| topic | document | path | why | verdict |':
            active = True
            continue
        if not active:
            continue
        if not line.startswith('|'):
            active = False
            continue
        parts = [html.unescape(s.strip()) for s in line.strip().strip('|').split('|')]
        if all(re.fullmatch(r'[-: ]+', s) for s in parts):
            continue
        if len(parts) != 5 or parts[4] not in {'use', 'outdated', 'noise', '?'}:
            raise ValueError('Malformed routing row or invalid verdict: ' + line[:160])
        topic, doc, path, why, verdict = parts
        if verdict != '?' and not path.startswith(('/brain/', '/kb/', '/tenant/', '/mirrors/')):
            raise ValueError('Decided rows require an absolute runtime document path')
        if '..' in Path(path).parts:
            raise ValueError('Parent traversal is not a source path')
        match = re.fullmatch(r'\[(.*?)\]\((.*?)\)', doc)
        rows.append({'topic': topic, 'title': match[1] if match else doc, 'url': match[2] if match else '', 'path': path, 'why': why, 'verdict': verdict})
    if not rows:
        raise ValueError('No routing table rows found; refusing an accidental empty replacement')
    return rows


def merge(old, rows):
    for start, end in [(START, END), (HUMAN_START, HUMAN_END)]:
        if old.count(start) != old.count(end) or old.count(start) > 1:
            raise ValueError('Unbalanced or repeated routing markers')
        if start in old and old.index(start) > old.index(end):
            raise ValueError('Reversed routing markers')
    if START in old and HUMAN_START in old and old.index(START) < old.index(HUMAN_START) < old.index(END):
        raise ValueError('Human overrides must be outside the generated section')
    protected = []
    human = re.search(re.escape(HUMAN_START) + '(.*?)' + re.escape(HUMAN_END), old, re.S)
    if human:
        protected = parse(human[1]) if '| topic |' in human[1].lower() else []
    keys = {(r['topic'], r['path']) for r in protected}
    generated = [r for r in rows if (r['topic'], r['path']) not in keys]
    effective = generated + protected
    preferences = []
    for topic in sorted({r['topic'] for r in effective}):
        prefer = [r['path'] for r in effective if r['topic'] == topic and r['verdict'] == 'use']
        avoid = [r['path'] for r in effective if r['topic'] == topic and r['verdict'] in {'outdated', 'noise'}]
        from common import cell
        preferences.append(f"- {cell(topic)} — prefer: {cell(', '.join(prefer) or '—')}; avoid: {cell(', '.join(avoid) or '—')}.")
    section = START + '\n' + table(generated) + '\n' + '\n'.join(preferences) + '\n' + END
    if START in old:
        return old[:old.index(START)] + section + old[old.index(END) + len(END):]
    return old.rstrip() + '\n\n' + section + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reviewed', required=True, help='human-pruned Markdown; never the unreviewed proposal')
    p.add_argument('--brain', required=True)
    p.add_argument('--apply', action='store_true', help='write locally; default prints preview; does not publish')
    p.add_argument('--out', help='optional parsed JSON output')
    a = p.parse_args()
    rows = parse(Path(a.reviewed).read_text())
    target = Path(a.brain) / 'notes/source-routing.md'
    old = target.read_text() if target.exists() else '# Source routing\n\nReviewed source preferences; current primary evidence still wins.\n'
    merged = merge(old, rows)
    if a.out:
        write(a.out, {'rows': rows})
    if a.apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        if merged != old:
            target.write_text(merged)
        print(target.resolve())
    else:
        print(merged)


if __name__ == '__main__':
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import kit_fresh  # re-execs from origin/main when the installed kit is behind
    kit_fresh.ensure(__file__)
    main()
