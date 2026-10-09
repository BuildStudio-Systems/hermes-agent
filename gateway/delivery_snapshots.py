"""Private, immutable copies of validated chat deliverables.

Execution workspaces need local POSIX semantics. Published bytes can instead
live on the configured document volume. This module does not authorize paths;
the gateway must apply its media policy before copying and before delivery.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile


def snapshot_file(source: Path, root: Path, *, owner: str, chat: str, limit: int) -> Path:
    """Copy a stable regular file, publishing atomically without replacing bytes.

    The content address includes its owner, conversation, and original name.
    Existing copies retain their timestamp so previously issued links stay valid.
    Snapshots are durable output, not the disposable top-level media cache.
    """
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError("Snapshot directory must be canonical")
    before = source.stat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    source_fd = os.open(source, flags)
    temporary = None
    try:
        opened = os.fstat(source_fd)
        # Opening an SMB file can refresh cached timestamps. Pin identity here;
        # compare content metadata on the same descriptor before/after reading.
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev, opened.st_ino
        ) != (before.st_dev, before.st_ino):
            raise ValueError("Source changed before snapshot")
        if opened.st_size > limit:
            raise ValueError("Snapshot exceeds delivery limit")
        fd, name = tempfile.mkstemp(prefix=".delivery-", suffix=".part", dir=root)
        temporary = Path(name)
        content_hash = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as output:
            while block := os.read(source_fd, min(1024 * 1024, limit + 1 - size)):
                size += len(block)
                if size > limit:
                    raise ValueError("Snapshot exceeds delivery limit")
                output.write(block)
                content_hash.update(block)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_fd)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns
        ) or size != opened.st_size:
            raise ValueError("Source changed during snapshot")
        key = hashlib.sha256(
            (owner + "\0" + chat + "\0" + source.name + "\0" + content_hash.hexdigest()).encode()
        ).hexdigest()
        # Preserve the suffix for the existing media policy, not a user path.
        suffix = source.suffix if len(source.suffix) <= 20 else ""
        target = root / (key + suffix)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            check_fd = os.open(target, flags)
            try:
                info = os.fstat(check_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                    raise ValueError("Existing snapshot is invalid")
                actual = hashlib.sha256()
                checked = 0
                while block := os.read(check_fd, min(1024 * 1024, size + 1 - checked)):
                    checked += len(block)
                    if checked > size:
                        raise ValueError("Existing snapshot grew during verification")
                    actual.update(block)
                if checked != size or actual.digest() != content_hash.digest():
                    raise ValueError("Existing snapshot content changed")
            finally:
                os.close(check_fd)
        return target
    finally:
        os.close(source_fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
