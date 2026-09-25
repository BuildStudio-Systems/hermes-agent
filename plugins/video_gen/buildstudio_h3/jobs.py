"""Session-owned receipts for local H3 jobs; never store coordinator credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from gateway.chat_file_artifacts import normalize_chat_file_owner
from gateway.session_context import get_bound_session_env
from hermes_constants import get_hermes_home

_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


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


def owned_receipt(job_id: str, scope: str) -> dict:
    path = _receipt_path(job_id)
    if path.is_symlink():
        raise ValueError("Video job is unavailable in this conversation")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Video job is unavailable in this conversation") from None
    if not isinstance(data, dict) or data.get("scope") != scope or not isinstance(data.get("options"), dict):
        raise ValueError("Video job is unavailable in this conversation")
    return data["options"]
