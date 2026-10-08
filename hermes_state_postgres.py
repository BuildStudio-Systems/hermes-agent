"""Native SessionDB engine, under integration test until public activation.

Business methods remain in SessionDB. This class replaces file recovery, WAL,
connection ownership and transaction handling with PostgreSQL operations.
"""
from __future__ import annotations

import atexit
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import threading
import time

from hermes_state import SessionDB, SessionCompressionInProgressError, _ensure_test_isolation
from hermes_cli.postgres_runtime import configuration
from hermes_cli.session_postgres import SessionConnection, SessionAdmissionBusy


class PostgresSessionDB(SessionDB):
    is_postgres = True

    @classmethod
    def _encode_content(cls, content):
        from hermes_cli.session_content_codec import encode_legacy
        return encode_legacy(super()._encode_content(content))

    @classmethod
    def _decode_content(cls, content):
        from hermes_cli.session_content_codec import decode_legacy
        return super()._decode_content(decode_legacy(content))

    def __init__(self, db_path: Path, read_only=False):
        self.db_path = Path(db_path)
        _ensure_test_isolation(self.db_path)
        self._settings = configuration('sessions', self.db_path)
        if self._settings is None:
            raise ValueError('PostgreSQL sessions must be explicitly selected for this profile')
        self.read_only = read_only
        self._lock = threading.Lock()
        self._conn = SessionConnection(self._settings, read_only=read_only, autocommit=True)
        self._token_queue = deque()
        self._token_queue_cond = threading.Condition(threading.Lock())
        self._token_writer_thread = None
        self._token_writer_stop = False
        self._token_writer_busy = False
        self._token_atexit_hook = None
        self._write_count = 0
        self._closed = False
        try:
            self._conn.validate_schema()
            # Fail before any business write if the separately versioned
            # native runtime function migration has not been installed.
            result = self._conn.execute("SELECT json_extract(?, ?)", ('{"_reset_from":"x"}', '$._reset_from')).fetchone()[0]
            if result != 'x':
                raise ValueError('Session PostgreSQL runtime functions failed validation')
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    @contextmanager
    def _read_ctx(self):
        # A fresh handle borrows from the same bounded process pool. Multiple
        # queries within a read context use one read-only transaction.
        conn = SessionConnection(self._settings, read_only=True)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _execute_write(self, fn, patience_s=None):
        if self.read_only:
            raise PermissionError('Session database is read-only')
        patience = self._WRITE_PATIENCE_S if patience_s is None else patience_s
        deadline = time.monotonic() + patience
        compression_deadline = None
        while True:
            try:
                with self._lock:
                    if self._conn is None:
                        self._conn = SessionConnection(self._settings, autocommit=True)
                    with self._conn:
                        result = fn(self._conn)
                self._write_count += 1
                return result
            except SessionAdmissionBusy:
                # This exception can only arise before business SQL executes.
                # Do not broaden this retry to query failures or COMMIT errors.
                if self._sleep_before_write_retry(deadline, patience):
                    continue
                raise
            except SessionCompressionInProgressError:
                # Preserve the existing short compression lease wait. The
                # transaction has rolled back before retrying business work.
                if compression_deadline is None:
                    compression_deadline = min(deadline, time.monotonic() + self._COMPRESSION_BUSY_WAIT_S)
                if self._sleep_before_write_retry(compression_deadline, self._COMPRESSION_BUSY_WAIT_S):
                    continue
                raise
            # Network/commit failures are deliberately NOT replayed. The
            # caller sees persistence failure instead of duplicate messages.

    def close(self):
        self._stop_token_writer()
        hook, self._token_atexit_hook = self._token_atexit_hook, None
        if hook is not None:
            atexit.unregister(hook)
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()
        self._closed = True

    def _message_column_names(self, conn):
        return [row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='messages' ORDER BY ordinal_position"
        ).fetchall()]

    def apply_telegram_topic_migration(self):
        # Tables/FK/indexes are preprovisioned offline. This metadata update
        # preserves explicit topic-mode opt-in without granting runtime DDL.
        self.set_meta('telegram_dm_topic_schema_version', '2')

    def logical_size_bytes(self):
        with self._read_ctx() as conn:
            return int(conn.execute(
                'SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) '
                'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                "WHERE n.nspname=current_schema() AND c.relkind='r'"
            ).fetchone()[0])

    def _search_messages_impl(self, *args, **kwargs):
        from hermes_cli.session_postgres_search import search
        return search(self, *args, **kwargs)

    def _describe_search_path(self, query):
        return 'postgres_trigram_regex'

    def fts_optimize_available(self):
        # SQLite shadow-table conversion is not applicable to native indexes.
        return False

    def fts_rebuild_status(self):
        return None

    def fts_cjk_rebuild_status(self):
        return None

    def fts_rebuild_step(self):
        return False

    def fts_cjk_rebuild_step(self):
        return False

    def _try_incremental_merge_fts(self):
        # PostgreSQL's native GIN maintenance is owned by autovacuum/DBA.
        return None

    def vacuum(self):
        raise RuntimeError('PostgreSQL vacuum is managed by the database maintenance role')

    def optimize_fts(self):
        raise RuntimeError('PostgreSQL search maintenance is managed by the database maintenance role')

    def rebuild_fts(self):
        raise RuntimeError('PostgreSQL search maintenance is managed by the database maintenance role')

    def optimize_fts_storage(self, *args, **kwargs):
        raise RuntimeError('SQLite FTS storage conversion does not apply to PostgreSQL')

    def purge_stale_tool_call_markers(self, *, dry_run=False, backup=True):
        report = super().purge_stale_tool_call_markers(dry_run=True, backup=False)
        if dry_run:
            return report
        if backup and report['rows_affected']:
            raise RuntimeError('Create and verify a DBserver PostgreSQL backup before using backup=False for this maintenance operation')
        return super().purge_stale_tool_call_markers(dry_run=False, backup=False)
