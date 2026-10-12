"""Offline invariants; fixtures contain no customer mail."""
import argparse
import base64
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cockpit', Path(__file__).with_name('cockpit.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
ACCOUNT = 'owner@example.test'


def message(mid, sender='customer@example.test', to=ACCOUNT, sent=False, draft=False, at=1, text='body'):
    return {'id': mid, 'threadId': 'abc', 'internalDate': str(at),
            'labelIds': ['DRAFT'] if draft else ['SENT'] if sent else ['INBOX'],
            'payload': {'mimeType': 'text/plain', 'headers': [
                {'name': 'From', 'value': sender}, {'name': 'To', 'value': to},
                {'name': 'Subject', 'value': '<script>customer</script>'}],
                'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}}


def fixture():
    thread = {'id': 'abc', 'messages': [message('incoming'),
              message('draft', ACCOUNT, 'customer@example.test', draft=True, at=2, text='AI body')]}
    detail = {'gmail': thread, 'fingerprint': c.fingerprint(thread), 'collected_at': c.now(),
              'run': {'run_id': 'run', 'draft_markdown': 'AI body'},
              'drafts': [{'id': 'gmail-draft', 'message': thread['messages'][1]}]}
    finding = {'thread_id': 'abc', 'run_id': 'run', 'draft_id': 'gmail-draft',
               'evidence_fingerprint': detail['fingerprint'], 'priority': 'today',
               'verdict': 'incorrect', 'actionable': True, 'action': 'Send the verified reply',
               'why': '<img src=x onerror=evil()>', 'changed_since_run': 'No changes',
               'sources': [{'kind': 'code', 'location': 'https://example.test/check?token=secret',
                            'fact': 'Independent fact'}], 'replacement_body': 'Corrected body'}
    return detail, finding


class Tests(unittest.TestCase):
    def test_mailbox_mismatch_and_same_email_intercom(self):
        class Fake:
            config = {'mailbox_id': 'one'}
            def gws(self, *args):
                return {'email': ACCOUNT}
            def rc(self, *args):
                return {'mailboxes': [{'id': 'one', 'provider': 'intercom', 'email_address': ACCOUNT}]}
        with self.assertRaises(c.Refused):
            c.verify_mailbox(Fake())
        fake = Fake()
        fake.rc = lambda *a: {'mailboxes': [{'id': 'one', 'provider': 'google',
                                            'email_address': 'other@example.test'}]}
        with self.assertRaises(c.Refused):
            c.verify_mailbox(fake)

    def test_note_not_reply_and_automation_uncertain(self):
        thread = {'id': 'abc', 'messages': [message('in'), message('note', ACCOUNT, sent=True, at=2)]}
        self.assertTrue(c.mail_state(thread, ACCOUNT)['unanswered'])
        self.assertFalse(c.mail_state(thread, ACCOUNT)['answered'])
        reply = message('reply', ACCOUNT, 'customer@example.test', sent=True, at=3)
        thread['messages'].append(reply)
        self.assertTrue(c.mail_state(thread, ACCOUNT)['answered'])
        reply['payload']['headers'].append({'name': 'X-AC-Message-ID', 'value': 'automation'})
        self.assertTrue(c.mail_state(thread, ACCOUNT)['ambiguous_outbound'])
        self.assertTrue(c.mail_state(thread, ACCOUNT)['unanswered'])

    def test_outlook_reply_answered_but_spoofed_self_inbound_surfaces(self):
        reply = message('reply', ACCOUNT, 'customer@example.test', sent=True, at=3)
        reply['payload']['headers'].append({'name': 'X-Mailer', 'value': 'Microsoft Outlook 16.0'})
        thread = {'id': 'abc', 'messages': [message('in'), reply]}
        self.assertTrue(c.mail_state(thread, ACCOUNT)['answered'])
        self.assertFalse(c.mail_state(thread, ACCOUNT)['ambiguous_outbound'])
        spoofed = {'id': 'abc', 'messages': [message('spoof', ACCOUNT, ACCOUNT)]}
        state = c.mail_state(spoofed, ACCOUNT)
        self.assertTrue(state['unanswered'])
        self.assertTrue(state['ambiguous_sender'])
        self.assertEqual(state['notes'], [])
        self.assertEqual(c.summary(spoofed, {}, ACCOUNT, [])['state'], 'uncertain_sender')

    def test_google_provider_filter(self):
        data = {'threads': [{'provider': 'google', 'external_thread_id': 'abc', 'local_thread_id': 'g'},
                            {'provider': 'intercom', 'external_thread_id': 'abc', 'local_thread_id': 'i'}],
                'runs': [{'run_id': name, 'attribution': {'thread_id': 'abc', 'local_thread_id': local}}
                         for name, local in [('gmail', 'g'), ('intercom', 'i')]]}
        self.assertEqual([r['run_id'] for r in c.google_evidence(data, 'abc')['runs']], ['gmail'])

    def test_human_edit_or_multiple_match_refused(self):
        detail, _ = fixture()
        self.assertEqual(c.matched_draft(detail, 'gmail-draft')['id'], 'gmail-draft')
        detail['drafts'][0]['message'] = message('changed', draft=True, text='AI body\n')
        with self.assertRaises(c.Refused):
            c.matched_draft(detail, 'gmail-draft')
        detail, _ = fixture()
        detail['drafts'].append({'id': 'second', 'message': detail['drafts'][0]['message']})
        with self.assertRaises(c.Refused):
            c.matched_draft(detail, 'gmail-draft')

    def test_thread_change_or_new_reply_refused(self):
        detail, _ = fixture()
        c.guard_thread(detail['gmail'], detail, ACCOUNT)
        changed = copy.deepcopy(detail['gmail'])
        changed['messages'].append(message('reply', ACCOUNT, 'customer@example.test', sent=True, at=3))
        with self.assertRaises(c.Refused):
            c.guard_thread(changed, detail, ACCOUNT)
        detail['gmail'] = changed
        detail['fingerprint'] = c.fingerprint(changed)
        with self.assertRaises(c.Refused):
            c.guard_thread(changed, detail, ACCOUNT)

    def test_findings_freshness_run_identity_and_quotes(self):
        detail, finding = fixture()
        c.check_finding(finding, detail)
        detail['collected_at'] = '2020-01-01T00:00:00+00:00'
        with self.assertRaises(c.Refused):
            c.check_finding(finding, detail)
        detail, finding = fixture()
        finding['run_id'] = 'different'
        with self.assertRaises(c.Refused):
            c.check_finding(finding, detail)
        detail, finding = fixture()
        body = 'AI body\nOn yesterday customer wrote:\n> Keep me'
        detail['run']['draft_markdown'] = body
        detail['drafts'][0]['message'] = message('draft', draft=True, text=body)
        with self.assertRaises(c.Refused):
            c.check_finding(finding, detail)

    def test_pagination_and_raw_cli(self):
        client = c.Client({'gws_script': 'core.py', 'gws_config': 'config.json', 'project': 'test'})
        with patch.object(client, 'api', side_effect=[{'drafts': [{'id': '1'}], 'nextPageToken': 'next'},
                                                    {'drafts': [{'id': '2'}]}]) as api:
            items, truncated = client.pages('drafts', 'drafts')
            self.assertEqual(len(items), 2)
            self.assertFalse(truncated)
            self.assertEqual(api.call_args.kwargs['pageToken'], 'next')
        with patch.object(c, 'cli_json', return_value={}) as run:
            client.rc('run', 'thread', 'abc')
            self.assertIn('--raw-output', run.call_args.args[0])

    def test_old_unanswered_draft_included_and_cap_explicit(self):
        class Fake:
            def __init__(self, config):
                self.config = config
            def pages(self, path, key, **params):
                if path == 'threads':
                    return [{'id': i} for i in ('a', 'b', 'c', 'd')], False
                return [{'id': 'old-draft', 'message': {'id': 'not-thread', 'threadId': 'e'}}], False
            def gws(self, *args):
                if args[0] == 'whoami':
                    return {'email': ACCOUNT}
                return {'id': args[1], 'messages': [message('in')]}
            def rc(self, *args):
                if args[:3] == ('project', 'mailbox', 'ls'):
                    return {'mailboxes': [{'id': 'one', 'provider': 'google', 'email_address': ACCOUNT}]}
                return {'runs': [], 'threads': []}
        with tempfile.TemporaryDirectory() as temp, patch.object(c, 'Client', Fake):
            args = argparse.Namespace(out=temp, project='test', mailbox_id='one', gws_script='gws.py',
                                      gws_config='config.json', max_pages=20, days=7, max_threads=3)
            c.collect(args)
            index = c.read(Path(temp) / 'index.json')
            self.assertIn('e', [t['thread_id'] for t in index['threads']])
            self.assertEqual(index['coverage']['uncovered_threads'], 2)
            self.assertEqual(index['coverage']['recent_inbox_checked'], 2)
            self.assertEqual(index['coverage']['older_draft_threads_checked'], 1)
            self.assertEqual(index['coverage']['recent_inbox_discovered'], 4)
            self.assertEqual(index['threads'][2]['state'], 'not_processed')

    def test_html_escaping_secret_links_and_confirmed_action(self):
        detail, finding = fixture()
        del finding['replacement_body']
        finding['verdict'] = 'confirmed'
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp)
            c.save(bundle / 'details/abc.json', detail)
            c.save(bundle / 'findings.json', [finding])
            c.save(bundle / 'index.json', {'account': ACCOUNT, 'collected_at': c.now(), 'threads': [
                {'thread_id': 'abc', 'subject': '<script>subject</script>', 'state': 'draft_ready'}],
                'coverage': {'checked_threads': 1, 'uncovered_threads': 2, 'list_truncated': True,
                             'standalone_drafts_excluded': 4}})
            c.render(argparse.Namespace(bundle=temp, findings=str(bundle / 'findings.json'),
                                        out=str(bundle / 'report.html')))
            report = (bundle / 'report.html').read_text()
            self.assertIn('Send the verified reply', report)
            self.assertIn('&lt;script&gt;', report)
            self.assertNotIn('<script>', report)
            self.assertNotIn('token=secret', report)
            self.assertIn('unlisted threads unknown', report)
            detail['collected_at'] = '2020-01-01T00:00:00+00:00'
            c.save(bundle / 'details/abc.json', detail)
            c.render(argparse.Namespace(bundle=temp, findings=str(bundle / 'findings.json'),
                                        out=str(bundle / 'report.html')))
            self.assertIn('Refresh evidence before acting.', (bundle / 'report.html').read_text())
            with self.assertRaises(c.Refused):
                c.findings(bundle, bundle / 'findings.json')
            c.save(bundle / 'index.json', {'threads': []})
            with self.assertRaises(c.Refused):
                c.findings(bundle, bundle / 'findings.json', require_fresh=False)
        self.assertEqual(c.safe_url('https://example.test/reviews (fetch blocked); another source'), '')
        self.assertEqual(c.safe_url('https://example.test/reviews'), 'https://example.test/reviews')
        self.assertEqual(c.safe_url('javascript:alert(1)'), '')
        self.assertEqual(c.safe_url('https://example.test:bad/'), '')

    def test_guarded_apply_reads_back_and_audits_without_send(self):
        detail, finding = fixture()
        calls = []
        class Fake:
            def __init__(self, config):
                self.config = config
                self.updated = False
            def rc(self, *args):
                if args[:3] == ('project', 'mailbox', 'ls'):
                    return {'mailboxes': [{'id': 'one', 'provider': 'google', 'email_address': ACCOUNT}]}
                if args[:2] == ('run', 'show'):
                    return detail['run']
                return {'threads': [{'provider': 'google', 'external_thread_id': 'abc', 'local_thread_id': 'g'}],
                        'runs': [{'run_id': 'run', 'attribution': {'thread_id': 'abc', 'local_thread_id': 'g'}}]}
            def pages(self, *args):
                return [{'id': 'gmail-draft', 'message': {'threadId': 'abc'}}], False
            def api(self, *args, **kwargs):
                draft = copy.deepcopy(detail['drafts'][0])
                if self.updated:
                    draft['message'] = message('after', ACCOUNT, 'customer@example.test', draft=True,
                                                text='Corrected body')
                return draft
            def gws(self, *args):
                calls.append(args)
                if args[0] == 'whoami':
                    return {'email': ACCOUNT}
                if args[0] == 'gmail-thread':
                    return detail['gmail']
                self.updated = True
                return {'verified': {'threadId': 'abc'}}
        with tempfile.TemporaryDirectory() as temp, patch.object(c, 'Client', Fake):
            bundle = Path(temp)
            c.save(bundle / 'details/abc.json', detail)
            c.save(bundle / 'findings.json', [finding])
            c.save(bundle / 'config.json', {'mailbox_id': 'one', 'account': ACCOUNT})
            c.save(bundle / 'index.json', {'threads': [{'thread_id': 'abc'}]})
            c.apply(argparse.Namespace(bundle=temp, findings=str(bundle / 'findings.json'),
                                       thread_id='abc', allow_draft_corrections=True))
            audit = c.read(bundle / 'corrections/abc.json')
            self.assertEqual(audit['status'], 'corrected')
            self.assertEqual(audit['message_id_after'], 'after')
            self.assertEqual(c.plain_body(audit['draft']['message']), 'AI body')
        writes = [call for call in calls if call[0] == 'gmail-draft-update']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][-2:], ('--expect-message', 'draft'))
        self.assertNotIn('gmail-send', [call[0] for call in calls])

    def test_apply_requires_authorization_before_any_client(self):
        with patch.object(c, 'Client') as client:
            with self.assertRaises(c.Refused):
                c.apply(argparse.Namespace(allow_draft_corrections=False))
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
