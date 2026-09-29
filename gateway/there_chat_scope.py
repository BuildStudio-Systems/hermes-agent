"""Business-chat identity supplied by the authenticated THERE Web adapter.

This identifies execution state, not a source of conversation history. THERE's
PostgreSQL-backed request remains authoritative; never reload state.db history
merely because a business chat was supplied.
"""

from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import uuid

from gateway.chat_file_artifacts import normalize_chat_file_owner


CHAT_HEADER = "X-BuildStudio-Chat-Id"
OWNER_HEADER = "X-BuildStudio-User-Id"
DEVICE_HEADER = "X-BuildStudio-Device-Capability"
_device_capability = ContextVar("there_device_capability", default="")
_device_evidence = ContextVar("there_device_evidence", default=None)


def current_device_capability():
    return _device_capability.get()


def record_device_result(arguments, response):
    evidence = _device_evidence.get()
    if evidence is not None:
        evidence.record(arguments, response)


@contextmanager
def device_capability_scope(value):
    from gateway.there_device_evidence import DeviceEvidence
    evidence = DeviceEvidence()
    token = _device_capability.set(value)
    evidence_token = _device_evidence.set(evidence)
    try:
        yield evidence
    finally:
        _device_evidence.reset(evidence_token)
        _device_capability.reset(token)


@dataclass(frozen=True)
class ThereChatScope:
    owner_id: str
    chat_id: str
    device_capability: str = field(default="", repr=False, compare=False)

    @property
    def session_id(self) -> str:
        payload = json.dumps([self.owner_id, self.chat_id], separators=(",", ":"))
        return "there-v1-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def matches_session(self, row: dict) -> bool:
        return (
            row.get("source") == "api_server"
            and row.get("user_id") == self.owner_id
            and row.get("chat_id") == self.chat_id
        )


def parse_there_chat_scope(headers, *, authenticated_key_configured: bool):
    """Validate an optional trusted header, failing closed on mixed histories.

    The caller MUST authenticate the API request before using this result.
    Only Web's final server-side proxy may mint this header after checking the
    saved chat's owner; it is not accepted from client body metadata.
    """
    raw = headers.get(CHAT_HEADER)
    if raw is None:
        return None
    if not authenticated_key_configured:
        raise ValueError("THERE chat binding requires API key authentication")
    owner = normalize_chat_file_owner(headers.get(OWNER_HEADER))
    if not owner:
        raise ValueError("THERE chat binding requires a valid owner")
    try:
        if str(uuid.UUID(raw)) != raw:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Invalid THERE chat ID") from None
    if any(name in headers for name in ("X-Hermes-Session-Id", "X-Hermes-Session-Key")):
        raise ValueError("THERE chat binding cannot override execution history or memory scope")
    proof = headers.get(DEVICE_HEADER, "")
    if not isinstance(proof, str) or len(proof) > 4096:
        raise ValueError("Invalid device capability")
    # This parser does not grant device access: the private broker verifies the
    # Web signature, owner allowlist, scope and expiry on every operation.
    return ThereChatScope(owner_id=owner, chat_id=raw, device_capability=proof)
