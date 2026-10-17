# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect inbound run questions and compact source evidence via public rp; cluster lexically."""
import argparse
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from common import cell, link, read, words, write

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'brain-helpcenter-suggestions' / 'scripts'))
from corpus import clean  # shared email quote/signature cleanup


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

PATH = re.compile(r'/(?:brain|kb|tenant)/[^\s\x00\'"`<>;|]+?\.(?:md|txt|csv|html|json)(?=$|[\s\'"`:;,|>&\]\)])')


def paths(value):
    found = set(PATH.findall(value))
    for match in re.finditer(r"(['\"])(/(?:brain|kb|tenant)/[^\n]*?)\1", value):
        if re.search(r'\.(?:md|txt|csv|html|json)$', match[2]):
            found.add(match[2])
    return sorted(found)


def reduce_trace(records):
    header = next((x for x in records if x.get('type') == 'run'), {})
    evidence = []
    events = 0
    for event in records:
        if event.get('type') != 'event':
            continue
        events += 1
        args = event.get('args') or {}
        if not isinstance(args, dict):
            args = {}
        for item in args.get('selected') or []:
            if isinstance(item, dict) and item.get('path'):
                evidence.append({'path': item['path'], 'method': 'grounding_selected', 'seq': event.get('seq')})
        command = event.get('command') or args.get('command') or ''
        if event.get('tool') == 'bash' and command:
            # A command is evidence of an attempted access, not proof its content reached the answer.
            method = 'command_reference'
            if re.search(r'\b(cat|sed|head|tail|nl|read_text)\b', command):
                method = 'read_command' if event.get('exit_code') == 0 else 'failed_read'
            for path in paths(command):
                evidence.append({'path': path, 'method': method, 'seq': event.get('seq')})
        if args.get('journal'):
            for path in paths(json.dumps(args['journal'], ensure_ascii=False)):
                evidence.append({'path': path, 'method': 'journal_reference', 'seq': event.get('seq')})
    return {'run_id': header.get('run_id'), 'project': header.get('project'), 'tenant': header.get('tenant'),
            'question': clean(header.get('question'), limit=12000),
            'topic': header.get('topic', ''), 'events': events,
            'evidence': list({(e['path'], e['method']): e for e in evidence}.values())}


def rc(args):
    done = subprocess.run([_cli(), *args, '-o', 'json', '--raw-output'], capture_output=True, text=True, timeout=180)
    if done.returncode:
        raise RuntimeError(done.stderr[:300] or done.stdout[:300])
    if done.stderr.strip():
        raise RuntimeError('rc warning; refusing an unqualified census: ' + done.stderr[:300])
    return done.stdout


def moment(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def collect(args):
    scope = ['--project', args.project, '--scope', 'tenant' if args.tenant else 'project']
    if args.tenant:
        scope += ['--tenant', args.tenant]
    raw = read(args.runs) if args.runs else json.loads(rc(['fleet', 'runs', '--days', str(args.days), *scope]))
    if not isinstance(raw, dict) or not isinstance(raw.get('runs'), list):
        raise ValueError('Expected rp fleet runs JSON object with runs list')
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    rows = [r for r in raw['runs'] if r.get('kind') in args.kind and not r.get('simulation')
            and start <= moment(r['created_at']) <= end]
    cache = Path(args.cache) / args.project / (args.tenant or '_project')
    cache.mkdir(parents=True, exist_ok=True)

    def fetch(row):
        target = cache / (row['run_id'] + '.json')
        try:
            if target.exists() and not args.refresh:
                reduced = read(target)
            else:
                raw_trace = rc(['run', 'trace', row['run_id'], '--stream', *scope])
                records = [json.loads(line) for line in raw_trace.splitlines() if line.strip()]
                reduced = reduce_trace(records)
                if reduced['run_id'] != row['run_id']:
                    raise ValueError('trace header missing or mismatched')
                write(target, reduced)
            if reduced.get('project') and reduced['project'] != args.project:
                raise ValueError('trace project does not match requested scope')
            if args.tenant and reduced.get('tenant') != args.tenant:
                raise ValueError('trace tenant missing or does not match requested scope')
            return {**reduced, 'created_at': row['created_at'],
                    'thread': row.get('local_thread_id') or row.get('session_id') or row['run_id'],
                    'turn_key': row.get('turn_key'), 'topic': reduced['topic'] or row.get('topic', '')}
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            return {'run_id': row['run_id'], 'error': str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        traces = list(pool.map(fetch, rows))
    questions = {}
    for trace in traces:
        if trace.get('error') or not trace.get('question'):
            continue
        normalized = re.sub(r'\s+', ' ', trace['question'].lower()).strip()
        # Retry turn_key values may be synthetic (retry:UUID); dedupe text within the thread.
        key = (trace['thread'], normalized)
        question = questions.setdefault(key, {'text': trace['question'], 'subject': trace['topic'], 'run_ids': [], 'created_at': trace['created_at']})
        question['run_ids'].append(trace['run_id'])
    return {'schema': 'source-routing/v1', 'project': args.project, 'tenant': args.tenant,
            'start': start.isoformat(), 'end': end.isoformat(), 'runs': traces,
            'questions': list(questions.values()), 'coverage': {
                'runs_in_window': len(rows), 'trace_errors': sum('error' in t for t in traces),
                'missing_question': sum(not t.get('question') and 'error' not in t for t in traces),
                'question_count': len(questions),
                'limits': 'Run-backed inbound turns only; triage-skipped mail has no run. Retained text may cover less than the requested window. Source evidence may be scrubbed. Counts include follow-ups; lexical topics are provisional.'}}


def cluster(data, definitions=None):
    topics = []
    for label, terms in (definitions or {}).items():
        topics.append({'topic': label, 'terms': words(' '.join(terms)), 'questions': []})
    for q in data['questions']:
        tokens = words(q['subject'] + ' ' + q['text'])
        if definitions:
            scores = [len(tokens & t['terms']) for t in topics]
        else:
            scores = [len(tokens & t['terms']) / max(1, len(tokens | t['terms'])) for t in topics]
        best = max(range(len(scores)), key=scores.__getitem__) if scores else None
        if best is not None and scores[best] >= (1 if definitions else .18):
            topics[best]['questions'].append(q)
        elif definitions:
            q['unclassified'] = True
        else:
            terms = words(q['subject']) or tokens
            label = ' / '.join(sorted(terms)[:4]) or 'Unclassified'
            topics.append({'topic': label, 'terms': tokens, 'questions': [q]})
    unclassified = [q for q in data['questions'] if q.get('unclassified')]
    if unclassified:
        topics.append({'topic': 'Overig / handmatig indelen', 'terms': set(), 'questions': unclassified})
    for t in topics:
        t['terms'] = sorted(t['terms'])
        t['count'] = len(t['questions'])
        t['unique_texts'] = len({hashlib.sha256(q['text'].lower().encode()).hexdigest() for q in t['questions']})
        t['run_ids'] = sorted({r for q in t['questions'] for r in q['run_ids']})
    data['topics'] = sorted([t for t in topics if t['count']], key=lambda t: (-t['count'], t['topic']))
    return data


def markdown(data):
    lines = ['# Question digest', '', f"Window: {data['start'][:10]} — {data['end'][:10]}", '',
             str(data['coverage']), '']
    for topic in data['topics']:
        lines += [f"- **{cell(topic['topic'])}** — {topic['count']} questions ({topic['unique_texts']} distinct texts)"]
    for topic in data['topics']:
        lines += ['', f"<details><summary>{cell(topic['topic'])}: examples</summary>", '']
        for q in topic['questions'][:2]:
            lines.append('- ' + link(q['subject'] or 'Question', 'https://app.replypen.com/runs/' + q['run_ids'][0]))
        lines += ['', '</details>']
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True)
    p.add_argument('--tenant')
    p.add_argument('--days', type=int, default=60)
    p.add_argument('--kind', action='append', choices=['email', 'chat', 'analysis'])
    p.add_argument('--runs', help='optional previously exported rp fleet runs JSON; still date/scope filtered')
    p.add_argument('--input', help='offline previously collected digest JSON; recluster without rc')
    p.add_argument('--topics', help='optional JSON {topic: [keywords]} for language/domain vocabulary')
    p.add_argument('--cache', default='.replypen/source-routing/cache')
    p.add_argument('--refresh', action='store_true')
    p.add_argument('--out', required=True, help='JSON output')
    p.add_argument('--markdown', required=True)
    args = p.parse_args()
    if args.days < 1:
        p.error('--days must be positive')
    args.kind = args.kind or ['email', 'chat', 'analysis']
    data = read(args.input) if args.input else collect(args)
    if data['project'] != args.project or data.get('tenant') != args.tenant:
        p.error('input project/tenant does not match requested scope')
    for q in data['questions']:
        q.pop('unclassified', None)
    cluster(data, read(args.topics) if args.topics else None)
    write(args.out, data)
    Path(args.markdown).write_text(markdown(data))
    print(json.dumps(data['coverage']))


if __name__ == '__main__':
    main()
