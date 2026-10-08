"""Native transcript runtime contracts against disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path

import pytest

import importlib.util
_spec = importlib.util.spec_from_file_location('session_ledger_fixtures', Path(__file__).with_name('test_session_postgres_ledgers.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
profile = _fixtures.profile
from hermes_state_postgres import PostgresSessionDB


@pytest.fixture
def database(profile):
    import psycopg
    from psycopg import sql
    path, settings = profile
    with psycopg.connect(**settings['connection']) as db:
        db.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
        db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(settings['schema'])))
        db.execute((Path(__file__).resolve().parents[2]/'hermes_cli/session_postgres_runtime.sql').read_text())
    with PostgresSessionDB(path) as database:
        yield database


def test_transcript_prompt_and_usage_roundtrip(database):
    db = database
    db.create_session('session', source='api', user_id='owner', system_prompt='原文 日本語 % ?',
                      model_config={'temperature':0.7}, model='local-model')
    first = db.append_message('session',role='user',content='你好\n日本語')
    second = db.append_message('session',role='assistant',content='回答')
    assert second > first
    db.update_token_counts('session',input_tokens=123,output_tokens=45,model='local-model',api_call_count=1)
    session = db.get_session('session')
    assert session['system_prompt'] == '原文 日本語 % ?'
    assert session['input_tokens'] == 123
    assert session['output_tokens'] == 45
    messages = db.get_messages('session')
    assert [m['content'] for m in messages] == ['你好\n日本語','回答']
    assert db.get_messages('session',offset=1)[0]['id'] == second
    rows = db.list_sessions_rich()
    assert rows[0]['id'] == 'session'
    assert '你好' in rows[0]['preview']


def test_named_profile_search_and_a2a_use_target_backend(database, tmp_path, monkeypatch):
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from tools.session_search_tool import _resolve_profile_db, _locate_session_db
    from plugins.platforms.a2a.adapter import A2AAdapter
    from gateway.config import PlatformConfig
    from types import SimpleNamespace
    home = database.db_path.parent
    caller = tmp_path/'caller'
    caller.mkdir()
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: home if name == 'target' else caller)
    monkeypatch.setattr(profiles, 'list_profiles', lambda: [SimpleNamespace(name='target', path=home)])
    database.create_session('target-session', source='a2a')
    database.append_message('target-session', role='user', content='目标配置会话')
    before_env = __import__('os').environ.get('HERMES_HOME')
    token = set_hermes_home_override(caller)
    try:
        with _resolve_profile_db('target') as reader:
            assert reader.is_postgres
            assert reader.get_messages('target-session')[0]['content'] == '目标配置会话'
            with pytest.raises(PermissionError):
                reader.append_message('target-session', role='user', content='forbidden')
        found, name = _locate_session_db('target-session')
        with closing(found):
            assert name == 'target'
            assert found.get_session('target-session')
        adapter = A2AAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._title_forward_session('target', 'target-session', 'remote-title')
        assert adapter._lookup_forward_session('target', 'remote-title') == 'target-session'
        assert adapter._latest_a2a_session('target', 0) == 'target-session'
        assert get_hermes_home() == caller
        assert __import__('os').environ.get('HERMES_HOME') == before_env
        assert not (caller/'state.db').exists()
    finally:
        reset_hermes_home_override(token)


def test_profile_backend_failure_does_not_read_retained_sqlite(database, monkeypatch):
    from hermes_cli import profiles
    from hermes_cli.profile_session_storage import profile_session_connection, open_profile_session_db
    import sqlite3
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda name: database.db_path.parent)
    # A retained pre-migration file must never become a silent fallback.
    with sqlite3.connect(database.db_path) as old:
        old.execute('CREATE TABLE sessions(id TEXT)')
        old.execute("INSERT INTO sessions VALUES ('stale')")
    credentials = database.db_path.parent/'credentials.json'
    original = credentials.read_text()
    try:
        from hermes_state import repair_state_db_schema
        old_bytes = database.db_path.read_bytes()
        report = repair_state_db_schema(database.db_path)
        assert not report['repaired']
        assert 'PostgreSQL' in report['error']
        assert database.db_path.read_bytes() == old_bytes
        credentials.write_text('{}')
        with pytest.raises(ValueError, match='different Agent profile'):
            with profile_session_connection('target'):
                pytest.fail('must fail before yielding connection')
        with pytest.raises(ValueError, match='different Agent profile'):
            open_profile_session_db('target')
        with pytest.raises(ValueError):
            open_profile_session_db('../target')
    finally:
        credentials.write_text(original)
        database.db_path.unlink()


def test_native_diagnostics_and_maintenance_do_not_touch_local_files(database):
    from hermes_state import collect_state_db_stats
    from hermes_cli.doctor import _render_state_db_stats
    database.create_session('stats-session', source='api')
    database.append_message('stats-session', role='user', content='diagnostic')
    stats = collect_state_db_stats(database.db_path)
    assert stats['backend'] == 'postgresql' and stats['healthy']
    assert stats['sessions'] == 1 and stats['messages'] == 1
    assert stats['logical_size_bytes'] > 0
    assert stats['wal_size_bytes'] is None
    rendered = _render_state_db_stats(stats)
    assert all(kind == 'info' for kind, _, _ in rendered)
    assert 'PostgreSQL' in rendered[0][1]
    for method in (database.vacuum, database.optimize_fts, database.rebuild_fts, database.optimize_fts_storage):
        with pytest.raises(RuntimeError, match='PostgreSQL'):
            method()
    with database._read_ctx() as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1


def test_native_diagnostics_missing_index_fails_without_exposing_sql(database):
    from hermes_state import collect_state_db_stats
    from hermes_cli.doctor import _render_state_db_stats
    with database._conn:
        database._conn.execute('DROP INDEX messages_content_trgm')
    stats = collect_state_db_stats(database.db_path)
    assert stats['healthy'] is False
    assert stats['error'] == 'SearchIndexesUnavailable'
    assert _render_state_db_stats(stats)[0][0] == 'warn'
    credentials = database.db_path.parent/'credentials.json'
    saved = credentials.read_text()
    try:
        credentials.write_text('{"bad-secret":"do-not-expose"}')
        failed = collect_state_db_stats(database.db_path)
        assert not failed['healthy']
        assert failed['backend'] == 'unavailable'
        assert 'do-not-expose' not in json.dumps(failed)
        assert failed['wal_size_bytes'] is None
    finally:
        credentials.write_text(saved)


def test_batch_replace_export_and_import(database):
    db = database
    db.create_session('session',source='api',user_id='owner')
    messages = [{'role':'user','content':'question'}, {'role':'assistant','content':'answer'}]
    db.replace_messages('session',messages)
    assert all(isinstance(m['_row_id'],int) for m in messages)
    exported = db.export_session('session')
    assert exported['id'] == 'session'
    assert len(exported['messages']) == 2
    db.delete_session('session')
    result = db.import_sessions([exported])
    assert db.get_session('session')['user_id'] == 'owner'
    assert [m['content'] for m in db.get_messages('session')] == ['question','answer']


def test_native_turn_and_compression_lease_contention(database):
    db = database
    db.create_session('session',source='api')
    def claim(holder):
        with PostgresSessionDB(db.db_path) as sibling:
            return sibling.try_acquire_session_turn_lease('session',holder)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim,['owner-a','owner-b']))
    assert sum(claims) == 1
    winner = ['owner-a','owner-b'][claims.index(True)]
    db.release_session_turn_lease('session',winner)
    assert db.try_acquire_session_turn_lease('session','owner-c')
    db.release_session_turn_lease('session','owner-c')
    assert db.try_acquire_compression_lock('session','compressor-a')
    assert not db.try_acquire_compression_lock('session','compressor-b')
    db.release_compression_lock('session','compressor-a')
    assert db.try_acquire_compression_lock('session','compressor-b')


def test_native_profile_readonly_and_token_queue(database):
    db = database
    db.create_session('session',source='api')
    for _ in range(5):
        db.queue_token_counts('session',input_tokens=10,output_tokens=2,model='local-model')
    assert db.flush_token_counts(timeout=10)
    assert db.get_session('session')['input_tokens'] == 50
    with PostgresSessionDB(db.db_path,read_only=True) as reader:
        assert reader.get_session('session')['output_tokens'] == 10
        with pytest.raises(PermissionError):
            reader.append_message('session',role='user',content='forbidden')
    assert db.get_messages('session') == []


def test_lineage_marker_enrichment_and_topic_opt_in(database):
    db = database
    db.create_session('parent',source='api',user_id='owner')
    db.create_session('reset',source='api',model_config={'_reset_from':'parent'})
    db.create_session('reset',source='api',model_config={'temperature':0.5})
    config = json.loads(db.get_session('reset')['model_config'])
    assert config == {'_reset_from':'parent','temperature':0.5}
    db.enable_telegram_topic_mode(chat_id='chat',user_id='owner')
    assert db.get_meta('telegram_dm_topic_schema_version') == '2'
    assert db.logical_size_bytes() > 0

# Reuse public behavioral contracts against the native engine, not SQLite mocks.
_spec_contract = importlib.util.spec_from_file_location('session_core_contracts', Path(__file__).resolve().parents[1]/'test_hermes_state.py')
_contract = importlib.util.module_from_spec(_spec_contract)
_spec_contract.loader.exec_module(_contract)

@pytest.fixture
def db(database):
    return database

for _class in (
    'TestTimestampPreservation', 'TestSearchSessions', 'TestCounts',
    'TestDeleteAndExport', 'TestPruneSessions', 'TestPruneSessionFilters',
    'TestDeleteSessionOrphansChildren', 'TestSessionTitle',
    'TestSessionTitleLineage', 'TestTitleUniqueness', 'TestTitleLineage',
    'TestTitleSqlWildcards', 'TestListSessionsRich',
):
    globals()[_class] = getattr(_contract, _class)


@pytest.mark.parametrize('method', [
    'test_search_finds_content', 'test_search_returns_context',
    'test_search_fields_project_results_without_changing_default',
    'test_long_search_query_is_capped_and_does_not_crash',
])
def test_native_existing_search_contract(database, method):
    getattr(_contract.TestFTS5Search(), method)(database)


def test_native_boolean_prefix_phrase_cjk_and_visibility(database):
    db = database
    db.create_session('s',source='cli')
    contents=['Docker deployment 日本語設計','Docker Java','red\nblue deployment',
              'red green blue deploy','中文100%记录','中文100记录','secrethidden']
    ids=[db.append_message('s','user',x) for x in contents]
    def matched(query, **kwargs):
        return {x['id'] for x in db.search_messages(query,fields=('id',),**kwargs)}
    assert matched('docker NOT java') == {ids[0]}
    assert matched('deploy*') == set(ids[:1]+ids[2:4])
    assert matched('"red blue"') == {ids[2]}
    assert matched('日本語') == {ids[0]}
    assert matched('中文100%') == {ids[4]}
    assert matched('docker',source_filter=[]) == set()
    assert matched('docker',exclude_sources=['cli']) == set()
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET active=0 WHERE id=?',(ids[-1],)))
    assert matched('secrethidden') == set()
    assert matched('secrethidden',include_inactive=True) == {ids[-1]}
    results=db.search_messages('中文100记录')
    assert all('secrethidden' not in c['content'] for r in results for c in r['context'])


def test_native_search_large_transcript_tail(database):
    db=database
    db.create_session('large',source='api')
    content=('noise '*200000)+'uniqueendneedle 中文尾部'
    mid=db.append_message('large','tool',content)
    result=db.search_messages('uniqueendneedle',fields=('id','snippet'))
    assert result[0]['id']==mid and 'uniqueendneedle' in result[0]['snippet']
    assert len(result[0]['snippet'])<=160
    assert db.search_messages('中文尾部',fields=('id',)) == [{'id':mid}]


class TestListSessionsRich(_contract.TestListSessionsRich):
    def test_session_key_predicate_can_use_session_key_index(self, db):
        with db._read_ctx() as conn:
            conn.execute("SELECT set_config('enable_seqscan','off',true)")
            plan=conn.execute('EXPLAIN (FORMAT JSON) SELECT s.id FROM sessions s WHERE s.session_key=? ORDER BY s.started_at DESC LIMIT 10',('lane',)).fetchone()[0]
        assert 'idx_sessions_session_key' in json.dumps(plan)


def test_native_named_parameters_and_atomic_failure(database):
    db=database
    assert db._conn.execute("SELECT :value AS result, ':untouched' AS literal", {'value':'中文% ?'}).fetchone()['result']=='中文% ?'
    db.create_session('atomic',source='api')
    import psycopg
    # Explicit failure after a prior statement must roll back the whole unit.
    def abort(conn):
        conn.execute("UPDATE sessions SET title='must roll back' WHERE id='atomic'")
        conn.execute('SELECT 1/0')
    with pytest.raises(psycopg.errors.DivisionByZero):
        db._execute_write(abort)
    assert db.get_session('atomic')['title'] is None



def test_committed_write_acknowledgement_loss_is_not_replayed(database, monkeypatch):
    from hermes_cli.session_postgres import SessionConnection
    db=database
    db.create_session('ack',source='api')
    commit=SessionConnection.commit
    lost=[]
    def lose_ack(conn):
        commit(conn)
        if conn is db._conn and not lost:
            lost.append(True)
            raise ConnectionError('simulated lost COMMIT acknowledgement')
    monkeypatch.setattr(SessionConnection,'commit',lose_ack)
    with pytest.raises(ConnectionError):
        db.append_message('ack','user','exactly once')
    assert [m['content'] for m in db.get_messages('ack')]==['exactly once']


def test_only_admission_is_retried(database):
    import psycopg
    import threading
    from hermes_cli.session_postgres import SessionConnection
    db=database
    db.create_session('busy',source='api')
    db._conn._timeout=0.05
    with psycopg.connect(**db._settings['connection'],autocommit=True) as blocker:
        blocker.execute('SELECT pg_advisory_lock(%s)',(db._conn._write_lock,))
        timer=threading.Timer(0.2,lambda: blocker.execute('SELECT pg_advisory_unlock(%s)',(db._conn._write_lock,)))
        timer.start()
        try:
            db.append_message('busy','user','after lock')
        finally:
            timer.join()
    attempts=[]
    def delayed_body(conn):
        attempts.append(True)
        conn.execute('SELECT pg_sleep(0.1)')
    with pytest.raises(psycopg.errors.QueryCanceled):
        db._execute_write(delayed_body)
    assert len(attempts)==1
    assert len(db.get_messages('busy'))==1



def test_readiness_and_unclean_exit_probe_use_selected_remote_store(database):
    from gateway.readiness import _probe_state_db
    from gateway.lifecycle_ledger import check_state_db_integrity
    from psycopg import sql
    import psycopg
    db=database
    assert not db.db_path.exists()
    assert _probe_state_db(db.db_path.parent)['backend']=='postgresql'
    assert check_state_db_integrity(home=db.db_path.parent)=='postgresql-schema-ok'
    with psycopg.connect(**db._settings['connection']) as admin:
        admin.execute(sql.SQL('ALTER TABLE {}.messages RENAME TO missing_messages').format(sql.Identifier(db._settings['schema'])))
    assert _probe_state_db(db.db_path.parent)['status']=='degraded'
    assert check_state_db_integrity(home=db.db_path.parent).startswith('check-failed:')
    assert not db.db_path.exists()



def test_native_approval_scan_without_sqlite_and_maintenance_status(database):
    from hermes_cli.approvals_suggest import scan_approval_history
    db=database
    db.create_session('approvals',source='api')
    for ident,result in [('accepted','ok: done'),('denied','BLOCKED: User denied execution')]:
        calls=[{'id':ident,'type':'function','function':{'name':'terminal','arguments':json.dumps({'command':'git push --force origin main'})}}]
        db.append_message('approvals','assistant',content='',tool_calls=calls)
        db.append_message('approvals','tool',content=result,tool_call_id=ident)
    records=scan_approval_history(db.db_path,days=0)
    assert len(records)==1 and records[0][0]=='git push --force origin main'
    assert not db.fts_optimize_available()
    assert db.fts_rebuild_status() is None
    assert db.fts_cjk_rebuild_status() is None
    assert not db.fts_rebuild_step()
    assert not db.fts_cjk_rebuild_step()
    assert db._conn.execute("SELECT :value AS v, ':ignored?' AS literal /* :comment? 50% */",{'value':'kept'}).fetchone()['v']=='kept'



def test_native_read_context_has_one_snapshot(database):
    db=database
    db.create_session('stable',source='api')
    with db._read_ctx() as reader:
        assert reader.execute("SELECT title FROM sessions WHERE id='stable'").fetchone()[0] is None
        db.set_session_title('stable','new title')
        assert reader.execute("SELECT title FROM sessions WHERE id='stable'").fetchone()[0] is None
    assert db.get_session('stable')['title']=='new title'
