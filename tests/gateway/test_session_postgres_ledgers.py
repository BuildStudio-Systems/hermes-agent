"""Real PostgreSQL shared-session consumers; isolated cluster only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import time
import uuid

import pytest

from hermes_cli import session_postgres as pg


@pytest.fixture
def profile(tmp_path, monkeypatch):
    config = Path(__file__).resolve().parents[2] / '.test-postgres.json'
    if not config.exists():
        pytest.skip('requires disposable PostgreSQL cluster')
    import psycopg
    from psycopg import sql
    connection = json.loads(config.read_text())
    assert connection['dbname'] == 'test_agent_operational'
    schema = 'agent_test_' + uuid.uuid4().hex
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    settings = {'connection': connection, 'schema': schema, 'profile': str(home)}
    with psycopg.connect(**connection) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        db.execute((Path(__file__).resolve().parents[2]/'hermes_cli/session_postgres_schema.sql').read_text())
        db.execute('INSERT INTO schema_version VALUES (26)')
    secret = home / 'credentials.json'
    secret.write_text(json.dumps({'connection': connection, 'profile_home': str(home),
                                 'schemas': {'sessions': schema}}))
    secret.chmod(0o600)
    (home/'config.yaml').write_text('storage:\n  postgresql:\n    stores: [sessions]\n    config_file: credentials.json\n')
    try:
        yield home/'state.db', settings
        assert not (home/'state.db').exists(), 'native consumers must not create SQLite'
    finally:
        pg.close_pools()
        with psycopg.connect(**connection) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_many_idle_handles_release_pool_and_keep_results(profile):
    path, settings = profile
    handles = [pg.connection_for(path) for _ in range(60)]
    try:
        results = [conn.execute("SELECT ? AS text, ? AS value", ('中文 日本語 % ? "', i))
                   for i, conn in enumerate(handles)]
        for i, result in enumerate(results):
            row = result.fetchone()
            assert row[0] == '中文 日本語 % ? "'
            assert dict(row) == {'text': row[0], 'value': i}
        assert all(not h.in_transaction for h in handles)
        assert len({id(h._pool) for h in handles}) == 1
        handles[0].validate_schema()
    finally:
        for handle in handles:
            handle.close()


def test_transaction_rollback_failed_transaction_and_reuse(profile):
    import psycopg
    path, _ = profile
    with closing(pg.connection_for(path)) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn:
                conn.execute("INSERT INTO state_meta VALUES ('x', 'one')")
                conn.execute("INSERT INTO state_meta VALUES ('x', 'two')")
        assert conn.execute('SELECT count(*) FROM state_meta').fetchone()[0] == 0
        with conn:
            conn.executemany('INSERT INTO state_meta VALUES (?, ?)', [('x','中文'),('y','日本語')])
        assert conn.execute('SELECT count(*) FROM state_meta').fetchone()[0] == 2
        with pytest.raises(RuntimeError, match='failed session transaction'):
            with conn:
                try:
                    conn.execute("INSERT INTO state_meta VALUES ('x', 'duplicate')")
                except psycopg.errors.UniqueViolation:
                    pass
        assert not conn.in_transaction


def test_deferred_foreign_key_commit_failure_rolls_back_and_releases(profile):
    import psycopg
    path, _ = profile
    with closing(pg.connection_for(path)) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn:
                conn.execute("INSERT INTO messages(session_id,role,timestamp) VALUES('missing','user',1)")
        assert not conn.in_transaction
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


def test_readonly_is_enforced_by_database_even_for_cte(profile):
    import psycopg
    path, _ = profile
    with closing(pg.connection_for(path, read_only=True)) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with conn:
                conn.execute("WITH inserted AS (INSERT INTO state_meta VALUES ('x','y') RETURNING *) SELECT * FROM inserted")
        assert conn.execute('SELECT count(*) FROM state_meta').fetchone()[0] == 0


def test_profile_does_not_fall_back_and_core_activation_is_blocked(profile):
    path, _ = profile
    from hermes_state import SessionDB
    with pytest.raises(RuntimeError, match='awaits the transcript/search'):
        SessionDB(path)
    with pytest.raises(ValueError, match='override'):
        pg.connection_for(path.parent/'different.db')
    (path.parent/'config.yaml').write_text('{}')
    with pytest.raises(ValueError, match='fall back'):
        pg.connection_for(path)


def test_delivery_recovery_and_runtime_claim_fencing(profile, monkeypatch):
    from gateway import delivery_ledger as ledger
    monkeypatch.setattr(ledger, '_owner_stamp', lambda: (111,222))
    monkeypatch.setattr(ledger, '_owner_alive', lambda pid, started: pid == 111)
    def record(oid):
        ledger.record_obligation(obligation_id=oid, session_key='owner/thread', platform='wecom',
                                 chat_id='owner', thread_id='one', content='中文通知 日本語')
    record('pending')
    record('attempting')
    ledger.mark_attempting('attempting')
    with ledger._transaction() as conn:
        conn.execute('UPDATE delivery_obligations SET owner_pid=NULL, owner_started_at=NULL')
    rows = ledger.sweep_recoverable(deliverable_targets={('wecom','default')})
    assert {r['obligation_id']: r['needs_marker'] for r in rows} == {'pending':False,'attempting':True}
    assert ledger.sweep_recoverable() == []  # now owned by the live claimant
    ledger.mark_failed('pending','send_path_degraded')
    rows = ledger.sweep_failed_for_runtime(platform='wecom', profile='default')
    assert len(rows) == 1 and rows[0]['runtime_recovery']
    assert ledger.sweep_failed_for_runtime(platform='wecom', profile='default') == []
    assert ledger.release_runtime_claim('pending','send_path_degraded')
    ledger.mark_delivered('pending')
    assert not ledger.release_runtime_claim('pending')


def test_delivery_reset_preserves_existing_replace_semantics(profile):
    from gateway import delivery_ledger as ledger
    kwargs = dict(obligation_id='one', session_key='s',platform='wecom',chat_id='me',thread_id=None,content='original')
    ledger.record_obligation(**kwargs)
    ledger.mark_failed('one','failure')
    ledger.record_obligation(**{**kwargs,'content':'replacement'})
    with ledger._transaction() as conn:
        row = conn.execute('SELECT state,content,last_error,attempts FROM delivery_obligations').fetchone()
    assert tuple(row) == ('pending','replacement',None,0)


def test_delegation_claim_competition_and_completion(profile):
    from tools import async_delegation as ledger
    ledger._persist_dispatch({'delegation_id':'task','session_key':'owner',
                              'origin_session_id':'api-owner','dispatched_at':time.time(),'goal':'中文'})
    ledger._persist_completion({'delegation_id':'task','status':'completed'}, {'answer':'日本語'})
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda c: ledger.claim_completion_delivery('task',c), ['one','two']))
    assert sum(claims) == 1
    winner = ['one','two'][claims.index(True)]
    assert not ledger.complete_completion_delivery('task','wrong')
    assert ledger.complete_completion_delivery('task',winner)
    assert not ledger.claim_completion_delivery('task','again')
    assert ledger.get_durable_delegation('task')['origin_session_id'] == 'api-owner'


def test_room_driver_and_policy_use_same_database(profile):
    from gateway import hosted_rooms as rooms, hosted_room_driver as driver
    from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
    path, _ = profile
    room = rooms.create_room(path, room_id='room-1', name='中文 日本語',
                             members=[{'profile':'ops','handle':'ops'}], authority_gateway_id='gateway-a', now=90)
    assert room['room_id'] == 'room-1'
    lease = driver.acquire_lease(path,room_id='room-1',gateway_id='gateway-a',authority_epoch=1,
                                 process_generation='process-a',ttl_seconds=30,clock=lambda:100)
    assert lease is not None
    identity = driver.TaskIdentity(room_id='room-1',task_id='task-1',thread_id='thread-1',turn_id='turn-1')
    task = driver.admit_task(path, identity, payload={'target_profile':'ops','prompt':'inspect','source_event_seq':1},clock=lambda:100)
    assert task['identity'].task_id == 'task-1'
    policy = HostedRoomPolicyCheckpoint(path)
    page = rooms.read_events(path,room_id='room-1',since_seq=0,limit=50)
    cursor = policy.sync(room_id='room-1',latest_seq=page['cursor'])
    assert cursor == page['cursor']
    assert policy.sync(room_id='room-1',latest_seq=cursor) == cursor


def test_concurrent_native_transactions_serialize_read_modify_write(profile):
    path, _ = profile
    with closing(pg.connection_for(path)) as conn, conn:
        conn.execute("INSERT INTO state_meta VALUES ('count','0')")
    def increment(_):
        with closing(pg.connection_for(path)) as conn, conn:
            value = int(conn.execute("SELECT value FROM state_meta WHERE key='count'").fetchone()[0])
            time.sleep(.005)
            conn.execute("UPDATE state_meta SET value=? WHERE key='count'", (str(value+1),))
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(increment, range(18)))
    with closing(pg.connection_for(path)) as conn:
        assert conn.execute("SELECT value FROM state_meta WHERE key='count'").fetchone()[0] == '18'


def test_connection_loss_does_not_replay_and_next_operation_recovers(profile):
    import psycopg
    path, settings = profile
    with closing(pg.connection_for(path)) as conn:
        conn._begin()
        pid = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
        conn.execute("INSERT INTO state_meta VALUES ('must-rollback','once')")
        with psycopg.connect(**settings['connection'], autocommit=True) as admin:
            assert admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]
        with pytest.raises(psycopg.OperationalError):
            conn.commit()
        assert not conn.in_transaction
        assert conn.execute('SELECT count(*) FROM state_meta').fetchone()[0] == 0


def test_read_probe_and_grant_revocation_do_not_need_sqlite_file(profile):
    from gateway import hosted_rooms as rooms
    path, _ = profile
    rooms.create_room(path, room_id='room', name='Room', members=[], authority_gateway_id='gw', now=1)
    assert rooms.probe_hosted_room(path, room_id='room')
    assert not rooms.probe_hosted_room(path, room_id='absent')
    claims = dict(room_id='room',home_install_id='home',authority_gateway_id='gw',authority_epoch=1,
                  member_id='member',target_install_id='target',target_profile='ops')
    rooms.reserve_peer_room(path,claims=claims,expires_at=100,now=2)
    rooms.reserve_peer_room(path,claims=claims,expires_at=50,now=3)
    assert rooms.probe_peer_room_reservation(path,room_id='room',target_profile='ops',now=60)
    rooms.revoke_room_grant_scope(path,claims=claims,expires_at=100,now=4)
    assert not rooms.probe_peer_room_reservation(path,room_id='room',target_profile='ops',now=60)
    rooms.revoke_room_grant_scope(path,claims=claims,expires_at=80,now=5)
    with closing(pg.connection_for(path)) as conn:
        assert tuple(conn.execute('SELECT expires_at,revoked_before FROM hosted_room_revoked_grants').fetchone()) == (100,5)


def test_native_policy_projection_replay_and_monotonic_watermarks(profile):
    from gateway import hosted_rooms as rooms
    from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
    path, _ = profile
    rooms.create_room(path,room_id='room',name='Room',members=[],authority_gateway_id='gw',now=1)
    def append(event_id,kind,payload):
        return rooms.append_event(path,room_id='room',event_id=event_id,kind=kind,
                                  actor={'kind':'user','id':'owner'} if kind=='message.user' else {'kind':'gateway','id':'gw'},
                                  authority_gateway_id='gw',authority_epoch=1,payload=payload)
    user = append('user','message.user',{'thread_id':'thread','text':'中文 日本語'})
    for event_id, watermark in [('settled-new',10),('settled-old',5)]:
        append(event_id,'turn.settled',{'thread_id':'thread','discussion_event_id':'user',
                                       'task_id':event_id,'member_id':'member','seen_through_seq':watermark})
    checkpoint = HostedRoomPolicyCheckpoint(path)
    latest = rooms.room_state(path,room_id='room')['latest_seq']
    checkpoint.sync(room_id='room',latest_seq=latest)
    checkpoint.sync(room_id='room',latest_seq=latest)
    snapshot = checkpoint.snapshot(room_id='room',latest_seq=latest)
    assert snapshot.watermarks[('thread','member')] == 10
    assert len(snapshot.events) == 3
    assert checkpoint.publication_exists(room_id='room',task_id='settled-new',status='settled',execution_generation=1)
    assert checkpoint.events_for_task(room_id='room',source_event_seq=user['seq'])


def test_session_runtime_credentials_cannot_run_ddl(profile):
    import psycopg
    from psycopg import sql
    path, settings = profile
    role = 'agent_reader_' + uuid.uuid4().hex
    password = uuid.uuid4().hex
    with psycopg.connect(**settings['connection'], autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
        admin.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(settings['schema']),sql.Identifier(role)))
        admin.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(settings['schema']),sql.Identifier(role)))
        limited = {**settings,'connection':{**settings['connection'],'user':role,'password':password}}
        try:
            with closing(pg.SessionConnection(limited)) as conn:
                conn.validate_schema()
                with conn:
                    conn.execute("INSERT INTO state_meta VALUES ('allowed','中文')")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn:
                        conn.execute('CREATE TABLE unexpected(value text)')
                assert conn.execute('SELECT value FROM state_meta').fetchone()[0] == '中文'
        finally:
            pg.close_pools()
            admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# Reuse established behavior contracts against the real native backend.
# SQLite file-format/migration contracts stay in their original test suite.
@pytest.mark.parametrize("filename,case,argument", [
    ('test_hosted_room_driver.py', 'test_two_contenders_have_one_winner', 'db'),
    ('test_hosted_room_driver.py', 'test_expiry_allows_reclaim_and_fences_stale_renew_and_release', 'db'),
    ('test_hosted_room_driver.py', 'test_nonexistent_and_disbanded_rooms_cannot_lease_or_admit', 'db'),
    ('test_hosted_room_driver.py', 'test_same_process_acquire_and_release_are_idempotent', 'db'),
    ('test_hosted_room_driver.py', 'test_renew_extends_only_the_current_lease_generation', 'db'),
    ('test_hosted_room_driver.py', 'test_authority_transfer_fences_lease_and_late_settlement', 'db'),
    ('test_hosted_room_driver.py', 'test_room_disband_fences_active_lease_operations', 'db'),
    ('test_hosted_room_driver.py', 'test_task_admission_is_idempotent_and_identity_conflicts_fail', 'db'),
    ('test_hosted_room_driver.py', 'test_concurrent_task_start_has_one_winner', 'db'),
    ('test_hosted_room_driver.py', 'test_stale_lease_cannot_start_or_commit_task', 'db'),
    ('test_hosted_room_driver.py', 'test_cancellation_fences_late_success', 'db'),
    ('test_hosted_room_driver.py', 'test_release_fails_closed_while_its_task_is_running', 'db'),
    ('test_hosted_room_driver.py', 'test_restart_recovery_never_requeues_indeterminate_work', 'db'),
    ('test_hosted_room_driver.py', 'test_recovery_is_required_before_starting_later_work', 'db'),
    ('test_hosted_room_driver.py', 'test_current_lease_can_commit_verified_indeterminate_receipt', 'db'),
    ('test_hosted_room_driver.py', 'test_current_lease_can_commit_verified_indeterminate_cancellation', 'db'),
    ('test_hosted_room_driver.py', 'test_indeterminate_retry_is_explicit_and_advances_execution_generation', 'db'),
    ('test_hosted_room_driver.py', 'test_indeterminate_task_can_be_deferred_retried_and_cancelled', 'db'),
    ('test_hosted_room_driver.py', 'test_proven_not_admitted_attempt_returns_to_queue_under_exact_fence', 'db'),
    ('test_hosted_room_driver.py', 'test_not_admitted_requeue_rejects_stale_lease_and_task_generation', 'db'),
    ('test_hosted_room_driver.py', 'test_tasks_follow_source_event_order_not_admission_time', 'db'),
    ('test_hosted_room_driver.py', 'test_optional_target_member_id_is_durable_and_digest_bound', 'db'),
    ('test_hosted_room_driver.py', 'test_renewal_never_shortens_an_active_lease', 'db'),
    ('test_hosted_rooms.py', 'test_create_room_is_idempotent_but_conflicts_fail_closed', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_concurrent_first_database_open_keeps_every_room', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_room_state_exposes_authority_and_replay_cursor', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_authority_claim_fences_stale_gateway_events', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_concurrent_authority_claim_has_one_winner', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_retry_of_successful_but_superseded_claim_is_distinct', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_authority_scoped_events_require_gateway_and_epoch', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_append_is_idempotent_and_conflicting_event_id_is_rejected', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_since_seq_returns_ordered_deltas_and_stable_cursor', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_room_log_survives_store_reopen', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_concurrent_appends_allocate_one_monotonic_sequence', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_unknown_room_and_invalid_cursor_fail_closed', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_actor_is_part_of_event_idempotency_and_replay', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_disband_is_idempotent_and_room_id_cannot_be_reused', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_disband_rejects_stale_authority', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_retention_pruning_keeps_retired_room_id_reserved', 'tmp_path'),
    ('test_hosted_rooms.py', 'test_peer_reservation_rejects_stale_or_conflicting_authority', 'tmp_path'),
])
def test_existing_room_contract_on_postgres(profile, filename, case, argument):
    import importlib.util
    from gateway import hosted_rooms as rooms
    path, _ = profile
    spec = importlib.util.spec_from_file_location('native_contract_' + filename[:-3], Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if argument == 'db':
        rooms.create_room(path, room_id='room-1', name='Release room',
                          members=[{'profile':'ops','handle':'ops'}], authority_gateway_id='gateway-a', now=90)
        getattr(module, case)(path)
    else:
        getattr(module, case)(path.parent)
