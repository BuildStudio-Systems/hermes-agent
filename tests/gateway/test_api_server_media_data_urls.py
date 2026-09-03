"""MEDIA: tag → base64 data-URL resolution for the API server (salvage of #2696).

Remote OpenAI-compatible frontends can't read local file paths, so
``MEDIA:<path>`` image tags in final responses are inlined as markdown
data URLs before crossing the HTTP boundary.
"""

import base64
import re
import unittest
from pathlib import Path

import pytest

try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:  # api_server is an optional messaging dependency
    web = None
    TestClient = TestServer = None

from gateway.chat_file_artifacts import ChatFileArtifactStore  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.api_server import (  # noqa: E402
    APIServerAdapter,
    _StreamingMediaResolver,
    _api_request_file_owner,
    _resolve_media_to_data_urls,
)
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402

# 1x1 transparent PNG
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


class TestResolveMediaToDataUrls(unittest.TestCase):
    def _write_png(self, tmpdir_name="hermes_media_test"):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix=tmpdir_name))
        p = d / "shot.png"
        p.write_bytes(_PNG_BYTES)
        return p

    def test_media_tag_inlined(self):
        p = self._write_png()
        out = _resolve_media_to_data_urls(f"Here you go: MEDIA:{p}")
        self.assertIn("data:image/png;base64,", out)
        self.assertNotIn("MEDIA:", out)

    def test_backtick_wrapped_tag(self):
        p = self._write_png()
        out = _resolve_media_to_data_urls(f"See `MEDIA:{p}` above")
        self.assertIn("data:image/png;base64,", out)

    def test_missing_file_left_untouched(self):
        text = "MEDIA:/nonexistent/path/shot.png"
        self.assertEqual(_resolve_media_to_data_urls(text), text)

    def test_non_image_left_untouched(self):
        text = "MEDIA:/tmp/archive.zip"
        self.assertEqual(_resolve_media_to_data_urls(text), text)


def _enable_strict_test_media(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
    monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "0")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(root))


def test_non_image_media_becomes_download_link(tmp_path: Path, monkeypatch):
    _enable_strict_test_media(monkeypatch, tmp_path)
    source = tmp_path / "result video.mp4"
    source.write_bytes(b"video")
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "file_delivery": {
                    "enabled": True,
                    "public_base_url": "/api/v1/agent-files",
                }
            },
        )
    )
    store = ChatFileArtifactStore(tmp_path / "artifacts.sqlite3")
    adapter._get_chat_file_store = lambda: store

    output = adapter._resolve_media_for_delivery(
        f"好了。\nMEDIA:{source}", owner_id="user-1"
    )

    assert "MEDIA:" not in output
    assert str(source) not in output
    assert "/api/v1/agent-files/" in output
    assert "result%20video.mp4" in output


def test_streaming_resolver_never_emits_partial_local_path():
    resolver = _StreamingMediaResolver(
        lambda text: text.replace("MEDIA:/tmp/result.mp4", "[download](/safe/file)")
    )

    chunks = []
    chunks.extend(resolver.feed("Ready. ME"))
    chunks.extend(resolver.feed("DIA:/tmp/res"))
    chunks.extend(resolver.feed("ult.mp4"))

    assert "/tmp/" not in "".join(chunks)
    chunks.extend(resolver.finish())
    assert "".join(chunks) == "Ready. [download](/safe/file)"


def test_streaming_resolver_holds_lowercase_media_path():
    resolver = _StreamingMediaResolver(
        lambda text: re.sub(
            r"media:/tmp/result\.custom", "[download](/safe/file)", text, flags=re.I
        )
    )

    chunks = []
    chunks.extend(resolver.feed("Ready. me"))
    chunks.extend(resolver.feed("dia:/tmp/res"))
    chunks.extend(resolver.feed("ult.custom"))

    assert "/tmp/" not in "".join(chunks)
    chunks.extend(resolver.finish())
    assert "".join(chunks) == "Ready. [download](/safe/file)"


def test_lowercase_extensionless_media_becomes_download_link(
    tmp_path: Path, monkeypatch
):
    _enable_strict_test_media(monkeypatch, tmp_path)
    source = tmp_path / "result.custom"
    source.write_bytes(b"artifact")
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "file_delivery": {
                    "enabled": True,
                    "public_base_url": "/api/v1/agent-files",
                }
            },
        )
    )
    adapter._get_chat_file_store = lambda: ChatFileArtifactStore(
        tmp_path / "artifacts.sqlite3"
    )

    output = adapter._resolve_media_for_delivery(
        f"media:{source}", owner_id="user-1"
    )

    assert "media:" not in output.lower()
    assert str(source) not in output
    assert "/api/v1/agent-files/" in output


@pytest.mark.parametrize("strict_value", [None, "0"])
@pytest.mark.parametrize(
    "directive_template",
    ["MEDIA:{}", 'MEDIA:"{}"', "MEDIA:'{}'", "MEDIA:`{}`"],
)
def test_disabled_or_non_strict_delivery_redacts_host_path(
    tmp_path: Path, monkeypatch, strict_value, directive_template
):
    source = tmp_path / "private.mp4"
    source.write_bytes(b"video")
    if strict_value is None:
        monkeypatch.delenv("HERMES_MEDIA_DELIVERY_STRICT", raising=False)
        enabled = False
    else:
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", strict_value)
        enabled = True
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "file_delivery": {
                    "enabled": enabled,
                    "public_base_url": "/api/v1/agent-files",
                }
            },
        )
    )

    output = adapter._resolve_media_for_delivery(
        directive_template.format(source), owner_id="user-1"
    )

    assert str(source) not in output
    assert "unavailable" in output.lower()


def test_delivery_failure_redacts_host_path(tmp_path: Path, monkeypatch):
    _enable_strict_test_media(monkeypatch, tmp_path)
    source = tmp_path / "private.mp4"
    source.write_bytes(b"video")
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "file_delivery": {
                    "enabled": True,
                    "public_base_url": "/api/v1/agent-files",
                }
            },
        )
    )
    monkeypatch.setattr(
        BasePlatformAdapter,
        "extract_media",
        staticmethod(lambda _text: (_ for _ in ()).throw(RuntimeError("boom"))),
    )

    output = adapter._resolve_media_for_delivery(
        f'MEDIA:"{source}"', owner_id="user-1"
    )

    assert str(source) not in output
    assert "unavailable" in output.lower()


def test_streaming_resolvers_capture_request_owner(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    resolved_owners = []

    def resolve(text, *, owner_id=None):
        resolved_owners.append(owner_id)
        return text

    monkeypatch.setattr(adapter, "_resolve_media_for_delivery", resolve)

    first_token = _api_request_file_owner.set("user-1")
    try:
        first = adapter._media_resolver_for_request()
    finally:
        _api_request_file_owner.reset(first_token)

    second_token = _api_request_file_owner.set("user-2")
    try:
        second = adapter._media_resolver_for_request()
        first.feed("MEDIA:/tmp/first.mp4\n")
        second.feed("MEDIA:/tmp/second.mp4\n")
    finally:
        _api_request_file_owner.reset(second_token)

    assert resolved_owners == ["user-1", "user-2"]


@pytest.mark.skipif(web is None, reason="aiohttp is not installed")
@pytest.mark.asyncio
async def test_download_endpoint_requires_auth_and_supports_range(
    tmp_path: Path, monkeypatch
):
    _enable_strict_test_media(monkeypatch, tmp_path)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0123456789")
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": "test-api-key",
                "file_delivery": {
                    "enabled": True,
                    "public_base_url": "/api/v1/agent-files",
                },
            },
        )
    )
    store = ChatFileArtifactStore(tmp_path / "artifacts.sqlite3")
    adapter._get_chat_file_store = lambda: store
    link = adapter._resolve_media_for_delivery(
        f"MEDIA:{source}", owner_id="user-1"
    )
    artifact_id = re.search(r"/([0-9a-f]{32})/", link).group(1)

    app = web.Application()
    app.router.add_get("/v1/files/{artifact_id}", adapter._handle_chat_file_download)
    async with TestClient(TestServer(app)) as client:
        unauthenticated = await client.get(f"/v1/files/{artifact_id}")
        assert unauthenticated.status == 401

        missing_owner = await client.get(
            f"/v1/files/{artifact_id}",
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert missing_owner.status == 404

        invalid_owner = await client.get(
            f"/v1/files/{artifact_id}",
            headers={
                "Authorization": "Bearer test-api-key",
                "X-BuildStudio-User-Id": "../user-1",
            },
        )
        assert invalid_owner.status == 404

        response = await client.get(
            f"/v1/files/{artifact_id}",
            headers={
                "Authorization": "Bearer test-api-key",
                "Range": "bytes=2-5",
                "X-BuildStudio-User-Id": "user-1",
            },
        )
        assert response.status == 206
        assert await response.read() == b"2345"
        assert response.headers["Accept-Ranges"] == "bytes"
        assert "attachment" in response.headers["Content-Disposition"]

        wrong_owner = await client.get(
            f"/v1/files/{artifact_id}",
            headers={
                "Authorization": "Bearer test-api-key",
                "X-BuildStudio-User-Id": "user-2",
            },
        )
        assert wrong_owner.status == 404


if __name__ == "__main__":
    unittest.main()
