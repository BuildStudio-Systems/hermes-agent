"""Cron audit and bounded notepad behavior on real TLS PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

import pytest
from cron import executions, incidents, notepad
from hermes_cli import postgres_runtime as runtime


@pytest.fixture
def pg(tmp_path,monkeypatch):
    config=Path(__file__).resolve().parents[2]/'.test-postgres.json'
    if not config.exists():pytest.skip('requires disposable PostgreSQL cluster')
    import psycopg
    from psycopg import sql
    connection=json.loads(config.read_text())
    assert connection['dbname']=='test_agent_operational'
    schema='agent_test_'+uuid.uuid4().hex
    role='agent_test_'+uuid.uuid4().hex
    password=secrets.token_hex(24)
    home=tmp_path/'profile';home.mkdir()
    monkeypatch.setenv('HERMES_HOME',str(home))
    for module,name in [(executions,'EXECUTIONS_FILE'),(incidents,'EXECUTIONS_FILE'),(notepad,'NOTEPAD_FILE')]:
        monkeypatch.setattr(module,name,None)
    # Tests exercise persistence, not external monitoring delivery.
    monkeypatch.setattr(executions,'_emit_execution_state',lambda *a,**k:None)
    with psycopg.connect(**connection) as db:
        db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        db.execute((Path(__file__).resolve().parents[2]/'hermes_cli/cron_postgres_schema.sql').read_text())
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    secret=home/'database.json'
    secret.write_text(json.dumps({'profile_home':str(home),'connection':{**connection,'user':role,'password':password},
                                 'schemas':{'cron':schema,'cron_notes':schema}}));secret.chmod(0o600)
    (home/'config.yaml').write_text('storage:\n  postgresql:\n    config_file: database.json\n    stores: [cron, cron_notes]\n')
    try:yield home
    finally:
        runtime.close_pools()
        with psycopg.connect(**connection) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_execution_state_machine_and_no_local_files(pg):
    row=executions.create_execution('synthetic',source='test')
    assert executions.mark_execution_running(row['id'])['status']=='running'
    assert executions.finish_execution(row['id'],success=True)['status']=='completed'
    assert executions.finish_execution(row['id'],success=False,error='late') is None
    assert executions.list_executions(job_id='synthetic')[0]['status']=='completed'
    assert not list(pg.rglob('*.db'))


def test_incident_dedup_and_closed_stays_closed(pg):
    identity,new=incidents.upsert_incident('synthetic','network timeout')
    assert new
    assert incidents.ack_incident(identity)
    assert incidents.upsert_incident('synthetic','network timeout')==(identity,False)
    assert incidents.list_incidents()[0]['state']=='closed'
    assert incidents.upsert_incident('synthetic','different problem')[1]


def test_terminal_retention_keeps_active_attempts(pg,monkeypatch):
    monkeypatch.setattr(executions,'MAX_TERMINAL_EXECUTIONS',2)
    active=executions.create_execution('active',source='test')
    for _ in range(4):
        row=executions.create_execution('terminal',source='test')
        executions.finish_execution(row['id'],success=True)
    assert len(executions.list_executions(job_id='terminal'))==2
    assert executions.list_executions(job_id='active')[0]['id']==active['id']


def test_notepad_utf8_caps_and_job_removal(pg):
    notepad.set_note('synthetic','日本語','中文')
    assert notepad.get_note('synthetic','日本語')=='中文'
    assert '中文' in notepad.render_notepad_section('synthetic')
    assert notepad.clear_notepad('synthetic')==1
    assert notepad.render_notepad_section('synthetic')==''
    for i in range(3):notepad.set_note('bounded',str(i),'中'*5000)
    with pytest.raises(ValueError,match='notepad full'):
        for i in range(3,5):notepad.set_note('bounded',str(i),'中'*5000)
    assert len(notepad.list_notes('bounded'))==4
    assert not (pg/'cron'/'notepad.db').exists()


def test_concurrent_process_dedup_and_quota(pg):
    # Separate processes avoid the modules' in-process locks masking a race.
    code="from cron.incidents import upsert_incident; print(upsert_incident('concurrent','timeout')[1])"
    def run(_):
        return subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=25,check=True).stdout.strip()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(run,range(4)))
    assert results.count('True')==1
    assert results.count('False')==3
    notepad.set_note('quota','initial','中'*5000)
    notepad.set_note('quota','second','中'*5000)
    notepad.set_note('quota','third','中'*5000)
    code="import sys\nfrom cron.notepad import set_note\ntry:set_note('quota',sys.argv[1],'中'*5000);print('ok')\nexcept ValueError:print('full')"
    def quota(i):
        return subprocess.run([sys.executable,'-c',code,str(i)],capture_output=True,text=True,timeout=25,check=True).stdout.strip()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(quota,range(4)))
    assert results.count('ok')==1
    assert results.count('full')==3


def test_rollback_dml_only_and_no_fallback(pg):
    import psycopg
    with pytest.raises(RuntimeError):
        with notepad._transaction() as db:
            db.execute("INSERT INTO cron_notepad VALUES('x','x','x','x')")
            raise RuntimeError('abort')
    assert notepad.get_note('x','x') is None
    with closing(executions._connect()) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('CREATE TABLE forbidden(id int)')
    (pg/'config.yaml').unlink()
    with pytest.raises(ValueError,match='cannot fall back'):notepad.set_note('x','x','x')
    assert not list(pg.rglob('*.db'))


def test_readiness_and_path_isolation(pg,monkeypatch,tmp_path):
    result=subprocess.run([sys.executable,'-m','hermes_cli.postgres_runtime','--require','cron','cron_notes'],
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    monkeypatch.setattr(executions,'EXECUTIONS_FILE',tmp_path/'outside.db')
    with pytest.raises(ValueError,match='cannot override'):executions.list_executions()
    assert not (tmp_path/'outside.db').exists()


def test_notepad_resolves_current_profile(monkeypatch,tmp_path):
    monkeypatch.setattr(notepad,'NOTEPAD_FILE',None)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'first'))
    notepad.set_note('same','key','first')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'second'))
    assert notepad.get_note('same','key') is None
    notepad.set_note('same','key','second')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'first'))
    assert notepad.get_note('same','key')=='first'
