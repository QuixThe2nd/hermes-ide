"""Discord's opt-in restart wind-down offer and shared queue integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

_REQUESTER_ID = "111222333444555666"


# ── restart wind-down ⏸️ opt-in offer ──────────────────────────────────────
#
# The drain waits naturally, and this one embed is the only thing that can
# turn that wait into a cooperative
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
async def test_a_send_that_lands_after_the_reaction_wait_timed_out_never_registers(
    tmp_path, monkeypatch
):
    """The bounded wait ends the send attempt, it does not pause judgement.

    A valid ⏸️ waits out the in-flight offer send, the wait times out, and the
    reaction is answered ``no_offer`` — which retires the prompt on the
    adapter. If the embed then finished delivering, registering it would
    resurrect an actionable offer behind a message the requester has already
    been told is spent, and a second ⏸️ on it would steer a wind-down the
    first one never ran.
    """
    monkeypatch.setattr("gateway.restart_wind_down.get_hermes_home", lambda: tmp_path)
    from gateway.restart_wind_down import COOPERATIVE_RESTART_STEER

    # Bound the wait at a hair above zero: the seeded reaction below stays
    # blocked for the whole test, so the timeout is certain to fire and the
    # send is certain to complete only once the test releases it.
    monkeypatch.setattr(gateway_run, "RESTART_WIND_DOWN_SEND_WAIT_SECONDS", 0.01)

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
    adapter.gateway_runner = runner

    _slow_seed, seed_in_flight, release_seed = _blocking_seed()
    sent.add_reaction = _slow_seed

    send_task = asyncio.create_task(runner._send_restart_wind_down_prompt(source))
    await seed_in_flight.wait()
    # The restart is requested while the embed is still delivering — the
    # same window the user-restart paths leave open between their offer send
    # and the request_restart() call behind it (or any concurrent restart
    # trigger). Set before the send the eligibility gate would refuse the
    # offer outright and there would be no race to run.
    runner._restart_requested = True
    reaction_task = asyncio.create_task(adapter._dispatch_raw_reaction(_reaction()))
    # Awaiting the reaction waits out the whole bounded send wait, times out,
    # and retires the prompt — all while the send itself is still parked in
    # its seeded reaction.
    await reaction_task
    assert runner._restart_wind_down_accepted is False

    release_seed.set()

    assert await send_task is False
    # No resurrection: neither side is left holding an actionable prompt for
    # the message that did eventually land.
    assert runner._restart_wind_down_offer is None
    assert adapter._restart_wind_down_offers == {}
    other.steer.assert_not_called()
    # The rejection already made the terminal edit; the late send must not
    # add a second one on top of it.
    assert partial.edit.await_count == 1
    assert partial.clear_reactions.await_count == 1

    # And the late-landing prompt really is spent: a second ⏸️ on it has no
    # adapter registry entry to ride, so it never reaches the gateway.
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "accepted_count": 1}
    )
    await adapter._dispatch_raw_reaction(_reaction())
    runner.accept_restart_wind_down_opt_in.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_the_offer_send_during_the_seeded_reaction_leaves_no_state(
    tmp_path, monkeypatch
):
    """Cancellation must not leave an authorization the caller never received.

    The registry entry is inserted before ``add_reaction`` is awaited, so a
    cancellation landing inside that round trip propagates straight past the
    best-effort ``except Exception``. Left in place, it would authorize a ⏸️
    on a message whose offer send never returned to the gateway.
    """
    monkeypatch.setattr("gateway.restart_wind_down.get_hermes_home", lambda: tmp_path)

    adapter, _channel, sent, _partial = _wind_down_adapter()
    runner, _platform = make_restart_runner(adapter=adapter, platform=Platform.DISCORD)
    source = make_restart_source(
        chat_id=str(_OFFER_CHANNEL),
        chat_type="thread",
        thread_id=str(_OFFER_CHANNEL),
        platform=Platform.DISCORD,
        user_id=_REQUESTER_ID,
    )
    runner._restart_command_source = source
    runner._running_agents["agent:main:discord:thread:other"] = MagicMock()
    adapter.gateway_runner = runner

    _slow_seed, seed_in_flight, _release_seed = _blocking_seed()
    sent.add_reaction = _slow_seed

    send_task = asyncio.create_task(runner._send_restart_wind_down_prompt(source))
    await seed_in_flight.wait()
    # The restart request lands while the embed is still delivering, as it
    # can behind any concurrent restart trigger; before the send it would
    # simply refuse the offer and there would be nothing to cancel.
    runner._restart_requested = True
    # The window is real: the entry is live while the seed is still in flight.
    assert str(_OFFER_MSG_ID) in adapter._restart_wind_down_offers

    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task

    # Cancellation leaves no offer state on either side, and nothing re-arms
    # it once the cancellation has propagated.
    assert adapter._restart_wind_down_offers == {}
    assert runner._restart_wind_down_offer is None
    assert runner._restart_wind_down_accepted is False
    assert runner._restart_wind_down_send_in_flight is None

    # So a ⏸️ on the half-offered message has nothing to authorize against
    # and never reaches the gateway.
    runner.accept_restart_wind_down_opt_in = AsyncMock(
        return_value={"accepted": True, "accepted_count": 1}
    )
    await adapter._dispatch_raw_reaction(_reaction())
    runner.accept_restart_wind_down_opt_in.assert_not_awaited()


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


# ── the user-restart paths offer exactly one prompt ────────────────────────


@pytest.mark.asyncio
async def test_slash_restart_offers_the_pause_embed_for_native_discord(
    tmp_path, monkeypatch
):
    """/restart offers the ⏸️ embed too, not just the restart tool path."""
    from gateway.platforms.base import MessageEvent, MessageType

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda: False)
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: False)
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
    runner.request_restart = MagicMock(return_value=True)

    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-0",
        platform_update_id=4242,
    )
    result = await runner._handle_restart_command(event)

    # A peer chat is live, so the reply is the draining notice.
    assert "1" in result
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
    # request_restart re-entered the already-open cycle rather than orphaning
    # the offer behind a fresh generation.
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)
    assert runner._restart_generation == 1


@pytest.mark.asyncio
async def test_slash_restart_skips_the_offer_with_no_other_live_chat(
    tmp_path, monkeypatch
):
    from gateway.platforms.base import MessageEvent, MessageType

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda: False)
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: False)
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

    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-0",
    )
    await runner._handle_restart_command(event)

    adapter.send_restart_wind_down_offer.assert_not_awaited()
    assert runner._restart_wind_down_offer is None
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_tool_queue_skips_the_offer_for_relay_discord(tmp_path, monkeypatch):
    """Relay-fronted Discord is not a surface the ⏸️ prompt may land on."""
    from plugins.gateway_restart.tool import _queue_user_restart

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

    status = await _queue_user_restart(runner, source, "m-0")

    assert status["status"] == "restarting"
    adapter.send_restart_wind_down_offer.assert_not_awaited()
    assert runner._restart_wind_down_offer is None
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


@pytest.mark.asyncio
async def test_both_user_restart_entry_points_share_the_gateway_queue_helper(
    tmp_path, monkeypatch
):
    """/restart and the restart tool funnel into ONE gateway-owned sequence.

    The requester-facing queue choreography (comeback routing + notify
    marker, wind-down offer, drain hand-off) must have a single
    implementation the slash handler and the tool both call — a private copy
    in either place is exactly the drift that once crashed the tool when the
    runner API moved under it.
    """
    from gateway.platforms.base import MessageEvent, MessageType

    import gateway.restart as gateway_restart
    from plugins.gateway_restart import tool as restart_tool

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    shared_calls: list[str] = []
    real_queue = gateway_restart.queue_user_restart

    async def _spy_queue(runner, source, message_id):
        shared_calls.append(str(getattr(source, "chat_id", None) or ""))
        return await real_queue(runner, source, message_id)

    monkeypatch.setattr(gateway_restart, "queue_user_restart", _spy_queue)

    runner, _telegram = make_restart_runner()
    runner.adapters = {Platform.TELEGRAM: MagicMock()}
    runner.request_restart = MagicMock(return_value=True)

    # The slash entry point queues through the shared helper.
    slash_event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="7"),
        message_id="s-1",
    )
    await runner._handle_restart_command(slash_event)

    # The tool's queued restart delegates to the same helper — no private
    # choreography of its own.
    status = await restart_tool._queue_user_restart(
        runner, make_restart_source(chat_id="8"), "t-1"
    )

    assert status["status"] == "restarting"
    assert shared_calls == ["7", "8"]
    assert runner.request_restart.call_count == 2


