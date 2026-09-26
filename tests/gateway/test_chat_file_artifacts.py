from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from gateway.chat_file_artifacts import (
    DEFAULT_CHAT_FILE_TTL_SECONDS,
    ChatFileArtifact,
    ChatFileArtifactNotFound,
    ChatFileArtifactStore,
    ChatFileArtifactTooLarge,
)

CHAT_ONE = "11111111-1111-4111-8111-111111111111"
CHAT_TWO = "22222222-2222-4222-8222-222222222222"


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
    assert first.chat_id == ""


def test_dataclass_preserves_legacy_positional_arguments() -> None:
    artifact = ChatFileArtifact("a" * 32, "user-1", "/file", "file", "text/plain", 1, 2, 3)
    assert artifact.chat_id == ""


def test_publish_reuses_only_same_owner_and_business_chat(tmp_path: Path) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"report")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")
    same_chat = store.publish(str(source), owner_id="user-1", chat_id=CHAT_ONE)
    repeated = store.publish(str(source), owner_id="user-1", chat_id=CHAT_ONE)
    other_chat = store.publish(str(source), owner_id="user-1", chat_id=CHAT_TWO)
    unscoped = store.publish(str(source), owner_id="user-1")
    other_owner = store.publish(str(source), owner_id="user-2", chat_id=CHAT_ONE)

    assert repeated == same_chat
    assert len({item.artifact_id for item in (same_chat, other_chat, unscoped, other_owner)}) == 4
    for item in (same_chat, other_chat, unscoped, other_owner):
        assert store.resolve(item.artifact_id, owner_id=item.owner_id) == item
    assert same_chat.chat_id == CHAT_ONE
    assert other_chat.chat_id == CHAT_TWO
    assert unscoped.chat_id == ""
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(same_chat.artifact_id, owner_id="user-2")


@pytest.mark.parametrize(
    "chat_id",
    [
        " ", "chat-1", "../chat", "用户", "x" * 129,
        CHAT_ONE.replace("-", ""), "{" + CHAT_ONE + "}",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", " " + CHAT_ONE,
        CHAT_ONE + "\n", CHAT_ONE + "/file", None, 0,
    ],
)
def test_publish_rejects_noncanonical_chat_without_creating_metadata(
    tmp_path: Path, chat_id: object
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"report")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")
    with pytest.raises(ChatFileArtifactNotFound):
        store.publish(str(source), owner_id="user-1", chat_id=chat_id)
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_file_artifacts").fetchone()[0] == 0
    assert source.read_bytes() == b"report"


def test_owner_bound_legacy_registry_migrates_without_claiming_chat(
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    source = tmp_path / "report.txt"
    source.write_bytes(b"report")
    stat = source.stat()
    old_id = "b" * 32
    expires_at = time.time() + 3600
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute(
                """CREATE TABLE chat_file_artifacts (
                    artifact_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    path TEXT NOT NULL, filename TEXT NOT NULL,
                    content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, expires_at REAL NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX chat_file_artifacts_source "
                "ON chat_file_artifacts(owner_id, path, size_bytes, mtime_ns, expires_at)"
            )
            connection.execute(
                "INSERT INTO chat_file_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (old_id, "user-1", str(source.resolve()), source.name, "text/plain",
                 stat.st_size, stat.st_mtime_ns, expires_at),
            )

    store = ChatFileArtifactStore(database)
    legacy = store.resolve(old_id, owner_id="user-1")
    assert legacy.chat_id == ""
    assert legacy.expires_at == expires_at
    assert store.publish(str(source), owner_id="user-1") == legacy
    scoped = store.publish(str(source), owner_id="user-1", chat_id=CHAT_ONE)
    assert scoped.artifact_id != old_id
    assert scoped.chat_id == CHAT_ONE
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(old_id, owner_id="user-2")

    # Reinitialization is idempotent, including the upgraded lookup index.
    reopened = ChatFileArtifactStore(database)
    assert reopened.resolve(old_id, owner_id="user-1") == legacy
    assert reopened.publish(str(source), owner_id="user-1", chat_id=CHAT_ONE) == scoped
    with closing(sqlite3.connect(database)) as connection:
        index_columns = [
            row[2] for row in connection.execute("PRAGMA index_info(chat_file_artifacts_source)")
        ]
    assert index_columns == ["owner_id", "chat_id", "path", "size_bytes", "mtime_ns", "expires_at"]


def test_expiry_prunes_metadata_only_and_new_publication_gets_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "report.txt"
    source.write_bytes(b"report")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", ttl_seconds=60)
    monkeypatch.setattr("gateway.chat_file_artifacts.time.time", lambda: 1000)
    expired = store.publish(str(source), owner_id="user-1", chat_id=CHAT_ONE)
    monkeypatch.setattr("gateway.chat_file_artifacts.time.time", lambda: 1030)
    live = store.publish(str(source), owner_id="user-1", chat_id=CHAT_TWO)
    monkeypatch.setattr("gateway.chat_file_artifacts.time.time", lambda: 1061)

    # A successful resolve commits expiry housekeeping without deleting bytes.
    assert store.resolve(live.artifact_id, owner_id="user-1") == live
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute(
            "SELECT artifact_id FROM chat_file_artifacts ORDER BY artifact_id"
        ).fetchall() == [(live.artifact_id,)]
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(expired.artifact_id, owner_id="user-1")
    replacement = store.publish(str(source), owner_id="user-1", chat_id=CHAT_ONE)
    assert replacement.artifact_id not in {expired.artifact_id, live.artifact_id}
    assert replacement.chat_id == CHAT_ONE
    assert source.read_bytes() == b"report"


def test_upgraded_schema_accepts_legacy_writer_without_chat_column(tmp_path: Path) -> None:
    source = tmp_path / "legacy.txt"
    source.write_bytes(b"legacy")
    stat = source.stat()
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")
    artifact_id = "c" * 32
    with closing(sqlite3.connect(store.db_path)) as connection:
        with connection:
            connection.execute(
                """INSERT INTO chat_file_artifacts
                   (artifact_id, owner_id, path, filename, content_type,
                    size_bytes, mtime_ns, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, "user-1", str(source.resolve()), source.name,
                 "text/plain", stat.st_size, stat.st_mtime_ns, time.time() + 3600),
            )
    restored = store.resolve(artifact_id, owner_id="user-1")
    assert restored.chat_id == ""
    assert restored.path == str(source.resolve())


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


def test_same_source_has_distinct_artifacts_for_distinct_owners(
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")

    first = store.publish(str(source), owner_id="user-1")
    second = store.publish(str(source), owner_id="user-2")

    assert first.artifact_id != second.artifact_id
    assert store.resolve(first.artifact_id, owner_id="user-1") == first
    assert store.resolve(second.artifact_id, owner_id="user-2") == second
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(first.artifact_id, owner_id="user-2")
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(second.artifact_id, owner_id="user-1")


@pytest.mark.parametrize(
    "owner_id",
    ["", "   ", "../user", "user/name", "user name", "用户", "x" * 129],
)
def test_store_rejects_empty_or_invalid_owner(
    tmp_path: Path, owner_id: str
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")

    with pytest.raises(ChatFileArtifactNotFound):
        store.publish(str(source), owner_id=owner_id)

    artifact = store.publish(str(source), owner_id="valid-owner")
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(artifact.artifact_id, owner_id=owner_id)


def test_legacy_registry_migrates_without_reusing_unowned_links(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    stat = source.stat()
    legacy_artifact_id = "a" * 32
    with closing(sqlite3.connect(database)) as connection:
        with connection:
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
            connection.execute(
                """
                INSERT INTO chat_file_artifacts
                    (artifact_id, path, filename, content_type, size_bytes,
                     mtime_ns, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_artifact_id,
                    str(source.resolve()),
                    source.name,
                    "video/mp4",
                    stat.st_size,
                    stat.st_mtime_ns,
                    time.time() + 3600,
                ),
            )

    store = ChatFileArtifactStore(database)
    for owner_id in ("user-1", "user-2"):
        with pytest.raises(ChatFileArtifactNotFound):
            store.resolve(legacy_artifact_id, owner_id=owner_id)

    artifact = store.publish(str(source), owner_id="user-1")

    assert artifact.artifact_id != legacy_artifact_id
    assert store.resolve(artifact.artifact_id, owner_id="user-1") == artifact
    with closing(sqlite3.connect(database)) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(chat_file_artifacts)"
            ).fetchall()
        }
    assert {"owner_id", "chat_id"} <= columns


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission assertion")
def test_registry_uses_private_permissions(tmp_path: Path) -> None:
    registry = tmp_path / "private" / "index.sqlite3"
    ChatFileArtifactStore(registry)

    assert registry.parent.stat().st_mode & 0o777 == 0o700
    assert registry.stat().st_mode & 0o777 == 0o600
