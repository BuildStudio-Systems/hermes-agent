from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import plugins.video_gen.buildstudio_h3 as h3_plugin


@pytest.fixture(autouse=True)
def _credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("VIDEO_COORDINATOR_API_KEY", "coordinator-secret")


def _fake_openai(captured: dict):
    class _Videos:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                id="video-1",
                status="completed",
                error=None,
                data=[
                    {
                        "url": "https://buildstudio-there.com/api/v1/videos/jobs/video-1/content"
                    }
                ],
            )

        def retrieve(self, video_id):
            raise AssertionError(f"terminal job {video_id} must not be polled")

    class _Client:
        def __init__(self, api_key=None, base_url=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.videos = _Videos()

        def close(self):
            captured["closed"] = True

    module = MagicMock()
    module.OpenAI = _Client
    return module


@contextmanager
def _mock_video_download(captured: dict):
    import agent.video_gen_provider as base

    def save(url, *, prefix="video", **_kwargs):
        captured["url"] = url
        return Path(f"/tmp/{prefix}.mp4")

    with patch.object(base, "save_url_video", save):
        yield


def test_local_provider_uses_coordinator_and_landscape_profile():
    captured = {}
    provider = h3_plugin.BuildStudioH3VideoGenProvider()
    with (
        patch.dict("sys.modules", {"openai": _fake_openai(captured)}),
        _mock_video_download(captured),
    ):
        result = provider.generate("robot welding with workshop ambience", duration=5)

    assert result["success"] is True
    assert result["provider"] == "buildstudio_h3"
    assert captured["base_url"] == "http://127.0.0.1:8890/v1"
    assert captured["api_key"] == "coordinator-secret"
    assert captured["kwargs"]["size"] == "864x480"
    assert captured["kwargs"]["seconds"] == "5"
    assert captured["closed"] is True


def test_portrait_profile_and_limits_are_enforced():
    provider = h3_plugin.BuildStudioH3VideoGenProvider()
    assert provider._size("720p", "9:16") == "480x864"
    assert provider._size("2560x1440", "16:9") == "2560x1440"
    assert provider.capabilities()["supports_upscale"] is True
    result = provider.generate("too long", duration=6)
    assert result["success"] is False
    assert result["error_type"] == "invalid_request"


def test_image_to_video_fails_closed():
    result = h3_plugin.BuildStudioH3VideoGenProvider().generate(
        "animate this", image_url="https://example.com/input.png"
    )
    assert result["success"] is False
    assert result["error_type"] == "unsupported_modality"
