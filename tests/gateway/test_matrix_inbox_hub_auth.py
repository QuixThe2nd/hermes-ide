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
from plugins.platforms.matrix.adapter_inbox import MatrixInboxRouter, _matrix_link

INBOX_ROOM = "!inbox:example.org"
CHILD_ROOM = "!child:example.org"


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


async def _route(router, sender="@sender:example.org", event_id="$evt1", body="hello"):
    return await router.route_inbound(
        INBOX_ROOM, sender, event_id, time.time(), {"body": body, "msgtype": "m.text"},
        {}, "m.text", is_command=False)


def _child_origin_link_text(adapter):
    for call in adapter.send.await_args_list:
        if call.args and call.args[0] == CHILD_ROOM:
            return call.args[1]
    pytest.fail("no origin-link send to child room")


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

    async def test_origin_link_quotes_original_body_in_child_room(self):
        adapter = _make_adapter(["@sender:example.org"])
        router = _make_router(adapter)
        event_id = "$evt1"
        await _route(router, event_id=event_id)
        msg = _child_origin_link_text(adapter)
        assert "\n\n> hello" in msg
        assert "Started from [the original inbox message](" in msg
        assert _matrix_link(INBOX_ROOM, event_id) in msg

    async def test_origin_link_multiline_body_blockquoted_per_line(self):
        adapter = _make_adapter(["@sender:example.org"])
        router = _make_router(adapter)
        event_id = "$evt1"
        await _route(router, event_id=event_id, body="line1\nline2")
        msg = _child_origin_link_text(adapter)
        assert "> line1\n> line2" in msg
        assert "Started from [the original inbox message](" in msg
        assert _matrix_link(INBOX_ROOM, event_id) in msg

    async def test_origin_link_empty_body_omits_blockquote(self):
        adapter = _make_adapter(["@sender:example.org"])
        router = _make_router(adapter)
        event_id = "$evt1"
        await _route(router, event_id=event_id, body="")
        msg = _child_origin_link_text(adapter)
        assert msg == f"Started from [the original inbox message]({_matrix_link(INBOX_ROOM, event_id)})."
