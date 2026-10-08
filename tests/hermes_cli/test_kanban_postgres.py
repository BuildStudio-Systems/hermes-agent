"""Kanban behavior against a private TLS PostgreSQL cluster, not mocks."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import sqlite3
import uuid

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import postgres_runtime as runtime


@pytest.fixture
def pg(tmp_path, monkeypatch):
    config = Path(__file__).resolve().parents[2] / '.test-postgres.json'
    if not config.exists():
        pytest.skip('requires a disposable PostgreSQL cluster')
    import psycopg
    from psycopg import sql
    connection = json.loads(config.read_text())
    assert connection['dbname'] == 'test_agent_operational'
    schema = 'agent_test_' + uuid.uuid4().hex
    role = 'agent_test_' + uuid.uuid4().hex
    password = secrets.token_hex(24)
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_HOME', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    with psycopg.connect(**connection) as db:
        db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        db.execute((Path(kb.__file__).parent / 'kanban_postgres_schema.sql').read_text())
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
        db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
        db.execute(sql.SQL('GRANT USAGE ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
    secret = home / 'database.json'
    secret.write_text(json.dumps({'profile_home':str(home), 'connection':{**connection,'user':role,'password':password}, 'schemas':{'kanban':schema}}))
    secret.chmod(0o600)
    (home/'config.yaml').write_text('storage:\n  postgresql:\n    config_file: database.json\n    stores: [kanban]\n')
    try:
        yield home
    finally:
        runtime.close_pools()
        with psycopg.connect(**connection) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def task(conn, title='中文 日本語 100% ?', **kwargs):
    return kb.create_task(conn, title=title, **kwargs)


def test_schema_matches_current_sqlite(pg, tmp_path):
    # Build the reference using the real legacy migration path outside this profile.
    # No production home or existing database is involved.
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {'HERMES_HOME':str(tmp_path/'legacy')}):
        legacy = kb.connect(tmp_path/'legacy'/'kanban.db')
    with closing(legacy), closing(kb.connect()) as db:
        for table in runtime.SCOPES['kanban'][1][2:]:
            old = {r['name'] for r in legacy.execute(f'PRAGMA table_info({table})')}
            new = {r[0] for r in db.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=?',(db.schema, table))}
            assert new - {'board_id'} == old, table


def test_native_lifecycle_and_no_sqlite(pg):
    with closing(kb.connect()) as db, db:
        tid = task(db)
        assert kb.get_task(db, tid).title == '中文 日本語 100% ?'
        assert kb.add_comment(db, tid, 'owner', 'comment') > 0
        assert kb.add_attachment(db,tid,filename='report.pdf',stored_path='/private/report.pdf') > 0
        assert kb.complete_task(db,tid,summary='done',fire_lifecycle_hook=False)
        assert kb.list_runs(db,tid)[0].summary == 'done'
        kb._maybe_checkpoint_wal(db, pg/'kanban.db')
    with closing(kb.connect()) as db:
        assert kb.get_task(db,tid).status == 'done'
    assert not (pg/'kanban.db').exists()


def test_production_readiness_initializes_board_scope(pg):
    import subprocess
    import sys
    result = subprocess.run([sys.executable,'-m','hermes_cli.postgres_runtime','--require','kanban'],
                            capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'stores are ready' in result.stdout


def test_board_isolation_even_for_unfiltered_dml(pg):
    import psycopg
    kb.create_board('alpha')
    kb.create_board('beta')
    with closing(kb.connect(board='alpha')) as a, closing(kb.connect(board='beta')) as b:
        ta, tb = task(a,'alpha'), task(b,'beta')
        assert kb.get_task(a,tb) is None and kb.get_task(b,ta) is None
        with kb.write_txn(a):
            a.execute("UPDATE tasks SET title='changed'")
        assert kb.get_task(b,tb).title == 'beta'
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with kb.write_txn(a):
                a.execute("UPDATE tasks SET board_id=?",(b.board,))
        assert kb.get_task(a,ta).title == 'changed'
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.execute('CREATE TABLE forbidden(id int)')


def test_concurrent_claim_and_idempotency(pg):
    def create(i):
        with closing(kb.connect()) as db:
            return task(db, idempotency_key='same')
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(create,range(8)))
    assert len(set(ids)) == 1
    def claim(i):
        with closing(kb.connect()) as db:
            return kb.claim_task(db,ids[0],claimer=f'worker-{i}')
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(claim,range(8)))
    assert sum(item is not None for item in claimed) == 1
    with closing(kb.connect()) as db:
        assert len(kb.list_runs(db,ids[0])) == 1


def test_nested_failure_preserves_outer_write(pg):
    import psycopg
    with closing(kb.connect()) as db:
        tid = task(db)
        with kb.write_txn(db):
            kb.add_comment(db,tid,'owner','first')
            with pytest.raises(psycopg.errors.NotNullViolation):
                with kb.write_txn(db,allow_nested=True):
                    db.execute('UPDATE tasks SET title=NULL WHERE id=?',(tid,))
            kb.add_comment(db,tid,'owner','last')
        assert [c.body for c in kb.list_comments(db,tid)] == ['first','last']
        with pytest.raises(ValueError):
            with kb.write_txn(db):
                kb.add_comment(db,tid,'owner','must rollback')
                raise ValueError('abort')
        assert len(kb.list_comments(db,tid)) == 2


def test_dependency_and_notify_inheritance(pg):
    with closing(kb.connect()) as db:
        parent = task(db)
        kb.add_notify_sub(db,task_id=parent,platform='telegram',chat_id='private-chat')
        kb.add_notify_sub(db,task_id=parent,platform='telegram',chat_id='private-chat')
        child = task(db,parents=[parent])
        assert kb.get_task(db,child).status == 'todo'
        assert kb.claim_task(db,child) is None
        assert len(kb.list_notify_subs(db,parent)) == 1
        assert len(kb.list_notify_subs(db,child)) == 1
        assert kb.count_notify_subs() == 2
        assert kb.complete_task(db,parent,summary='parent done',fire_lifecycle_hook=False)
        assert kb.claim_task(db,child) is not None


def test_dispatch_lock_is_database_scoped(pg):
    kb.create_board('other')
    with kb._dispatch_tick_lock(pg/'kanban.db') as first:
        assert first
        with kb._dispatch_tick_lock(pg/'kanban.db') as second:
            assert not second
        with kb._dispatch_tick_lock(kb.kanban_db_path('other')) as other:
            assert other
    with kb._dispatch_tick_lock(pg/'kanban.db') as again:
        assert again


def test_path_and_profile_override_fail_closed(pg, tmp_path):
    with pytest.raises(ValueError,match='outside'):
        kb.connect(tmp_path/'other'/'kanban.db')
    with closing(kb.connect()):
        pass
    (pg/'config.yaml').unlink()
    with pytest.raises(ValueError,match='fall back'):
        kb.connect()


def test_archive_recreate_and_delete_do_not_resurrect_tasks(pg):
    import psycopg
    kb.create_board('project')
    with closing(kb.connect(board='project')) as old:
        tid = task(old)
        result = kb.remove_board('project')
        with closing(kb.connect(Path(result['new_path'])/'kanban.db')) as archived:
            assert kb.get_task(archived,tid) is not None
        kb.create_board('project')
        with closing(kb.connect(board='project')) as new:
            assert new.board != old.board
            assert kb.get_task(new,tid) is None
            next_id = task(new)
            kb.remove_board('project',archive=False)
            assert kb.get_task(new,next_id) is None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                task(new)
        with closing(kb.connect(Path(result['new_path'])/'kanban.db')) as archived:
            assert kb.get_task(archived,tid) is not None


def test_archive_filesystem_failure_rolls_back_database(pg, monkeypatch):
    kb.create_board('project')
    with closing(kb.connect(board='project')) as db:
        tid = task(db)
        original = Path.rename
        def fail(source, destination):
            if source == kb.board_dir('project'):
                raise OSError('simulated filesystem failure')
            return original(source,destination)
        monkeypatch.setattr(Path,'rename',fail)
        with pytest.raises(OSError,match='simulated'):
            kb.remove_board('project')
        assert kb.get_task(db,tid) is not None
        assert db.execute('SELECT count(*) FROM board_fs_operations').fetchone()[0] == 0
        assert kb.board_exists('project')


@pytest.mark.parametrize('committed',[False,True])
def test_interrupted_archive_recovery_uses_database_commit(pg, committed):
    kb.create_board('project')
    with closing(kb.connect(board='project')) as db:
        tid = task(db)
        target = '_archived/project-interrupted'
        destination = pg/'kanban'/'boards'/target
        destination.parent.mkdir(parents=True)
        db.execute('INSERT INTO board_fs_operations VALUES(?,?,?,?,?)',('operation',db.board,'project',target,'archive'))
        if committed:
            db.execute('UPDATE board_registry SET name=? WHERE id=?',(target,db.board))
        kb.board_dir('project').rename(destination)
    # A new connection reconciles the filesystem before selecting a board.
    path = destination/'kanban.db' if committed else kb.kanban_db_path('project')
    with closing(kb.connect(path)) as db:
        assert kb.get_task(db,tid) is not None
        assert db.execute('SELECT count(*) FROM board_fs_operations').fetchone()[0] == 0
    assert destination.exists() == committed
    assert kb.board_dir('project').exists() != committed


def test_portable_export_import_without_sqlite_or_live_claims(pg,tmp_path):
    from hermes_cli import kanban_transfer as transfer
    import tarfile
    kb.create_board('project')
    with closing(kb.connect(board='project')) as db:
        tid = task(db,board='project')
        kb.add_comment(db,tid,'owner','portable')
        kb.add_notify_sub(db,task_id=tid,platform='telegram',chat_id='private-channel')
        kb.claim_task(db,tid,claimer='original-worker')
        archive = transfer.export_board('project',str(tmp_path/'export'))['archive']
        assert kb.get_task(db,tid).claim_lock == 'original-worker'
    with tarfile.open(archive) as bundle:
        assert not any(member.name.endswith('.db') for member in bundle.getmembers())
        payload = bundle.extractfile('project/kanban.json').read()
        assert b'original-worker' not in payload and b'private-channel' not in payload
    imported = transfer.import_board(archive)
    assert imported['board'] == 'project-2'
    with closing(kb.connect(board=imported['board'])) as db:
        assert kb.get_task(db,tid).claim_lock is None
        assert kb.get_task(db,tid).status == 'ready'
        assert kb.list_comments(db,tid)[0].body == 'portable'
        assert kb.list_notify_subs(db) == []
        assert kb.claim_task(db,tid,claimer='new-worker') is not None
        assert len(kb.list_runs(db,tid)) == 2
    assert not list(pg.rglob('*.db'))


def test_invalid_portable_import_rolls_back_all_tables(pg):
    from hermes_cli.kanban_postgres_transfer import snapshot, import_snapshot
    kb.create_board('destination')
    with closing(kb.connect()) as db:
        tid = task(db)
        kb.complete_task(db,tid,fire_lifecycle_hook=False)
        payload = snapshot(db)
    events = payload['tables']['task_events']
    events['rows'][-1][events['columns'].index('run_id')] = 99999999
    with closing(kb.connect(board='destination')) as db:
        with pytest.raises(ValueError,match='missing run'):
            import_snapshot(db,payload)
        assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_legacy_archive_import_reads_without_retaining_sqlite(pg,tmp_path,monkeypatch):
    from hermes_cli import kanban_transfer as transfer
    legacy_home = tmp_path/'legacy'
    with monkeypatch.context() as local:
        local.setenv('HERMES_HOME',str(legacy_home))
        kb.create_board('legacy')
        with closing(kb.connect(board='legacy')) as db:
            tid = task(db,board='legacy')
        archive = transfer.export_board('legacy',str(tmp_path/'old-export'))['archive']
    imported = transfer.import_board(archive)
    with closing(kb.connect(board=imported['board'])) as db:
        assert kb.get_task(db,tid) is not None
    assert not list(pg.rglob('*.db'))


def test_offline_migration_preserves_all_columns_and_rolls_back(pg,tmp_path,monkeypatch):
    import psycopg
    from hermes_cli.kanban_postgres_migrate import import_boards
    # Administrative credentials exist only in the disposable test checkout.
    admin = json.loads((Path(__file__).resolve().parents[2]/'.test-postgres.json').read_text())
    schema = runtime.configuration('kanban')['schema']
    settings = {'schema':schema,'connection':admin}
    legacy = tmp_path/'legacy'
    with monkeypatch.context() as local:
        local.setenv('HERMES_HOME',str(legacy))
        with closing(kb.connect()) as db:
            tid = task(db)
            kb.add_comment(db,tid,'owner','preserve exact columns')
            kb.add_notify_sub(db,task_id=tid,platform='telegram',chat_id='preserve-routing')
            kb.complete_task(db,tid,fire_lifecycle_hook=False)
        with closing(kb.connect(legacy/'broken.db')) as db:
            db.execute('DROP TABLE task_events')
    with pytest.raises(ValueError,match='schema differs'):
        import_boards(settings,{'default':legacy/'kanban.db','broken':legacy/'broken.db'})
    with psycopg.connect(**admin) as db:
        from psycopg import sql
        assert db.execute(sql.SQL('SELECT COUNT(*) FROM {}.board_registry').format(sql.Identifier(schema))).fetchone()[0] == 0
    receipt = import_boards(settings,{'default':legacy/'kanban.db'})
    assert receipt['boards']['default']['tasks']['rows'] == 1
    with closing(kb.connect()) as db:
        assert kb.get_task(db,tid).status == 'done'
        assert kb.list_notify_subs(db)[0]['chat_id'] == 'preserve-routing'
        assert kb.add_comment(db,tid,'owner','new comment') > 1
    with pytest.raises(ValueError,match='not empty'):
        import_boards(settings,{'default':legacy/'kanban.db'})
