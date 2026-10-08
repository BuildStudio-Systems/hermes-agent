"""Portable JSON board snapshots and imports for the PostgreSQL backend."""
from contextlib import closing
import json
from pathlib import Path
import time
import sqlite3
import re

from hermes_cli.kanban_postgres import TABLES

MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
IDENTITY_TABLES = {'task_comments','task_events','task_runs','task_attachments'}


def columns(conn, table):
    if table not in TABLES:
        raise ValueError('Invalid board table')
    return [r[0] for r in conn.execute(
        'SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? AND column_name<>? ORDER BY ordinal_position',
        (conn.schema,table,'board_id'))]


def snapshot(conn):
    """One consistent snapshot; no source writes and no private board identity."""
    tables = {}
    conn._ensure_connection()
    if conn.in_transaction:
        raise RuntimeError('Export requires an independent read transaction')
    conn._transaction_started = True
    try:
        with conn._db.transaction():
            conn._db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            for table in TABLES:
                names = columns(conn,table)
                rows = conn.execute('SELECT '+','.join(names)+' FROM '+table+' LIMIT ?', (MAX_ROWS+1,)).fetchall()
                if len(rows) > MAX_ROWS:
                    raise ValueError('Board is too large for portable export')
                tables[table] = {'columns':names,'rows':[list(r) for r in rows]}
    finally:
        conn._transaction_started = False
    return {'format':'hermes-kanban-postgresql','version':1,'tables':tables}


def scrub(payload):
    """Same runtime-state scrub as legacy exports, before leaving the host."""
    now = int(time.time())
    tables = payload['tables']
    tables['kanban_notify_subs']['rows'] = []
    for table in ('tasks','task_runs'):
        names = tables[table]['columns']
        output = []
        for values in tables[table]['rows']:
            row = dict(zip(names,values))
            if table == 'tasks':
                for name in ('claim_lock','claim_expires','worker_pid','current_run_id',
                             'last_heartbeat_at','session_id','project_id','last_failure_error'):
                    row[name] = None
                row['consecutive_failures'] = 0
                if row['status'] == 'running':
                    row['status'] = 'ready'
            else:
                if row['status'] == 'running':
                    row['status'] = 'released'
                    row['outcome'] = row['outcome'] or 'reclaimed'
                    row['ended_at'] = row['ended_at'] if row['ended_at'] is not None else now
                    row['last_heartbeat_at'] = None
                row['claim_lock'] = row['worker_pid'] = None
            output.append([row[n] for n in names])
        tables[table]['rows'] = output
    events = tables['task_events']
    offset = events['columns'].index('payload')
    for row in events['rows']:
        if row[offset]:
            try:
                metadata = json.loads(row[offset])
            except (TypeError,ValueError):
                continue
            if isinstance(metadata,dict):
                for key in ('lock','expires','claim_lock','claim_expires','worker_pid','session_id','chat_id','thread_id','user_id','user_id_alt'):
                    metadata.pop(key,None)
                row[offset] = json.dumps(metadata,ensure_ascii=False)
    return payload


def write_snapshot(conn, path):
    payload = scrub(snapshot(conn))
    validate(conn,payload)
    encoded = json.dumps(payload,ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_BYTES:
        raise ValueError('Board exceeds portable export size limit')
    Path(path).write_bytes(encoded)
    return {t:len(v['rows']) for t,v in payload['tables'].items() if t != 'kanban_notify_subs'}


def read_snapshot(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Board exceeds portable import size limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('format') != 'hermes-kanban-postgresql' or value.get('version') != 1:
        raise ValueError('Unsupported PostgreSQL board snapshot')
    return value


def legacy_snapshot(conn, path):
    """Read an old portable/migration SQLite file without modifying it."""
    payload = {'format':'hermes-kanban-postgresql','version':1,'tables':{}}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
        source.execute('PRAGMA trusted_schema=OFF')
        source.execute('BEGIN')
        for table in TABLES:
            names = columns(conn,table)
            actual = [r[1] for r in source.execute('PRAGMA table_info('+table+')')]
            if set(actual) != set(names):
                raise ValueError('Legacy board needs a schema upgrade before import: '+table)
            rows = source.execute('SELECT '+','.join(names)+' FROM '+table+' LIMIT ?', (MAX_ROWS+1,)).fetchall()
            payload['tables'][table] = {'columns':names,'rows':[list(row) for row in rows]}
    validate(conn,payload)
    return payload


def validate(conn, payload):
    tables = payload.get('tables')
    if not isinstance(tables,dict) or set(tables) != set(TABLES):
        raise ValueError('Board snapshot must contain exactly the supported tables')
    total = 0
    for table in TABLES:
        content = tables[table]
        expected = columns(conn,table)
        if not isinstance(content,dict) or content.get('columns') != expected or not isinstance(content.get('rows'),list):
            raise ValueError('Board snapshot schema mismatch: '+table)
        total += len(content['rows'])
        if total > MAX_ROWS:
            raise ValueError('Board exceeds portable row limit')
        seen = set()
        for row in content['rows']:
            if not isinstance(row,list) or len(row) != len(expected) or any(v is not None and type(v) not in (str,int) for v in row):
                raise ValueError('Invalid board snapshot row')
            if 'id' in expected:
                identity = row[expected.index('id')]
                if identity in seen:
                    raise ValueError('Duplicate board row identity')
                seen.add(identity)
            for name in ('task_id','parent_id','child_id') + (('id',) if table == 'tasks' else ()):
                if name in expected:
                    value = row[expected.index(name)]
                    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',value) or value in {'.','..'}:
                        raise ValueError('Unsafe board task identifier')


def import_snapshot(conn, payload):
    """Import into an empty board in one transaction, using new sequence IDs.

    Numeric IDs are per-board references, not cross-board authority. PostgreSQL
    sequences allocate every new ID, including imports, so a runtime role never
    receives sequence UPDATE or schema CREATE privileges.
    """
    validate(conn,payload)
    scrub(payload)
    tables = payload['tables']
    run_ids = {}
    with conn.write_transaction(allow_nested=True):
        if any(conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in TABLES):
            raise ValueError('Cannot import over an existing board')
        for table in ('tasks','task_runs','task_links','task_comments','task_events','task_attachments','kanban_notify_subs'):
            names = tables[table]['columns']
            for values in tables[table]['rows']:
                row = dict(zip(names,values))
                old_id = row.pop('id') if table in IDENTITY_TABLES else None
                if table == 'task_events' and row['run_id'] is not None:
                    if row['run_id'] not in run_ids:
                        raise ValueError('Event references a missing run')
                    row['run_id'] = run_ids[row['run_id']]
                if table == 'task_events' and row.get('payload'):
                    try:
                        metadata = json.loads(row['payload'])
                    except (TypeError,ValueError):
                        metadata = None
                    if isinstance(metadata,dict) and metadata.get('run_id') in run_ids:
                        metadata['run_id'] = run_ids[metadata['run_id']]
                        row['payload'] = json.dumps(metadata,ensure_ascii=False)
                query = 'INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')'
                if table in IDENTITY_TABLES:
                    new_id = conn.execute(query+' RETURNING id',tuple(row.values())).fetchone()[0]
                    if table == 'task_runs':
                        run_ids[old_id] = new_id
                else:
                    conn.execute(query,tuple(row.values()))
