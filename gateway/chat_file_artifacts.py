"""Persistent metadata registry for files linked from API chat responses.

The file bytes stay in their original, already validated location.  Only a
random identifier crosses the API boundary; the local path is kept in a small
SQLite index under the Hermes home directory.  Callers must validate the path
both before :meth:`publish` and after :meth:`resolve`.
"""

from __future__ import annotations

import os
import mimetypes
import re
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


# Generated media is cleaned from the Hermes cache after roughly one day.
# Keep links comfortably inside that retention boundary instead of promising a
# longer lifetime than the underlying file can provide.
DEFAULT_CHAT_FILE_TTL_SECONDS = 12 * 60 * 60
DEFAULT_CHAT_FILE_MAX_BYTES = 512 * 1024 * 1024
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ChatFileArtifactError(Exception):
    """Base error for chat file artifacts."""


class ChatFileArtifactNotFound(ChatFileArtifactError):
    """The artifact does not exist, expired, or its source file changed."""


class ChatFileArtifactTooLarge(ChatFileArtifactError):
    """The source file exceeds the configured delivery limit."""


@dataclass(frozen=True)
class ChatFileArtifact:
    artifact_id: str
    path: str
    filename: str
    content_type: str
    size_bytes: int
    mtime_ns: int
    expires_at: float


class ChatFileArtifactStore:
    """SQLite-backed registry of immutable file references."""

    def __init__(
        self,
        db_path: Path,
        *,
        ttl_seconds: int = DEFAULT_CHAT_FILE_TTL_SECONDS,
        max_bytes: int = DEFAULT_CHAT_FILE_MAX_BYTES,
    ) -> None:
        self.db_path = Path(db_path)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_bytes = max(1, int(max_bytes))
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._protect_path(self.db_path.parent, 0o700)
        self._initialize()
        self._protect_path(self.db_path, 0o600)

    @staticmethod
    def _protect_path(path: Path, mode: int) -> None:
        """Best-effort private permissions; systemd also runs with UMask=0077."""
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_file_artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS chat_file_artifacts_source "
                    "ON chat_file_artifacts(path, size_bytes, mtime_ns, expires_at)"
                )

    def _prune(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM chat_file_artifacts WHERE expires_at <= ?", (now,)
        )

    def publish(self, path: str) -> ChatFileArtifact:
        source = Path(path).resolve(strict=True)
        stat = source.stat()
        if not source.is_file():
            raise ChatFileArtifactNotFound("Source is not a regular file")
        if stat.st_size > self.max_bytes:
            raise ChatFileArtifactTooLarge(
                f"File exceeds the {self.max_bytes}-byte delivery limit"
            )

        now = time.time()
        expires_at = now + self.ttl_seconds
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"

        with closing(self._connect()) as connection:
            with connection:
                self._prune(connection, now)
                existing = connection.execute(
                    """
                    SELECT * FROM chat_file_artifacts
                    WHERE path = ? AND size_bytes = ? AND mtime_ns = ? AND expires_at > ?
                    ORDER BY expires_at DESC LIMIT 1
                    """,
                    (str(source), stat.st_size, stat.st_mtime_ns, now),
                ).fetchone()
                if existing is not None:
                    return self._from_row(existing)

                artifact_id = secrets.token_hex(16)
                connection.execute(
                    """
                    INSERT INTO chat_file_artifacts
                        (artifact_id, path, filename, content_type, size_bytes,
                         mtime_ns, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        str(source),
                        source.name,
                        content_type,
                        stat.st_size,
                        stat.st_mtime_ns,
                        expires_at,
                    ),
                )

        return ChatFileArtifact(
            artifact_id=artifact_id,
            path=str(source),
            filename=source.name,
            content_type=content_type,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            expires_at=expires_at,
        )

    def resolve(self, artifact_id: str) -> ChatFileArtifact:
        if not _ARTIFACT_ID_RE.fullmatch(str(artifact_id or "")):
            raise ChatFileArtifactNotFound("Invalid artifact id")

        now = time.time()
        with closing(self._connect()) as connection:
            with connection:
                self._prune(connection, now)
                row = connection.execute(
                    "SELECT * FROM chat_file_artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if row is None:
                    raise ChatFileArtifactNotFound("Artifact not found")
                artifact = self._from_row(row)

        try:
            source = Path(artifact.path).resolve(strict=True)
            stat = source.stat()
        except OSError as exc:
            raise ChatFileArtifactNotFound("Source file is unavailable") from exc
        if (
            not source.is_file()
            or stat.st_size != artifact.size_bytes
            or stat.st_mtime_ns != artifact.mtime_ns
        ):
            raise ChatFileArtifactNotFound("Source file changed after publication")
        return artifact

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ChatFileArtifact:
        return ChatFileArtifact(
            artifact_id=str(row["artifact_id"]),
            path=str(row["path"]),
            filename=str(row["filename"]),
            content_type=str(row["content_type"]),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
            expires_at=float(row["expires_at"]),
        )
