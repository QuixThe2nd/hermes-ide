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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
import tools.clarify_gateway as cg
from gateway.config import Platform, PlatformConfig
from gateway.session_context import clear_session_vars, set_session_vars
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

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


# ── restart wind-down ⏸️ opt-in offer ──────────────────────────────────────
#
# The restart confirm gate above asks "should we restart at all". The wind-down
# offer below is what happens next: the drain is already waiting naturally, and
# this one embed is the only thing that can turn that wait into a cooperative
# park steer. Its authorization is entirely server-side — the adapter filters
# raw reactions down to the exact message it offered for the exact requester,
# and the gateway re-checks every field against state the adapter cannot
# influence. Footer text routes nothing on its own.

_OFFER_MSG_ID = 777
_OFFER_CHANNEL = 9001


def _wind_down_adapter():
    """A DiscordAdapter whose fake client has one channel and one bot user."""
    config = PlatformConfig(enabled=True, token="test-token")
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    adapter._client.user = MagicMock(id=42)

    channel = MagicMock()
    sent = MagicMock()
    sent.id = _OFFER_MSG_ID
    sent.add_reaction = AsyncMock(return_value=None)
    channel.send = AsyncMock(return_value=sent)

    partial = MagicMock()
    partial.edit = AsyncMock(return_value=None)
    partial.clear_reactions = AsyncMock(return_value=None)
    partial.remove_reaction = AsyncMock(return_value=None)
    channel.get_partial_message = MagicMock(return_value=partial)

    adapter._client.get_channel = MagicMock(return_value=channel)
    adapter._client.fetch_channel = AsyncMock(return_value=channel)
    return adapter, channel, sent, partial


def _reaction(
    *,
    message_id=_OFFER_MSG_ID,
    channel_id=_OFFER_CHANNEL,
    user_id=_REQUESTER_ID,
    emoji="⏸️",
):
    payload = MagicMock()
    payload.message_id = message_id
    payload.channel_id = channel_id
    payload.user_id = user_id
    payload.emoji = MagicMock()
    payload.emoji.name = emoji
    return payload


async def _offer(adapter, *, generation=3, nonce="nonce-abc"):
    return await adapter.send_restart_wind_down_offer(
        channel_id=str(_OFFER_CHANNEL),
        requester_user_id=_REQUESTER_ID,
        generation=generation,
        nonce=nonce,
    )


@pytest.mark.asyncio
async def test_wind_down_offer_is_one_embed_in_the_requesters_thread():
    adapter, channel, sent, _partial = _wind_down_adapter()

    message_id = await _offer(adapter)

    assert message_id == str(_OFFER_MSG_ID)
    # Routed into the requester's own channel/thread — chat_id is the thread
    # id for Discord threads, so no parent-channel fallback.
    assert adapter._client.get_channel.call_args.args == (_OFFER_CHANNEL,)
    assert channel.send.await_count == 1
    embed = channel.send.call_args.kwargs["embed"]
    assert embed.title == "⏳ Waiting for active sessions"
    assert embed.description == (
        "The gateway will restart when active sessions finish. "
        "React with ⏸️ to ask them to pause safely now."
    )
    assert "restart wind-down" in embed.footer["text"]
    assert "ticket" not in embed.footer["text"]
    # The agent-facing steer text never surfaces in the user prompt.
    from gateway.restart_wind_down import COOPERATIVE_RESTART_STEER

    assert COOPERATIVE_RESTART_STEER not in embed.description
    sent.add_reaction.assert_awaited_once_with("⏸️")
    assert adapter._restart_wind_down_offers == {
        str(_OFFER_MSG_ID): {
            "channel_id": str(_OFFER_CHANNEL),
            "requester_user_id": _REQUESTER_ID,
            "generation": 3,
            "nonce": "nonce-abc",
        }
    }


@pytest.mark.asyncio
async def test_wind_down_offer_send_failure_registers_nothing():
    adapter, channel, _sent, _partial = _wind_down_adapter()
    channel.send = AsyncMock(side_effect=RuntimeError("nope"))

    assert await _offer(adapter) is None
    assert adapter._restart_wind_down_offers == {}


@pytest.mark.asyncio
async def test_wind_down_offer_without_a_client_is_a_no_op():
    adapter, _channel, _sent, _partial = _wind_down_adapter()
    adapter._client = None

    assert await _offer(adapter) is None
    assert adapter._restart_wind_down_offers == {}


@pytest.mark.asyncio
async def test_wind_down_reaction_seed_failure_still_offers():
    adapter, _channel, sent, _partial = _wind_down_adapter()
    sent.add_reaction = AsyncMock(side_effect=RuntimeError("Missing Permissions"))

    assert await _offer(adapter) == str(_OFFER_MSG_ID)
    assert str(_OFFER_MSG_ID) in adapter._restart_wind_down_offers


def _blocking_seed():
    """An add_reaction that parks until the test releases it.

    Forcing the window between the message id becoming known and the offer
    send returning — exactly when a fast requester ⏸️ can arrive.
    """
    seed_in_flight = asyncio.Event()
    release_seed = asyncio.Event()

    async def _slow_seed(emoji):
        assert emoji == "⏸️"
        seed_in_flight.set()
        await release_seed.wait()

    return _slow_seed, seed_in_flight, release_seed


@pytest.mark.asyncio
async def test_wind_down_offer_registers_before_the_seeded_reaction_lands():
    """The registry entry exists the moment the message id is known.

    Registering only after the seeded reaction's round trip dropped a ⏸️
    that arrived during it, then left the freshly registered prompt behind
    as stale actionable state.
    """
    adapter, _channel, sent, _partial = _wind_down_adapter()
    _slow_seed, seed_in_flight, release_seed = _blocking_seed()
    sent.add_reaction = _slow_seed

    offer_task = asyncio.create_task(_offer(adapter, generation=3, nonce="nonce-abc"))
    await seed_in_flight.wait()

    # Actionable while the seed is still in flight, not after it.
    assert adapter._restart_wind_down_offers == {
        str(_OFFER_MSG_ID): {
            "channel_id": str(_OFFER_CHANNEL),
            "requester_user_id": _REQUESTER_ID,
            "generation": 3,
            "nonce": "nonce-abc",
        }
    }

    release_seed.set()
    assert await offer_task == str(_OFFER_MSG_ID)
    # The seed await must not re-register — and so re-arm — the prompt.
    assert list(adapter._restart_wind_down_offers) == [str(_OFFER_MSG_ID)]


@pytest.mark.asyncio
async def test_requester_reaction_during_offer_send_is_accepted_once(
    tmp_path, monkeypatch
):
    """A valid ⏸️ in the send window is honored, not lost and resurrected.

    Real adapter and real runner wiring: the seeded reaction blocks, so the
    requester's reaction reaches the gateway while the offer send has not
    yet returned and the runner has not yet registered the offer.
    """
    # Patch only this module — patching hermes_constants.get_hermes_home
    # poisons later imports of restart_loop_guard in the same process.
    monkeypatch.setattr("gateway.restart_wind_down.get_hermes_home", lambda: tmp_path)
    from gateway.restart_wind_down import (
        COOPERATIVE_RESTART_REASON,
        COOPERATIVE_RESTART_STEER,
        load_resume_allowlist,
    )

    adapter, _channel, sent, partial = _wind_down_adapter()
    runner, _platform = make_restart_runner(adapter=adapter, platform=Platform.DISCORD)
    source = make_restart_source(
        chat_id=str(_OFFER_CHANNEL),
        chat_type="thread",
        thread_id=str(_OFFER_CHANNEL),
        platform=Platform.DISCORD,
        user_id=_REQUESTER_ID,
    )
    runner._restart_command_source = source
    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:discord:thread:other"] = other
    runner.session_store.mark_resume_pending.return_value = True
    adapter.gateway_runner = runner

    _slow_seed, seed_in_flight, release_seed = _blocking_seed()
    sent.add_reaction = _slow_seed

    send_task = asyncio.create_task(runner._send_restart_wind_down_prompt(source))
    await seed_in_flight.wait()
    # The restart is requested — and the requester reacts — while the embed's
    # own seeded reaction is still in flight.
    runner._restart_requested = True
    reaction_task = asyncio.create_task(adapter._dispatch_raw_reaction(_reaction()))
    await asyncio.sleep(0)
    release_seed.set()

    assert await send_task is True
    await reaction_task

    # Accepted exactly once: one steer, one mark, one receipt.
    other.steer.assert_called_once_with(COOPERATIVE_RESTART_STEER)
    runner.session_store.mark_resume_pending.assert_called_once_with(
        "agent:main:discord:thread:other", COOPERATIVE_RESTART_REASON
    )
    assert load_resume_allowlist() == {"agent:main:discord:thread:other"}
    assert runner._restart_wind_down_accepted is True
    # No stale state on either side: neither an actionable adapter entry nor
    # an authorized runner offer survives the spent prompt.
    assert adapter._restart_wind_down_offers == {}
    assert runner._restart_wind_down_offer is None
    # The prompt got exactly one terminal edit and one reaction cleanup.
    assert partial.edit.await_count == 1
    assert partial.clear_reactions.await_count == 1


@pytest.mark.asyncio
async def test_valid_requester_reaction_routes_to_the_gateway_once():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter, generation=3, nonce="nonce-abc")
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "accepted_count": 2, "steered": ["a", "b"]}
    )
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction())

    runner.accept_restart_wind_down_opt_in.assert_awaited_once_with(
        message_id=str(_OFFER_MSG_ID),
        channel_id=str(_OFFER_CHANNEL),
        requester_user_id=_REQUESTER_ID,
        emoji="⏸️",
        generation=3,
        nonce="nonce-abc",
    )
    embed = partial.edit.call_args.kwargs["embed"]
    assert embed.title == "⏸️ Pausing 2 active sessions"
    assert "2 active sessions accepted" in embed.description
    partial.clear_reactions.assert_awaited_once()
    # Spent: a re-add on the same message can never re-run the wind-down.
    assert adapter._restart_wind_down_offers == {}


@pytest.mark.asyncio
async def test_bare_pause_glyph_reaction_is_the_same_emoji():
    adapter, _channel, _sent, _partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "accepted_count": 1, "steered": ["a"]}
    )
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction(emoji="⏸"))

    assert runner.accept_restart_wind_down_opt_in.await_count == 1
    assert runner.accept_restart_wind_down_opt_in.await_args.kwargs["emoji"] == "⏸"


@pytest.mark.asyncio
async def test_reaction_with_no_targets_uses_the_nothing_left_copy():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "no_targets": True, "accepted_count": 0}
    )
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction())

    embed = partial.edit.call_args.kwargs["embed"]
    assert embed.title == "⏸️ Nothing left to pause"
    assert "already finished" in embed.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_id": "999000999000999000"},  # someone else
        {"channel_id": 1111},  # right message, wrong channel
        {"message_id": 555},  # unknown / wrong message id
        {"emoji": "🔥"},  # wrong emoji
        {"emoji": "<:pause:123>"},  # custom emoji that merely looks like it
    ],
)
async def test_invalid_reaction_events_never_reach_the_gateway(kwargs):
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock()
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction(**kwargs))

    runner.accept_restart_wind_down_opt_in.assert_not_awaited()
    partial.edit.assert_not_awaited()
    # A rejected event does not retire the offer — the requester can still
    # click the real one.
    assert str(_OFFER_MSG_ID) in adapter._restart_wind_down_offers


@pytest.mark.asyncio
async def test_bot_self_reaction_is_ignored():
    adapter, _channel, _sent, _partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock()
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction(user_id=42))

    runner.accept_restart_wind_down_opt_in.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_rejection_retires_the_prompt():
    """The runner is the authorization authority — its "no" closes the embed.

    A stale-generation or already-finalized offer must not keep looking
    actionable with a seeded ⏸️ still on it.
    """
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": False, "reason": "stale_generation"}
    )
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction())

    runner.accept_restart_wind_down_opt_in.assert_awaited_once()
    embed = partial.edit.call_args.kwargs["embed"]
    assert embed.title == "⏸️ Restart wind-down closed"
    assert "no longer active" in embed.description
    partial.clear_reactions.assert_awaited_once()
    assert adapter._restart_wind_down_offers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "already_accepted",
        "no_offer",
        "stale_nonce",
        "wrong_message",
        "not_restarting",
    ],
)
async def test_every_gateway_rejection_kind_retires_the_prompt(reason):
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": False, "reason": reason}
    )
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction())

    partial.edit.assert_awaited_once()
    assert adapter._restart_wind_down_offers == {}


@pytest.mark.asyncio
async def test_reaction_where_no_session_accepts_the_steer_tells_the_truth():
    """Zero accepted steers still names the count honestly.

    "0 active sessions accepted … and will continue automatically" would be
    vacuous copy — nobody was paused and nobody will auto-continue.
    """
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "accepted_count": 0, "steered": []}
    )
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction())

    embed = partial.edit.call_args.kwargs["embed"]
    assert embed.title == "⏸️ Pause requested"
    assert "No active sessions accepted" in embed.description
    assert "will continue automatically" not in embed.description
    assert "0 active sessions" not in embed.description
    partial.clear_reactions.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaction_without_a_gateway_runner_is_swallowed():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    adapter.gateway_runner = None

    await adapter._dispatch_raw_reaction(_reaction())

    # Nothing can ever authorize this offer again, so it must not look live.
    assert adapter._restart_wind_down_offers == {}
    embed = partial.edit.call_args.kwargs["embed"]
    assert embed.title == "⏸️ Restart wind-down closed"
    partial.clear_reactions.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_valid_reactions_reach_the_gateway_once():
    adapter, _channel, _sent, _partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "accepted_count": 1, "steered": ["a"]}
    )
    adapter.gateway_runner = runner

    # Both events are dispatched before either handler gets an await in.
    await asyncio.gather(
        adapter._dispatch_raw_reaction(_reaction()),
        adapter._dispatch_raw_reaction(_reaction()),
    )

    assert runner.accept_restart_wind_down_opt_in.await_count == 1


@pytest.mark.asyncio
async def test_finalize_edits_the_embed_and_clears_the_offered_reaction():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    from gateway.restart_wind_down import restart_wind_down_terminal_spec

    spec = restart_wind_down_terminal_spec("drained")

    assert (
        await adapter.finalize_restart_wind_down_offer(
            message_id=str(_OFFER_MSG_ID),
            channel_id=str(_OFFER_CHANNEL),
            spec=spec,
        )
        is True
    )

    embed = partial.edit.call_args.kwargs["embed"]
    assert embed.title == "✅ Active sessions finished"
    assert "restart is proceeding" in embed.description
    partial.clear_reactions.assert_awaited_once()
    assert adapter._restart_wind_down_offers == {}


@pytest.mark.asyncio
async def test_finalize_falls_back_to_removing_the_seeded_reaction():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    partial.clear_reactions = AsyncMock(side_effect=RuntimeError("403"))

    from gateway.restart_wind_down import restart_wind_down_terminal_spec

    await adapter.finalize_restart_wind_down_offer(
        message_id=str(_OFFER_MSG_ID),
        channel_id=str(_OFFER_CHANNEL),
        spec=restart_wind_down_terminal_spec("closed"),
    )

    partial.remove_reaction.assert_awaited_once_with("⏸️", adapter._client.user)


@pytest.mark.asyncio
async def test_finalize_is_best_effort_and_reports_failure():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    partial.edit = AsyncMock(side_effect=RuntimeError("channel gone"))

    from gateway.restart_wind_down import restart_wind_down_terminal_spec

    assert (
        await adapter.finalize_restart_wind_down_offer(
            message_id=str(_OFFER_MSG_ID),
            channel_id=str(_OFFER_CHANNEL),
            spec=restart_wind_down_terminal_spec("closed"),
        )
        is False
    )
    # Local state went first, so nothing is actionable however Discord behaves.
    assert adapter._restart_wind_down_offers == {}


# ── resolve-ticket isolation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_ticket_reactions_still_route_and_never_see_wind_down():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    handled = []

    def _fake_resolve(channel_id, message_id, emoji, token):
        handled.append((channel_id, message_id, emoji, token))
        return {"acted": True, "decision": "closed"}

    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock()
    adapter.gateway_runner = runner

    with patch(
        "tools.discord_resolve_tool.handle_resolve_reaction",
        side_effect=_fake_resolve,
    ) as resolve_mock:
        await adapter._dispatch_raw_reaction(
            _reaction(message_id=555, emoji="✅")
        )

    resolve_mock.assert_called_once()
    assert handled == [("9001", "555", "✅", "test-token")]
    # The wind-down offer was untouched by a ✅ elsewhere.
    assert str(_OFFER_MSG_ID) in adapter._restart_wind_down_offers
    runner.accept_restart_wind_down_opt_in.assert_not_awaited()
    partial.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_ticket_failure_does_not_leak_into_wind_down():
    adapter, _channel, _sent, _partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock()
    adapter.gateway_runner = runner

    with patch(
        "tools.discord_resolve_tool.handle_resolve_reaction",
        side_effect=RuntimeError("resolve blew up"),
    ):
        await adapter._dispatch_raw_reaction(_reaction(message_id=555, emoji="✅"))

    # The wind-down branch never ran for the ✅, and the exception above did
    # not stop the adapter.
    runner.accept_restart_wind_down_opt_in.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_emoji_costs_one_check_and_touches_nothing():
    adapter, _channel, _sent, partial = _wind_down_adapter()
    await _offer(adapter)
    runner = MagicMock()
    runner.accept_restart_wind_down_opt_in = AsyncMock()
    adapter.gateway_runner = runner

    await adapter._dispatch_raw_reaction(_reaction(message_id=555, emoji="🔥"))

    runner.accept_restart_wind_down_opt_in.assert_not_awaited()
    partial.edit.assert_not_awaited()


# ── begin_user_restart offers exactly one prompt ──────────────────────────


@pytest.mark.asyncio
async def test_begin_user_restart_offers_the_pause_embed_for_native_discord(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    adapter = MagicMock()
    adapter.send_restart_wind_down_offer = AsyncMock(return_value="m-1")
    adapter.finalize_restart_wind_down_offer = AsyncMock(return_value=True)
    runner, _telegram = make_restart_runner(adapter=MagicMock())
    runner.adapters = {Platform.DISCORD: adapter, Platform.TELEGRAM: MagicMock()}

    source = make_restart_source(
        chat_id="9001",
        chat_type="thread",
        thread_id="9001",
        platform=Platform.DISCORD,
        user_id=_REQUESTER_ID,
    )
    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:discord:thread:other"] = other
    runner._restart_command_source = source
    runner.request_restart = MagicMock(return_value=True)

    status = await runner.begin_user_restart(source=source, message_id="m-0")

    assert status["status"] == "restarting"
    adapter.send_restart_wind_down_offer.assert_awaited_once()
    kwargs = adapter.send_restart_wind_down_offer.await_args.kwargs
    assert kwargs["channel_id"] == "9001"
    assert kwargs["requester_user_id"] == _REQUESTER_ID
    assert kwargs["spec"]["title"] == "⏳ Waiting for active sessions"
    assert runner._restart_wind_down_offer == {
        "generation": 1,
        "nonce": kwargs["nonce"],
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": _REQUESTER_ID,
    }
    # request_restart re-entered the same cycle rather than orphaning the
    # offer behind a fresh generation.
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)
    assert runner._restart_generation == 1


@pytest.mark.asyncio
async def test_begin_user_restart_skips_the_offer_with_no_other_live_chat(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    adapter = MagicMock()
    adapter.send_restart_wind_down_offer = AsyncMock(return_value="m-1")
    runner, _telegram = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter, Platform.TELEGRAM: MagicMock()}

    source = make_restart_source(
        chat_id="9001",
        chat_type="thread",
        thread_id="9001",
        platform=Platform.DISCORD,
        user_id=_REQUESTER_ID,
    )
    # Only the requester's own turn is live.
    runner._running_agents[runner._session_key_for_source(source)] = MagicMock()
    runner.request_restart = MagicMock(return_value=True)

    await runner.begin_user_restart(source=source, message_id="m-0")

    adapter.send_restart_wind_down_offer.assert_not_awaited()
    assert runner._restart_wind_down_offer is None
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_begin_user_restart_skips_the_offer_for_relay_discord(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    adapter = MagicMock()
    adapter.send_restart_wind_down_offer = AsyncMock(return_value="m-1")
    runner, _telegram = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter, Platform.TELEGRAM: MagicMock()}

    source = make_restart_source(
        chat_id="9001",
        chat_type="thread",
        thread_id="9001",
        platform=Platform.DISCORD,
        user_id=_REQUESTER_ID,
        delivered_via_upstream_relay=True,
    )
    runner._running_agents["agent:main:discord:thread:other"] = MagicMock()
    runner.request_restart = MagicMock(return_value=True)

    await runner.begin_user_restart(source=source, message_id="m-0")

    adapter.send_restart_wind_down_offer.assert_not_awaited()
