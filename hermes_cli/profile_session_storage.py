"""Storage routing for existing, explicitly addressed local profiles.

Callers retain their existing authorization checks. This module only resolves
the named profile's own backend; it does not inherit the active profile's
credentials, change process environment, or create missing local databases.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@contextmanager
def _profile_scope(profile):
    from hermes_cli import profiles
    name = profiles.normalize_profile_name(profile or 'default')
    profiles.validate_profile_name(name)
    if not profiles.profile_exists(name):
        raise ValueError('Session target profile does not exist')
    home = profiles.get_profile_dir(name).resolve()
    token = set_hermes_home_override(home)
    try:
        yield home / 'state.db'
    finally:
        reset_hermes_home_override(token)


def open_profile_session_db(profile, *, read_only=True):
    """Return a caller-owned SessionDB, or None for an uninitialized profile."""
    from hermes_cli.postgres_runtime import configuration
    with _profile_scope(profile) as path:
        if configuration('sessions', path) is not None:
            from hermes_state_postgres import PostgresSessionDB
            return PostgresSessionDB(path, read_only=read_only)
        if not path.exists():
            return None
        from hermes_state import SessionDB
        return SessionDB(db_path=path, read_only=read_only)


@contextmanager
def profile_session_connection(profile, *, read_only=True):
    """Close on every path; PG failure never falls back to a retained file."""
    from hermes_cli.session_postgres import connection_for
    with _profile_scope(profile) as path:
        conn = connection_for(path, read_only=read_only)
        if conn is None and path.exists():
            mode = 'ro' if read_only else 'rw'
            conn = sqlite3.connect(Path(path).as_uri() + '?mode=' + mode,
                                   uri=True, timeout=5)
    if conn is None:
        yield None
        return
    try:
        if getattr(conn, 'is_postgres', False):
            conn.validate_schema()
        with conn:
            yield conn
    finally:
        conn.close()
