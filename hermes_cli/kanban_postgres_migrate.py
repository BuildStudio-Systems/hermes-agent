"""Offline, all-or-nothing import of quiesced SQLite board snapshots.

Use a temporary migration credential. Runtime never invokes this module.
Caller must stop writers and back up sources before calling import_boards.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from hermes_cli.kanban_postgres import TABLES


def digest(rows):
    encoded = sorted(json.dumps(list(row),ensure_ascii=False,separators=(',',':')) for row in rows)
    return hashlib.sha256(('\n'.join(encoded)+'\n').encode()).hexdigest()


def import_boards(settings, sources):
    """Preserve every column and ID, verify contents, reset sequences once.

    sources maps canonical board names to consistent SQLite backup files.
    The destination schema must be provisioned and completely empty.
    """
    import psycopg
    from psycopg import sql
    schema = settings['schema']
    if not re.fullmatch(r'agent_[a-z0-9_]{1,48}',schema):
        raise ValueError('Invalid migration schema')
    for name in sources:
        if name != 'default' and not re.fullmatch(r'(?:_archived/)?[a-z0-9][a-z0-9_-]{0,149}',name):
            raise ValueError('Invalid migration board name')
    receipts = {}
    maximum = {t:0 for t in ('task_runs','task_comments','task_events','task_attachments')}
    with psycopg.connect(**settings['connection']) as target:
        owner = settings.get('owner_role')
        if owner is not None:
            if owner != 'there_agent_operational_owner':
                raise ValueError('Unexpected offline migration owner')
            target.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(owner)))
        target.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        target.execute("SET LOCAL statement_timeout='60s'")
        target.execute("SET LOCAL lock_timeout='3s'")
        # Only one offline import; no upsert or partial second pass.
        target.execute('LOCK TABLE board_registry IN ACCESS EXCLUSIVE MODE')
        if target.execute('SELECT 1 FROM board_registry LIMIT 1').fetchone():
            raise ValueError('Destination board registry is not empty')
        for name,path in sources.items():
            board = uuid.uuid4().hex
            target.execute('INSERT INTO board_registry(id,name) VALUES(%s,%s)',(board,name))
            target.execute("SELECT set_config('buildstudio.kanban_board',%s,true)",(board,))
            receipts[name] = {}
            with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
                source.execute('PRAGMA trusted_schema=OFF')
                source.execute('BEGIN')
                if source.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Source SQLite integrity check failed')
                for table in TABLES:
                    cols = [r[0] for r in target.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND column_name<>%s ORDER BY ordinal_position',(schema,table,'board_id'))]
                    old = [r[1] for r in source.execute('PRAGMA table_info('+table+')')]
                    if set(old) != set(cols):
                        raise ValueError('Source board schema differs: '+table)
                    rows = source.execute('SELECT '+','.join(cols)+' FROM '+table).fetchall()
                    query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,cols)),sql.SQL(',').join(sql.Placeholder() for _ in cols))
                    with target.cursor() as cursor:
                        cursor.executemany(query,rows)
                    actual = target.execute(sql.SQL('SELECT {} FROM {} WHERE board_id=%s').format(sql.SQL(',').join(map(sql.Identifier,cols)),sql.Identifier(table)),(board,)).fetchall()
                    if len(actual) != len(rows) or digest(actual) != digest(rows):
                        differences = [cols[i]+':'+type(a).__name__+'/'+type(b).__name__
                                       for left,right in zip(rows,actual) for i,(a,b) in enumerate(zip(left,right))
                                       if a != b or type(a) != type(b)]
                        raise ValueError('Board content verification failed: '+table+' ('+','.join(sorted(set(differences)))+')')
                    receipts[name][table] = {'rows':len(rows),'sha256':digest(rows)}
                    if table in maximum and rows:
                        maximum[table] = max(maximum[table],max(row[cols.index('id')] for row in rows))
        for table, value in maximum.items():
            # ALTER IDENTITY is transactional; setval would survive a rollback.
            target.execute(sql.SQL('ALTER TABLE {} ALTER COLUMN id RESTART WITH {}').format(
                sql.Identifier(table),sql.Literal(value+1)))
    return {'schema':schema,'boards':receipts,'source':'SQLite snapshots','destination':'PostgreSQL','verified':True}
