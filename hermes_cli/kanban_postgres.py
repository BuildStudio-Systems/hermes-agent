"""Native PostgreSQL board connections, transactions and dispatcher locks.

Board rows are isolated by PostgreSQL RLS. Provision schema/roles offline;
runtime never creates tables and never converts arbitrary SQLite SQL.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import shutil
import uuid

from hermes_constants import get_hermes_home
from hermes_cli.postgres_runtime import Connection, configuration, _parameters, SCOPES

TABLES = SCOPES['kanban'][1][2:]


def board_key(path):
    home = get_hermes_home().resolve()
    path = Path(path).resolve()
    if path == home / "kanban.db":
        return "default"
    try:
        relative = path.relative_to(home / "kanban" / "boards")
    except ValueError as exc:
        raise ValueError("PostgreSQL board path is outside the active profile") from exc
    parts = relative.parts
    if not parts or parts[-1] != "kanban.db" or len(parts) not in (2, 3):
        raise ValueError("Invalid PostgreSQL board path")
    if len(parts) == 3 and parts[0] != "_archived":
        raise ValueError("Invalid archived board path")
    if len(parts) == 2 and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", parts[0]):
        raise ValueError("Invalid PostgreSQL board identifier")
    if any(not re.fullmatch(r"[a-z0-9_-]{1,150}", part) for part in parts[:-1]):
        raise ValueError("Invalid PostgreSQL board identifier")
    return "/".join(parts[:-1])


def connect_if_configured(path):
    settings = configuration("kanban")
    if settings is None:
        return None
    return KanbanConnection(settings, board_key(path))


class KanbanConnection(Connection):
    def __init__(self, settings, board):
        self.board_name = board
        self.board = None
        self.home = Path(settings['profile'])
        super().__init__(settings, "kanban")
        self._write_lock = int.from_bytes(hashlib.sha256(
            (self.schema + ":board:" + self.board).encode()).digest()[:8], "big", signed=True)

    def _configure(self):
        self._db.execute("SELECT set_config('buildstudio.kanban_board', '', false)")
        super()._configure()
        with self.lifecycle_lock():
            self.recover_filesystem_operations()
            if self.board is None:
                self._db.execute('INSERT INTO board_registry(id,name) VALUES(%s,%s) ON CONFLICT(name) DO NOTHING',
                                 (uuid.uuid4().hex, self.board_name))
                self.board = self._db.execute('SELECT id FROM board_registry WHERE name=%s AND state=\'live\'',
                                             (self.board_name,)).fetchone()[0]
            elif not self._db.execute("SELECT 1 FROM board_registry WHERE id=%s AND state='live'", (self.board,)).fetchone():
                raise RuntimeError('The PostgreSQL board was deleted')
        self._db.execute("SELECT set_config('buildstudio.kanban_board', %s, false)", (self.board,))

    @contextmanager
    def lifecycle_lock(self):
        key = int.from_bytes(hashlib.sha256((self.schema + ':lifecycle').encode()).digest()[:8], 'big', signed=True)
        self._db.execute('SELECT pg_advisory_lock(%s)', (key,))
        try:
            yield
        finally:
            if not self._db.closed:
                try:
                    self._db.execute('SELECT pg_advisory_unlock(%s)', (key,))
                except Exception:
                    self._db.close()
                    raise

    def _directory(self, name):
        # Journal values are database data, never trusted paths.
        parts = name.split('/')
        valid = (len(parts) == 1 and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name)) or (
            len(parts) == 2 and parts[0] in {'_archived','_deleted'} and re.fullmatch(r'[a-z0-9_-]{1,150}',parts[1]))
        if not valid or name == 'default':
            raise ValueError('Invalid PostgreSQL board lifecycle path')
        root = (self.home/'kanban'/'boards').resolve()
        path = root.joinpath(*parts)
        if path.resolve() != path or not path.resolve().is_relative_to(root):
            raise ValueError('Board lifecycle path cannot traverse a symlink')
        return path

    def recover_filesystem_operations(self):
        """Reconcile an interrupted rename using the committed DB identity.

        Called under the database lifecycle lock, before resolving any name.
        A lost COMMIT reply never triggers an unverified reverse operation.
        """
        rows = self._db.execute('SELECT o.*, b.name AS live_name, b.state FROM board_fs_operations o JOIN board_registry b ON b.id=o.board_id').fetchall()
        for row in rows:
            source, target = self._directory(row['source_name']), self._directory(row['target_name'])
            committed = row['live_name'] == row['target_name']
            if not committed and row['live_name'] != row['source_name']:
                raise RuntimeError('Board filesystem journal conflicts with database identity')
            desired, other = (target, source) if committed else (source, target)
            if desired.exists() and other.exists():
                raise RuntimeError('Both board lifecycle paths exist; recovery requires inspection')
            if other.exists():
                desired.parent.mkdir(parents=True, exist_ok=True)
                other.rename(desired)
            if committed and row['kind'] == 'delete':
                if row['state'] != 'deleted':
                    raise RuntimeError('Board delete journal has no committed tombstone')
                if target.exists():
                    shutil.rmtree(target)
            elif not desired.exists():
                raise RuntimeError('Board lifecycle directory is missing')
            self._db.execute('DELETE FROM board_fs_operations WHERE id=%s', (row['id'],))

    def remove_board(self, target_name, *, archive):
        if self.in_transaction:
            raise RuntimeError('Board removal cannot run inside another transaction')
        source, target = self._directory(self.board_name), self._directory(target_name)
        with self.lifecycle_lock():
            self.recover_filesystem_operations()
            if not source.is_dir() or target.exists():
                raise ValueError('Board lifecycle source or destination changed')
            operation = uuid.uuid4().hex
            # Durable intent precedes any filesystem change.
            self._db.execute('INSERT INTO board_fs_operations VALUES(%s,%s,%s,%s,%s)',
                             (operation,self.board,self.board_name,target_name,'archive' if archive else 'delete'))
            try:
                with self.write_transaction():
                    if not archive:
                        for table in reversed(TABLES):
                            self.execute('DELETE FROM ' + table)
                    changed = self.execute('UPDATE board_registry SET name=?, state=? WHERE id=? AND name=?',
                                           (target_name,'live' if archive else 'deleted',self.board,self.board_name))
                    if changed.rowcount != 1:
                        raise RuntimeError('Board identity changed during removal')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(target)
            except Exception:
                if not self._db.closed:
                    self.recover_filesystem_operations()
                raise
            self.recover_filesystem_operations()
        return target

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self._db.info.transaction_status != TransactionStatus.IDLE

    def execute(self, query, parameters=None):
        # Transaction ownership belongs to write_txn. In particular a failed
        # nested operation must roll back its savepoint, not the outer write.
        self._ensure_connection()
        return self._db.execute(query if parameters is None else _parameters(query), parameters)

    def __enter__(self):
        # Kanban's legacy connection is autocommit. Entering a connection
        # context must not start an outer transaction around write_txn helpers.
        self._ensure_connection()
        return self

    @contextmanager
    def write_transaction(self, allow_nested=False):
        self._ensure_connection()
        nested = self.in_transaction
        if nested and not allow_nested:
            raise RuntimeError("Nested board writes require explicit savepoint permission")
        self._transaction_started = True
        try:
            with self._db.transaction():
                if not nested:
                    self._db.execute("SELECT pg_advisory_xact_lock(%s)", (self._write_lock,))
                yield self
        finally:
            self._transaction_started = nested

    @contextmanager
    def dispatch_lock(self):
        key = int.from_bytes(hashlib.sha256(
            (self.schema + ":dispatcher:" + self.board).encode()).digest()[:8], "big", signed=True)
        acquired = self._db.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    self._db.execute("SELECT pg_advisory_unlock(%s)", (key,))
                except Exception:
                    # Never return a pooled connection with a session lock.
                    self._db.close()
                    raise


def insert_id(connection, query, parameters):
    if getattr(connection, "is_postgres", False):
        return int(connection.execute(query.rstrip().rstrip(";") + " RETURNING id", parameters).fetchone()[0])
    return int(connection.execute(query, parameters).lastrowid or 0)
