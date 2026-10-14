"""Queue lifecycle against an actual migrated make-dev Postgres; every test rolls back.

FLEET_TEST_DSN=postgres://... uv run --with pydantic --with 'psycopg[binary]' \
  --with pytest --no-project pytest skills/brain-fleet-report/tests/test_publish.py -q
"""
import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from prior import prior_table, read_prior
from publish import publish
from queue_db import require_schema
from report_schema import Report


@pytest.fixture
def db():
    dsn = os.environ.get('FLEET_TEST_DSN')
    if not dsn:
        pytest.skip('Set FLEET_TEST_DSN to a local make-dev database with Package A applied')
    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        require_schema(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def projects(db):
    names = ['fleet-test-' + uuid4().hex[:12] for _ in range(2)]
    ids = {}
    for name in names:
        ids[name] = db.execute(
            'INSERT INTO projects(name,webhook_secret) VALUES (%s,%s) RETURNING id',
            (name, 'test-only'),
        ).fetchone()['id']
        db.execute('INSERT INTO tenants(project_id,slug) VALUES (%s,%s)', (ids[name], 'practice'))
    return ids


def report(projects, *, audience='technical', day='2026-09-04'):
    data = json.loads((ROOT / 'fixtures/sample_report.json').read_text())
    finding = copy.deepcopy(data['findings'][0])
    finding.update(members=list(projects), audience=audience, evidence={'run_ids': [], 'run_urls': []})
    data.update(findings=[finding], date=day)
    data['coverage']['projects'] = list(projects)
    return Report.model_validate(data)


def rows(db, projects):
    return db.execute('''SELECT i.*,s.audience,s.project_id,s.tenant_id,s.cadence
      FROM review_items i JOIN review_sessions s ON s.id=i.session_id
      WHERE s.project_id=ANY(%s::uuid[]) ORDER BY s.audience,i.position''',
      (list(projects.values()),)).fetchall()


def next_day(r, *, changed=False):
    r = r.model_copy(deep=True)
    r.date = '2026-09-05'
    r.findings[0].status = 'changed' if changed else 'unchanged'
    r.findings[0].update_en = 'Still reproducible in the next observation window.'
    r.findings[0].update_nl = 'Nog steeds zichtbaar in het volgende venster.'
    return r


def test_preview_has_no_session_or_item_writes(db, projects):
    plan = publish(db, report(projects), write=False)
    assert {p['action'] for p in plan} == {'insert'}
    assert not rows(db, projects)
    assert db.execute('SELECT count(*) AS n FROM review_sessions WHERE project_id=ANY(%s::uuid[])',
                      (list(projects.values()),)).fetchone()['n'] == 0


def test_both_members_have_own_project_sessions_and_linked_tenant_cards(db, projects):
    r = report(projects, audience='both')
    r.findings[0].scope.level = 'tenant'
    r.findings[0].scope.tenant = 'practice'
    publish(db, r, write=True)
    cards = rows(db, projects)
    assert len(cards) == 4
    for project_id in projects.values():
        owner, technical = [c for c in cards if c['project_id'] == project_id]
        assert technical['linked_item_id'] == owner['id']
        assert owner['options'][0]['label'] == 'Gegevens invullen'
        assert technical['options'][0]['instruction'] == r.findings[0].prompt.change
        assert len(technical['options']) == 5
        assert technical['prompt']['text']
        assert owner['tenant_id'] == db.execute('SELECT id FROM tenants WHERE project_id=%s AND slug=%s', (project_id, 'practice')).fetchone()['id']
        assert technical['tenant_id'] is None
        assert owner['subject'] == r.findings[0].title_nl
        assert technical['subject'] == r.findings[0].title
    assert all(c['scope_label'] == 'practice' and c['cadence'] == 'queue' for c in cards)
    publish(db, r, write=True)
    assert rows(db, projects) == cards
    history = read_prior(db, list(projects))[r.findings[0].signature]
    assert len(history['rows']) == 4
    assert history['finding']['title_nl'] == r.findings[0].title_nl
    assert history['finding']['title'] == r.findings[0].title


def test_same_day_idempotent_next_day_unchanged_preserves_task(db, projects):
    r = report(projects)
    publish(db, r, write=True)
    first = rows(db, projects)
    publish(db, r, write=True)
    assert rows(db, projects) == first
    carried = next_day(r)
    carried.findings[0].prompt = None
    carried.findings[0].text_en = None
    publish(db, carried, write=True)
    cards = rows(db, projects)
    assert all(c['occurrences'] == 2 and c['last_seen'] == date(2026, 9, 5) for c in cards)
    assert [c['prompt'] for c in cards] == [c['prompt'] for c in first]
    assert [c['body'] for c in cards] == [c['body'] for c in first]
    publish(db, carried, write=True)
    assert rows(db, projects) == cards
    prior = read_prior(db, list(projects))
    assert prior[r.findings[0].signature]['finding']['prompt']['change'] == r.findings[0].prompt.change


def test_changed_updates_open_but_decided_task_is_frozen(db, projects):
    r = report(projects)
    publish(db, r, write=True)
    first = rows(db, projects)
    db.execute("UPDATE review_items SET status='decided',decision_label='Do it' WHERE id=%s", (first[0]['id'],))
    changed = next_day(r, changed=True)
    changed.findings[0].text_en = 'Newly grounded body'
    changed.findings[0].prompt.change = 'A different bounded implementation task'
    publish(db, changed, write=True)
    cards = {c['id']: c for c in rows(db, projects)}
    assert cards[first[0]['id']]['prompt'] == first[0]['prompt']
    assert cards[first[0]['id']]['body'] == first[0]['body']
    assert cards[first[1]['id']]['body'] == 'Newly grounded body'
    assert cards[first[1]['id']]['prompt']['change'] == changed.findings[0].prompt.change
    assert all(c['updates'][-1]['text'] == changed.findings[0].update_en for c in cards.values())


def test_applied_recurrence_requires_new_evidence_and_preserves_receipt(db, projects):
    name, project_id = next(iter(projects.items()))
    r = report({name: project_id})
    publish(db, r, write=True)
    old = rows(db, projects)[0]
    applied_at = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    receipt = {'disposition': 'done', 'rounds': [{'commit': 'abc123', 'shipped': True}]}
    db.execute("UPDATE review_items SET status='applied',applied=%s,applied_at=%s WHERE id=%s",
               (Jsonb(receipt), applied_at, old['id']))
    changed = next_day(r, changed=True)
    assert publish(db, changed, write=True)[0]['action'] == 'skip'
    run = db.execute('''INSERT INTO runs(project_id,thread_id,session_id,created_at)
      VALUES (%s,%s,%s,%s) RETURNING id''',
      (project_id, str(uuid4()), str(uuid4()), datetime(2026, 9, 5, tzinfo=timezone.utc))).fetchone()['id']
    changed.findings[0].evidence.run_ids = [str(run)]
    assert publish(db, changed, write=True)[0]['action'] == 'regression'
    history, current = rows(db, projects)
    assert history['signature'].startswith(old['signature'] + '#archived:')
    assert history['applied'] == receipt and history['status'] == 'applied'
    assert current['signature'] == old['signature'] and current['kind'] == 'regression'
    assert 'seen again after fix abc123' in current['updates'][0]['text']
    prior = read_prior(db, [name])[old['signature']]
    assert len(prior['rows']) == 2
    assert prior['finding']['status'] == 'open'


@pytest.mark.parametrize('disposition', ['accepted', 'noise'])
def test_closed_requires_judge_retest_assertion(db, projects, disposition):
    r = report(projects)
    publish(db, r, write=True)
    db.execute('''UPDATE review_items SET status='closed',applied=%s,
      decision_label=%s,decision_instruction='Retest when the upstream capability ships'
      WHERE session_id IN (SELECT id FROM review_sessions WHERE project_id=ANY(%s::uuid[]))''',
      (Jsonb({'disposition': disposition}), disposition, list(projects.values())))
    assert all(p['action'] == 'skip' for p in publish(db, next_day(r), write=True))
    changed = next_day(r, changed=True)
    changed.findings[0].update_en = 'The upstream capability shipped; the retest trigger is met.'
    assert 'Retest when the upstream capability ships' in prior_table(read_prior(db, list(projects)))
    assert all(p['action'] == 'reopen' for p in publish(db, changed, write=True))
    assert len(rows(db, projects)) == 4


def test_later_reopens_only_after_date(db, projects):
    r = report(projects)
    publish(db, r, write=True)
    db.execute('''UPDATE review_items SET status='closed',applied=%s,decision_instruction='After 2026-09-05'
      WHERE session_id IN (SELECT id FROM review_sessions WHERE project_id=ANY(%s::uuid[]))''',
      (Jsonb({'disposition': 'later'}), list(projects.values())))
    changed = next_day(r, changed=True)
    assert all(p['action'] == 'skip' for p in publish(db, changed, write=True))
    changed.date = '2026-09-06'
    assert all(p['action'] == 'reopen' for p in publish(db, changed, write=True))


def test_invalid_member_rolls_back_whole_publication(db, projects):
    r = report(projects)
    bad = r.findings[0].model_copy(deep=True)
    bad.signature = 'invalid-project'
    bad.members = ['does-not-exist-' + uuid4().hex]
    r.findings.append(bad)
    with pytest.raises(ValueError, match='valid member projects'):
        with db.transaction():
            publish(db, r, write=True)
    assert not rows(db, projects)
    assert db.execute('SELECT count(*) AS n FROM review_sessions WHERE project_id=ANY(%s::uuid[])',
                      (list(projects.values()),)).fetchone()['n'] == 0


def test_ledger_parse_and_idempotent_import_preserve_decisions(db, projects, tmp_path):
    from ledger_import import import_rows, parse_ledger

    ledger = tmp_path / 'ledger.md'
    ledger.write_text('''| Since | Pattern | Disposition | Re-test when |
|---|---|---|---|
| 2026-09-01 | `script_error:wrong-column` | open | after schema change |
| 2026-09-02 | historic fix | fix-landed in abc123 | new occurrence |
| 2026-09-03 | accepted limitation | **accepted** | capability ships |
| 2026-09-03 | vendor mail | **noise** | sender changes |
''')
    parsed = parse_ledger(ledger)
    assert [(r['status'], r['outcome']) for r in parsed] == [
        ('open', None), ('applied', 'done'), ('closed', 'accepted'), ('closed', 'noise')]
    assert parsed[0]['signature'] == 'script_error:wrong-column'
    name = next(iter(projects))
    assert all(r['action'] == 'would insert' for r in import_rows(db, name, parsed))
    assert not rows(db, projects)
    assert all(r['action'] == 'insert' for r in import_rows(db, name, parsed, write=True))
    cards = rows(db, projects)
    assert cards[1]['applied_at'] is not None
    assert cards[2]['decision_instruction'] == 'capability ships'
    db.execute("UPDATE review_items SET status='decided',decision_label='Do it' WHERE id=%s", (cards[0]['id'],))
    before = rows(db, projects)
    assert all(r['action'] == 'preserved' for r in import_rows(db, name, parsed, write=True))
    assert rows(db, projects) == before


def test_regression_marker_survives_later_changed_report(db, projects):
    r = report(projects)
    publish(db, r, write=True)
    item = rows(db, projects)[0]
    db.execute("UPDATE review_items SET kind='regression' WHERE id=%s", (item['id'],))
    publish(db, next_day(r, changed=True), write=True)
    assert db.execute('SELECT kind FROM review_items WHERE id=%s', (item['id'],)).fetchone()['kind'] == 'regression'


def test_frozen_technical_twin_follows_reopened_owner(db, projects):
    r = report(projects, audience='both')
    publish(db, r, write=True)
    original = rows(db, projects)
    for item in original:
        if item['audience'] == 'owner':
            db.execute("UPDATE review_items SET status='closed',applied=%s,decision_instruction='new evidence' WHERE id=%s",
                       (Jsonb({'disposition': 'accepted'}), item['id']))
        else:
            db.execute("UPDATE review_items SET status='decided' WHERE id=%s", (item['id'],))
    publish(db, next_day(r, changed=True), write=True)
    all_rows = rows(db, projects)
    for technical in (row for row in all_rows if row['audience'] == 'technical'):
        owner = next(row for row in all_rows if row['id'] == technical['linked_item_id'])
        assert owner['status'] == 'open'
        assert owner['signature'] == r.findings[0].signature
        assert technical['status'] == 'decided'


@pytest.mark.parametrize('status', ['open', 'decided'])
def test_legacy_owner_moves_on_next_sighting_preserving_identity(db, projects, tmp_path, status):
    from report_schema import load_report
    r = report(projects, audience='both')
    r.findings[0].scope.level = 'tenant'
    r.findings[0].scope.tenant = 'practice'
    r.kpis.per_axis = []  # Report helper changes scope after validation; add the real axis below.
    publish(db, r, write=True)
    # Reproduce old releases: owner card has tenant scope_label but a project queue session.
    for owner in (c for c in rows(db, projects) if c['audience'] == 'owner'):
        session = db.execute("SELECT id FROM review_sessions WHERE project_id=%s AND audience='owner' AND tenant_id IS NULL", (owner['project_id'],)).fetchone()['id']
        db.execute("UPDATE review_items SET session_id=%s,status=%s,decision_instruction='Existing choice' WHERE id=%s", (session, status, owner['id']))
    before = rows(db, projects)
    carried = next_day(r)
    data = carried.model_dump(exclude_unset=True)
    data['window']['focus'] = carried.date
    data['kpis']['per_axis'] = [{'axis': 'tenant', 'key': 'practice', 'name': 'Practice'}]
    for key in ('text_en', 'text_nl', 'ask_nl', 'ask_for', 'prompt', 'impact', 'root_cause'):
        data['findings'][0].pop(key, None)
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(data))
    carried = load_report(path, prior=read_prior(db, list(projects)))
    assert all(p['action'] == 'update' for p in publish(db, carried, write=False))
    assert rows(db, projects) == before
    publish(db, carried, write=True)
    after = rows(db, projects)
    assert {c['id'] for c in after} == {c['id'] for c in before}
    for card in after:
        old = next(c for c in before if c['id'] == card['id'])
        assert card['status'] == old['status']
        assert card['decision_instruction'] == old['decision_instruction']
        assert card['linked_item_id'] == old['linked_item_id']
        assert (card['tenant_id'] is not None) == (card['audience'] == 'owner')


def test_project_scoped_owner_card_moves_to_tenant_on_tenant_sighting(db, projects, tmp_path):
    from report_schema import load_report
    r = report(projects, audience='owner')
    publish(db, r, write=True)
    before = rows(db, projects)
    assert {c['scope_label'] for c in before} == {'project'} and all(c['tenant_id'] is None for c in before)
    carried = next_day(r)
    data = carried.model_dump(exclude_unset=True)
    data['window']['focus'] = carried.date
    data['findings'][0]['scope'] = {'level': 'tenant', 'tenant': 'practice'}
    data['kpis']['per_axis'] = [{'axis': 'tenant', 'key': 'practice', 'name': 'Practice'}]
    for key in ('text_en', 'text_nl', 'ask_nl', 'ask_for', 'prompt', 'impact', 'root_cause'):
        data['findings'][0].pop(key, None)
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(data))
    carried = load_report(path, prior=read_prior(db, list(projects)))
    assert all(p['action'] == 'update' for p in publish(db, carried, write=True))
    after = rows(db, projects)
    assert {c['id'] for c in after} == {c['id'] for c in before}
    assert all(c['tenant_id'] is not None and c['scope_label'] == 'practice' for c in after)

def test_prototype_open_card_repair_preserves_decisions(db, projects):
    from body_update import update_body
    from test_helper_prototype import sample
    r = report(projects)
    r.findings[0].prototype = sample()
    publish(db, r, write=True)
    row = rows(db, projects)[0]
    assert row['options'][0]['label'] == 'Merge branch'
    project = next(name for name, pid in projects.items() if pid == row['project_id'])
    original = row['body']
    update_body(db, row['id'], project, sample())
    assert db.execute('SELECT body FROM review_items WHERE id=%s', (row['id'],)).fetchone()['body'] == original
    first = update_body(db, row['id'], project, sample(), write=True)
    assert first['body'].count('## Before') == 1
    assert update_body(db, row['id'], project, sample(), write=True) == first
    db.execute("UPDATE review_items SET status='decided',decision_label='Later' WHERE id=%s", (row['id'],))
    with pytest.raises(ValueError, match='open technical'):
        update_body(db, row['id'], project, sample(), write=True)
