#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2", "psycopg[binary]>=3.2"]
# ///
"""Publish a validated daily report into rolling project review queues; preview by default."""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import quote
from uuid import UUID

from psycopg.types.json import Jsonb
from prototype_format import prototype_markdown, merge_option, MARKER
from prior import read_prior
from prompt_compose import compose_prompt
from queue_db import connect, project_ids
from report_schema import Report, load_report, owner_prompt_allowed


def technical_options(change):
    return [
        {'label': 'Do it', 'instruction': change or 'Perform the bounded task described in this finding.'},
        {'label': 'Investigate first', 'instruction': 'Investigate the cited evidence and return a bounded plan before changing behavior.'},
        {'label': "Accept (won't fix)", 'instruction': 'State the condition that would justify retesting this accepted issue.'},
        {'label': 'Noise (never again)', 'instruction': 'Suppress this pattern; state an explicit retest trigger if one exists.'},
        {'label': 'Later', 'instruction': 'Enter the revisit date as YYYY-MM-DD.'},
    ]


def ensure_session(conn, project_id, audience, today, tenant_id=None):
    row = conn.execute("SELECT id FROM review_sessions WHERE project_id=%s AND tenant_id IS NOT DISTINCT FROM %s AND audience=%s AND cadence='queue' AND status='open' FOR UPDATE", (project_id, tenant_id, audience)).fetchone()
    if row:
        conn.execute('UPDATE review_sessions SET period_end=GREATEST(period_end,%s) WHERE id=%s', (today, row['id']))
        return row['id']
    return conn.execute("""INSERT INTO review_sessions(project_id,tenant_id,audience,cadence,period_start,period_end)
      VALUES (%s,%s,%s,'queue',%s,%s) RETURNING id""", (project_id, tenant_id, audience, today, today)).fetchone()['id']


def evidence_for(conn, finding, project_id):
    ids = list(dict.fromkeys(finding.evidence.run_ids + [e.run_id for e in finding.evidence.entries] + [url.split('/')[-1].split('?')[0] for url in finding.evidence.run_urls]))
    result = {}
    for value in ids:
        if re.fullmatch(r'[0-9a-fA-F]{8}', value):
            rows = conn.execute('SELECT id,created_at FROM runs WHERE project_id=%s AND id::text LIKE %s', (project_id, value.lower() + '%')).fetchall()
            if len(rows) > 1:
                raise ValueError(f'Ambiguous run prefix: {value}')
        else:
            try:
                value = str(UUID(value))
            except ValueError as exc:
                raise ValueError(f'Invalid evidence run id: {value}') from exc
            rows = conn.execute('SELECT id,created_at FROM runs WHERE project_id=%s AND id=%s', (project_id, value)).fetchall()
        for row in rows:
            result[str(row['id'])] = row['created_at']
    return result


def transition(old, finding, run_times, focus):
    if not old:
        if finding.status == 'unchanged':
            raise ValueError(f'{finding.signature}: unchanged has no item in this project/audience')
        return 'insert', None
    if old['last_seen'] and focus < old['last_seen']:
        return 'skip', 'older than latest publication'
    if old['status'] == 'applied':
        if not old['applied_at'] or not any(t > old['applied_at'] for t in run_times):
            return 'skip', 'no evidence after applied_at'
        if finding.status == 'unchanged':
            raise ValueError('Regression requires full finding fields; use changed')
        commits = [r.get('commit') for r in old['applied'].get('rounds', []) if r.get('commit')]
        return 'regression', 'seen again after fix ' + (', '.join(commits) or '(receipt has no commit)')
    if old['status'] == 'closed':
        disposition = old['applied'].get('disposition')
        if disposition == 'later':
            match = re.search(r'\b\d{4}-\d{2}-\d{2}\b', old['decision_instruction'] or '')
            if not match or focus <= date.fromisoformat(match[0]):
                return 'skip', 'Later date not passed (or missing)'
        else:
            # Reintroduction is an explicit judge assertion, never a side effect of carry-over.
            update = finding.update_en or finding.update_nl
            if finding.status != 'changed' or not update:
                return 'skip', 'closed; changed + update explaining the met retest trigger required'
        if finding.status == 'unchanged':
            raise ValueError('Reopening requires full finding fields; use changed')
        return 'reopen', None
    return 'update', None


def payload(finding, audience, run_ids, linked=None):
    prompt = {}
    if finding.prompt and (audience == 'technical' or owner_prompt_allowed(finding)):
        prompt = finding.prompt.model_dump(exclude_none=True)
        prompt['text'] = compose_prompt(finding)
    return {
        'subject': finding.title_nl if audience == 'owner' else finding.title, 'kind': 'got_it_right' if finding.kind == 'good' else finding.kind,
        'severity': 'low' if finding.severity == 'good' else finding.severity,
        'plane': finding.root_cause.plane if finding.root_cause else 'unknown',
        'scope_label': finding.scope.axis_value() or 'project',
        'body': ((finding.text_en or '') + (MARKER + prototype_markdown(finding.prototype) if finding.prototype else '')) if audience == 'technical' else (finding.text_nl or ''),
        'ask': finding.ask_nl if audience == 'owner' else None,
        'ask_for': finding.ask_for if audience == 'owner' else None,
        'evidence': [dict(run_id=r, label=r[:8]) | next((e.model_dump(exclude_none=True) for e in finding.evidence.entries if e.run_id.lower() == r.lower()), {}) for r in run_ids],
        'prompt': prompt,
        'options': ([merge_option(finding.prototype)] if finding.prototype else []) + technical_options(finding.prompt.change if finding.prompt else '') if audience == 'technical'
                   else [o.model_dump() for o in finding.options],
        'linked_item_id': linked,
    }


JSON_FIELDS = {'evidence', 'prompt', 'options', 'updates', 'applied'}


def insert_item(conn, session_id, signature, focus, values, *, status='open', applied=None, instruction=None):
    position = conn.execute('SELECT COALESCE(MAX(position),0)+1 AS n FROM review_items WHERE session_id=%s', (session_id,)).fetchone()['n']
    fields = dict(session_id=session_id, position=position, signature=signature, first_seen=focus,
                  last_seen=focus, status=status, **values)
    if applied is not None:
        fields['applied'] = applied
    if instruction is not None:
        fields['decision_instruction'] = instruction
    keys = list(fields)
    return conn.execute('INSERT INTO review_items (' + ','.join(keys) + ') VALUES (' + ','.join(['%s'] * len(keys)) + ') RETURNING id',
                        [Jsonb(fields[k]) if k in JSON_FIELDS else fields[k] for k in keys]).fetchone()['id']


def publish(conn, report, *, write=False):
    projects = project_ids(conn, report.coverage.projects)
    focus = date.fromisoformat(report.date)
    today = datetime.now(timezone.utc).date()
    if write:
        # Fixed order serializes combined-member reports and independent scheduled jobs.
        for name in sorted(projects):
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('review-queue:' + name,))
    sessions = {}
    for name, project_id in projects.items():
        for audience in ('owner', 'technical'):
            if write:
                sessions[name, audience] = ensure_session(conn, project_id, audience, today)
            else:
                row = conn.execute("SELECT id FROM review_sessions WHERE project_id=%s AND tenant_id IS NULL AND audience=%s AND cadence='queue' AND status='open'", (project_id, audience)).fetchone()
                sessions[name, audience] = row['id'] if row else None
    output = []
    for finding in report.findings:
        members = finding.members or ([report.coverage.projects[0]] if len(projects) == 1 else [])
        if not members or set(members) - projects.keys():
            raise ValueError(f'{finding.signature}: specify valid member projects')
        if finding.recurrence.state == 'gone':
            continue  # Absence is not a sighting and cannot reopen or resolve a queue item.
        for member in sorted(set(members)):
            project_id = projects[member]
            tenant_id = None
            if finding.scope.level == 'tenant':
                tenant = conn.execute('SELECT id FROM tenants WHERE project_id=%s AND slug=%s', (project_id, finding.scope.tenant)).fetchone()
                if not tenant:
                    raise ValueError(f'Unknown tenant {member}/{finding.scope.tenant}')
                tenant_id = tenant['id']
            runs = evidence_for(conn, finding, project_id)
            if (finding.evidence.run_ids or finding.evidence.run_urls or finding.evidence.entries) and not runs:
                raise ValueError(f'{finding.signature}: no cited evidence belongs to {member}')
            linked = None
            audiences = ('owner', 'technical') if finding.audience == 'both' else (finding.audience,)
            for audience in audiences:
                if audience == 'owner' and tenant_id is not None:
                    key = (member, audience, tenant_id)
                    if key not in sessions:
                        if write:
                            sessions[key] = ensure_session(conn, project_id, audience, today, tenant_id)
                        else:
                            row = conn.execute("SELECT id FROM review_sessions WHERE project_id=%s AND tenant_id=%s AND audience=%s AND cadence='queue' AND status='open'", (project_id, tenant_id, audience)).fetchone()
                            sessions[key] = row['id'] if row else None
                    session = sessions[key]
                else:
                    session = sessions[member, audience]
                old = conn.execute('SELECT * FROM review_items WHERE session_id=%s AND signature=%s' + (' FOR UPDATE' if write else ''), (session, finding.signature)).fetchone() if session else None
                legacy = False
                if not old and audience == 'owner' and tenant_id is not None:
                    # Pre-tenant-routing cards keep their identity/decisions on their next sighting:
                    # prefer the tenant-labelled row, else a card first filed at project scope.
                    old = conn.execute("SELECT * FROM review_items WHERE session_id=%s AND signature=%s AND scope_label IN (%s,'project')"
                                       " ORDER BY scope_label=%s DESC LIMIT 1" + (' FOR UPDATE' if write else ''),
                                       (sessions[member, audience], finding.signature, finding.scope.tenant, finding.scope.tenant)).fetchone()
                    legacy = old is not None
                action, note = transition(old, finding, runs.values(), focus)
                output.append({'project': member, 'audience': audience, 'signature': finding.signature, 'action': action, 'note': note})
                if action == 'skip':
                    if audience == 'owner' and old:
                        linked = old['id']
                    continue
                if not write:
                    continue
                if legacy and action == 'update':
                    position = conn.execute('SELECT COALESCE(MAX(position),0)+1 AS n FROM review_items WHERE session_id=%s', (session,)).fetchone()['n']
                    conn.execute('UPDATE review_items SET session_id=%s,position=%s,scope_label=%s WHERE id=%s', (session, position, finding.scope.tenant, old['id']))
                update = finding.update_en if audience == 'technical' else finding.update_nl
                if action in ('regression', 'reopen'):
                    # The locked unique(session_id,signature) contract needs the historical generation
                    # to release its slot. Keep its exact signature plus a reversible UUID suffix.
                    conn.execute('UPDATE review_items SET signature=signature || %s WHERE id=%s', ('#archived:' + str(old['id']), old['id']))
                if action in ('insert', 'regression', 'reopen'):
                    values = payload(finding, audience, runs, linked)
                    if action == 'regression':
                        values['kind'] = 'regression'
                        recurrence_note = note if audience == 'technical' else 'Opnieuw gezien na de eerdere oplossing'
                        update = recurrence_note + (': ' + update if update else '')
                    values['updates'] = [{'date': report.date, 'text': update}] if update else []
                    item_id = insert_item(conn, session, finding.signature, focus, values)
                else:
                    # Do not change the task a human has already decided while its worker may run.
                    values = payload(finding, audience, runs, linked) if finding.status != 'unchanged' and old['status'] == 'open' else {}
                    if old['kind'] == 'regression':
                        values.pop('kind', None)
                    # Owner generation may reopen while this approved task is frozen. Keep its
                    # dependency current so the implement job waits for the new owner decision.
                    if audience == 'technical' and finding.audience == 'both':
                        values['linked_item_id'] = linked
                    updates = old['updates']
                    entry = {'date': report.date, 'text': update}
                    if update and entry not in updates:
                        updates = updates + [entry]
                    values.update(last_seen=focus, occurrences=old['occurrences'] + int(old['last_seen'] != focus), updates=updates)
                    assignments = ','.join(k + '=%s' for k in values)
                    conn.execute('UPDATE review_items SET ' + assignments + ' WHERE id=%s',
                                 [Jsonb(v) if k in JSON_FIELDS else v for k, v in values.items()] + [old['id']])
                    item_id = old['id']
                if audience == 'owner':
                    linked = item_id
    return output


def urls(projects):
    return [f'https://app.replypen.com/projects/{quote(project, safe="")}/weekly-review?audience={audience}'
            for project in projects for audience in ('owner', 'technical')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--dsn')
    args = parser.parse_args()
    raw = Report.model_validate_json(args.report.read_text())
    with connect(args.dsn, write=args.write) as conn:
        report = load_report(args.report, prior=read_prior(conn, raw.coverage.projects))
        print(json.dumps(publish(conn, report, write=args.write), indent=2, default=str))
    print('Published.' if args.write else 'Preview only; pass --write to publish.')
    print('\n'.join(urls(report.coverage.projects)))


if __name__ == '__main__':
    main()
