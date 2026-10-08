"""Profile-scoped PostgreSQL persistence for the four small operational stores.

This is not a SQLite SQL translator. Callers use portable DML; the only DB-API
difference handled here is qmark parameters. Schema creation/import are offline.
Session transcripts are not routed through this adapter. Kanban subclasses
the connection for native board transactions and row isolation.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from hermes_constants import get_hermes_home

SCOPES = {
    "responses": ("response_store.db", ("responses", "conversations")),
    "runs": ("runs_idempotency.db", ("run_idempotency",)),
    "artifacts": ("cache/chat-files/index.sqlite3", ("chat_file_artifacts",)),
    "verification": ("verification_evidence.db", ("meta", "verification_events", "verification_state")),
    "kanban": ("kanban.db", ("board_registry", "board_fs_operations", "tasks", "task_links", "task_comments", "task_events", "task_runs", "task_attachments", "kanban_notify_subs")),
    "cron": ("cron/executions.db", ("executions", "cron_incidents")),
    "cron_notes": ("cron/notepad.db", ("cron_notepad",)),
}
_pools = {}
_pool_lock = threading.Lock()
_selected_profiles = set()


class Row:
    def __init__(self, names, values):
        self._names, self._values = names, values
        self._mapping = dict(zip(names, values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, (int, slice)) else self._mapping[key]

    def keys(self):
        return self._names

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _row_factory(cursor):
    names = tuple(c.name for c in cursor.description or ())
    return lambda values: Row(names, values)


def _parameters(query: str) -> str:
    """Convert qmark bindings without changing quoted text or identifiers."""
    result = []
    quote = None
    index = 0
    while index < len(query):
        char = query[index]
        if quote:
            result.append("%%" if char == "%" else char)
            if char == quote:
                if index + 1 < len(query) and query[index + 1] == quote:
                    result.append(query[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            result.append(char)
        elif char == "?":
            result.append("%s")
        else:
            result.append("%%" if char == "%" else char)
        index += 1
    return "".join(result)


def configuration(scope: str, db_path=None):
    """Read only the active profile. A selected but broken backend fails closed."""
    if scope not in SCOPES:
        raise ValueError("Unsupported PostgreSQL store")
    home = get_hermes_home().resolve()
    selection = (str(home), scope)
    def local_backend():
        if selection in _selected_profiles:
            raise ValueError("A running PostgreSQL profile cannot fall back to local storage")
        return None
    path = home / "config.yaml"
    if not path.exists():
        return local_backend()
    import yaml

    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("Agent profile configuration must be a mapping")
    storage = config.get("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("Agent storage configuration must be a mapping")
    postgres = storage.get("postgresql")
    if postgres is None:
        return local_backend()
    if not isinstance(postgres, dict) or not isinstance(postgres.get("stores"), list):
        raise ValueError("PostgreSQL stores must be configured explicitly")
    if set(postgres["stores"]) - SCOPES.keys():
        raise ValueError("Unknown operational PostgreSQL store")
    if scope not in postgres["stores"]:
        return local_backend()
    _selected_profiles.add(selection)
    if db_path is not None and Path(db_path).resolve() != (home / SCOPES[scope][0]).resolve():
        raise ValueError("A PostgreSQL profile cannot override its store with a local database")
    filename = postgres.get("config_file")
    if not isinstance(filename, str) or not filename:
        raise ValueError("PostgreSQL credential file is required")
    secret_path = Path(filename).expanduser()
    if not secret_path.is_absolute():
        secret_path = home / secret_path
    stat = secret_path.stat()
    if os.name != "nt" and stat.st_mode & 0o027:
        raise ValueError("PostgreSQL credential file must be owner/group restricted")
    credentials = json.loads(secret_path.read_text(encoding="utf-8"))
    if Path(credentials.get("profile_home", "")).resolve() != home:
        raise ValueError("PostgreSQL credential file belongs to a different Agent profile")
    connection = credentials.get("connection", {})
    allowed = {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}
    if not isinstance(connection, dict) or set(connection) - allowed:
        raise ValueError("Invalid PostgreSQL connection options")
    if connection.get("sslmode") != "verify-full" or not all(
        connection.get(k) for k in ("host", "dbname", "user", "sslrootcert")
    ):
        raise ValueError("Agent PostgreSQL requires verified TLS and an explicit destination")
    schema = credentials.get("schemas", {}).get(scope, "")
    if not re.fullmatch(r"agent_[a-z0-9_]{1,48}", schema):
        raise ValueError("Invalid Agent PostgreSQL schema")
    return {"connection": connection, "schema": schema, "profile": str(home)}


def connection_for(scope: str, db_path=None):
    if scope == 'kanban':
        from hermes_cli.kanban_postgres import connect_if_configured
        return connect_if_configured(db_path or get_hermes_home()/'kanban.db')
    settings = configuration(scope, db_path)
    if settings is None:
        return None
    return Connection(settings, scope)


def close_pools():
    with _pool_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


class Connection:
    is_postgres = True

    def __init__(self, settings: dict, scope: str):
        from psycopg_pool import ConnectionPool

        self.schema = settings["schema"]
        self.scope = scope
        identity = hashlib.sha256(json.dumps(
            {"profile": settings["profile"], "connection": settings["connection"]},
            sort_keys=True).encode()).hexdigest()
        with _pool_lock:
            pool = _pools.get(identity)
            if pool is None:
                pool = ConnectionPool(kwargs={**settings["connection"], "connect_timeout": 3,
                    "autocommit": True, "row_factory": _row_factory,
                    "application_name": "there-agent-operational"},
                    min_size=0, max_size=8, timeout=3, max_waiting=16, open=False)
                pool.open()
                _pools[identity] = pool
        self._pool = pool
        self._lease = pool.connection()
        self._db = self._lease.__enter__()
        self._closed = False
        self._transaction_started = False
        self._write_lock = int.from_bytes(hashlib.sha256(self.schema.encode()).digest()[:8], "big", signed=True)
        try:
            self._configure()
        except Exception:
            self.close()
            raise

    def _configure(self):
        from psycopg import sql
        self._db.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
        self._db.execute("SET statement_timeout='5s'")
        self._db.execute("SET lock_timeout='3s'")
        self._db.execute("SET idle_in_transaction_session_timeout='8s'")
        for table in SCOPES[self.scope][1]:
            self._db.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(table)))

    def _ensure_connection(self):
        if self._closed:
            raise RuntimeError("Operational database connection is closed")
        if self._db.closed and not self._transaction_started:
            # Recover only on a NEW operation after the previous operation
            # failed. Never replay an ambiguous write/commit automatically.
            if self._lease is not None:
                self._lease.__exit__(None, None, None)
                self._lease = None
            lease = self._pool.connection()
            self._db = lease.__enter__()
            self._lease = lease
            try:
                self._configure()
            except Exception:
                self._db.close()
                raise

    def _begin(self):
        from psycopg.pq import TransactionStatus
        self._ensure_connection()
        if self._db.info.transaction_status == TransactionStatus.IDLE:
            self._db.execute("BEGIN")
            self._transaction_started = True
            try:
                self._db.execute("SELECT pg_advisory_xact_lock(%s)", (self._write_lock,))
            except Exception:
                self.rollback()
                raise

    def execute(self, query, parameters=None):
        try:
            self._ensure_connection()
            verb = query.lstrip().split(None, 1)[0].upper()
            if verb in {"INSERT", "UPDATE", "DELETE"}:
                self._begin()
            if parameters is None:
                return self._db.execute(query)
            return self._db.execute(_parameters(query), parameters)
        except Exception:
            # A failed operation must not poison the long-lived response/run
            # handle. Roll back the entire operation; never retry admission.
            self.rollback()
            raise

    def commit(self):
        try:
            self._db.commit()
        finally:
            self._transaction_started = False

    def rollback(self):
        try:
            if not self._db.closed:
                self._db.rollback()
        finally:
            self._transaction_started = False

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.rollback()
            finally:
                if self._lease is not None:
                    self._lease.__exit__(None, None, None)

    def __enter__(self):
        self._begin()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.rollback() if exc_type else self.commit()


def begin_write(connection):
    if getattr(connection, "is_postgres", False):
        connection._begin()
    else:
        connection.execute("BEGIN IMMEDIATE")


if __name__ == "__main__":
    # Production ExecStartPre requires explicit PostgreSQL for every migrated
    # store, so a missing config cannot revive a stale SQLite database.
    import argparse
    parser = argparse.ArgumentParser(description="Validate operational PostgreSQL readiness")
    parser.add_argument("--require", nargs="+", choices=tuple(SCOPES), required=True)
    args = parser.parse_args()
    try:
        for scope in args.require:
            connection = connection_for(scope)
            if connection is None:
                raise RuntimeError("Required PostgreSQL store is not configured: " + scope)
            connection.close()
        print("Required PostgreSQL stores are ready")
    finally:
        close_pools()
