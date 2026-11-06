"""Tests for tools/send_message_tool.py."""

import asyncio
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from tools.send_message_tool import (
    _send_matrix_via_adapter,
    _send_to_platform,
    send_message_tool,
)
from tools.send_message_targets import _parse_target_ref
# Discord helpers moved to the plugin in #24325.  Import from the new path
# and provide a thin ``_send_discord(token, ...)`` shim that mirrors the
# pre-migration signature so the existing test bodies keep working.
from plugins.platforms.discord.adapter import (
    _DISCORD_STANDALONE_JSON_BODY_LIMIT_BYTES,
    _derive_forum_thread_name,
    _probe_is_forum_cached,
    _standalone_send,
)


async def _send_discord(
    token,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
):
    """Pre-migration ``(token, chat_id, message, …)`` adapter around the
    plugin's ``_standalone_send(pconfig, …)``.  Lets test bodies continue
    to call ``_send_discord("tok", ...)`` without rewriting every signature.
    """
    pconfig = SimpleNamespace(token=token, extra={})
    return await _standalone_send(
        pconfig,
        chat_id,
        message,
        thread_id=thread_id,
        media_files=media_files,
    )


class _StreamingAiohttpContent:
    def __init__(self, body: bytes):
        self._body = body
        self.read_sizes = []

    async def read(self, size=-1):
        self.read_sizes.append(size)
        if not self._body:
            return b""
        if size is None or size < 0:
            chunk = self._body
            self._body = b""
            return chunk
        chunk = self._body[:size]
        self._body = self._body[size:]
        return chunk


class _StreamingAiohttpResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.content = _StreamingAiohttpContent(body)
        self.closed = False
        self.json = AsyncMock(side_effect=AssertionError("resp.json() should not be used"))
        self.text = AsyncMock(side_effect=AssertionError("resp.text() should not be used"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self):
        self.closed = True


class _StreamingAiohttpSession:
    def __init__(self, response: _StreamingAiohttpResponse):
        self.response = response
        self.post = MagicMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _discord_entry():
    """Return the live Discord PlatformEntry, importing lazily so plugin
    discovery is forced exactly once and patches survive across tests."""
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry
    discover_plugins()
    return platform_registry.get("discord")


class _patch_discord_sender:
    """Patch the Discord registry entry's ``standalone_sender_fn`` with the
    given mock and translate the production ``(pconfig, ...)`` call shape
    back to the pre-migration ``(token, ...)`` shape the test mocks expect.

    Use as a context manager:

        send_mock = AsyncMock(return_value={...})
        with _patch_discord_sender(send_mock):
            asyncio.run(_send_to_platform(Platform.DISCORD, ...))
        send_mock.assert_awaited_once_with("tok", "chat", "msg",
                                           thread_id=None, media_files=[])
    """

    def __init__(self, mock):
        self._mock = mock
        self._entry = None
        self._original = None

    async def _adapter(self, pconfig, chat_id, message, *, thread_id=None, media_files=None, caption=None):
        token = getattr(pconfig, "token", None)
        # Only forward caption= when set, so mocks written against the
        # pre-caption signature (no caption kwarg) keep working.
        extra = {"caption": caption} if caption is not None else {}
        return await self._mock(
            token, chat_id, message,
            thread_id=thread_id, media_files=media_files, **extra,
        )

    def __enter__(self):
        self._entry = _discord_entry()
        self._original = self._entry.standalone_sender_fn
        self._entry.standalone_sender_fn = self._adapter
        return self._mock

    def __exit__(self, exc_type, exc, tb):
        if self._entry is not None:
            self._entry.standalone_sender_fn = self._original
        return False


def _run_async_immediately(coro):
    return asyncio.run(coro)


def _make_config():
    discord_cfg = SimpleNamespace(enabled=True, token="***", extra={})
    return SimpleNamespace(
        platforms={Platform.DISCORD: discord_cfg},
    ), discord_cfg


class TestSendMessageTool:
    def test_media_tag_outside_allowed_roots_is_not_sent(self, tmp_path, monkeypatch):
        # This test exercises the strict-allowlist path; force strict mode on
        # and disable recency trust so the freshly-written tmp_path file is
        # not auto-accepted by the trust window. (Recency trust is covered
        # in test_platform_base.py. The public default flipped to non-strict
        # in 2026-05; this test pins strict on explicitly.)
        monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
        monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "0")
        config, discord_cfg = _make_config()
        secret = tmp_path / "secret.pdf"
        secret.write_bytes(b"%PDF secret")

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "discord:12345",
                        "message": f"hello\nMEDIA:{secret}",
                    }
                )
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.DISCORD,
            discord_cfg,
            "12345",
            "hello",
            thread_id=None,
            media_files=[],
            force_document=False,
        )

    def test_top_level_send_failure_redacts_query_token(self):
        config, _discord_cfg = _make_config()
        leaked = "very-secret-query-token-123456"

        def _raise_and_close(coro):
            coro.close()
            raise RuntimeError(
                f"transport error: https://api.example.com/send?access_token={leaked}"
            )

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_raise_and_close):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "discord:-1001",
                        "message": "hello",
                    }
                )
            )

        assert "error" in result
        assert leaked not in result["error"]
        assert "access_token=***" in result["error"]


# ---------------------------------------------------------------------------
# Regression: long messages are chunked before platform dispatch
# ---------------------------------------------------------------------------


class TestSendToPlatformChunking:
    def test_long_message_is_chunked(self):
        """Messages exceeding the platform limit are split into multiple sends."""
        send = AsyncMock(return_value={"success": True, "message_id": "1"})
        long_msg = "word " * 1000  # ~5000 chars, well over Discord's 2000 limit
        with _patch_discord_sender(send):
            result = asyncio.run(
                _send_to_platform(
                    Platform.DISCORD,
                    SimpleNamespace(enabled=True, token="***", extra={}),
                    "ch", long_msg,
                )
            )
        assert result["success"] is True
        assert send.await_count >= 3
        for call in send.await_args_list:
            assert len(call.args[2]) <= 2020  # each chunk fits the limit

    def test_matrix_media_uses_native_adapter_helper(self, tmp_path):
        doc_path = tmp_path / "test-send-message-matrix.pdf"
        doc_path.write_bytes(b"%PDF-1.4 test")

        try:
            helper = AsyncMock(return_value={"success": True, "platform": "matrix", "chat_id": "!room:example.com", "message_id": "$evt"})
            with patch("tools.send_message_tool._send_matrix_via_adapter", helper):
                result = asyncio.run(
                    _send_to_platform(
                        Platform.MATRIX,
                        SimpleNamespace(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.com"}),
                        "!room:example.com",
                        "here you go",
                        media_files=[(str(doc_path), False)],
                    )
                )

            assert result["success"] is True
            helper.assert_awaited_once()
            call = helper.await_args
            assert call.args[1] == "!room:example.com"
            assert call.args[2] == "here you go"
            assert call.kwargs["media_files"] == [(str(doc_path), False)]
        finally:
            doc_path.unlink(missing_ok=True)

class TestMatrixMediaLiveAdapterReuse:
    """Verify _send_matrix_via_adapter reuses the live gateway adapter
    when available, avoiding per-message E2EE re-init storms (#46310)."""

    def test_live_adapter_skips_connect_disconnect(self, tmp_path):
        """When a live gateway adapter exists, no connect() or disconnect()
        should be called — the persistent E2EE session is reused."""
        img_path = tmp_path / "photo.png"
        img_path.write_bytes(b"\x89PNG\r\n")

        calls = []

        class LiveAdapter:
            async def send(self, chat_id, message, metadata=None):
                calls.append(("send", chat_id, message))
                return SimpleNamespace(success=True, message_id="$text")

            async def send_image_file(self, chat_id, path, metadata=None):
                calls.append(("send_image_file", chat_id, path))
                return SimpleNamespace(success=True, message_id="$img")

        live_adapter = LiveAdapter()
        fake_runner = SimpleNamespace(
            adapters={Platform.MATRIX: live_adapter}
        )

        with patch(
            "gateway.run._gateway_runner_ref",
            return_value=fake_runner,
        ), patch.dict(
            sys.modules, {"plugins.platforms.matrix.adapter": SimpleNamespace()}
        ):
            result = asyncio.run(
                _send_matrix_via_adapter(
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "!room:example.com",
                    "here is an image",
                    media_files=[(str(img_path), False)],
                )
            )

        assert result["success"] is True
        assert result["message_id"] == "$img"
        # Only send + send_image_file; no connect / disconnect
        assert calls == [
            ("send", "!room:example.com", "here is an image"),
            ("send_image_file", "!room:example.com", str(img_path)),
        ]

    def test_live_adapter_not_available_falls_back_to_ephemeral(self, tmp_path):
        """When _gateway_runner_ref returns None, the ephemeral adapter
        path (connect + disconnect) is used as before."""
        doc_path = tmp_path / "doc.pdf"
        doc_path.write_bytes(b"%PDF-1.4")

        calls = []

        class EphemeralAdapter:
            def __init__(self, _config):
                pass

            async def connect(self):
                calls.append(("connect",))
                return True

            async def send(self, chat_id, message, metadata=None):
                calls.append(("send", chat_id, message))
                return SimpleNamespace(success=True, message_id="$txt")

            async def send_document(self, chat_id, path, metadata=None):
                calls.append(("send_document", chat_id, path))
                return SimpleNamespace(success=True, message_id="$doc")

            async def disconnect(self):
                calls.append(("disconnect",))

        fake_module = SimpleNamespace(MatrixAdapter=EphemeralAdapter)

        with patch(
            "gateway.run._gateway_runner_ref", return_value=None
        ), patch.dict(sys.modules, {"plugins.platforms.matrix.adapter": fake_module}):
            result = asyncio.run(
                _send_matrix_via_adapter(
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "!room:example.com",
                    "report attached",
                    media_files=[(str(doc_path), False)],
                )
            )

        assert result["success"] is True
        assert calls == [
            ("connect",),
            ("send", "!room:example.com", "report attached"),
            ("send_document", "!room:example.com", str(doc_path)),
            ("disconnect",),
        ]

# ---------------------------------------------------------------------------
# HTML auto-detection in Telegram send
# ---------------------------------------------------------------------------


class TestSendToPlatformWhatsapp:
    def test_whatsapp_routes_via_local_bridge_sender(self):
        """WhatsApp delivery routes through the plugin's registry
        standalone_sender_fn (was tools.send_message_tool._send_whatsapp
        before the #41112 plugin migration)."""
        from hermes_cli.plugins import discover_plugins
        from gateway.platform_registry import platform_registry
        discover_plugins()
        chat_id = "test-user@lid"
        async_mock = AsyncMock(return_value={"success": True, "platform": "whatsapp", "chat_id": chat_id, "message_id": "abc123"})

        wa_entry = platform_registry.get("whatsapp")
        original_sender = wa_entry.standalone_sender_fn
        wa_entry.standalone_sender_fn = async_mock
        try:
            result = asyncio.run(
                _send_to_platform(
                    Platform.WHATSAPP,
                    SimpleNamespace(enabled=True, token=None, extra={"bridge_port": 3000}),
                    chat_id,
                    "hello from hermes",
                )
            )
        finally:
            wa_entry.standalone_sender_fn = original_sender

        assert result["success"] is True
        # _registry_standalone_send passes (pconfig, chat_id, message, thread_id=None)
        async_mock.assert_awaited_once()
        _call = async_mock.await_args
        assert _call.args[1] == chat_id
        assert _call.args[2] == "hello from hermes"


# ---------------------------------------------------------------------------
# Tests for Discord thread_id support
# ---------------------------------------------------------------------------


class TestParseTargetRef:
    """_parse_target_ref extracts (chat_id, thread_id, is_explicit) per platform.

    The tables below cover every explicit target form each platform accepts,
    the forms that must fall through to directory resolution, and the
    cross-platform scoping guards (a Slack ID is not a Discord target, a
    WhatsApp JID is not a Signal target, ...).
    """

    def test_explicit_targets_round_trip_chat_and_thread(self):
        cases = [
            # Discord: snowflake, optional :thread, surrounding whitespace
            ("discord", "-1001234567890:17585", "-1001234567890", "17585"),
            ("discord", "1003724596514", "1003724596514", None),
            ("discord", "  123456:789  ", "123456", "789"),
            # Matrix: room id, room:thread-event, user MXID
            ("matrix", "!HLOQwxYGgFPMPJUSNR:matrix.org:$thread123:matrix.org",
             "!HLOQwxYGgFPMPJUSNR:matrix.org", "$thread123:matrix.org"),
            ("matrix", "!HLOQwxYGgFPMPJUSNR:matrix.org",
             "!HLOQwxYGgFPMPJUSNR:matrix.org", None),
            ("matrix", "@hermes:matrix.org", "@hermes:matrix.org", None),
            # Phone platforms: E.164 keeps its '+' (the '+' fails the isdigit
            # rule and the directory cannot resolve a raw number).
            ("whatsapp", "+15551234567", "+15551234567", None),
            # WhatsApp native JIDs. Regression: group (@g.us) and linked-identity
            # (@lid) JIDs matched no branch and silently fell through to the
            # configured home DM instead of the requested group.
            ("whatsapp", "120363408391911677@g.us", "120363408391911677@g.us", None),
            ("whatsapp", "19255551234@s.whatsapp.net", "19255551234@s.whatsapp.net", None),
            ("whatsapp", "149606612619433@lid", "149606612619433@lid", None),
            ("whatsapp", "status@broadcast", "status@broadcast", None),
            ("whatsapp", "120363000000000000@newsletter",
             "120363000000000000@newsletter", None),
        ]
        for platform, target, expected_chat, expected_thread in cases:
            chat_id, thread_id, is_explicit = _parse_target_ref(platform, target)
            label = f"{platform}:{target}"
            assert is_explicit is True, label
            assert chat_id == expected_chat, label
            assert thread_id == expected_thread, label

    def test_non_explicit_targets_fall_through_to_resolution(self):
        cases = [
            ("matrix", "#general:matrix.org"),   # alias needs resolution
            ("whatsapp", "general"),             # friendly name, not a JID
            ("discord", "not-a-snowflake"),      # non-numeric ids need the directory
        ]
        for platform, target in cases:
            chat_id, _, is_explicit = _parse_target_ref(platform, target)
            assert is_explicit is False, f"{platform}:{target}"

    def test_prefixes_and_suffixes_are_platform_scoped(self):
        """A form that is explicit on one platform must not leak to another."""
        cases = [
            ("discord", "@someone"),
            ("discord", "+15551234567"),
            ("matrix", "+15551234567"),
            ("matrix", "120363408391911677@g.us"),   # a WhatsApp JID is not a Matrix id
            ("matrix", "149606612619433@lid"),      # a Signal LID is not a Matrix id
            ("discord", "C0B0QV5434G"),
            ("discord", "user@example.com"),
        ]
        for platform, target in cases:
            assert _parse_target_ref(platform, target)[2] is False, f"{platform}:{target}"


class TestSendDiscordThreadId:
    """_send_discord uses thread_id when provided."""

    @staticmethod
    def _build_mock(response_status, response_data=None, response_text="error body"):
        """Build a properly-structured aiohttp mock chain.

        session.post() returns a context manager yielding mock_resp.
        """
        mock_resp = MagicMock()
        mock_resp.status = response_status
        mock_resp.json = AsyncMock(return_value=response_data or {"id": "msg123"})
        mock_resp.text = AsyncMock(return_value=response_text)

        # mock_resp as async context manager (for "async with session.post(...) as resp")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_resp)

        return mock_session, mock_resp

    def _run(self, token, chat_id, message, thread_id=None):
        return asyncio.run(_send_discord(token, chat_id, message, thread_id=thread_id))

    def test_with_thread_id_uses_thread_endpoint(self):
        """When thread_id is provided, sends to /channels/{thread_id}/messages."""
        mock_session, _ = self._build_mock(200)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            self._run("tok", "999888777", "hello from thread", thread_id="555444333")
        call_url = mock_session.post.call_args.args[0]
        assert call_url == "https://discord.com/api/v10/channels/555444333/messages"


    def test_success_response_json_read_is_bounded(self):
        """Standalone Discord sends parse success JSON through the bounded reader."""
        body = b'{"id":"bounded-json"}'
        response = _StreamingAiohttpResponse(200, body)
        session = _StreamingAiohttpSession(response)

        with patch("aiohttp.ClientSession", return_value=session):
            result = self._run("tok", "111", "hi", thread_id="999")

        assert result["success"] is True
        assert result["message_id"] == "bounded-json"
        assert response.content.read_sizes[0] == _DISCORD_STANDALONE_JSON_BODY_LIMIT_BYTES + 1
        response.json.assert_not_awaited()
        response.text.assert_not_awaited()

class TestSendToPlatformDiscordThread:
    """_send_to_platform passes thread_id through to _send_discord."""

    def test_discord_thread_id_passed_to_send_discord(self):
        """Discord platform with thread_id passes it to _send_discord."""
        send_mock = AsyncMock(return_value={"success": True, "message_id": "1"})

        with _patch_discord_sender(send_mock):
            result = asyncio.run(
                _send_to_platform(
                    Platform.DISCORD,
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "-1001234567890",
                    "hello thread",
                    thread_id="17585",
                )
            )

        assert result["success"] is True
        send_mock.assert_awaited_once()
        _, call_kwargs = send_mock.await_args
        assert call_kwargs["thread_id"] == "17585"

# ---------------------------------------------------------------------------
# Discord media attachment support
# ---------------------------------------------------------------------------


class TestSendDiscordMedia:
    """_send_discord uploads media files via multipart/form-data."""

    @staticmethod
    def _build_mock(response_status, response_data=None, response_text="error body"):
        """Build a properly-structured aiohttp mock chain."""
        mock_resp = MagicMock()
        mock_resp.status = response_status
        mock_resp.json = AsyncMock(return_value=response_data or {"id": "msg123"})
        mock_resp.text = AsyncMock(return_value=response_text)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_resp)

        return mock_session, mock_resp

    def test_text_and_media_sends_both(self, tmp_path):
        """Text message is sent first, then each media file as multipart."""
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG fake image data")

        mock_session, _ = self._build_mock(200, {"id": "msg999"})
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(
                _send_discord("tok", "111", "hello", media_files=[(str(img), False)])
            )

        assert result["success"] is True
        assert result["message_id"] == "msg999"
        # Two POSTs: one text JSON, one multipart upload
        assert mock_session.post.call_count == 2


    def test_no_text_no_media_returns_error(self):
        """Empty text with no media returns error dict."""
        mock_session, _ = self._build_mock(200)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(
                _send_discord("tok", "555", "", media_files=[])
            )

        # Text is empty but media_files is empty, so text POST fires
        # (the "skip text if media present" condition isn't met)
        assert result["success"] is True

class TestSendToPlatformDiscordMedia:
    """_send_to_platform routes Discord media correctly."""

    def test_media_files_passed_on_last_chunk_only(self):
        """Discord media_files are only passed on the final chunk."""
        call_log = []

        async def mock_send_discord(token, chat_id, message, thread_id=None, media_files=None):
            call_log.append({"message": message, "media_files": media_files or []})
            return {"success": True, "platform": "discord", "chat_id": chat_id, "message_id": "1"}

        # A message long enough to get chunked (Discord limit is 2000)
        long_msg = "A" * 1900 + " " + "B" * 1900

        with _patch_discord_sender(AsyncMock(side_effect=mock_send_discord)):
            result = asyncio.run(
                _send_to_platform(
                    Platform.DISCORD,
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "999",
                    long_msg,
                    media_files=[("/fake/img.png", False)],
                )
            )

        assert result["success"] is True
        assert len(call_log) == 2  # Message was chunked
        assert call_log[0]["media_files"] == []  # First chunk: no media
        assert call_log[1]["media_files"] == [("/fake/img.png", False)]  # Last chunk: media attached

class TestSendMatrixUrlEncoding:
    """The matrix plugin's _standalone_send URL-encodes Matrix room IDs in the
    API path (was tools.send_message_tool._send_matrix before #41112)."""

    def test_room_id_is_percent_encoded_in_url(self):
        """Matrix room IDs with ! and : are percent-encoded in the PUT URL."""

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"event_id": "$evt123"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            from plugins.platforms.matrix.adapter import _standalone_send
            result = asyncio.get_event_loop().run_until_complete(
                _standalone_send(
                    SimpleNamespace(token="test_token", extra={"homeserver": "https://matrix.example.org"}),
                    "!HLOQwxYGgFPMPJUSNR:matrix.org",
                    "hello",
                )
            )

        assert result["success"] is True
        # Verify the URL was called with percent-encoded room ID
        put_url = mock_session.put.call_args[0][0]
        assert "%21HLOQwxYGgFPMPJUSNR%3Amatrix.org" in put_url
        assert "!HLOQwxYGgFPMPJUSNR:matrix.org" not in put_url


# ---------------------------------------------------------------------------
# Tests for _derive_forum_thread_name
# ---------------------------------------------------------------------------


class TestDeriveForumThreadName:
    def test_first_line_heading_and_fallback_handling(self):
        cases = [
            ("Hello world", "Hello world"),
            ("First line\nSecond line", "First line"),
            ("  Title  \nBody", "Title"),
            ("## My Heading", "My Heading"),
            ("### Deep heading", "Deep heading"),
            ("", "New Post"),
            ("   \n  ", "New Post"),
            ("###", "New Post"),
        ]
        for message, expected in cases:
            assert _derive_forum_thread_name(message) == expected, repr(message)

        # Titles are capped at Discord's 100-char thread-name limit.
        assert len(_derive_forum_thread_name("A" * 200)) == 100


# ---------------------------------------------------------------------------
# Tests for _send_discord with forum channel support
# ---------------------------------------------------------------------------


class TestSendDiscordForum:
    """_send_discord creates thread posts for forum channels."""

    @staticmethod
    def _build_mock(response_status, response_data=None, response_text="error body"):
        mock_resp = MagicMock()
        mock_resp.status = response_status
        mock_resp.json = AsyncMock(return_value=response_data or {})
        mock_resp.text = AsyncMock(return_value=response_text)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.get = MagicMock(return_value=mock_resp)

        return mock_session, mock_resp

    def test_directory_forum_creates_thread(self):
        """Directory says 'forum' — creates a thread post."""
        thread_data = {
            "id": "t123",
            "message": {"id": "m456"},
        }
        mock_session, _ = self._build_mock(200, response_data=thread_data)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("gateway.channel_directory.lookup_channel_type", return_value="forum"):
            result = asyncio.run(
                _send_discord("tok", "forum_ch", "Hello forum")
            )

        assert result["success"] is True
        assert result["thread_id"] == "t123"
        assert result["message_id"] == "m456"
        # Should POST to threads endpoint, not messages
        call_url = mock_session.post.call_args.args[0]
        assert "/threads" in call_url
        assert "/messages" not in call_url


    def test_forum_thread_creation_error(self):
        """Forum thread creation returning non-200/201 returns an error dict."""
        mock_session, _ = self._build_mock(403, response_text="Forbidden")

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("gateway.channel_directory.lookup_channel_type", return_value="forum"):
            result = asyncio.run(
                _send_discord("tok", "forum_ch", "Hello")
            )

        assert "error" in result
        assert "403" in result["error"]


class TestSendToPlatformDiscordForum:
    """_send_to_platform delegates forum detection to _send_discord."""

    def test_send_to_platform_discord_delegates_to_send_discord(self):
        """Discord messages are routed through _send_discord, which handles forum detection."""
        send_mock = AsyncMock(return_value={"success": True, "message_id": "1"})

        with _patch_discord_sender(send_mock):
            result = asyncio.run(
                _send_to_platform(
                    Platform.DISCORD,
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "forum_ch",
                    "Hello forum",
                )
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            "tok", "forum_ch", "Hello forum", media_files=[], thread_id=None,
        )

# ---------------------------------------------------------------------------
# Tests for _send_discord forum + media multipart upload
# ---------------------------------------------------------------------------


class TestSendDiscordForumMedia:
    """_send_discord uploads media as part of the starter message when the target is a forum."""

    @staticmethod
    def _build_thread_resp(thread_id="th_999", msg_id="msg_500"):
        resp = MagicMock()
        resp.status = 201
        resp.json = AsyncMock(return_value={"id": thread_id, "message": {"id": msg_id}})
        resp.text = AsyncMock(return_value="")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        return resp

    def test_forum_with_media_uses_multipart(self, tmp_path, monkeypatch):
        """Forum + media → single multipart POST to /threads carrying the starter + files."""
        from tools import send_message_tool as smt

        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNGbytes")

        monkeypatch.setattr(smt, "lookup_channel_type", lambda p, cid: "forum", raising=False)
        monkeypatch.setattr(
            "gateway.channel_directory.lookup_channel_type", lambda p, cid: "forum"
        )

        thread_resp = self._build_thread_resp()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(return_value=thread_resp)

        post_calls = []
        orig_post = session.post

        def track_post(url, **kwargs):
            post_calls.append({"url": url, "kwargs": kwargs})
            return thread_resp

        session.post = MagicMock(side_effect=track_post)

        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _send_discord("tok", "forum_ch", "Thread title\nbody", media_files=[(str(img), False)])
            )

        assert result["success"] is True
        assert result["thread_id"] == "th_999"
        assert result["message_id"] == "msg_500"
        # Exactly one POST — the combined thread-creation + attachments call
        assert len(post_calls) == 1
        assert post_calls[0]["url"].endswith("/threads")
        # Multipart form, not JSON
        assert post_calls[0]["kwargs"].get("data") is not None
        assert post_calls[0]["kwargs"].get("json") is None

    def test_forum_missing_media_file_collected_as_warning(self, tmp_path, monkeypatch):
        """Missing media files produce warnings but the thread is still created."""
        monkeypatch.setattr(
            "gateway.channel_directory.lookup_channel_type", lambda p, cid: "forum"
        )

        thread_resp = self._build_thread_resp()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(return_value=thread_resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _send_discord(
                    "tok", "forum_ch", "hi",
                    media_files=[("/nonexistent/does-not-exist.png", False)],
                )
            )

        assert result["success"] is True
        assert "warnings" in result
        assert any("not found" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Tests for the process-local forum-probe cache
# ---------------------------------------------------------------------------


class TestForumProbeCache:
    """_DISCORD_CHANNEL_TYPE_PROBE_CACHE memoizes forum detection results."""

    def setup_method(self):
        from plugins.platforms.discord import adapter as discord_adapter
        discord_adapter._DISCORD_CHANNEL_TYPE_PROBE_CACHE.clear()

    def test_probe_result_is_memoized(self, monkeypatch):
        """An API-probed channel type is cached so subsequent sends skip the probe."""
        monkeypatch.setattr(
            "gateway.channel_directory.lookup_channel_type", lambda p, cid: None
        )

        # First probe response: type=15 (forum)
        probe_resp = MagicMock()
        probe_resp.status = 200
        probe_resp.json = AsyncMock(return_value={"type": 15})
        probe_resp.__aenter__ = AsyncMock(return_value=probe_resp)
        probe_resp.__aexit__ = AsyncMock(return_value=None)

        thread_resp = MagicMock()
        thread_resp.status = 201
        thread_resp.json = AsyncMock(return_value={"id": "t1", "message": {"id": "m1"}})
        thread_resp.__aenter__ = AsyncMock(return_value=thread_resp)
        thread_resp.__aexit__ = AsyncMock(return_value=None)

        probe_session = MagicMock()
        probe_session.__aenter__ = AsyncMock(return_value=probe_session)
        probe_session.__aexit__ = AsyncMock(return_value=None)
        probe_session.get = MagicMock(return_value=probe_resp)

        thread_session = MagicMock()
        thread_session.__aenter__ = AsyncMock(return_value=thread_session)
        thread_session.__aexit__ = AsyncMock(return_value=None)
        thread_session.post = MagicMock(return_value=thread_resp)

        # Two _send_discord calls: first does probe + thread-create; second should skip probe
        from plugins.platforms.discord import adapter as discord_adapter

        sessions_created = []

        def session_factory(**kwargs):
            # Alternate: each new ClientSession() call returns a probe_session, thread_session pair
            idx = len(sessions_created)
            sessions_created.append(idx)
            # Returns the same mocks; the real code opens a probe session then a thread session.
            # Hand out probe_session if this is the first time called within _send_discord,
            # otherwise thread_session.
            if idx % 2 == 0:
                return probe_session
            return thread_session

        with patch("aiohttp.ClientSession", side_effect=session_factory):
            result1 = asyncio.run(_send_discord("tok", "ch1", "first"))
        assert result1["success"] is True
        assert discord_adapter._probe_is_forum_cached("ch1") is True

        # Second call: cache hits, no new probe session needed. We need to only
        # return thread_session now since probe is skipped.
        sessions_created.clear()
        with patch("aiohttp.ClientSession", return_value=thread_session):
            result2 = asyncio.run(_send_discord("tok", "ch1", "second"))
        assert result2["success"] is True
        # Only one session opened (thread creation) — no probe session this time
        # (verified by not raising from our side_effect exhaustion)


# ── _send_via_adapter standalone fallback ────────────────────────────────


class _FakePlatform:
    """Stand-in for the gateway.config.Platform enum.  Holds the .value
    attribute consulted by ``_send_via_adapter`` for registry lookups."""

    def __init__(self, value):
        self.value = value


class TestSendViaAdapterStandaloneFallback:
    """Coverage for the out-of-process plugin-platform send path.

    When the gateway runner is not in this process (e.g. ``hermes cron``
    runs separately from ``hermes gateway``), ``_send_via_adapter`` should
    fall through to the plugin's ``standalone_sender_fn`` registered on
    its ``PlatformEntry``.  Without the hook, the existing error string
    is returned (with a more helpful tail).
    """

    @staticmethod
    def _make_entry(send_fn):
        from gateway.platform_registry import PlatformEntry

        return PlatformEntry(
            name="fakeplatform",
            label="Fake",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            standalone_sender_fn=send_fn,
        )

    @pytest.mark.asyncio
    async def test_standalone_sender_fn_raises_is_caught_and_formatted(self, monkeypatch):
        """Hook raises: error dict has 'Plugin standalone send failed: ...'"""
        from tools.send_message_tool import _send_via_adapter
        from gateway.platform_registry import platform_registry

        async def boom(pconfig, chat_id, message, **kwargs):
            raise ValueError("boom!")

        platform_registry.register(self._make_entry(boom))
        try:
            monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)

            result = await _send_via_adapter(
                _FakePlatform("fakeplatform"),
                SimpleNamespace(extra={}),
                "chat-1",
                "hi",
            )
        finally:
            platform_registry.unregister("fakeplatform")

        assert result == {"error": "Plugin standalone send failed: boom!"}

def test_not_configured_error_names_resolved_home_and_consulted_sources(tmp_path, monkeypatch):
    """The 'not configured' error names the files this process actually read (resolved home, not a
    hardcoded ``~/.hermes``) and what each source held, so a Windows/profile home user can fix the right file."""
    from gateway.config import GatewayConfig
    from tools.send_message_tool import _resolve_platform_config

    home = tmp_path / "AppData" / "Local" / "hermes"
    home.mkdir(parents=True)
    (home / ".env").write_text("FIRECRAWL_API_KEY=x\n", encoding="utf-8")
    (home / "config.yaml").write_text("platforms:\n  discord:\n    enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    _, _, _, err = _resolve_platform_config("discord", GatewayConfig())

    assert "~/.hermes" not in err
    assert f"{home / '.env'} (no DISCORD_BOT_TOKEN)" in err
    assert f"{home / 'config.yaml'} (platforms.discord.enabled: false)" in err
    assert "environment (DISCORD_BOT_TOKEN unset)" in err


def test_not_configured_error_names_default_root_gateway_and_secret_sources(tmp_path, monkeypatch):
    """Under ``HERMES_HOME=<root>/profiles/<p>`` the error says a live gateway from the default root has the
    platform connected (its credentials never came from this profile's ``.env``) and lists external secret
    sources by name only (#114272 step 5)."""
    import json
    import os

    from gateway.config import GatewayConfig
    from tools.send_message_tool import _resolve_platform_config

    root = tmp_path / "hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    (root / "gateway_state.json").write_text(
        json.dumps({"pid": os.getpid(), "platforms": {"discord": {"state": "connected"}}}), encoding="utf-8")
    (profile / ".env").write_text("FIRECRAWL_API_KEY=x\n", encoding="utf-8")
    (profile / "config.yaml").write_text(
        "secrets:\n  bitwarden:\n    enabled: false\n    session_token: SECRET-VALUE\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    _, _, _, err = _resolve_platform_config("discord", GatewayConfig())

    assert (f"A gateway (pid {os.getpid()}) running from {root} has discord connected; "
            f"this shell is scoped to profile home {profile} whose .env has no DISCORD_BOT_TOKEN.") in err
    assert "external secret sources (bitwarden: disabled)" in err
    assert "SECRET-VALUE" not in err
