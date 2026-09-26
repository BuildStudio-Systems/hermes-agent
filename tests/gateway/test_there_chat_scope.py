"""THERE business identity: real HTTP adapter and real temporary SessionDB."""

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict
import pytest

from gateway.config import PlatformConfig
from gateway.chat_file_artifacts import ChatFileArtifactStore
from gateway.platforms import api_server
from gateway.session_context import get_bound_session_env
from gateway.there_chat_scope import CHAT_HEADER, OWNER_HEADER, ThereChatScope, parse_there_chat_scope
from hermes_state import SessionDB

CHAT_A = "10000000-0000-4000-8000-000000000001"
CHAT_B = "10000000-0000-4000-8000-000000000002"
USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def headers(owner="admin-a", chat=CHAT_A, **extra):
    return {"Authorization": "Bearer synthetic-key", OWNER_HEADER: owner, CHAT_HEADER: chat, **extra}


def body(system="synthetic policy", stream=False):
    return {"messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": "first synthetic question"},
        {"role": "assistant", "content": "prior answer from PostgreSQL"},
        {"role": "user", "content": "next synthetic question"},
    ], "stream": stream}


@asynccontextmanager
async def server(tmp_path, monkeypatch, *, key="synthetic-key"):
    db = SessionDB(tmp_path / "execution.sqlite3")
    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    monkeypatch.setattr(adapter, "_ensure_session_db_async", AsyncMock(return_value=db))
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: db)
    monkeypatch.setattr(api_server, "_idem_cache", api_server._IdempotencyCache())
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    try:
        async with TestClient(TestServer(app)) as client:
            yield adapter, db, client
    finally:
        await adapter.disconnect()
        db.close()


@pytest.mark.parametrize("bad", ["", "../other", CHAT_A.upper().replace("1000", "ABCD", 1), CHAT_A.replace("-", ""), " " + CHAT_A, "not-a-uuid"])
def test_rejects_noncanonical_chat(bad):
    with pytest.raises(ValueError):
        parse_there_chat_scope(CIMultiDict(headers(chat=bad)), authenticated_key_configured=True)


@pytest.mark.parametrize("legacy", ["X-Hermes-Session-Id", "X-Hermes-Session-Key"])
def test_rejects_mixed_history_even_empty_header(legacy):
    with pytest.raises(ValueError):
        parse_there_chat_scope(CIMultiDict(headers(**{legacy: ""})), authenticated_key_configured=True)


def test_identity_scopes_and_case_insensitive_headers():
    scope = parse_there_chat_scope(CIMultiDict({k.lower(): v for k, v in headers().items()}), authenticated_key_configured=True)
    assert scope == ThereChatScope("admin-a", CHAT_A)
    assert scope.session_id == ThereChatScope("admin-a", CHAT_A).session_id
    assert len({scope.session_id, ThereChatScope("admin-b", CHAT_A).session_id,
                ThereChatScope("admin-a", CHAT_B).session_id}) == 3
    assert parse_there_chat_scope({}, authenticated_key_configured=False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_saved_chat_stable_across_prompt_changes_but_history_from_web(tmp_path, monkeypatch, stream):
    async with server(tmp_path, monkeypatch) as (adapter, db, client):
        scope = ThereChatScope("admin-a", CHAT_A)
        db.create_session(scope.session_id, "api_server", user_id=scope.owner_id, chat_id=scope.chat_id)
        # Any history read is an error: the Web request remains authoritative.
        monkeypatch.setattr(db, "get_messages_as_conversation", lambda *_: pytest.fail("reloaded internal history"))
        calls = []

        async def run(**kwargs):
            calls.append((kwargs, api_server._api_request_there_chat.get()))
            if kwargs.get("stream_delta_callback"):
                kwargs["stream_delta_callback"]("synthetic answer")
            return {"final_response": "synthetic answer", "messages": []}, USAGE

        monkeypatch.setattr(adapter, "_run_agent", run)
        for policy in ("initial policy", "changed RAG and language policy"):
            response = await client.post("/v1/chat/completions", headers=headers(), json=body(policy, stream))
            assert response.status == 200, await response.text()
            assert "synthetic answer" in await response.text()
            assert response.headers["X-Hermes-Session-Id"] == scope.session_id
        for kwargs, captured_scope in calls:
            assert captured_scope == scope
            assert kwargs["session_id"] == kwargs["gateway_session_key"] == scope.session_id
            assert kwargs["conversation_history"] == body()["messages"][1:-1]
        assert calls[1][0]["ephemeral_system_prompt"] == "changed RAG and language policy"
        assert api_server._api_request_there_chat.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,chat", [(None, None), ("admin-b", CHAT_A), ("admin-a", CHAT_B)])
async def test_never_adopts_conflicting_or_unowned_session(tmp_path, monkeypatch, owner, chat):
    async with server(tmp_path, monkeypatch) as (adapter, db, client):
        scope = ThereChatScope("admin-a", CHAT_A)
        db.create_session(scope.session_id, "api_server", user_id=owner, chat_id=chat)
        run = AsyncMock()
        monkeypatch.setattr(adapter, "_run_agent", run)
        response = await client.post("/v1/chat/completions", headers=headers(), json=body())
        assert response.status == 409
        run.assert_not_called()
        assert db.get_session(scope.session_id)["user_id"] == owner


@pytest.mark.asyncio
async def test_cross_chat_user_idempotency_and_concurrent_contexts(tmp_path, monkeypatch):
    async with server(tmp_path, monkeypatch) as (adapter, _db, client):
        seen = []

        async def run(**kwargs):
            scope = api_server._api_request_there_chat.get()
            await asyncio.sleep(0)
            assert api_server._api_request_there_chat.get() == scope
            seen.append(scope)
            return {"final_response": scope.owner_id + ":" + scope.chat_id}, USAGE

        monkeypatch.setattr(adapter, "_run_agent", run)

        async def submit(owner, chat):
            response = await client.post("/v1/chat/completions", headers=headers(owner, chat, **{"Idempotency-Key": "same-retry"}), json=body())
            assert response.status == 200
            return (await response.json())["choices"][0]["message"]["content"]

        values = await asyncio.gather(submit("admin-a", CHAT_A), submit("admin-b", CHAT_A), submit("admin-a", CHAT_B))
        assert len(set(values)) == len(seen) == 3
        assert await submit("admin-a", CHAT_A) == values[0]
        assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "read", "write"])
async def test_origin_must_be_durable_before_any_execution(tmp_path, monkeypatch, failure):
    async with server(tmp_path, monkeypatch) as (adapter, db, client):
        run = AsyncMock()
        monkeypatch.setattr(adapter, "_run_agent", run)
        def fail(*_args, **_kwargs):
            raise RuntimeError("synthetic storage failure")
        if failure == "missing":
            monkeypatch.setattr(adapter, "_ensure_session_db_async", AsyncMock(return_value=None))
        elif failure == "read":
            monkeypatch.setattr(db, "get_session", fail)
        else:
            monkeypatch.setattr(db, "create_session", fail)
        response = await client.post("/v1/chat/completions", headers=headers(), json=body())
        assert response.status == 503
        assert "synthetic storage failure" not in await response.text()
        run.assert_not_called()
        assert adapter._pending_agent_requests == 0


@pytest.mark.asyncio
async def test_legacy_caller_cannot_construct_mapped_idempotency_key(tmp_path, monkeypatch):
    async with server(tmp_path, monkeypatch) as (adapter, _db, client):
        run = AsyncMock(side_effect=[({"final_response": "mapped output"}, USAGE),
                                     ({"final_response": "legacy output"}, USAGE)])
        monkeypatch.setattr(adapter, "_run_agent", run)
        response = await client.post("/v1/chat/completions", headers=headers(**{"Idempotency-Key": "same-retry"}), json=body())
        assert response.status == 200
        legacy_key = "there-v1:" + hashlib.sha256(json.dumps([
            None, ThereChatScope("admin-a", CHAT_A).session_id, "same-retry"
        ], separators=(",", ":")).encode()).hexdigest()
        response = await client.post("/v1/chat/completions", headers={
            "Authorization": "Bearer synthetic-key", "Idempotency-Key": legacy_key,
        }, json=body())
        assert response.status == 200
        assert (await response.json())["choices"][0]["message"]["content"] == "legacy output"
        assert run.await_count == 2


@pytest.mark.asyncio
async def test_context_resets_after_handler_exception(tmp_path, monkeypatch):
    async with server(tmp_path, monkeypatch) as (adapter, _db, client):
        seen = []
        async def run(**kwargs):
            seen.append(api_server._api_request_there_chat.get())
            if len(seen) == 1:
                raise RuntimeError("synthetic failure")
            return {"final_response": "legacy answer"}, USAGE
        monkeypatch.setattr(adapter, "_run_agent", run)
        response = await client.post("/v1/chat/completions", headers=headers(), json=body())
        assert response.status == 500
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer synthetic-key"}, json=body())
        assert response.status == 200
        assert seen == [ThereChatScope("admin-a", CHAT_A), None]
        assert api_server._api_request_there_chat.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_artifacts_persist_chat_and_return_stored_header_not_download_input(tmp_path, monkeypatch, stream):
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
    monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "0")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    source = tmp_path / "synthetic.txt"
    source.write_bytes(b"synthetic artifact")
    async with server(tmp_path, monkeypatch) as (adapter, db, client):
        adapter._file_delivery_enabled = True
        adapter._file_delivery_public_base_url = "/api/v1/agent-files"
        store = ChatFileArtifactStore(tmp_path / "artifacts.sqlite3")
        monkeypatch.setattr(adapter, "_get_chat_file_store", lambda: store)
        async def run(**kwargs):
            row = db.get_session(kwargs["session_id"])
            assert ThereChatScope("admin-a", CHAT_A).matches_session(row)
            text = f"MEDIA:{source}"
            if kwargs.get("stream_delta_callback"):
                kwargs["stream_delta_callback"](text)
            return {"final_response": text, "messages": []}, USAGE
        monkeypatch.setattr(adapter, "_run_agent", run)
        response = await client.post("/v1/chat/completions", headers=headers(), json=body(stream=stream))
        assert response.status == 200
        output = await response.text()
        match = re.search(r"/api/v1/agent-files/([0-9a-f]{32})/", output)
        assert match, output
        artifact_id = match.group(1)
        assert store.resolve(artifact_id, owner_id="admin-a").chat_id == CHAT_A
        # A distinct HTTP download request cannot relabel a stored attachment.
        app = web.Application()
        app.router.add_get("/v1/files/{artifact_id}", adapter._handle_chat_file_download)
        async with TestClient(TestServer(app)) as download:
            response = await download.get(f"/v1/files/{artifact_id}", headers=headers(chat=CHAT_B, Range="bytes=0-8"))
            assert response.status == 206
            assert response.headers[CHAT_HEADER] == CHAT_A
            assert await response.read() == b"synthetic"
            denied = await download.get(f"/v1/files/{artifact_id}", headers=headers(owner="admin-b"))
            assert denied.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("key,request_headers,status", [
    ("synthetic-key", headers(**{"Authorization": "Bearer wrong"}), 401),
    ("", headers(), 400),
    ("synthetic-key", headers(owner=""), 400),
    ("synthetic-key", headers(**{"X-Hermes-Session-Id": "legacy"}), 400),
])
async def test_auth_and_header_fail_closed(tmp_path, monkeypatch, key, request_headers, status):
    async with server(tmp_path, monkeypatch, key=key) as (adapter, _db, client):
        run = AsyncMock()
        monkeypatch.setattr(adapter, "_run_agent", run)
        response = await client.post("/v1/chat/completions", headers=request_headers, json=body())
        assert response.status == status
        run.assert_not_called()
        assert adapter._pending_agent_requests == 0


@pytest.mark.asyncio
async def test_thread_bound_identity_and_constructor(tmp_path, monkeypatch):
    captured = {}

    class FakeAgent:
        session_prompt_tokens = session_completion_tokens = session_total_tokens = 0
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs["session_id"]
        def run_conversation(self, **kwargs):
            captured["bound_user"] = get_bound_session_env("HERMES_SESSION_USER_ID")
            captured["bound_chat"] = get_bound_session_env("HERMES_SESSION_CHAT_ID")
            captured["request_history_authoritative"] = self._request_history_authoritative
            return {"final_response": "synthetic answer"}

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"provider": "openai", "api_key": "synthetic", "base_url": "https://example.test/v1"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda *_: {"enabled": False}))
    monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())
    async with server(tmp_path, monkeypatch) as (_adapter, _db, client):
        response = await client.post("/v1/chat/completions", headers=headers(), json=body())
        assert response.status == 200, await response.text()
        assert captured["user_id"] == captured["bound_user"] == "admin-a"
        assert captured["chat_id"] == captured["bound_chat"] == CHAT_A
        assert captured["request_history_authoritative"] is True
        assert captured["session_id"] == captured["gateway_session_key"] == ThereChatScope("admin-a", CHAT_A).session_id
        assert captured["ephemeral_system_prompt"] == body()["messages"][0]["content"]


def test_existing_lazy_persistence_records_business_origin(tmp_path):
    from run_agent import AIAgent
    scope = ThereChatScope("admin-a", CHAT_A)
    db = SessionDB(tmp_path / "persist.sqlite3")
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = scope.session_id
    agent._session_db = db
    agent._session_db_created = False
    agent.platform = "api_server"
    agent.model = "synthetic-model"
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "synthetic policy"
    agent._parent_session_id = None
    agent._user_id = scope.owner_id
    agent._chat_id = scope.chat_id
    agent._gateway_session_key = scope.session_id
    try:
        agent._ensure_db_session()
        row = db.get_session(scope.session_id)
        assert row is not None and scope.matches_session(row)
        assert row["session_key"] == scope.session_id
    finally:
        db.close()
