from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from gateway.chat_file_artifacts import (
    DEFAULT_CHAT_FILE_TTL_SECONDS,
    ChatFileArtifactNotFound,
    ChatFileArtifactStore,
    ChatFileArtifactTooLarge,
)


def test_publish_reuses_unchanged_source_and_resolves(tmp_path: Path) -> None:
    source = tmp_path / "demo video.mp4"
    source.write_bytes(b"video-bytes")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", ttl_seconds=3600)

    first = store.publish(str(source), owner_id="user-1")
    second = store.publish(str(source), owner_id="user-1")

    assert first.artifact_id == second.artifact_id
    assert store.resolve(first.artifact_id, owner_id="user-1").path == str(
        source.resolve()
    )
    assert first.content_type == "video/mp4"


def test_resolve_rejects_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"one")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")
    artifact = store.publish(str(source), owner_id="user-1")

    source.write_bytes(b"different")
    os.utime(source, ns=(time.time_ns(), time.time_ns()))

    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(artifact.artifact_id, owner_id="user-1")


def test_publish_enforces_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.zip"
    source.write_bytes(b"1234")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", max_bytes=3)

    with pytest.raises(ChatFileArtifactTooLarge):
        store.publish(str(source), owner_id="user-1")


def test_resolve_rejects_unminted_id(tmp_path: Path) -> None:
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")

    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve("../../etc/passwd", owner_id="user-1")


def test_default_ttl_stays_inside_media_cache_retention() -> None:
    assert DEFAULT_CHAT_FILE_TTL_SECONDS == 12 * 60 * 60


def test_database_connections_are_closed_after_operations(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    database = tmp_path / "index.sqlite3"
    store = ChatFileArtifactStore(database)

    artifact = store.publish(str(source), owner_id="user-1")
    store.resolve(artifact.artifact_id, owner_id="user-1")

    moved = tmp_path / "moved.sqlite3"
    database.replace(moved)
    moved.replace(database)


def test_artifact_is_bound_to_its_owner(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")
    artifact = store.publish(str(source), owner_id="user-1")

    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(artifact.artifact_id, owner_id="user-2")


def test_legacy_registry_migrates_without_reusing_unowned_links(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE chat_file_artifacts (
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
            "CREATE INDEX chat_file_artifacts_source "
            "ON chat_file_artifacts(path, size_bytes, mtime_ns, expires_at)"
        )

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    store = ChatFileArtifactStore(database)
    artifact = store.publish(str(source), owner_id="user-1")

    assert store.resolve(artifact.artifact_id, owner_id="user-1") == artifact
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(chat_file_artifacts)"
            ).fetchall()
        }
    assert "owner_id" in columns


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission assertion")
def test_registry_uses_private_permissions(tmp_path: Path) -> None:
    registry = tmp_path / "private" / "index.sqlite3"
    ChatFileArtifactStore(registry)

    assert registry.parent.stat().st_mode & 0o777 == 0o700
    assert registry.stat().st_mode & 0o777 == 0o600
