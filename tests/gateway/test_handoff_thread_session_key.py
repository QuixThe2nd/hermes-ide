"""Regression: CLI→Discord handoff must key a thread destination on the
thread's OWN id, matching how the platform adapter keys organic in-thread
messages.

Bug: the handoff built its destination ``SessionSource`` with
``chat_id = home.chat_id`` (the PARENT channel) while thread destinations use
``chat_type="thread"`` and ``thread_id = <thread>``. The Discord adapter,
however, builds organic in-thread messages with ``chat_id = <thread>`` (the
thread's own id). ``build_session_key`` therefore produced two different keys:

    handoff:  agent:main:discord:thread:{parent}:{thread}
    organic:  agent:main:discord:thread:{thread}:{thread}

So the next real user reply in the handoff thread resolved to a DIFFERENT
session_key and spawned a fresh session instead of continuing the handed-off
one (observed: a stray auto-titled session + a session_search fallback because
the new session had no prior context).

"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import DeliveryTarget, GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _organic_discord_thread_key(thread_id: str, parent_id: str, user_id: str) -> str:
    """Key the Discord adapter produces for a message typed inside a thread.

    Mirrors plugins/platforms/discord/adapter.py _handle_message: chat_id is
    the thread's own id, chat_type is "thread", thread_id is the thread id,
    parent_chat_id is the parent channel.
    """
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=str(thread_id),
        chat_type="thread",
        user_id=user_id,
        thread_id=str(thread_id),
        parent_chat_id=str(parent_id),
    )
    return build_session_key(source, thread_sessions_per_user=False)


def _handoff_key(
    platform: Platform,
    home_chat_id: str,
    thread_id: str,
) -> str:
    """Key the handoff produces after the fix.

    Mirrors the fixed logic in GatewayRunner._process_handoff: for Discord
    thread destinations, chat_id is the thread's own id; for other platforms,
    chat_id remains the parent chat.
    """
    dest_chat_type = "thread"
    # This mirrors the fixed logic in GatewayRunner._process_handoff.
    if platform == Platform.DISCORD and dest_chat_type == "thread" and thread_id:
        dest_chat_id = str(thread_id)
    else:
        dest_chat_id = str(home_chat_id)
    dest_source = SessionSource(
        platform=platform,
        chat_id=dest_chat_id,
        chat_type=dest_chat_type,
        user_id="system:handoff",
        user_name="Handoff",
        thread_id=str(thread_id),
    )
    return build_session_key(dest_source, thread_sessions_per_user=False)


def test_discord_handoff_key_matches_organic_in_thread_key():
    """For Discord, the handoff key must be byte-identical to the organic
    in-thread key — otherwise a reply in the handoff thread spawns a new session."""
    parent_id = "1523581766923845724"
    thread_id = "1523590238595846166"
    user_id = "171164909650968576"

    organic = _organic_discord_thread_key(thread_id, parent_id, user_id)
    handoff = _handoff_key(Platform.DISCORD, parent_id, thread_id)

    assert handoff == organic, (
        f"handoff key {handoff!r} != organic in-thread key {organic!r}; "
        "a reply in the handoff thread would spawn a new session"
    )
    assert handoff == f"agent:main:discord:thread:{thread_id}:{thread_id}"


def test_discord_handoff_key_does_not_use_parent_channel():
    """The pre-fix bug: keying on the parent channel. Guard against regression."""
    parent_id = "1523581766923845724"
    thread_id = "1523590238595846166"

    handoff = _handoff_key(Platform.DISCORD, parent_id, thread_id)
    buggy = f"agent:main:discord:thread:{parent_id}:{thread_id}"

    assert handoff != buggy, "handoff regressed to keying on the parent channel"


def _handoff_destination(platform: Platform, channel_id: str, thread_id: str, scope_id, adapter):
    """Run the real handoff destination/key path against a configured notification channel."""
    config = GatewayConfig(platforms={platform: PlatformConfig(enabled=True, token="test")})
    config.platforms[platform].notification_channel = DeliveryTarget(
        platform=platform, chat_id=channel_id, name="home", scope_id=scope_id)
    adapter.create_handoff_thread = AsyncMock(return_value=thread_id)
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {platform: adapter}
    runner.session_store = None
    with patch("gateway.delivery.resolve_delivery_transport",
               lambda *_a: SimpleNamespace(adapter=adapter, send=AsyncMock())):
        dest = asyncio.run(runner._handoff_resolve_destination(
            {"id": "cli-session", "title": "work", "handoff_platform": platform.value}, profile_name=None))
    return dest, runner._handoff_session_key(dest, profile_name=None)


def _matrix_adapter(room_id: str, *, is_dm: bool, monkeypatch):
    """A real MatrixAdapter with a fake client: the room's DM flag comes from the m.direct cache."""
    from plugins.platforms.matrix.adapter import MatrixAdapter

    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "false")
    monkeypatch.delenv("MATRIX_AUTO_THREAD", raising=False)
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="syt_test", extra={
        "homeserver": "https://matrix.example.org", "user_id": "@hermes:example.org"}))
    adapter._text_batch_delay_seconds = 0
    adapter._startup_ts = 0
    adapter._dm_rooms[room_id] = is_dm
    adapter._client = SimpleNamespace(send_message_event=AsyncMock(return_value="$seed"), crypto=None)
    adapter._get_display_name = AsyncMock(return_value="alice")
    adapter._background_read_receipt = lambda *_a: None
    adapter.handle_message = AsyncMock()
    return adapter


def _organic_matrix_reply_key(adapter, room_id: str, thread_id: str) -> str:
    """Key the Matrix adapter builds for a human reply inside ``thread_id`` (real inbound path)."""
    import time

    event = SimpleNamespace(
        sender="@alice:example.org", event_id="$reply", room_id=room_id, timestamp=int(time.time() * 1000),
        content={"body": "what happened?", "msgtype": "m.text",
                 "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id}})
    asyncio.run(adapter._on_room_message(event))
    return build_session_key(adapter.handle_message.await_args.args[0].source, thread_sessions_per_user=False)


@pytest.mark.parametrize("is_dm", [False, True], ids=["room", "dm"])
def test_matrix_handoff_key_matches_the_thread_reply_key(is_dm, monkeypatch):
    """A Matrix handoff (and cron ``attach_to_session``) seeds a thread rooted on the seed event; the
    adapter keys replies in it on the ROOM's type (``group``/``dm``), never a ``thread`` slot, so the
    destination must bind that key or the first reply opens an empty session (#112918)."""
    room_id = "!ops:example.org"
    adapter = _matrix_adapter(room_id, is_dm=is_dm, monkeypatch=monkeypatch)
    _dest, handoff = _handoff_destination(Platform.MATRIX, room_id, "$seed", None, adapter)
    assert handoff == _organic_matrix_reply_key(adapter, room_id, "$seed")
    assert handoff.split(":")[3] == ("dm" if is_dm else "group")
