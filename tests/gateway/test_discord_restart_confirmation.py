"""The ``restart`` tool's Discord confirmation is one dedicated embed.

``send_restart_confirmation`` owns the Discord presentation of the restart
confirm gate: a restart-themed embed (not the generic clarify card) that is
the only place the prompt text appears, the requester's mention carried by
plain message content so it actually notifies, and allowed mentions pinned
to that one user. The ping is part of the prompt, so unlike clarify
mentions it must survive the ``discord.clarify_mentions`` opt-out.

The failure boundary is load-bearing: ``SendResult(success=False)`` only
for failures known before ``channel.send`` (plain fallback safe — nothing
was sent), while a send-time exception propagates so the tool treats
delivery as ambiguous and sends no fallback.
"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
import tools.clarify_gateway as cg
from gateway.config import Platform, PlatformConfig
from gateway.session_context import clear_session_vars, set_session_vars
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.restart_test_helpers import make_restart_runner

_REQUESTER_ID = "111222333444555666"

_PROMPT = (
    "Gateway restart requested — reply with the exact word `restart` "
    "(lowercase, on its own) to bounce the gateway now. Anything else "
    "cancels."
)


def _make_adapter(*, extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    return adapter


def _wire_channel(adapter):
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = 123456
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)
    return channel


def _sent_kwargs(channel):
    assert channel.send.await_count == 1
    return channel.send.call_args.kwargs


@pytest.mark.asyncio
async def test_restart_confirmation_is_a_dedicated_embed_pinging_the_requester(
    monkeypatch,
):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channel = _wire_channel(adapter)

    result = await adapter.send_restart_confirmation(
        chat_id="9001",
        prompt=_PROMPT,
        requester_user_id=_REQUESTER_ID,
        metadata={"thread_id": "999"},
    )

    assert result.success is True
    assert result.message_id == "123456"
    # Routed into the Discord thread, not the parent channel.
    assert adapter._client.get_channel.call_args.args == (999,)

    kwargs = _sent_kwargs(channel)
    # Content is the requester mention and nothing else — the prompt text
    # must appear exactly once, in the embed (a mention inside embed text
    # does not notify, so the ping needs the plain content slot).
    assert kwargs["content"] == f"<@{_REQUESTER_ID}>"
    assert "restart" not in kwargs["content"]
    # A restart-themed embed, not the generic clarify card, in the caution
    # register (orange like the approval/clarify cards), not success green.
    embed = kwargs["embed"]
    assert "restart" in embed.title.lower()
    assert "♻️" in embed.title
    assert "Hermes needs your input" not in embed.title
    assert "exact word `restart`" in embed.description
    assert "anything else cancels" in embed.description.lower()
    from plugins.platforms.discord import adapter as discord_adapter

    assert embed.color == discord_adapter.discord.Color.orange()
    assert embed.color != discord_adapter.discord.Color.green()
    # Allowed mentions restricted to that one user; everything else off.
    # The gateway conftest installs the discord mock, so the constructor
    # call itself is recorded — assert on the exact restriction payload.
    mentions_cls = discord_adapter.discord.AllowedMentions
    assert kwargs["allowed_mentions"] is mentions_cls.return_value
    am_kwargs = mentions_cls.call_args.kwargs
    assert am_kwargs["roles"] is False
    assert am_kwargs["everyone"] is False
    assert am_kwargs["replied_user"] is False
    assert am_kwargs["users"] == [discord_adapter.discord.Object.return_value]
    assert discord_adapter.discord.Object.call_args.kwargs["id"] == int(_REQUESTER_ID)
    # No buttons — the reply is the gateway's text-intercept.
    assert "view" not in kwargs


@pytest.mark.asyncio
async def test_restart_ping_ignores_the_clarify_mentions_opt_out(monkeypatch):
    """`discord.clarify_mentions: false` must not silence the restart ping."""
    monkeypatch.setenv("DISCORD_CLARIFY_MENTIONS", "false")
    adapter = _make_adapter(extra={"clarify_mentions": False})
    channel = _wire_channel(adapter)

    result = await adapter.send_restart_confirmation(
        chat_id="9001",
        prompt=_PROMPT,
        requester_user_id=_REQUESTER_ID,
    )

    assert result.success is True
    kwargs = _sent_kwargs(channel)
    assert kwargs["content"] == f"<@{_REQUESTER_ID}>"
    assert "allowed_mentions" in kwargs


@pytest.mark.asyncio
async def test_restart_confirmation_without_numeric_requester_sends_unpinged(
    monkeypatch,
):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channel = _wire_channel(adapter)

    result = await adapter.send_restart_confirmation(
        chat_id="9001",
        prompt=_PROMPT,
        requester_user_id="alice",
    )

    assert result.success is True
    kwargs = _sent_kwargs(channel)
    # No requester to ping: no mention, so nothing to say outside the
    # embed — content is omitted rather than left empty.
    assert "content" not in kwargs
    assert "allowed_mentions" not in kwargs
    # The prompt itself still lands — the embed is the message.
    assert "restart" in kwargs["embed"].title.lower()
    assert "exact word `restart`" in kwargs["embed"].description


@pytest.mark.asyncio
async def test_restart_confirmation_send_failure_propagates(monkeypatch):
    """A send-time exception escapes the adapter — delivery is ambiguous.

    ``channel.send`` raising does NOT mean the message is absent: Discord
    can create the message and then lose the response (timeout, dropped
    connection). Flattening that into a definitive ``SendResult`` failure
    is what would make the restart tool send the plain fallback next to
    the embed that did land, so the exception must propagate untouched.
    """
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channel = _wire_channel(adapter)
    channel.send = AsyncMock(side_effect=asyncio.TimeoutError("response lost"))

    with pytest.raises(asyncio.TimeoutError):
        await adapter.send_restart_confirmation(
            chat_id="9001",
            prompt=_PROMPT,
            requester_user_id=_REQUESTER_ID,
        )

    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_restart_confirmation_channel_resolution_failure_is_definitive(
    monkeypatch,
):
    """A failure BEFORE the send returns SendResult failure — fallback safe.

    Nothing was sent when channel resolution fails, so reporting a
    definitive failure (and letting the tool fall back to the one plain
    prompt) cannot duplicate anything.
    """
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    adapter._client.get_channel = MagicMock(return_value=None)
    adapter._client.fetch_channel = AsyncMock(
        side_effect=RuntimeError("no such channel")
    )

    result = await adapter.send_restart_confirmation(
        chat_id="9001",
        prompt=_PROMPT,
        requester_user_id=_REQUESTER_ID,
    )

    assert result.success is False
    assert "no such channel" in result.error


@pytest.mark.asyncio
async def test_restart_confirmation_without_client_fails_definitively():
    adapter = _make_adapter()
    adapter._client = None

    result = await adapter.send_restart_confirmation(
        chat_id="9001",
        prompt=_PROMPT,
        requester_user_id=_REQUESTER_ID,
    )

    assert result.success is False


# ── lost response: one visible message, no plain fallback ───────────────────
#
# The composition regression: Discord created the message and then the
# response was lost, so channel.send raises AFTER the message exists. The
# adapter lets that exception reach the restart tool, which treats delivery
# as ambiguous — disarm the registration, cancel, and send NO plain fallback
# — instead of receiving a definitive SendResult failure and duplicating the
# prompt as both embed and plain text.


class _GatewayLoop:
    """A live event loop on a background thread, standing in for the gateway.

    Tool handlers run in a worker thread and hop sends onto the gateway
    event loop, so the test exercises the real threadsafe hop.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest.fixture
def gateway_loop():
    holder = _GatewayLoop()
    yield holder.loop
    holder.close()


_SESSION_REQUESTER_ID = "123456789012345678"

_DISCORD_SESSION = {
    "platform": "discord",
    "chat_id": "55",
    "chat_type": "thread",
    "thread_id": "999",
    "user_id": _SESSION_REQUESTER_ID,
    "session_key": "discord-55",
}


def test_lost_response_leaves_one_message_and_no_plain_fallback(
    gateway_loop, monkeypatch
):
    """Remote created the embed, the response was lost → no duplicate prompt.

    The channel records every message Discord actually created. The send
    then raises (lost response), which must surface as an exception from
    ``send_restart_confirmation`` — so the tool disarms the registration,
    cancels the restart, and never sends the plain fallback. Exactly one
    message is visible; a fallback would be a second.
    """
    from plugins.gateway_restart.tool import handle_restart

    adapter = _make_adapter()
    channel = _wire_channel(adapter)
    visible: list[dict] = []

    async def _send_creates_then_loses_response(**kwargs):
        visible.append(dict(kwargs))  # the message exists on Discord's side
        raise asyncio.TimeoutError("response lost after message creation")

    channel.send = _send_creates_then_loses_response

    runner, _telegram_adapter = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._gateway_loop = gateway_loop
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    set_session_vars(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "confirmation prompt" in result["error"]
    # Exactly one message ever hit the channel — the confirmation embed
    # Discord created before the response was lost. The plain fallback
    # would have been a second visible message.
    assert len(visible) == 1
    assert "embed" in visible[0]
    assert visible[0]["content"] == f"<@{_SESSION_REQUESTER_ID}>"
    runner.request_restart.assert_not_called()
    # The armed registration was disarmed, not left to eat the next message.
    assert cg.has_pending("discord-55") is False


def test_fast_reply_racing_the_lost_response_send_still_reaps_the_entry(
    gateway_loop, monkeypatch
):
    """The requester answered before the ambiguous send failure surfaced.

    The fast reply resolves the registration while the rich send is still
    in flight — Discord created the embed, the requester typed ``restart``,
    and only then did ``channel.send`` raise its lost-response exception.
    The delivery error makes the tool cancel without ever waiting on the
    entry, so the disarm path is the only chance to reap it — and the
    racing reply means ``resolve_gateway_clarify`` reports "already
    resolved" there. A reap skipped on that answer leaves the entry
    registered at the head of the session's clarify index, where it eats
    the replies meant for the session's next clarify.
    """
    from plugins.gateway_restart.tool import handle_restart

    adapter = _make_adapter()
    channel = _wire_channel(adapter)
    visible: list[dict] = []

    async def _send_reply_races_then_loses_response(**kwargs):
        visible.append(dict(kwargs))  # the message exists on Discord's side
        # The requester's fast reply lands while the send is still pending:
        # the gateway text-intercept resolves the registration for real,
        # from the gateway loop thread exactly as in production.
        assert (
            cg.attempt_text_response_for_session("discord-55", "restart")
            == cg.TEXT_RESOLVED
        )
        raise asyncio.TimeoutError("response lost after message creation")

    channel.send = _send_reply_races_then_loses_response

    runner, _telegram_adapter = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._gateway_loop = gateway_loop
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    set_session_vars(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))

        # Ambiguous delivery cancels even though the requester typed the
        # word — the restart must not fire on a send we cannot vouch for.
        assert result["success"] is False
        assert result["status"] == "cancelled"
        assert "confirmation prompt" in result["error"]
        # Exactly one visible message (the embed); the plain fallback would
        # have been a second.
        assert len(visible) == 1
        assert "embed" in visible[0]
        assert visible[0]["content"] == f"<@{_SESSION_REQUESTER_ID}>"
        # The entry was reaped despite the racing reply having beat the
        # disarm to the resolution — nothing stale stays registered.
        assert cg.has_pending("discord-55") is False
        # …so the session's NEXT clarify is reachable: its reply resolves
        # it instead of being swallowed by a stale index entry.
        cg.register(
            clarify_id="after-the-race",
            session_key="discord-55",
            question="Proceed?",
            choices=None,
        )
        assert (
            cg.attempt_text_response_for_session("discord-55", "yes please")
            == cg.TEXT_RESOLVED
        )
        assert cg.wait_for_response("after-the-race", 1.0) == "yes please"
    finally:
        clear_session_vars(None)
        cg.clear_session("discord-55")
    runner.request_restart.assert_not_called()
