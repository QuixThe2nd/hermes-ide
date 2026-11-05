"""Matrix inbox-room sender authorization gate for hub-side adapters.

Since the hub transport migration, the Matrix sync loop lives in the hub
process where no runner authorization callback is registered, so the inbox
router must fall back to the adapter's own intake allowlist
(``matrix.allowed_users``) when `_is_sender_authorized` returns None.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter_inbox import MatrixInboxRouter

INBOX_ROOM = "!inbox:example.org"


def _make_adapter(allowed_users):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@hermes:example.org",
            "allowed_users": allowed_users,
        },
    )
    adapter = MatrixAdapter(config)
    adapter._startup_ts = time.time() - 10
    adapter._client = None  # no live homeserver; stubbed seams below
    return adapter


def _make_router(adapter):
    router = MatrixInboxRouter(adapter, {INBOX_ROOM: None})
    router._source_encryption_algorithm = AsyncMock(return_value=None)
    adapter.create_room = AsyncMock(return_value="!child:example.org")
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="$mid"))
    adapter._handle_text_message = AsyncMock(
        return_value=SimpleNamespace(_gateway_accepted=True))
    adapter._invalidate_room_identities = MagicMock()
    return router


async def _route(router, sender="@sender:example.org", event_id="$evt1"):
    return await router.route_inbound(
        INBOX_ROOM, sender, event_id, time.time(), {"body": "hello", "msgtype": "m.text"},
        {}, "m.text", is_command=False)


@pytest.mark.asyncio
class TestInboxHubAuth:
    async def test_hub_allowlisted_sender_creates_child_room(self):
        adapter = _make_adapter(["@sender:example.org"])
        assert adapter._authorization_check is None
        router = _make_router(adapter)
        assert await _route(router) is True
        adapter.create_room.assert_awaited_once()

    async def test_hub_stranger_is_fail_closed(self):
        adapter = _make_adapter(["@sender:example.org"])
        router = _make_router(adapter)
        assert await _route(router, sender="@stranger:example.org") is True
        adapter.create_room.assert_not_awaited()

    async def test_registered_check_deny_overrides_allowlist(self):
        adapter = _make_adapter(["@sender:example.org"])
        adapter._authorization_check = lambda *a, **k: False
        router = _make_router(adapter)
        assert await _route(router) is True
        adapter.create_room.assert_not_awaited()

    async def test_registered_check_true_admits_non_allowlisted_sender(self):
        adapter = _make_adapter(["@someone:example.org"])
        adapter._authorization_check = lambda *a, **k: True
        router = _make_router(adapter)
        assert await _route(router) is True
        adapter.create_room.assert_awaited_once()

    async def test_empty_allowlist_without_check_is_rejected(self):
        adapter = _make_adapter([])
        router = _make_router(adapter)
        assert await _route(router) is True
        adapter.create_room.assert_not_awaited()


@pytest.mark.asyncio
class TestOriginLinkPreview:
    async def _origin_message(self, content, event_id="$evt1"):
        adapter = _make_adapter(["@sender:example.org"])
        router = _make_router(adapter)
        assert await router.route_inbound(
            INBOX_ROOM, "@sender:example.org", event_id, time.time(), content, {},
            "m.text", is_command=False) is True
        child_sends = [c for c in adapter.send.await_args_list if c.args[0] == "!child:example.org"]
        return child_sends[0].args[1]

    async def test_preview_quotes_body_text(self):
        text = await self._origin_message({"body": "hello world", "msgtype": "m.text"})
        assert text == (
            "Started from [the original inbox message]"
            "(https://matrix.to/#/%21inbox%3Aexample.org/%24evt1).\n\n> hello world")

    async def test_no_body_sends_bare_link(self):
        # Body-less events are filtered by intake before routing; assert the
        # degradation directly on the link-text builder.
        from plugins.platforms.matrix.adapter_inbox import _origin_link_text
        link = ("Started from [the original inbox message]"
                "(https://matrix.to/#/%21inbox%3Aexample.org/%24evt1).")
        for content in ({"body": None}, {}, {"body": ""}, {"body": "   \n  "}):
            assert _origin_link_text(INBOX_ROOM, "$evt1", content) == link

    async def test_formatted_body_is_ignored(self):
        text = await self._origin_message({
            "body": "plain text", "format": "org.matrix.custom.html",
            "formatted_body": "<b>plain text</b>"})
        assert text.endswith("\n\n> plain text")

    async def test_newlines_and_whitespace_collapse(self):
        text = await self._origin_message({"body": "line one\n\nline   two\n\tthree"})
        assert text.endswith("\n\n> line one line two three")

    async def test_leading_quote_markers_are_stripped(self):
        text = await self._origin_message({"body": "> quoted reply\n>> deeper\nown words"})
        assert text.endswith("\n\n> quoted reply deeper own words")

    async def test_long_body_is_capped(self):
        text = await self._origin_message({"body": "x" * 600})
        quote = text.rsplit("\n\n> ", 1)[1]
        assert len(quote) == 501  # 500 chars + ellipsis
        assert quote.endswith("…")
