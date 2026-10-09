from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from gateway.chat_file_artifacts import ChatFileArtifactNotFound, ChatFileArtifactStore
from gateway.delivery_snapshots import snapshot_file


def test_published_workspace_file_survives_source_removal(tmp_path):
    source = tmp_path / "report 中文.pdf"
    source.write_bytes(b"%PDF-synthetic")
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", snapshot_root=tmp_path / "central")
    first = store.publish(str(source), owner_id="owner")
    repeated = store.publish(str(source), owner_id="owner")
    assert first == repeated
    assert first.filename == source.name and first.content_type == "application/pdf"
    source.unlink()
    assert Path(store.resolve(first.artifact_id, owner_id="owner").path).read_bytes() == b"%PDF-synthetic"
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(first.artifact_id, owner_id="other")


def test_snapshot_owner_chat_and_content_are_independent(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"first")
    options = dict(source=source, root=tmp_path / "central", limit=100)
    first = snapshot_file(**options, owner="one", chat="a")
    assert snapshot_file(**options, owner="two", chat="a") != first
    assert snapshot_file(**options, owner="one", chat="b") != first
    source.write_bytes(b"second")
    assert snapshot_file(**options, owner="one", chat="a") != first
    assert first.read_bytes() == b"first"


def test_concurrent_snapshot_does_not_replace_existing_timestamp(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"content" * 1000)
    options = dict(source=source, root=tmp_path / "central", owner="one", chat="a", limit=10000)
    first = snapshot_file(**options)
    before = first.stat().st_mtime_ns
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: snapshot_file(**options), range(12)))
    assert all(p == first for p in results)
    assert first.stat().st_mtime_ns == before
    assert len(list(options["root"].iterdir())) == 1


def test_snapshot_limit_and_failed_publish_leave_no_partial(tmp_path, monkeypatch):
    source = tmp_path / "report.txt"
    source.write_bytes(b"content")
    root = tmp_path / "central"
    with pytest.raises(ValueError, match="limit"):
        snapshot_file(source, root, owner="one", chat="", limit=3)
    assert not list(root.iterdir())
    def fail(*args, **kwargs):
        raise OSError("storage unavailable")
    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="unavailable"):
        snapshot_file(source, root, owner="one", chat="", limit=100)
    assert not list(root.iterdir()) and source.read_bytes() == b"content"


@pytest.mark.skipif(os.name != "posix", reason="Linux symlink and permission semantics")
def test_snapshot_rejects_links_and_keeps_private_files(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"content")
    root = tmp_path / "central"
    result = snapshot_file(source, root, owner="one", chat="", limit=100)
    assert result.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    result.unlink()
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    result.symlink_to(secret)
    with pytest.raises(OSError):
        snapshot_file(source, root, owner="one", chat="", limit=100)
    assert secret.read_bytes() == b"private"
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        snapshot_file(source, linked, owner="one", chat="", limit=100)


def test_invalid_owner_does_not_copy_workspace_bytes(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"content")
    root = tmp_path / "central"
    store = ChatFileArtifactStore(tmp_path / "index.sqlite3", snapshot_root=root)
    with pytest.raises(ChatFileArtifactNotFound):
        store.publish(str(source), owner_id="../other")
    assert not root.exists()


def test_mutated_snapshot_is_not_reused(tmp_path):
    source = tmp_path / "report.txt"
    source.write_bytes(b"content")
    root = tmp_path / "central"
    result = snapshot_file(source, root, owner="one", chat="", limit=100)
    result.write_bytes(b"changed")
    with pytest.raises(ValueError, match="content changed"):
        snapshot_file(source, root, owner="one", chat="", limit=100)
    assert not list(root.glob("*.part"))


def test_smb_timestamp_refresh_does_not_masquerade_as_inode_replacement(tmp_path, monkeypatch):
    from types import SimpleNamespace
    source = tmp_path / "report.txt"
    source.write_bytes(b"content")
    original = Path.stat
    def cached(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == source:
            return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
                                   st_size=value.st_size, st_mtime_ns=value.st_mtime_ns + 100)
        return value
    monkeypatch.setattr(Path, "stat", cached)
    result = snapshot_file(source, tmp_path / "central", owner="one", chat="", limit=100)
    assert result.read_bytes() == b"content"
