"""Exercise the H3 transport against an offline coordinator, never a real GPU."""
from __future__ import annotations

import json
import re
import threading
from unittest.mock import MagicMock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import plugins.video_gen.buildstudio_h3 as h3
from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars


@pytest.fixture(autouse=True)
def _credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("VIDEO_COORDINATOR_API_KEY", "coordinator-test-secret")
    tokens = set_session_vars(platform="api_server", user_id="user-1", chat_id="chat-1", session_id="turn-session")
    yield
    clear_session_vars(tokens)


@pytest.fixture
def coordinator(monkeypatch):
    state = {"calls": [], "status": "queued", "owner": "", "redirect": False, "response_status": 200, "content": b"\x00\x00\x00\x18ftypmp42test-video", "mime": "video/mp4"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.serve()

        def do_GET(self):
            self.serve()

        def serve(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            owner = self.headers.get("X-OpenWebUI-User-Id")
            state["calls"].append((self.command, self.path, owner, payload, self.headers.get("Authorization")))
            if state["redirect"]:
                self.send_response(307)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                return
            if state["response_status"] != 200:
                self.send_response(state["response_status"])
                self.end_headers()
                self.wfile.write(b"internal coordinator-test-secret must-not-leak")
                return
            if self.command == "POST":
                state["owner"] = owner
            if owner != state["owner"]:
                self.send_response(404)
                self.end_headers()
                return
            content = self.path.endswith("/content")
            data = state["content"] if content else json.dumps({
                "id": "video-1", "status": state["status"], "progress": 0.5,
                "status_url": "http://private-host/status?secret=must-not-leak",
                "data": [{"url": "http://private-host/content?secret=must-not-leak"}],
                "error": {"message": "internal coordinator-test-secret must-not-leak"},
            }).encode()
            if not content and state.get("invalid_json"):
                data = b"not JSON coordinator-test-secret"
            self.send_response(200)
            self.send_header("Content-Type", state["mime"] if content else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(h3.BuildStudioH3VideoGenProvider, "_default_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    state["base_url"] = f"http://127.0.0.1:{server.server_port}/v1"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_submit_returns_queued_without_polling_then_checks_once(coordinator):
    provider = h3.BuildStudioH3VideoGenProvider()
    result = provider.generate("robot welding with workshop ambience", duration=5, quality="standard", seed=3)
    assert result["success"] and result["video"] is None
    assert result["job_id"] == "video-1" and result["status"] == "queued"
    assert result["check_args"] == {"job_id": "video-1"}
    assert result["automatic_notification"] is False
    assert len(coordinator["calls"]) == 1
    method, path, owner, body, authorization = coordinator["calls"][0]
    assert (method, path, owner) == ("POST", "/v1/videos", "user-1")
    assert authorization == "Bearer coordinator-test-secret"
    assert body == {"model": "minimax-h3", "prompt": "robot welding with workshop ambience", "seconds": "5", "size": "864x480", "quality": "standard", "seed": 3}
    result = h3.BuildStudioH3VideoGenProvider().get_job("video-1")
    assert result["status"] == "queued"
    assert [call[0] for call in coordinator["calls"]] == ["POST", "GET"]
    assert "must-not-leak" not in json.dumps(result)


def test_completed_job_downloads_authenticated_fixed_content_path(coordinator, tmp_path):
    provider = h3.BuildStudioH3VideoGenProvider()
    provider.generate("video", aspect_ratio="9:16", upscale=True)
    coordinator["status"] = "completed"
    result = provider.get_job("video-1")
    assert result["success"] and result["upscaled"]
    assert result["resolution"] == "1440x2560" and result["audio"] is True
    path = Path(result["video"])
    assert path.is_relative_to(tmp_path) and path.read_bytes() == coordinator["content"]
    assert coordinator["calls"][-1][:3] == ("GET", "/v1/videos/video-1/content", "user-1")
    assert "private-host" not in json.dumps(result) and "coordinator-test-secret" not in json.dumps(result)


@pytest.mark.parametrize("user,chat", [("user-2", "chat-1"), ("user-1", "chat-2")])
def test_other_user_or_conversation_cannot_query_even_with_valid_id(coordinator, user, chat):
    provider = h3.BuildStudioH3VideoGenProvider()
    provider.generate("video")
    set_session_vars(platform="api_server", user_id=user, chat_id=chat)
    result = provider.get_job("video-1")
    assert result["success"] is False
    assert "unavailable in this conversation" in result["error"]
    assert len(coordinator["calls"]) == 1


def test_unknown_or_path_injected_job_ids_never_reach_coordinator(coordinator):
    provider = h3.BuildStudioH3VideoGenProvider()
    for value in ["missing", "../video-1", "video-1/content", "video-1?owner=other"]:
        assert provider.get_job(value)["success"] is False
    assert not coordinator["calls"]


def test_unbound_or_missing_owner_does_not_borrow_process_environment(coordinator, monkeypatch):
    provider = h3.BuildStudioH3VideoGenProvider()
    reset_session_vars()
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user-1")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-1")
    monkeypatch.setenv("HERMES_SESSION_ID", "stale")
    assert provider.generate("video")["success"] is False
    set_session_vars(platform="api_server", chat_id="chat-1")
    assert provider.generate("video")["success"] is False
    assert not coordinator["calls"]


def test_redirect_never_receives_bearer_or_owner(coordinator):
    coordinator["redirect"] = True
    result = h3.BuildStudioH3VideoGenProvider().generate("video")
    assert result["success"] is False and len(coordinator["calls"]) == 1
    assert result["error_type"] == "submission_unconfirmed"
    assert "do not automatically resubmit" in result["error"]
    assert "coordinator-test-secret" not in json.dumps(result)


@pytest.mark.parametrize("options", [
    {"duration": 0}, {"duration": 6}, {"audio": False}, {"negative_prompt": "bad"},
    {"quality": "unknown"}, {"quality": []}, {"image_url": "https://example.com/input.png"},
    {"reference_image_urls": ["x"]}, {"aspect_ratio": "4:3"},
    {"aspect_ratio": "1:1", "upscale": True}, {"resolution": "9000x9000"},
])
def test_unsupported_options_fail_before_submit(coordinator, options):
    assert h3.BuildStudioH3VideoGenProvider().generate("video", **options)["success"] is False
    assert not coordinator["calls"]


def test_failed_job_does_not_expose_coordinator_error(coordinator):
    provider = h3.BuildStudioH3VideoGenProvider()
    provider.generate("video")
    coordinator["status"] = "failed"
    result = provider.get_job("video-1")
    assert result["error_type"] == "generation_failed"
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.parametrize("mime,content,limit", [("text/html", b"error", 100), ("video/mp4", b"", 100), ("video/mp4", b"oversized", 2)])
def test_invalid_or_oversized_download_leaves_no_partial_video(coordinator, tmp_path, monkeypatch, mime, content, limit):
    provider = h3.BuildStudioH3VideoGenProvider()
    provider.generate("video")
    coordinator.update(status="completed", mime=mime, content=content)
    monkeypatch.setattr(provider, "_max_video_bytes", limit)
    result = provider.get_job("video-1")
    assert result["success"] is False
    assert not list(tmp_path.rglob("*.mp4"))
    assert not str(result.get("video", "")).startswith("http")


def test_credentials_never_persist_in_receipt(coordinator, tmp_path):
    provider = h3.BuildStudioH3VideoGenProvider()
    provider.generate("a private video prompt")
    contents = next(tmp_path.rglob("video-1.json")).read_text()
    assert "coordinator-test-secret" not in contents and "private video prompt" not in contents


def test_session_binding_uses_authenticated_file_owner():
    from gateway.platforms.api_server import APIServerAdapter, _api_request_file_owner
    from gateway.session_context import get_bound_session_env

    token = _api_request_file_owner.set("trusted-owner")
    try:
        tokens = APIServerAdapter._bind_api_server_session(chat_id="trusted-chat")
        assert get_bound_session_env("HERMES_SESSION_USER_ID") == "trusted-owner"
        clear_session_vars(tokens)
    finally:
        _api_request_file_owner.reset(token)


def test_tool_dispatch_queries_without_prompt_or_second_post(coordinator, monkeypatch):
    from tools import video_generation_tool as tool

    provider = h3.BuildStudioH3VideoGenProvider()
    monkeypatch.setattr(tool, "_resolve_active_provider", lambda: provider)
    monkeypatch.setattr(tool, "_read_configured_video_provider", lambda: provider.name)
    monkeypatch.setattr(tool, "_read_configured_video_model", lambda: None)
    first = json.loads(tool._handle_video_generate({
        "prompt": "video", "quality": "standard", "upscale": True,
        "owner": "forged-user", "metadata": {"owner": "forged-user"},
        "base_url": "https://must-not-call.invalid/", "headers": {"Authorization": "forged"},
    }))
    assert first["status"] == "queued"
    check = json.loads(tool._handle_video_generate({"job_id": first["job_id"]}))
    assert check["status"] == "queued"
    assert [call[0] for call in coordinator["calls"]] == ["POST", "GET"]
    body = coordinator["calls"][0][3]
    assert body["quality"] == "standard" and body["size"] == "2560x1440"
    assert set(body) == {"model", "prompt", "seconds", "size", "quality"}
    assert coordinator["calls"][0][2] == "user-1"


def test_dynamic_schema_matches_async_audio_and_quality_contract(monkeypatch):
    from tools import video_generation_tool as tool

    provider = h3.BuildStudioH3VideoGenProvider()
    monkeypatch.setattr(tool, "_resolve_active_provider", lambda: provider)
    monkeypatch.setattr(tool, "_read_configured_video_model", lambda: None)
    schema = tool._build_dynamic_video_schema()
    properties = schema["parameters"]["properties"]
    assert "job_id" in properties and "prompt" not in schema["parameters"]["required"]
    assert "audio" not in properties and "always on" in schema["description"]
    assert "call blocks" not in schema["description"]
    assert "Automatic completion notification is not provided" in schema["description"]
    assert set(properties["quality"]["enum"]) == set(provider.capabilities()["quality_modes"])
    assert properties["resolution"]["default"] in properties["resolution"]["enum"]
    assert "Lanczos" in properties["upscale"]["description"]
    assert "not native 2K" in properties["upscale"]["description"]


def test_completed_video_enters_existing_owner_bound_artifact_delivery(coordinator, tmp_path, monkeypatch):
    from gateway.chat_file_artifacts import ChatFileArtifactNotFound, ChatFileArtifactStore
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    provider = h3.BuildStudioH3VideoGenProvider()
    provider.generate("video")
    coordinator["status"] = "completed"
    result = provider.get_job("video-1")
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
    monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "0")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={
        "file_delivery": {"enabled": True, "public_base_url": "/api/v1/agent-files"},
    }))
    store = ChatFileArtifactStore(tmp_path / "artifacts.sqlite3")
    monkeypatch.setattr(adapter, "_get_chat_file_store", lambda: store)
    output = adapter._resolve_media_for_delivery("MEDIA:" + result["video"], owner_id="user-1")
    match = re.search(r"/api/v1/agent-files/([0-9a-f]{32})/", output)
    assert match, output
    assert store.resolve(match.group(1), owner_id="user-1").path == result["video"]
    with pytest.raises(ChatFileArtifactNotFound):
        store.resolve(match.group(1), owner_id="user-2")
    assert result["video"] not in output and "private-host" not in output


@pytest.mark.parametrize("error_kind", ["InvalidURL", "Timeout", "ConnectionError"])
def test_transport_exceptions_do_not_expose_credentials(monkeypatch, error_kind):
    import requests
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise getattr(requests.exceptions, error_kind)("coordinator-test-secret internal-url")

    monkeypatch.setattr(requests.Session, "request", fail)
    result = h3.BuildStudioH3VideoGenProvider().generate("video")
    assert result["success"] is False
    assert result["error_type"] == "submission_unconfirmed"
    assert "do not automatically resubmit" in result["error"]
    assert "coordinator-test-secret" not in json.dumps(result)
    assert "internal-url" not in json.dumps(result)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 500, 503])
def test_uncertain_submission_errors_forbid_automatic_resubmit(coordinator, status):
    coordinator["response_status"] = status
    result = h3.BuildStudioH3VideoGenProvider().generate("video")
    assert result["error_type"] == "submission_unconfirmed"
    assert "do not automatically resubmit" in result["error"]
    assert len(coordinator["calls"]) == 1
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 422, 429])
def test_confirmed_rejection_is_sanitized_invalid_request(coordinator, status):
    coordinator["response_status"] = status
    result = h3.BuildStudioH3VideoGenProvider().generate("video")
    assert result["error_type"] == "invalid_request"
    assert len(coordinator["calls"]) == 1
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.parametrize("invalid_json", [True, False])
def test_malformed_submission_reply_must_not_trigger_second_post(coordinator, invalid_json):
    coordinator.update(status="unexpected", invalid_json=invalid_json)
    result = h3.BuildStudioH3VideoGenProvider().generate("video")
    assert result["error_type"] == "submission_unconfirmed"
    assert "do not automatically resubmit" in result["error"]
    assert len(coordinator["calls"]) == 1


def test_real_plugin_discovery_and_config_dispatch(coordinator, tmp_path, monkeypatch):
    from agent import video_gen_registry
    from hermes_cli import plugins
    from tools.video_generation_tool import _handle_video_generate

    video_gen_registry._reset_for_tests()
    monkeypatch.setenv("BUILDSTUDIO_H3_BASE_URL", coordinator["base_url"])
    (tmp_path / "config.yaml").write_text("video_gen:\n  provider: buildstudio_h3\n  model: minimax-h3\n", encoding="utf-8")
    try:
        plugins._ensure_plugins_discovered(force=True)
        result = json.loads(_handle_video_generate({"prompt": "workshop video", "quality": "turbo"}))
        assert result["success"] is True, result
        assert result["status"] == "queued"
        lookup = json.loads(_handle_video_generate({"job_id": result["job_id"]}))
        assert lookup["status"] == "queued"
        assert [call[0] for call in coordinator["calls"]] == ["POST", "GET"]
    finally:
        video_gen_registry._reset_for_tests()


@pytest.mark.asyncio
async def test_api_executor_preserves_owner_and_real_profile_secret_scope(coordinator, tmp_path, monkeypatch):
    from agent import secret_scope
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter, _api_request_file_owner
    from gateway.session_context import get_bound_session_env
    from tools import video_generation_tool as tool

    (tmp_path / ".env").write_text("VIDEO_COORDINATOR_API_KEY=coordinator-test-secret\n", encoding="utf-8")
    monkeypatch.setenv("VIDEO_COORDINATOR_API_KEY", "wrong-process-profile-key")
    provider = h3.BuildStudioH3VideoGenProvider()
    monkeypatch.setattr(tool, "_resolve_active_provider", lambda: provider)
    monkeypatch.setattr(tool, "_read_configured_video_provider", lambda: provider.name)
    monkeypatch.setattr(tool, "_read_configured_video_model", lambda: None)
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
    captured = {}

    def run_conversation(**kwargs):
        captured.update(owner=get_bound_session_env("HERMES_SESSION_USER_ID"),
                        key_available=provider.is_available(), scope_installed=secret_scope.current_secret_scope() is not None,
                        thread=threading.get_ident())
        captured["result"] = json.loads(tool._handle_video_generate({"prompt": "video"}))
        return {"final_response": "synthetic"}

    agent.run_conversation.side_effect = run_conversation
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    token = _api_request_file_owner.set("user-1")
    secret_scope.set_multiplex_active(True)
    try:
        await adapter._run_agent(user_message="synthetic", conversation_history=[], session_id="chat-1")
    finally:
        secret_scope.set_multiplex_active(False)
        _api_request_file_owner.reset(token)
    assert captured["thread"] != threading.get_ident()
    assert captured["owner"] == "user-1" and captured["key_available"] and captured["scope_installed"]
    assert captured["result"]["success"] is True
    assert coordinator["calls"][0][2] == "user-1"
    assert coordinator["calls"][0][4] == "Bearer coordinator-test-secret"


@pytest.mark.asyncio
async def test_runs_route_preserves_request_owner_in_tool_executor(monkeypatch):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session_context import get_bound_session_env

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-service-key"}))
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    captured = {}
    done = threading.Event()
    agent = MagicMock()
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

    def run_conversation(**kwargs):
        captured.update(owner=get_bound_session_env("HERMES_SESSION_USER_ID"),
                        chat=get_bound_session_env("HERMES_SESSION_CHAT_ID"), thread=threading.get_ident())
        done.set()
        return {"final_response": "synthetic"}

    agent.run_conversation.side_effect = run_conversation
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: agent)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/runs", json={"input": "synthetic", "session_id": "runs-chat"},
                                     headers={"Authorization": "Bearer test-service-key", "X-BuildStudio-User-Id": "runs-owner"})
        assert response.status == 202
        assert await asyncio.to_thread(done.wait, 5)
        payload = await response.json()
        for _ in range(50):
            status = await client.get("/v1/runs/" + payload["run_id"], headers={"Authorization": "Bearer test-service-key"})
            if (await status.json())["status"] == "completed":
                break
            await asyncio.sleep(0.02)
    assert captured["owner"] == "runs-owner" and captured["chat"] == "runs-chat"
    assert captured["thread"] != threading.get_ident()
