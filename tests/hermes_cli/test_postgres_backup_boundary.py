"""Real file archives must not present retained SQLite as current PG data."""
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from hermes_cli import backup
from hermes_cli.postgres_backup_boundary import BackupBoundary, MANIFEST


def provision(home, stores=('sessions', 'kanban', 'cron', 'artifacts')):
    home.mkdir(parents=True, exist_ok=True)
    (home/'config.yaml').write_text('storage:\n  postgresql:\n    config_file: private.json\n    stores: '+json.dumps(list(stores))+'\n')
    (home/'private.json').write_text(json.dumps({
        'profile_home': str(home),
        'connection': {'host':'localhost', 'dbname':'test_only', 'user':'test_only',
                       'password':'secret-not-for-manifests', 'sslmode':'verify-full', 'sslrootcert':'/private/ca.pem'},
        'schemas': {store:'agent_test_'+store for store in stores},
    }))
    (home/'private.json').chmod(0o600)
    return home


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = provision(tmp_path/'profile')
    monkeypatch.setenv('HERMES_HOME', str(home))
    for rel in ('state.db', 'state.db-wal', 'cron/executions.db',
                'cache/chat-files/index.sqlite3', 'kanban/boards/work/kanban.db'):
        path = home/rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'retained evidence; not a live database')
    (home/'cron/jobs.json').write_text('{"jobs": []}')
    return home


def test_plan_is_profile_bound_and_manifest_contains_no_credentials(home):
    from hermes_constants import get_hermes_home
    sibling = provision(home/'profiles/other', ('responses',))
    plan = BackupBoundary(home)
    assert plan.profiles['profiles/other'] == ['responses']
    assert plan.excluded('profiles/other/response_store.db-wal')
    assert not plan.excluded('profiles/other/state.db')
    assert plan.protected_restore('sub/../config.yaml')
    assert plan.protected_restore('private.json')
    assert not plan.protected_restore('cron/jobs.json')
    assert get_hermes_home() == home
    data = json.dumps(plan.manifest())
    assert 'secret-not-for-manifests' not in data and '/private/ca.pem' not in data
    assert sibling.is_dir()


def test_quick_snapshot_preserves_local_files_and_reports_external_data(home):
    snap_id = backup.create_quick_snapshot(hermes_home=home, keep=1)
    root = home/'state-snapshots'/snap_id
    meta = json.loads((root/'manifest.json').read_text())
    assert not meta['external_storage']['database_data_included']
    assert 'state.db' not in meta['files']
    assert 'cron/executions.db' not in meta['files']
    assert 'kanban/boards/work/kanban.db' not in meta['files']
    assert 'cron/jobs.json' in meta['files']
    # Restore only local data; retain the current storage config and credentials.
    config = (home/'config.yaml').read_bytes()
    secret = (home/'private.json').read_bytes()
    (root/'config.yaml').write_text('storage: {}\n')
    (home/'cron/jobs.json').write_text('{"jobs": [{"id":"changed"}]}')
    assert backup.restore_quick_snapshot(snap_id, hermes_home=home)
    assert (home/'config.yaml').read_bytes() == config
    assert (home/'private.json').read_bytes() == secret
    assert json.loads((home/'cron/jobs.json').read_text()) == {'jobs': []}
    backup.create_quick_snapshot(hermes_home=home, keep=1)
    assert root.exists(), 'file-only archives must not prune prior recovery evidence'


@pytest.mark.parametrize('manual', [False, True])
def test_full_archive_excludes_retained_files_and_marks_external_database(home, tmp_path, monkeypatch, manual):
    target = tmp_path/'files.zip'
    monkeypatch.setattr(backup, '_collect_memory_provider_external_paths', lambda: [])
    if manual:
        backup._run_backup_locked(SimpleNamespace(output=str(target)), home)
    else:
        assert backup._write_full_zip_backup(target, home) == target
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert 'state.db' not in names and 'state.db-wal' not in names
        assert 'cache/chat-files/index.sqlite3' not in names
        assert 'kanban/boards/work/kanban.db' not in names
        assert 'config.yaml' in names
        assert json.loads(archive.read(MANIFEST))['database_data_included'] is False


def test_full_import_preserves_selected_config_and_database(home, tmp_path, monkeypatch):
    archive = tmp_path/'legacy.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('config.yaml', 'storage: {}')
        z.writestr('private.json', '{}')
        z.writestr('state.db', 'old database')
        z.writestr('cron/jobs.json', '{"jobs":[{"id":"restored"}]}')
    monkeypatch.setattr(backup, 'get_default_hermes_root', lambda: home)
    config = (home/'config.yaml').read_bytes()
    database = (home/'state.db').read_bytes()
    secret = (home/'private.json').read_bytes()
    backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    assert (home/'config.yaml').read_bytes() == config
    assert (home/'private.json').read_bytes() == secret
    assert (home/'state.db').read_bytes() == database
    assert json.loads((home/'cron/jobs.json').read_text())['jobs'][0]['id'] == 'restored'


def test_archive_requires_every_target_profile_provisioned(home, tmp_path, monkeypatch):
    plan = BackupBoundary(home)
    meta = plan.manifest()
    meta['postgresql_profiles']['profiles/missing'] = ['sessions']
    archive = tmp_path/'external.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('config.yaml', 'storage: {}')
        z.writestr(MANIFEST, json.dumps(meta))
        z.writestr('must-not-appear.txt', 'no')
    monkeypatch.setattr(backup, 'get_default_hermes_root', lambda: home)
    with pytest.raises(SystemExit):
        backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    assert not (home/'must-not-appear.txt').exists()


def test_invalid_backend_config_cannot_create_misleading_backup(home, tmp_path):
    (home/'private.json').write_text('{"secret":"never-log-this"}')
    with pytest.raises(RuntimeError, match='ValueError') as error:
        backup.create_quick_snapshot(hermes_home=home)
    assert 'never-log-this' not in str(error.value)
    assert not (home/'state-snapshots').exists()
    assert backup._write_full_zip_backup(tmp_path/'bad.zip', home) is None
    assert not (tmp_path/'bad.zip').exists()


@pytest.mark.parametrize('kind', ['update', 'migration'])
def test_automatic_file_archive_never_prunes_previous_database_backups(home, kind):
    directory = home/'backups'
    directory.mkdir()
    previous = directory/f'pre-{kind}-2000-01-01.zip'
    with zipfile.ZipFile(previous, 'w') as z:
        z.writestr('state.db', 'previous recovery evidence')
    create = getattr(backup, f'create_pre_{kind}_backup')
    assert create(hermes_home=home, keep=0)
    assert previous.exists()
