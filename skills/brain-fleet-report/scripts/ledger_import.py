#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "psycopg[binary]>=3.2"]
# ///
"""One-off ledger migration. Preview the mapping; --write inserts without overwriting decisions."""
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from publish import ensure_session, insert_item, technical_options, urls
from queue_db import connect, project_ids


def parse_ledger(path, *, mapping=None):
    mapping = mapping or {}
    result = []
    header = None
    for line in Path(path).read_text().splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip('|'))]
        if any(c.lower() == 'pattern' for c in cells):
            header = [c.lower() for c in cells]
            continue
        if header is None or all(re.fullmatch(r'[-: ]+', c) for c in cells):
            continue
        if len(cells) != len(header):
            raise ValueError(f'Malformed ledger table row: {line}')
        row = dict(zip(header, cells))
        pattern, disposition = row['pattern'], row['disposition']
        retest = row.get('re-test when', row.get('retest-when', ''))
        text = disposition.lower()
        if any(word in text for word in ('fix-landed', 'resolved', 'closed ', 'transient')):
            status, outcome = 'applied', 'done'
        elif re.match(r'(?:\*\*)?(?:accepted|platform|benign|by design)', text) and 'open' not in text:
            status, outcome = 'closed', 'accepted'
        elif re.match(r'(?:\*\*)?noise', text):
            status, outcome = 'closed', 'noise'
        else:
            status, outcome = 'open', None
        explicit = re.search(r'`?\b((?:action_failure|capture_gap|correction|policy_question|script_error|usage|path_miss):[a-z][a-z0-9_-]+)\b', pattern)
        signature = explicit[1] if explicit else 'pattern:ledger-' + hashlib.sha256(pattern.encode()).hexdigest()[:12]
        since = row.get('since') or '2026-09-04'
        severity = 'high' if any(w in pattern.lower() for w in ('no draft', 'lost', 'wrong', 'failed', 'permission denied')) else 'medium'
        values = dict(signature=signature, pattern=pattern, disposition=disposition,
                      instruction=retest, first_seen=since, severity=severity,
                      status=status, outcome=outcome, plane='unknown')
        override = mapping.get(pattern, {})
        unknown = set(override) - {'signature', 'first_seen', 'severity', 'status', 'outcome', 'plane', 'project', 'applied_at'}
        if unknown:
            raise ValueError('Unknown mapping fields: ' + ', '.join(sorted(unknown)))
        values.update(override)
        if values['status'] not in ('open', 'closed', 'applied') or values['severity'] not in ('high','medium','low'):
            raise ValueError('Invalid status/severity mapping')
        result.append(values)
    if not result:
        raise ValueError('No ledger rows found')
    unused = mapping.keys() - {r['pattern'] for r in result}
    if unused:
        raise ValueError('Mapping names a missing pattern: ' + ', '.join(unused))
    return result


def import_rows(conn, project, rows, *, write=False):
    names = {r.get('project') or project for r in rows}
    projects = project_ids(conn, names)
    today = datetime.now(timezone.utc).date()
    sessions = {}
    if write:
        for name in sorted(names):
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('review-queue:' + name,))
            sessions[name] = ensure_session(conn, projects[name], 'technical', today)
    result = []
    for row in rows:
        name = row.get('project') or project
        old = conn.execute("""SELECT i.id FROM review_items i JOIN review_sessions s ON s.id=i.session_id
          WHERE s.project_id=%s AND s.tenant_id IS NULL AND s.audience='technical' AND s.cadence='queue'
          AND (i.signature=%s OR starts_with(i.signature,%s)) LIMIT 1""",
          (projects[name], row['signature'], row['signature'] + '#archived:')).fetchone()
        result.append(dict(project=name, **{k: v for k, v in row.items() if k != 'project'}, action='preserved' if old else 'insert' if write else 'would insert'))
        if not write or old:
            continue
        receipt = {'disposition': row['outcome'], 'rounds': [], 'note': 'Imported historical ledger: ' + row['disposition']} if row['outcome'] else {}
        values = dict(subject=re.sub(r'[`*]', '', row['pattern'])[:180], kind='pattern', severity=row['severity'],
                      plane=row['plane'], scope_label='project', body=row['pattern'] + '\n\n' + row['disposition'],
                      evidence=[], prompt={}, options=technical_options(row['disposition']), updates=[])
        item = insert_item(conn, sessions[name], row['signature'], date.fromisoformat(row['first_seen']), values,
                           status=row['status'], applied=receipt, instruction=row['instruction'])
        if row['status'] == 'applied':
            applied_at = datetime.fromisoformat(row['applied_at'].replace('Z','+00:00')) if row.get('applied_at') else datetime.now(timezone.utc)
            conn.execute('UPDATE review_items SET applied_at=%s WHERE id=%s', (applied_at, item))
        if row['outcome']:
            conn.execute('UPDATE review_items SET decision_label=%s WHERE id=%s',
                         ('Noise (never again)' if row['outcome'] == 'noise' else "Accept (won't fix)" if row['outcome'] == 'accepted' else 'Do it', item))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('brain', type=Path)
    parser.add_argument('--project')
    parser.add_argument('--mapping', type=Path, help='Reviewed overrides keyed by the exact Pattern cell')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--dsn')
    args = parser.parse_args()
    project = args.project or args.brain.resolve().name.removeprefix('replypen-brain-').removeprefix('rootcause-brain-')
    rows = parse_ledger(args.brain / '_internal/fleet-report/ledger.md', mapping=json.loads(args.mapping.read_text()) if args.mapping else None)
    with connect(args.dsn, write=args.write) as conn:
        result = import_rows(conn, project, rows, write=args.write)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print('\n'.join(urls(sorted({r['project'] for r in result}))))


if __name__ == '__main__':
    main()
