"""Native SessionDB migration against disposable PostgreSQL, never production."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from hermes_cli.session_postgres_migrate import TABLES, import_snapshot, inspect_snapshot


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'legacy'))
    from hermes_state import SessionDB
    from gateway import hosted_rooms, hosted_room_driver, delivery_ledger
    from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
    from tools import async_delegation
    path = tmp_path/'legacy'/'state.db'
    with SessionDB(path) as db:
        db.create_session('parent', source='api', user_id='owner',
                          system_prompt='Keep Unicode: 中文 日本語', model_config={'nested':{'v':1}})
        db.create_session('child', source='api', parent_session_id='parent', user_id='owner')
        db.append_message('parent', role='user', content='中文 日本語 100% ? exact\ntext')
        db.append_message('child', role='assistant', content='response')
        db.apply_telegram_topic_migration()
    with closing(hosted_rooms._connect(path)):
        pass
    # Build optional tables through the real initializer, closing each handle
    # explicitly (SQLite's connection context manager doesn't close).
    original = HostedRoomPolicyCheckpoint._connect
    handles = []
    def track(self):
        conn = original(self)
        handles.append(conn)
        return conn
    with monkeypatch.context() as m:
        m.setattr(HostedRoomPolicyCheckpoint, '_connect', track)
        try:
            HostedRoomPolicyCheckpoint(path)
        finally:
            for conn in handles:
                conn.close()
    with closing(hosted_room_driver._connect(path)), closing(delivery_ledger._connect()), closing(async_delegation._connect()):
        pass
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('UPDATE sessions SET started_at=? WHERE id=?', (1791398765.1234567,'parent'))
        db.execute("INSERT INTO state_meta VALUES('migration-test', ' literal {\"key\": 1} ')")
        db.execute("UPDATE sqlite_sequence SET seq=9000 WHERE name='messages'")
    return path


@pytest.fixture
def target():
    config = Path(__file__).resolve().parents[2]/'.test-postgres.json'
    if not config.exists():
        pytest.skip('requires disposable PostgreSQL cluster')
    import psycopg
    from psycopg import sql
    connection = json.loads(config.read_text())
    assert connection['dbname'] == 'test_agent_operational'
    schema = 'agent_test_'+uuid.uuid4().hex
    with psycopg.connect(**connection) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        db.execute((Path(__file__).resolve().parents[2]/'hermes_cli/session_postgres_schema.sql').read_text())
    try:
        yield {'connection':connection, 'schema':schema}
    finally:
        with psycopg.connect(**connection) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def connect(settings):
    import psycopg
    from psycopg import sql
    db = psycopg.connect(**settings['connection'])
    db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(settings['schema'])))
    return db


def test_native_schema_and_complete_copy(source, target):
    inventory = inspect_snapshot(source)
    assert set(inventory) == set(TABLES)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = import_snapshot(target, source)
    assert result['verified']
    assert set(result['tables']) == set(TABLES)
    assert result['tables']['sessions']['rows'] == 2
    assert result['tables']['messages']['rows'] == 2
    assert result['next_message_id'] == 9001  # preserves deleted-ID high-water
    with connect(target) as db, closing(sqlite3.connect(source)) as old:
        for table in TABLES:
            cols = inventory[table]['columns']
            a = old.execute('SELECT '+','.join('"'+x+'"' for x in cols)+' FROM '+table).fetchall()
            b = db.execute('SELECT '+','.join('"'+x+'"' for x in cols)+' FROM '+table).fetchall()
            assert sorted(a) == sorted(b), table
        row = db.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('child','user','next',1) RETURNING id").fetchone()
        assert row[0] == 9001
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_optional_tables_can_be_absent(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('DROP TABLE telegram_dm_topic_bindings')
        db.execute('DROP TABLE telegram_dm_topic_mode')
    result = import_snapshot(target, source)
    assert result['empty_optional_tables'] == ['telegram_dm_topic_bindings','telegram_dm_topic_mode']


def test_unknown_table_and_unknown_column_are_not_silently_dropped(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('CREATE TABLE new_feature(payload TEXT)')
    with pytest.raises(ValueError, match='Unmapped'):
        import_snapshot(target, source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('DROP TABLE new_feature')
        db.execute('ALTER TABLE sessions ADD COLUMN new_feature TEXT')
    with pytest.raises(ValueError, match='column mismatch'):
        import_snapshot(target, source)
    with connect(target) as db:
        assert db.execute('SELECT count(*) FROM system_prompts').fetchone()[0] == 0


def test_second_import_and_preexisting_optional_rows_refused(source, target):
    with connect(target) as db:
        db.execute("INSERT INTO telegram_dm_topic_mode(chat_id,user_id,activated_at,updated_at) VALUES('x','x',1,1)")
    with pytest.raises(ValueError, match='not empty'):
        import_snapshot(target, source)
    with connect(target) as db:
        db.execute('DELETE FROM telegram_dm_topic_mode')
    import_snapshot(target, source)
    with pytest.raises(ValueError, match='not empty'):
        import_snapshot(target, source)


def test_failure_in_last_table_rolls_back_all_rows_and_sequence(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("INSERT INTO delivery_obligations(obligation_id,session_key,platform,chat_id,content,state,created_at,updated_at) VALUES('x','x','api','x',?,'pending',1,1)",('bad\0text',))
    with pytest.raises(ValueError, match='NUL'):
        import_snapshot(target, source)
    with connect(target) as db:
        for table in TABLES:
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
        assert db.execute('SELECT last_value,is_called FROM messages_id_seq').fetchone() == (1,False)


def test_precision_loss_is_detected_and_rolls_back(source, target):
    with connect(target) as db:
        db.execute('ALTER TABLE sessions ALTER COLUMN started_at TYPE REAL')
    with pytest.raises(ValueError, match='content verification failed'):
        import_snapshot(target, source)
    with connect(target) as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0


def test_broken_foreign_key_refused_before_import(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("UPDATE messages SET session_id='missing'")
    with pytest.raises(ValueError, match='foreign keys'):
        import_snapshot(target, source)


def test_source_version_and_missing_required_table_refused(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('UPDATE schema_version SET version=27')
    with pytest.raises(ValueError, match='schema version'):
        import_snapshot(target, source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('UPDATE schema_version SET version=26')
        db.execute('DROP TABLE gateway_heartbeats')
    with pytest.raises(ValueError, match='required canonical'):
        import_snapshot(target, source)


def test_two_importers_only_one_can_commit(source, target):
    def attempt(_):
        try:
            return import_snapshot(target, source)['verified']
        except ValueError as exc:
            assert 'not empty' in str(exc)
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False,True]


def test_generated_source_column_and_view_are_not_lost(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('CREATE VIEW custom_view AS SELECT id FROM sessions')
    with pytest.raises(ValueError, match='unmapped view'):
        import_snapshot(target, source)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('DROP VIEW custom_view')
        db.execute('ALTER TABLE sessions ADD COLUMN custom_generated TEXT GENERATED ALWAYS AS (id) VIRTUAL')
    with pytest.raises(ValueError, match='generated column'):
        import_snapshot(target, source)


def test_unknown_destination_table_is_not_overlooked(source, target):
    with connect(target) as db:
        db.execute('CREATE TABLE preexisting_data(payload TEXT)')
    with pytest.raises(ValueError, match='unexpected tables'):
        import_snapshot(target, source)


def test_identity_exhaustion_rolls_back_everything(source, target):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("UPDATE sqlite_sequence SET seq=9223372036854775807 WHERE name='messages'")
    with pytest.raises(ValueError, match='exhausted'):
        import_snapshot(target, source)
    with connect(target) as db:
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    {'schema':'public'},
    {'owner_role':'postgres'},
    {'connection':{'sslmode':'disable'}},
])
def test_unsafe_settings_refused(source, target, change):
    with pytest.raises(ValueError):
        import_snapshot({**target,**change}, source)
