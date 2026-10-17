"""Laptop operator connection; no credentials or host internals in the kit."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


@contextmanager
def connect(dsn=None, *, write=False):
    if dsn:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.read_only = not write
            require_schema(conn)
            yield conn
        return
    root = Path(os.environ.get('RC_HOST_CHECKOUT', '~/code/replypen/replypen')).expanduser()
    path = root / '.agents/skills/support/scripts/db.py'
    if not path.is_file():
        raise ValueError('Set RC_HOST_CHECKOUT to the operator host checkout, or pass --dsn for local testing')
    spec = importlib.util.spec_from_file_location('support_db', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.ssm_tunnel(module.PROFILE, module.REGION) as tunnel:
        with psycopg.connect(tunnel, row_factory=dict_row) as conn:
            conn.read_only = not write
            # This SELECT is always before the first mutation, including production --write.
            require_schema(conn)
            yield conn


def require_schema(conn):
    expected = {
        'review_sessions': {'audience', 'cadence'},
        'review_items': {'signature', 'status', 'applied', 'applied_at', 'linked_item_id',
                         'body', 'ask', 'ask_for', 'evidence', 'prompt', 'first_seen',
                         'last_seen', 'occurrences', 'updates', 'severity', 'plane', 'scope_label'},
    }
    rows = conn.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=ANY(%s)", (list(expected),)).fetchall()
    for table, columns in expected.items():
        missing = columns - {r['column_name'] for r in rows if r['table_name'] == table}
        if missing:
            raise ValueError(f'Package A is not live on this database: {table} missing {", ".join(sorted(missing))}; no writes performed')


def project_ids(conn, names):
    rows = conn.execute('SELECT id,name FROM projects WHERE name=ANY(%s)', (list(names),)).fetchall()
    result = {r['name']: r['id'] for r in rows}
    missing = set(names) - result.keys()
    if missing:
        raise ValueError('Unknown projects: ' + ', '.join(sorted(missing)))
    return result
