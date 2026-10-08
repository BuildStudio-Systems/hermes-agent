"""Real PostgreSQL regression tests; private cluster config is never checked in.

Run with scripts/run_tests.sh. The isolated-cluster harness writes
.test-postgres.json in its disposable checkout, never in a live profile.
"""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import secrets
import uuid

import pytest

from hermes_cli import postgres_runtime as runtime
from hermes_cli.postgres_migrate import SCHEMA_SQL, import_stores


@pytest.fixture
def pg(tmp_path, monkeypatch):
    config = Path(__file__).resolve().parents[2] / ".test-postgres.json"
    if not config.exists():
        pytest.skip("requires a disposable PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    connection = json.loads(config.read_text())
    assert connection["dbname"] == "test_agent_operational", "refuse non-test database"
    schemas = {scope: "agent_test_" + uuid.uuid4().hex for scope in runtime.SCOPES}
    role = "agent_test_" + uuid.uuid4().hex
    password = secrets.token_hex(24)
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with psycopg.connect(**connection) as db:
        db.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        for scope, schema in schemas.items():
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            db.execute(SCHEMA_SQL[scope])
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            db.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            db.execute(sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
    secret = home / "database.json"
    secret.write_text(json.dumps({"profile_home": str(home), "connection": {**connection,"user":role,"password":password}, "schemas": schemas}))
    secret.chmod(0o600)
    (home / "config.yaml").write_text("storage:\n  postgresql:\n    config_file: database.json\n    stores: [responses, runs, artifacts, verification]\n")
    try:
        yield home, {"connection": connection, "schemas": schemas}
    finally:
        runtime.close_pools()
        with psycopg.connect(**connection) as db:
            for schema in schemas.values():
                db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_responses_lru_history_and_restart(pg):
    from gateway.platforms.api_server import ResponseStore
    store = ResponseStore(max_size=2)
    store.put("first", {"messages": [{"content": "中文 日本語 100% ?", "role": "user"}]})
    store.set_conversation("private", "first")
    store.put("second", {"ok": 2})
    assert store.get("first")["messages"][0]["content"].endswith("100% ?")
    store.put("third", {"ok": 3})
    assert store.get("second") is None
    store.close()
    store = ResponseStore(max_size=2)
    assert len(store) == 2
    assert store.get_conversation("private") == "first"
    assert store.delete("first")
    assert store.get_conversation("private") is None
    store.close()
    assert not (pg[0] / "response_store.db").exists()


def test_run_reservation_concurrent_retry_and_owner(pg):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
    def reserve(index):
        store = RunIdempotencyStore()
        try:
            assert store.durable
            return store.reserve("owner-a", "same-key", "same-fingerprint", f"run-{index}", {"status": "running"})
        finally:
            store.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(8)))
    assert [r[0] for r in results].count("created") == 1
    assert len({r[1]["run_id"] for r in results}) == 1
    run_id = results[0][1]["run_id"]
    store = RunIdempotencyStore()
    assert store.lookup("owner-a", "same-key", "different")[0] == "conflict"
    assert store.status_for_run("owner-b", run_id) is None
    assert not store.owns_run("owner-b", run_id)
    assert not store.acknowledge_terminal("owner-b", run_id)
    assert store.extend_retention("owner-a", run_id, 9999999999)
    assert store.lookup("owner-a", "same-key", "same-fingerprint", retention_until=1)[0] == "reused"
    store.update_status(run_id, {"status": "completed"})
    assert store.status_for_run("owner-a", run_id, retention_until=2)["status"]["status"] == "completed"
    assert store.acknowledge_terminal("owner-a", run_id)
    store.close()
    assert not (pg[0] / "runs_idempotency.db").exists()


def test_artifact_owner_chat_and_source_change(pg, tmp_path):
    from gateway.chat_file_artifacts import ChatFileArtifactStore, ChatFileArtifactNotFound
    store = ChatFileArtifactStore(pg[0] / runtime.SCOPES["artifacts"][0])
    source = tmp_path / "report.txt"
    source.write_text("私有文件")
    chat = str(uuid.uuid4())
    item = store.publish(str(source), owner_id="owner-a", chat_id=chat)
    assert store.publish(str(source), owner_id="owner-a", chat_id=chat) == item
    assert store.publish(str(source), owner_id="owner-a", chat_id=str(uuid.uuid4())).artifact_id != item.artifact_id
    assert store.resolve(item.artifact_id, owner_id="owner-a").mtime_ns == source.stat().st_mtime_ns
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(item.artifact_id, owner_id="owner-b")
    source.write_text("changed")
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(item.artifact_id, owner_id="owner-a")
    assert not store.db_path.exists()


def test_verification_evidence_persists(pg, tmp_path):
    from agent.verification_evidence import record_verify_run
    first = record_verify_run(root=tmp_path, session_id="owner-a", ok=True)
    second = record_verify_run(root=tmp_path, session_id="owner-b", ok=False)
    assert second["id"] > first["id"]
    conn = runtime.connection_for("verification")
    try:
        row = conn.execute("SELECT * FROM verification_events WHERE id=?", (first["id"],)).fetchone()
        assert row["session_id"] == "owner-a"
        assert dict(row)["status"] == "passed"
    finally:
        conn.close()
    assert not (pg[0] / "verification_evidence.db").exists()


def test_parameters_rollback_and_reuse(pg):
    conn = runtime.connection_for("responses")
    try:
        assert conn.execute("SELECT '?' AS quoted, ? AS bound, '100%' AS percentage", ("?'%",)).fetchone()[0:3] == ("?", "?'%", "100%")
        with pytest.raises(Exception):
            with conn:
                conn.execute("INSERT INTO responses VALUES (?, ?, ?)", ("rollback", "{}", 1.0))
                conn.execute("SELECT * FROM deliberately_missing_table")
        assert conn.execute("SELECT count(*) FROM responses").fetchone()[0] == 0
        with conn:
            conn.execute("INSERT INTO responses VALUES (?, ?, ?)", ("committed", "{}", 2.0))
        assert conn.execute("SELECT count(*) FROM responses").fetchone()[0] == 1
    finally:
        conn.close()


def test_lost_connection_fails_current_operation_then_recovers(pg):
    import psycopg
    conn = runtime.connection_for("responses")
    try:
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        with psycopg.connect(**pg[1]["connection"]) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        with pytest.raises(psycopg.OperationalError):
            conn.execute("INSERT INTO responses VALUES (?,?,?)", ("lost", "{}", 1.0))
        with conn:
            conn.execute("INSERT INTO responses VALUES (?,?,?)", ("next", "{}", 2.0))
        assert [r[0] for r in conn.execute("SELECT response_id FROM responses")] == ["next"]
    finally:
        conn.close()


def test_runtime_cannot_create_tables_or_reset_identity(pg):
    import psycopg
    conn = runtime.connection_for("verification")
    try:
        for statement in ("CREATE TABLE unauthorized(id bigint)",
                          "ALTER TABLE verification_events ALTER COLUMN id RESTART WITH 1"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
        assert conn.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()[0] is True
    finally:
        conn.close()


def test_selected_backend_cannot_disappear_mid_process(pg):
    conn = runtime.connection_for("verification")
    conn.close()
    (pg[0] / "config.yaml").unlink()
    with pytest.raises(ValueError, match="cannot fall back"):
        runtime.connection_for("verification")
    assert not (pg[0] / "verification_evidence.db").exists()


@pytest.mark.parametrize("damage", ["wrong-profile", "wrong-tls", "missing-secret", "local-override"])
def test_configuration_fails_closed(pg, damage):
    from gateway.platforms.api_server import ResponseStore
    home, _ = pg
    path = home / "database.json"
    value = json.loads(path.read_text())
    if damage == "wrong-profile":
        value["profile_home"] = str(home / "other")
    elif damage == "wrong-tls":
        value["connection"]["sslmode"] = "disable"
    path.write_text(json.dumps(value))
    if damage == "missing-secret":
        path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        ResponseStore(db_path=":memory:" if damage == "local-override" else None)
    assert not (home / "response_store.db").exists()


def test_import_atomic_rollback_and_content(pg, tmp_path, monkeypatch):
    from gateway.platforms.api_server import ResponseStore
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
    from gateway.chat_file_artifacts import ChatFileArtifactStore
    from agent.verification_evidence import record_verify_run
    import psycopg
    from psycopg import sql
    source = tmp_path / "legacy"
    source.mkdir()
    with monkeypatch.context() as legacy:
        legacy.setenv("HERMES_HOME", str(source))
        response = ResponseStore()
        response.put("migrated", {"text": "日本語 中文"})
        response.close()
        runs = RunIdempotencyStore()
        runs.reserve("owner", "key", "fp", "run", {"status": "running"})
        runs.close()
        ChatFileArtifactStore(source / runtime.SCOPES["artifacts"][0])
        original = record_verify_run(root=tmp_path, session_id="owner", ok=True)
    settings = pg[1]
    with psycopg.connect(**settings["connection"]) as db:
        db.execute(sql.SQL("INSERT INTO {}.meta VALUES ('conflict','1')").format(sql.Identifier(settings["schemas"]["verification"])))
    with pytest.raises(RuntimeError, match="not empty"):
        import_stores(source, settings)
    with psycopg.connect(**settings["connection"]) as db:
        assert db.execute(sql.SQL("SELECT count(*) FROM {}.responses").format(sql.Identifier(settings["schemas"]["responses"]))).fetchone()[0] == 0
        db.execute(sql.SQL("DELETE FROM {}.meta").format(sql.Identifier(settings["schemas"]["verification"])))
    receipt = import_stores(source, settings)
    assert receipt["responses.responses"]["rows"] == 1
    assert receipt["verification.verification_events"]["rows"] == 1
    response = ResponseStore()
    assert response.get("migrated") == {"text": "日本語 中文"}
    response.close()
    newer = record_verify_run(root=tmp_path, session_id="owner", ok=False)
    assert newer["id"] > original["id"]
    with pytest.raises(RuntimeError, match="not empty"):
        import_stores(source, settings)
