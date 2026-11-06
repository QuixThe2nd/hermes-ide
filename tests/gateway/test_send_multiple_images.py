"""
Tests for ``send_multiple_images`` native batching across platforms.

Covers:
    - Base default loop (per-image fallback for platforms without native batching)
    - Discord: ``channel.send(files=[...])``
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Base default loop
# ---------------------------------------------------------------------------


class _StubAdapter(BasePlatformAdapter):
    """Minimal adapter that records per-image send calls."""

    name = "stub"

    def __init__(self):
        self.sent_images = []
        self.sent_animations = []
        self.sent_files = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, **kwargs):
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {}

    async def send_image(self, chat_id, image_url, caption=None, **kwargs):
        from gateway.platforms.base import SendResult
        self.sent_images.append((chat_id, image_url, caption))
        return SendResult(success=True, message_id=str(len(self.sent_images)))

    async def send_animation(self, chat_id, animation_url, caption=None, **kwargs):
        from gateway.platforms.base import SendResult
        self.sent_animations.append((chat_id, animation_url, caption))
        return SendResult(success=True, message_id=str(len(self.sent_animations)))

    async def send_image_file(self, chat_id, image_path, caption=None, **kwargs):
        from gateway.platforms.base import SendResult
        self.sent_files.append((chat_id, image_path, caption))
        return SendResult(success=True, message_id=str(len(self.sent_files)))


class TestBaseDefaultLoop:
    def test_loops_per_image_by_default(self, tmp_path):
        local = tmp_path / "foo.png"
        a = _StubAdapter()
        images = [
            ("https://x.com/a.png", "alt 1"),
            ("https://x.com/b.png", "alt 2"),
            (local.as_uri(), "local"),
            ("https://x.com/c.gif", ""),
        ]
        _run(a.send_multiple_images("chat1", images))
        # 2 URL images + 1 animation + 1 local file
        assert len(a.sent_images) == 2
        assert len(a.sent_animations) == 1
        assert len(a.sent_files) == 1
        assert a.sent_files[0][1] == str(local)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    for name in ("discord", "discord.ext", "discord.ext.commands"):
        sys.modules.setdefault(name, discord_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class TestDiscordMultiImage:
    @pytest.fixture
    def adapter(self):
        config = PlatformConfig(enabled=True, token="fake-token")
        a = DiscordAdapter(config)
        a._client = MagicMock()
        return a


    def test_url_batch_follows_safe_redirect_location_header(self, adapter, monkeypatch):
        """Redirect handling preserves aiohttp's case-insensitive Location behavior."""
        import plugins.platforms.discord.adapter as discord_adapter

        public_url = "https://cdn.example.test/image.png"
        redirected_url = "https://assets.example.test/image.png"
        requested_urls = []

        class RedirectResponse:
            status = 302
            headers = {"Location": redirected_url}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def read(self):
                raise AssertionError("redirect responses must not be read")

        class ImageResponse:
            status = 200
            headers = {"Content-Type": "image/png"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def read(self):
                return b"\x89PNG"

        class FakeSession:
            def get(self, url, **kwargs):
                assert kwargs.get("allow_redirects") is False
                requested_urls.append(url)
                if url == public_url:
                    return RedirectResponse()
                assert url == redirected_url
                return ImageResponse()

            async def close(self):
                return None

        fake_aiohttp = types.SimpleNamespace(
            ClientSession=lambda **kwargs: FakeSession(),
            ClientTimeout=lambda **kwargs: kwargs,
        )
        monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)
        monkeypatch.setattr(discord_adapter, "is_safe_url", lambda url: True)

        mock_channel = MagicMock()
        mock_channel.send = AsyncMock(return_value=MagicMock(id=1))
        adapter._client.get_channel = MagicMock(return_value=mock_channel)
        adapter._is_forum_parent = MagicMock(return_value=False)

        _run(adapter.send_multiple_images("67890", [(public_url, "caption")]))

        assert requested_urls == [public_url, redirected_url]
        mock_channel.send.assert_awaited_once()

    def test_send_image_blocks_private_redirect_before_send(self, adapter, monkeypatch):
        import plugins.platforms.discord.adapter as discord_adapter

        public_url = "https://cdn.example.test/image.png"
        private_url = "http://169.254.169.254/latest/meta-data/"

        class FakeResponse:
            status = 302
            headers = {"Location": private_url}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def read(self):
                return b"metadata-secret"

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, **kwargs):
                assert kwargs.get("allow_redirects") is False
                return FakeResponse()

        fake_aiohttp = types.SimpleNamespace(
            ClientSession=lambda **kwargs: FakeSession(),
            ClientTimeout=lambda **kwargs: kwargs,
        )
        monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)
        monkeypatch.setattr(
            discord_adapter,
            "is_safe_url",
            lambda url: not str(url).startswith("http://169.254.169.254"),
        )
        adapter._is_forum_parent = MagicMock(return_value=False)
        mock_channel = MagicMock()
        mock_channel.send = AsyncMock(return_value=MagicMock(id=1))
        adapter._client.get_channel = MagicMock(return_value=mock_channel)
        adapter._client.fetch_channel = AsyncMock(return_value=mock_channel)
        adapter.send = AsyncMock()

        _run(adapter.send_image("67890", public_url, "caption"))

        mock_channel.send.assert_not_awaited()


