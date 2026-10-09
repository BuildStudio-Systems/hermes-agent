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
