from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gateway.chat_file_artifacts import (
    ChatFileArtifactNotFound,
    ChatFileArtifactStore,
    ChatFileArtifactTooLarge,
)


def test_publish_reuses_unchanged_source_and_resolves(tmp_path: Path) -> None:
    source = tmp_path / "demo video.mp4"
    source.write_bytes(b"video-bytes")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", ttl_seconds=3600)

    first = store.publish(str(source))
    second = store.publish(str(source))

    assert first.artifact_id == second.artifact_id
    assert store.resolve(first.artifact_id).path == str(source.resolve())
    assert first.content_type == "video/mp4"


def test_resolve_rejects_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"one")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")
    artifact = store.publish(str(source))

    source.write_bytes(b"different")
    os.utime(source, ns=(time.time_ns(), time.time_ns()))

    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(artifact.artifact_id)


def test_publish_enforces_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.zip"
    source.write_bytes(b"1234")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", max_bytes=3)

    with pytest.raises(ChatFileArtifactTooLarge):
        store.publish(str(source))


def test_resolve_rejects_unminted_id(tmp_path: Path) -> None:
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3")

    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve("../../etc/passwd")
