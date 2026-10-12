#!/usr/bin/env -S uv run --no-project python
"""Local Gmail inventory + public rc evidence; never runs a production agent or sends mail."""
from __future__ import annotations
import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone
from email.utils import getaddresses
from urllib.parse import quote, urlsplit, urlunsplit

GMAIL = 'https://gmail.googleapis.com/gmail/v1/users/me'
PRIMARY_ACTION_ROWS = 7


class Refused(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def read(path):
    return json.loads(Path(path).read_text())


def cli_json(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f'Command unavailable or timed out: {Path(argv[0]).name}') from exc
    if result.returncode:
        # Neither stderr nor stdout is safe to print: either can contain credentials or mail.
        raise Refused(f'Command failed (exit {result.returncode}); check login/scopes locally')
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict) and (data.get('error') or data.get('spilled') or
                data.get('type') in ('spill', 'output_artifact') or
                ('artifact' in data and 'preview' in data)):
            raise Refused('Command returned an error or spilled payload; full evidence is required')
        return data
    except ValueError as exc:
        raise Refused('Command returned a non-JSON response') from exc


class Client:
    def __init__(self, config):
        self.config = config

    def gws(self, *args):
        return cli_json(['uv', 'run', '--script', self.config['gws_script'], '--config',
                         self.config['gws_config'], '--json', *args])

    def rc(self, *args):
        return cli_json(['rc', '--project', self.config['project'], *args, '-o', 'json', '--raw-output'])

    def api(self, path, **params):
        args = ['api', 'GET', GMAIL + '/' + path]
        for key, value in params.items():
            args += ['--param', f'{key}={value}']
        return self.gws(*args)

    def pages(self, path, key, max_pages=20, **params):
        items, token = [], None
        for _ in range(max_pages):
            page = self.api(path, maxResults=500, **params,
                            **({'pageToken': token} if token else {}))
            items.extend(page.get(key, []))
            token = page.get('nextPageToken')
            if not token:
                break
        return items, bool(token)


def verify_mailbox(client):
    identity = str(client.gws('whoami').get('email', '')).lower()
    boxes = client.rc('project', 'mailbox', 'ls').get('mailboxes', [])
    selected = [box for box in boxes if box.get('id') == client.config['mailbox_id']]
    if len(selected) != 1 or selected[0].get('provider') != 'google' or not identity or (
            selected[0].get('email_address', '').lower() != identity):
        raise Refused('Mailbox UUID/provider/email does not match the authenticated GWS account')
    return identity


def headers(message):
    return {h['name'].lower(): h.get('value', '')
            for h in message.get('payload', {}).get('headers', [])}


def addresses(value):
    return {email.lower() for _, email in getaddresses([value]) if email}


def stamp(message):
    return int(message.get('internalDate') or 0)


def fingerprint(thread):
    # Full messages catch edits, changed bodies/recipients, outbound mail and Gmail draft saves.
    return digest({'id': thread.get('id'), 'messages': thread.get('messages', [])})


def mail_state(thread, account):
    inbound, replies, notes, uncertain = [], [], [], []
    for message in sorted(thread.get('messages', []), key=stamp):
        labels = message.get('labelIds', [])
        if 'DRAFT' in labels:
            continue
        h = headers(message)
        own = 'SENT' in labels
        if not own:
            inbound.append(message)
        elif addresses(h.get('to', '')) == {account}:
            notes.append(message)
        elif 'SENT' in labels:
            automated = (any(h.get(k) for k in ('list-id', 'list-unsubscribe',
                                               'x-campaign', 'x-ac-message-id')) or
                         h.get('auto-submitted', '').lower() not in ('', 'no') or
                         h.get('precedence', '').lower() in ('bulk', 'list', 'junk'))
            if automated or not addresses(h.get('to', '')):
                uncertain.append(message)
            else:
                replies.append(message)
        else:
            uncertain.append(message)
    latest = inbound[-1] if inbound else None
    answered = bool(latest and any(stamp(m) > stamp(latest) for m in replies))
    ambiguous = bool(latest and any(stamp(m) > stamp(latest) for m in uncertain))
    ambiguous_sender = bool(latest and account in addresses(headers(latest).get('from', '')))
    return {'latest_inbound': latest, 'answered': answered, 'ambiguous_outbound': ambiguous,
            'ambiguous_sender': ambiguous_sender,
            'notes': notes, 'replies': replies, 'unanswered': bool(latest and not answered)}


def google_evidence(data, thread_id):
    threads = [t for t in data.get('threads', []) if t.get('provider') == 'google'
               and t.get('external_thread_id') == thread_id]
    local_ids = {t.get('local_thread_id') for t in threads}
    runs = [r for r in data.get('runs', []) if not r.get('simulation') and
            r.get('attribution', {}).get('thread_id') == thread_id and
            r.get('attribution', {}).get('local_thread_id') in local_ids]
    return {'threads': threads, 'runs': runs}


def latest_run(evidence):
    runs = evidence.get('runs', [])
    return max(runs, key=lambda r: r.get('created_at', ''), default=None)


def safe_text(value, limit=240):
    # Summary/report must not reproduce bearer URLs from mail/run notes.
    value = re.sub(r'https?://\S+', '[link]', str(value))
    return value[:limit]


def summary(thread, evidence, account, draft_stubs):
    state = mail_state(thread, account)
    run = latest_run(evidence)
    inbound = state['latest_inbound'] or {}
    if state['answered']:
        status = 'answered'
    elif not state['unanswered']:
        status = 'no_unanswered_inbound'
    elif state['ambiguous_sender']:
        status = 'uncertain_sender'
    elif state['ambiguous_outbound']:
        status = 'uncertain_outbound'
    elif not run:
        status = 'not_processed'
    elif not run.get('has_draft') or run.get('outcome') == 'declined':
        status = 'needs_human'
    else:
        status = 'draft_ready'
    return {'thread_id': thread['id'], 'subject': safe_text(headers(inbound).get('subject') or
            headers((thread.get('messages') or [{}])[0]).get('subject', '(no subject)'), 180),
            'inbound_preview': safe_text(inbound.get('snippet', ''), 360),
            'inbound_at': inbound.get('internalDate'), 'state': status,
            'note_count': len(state['notes']), 'run_id': (run or {}).get('run_id'),
            'run_finished_at': (run or {}).get('finished_at'), 'run_health': (run or {}).get('health', {}),
            'has_note': (run or {}).get('has_note', False), 'draft_count': len(draft_stubs),
            'gmail_url': gmail_url(account, thread['id'])}


def gmail_url(account, thread_id):
    if not re.fullmatch(r'[0-9a-f]+', thread_id):
        raise Refused('Invalid Gmail thread ID')
    return f'https://mail.google.com/mail/?authuser={quote(account, safe="")}#all/{thread_id}'


def collect(args):
    out = Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise Refused('Use a new empty output directory; old evidence must not masquerade as current')
    out.mkdir(parents=True, mode=0o700, exist_ok=True)
    config = {key: getattr(args, key) for key in ('project', 'mailbox_id', 'gws_script', 'gws_config')}
    config['gws_script'] = str(Path(config['gws_script']).expanduser().resolve())
    config['gws_config'] = str(Path(config['gws_config']).expanduser().resolve())
    client = Client(config)
    account = verify_mailbox(client)
    config['account'] = account
    save(out / 'config.json', config)
    inbox, inbox_more = client.pages('threads', 'threads', max_pages=args.max_pages,
                                     q=f'in:inbox newer_than:{args.days}d')
    drafts, drafts_more = client.pages('drafts', 'drafts', max_pages=args.max_pages)
    standalone = [d for d in drafts if d.get('message', {}).get('threadId') ==
                  d.get('message', {}).get('id')]
    by_thread = {}
    for draft in drafts:
        msg = draft.get('message', {})
        if draft not in standalone and msg.get('threadId'):
            by_thread.setdefault(msg['threadId'], []).append(draft)
    # Recent inbox first, then stale in-thread drafts; no body read for excluded/capped stubs.
    recent_ids = set(t['id'] for t in inbox)
    older_ids = set(by_thread) - recent_ids
    save(out / 'inventory.json', {'recent_inbox_ids': sorted(recent_ids),
                                 'older_draft_thread_ids': sorted(older_ids),
                                 'inbox_list_truncated': inbox_more, 'draft_list_truncated': drafts_more})
    recent = list(dict.fromkeys(t['id'] for t in inbox))
    older = [tid for tid in by_thread if tid in older_ids]
    ids = []
    while recent or older:
        ids.extend(recent[:2])
        del recent[:2]
        ids.extend(older[:1])
        del older[:1]
    ids = list(dict.fromkeys(ids))
    rows, failures = [], []
    for tid in ids[:args.max_threads]:
        try:
            thread = client.gws('gmail-thread', tid)
            evidence = google_evidence(client.rc('run', 'thread', tid), tid)
            save(out / 'threads' / f'{tid}.json', {'gmail': thread, 'rc': evidence,
                 'draft_stubs': by_thread.get(tid, []), 'fingerprint': fingerprint(thread),
                 'collected_at': now()})
            row = summary(thread, evidence, account, by_thread.get(tid, []))
            row['collection_source'] = 'recent_inbox' if tid in recent_ids else 'older_draft'
            rows.append(row)
        except Refused:
            failures.append(tid)
    index = {'collected_at': now(), 'account': account, 'project': args.project,
             'mailbox_id': args.mailbox_id, 'coverage': {
                 'days': args.days, 'discovered_threads': len(ids), 'checked_threads': len(rows),
                 'recent_inbox_discovered': len(recent_ids),
                 'recent_inbox_checked': sum(r['thread_id'] in recent_ids for r in rows),
                 'older_draft_threads_discovered': len(older_ids),
                 'older_draft_threads_checked': sum(r['thread_id'] in older_ids for r in rows),
                 'uncovered_threads': max(0, len(ids) - args.max_threads) + len(failures),
                 'list_truncated': inbox_more or drafts_more,
                 'standalone_drafts_excluded': len(standalone), 'failed_thread_ids': failures},
             'threads': rows}
    save(out / 'index.json', index)
    print(json.dumps({'index': str(out / 'index.json'), 'coverage': index['coverage']}))


def detail(args):
    out = Path(args.bundle)
    config = read(out / 'config.json')
    client = Client(config)
    verify_mailbox(client)
    thread = client.gws('gmail-thread', args.thread_id)
    evidence = google_evidence(client.rc('run', 'thread', args.thread_id), args.thread_id)
    run = latest_run(evidence)
    shown = client.rc('run', 'show', run['run_id']) if run else None
    stubs, truncated = client.pages('drafts', 'drafts')
    if truncated:
        raise Refused('Draft listing incomplete; cannot prove unique AI draft match')
    drafts = [client.api('drafts/' + d['id'], format='full') for d in stubs
              if d.get('message', {}).get('threadId') == args.thread_id]
    body = {'gmail': thread, 'rc': evidence, 'run': shown, 'drafts': drafts,
            'fingerprint': fingerprint(thread), 'collected_at': now()}
    save(out / 'details' / f'{args.thread_id}.json', body)
    print(json.dumps({'detail': str(out / 'details' / f'{args.thread_id}.json'),
                      'fingerprint': body['fingerprint'], 'run_id': (shown or {}).get('run_id')}))


def plain_body(message):
    parts = []
    def visit(part):
        if part.get('filename'):
            return
        if part.get('mimeType') == 'text/plain':
            data = part.get('body', {}).get('data')
            if data is None:
                raise Refused('Draft text is not inline; exact match cannot be proven')
            try:
                parts.append(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as exc:
                raise Refused('Draft body is not exact UTF-8 plain text') from exc
        for child in part.get('parts', []):
            visit(child)
    visit(message.get('payload', {}))
    if len(parts) != 1:
        raise Refused('Correction requires exactly one plain-text draft body')
    return parts[0]


def matched_draft(detail_data, draft_id):
    run = detail_data.get('run') or {}
    target = run.get('draft_markdown')
    if not target:
        raise Refused('Latest ReplyPen run has no draft body')
    matches = []
    for draft in detail_data.get('drafts', []):
        try:
            if draft.get('message', {}).get('threadId') == detail_data['gmail']['id'] and (
                    plain_body(draft['message']) == target):
                matches.append(draft)
        except Refused:
            continue
    if len(matches) != 1 or matches[0].get('id') != draft_id:
        raise Refused('No unique exact AI draft match; human edits are never overwritten')
    return matches[0]


def evidence_expired(detail_data):
    age = datetime.now(timezone.utc) - datetime.fromisoformat(detail_data['collected_at'])
    return not 0 <= age.total_seconds() <= 4 * 3600


def check_finding(item, detail_data, require_fresh=True):
    required = ('thread_id', 'run_id', 'draft_id', 'evidence_fingerprint', 'priority', 'verdict',
                'actionable', 'action', 'why', 'sources', 'changed_since_run')
    if any(key not in item for key in required):
        raise Refused('Finding is missing required evidence/action fields')
    if item['thread_id'] != detail_data['gmail']['id'] or item['evidence_fingerprint'] != (
            detail_data['fingerprint']):
        raise Refused('Finding references stale or different Gmail evidence')
    if item['run_id'] != (detail_data.get('run') or {}).get('run_id'):
        raise Refused('Finding does not reference the latest provider-filtered run')
    if require_fresh and evidence_expired(detail_data):
        raise Refused('Finding evidence is older than four hours; refresh and review')
    if item['priority'] not in ('today', 'soon', 'waiting') or item['verdict'] not in (
            'confirmed', 'incorrect', 'uncertain', 'needs_human'):
        raise Refused('Invalid finding priority/verdict')
    if not isinstance(item['actionable'], bool):
        raise Refused('Finding actionable must be a boolean independent of quality verdict')
    if not item['action'] or not item['why'] or not isinstance(item['sources'], list):
        raise Refused('Finding needs a concrete action, reason, and sources')
    if not item['sources'] or not all(isinstance(s, dict) and s.get('kind') in (
            'production', 'code', 'public', 'mail') and s.get('location') and s.get('fact')
            for s in item['sources']):
        raise Refused('Supply independent sources with kind, location and verified fact')
    if item['draft_id'] is not None and item['draft_id'] not in [
            d['id'] for d in detail_data.get('drafts', [])]:
        raise Refused('Finding references an unknown Gmail draft')
    if 'replacement_body' in item:
        if item['verdict'] != 'incorrect' or not isinstance(item['replacement_body'], str) or not (
                item['replacement_body'].strip()):
            raise Refused('Correction requires demonstrably incorrect content and a complete body')
        draft = matched_draft(detail_data, item['draft_id'])
        original = plain_body(draft['message'])
        marker = re.search(r'(?m)^(?:On .+wrote:|Op .+schreef.*:|> |--+ Original Message)', original)
        if marker and not item['replacement_body'].endswith(original[marker.start():]):
            raise Refused('Correction must preserve the original quoted suffix exactly')


def findings(bundle, path, require_fresh=True):
    data = read(path)
    if not isinstance(data, list):
        raise Refused('Findings must be a JSON array')
    scope = {row['thread_id'] for row in read(Path(bundle) / 'index.json')['threads']}
    seen = set()
    for item in data:
        if not isinstance(item, dict) or not re.fullmatch(r'[0-9a-f]+', str(item.get('thread_id', ''))):
            raise Refused('Finding requires a valid Gmail thread ID')
        if item['thread_id'] not in scope:
            raise Refused('Finding is outside collected scope')
        if item.get('thread_id') in seen:
            raise Refused('One final finding per thread')
        seen.add(item.get('thread_id'))
        check_finding(item, read(Path(bundle) / 'details' / f'{item.get("thread_id")}.json'),
                      require_fresh=require_fresh)
    return data


def safe_url(url):
    if re.search(r'\s', str(url)):
        return ''
    try:
        parsed = urlsplit(str(url))
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return ''
        if parsed.port not in (None, 443):
            return ''
        # Drop query and fragment so secret-bearing run/share URLs never reach the report.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    except ValueError:
        return ''


def render(args):
    bundle = Path(args.bundle)
    index = read(bundle / 'index.json')
    items = findings(bundle, args.findings, require_fresh=False)
    expired = evidence_expired(index) or any(evidence_expired(
        read(bundle / 'details' / f'{item["thread_id"]}.json')) for item in items)
    freshness_note = '<p><strong>Refresh evidence before acting.</strong> This report contains evidence older than four hours.</p>' if expired else ''
    rank = {'today': 0, 'soon': 1, 'waiting': 2}
    rows = []
    for item in sorted(items, key=lambda i: rank[i['priority']]):
        if not item['actionable']:
            continue
        tid = item['thread_id']
        entry = next((t for t in index['threads'] if t['thread_id'] == tid), None)
        if entry is None:
            raise Refused('Report finding is outside collected scope')
        correction_path = bundle / 'corrections' / f'{tid}.json'
        correction = read(correction_path) if correction_path.exists() else {}
        corrected = ('<small>Draft corrected · readback verified at '
                     + html.escape(correction.get('at', '')) + '</small>'
                     if correction.get('status') == 'corrected' else '')
        sources = []
        for source in item['sources']:
            url = safe_url(source['location'])
            location = f'<a href="{html.escape(url, quote=True)}">source</a>' if url else (
                html.escape(safe_text(source['location'], 180)))
            sources.append(f'<li>{html.escape(safe_text(source["fact"], 360))} ({location})</li>')
        rows.append(f'<tr><td><a href="{html.escape(gmail_url(index["account"], tid), quote=True)}">'
                    f'{html.escape(entry["subject"])}</a><small>{html.escape(item["priority"])}</small>'
                    f'</td><td>{corrected}<strong>{html.escape(safe_text(item["action"], 280))}</strong><br>'
                    f'{html.escape(safe_text(item["why"], 420))}<details><summary>Evidence</summary><ul>'
                    + ''.join(sources) + '</ul><p>Since run: '
                    + html.escape(safe_text(item['changed_since_run'], 360)) + '</p></details></td></tr>')
    primary_rows = rows[:PRIMARY_ACTION_ROWS]
    remaining_rows = rows[PRIMARY_ACTION_ROWS:]
    remaining = ('<details class="more-actions"><summary>'
                 + f'{len(remaining_rows)} more action' + ('s' if len(remaining_rows) != 1 else '')
                 + '</summary><table>' + ''.join(remaining_rows) + '</table></details>'
                 if remaining_rows else '')
    reviewed = {i['thread_id'] for i in items}
    unresolved = sum(t['state'] not in ('answered', 'no_unanswered_inbound') and
                     t['thread_id'] not in reviewed for t in index['threads'])
    c = index['coverage']
    report = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
              'content="width=device-width,initial-scale=1"><title>Mailbox today</title><style>'
              'body{font:16px/1.45 system-ui;color:#18232c;max-width:1000px;margin:40px auto;padding:0 20px}'
              'h1{font-size:28px;margin-bottom:4px}p,small{color:#56616a}table{width:100%;border-collapse:collapse}'
              'td{vertical-align:top;border-top:1px solid #dce2e7;padding:14px 0}td:first-child{width:34%;'
              'padding-right:24px}a{color:#155c9b}small{display:block}details{font-size:14px;margin-top:8px}'
              '@media print{body{font-size:11px;margin:12px}details{display:none}}</style><h1>Mailbox today</h1><p>'
              + html.escape(index['account']) + ' · ' + html.escape(index['collected_at'][:10]) + '</p>'
              + freshness_note + '<table>'
              + (''.join(primary_rows) or '<tr><td>No reviewed actions to surface.</td></tr>')
              + '</table>' + remaining + '<p>'
              + (f'Recent inbox {c["recent_inbox_checked"]}/{c["recent_inbox_discovered"]} checked · '
                 f'older draft threads {c["older_draft_threads_checked"]}/{c["older_draft_threads_discovered"]} checked · '
                 if 'recent_inbox_checked' in c else f'{c["checked_threads"]} threads checked · ')
              + f'{c["uncovered_threads"]} discovered threads uncovered'
              + ('; additional unlisted threads unknown (pagination capped)' if c['list_truncated'] else '')
              + f' · {unresolved} unresolved threads not reviewed · {c["standalone_drafts_excluded"]} standalone drafts excluded.'
              + '</p></html>')
    output = Path(args.out).expanduser()
    output.write_text(report)
    output.chmod(0o600)
    print(json.dumps({'report': str(output), 'action_rows': len(rows), 'unreviewed': unresolved}))


def apply(args):
    if not args.allow_draft_corrections:
        raise Refused('Draft correction requires explicit --allow-draft-corrections authorization')
    bundle = Path(args.bundle)
    items = findings(bundle, args.findings)
    item = next((i for i in items if i['thread_id'] == args.thread_id), None)
    if not item or 'replacement_body' not in item:
        raise Refused('No validated correction for this thread')
    baseline = read(bundle / 'details' / f'{args.thread_id}.json')
    old = matched_draft(baseline, item['draft_id'])
    client = Client(read(bundle / 'config.json'))
    account = verify_mailbox(client)
    current_run = latest_run(google_evidence(client.rc('run', 'thread', args.thread_id), args.thread_id))
    shown = client.rc('run', 'show', current_run['run_id']) if current_run else None
    if not shown or shown['run_id'] != item['run_id'] or (
            shown.get('draft_markdown') != baseline['run'].get('draft_markdown')):
        raise Refused('ReplyPen run changed since the review')
    stubs, truncated = client.pages('drafts', 'drafts')
    if truncated:
        raise Refused('Current draft listing is incomplete')
    current_drafts = [client.api('drafts/' + d['id'], format='full') for d in stubs
                      if d.get('message', {}).get('threadId') == args.thread_id]
    current = matched_draft({**baseline, 'run': shown, 'drafts': current_drafts}, item['draft_id'])
    if current['message'].get('id') != old['message'].get('id') or digest(current) != digest(old):
        raise Refused('Draft changed after review; refresh instead of overwriting')
    thread = client.gws('gmail-thread', args.thread_id)
    guard_thread(thread, baseline, account)
    audit = bundle / 'corrections' / f'{args.thread_id}.json'
    original = {'draft': old, 'reason': item['why'], 'sources': item['sources'], 'run_id': item['run_id'],
                'sha256_before': hashlib.sha256(plain_body(old['message']).encode()).hexdigest(),
                'message_id_before': old['message']['id'], 'status': 'prepared', 'at': now()}
    save(audit, original)
    replacement = audit.with_suffix('.txt')
    replacement.write_text(item['replacement_body'])
    replacement.chmod(0o600)
    # Recheck immediately before the update; Gmail has no atomic thread+draft CAS.
    guard_thread(client.gws('gmail-thread', args.thread_id), baseline, account)
    save(audit, {**original, 'status': 'write_attempted', 'at': now()})
    result = client.gws('gmail-draft-update', item['draft_id'], '--body-file', str(replacement),
                        '--expect-message', old['message']['id'])
    back = client.api('drafts/' + item['draft_id'], format='full')
    if back['message'].get('threadId') != args.thread_id or plain_body(back['message']) != (
            item['replacement_body']):
        save(audit, {**original, 'status': 'written_readback_mismatch'})
        raise Refused('Draft was written but readback mismatched; inspect Gmail immediately')
    save(audit, {**original, 'status': 'corrected', 'result': result,
                 'message_id_after': back['message']['id'], 'at': now()})
    print(json.dumps({'corrected_thread': args.thread_id, 'audit': str(audit)}))


def guard_thread(thread, baseline, account):
    if thread.get('id') != baseline['gmail']['id'] or fingerprint(thread) != baseline['fingerprint']:
        raise Refused('Gmail thread changed since review')
    state = mail_state(thread, account)
    if not state['unanswered'] or state['ambiguous_outbound'] or state['ambiguous_sender']:
        raise Refused('Thread is answered or message authorship is uncertain')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    c = subs.add_parser('collect')
    for name in ('project', 'mailbox-id', 'gws-script', 'gws-config', 'out'):
        c.add_argument('--' + name, required=True)
    c.add_argument('--days', type=int, default=7)
    c.add_argument('--max-threads', type=int, default=60)
    c.add_argument('--max-pages', type=int, default=20)
    c.set_defaults(func=collect)
    for name, func in (('detail', detail), ('validate', None), ('render', render), ('apply', apply)):
        p = subs.add_parser(name)
        p.add_argument('--bundle', required=True)
        if name in ('detail', 'apply'):
            p.add_argument('--thread-id', required=True)
        if name != 'detail':
            p.add_argument('--findings', required=True)
        if name == 'render':
            p.add_argument('--out', required=True)
        if name == 'apply':
            p.add_argument('--allow-draft-corrections', action='store_true')
        p.set_defaults(func=func)
    args = parser.parse_args()
    if args.command == 'collect' and not (args.days > 0 and args.max_threads > 0 and args.max_pages > 0):
        parser.error('Collection bounds must be positive')
    try:
        if args.command == 'validate':
            print(json.dumps({'validated_findings': len(findings(args.bundle, args.findings))}))
        else:
            args.func(args)
    except (Refused, FileNotFoundError, KeyError, ValueError) as exc:
        # User-controlled JSON/remote values must never be interpolated in diagnostics.
        print(json.dumps({'refused': str(exc) if isinstance(exc, Refused) else
                          'Missing or malformed local evidence; recollect and review'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
