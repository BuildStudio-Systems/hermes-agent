"""Legacy H3 recovery through real HTTP, executor context and file delivery.

No real model, GPU, credentials or customer records are used. This proves an
authenticated internal compatibility route, not automatic Web-chat adoption.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from agent import secret_scope
from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.session_context import clear_session_vars, get_bound_session_env, set_session_vars
from gateway.there_chat_scope import CHAT_HEADER, OWNER_HEADER
from hermes_state import SessionDB
from plugins.video_gen.buildstudio_h3 import BuildStudioH3VideoGenProvider
from plugins.video_gen.buildstudio_h3.jobs import current_scope, save_receipt


OWNER = "synthetic-original-owner"
JOB = "synthetic-legacy-video"
WEB_CHAT = "11111111-1111-4111-8111-111111111111"
SYSTEM = "Synthetic legacy system"
FIRST_USER = "Synthetic original question"
LEGACY_SESSION = api_server._derive_chat_session_id(SYSTEM, FIRST_USER)
OPTIONS = {"duration": 5, "aspect_ratio": "16:9", "size": "864x480", "quality": "turbo"}
SERVICE_KEY = "synthetic-existing-service-key"
COORDINATOR_KEY = "synthetic-existing-coordinator-key"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42synthetic-video-content"


@pytest.fixture
def legacy_coordinator(monkeypatch):
    state = {"calls": [], "status": "completed", "response_status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            state["calls"].append(("POST", self.path, "", ""))
            self.send_response(405)
            self.end_headers()

        def do_GET(self):
            owner = self.headers.get("X-OpenWebUI-User-Id")
            authorization = self.headers.get("Authorization")
            state["calls"].append(("GET", self.path, owner, authorization))
            if authorization != "Bearer " + COORDINATOR_KEY or owner != OWNER:
                self.send_response(404)
                self.end_headers()
                return
            if state["response_status"] != 200:
                self.send_response(state["response_status"])
                self.end_headers()
                return
            if self.path == f"/v1/videos/{JOB}":
                body = json.dumps({"id": JOB, "status": state["status"]}).encode()
                content_type = "application/json"
            elif self.path == f"/v1/videos/{JOB}/content":
                body, content_type = VIDEO_BYTES, "video/mp4"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(BuildStudioH3VideoGenProvider, "_default_base_url",
                        f"http://127.0.0.1:{server.server_port}/v1")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_headers(*, owner=OWNER, session=LEGACY_SESSION):
    result = {"Authorization": "Bearer " + SERVICE_KEY, OWNER_HEADER: owner}
    if session is not None:
        result["X-Hermes-Session-Id"] = session
    return result


def request_body(stream=False):
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": FIRST_USER},
        {"role": "assistant", "content": "Synthetic prior answer"},
        {"role": "user", "content": "Collect the existing video, do not generate another."},
    ], "stream": stream}


@asynccontextmanager
async def legacy_gateway(tmp_path, monkeypatch):
    profile = tmp_path / "isolated-profile"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("VIDEO_COORDINATOR_API_KEY", "wrong-process-key")
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
    monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "0")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(profile))
    (profile / ".env").write_text(
        "VIDEO_COORDINATOR_API_KEY=" + COORDINATOR_KEY + "\n", encoding="utf-8"
    )
    tokens = set_session_vars(platform="api_server", user_id=OWNER,
                              chat_id=LEGACY_SESSION, session_id=LEGACY_SESSION)
    try:
        scope, owner = current_scope()
        assert owner == OWNER
        save_receipt(JOB, scope, dict(OPTIONS))
    finally:
        clear_session_vars(tokens)
    receipt = profile / "cache" / "videos" / "buildstudio_h3" / (JOB + ".json")
    original = receipt.read_bytes()
    # Preserve an independent original: the working copy may gain delivery
    # cache metadata, but must never have its ownership rewritten or removed.
    snapshot = tmp_path / "original-legacy-receipt.json"
    snapshot.write_bytes(original)
    seed_db = SessionDB(profile / "state.db")
    seed_db.create_session(LEGACY_SESSION, "api_server")
    seed_db.close()

    adapter = api_server.APIServerAdapter(PlatformConfig(enabled=True, extra={
        "key": SERVICE_KEY,
        "file_delivery": {"enabled": True, "public_base_url": "/api/v1/agent-files"},
    }))
    observed = []
    event_loop_thread = threading.get_ident()

    class FakeAgent:
        session_prompt_tokens = session_completion_tokens = session_total_tokens = 0

        def __init__(self, kwargs):
            self.session_id = kwargs["session_id"]
            self.callback = kwargs.get("stream_delta_callback")

        def run_conversation(self, **_kwargs):
            result = BuildStudioH3VideoGenProvider().get_job(JOB)
            observed.append({
                "thread": threading.get_ident(),
                "owner": get_bound_session_env("HERMES_SESSION_USER_ID"),
                "chat": get_bound_session_env("HERMES_SESSION_CHAT_ID"),
                "result": result,
            })
            text = ("MEDIA:" + result["video"] + "\n" if result.get("video")
                    else "Legacy status: " + result.get("status", "unavailable"))
            if self.callback:
                self.callback(text)
            return {"final_response": text, "messages": []}

    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent(kwargs))
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_get("/v1/files/{artifact_id}", adapter._handle_chat_file_download)
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, adapter, observed, receipt, snapshot, original
    finally:
        await adapter.disconnect()
    assert snapshot.read_bytes() == original
    assert all(item["thread"] != event_loop_thread for item in observed)


async def submit(client, headers, stream):
    response = await client.post("/v1/chat/completions", headers=headers, json=request_body(stream))
    output = await response.text()
    if response.status == 200:
        if stream:
            chunks = [json.loads(line[6:]) for line in output.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            output = "".join(choice.get("delta", {}).get("content") or ""
                             for chunk in chunks for choice in chunk.get("choices", []))
        else:
            output = json.loads(output)["choices"][0]["message"]["content"]
    return response.status, output


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("explicit_session", [False, True])
async def test_legacy_status_and_download_keep_original_scope(
    tmp_path, monkeypatch, legacy_coordinator, stream, explicit_session,
):
    async with legacy_gateway(tmp_path, monkeypatch) as (client, adapter, seen, receipt, _snapshot, original):
        headers = request_headers(session=LEGACY_SESSION if explicit_session else None)
        legacy_coordinator["status"] = "queued"
        status, output = await submit(client, headers, stream)
        assert seen[-1]["result"]["success"] is True, (seen[-1], legacy_coordinator["calls"])
        assert status == 200 and "Legacy status: queued" in output
        assert receipt.read_bytes() == original
        assert [call[1] for call in legacy_coordinator["calls"]] == [f"/v1/videos/{JOB}"]

        legacy_coordinator["status"] = "completed"
        status, output = await submit(client, headers, stream)
        assert status == 200
        match = re.search(r"/api/v1/agent-files/([0-9a-f]{32})/", output)
        assert match, output
        artifact_id = match.group(1)
        assert all(item["owner"] == OWNER and item["chat"] == LEGACY_SESSION for item in seen)
        artifact = adapter._get_chat_file_store().resolve(artifact_id, owner_id=OWNER)
        assert artifact.chat_id == ""  # No invented Web business UUID.
        assert Path(artifact.path).read_bytes() == VIDEO_BYTES
        collected = receipt.read_bytes()
        assert {key: json.loads(collected)[key] for key in ("scope", "options")} == json.loads(original)

        status, repeated = await submit(client, headers, stream)
        assert status == 200 and artifact_id in repeated
        assert receipt.read_bytes() == collected
        assert sum(call[1].endswith("/content") for call in legacy_coordinator["calls"]) == 1
        assert all(call[0] == "GET" and call[2:] == (OWNER, "Bearer " + COORDINATOR_KEY)
                   for call in legacy_coordinator["calls"])

        for download_headers, expected_status, expected_bytes in (
            (headers, 200, VIDEO_BYTES),
            ({**headers, "Range": "bytes=4-11", CHAT_HEADER: WEB_CHAT}, 206, VIDEO_BYTES[4:12]),
        ):
            response = await client.get(f"/v1/files/{artifact_id}", headers=download_headers)
            assert response.status == expected_status
            assert CHAT_HEADER not in response.headers
            assert await response.read() == expected_bytes
        for invalid_headers, expected_status in ((request_headers(owner="other-owner"), 404), ({}, 401)):
            response = await client.get(f"/v1/files/{artifact_id}", headers=invalid_headers)
            assert response.status == expected_status

        legacy_coordinator["response_status"] = 404
        status, output = await submit(client, headers, stream)
        assert status == 200 and "/api/v1/agent-files/" not in output
        assert seen[-1]["result"]["success"] is False
        assert receipt.read_bytes() == collected


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("boundary", ["other-owner", "other-session", "new-web-chat"])
async def test_legacy_receipt_cannot_be_claimed_by_other_scope(
    tmp_path, monkeypatch, legacy_coordinator, stream, boundary,
):
    headers = request_headers()
    if boundary == "other-owner":
        headers[OWNER_HEADER] = "other-owner"
    elif boundary == "other-session":
        headers["X-Hermes-Session-Id"] = "api-other-session"
    else:
        headers.pop("X-Hermes-Session-Id")
        headers[CHAT_HEADER] = WEB_CHAT
    async with legacy_gateway(tmp_path, monkeypatch) as (client, _adapter, seen, receipt, _snapshot, original):
        status, output = await submit(client, headers, stream)
        assert status == 200 and "/api/v1/agent-files/" not in output
        assert seen[-1]["result"]["success"] is False
        assert "unavailable in this conversation" in seen[-1]["result"]["error"]
        assert legacy_coordinator["calls"] == []
        assert receipt.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({**request_headers(), "Authorization": "Bearer wrong"}, 401),
    ({**request_headers(), CHAT_HEADER: WEB_CHAT}, 400),
])
async def test_legacy_route_rejects_auth_failure_and_mixed_binding_before_tools(
    tmp_path, monkeypatch, legacy_coordinator, headers, status,
):
    async with legacy_gateway(tmp_path, monkeypatch) as (client, _adapter, seen, receipt, _snapshot, original):
        response_status, output = await submit(client, headers, False)
        assert response_status == status and "/api/v1/agent-files/" not in output
        assert seen == [] and legacy_coordinator["calls"] == []
        assert receipt.read_bytes() == original
