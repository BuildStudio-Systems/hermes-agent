"""Storage stalls must not stall unrelated API work or lose request identity."""
import asyncio
from contextvars import ContextVar
import threading

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _StreamingMediaResolver


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stream", "finish", "final"])
async def test_slow_storage_keeps_loop_responsive_and_request_context(route, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    owner = ContextVar("test_delivery_owner", default="missing")
    token = owner.set("private-owner")
    main_thread = threading.get_ident()
    observed = []

    def slow(text):
        observed.append((threading.get_ident(), owner.get()))
        entered.set()
        assert release.wait(2), "event loop could not release blocked storage"
        return "[Private download](/files/opaque)"

    stream = _StreamingMediaResolver(slow)
    if route == "stream":
        # Split the marker across chunks; no raw path may escape.
        assert stream.feed("MED") == []
        work = stream.feed_async("IA:/private/report.pdf\n")
    elif route == "finish":
        assert stream.feed("MEDIA:/private/report.pdf") == []
        work = stream.finish_async()
    else:
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_resolve_media_for_delivery", slow)
        work = adapter._resolve_media_for_delivery_async("MEDIA:/private/report.pdf")
    task = asyncio.create_task(work)
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set() and not task.done()
        # This runs on the same loop while storage is deliberately stalled.
        assert await asyncio.wait_for(asyncio.sleep(0, result="health-ready"), 0.1) == "health-ready"
        assert observed == [(observed[0][0], "private-owner")]
        assert observed[0][0] != main_thread
    finally:
        release.set()
        result = await task
        owner.reset(token)
    assert "/private/report.pdf" not in str(result)
    assert "Private download" in str(result)


@pytest.mark.asyncio
async def test_plain_tokens_do_not_pay_thread_dispatch_overhead(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("plain text was sent to a worker")
    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    stream = _StreamingMediaResolver(lambda text: text)
    assert await stream.feed_async("Ordinary text") == ["Ordinary text"]
    assert await stream.finish_async() == []
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    assert await adapter._resolve_media_for_delivery_async("Ordinary text") == "Ordinary text"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["registry", "path"])
async def test_download_storage_wait_does_not_block_other_requests(stage, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aiohttp.test_utils import make_mocked_request
    from gateway.platforms import api_server

    entered, release = threading.Event(), threading.Event()
    context = ContextVar("download_test_profile", default="missing")
    token = context.set("profile-a")
    main_thread = threading.get_ident()
    seen = []
    source = tmp_path / "report.txt"
    source.write_text("private report")
    artifact = SimpleNamespace(path=str(source), filename="report.txt",
                               content_type="text/plain", chat_id="stored-chat")

    def stall():
        seen.append((threading.get_ident(), context.get()))
        entered.set()
        assert release.wait(3), "download storage blocked the event loop"

    def resolve(artifact_id, *, owner_id):
        assert artifact_id == "a" * 32 and owner_id == "owner-a"
        if stage == "registry":
            stall()
        return artifact

    def validate(path):
        assert path == str(source)
        if stage == "path":
            stall()
        return path

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    monkeypatch.setattr(adapter, "_file_delivery_active", lambda: True)
    monkeypatch.setattr(adapter, "_get_chat_file_store", lambda: SimpleNamespace(resolve=resolve))
    monkeypatch.setattr(api_server, "validate_media_delivery_path", validate)
    request = make_mocked_request("GET", "/v1/files/" + "a" * 32,
        headers={"Authorization": "Bearer test-key", "X-BuildStudio-User-Id": "owner-a"},
        match_info={"artifact_id": "a" * 32})
    task = asyncio.create_task(adapter._handle_chat_file_download(request))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set() and not task.done()
        assert await asyncio.sleep(0, result="other-request-ready") == "other-request-ready"
    finally:
        release.set()
        try:
            response = await task
        finally:
            context.reset(token)
    assert seen[0][0] != main_thread and seen[0][1] == "profile-a"
    assert response.status == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers[api_server.CHAT_HEADER] == "stored-chat"
