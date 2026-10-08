"""Native PostgreSQL transactions for the shared session database.

Handles do not retain a server connection while idle. SQL dialect differences
belong at the caller; this module only binds qmark parameters and adapts rows.
Activation remains blocked in SessionDB until every transcript consumer is ported.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
from collections.abc import Mapping
import threading
from collections import deque

from hermes_cli.postgres_runtime import _parameters, _row_factory, configuration

_pools = {}
_pool_lock = threading.Lock()


def close_pools():
    with _pool_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


def connection_for(db_path=None, *, read_only=False, timeout=3):
    settings = configuration('sessions', db_path)
    return None if settings is None else SessionConnection(settings, read_only=read_only, timeout=timeout)


def collect_stats(db_path):
    """Read-only native diagnostics, or None when SQLite is selected.

    A configuration failure is an unavailable backend, never permission to
    inspect/repair an old local file. No SQL/DSN payload is returned on failure.
    This checks logical schema/readability, not physical backup integrity.
    """
    stats = {'backend': 'unavailable', 'healthy': False}
    conn = None
    try:
        settings = configuration('sessions', db_path)
        if settings is None:
            return None
        stats['backend'] = 'postgresql'
        conn = SessionConnection(settings, read_only=True, timeout=2)
        conn.validate_schema()
        with conn:
            if conn.execute("SELECT json_extract(?, ?)", ('{"_reset_from":"ok"}', '$._reset_from')).fetchone()[0] != 'ok':
                raise ValueError('Native session functions unavailable')
            stats['sessions'] = conn.execute('SELECT count(*) FROM sessions').fetchone()[0]
            stats['messages'] = conn.execute('SELECT count(*) FROM messages').fetchone()[0]
            stats['logical_size_bytes'] = conn.execute(
                'SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) '
                'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                "WHERE n.nspname=current_schema() AND c.relkind='r'"
            ).fetchone()[0]
            indexes = conn.execute(
                'SELECT c.relname, i.indisvalid AND i.indisready '
                'FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid '
                'JOIN pg_namespace n ON n.oid=c.relnamespace '
                'WHERE n.nspname=current_schema() AND c.relname IN (?,?,?)',
                ('messages_content_trgm', 'messages_tool_name_trgm', 'messages_tool_calls_trgm'),
            ).fetchall()
            stats['search_indexes'] = {row[0]: bool(row[1]) for row in indexes}
        stats['healthy'] = len(indexes) == 3 and all(row[1] for row in indexes)
        if not stats['healthy']:
            stats['error'] = 'SearchIndexesUnavailable'
    except Exception as exc:
        stats['error'] = type(exc).__name__
    finally:
        if conn is not None:
            conn.close()
    return stats


def _bind_query(query, parameters):
    if not isinstance(parameters, Mapping):
        return _parameters(query), parameters
    # Named bindings occur in the import path. Preserve literals, quoted
    # identifiers, comments and PostgreSQL :: casts; only replace :names.
    values = []
    token = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/|::)|:([A-Za-z_][A-Za-z_0-9]*)", re.S)
    def bind(match):
        if match.group(1) is not None:
            return match.group(1).replace('%', '%%')
        values.append(parameters[match.group(2)])
        return '%s'
    pieces=[]
    position=0
    for match in token.finditer(query):
        pieces.append(query[position:match.start()].replace('%','%%'))
        pieces.append(bind(match))
        position=match.end()
    pieces.append(query[position:].replace('%','%%'))
    return ''.join(pieces), values


class SessionAdmissionBusy(TimeoutError):
    """No business SQL ran: bounded pool/transaction admission was busy."""


class Cursor:
    """Detached results: consumers cannot accidentally retain pooled sockets."""
    def __init__(self, rows=(), *, rowcount=-1, description=None):
        self._rows = deque(rows)
        self.rowcount = rowcount
        self.description = description

    def fetchone(self):
        return self._rows.popleft() if self._rows else None

    def fetchall(self):
        rows = list(self._rows)
        self._rows.clear()
        return rows

    def fetchmany(self, size=1):
        return [self._rows.popleft() for _ in range(min(size, len(self._rows)))]

    def __iter__(self):
        while self._rows:
            yield self._rows.popleft()


class SessionConnection:
    is_postgres = True

    def __init__(self, settings, *, read_only=False, timeout=3, autocommit=False):
        from psycopg_pool import ConnectionPool

        self.schema = settings['schema']
        if not re.fullmatch(r'agent_[a-z0-9_]{1,48}', self.schema):
            raise ValueError('Invalid session PostgreSQL schema')
        if settings['connection'].get('sslmode') != 'verify-full':
            raise ValueError('Session PostgreSQL requires verified TLS')
        identity = (os.getpid(), hashlib.sha256(json.dumps({
            'profile': settings.get('profile'), 'connection': settings['connection'],
        }, sort_keys=True).encode()).digest())
        with _pool_lock:
            pool = _pools.get(identity)
            if pool is None:
                pool = ConnectionPool(kwargs={**settings['connection'],
                    'autocommit': True, 'connect_timeout': 3,
                    'row_factory': _row_factory, 'application_name': 'there-agent-sessions'},
                    min_size=0, max_size=8, timeout=3, max_waiting=16, open=False)
                pool.open()
                _pools[identity] = pool
        self._pool = pool
        self.read_only = read_only
        self.autocommit = autocommit
        self._timeout = max(0.01, min(float(timeout), 3.0))
        self._db = None
        self._closed = False
        self._context = False
        self._failed = False
        self._write_lock = int.from_bytes(hashlib.sha256(
            ('session:' + self.schema).encode()).digest()[:8], 'big', signed=True)

    @property
    def in_transaction(self):
        return self._db is not None

    def _begin(self, *, read_only=None):
        from psycopg import sql
        if self._closed:
            raise RuntimeError('Session database connection is closed')
        if self._failed:
            raise RuntimeError('Session transaction requires rollback')
        if self._db is not None:
            return
        readonly = self.read_only if read_only is None else read_only
        if self.read_only and not readonly:
            raise RuntimeError('Session database is read-only')
        from psycopg_pool import PoolTimeout
        from psycopg.errors import LockNotAvailable, QueryCanceled
        try:
            self._db = self._pool.getconn(timeout=self._timeout)
        except PoolTimeout as exc:
            raise SessionAdmissionBusy('Session PostgreSQL connection pool is busy') from exc
        try:
            self._db.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY' if readonly else 'BEGIN')
            self._db.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(
                sql.Identifier(self.schema)))
            self._db.execute("SELECT set_config('statement_timeout', %s, true)",
                             (str(int(self._timeout * 1000)) if self._timeout < 3 else '10000',))
            self._db.execute("SET LOCAL lock_timeout='3s'")
            self._db.execute("SET LOCAL idle_in_transaction_session_timeout='15s'")
            if not readonly:
                # Shared across core/rooms/delivery/delegation, like the old
                # file write lock. Reads never wait on this advisory lock.
                self._db.execute('SELECT pg_advisory_xact_lock(%s)', (self._write_lock,))
        except (LockNotAvailable, QueryCanceled) as exc:
            self.rollback()
            raise SessionAdmissionBusy('Session PostgreSQL write admission is busy') from exc
        except BaseException:
            self.rollback()
            raise

    def execute(self, query, parameters=None):
        # No SQL rewrite, implicit PRAGMA emulation, DDL or RETURNING guessing.
        # Auxiliary DML retains explicit transactions; core handles opt into
        # standalone autocommit while business units still use with-transaction.
        standalone_read = self._db is None and query.lstrip().split(None, 1)[0].upper() == 'SELECT'
        standalone = self._db is None and (standalone_read or self.autocommit)
        self._begin(read_only=True if standalone_read else None)
        try:
            with self._db.cursor() as cursor:
                bound, values = (query, None) if parameters is None else _bind_query(query, parameters)
                cursor.execute(bound, values)
                result = Cursor(cursor.fetchall() if cursor.description else (),
                                rowcount=cursor.rowcount, description=cursor.description)
            if standalone:
                self.commit()
            return result
        except BaseException:
            self._failed = True
            if standalone:
                self.rollback()
            raise

    def executemany(self, query, parameters):
        self._begin()
        try:
            with self._db.cursor() as cursor:
                cursor.executemany(_parameters(query), parameters)
                return Cursor(rowcount=cursor.rowcount)
        except BaseException:
            self._failed = True
            raise

    def _finish(self, commit):
        db, self._db = self._db, None
        failed, self._failed = self._failed, False
        if db is None:
            return
        try:
            if commit and failed:
                db.rollback()
                raise RuntimeError('Cannot commit a failed session transaction')
            db.commit() if commit else db.rollback()
        except BaseException:
            # A lost COMMIT acknowledgement is ambiguous. Discard the socket;
            # never replay a transcript write or message-delivery claim here.
            db.close()
            raise
        finally:
            self._pool.putconn(db)

    def commit(self):
        self._finish(True)

    def rollback(self):
        self._finish(False)

    def close(self):
        if not self._closed:
            try:
                self.rollback()
            finally:
                self._closed = True

    def __enter__(self):
        if self._context or self.in_transaction:
            raise RuntimeError('Nested session transactions are not supported')
        self._begin()
        self._context = True
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.rollback() if exc_type else self.commit()
        finally:
            self._context = False

    def validate_schema(self):
        from psycopg import sql
        from hermes_cli.session_postgres_migrate import TABLES
        self._begin(read_only=True)
        try:
            for table in TABLES:
                self._db.execute(sql.SQL('SELECT * FROM {} LIMIT 0').format(sql.Identifier(table)))
            versions = self._db.execute('SELECT version FROM schema_version').fetchall()
            if [row[0] for row in versions] != [26]:
                raise ValueError('Session PostgreSQL schema version mismatch')
        finally:
            self.rollback()
