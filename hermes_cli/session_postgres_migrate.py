"""Offline SessionDB import. This does not select or activate a runtime backend.

Use a consistent, protected SQLite backup and a temporary migration credential.
Stop all writers before taking the final cutover backup. Never import a live
database pathname and assume that subsequent writes have been transferred.
"""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

TABLES = (
    'schema_version', 'system_prompts', 'sessions', 'messages',
    'session_model_usage', 'state_meta', 'gateway_routing',
    'gateway_hygiene_state', 'gateway_heartbeats', 'compression_locks',
    'session_turn_leases', 'async_delegations', 'telegram_dm_topic_mode',
    'telegram_dm_topic_bindings', 'hosted_rooms', 'hosted_room_events',
    'hosted_room_retired_ids', 'hosted_room_links', 'hosted_room_remote_runs',
    'hosted_room_revoked_grants', 'hosted_room_peer_reservations',
    'hosted_room_policy_cursors', 'hosted_room_policy_threads',
    'hosted_room_policy_events', 'hosted_room_policy_watermarks',
    'hosted_room_policy_publications', 'hosted_room_policy_transcript',
    'hosted_room_policy_transcript_state', 'hosted_room_driver_leases',
    'hosted_room_driver_tasks', 'delivery_obligations',
)
REQUIRED = frozenset(TABLES[:12])
FTS_TABLES = frozenset(
    name + suffix
    for name in ('messages_fts', 'messages_fts_trigram', 'messages_fts_cjk')
    for suffix in ('', '_data', '_idx', '_content', '_docsize', '_config')
)
_SQLITE_METADATA = frozenset(('sqlite_sequence', 'sqlite_stat1', 'sqlite_stat4'))


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _row_hash(row):
    # Tag types and encode doubles exactly. A text "1" is not an integer 1;
    # epoch timestamps must not be rounded by PostgreSQL REAL (float32).
    typed = []
    for value in row:
        if value is None:
            typed.append(['null'])
        elif isinstance(value, str):
            if '\0' in value:
                raise ValueError('Source contains text with an unsupported NUL character')
            typed.append(['text', value])
        elif type(value) is int:
            typed.append(['integer', str(value)])
        elif type(value) is float and math.isfinite(value):
            typed.append(['float64', value.hex()])
        else:
            raise ValueError('Source contains an unsupported value type')
    return hashlib.sha256(json.dumps(typed, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).digest()


def _digest(hashes):
    # Row order is not a storage invariant. Sorting fixed-size hashes retains
    # multiplicity without retaining transcript bodies in memory or receipts.
    h = hashlib.sha256()
    for item in sorted(hashes):
        h.update(item)
    return h.hexdigest()


def inspect_snapshot(path):
    """Inspect only metadata/counts; reject unknown tables instead of losing them."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as source:
        source.execute('PRAGMA trusted_schema=OFF')
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        return _inspect(source)


def _inspect(source):
    # quick_check validates canonical btrees without loading optional FTS
    # tokenizers. Foreign keys receive their own explicit validation below.
    if source.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise ValueError('Session snapshot integrity check failed')
    names = {row[0] for row in source.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    unknown = names - set(TABLES) - FTS_TABLES - _SQLITE_METADATA
    if unknown:
        raise ValueError('Unmapped SessionDB tables: '+', '.join(sorted(unknown)))
    if not REQUIRED <= names:
        raise ValueError('Session snapshot lacks required canonical tables')
    if source.execute('SELECT version FROM schema_version').fetchall() != [(26,)]:
        raise ValueError('Session snapshot must use canonical schema version 26')
    if source.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Session snapshot has broken foreign keys')
    for name, ddl in source.execute("SELECT name,sql FROM sqlite_master WHERE type='view'"):
        normalized = ' '.join(ddl.split()).rstrip(';')
        expected = ('CREATE VIEW messages_fts_trigram_src AS SELECT id, role, content, '
                    "tool_name, tool_calls FROM messages WHERE role <> 'tool'")
        if name != 'messages_fts_trigram_src' or normalized != expected:
            raise ValueError('Session snapshot contains an unmapped view')
    for table in set(TABLES) & names:
        if any(row[6] for row in source.execute('PRAGMA table_xinfo('+_quote(table)+')')):
            raise ValueError('Session snapshot contains an unmapped generated column: '+table)
    return {table: {
        'columns': [row[1] for row in source.execute('PRAGMA table_info('+_quote(table)+')')],
        'rows': source.execute('SELECT count(*) FROM '+_quote(table)).fetchone()[0],
    } for table in TABLES if table in names}


def import_snapshot(settings, path):
    """Copy and verify all supported rows atomically into an EMPTY schema.

    Preserves source columns, primary keys, JSON bytes, fractional timestamps
    and the deleted-message identity high-water mark. It never upserts, drops,
    truncates, modifies the SQLite source, or logs conversation data.
    """
    import psycopg
    from psycopg import sql

    schema = settings['schema']
    if not re.fullmatch(r'agent_[a-z0-9_]{1,48}', schema):
        raise ValueError('Invalid migration schema')
    connection = settings['connection']
    if connection.get('sslmode') != 'verify-full':
        raise ValueError('Session migration requires verified TLS')
    receipts = {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as source:
        source.execute('PRAGMA trusted_schema=OFF')
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        inventory = _inspect(source)
        with psycopg.connect(**{**connection, 'connect_timeout':5,
                               'application_name':'there-session-offline-migration'}) as target:
            owner = settings.get('owner_role')
            if owner is not None:
                if owner != 'there_agent_operational_owner':
                    raise ValueError('Unexpected offline migration owner')
                target.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(owner)))
            target.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
            target.execute("SET LOCAL statement_timeout='60s'")
            target.execute("SET LOCAL lock_timeout='3s'")
            target.execute('SET CONSTRAINTS ALL DEFERRED')
            destination_tables = {row[0] for row in target.execute(
                'SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=%s', (schema,))}
            if destination_tables != set(TABLES):
                raise ValueError('Destination SessionDB schema has missing or unexpected tables')
            target.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(
                sql.SQL(',').join(map(sql.Identifier, TABLES))))
            # Even an optional table with one existing row forbids a second import.
            for table in TABLES:
                if target.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
                    raise ValueError('Destination SessionDB is not empty: '+table)
            for table, details in inventory.items():
                columns = details['columns']
                actual_columns = [row[0] for row in target.execute(
                    'SELECT column_name FROM information_schema.columns '
                    'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (schema, table))]
                if set(columns) != set(actual_columns):
                    raise ValueError('Session snapshot column mismatch: '+table)
                selection = sql.SQL(',').join(map(sql.Identifier, columns))
                insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(table), selection,
                    sql.SQL(',').join(sql.Placeholder() for _ in columns))
                old_hashes = []
                cursor = source.execute('SELECT '+','.join(map(_quote, columns))+' FROM '+_quote(table))
                with target.cursor() as writer:
                    while batch := cursor.fetchmany(250):
                        old_hashes.extend(_row_hash(row) for row in batch)
                        writer.executemany(insert, batch)
                # Server cursor bounds verification memory for large tool outputs.
                new_hashes = []
                with target.cursor(name='verify_'+table) as reader:
                    reader.execute(sql.SQL('SELECT {} FROM {}').format(selection, sql.Identifier(table)))
                    while batch := reader.fetchmany(250):
                        new_hashes.extend(_row_hash(row) for row in batch)
                checksum = _digest(old_hashes)
                if len(old_hashes) != len(new_hashes) or checksum != _digest(new_hashes):
                    raise ValueError('Session content verification failed: '+table)
                receipts[table] = {'rows':len(old_hashes), 'sha256':checksum}
            max_id = source.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
            old_sequence = source.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
            next_id = max(max_id, old_sequence[0] if old_sequence else 0) + 1
            if not 1 <= next_id <= 9223372036854775807:
                raise ValueError('Message identity sequence is exhausted')
            # Finish deferred FK checks before ALTER TABLE; pending constraint
            # triggers otherwise reject the transactional identity restart.
            target.execute('SET CONSTRAINTS ALL IMMEDIATE')
            # setval is NOT transactional; ALTER IDENTITY rolls back with rows.
            target.execute(sql.SQL('ALTER TABLE messages ALTER COLUMN id RESTART WITH {}').format(sql.Literal(next_id)))
    return {'schema':schema, 'verified':True, 'tables':receipts,
            'empty_optional_tables':sorted(set(TABLES)-set(inventory)), 'next_message_id':next_id}
