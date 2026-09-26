"""Session-owned receipts for local H3 jobs; never store coordinator credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path

from gateway.chat_file_artifacts import normalize_chat_file_owner
from gateway.session_context import get_bound_session_env
from hermes_constants import get_hermes_home

_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_delivery_locks = weakref.WeakValueDictionary()
_delivery_locks_guard = threading.Lock()


def validate_job_id(value: object) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise ValueError("Invalid video job id")
    return value


def current_scope() -> tuple[str, str]:
    """Return a stable scope digest and the coordinator's authenticated owner."""
    platform = get_bound_session_env("HERMES_SESSION_PLATFORM")
    owner = normalize_chat_file_owner(get_bound_session_env("HERMES_SESSION_USER_ID"))
    chat = get_bound_session_env("HERMES_SESSION_CHAT_ID")
    session = chat or get_bound_session_env("HERMES_SESSION_KEY") or get_bound_session_env("HERMES_SESSION_ID")
    if not session or (platform and not owner):
        raise ValueError("An authenticated user and conversation context are required for video jobs")
    scope = hashlib.sha256(json.dumps([
        platform, owner, session, get_bound_session_env("HERMES_SESSION_SCOPE_ID"),
        get_bound_session_env("HERMES_SESSION_THREAD_ID"),
    ], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    # Local CLI sessions have no Web user. A private namespace keeps them
    # owner-bound at the coordinator instead of creating unowned jobs.
    return scope, owner or "hermes-h3-" + scope


def _receipt_path(job_id: str) -> Path:
    return get_hermes_home() / "cache" / "videos" / "buildstudio_h3" / (validate_job_id(job_id) + ".json")


def save_receipt(job_id: str, scope: str, options: dict) -> None:
    path = _receipt_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A repeated id must never replace another conversation's ownership.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"scope": scope, "options": options}, stream, ensure_ascii=True)


def _owned_receipt(job_id: str, scope: str) -> dict:
    path = _receipt_path(job_id)
    if path.is_symlink():
        raise ValueError("Video job is unavailable in this conversation")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Video job is unavailable in this conversation") from None
    if not isinstance(data, dict) or data.get("scope") != scope or not isinstance(data.get("options"), dict):
        raise ValueError("Video job is unavailable in this conversation")
    return data


def owned_receipt(job_id: str, scope: str) -> dict:
    return _owned_receipt(job_id, scope)["options"]


@contextmanager
def delivery_lock(job_id: str):
    """Coalesce same-job downloads in this process without blocking a turn.

    Different profiles/jobs do not share a lock. Weak values discard idle
    locks rather than retaining one entry for every historical video.
    """
    key = str(_receipt_path(job_id).resolve())
    with _delivery_locks_guard:
        lock = _delivery_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _delivery_locks[key] = lock
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def _video_identity(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Cached video is not a private regular file")
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "device": info.st_dev, "inode": info.st_ino}


def delivery_prefix(job_id: str, scope: str) -> str:
    digest = hashlib.sha256((scope + ":" + validate_job_id(job_id)).encode()).hexdigest()
    return "buildstudio_h3_" + digest + "_"


def cached_delivery(job_id: str, scope: str, source: str, max_bytes: int) -> Path | None:
    """Reuse only an unchanged owned file; never trust a path from a receipt."""
    delivery = _owned_receipt(job_id, scope).get("delivery")
    if not isinstance(delivery, dict) or delivery.get("source") != source:
        return None
    name = delivery.get("name")
    pattern = re.escape(delivery_prefix(job_id, scope)) + r"[A-Za-z0-9_-]+\.mp4"
    if not isinstance(name, str) or not re.fullmatch(pattern, name):
        return None
    path = _receipt_path(job_id).parent.parent / name
    try:
        identity = _video_identity(path)
    except (OSError, ValueError):
        return None
    if identity != delivery.get("identity") or not 0 < identity["size"] <= max_bytes:
        return None
    return path


def remember_delivery(job_id: str, scope: str, source: str, video: Path) -> None:
    """Publish completed-download metadata atomically, preserving ownership."""
    path = _receipt_path(job_id)
    data = _owned_receipt(job_id, scope)
    if (video.parent.resolve() != path.parent.parent.resolve()
            or not video.name.startswith(delivery_prefix(job_id, scope))):
        raise ValueError("Video cache path is outside the profile")
    data["delivery"] = {"source": source, "name": video.name, "identity": _video_identity(video)}
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
