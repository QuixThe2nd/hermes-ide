"""Cooperative restart: steer live sessions to park, then auto-continue.

Since the opt-in change nothing here runs merely because a restart began —
``request_restart()`` drains and waits naturally. The park steer fires only
when the requester explicitly opts in, and the snapshot is taken at that
moment, not at restart-request time.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import asyncio
import pytest

from gateway.config import Platform
from gateway.restart_wind_down import (
    COOPERATIVE_RESTART_REASON,
    COOPERATIVE_RESTART_STEER,
    WIND_DOWN_TERMINAL_DRAINED,
    WIND_DOWN_TERMINAL_OPTED_IN,
    clear_resume_allowlist,
    is_cooperative_restart_reason,
    load_resume_allowlist,
    mark_cooperative_restart_sessions,
    normalize_pause_emoji,
    restart_wind_down_prompt_spec,
    restart_wind_down_terminal_spec,
    should_preserve_cooperative_restart_marker,
    steer_running_agents_for_restart,
    write_resume_allowlist,
)
from gateway.run import GatewayRunner, build_resume_recovery_note
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def test_cooperative_reason_is_recognized():
    assert is_cooperative_restart_reason(COOPERATIVE_RESTART_REASON) is True
    assert is_cooperative_restart_reason("restart_timeout") is False
    assert is_cooperative_restart_reason(None) is False


def test_preserve_marker_only_during_drain_for_coop_reason():
    assert (
        should_preserve_cooperative_restart_marker(
            draining=True, resume_reason=COOPERATIVE_RESTART_REASON
        )
        is True
    )
    assert (
        should_preserve_cooperative_restart_marker(
            draining=False, resume_reason=COOPERATIVE_RESTART_REASON
        )
        is False
    )
    assert (
        should_preserve_cooperative_restart_marker(
            draining=True, resume_reason="restart_timeout"
        )
        is False
    )


def test_steer_skips_requester_and_is_idempotent():
    runner, _adapter = make_restart_runner()
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester
    requester_key = runner._session_key_for_source(requester)

    other = MagicMock()
    other.steer.return_value = True
    requester_agent = MagicMock()
    requester_agent.steer.return_value = True
    runner._running_agents[requester_key] = requester_agent
    runner._running_agents["agent:main:telegram:dm:other"] = other

    first = steer_running_agents_for_restart(runner)
    assert first == ["agent:main:telegram:dm:other"]
    other.steer.assert_called_once_with(COOPERATIVE_RESTART_STEER)
    requester_agent.steer.assert_not_called()

    runner._cooperative_restart_sessions = first
    second = steer_running_agents_for_restart(runner)
    assert second == []
    assert other.steer.call_count == 1


def test_steer_skips_agents_without_steer():
    runner, _adapter = make_restart_runner()
    runner._running_agents["agent:main:telegram:dm:1"] = object()
    assert steer_running_agents_for_restart(runner) == []


def test_mark_resume_pending_uses_cooperative_reason():
    store = MagicMock()
    store.mark_resume_pending.return_value = True
    runner = MagicMock()
    runner.session_store = store
    marked = mark_cooperative_restart_sessions(
        runner, ["agent:main:telegram:dm:other"]
    )
    assert marked == 1
    store.mark_resume_pending.assert_called_once_with(
        "agent:main:telegram:dm:other", COOPERATIVE_RESTART_REASON
    )


def _patch_resume_home(monkeypatch, tmp_path):
    # Patch only this module. Patching hermes_constants.get_hermes_home
    # poisons later imports of restart_loop_guard in the same process.
    monkeypatch.setattr("gateway.restart_wind_down.get_hermes_home", lambda: tmp_path)


@pytest.mark.asyncio
async def test_request_restart_does_not_auto_steer_mark_or_write_allowlist(
    tmp_path, monkeypatch
):
    """A plain restart waits naturally — no steer, no mark, no receipt."""
    _patch_resume_home(monkeypatch, tmp_path)
    runner, _adapter = make_restart_runner()
    runner.stop = MagicMock()
    runner._launch_detached_restart_command = MagicMock()
    runner._restart_after_turn_timeout = 0.0
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester
    requester_key = runner._session_key_for_source(requester)

    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:telegram:dm:other"] = other
    runner._running_agents[requester_key] = MagicMock()
    runner.session_store.mark_resume_pending.return_value = True

    assert runner.request_restart(detached=False, via_service=True) is True
    other.steer.assert_not_called()
    runner.session_store.mark_resume_pending.assert_not_called()
    assert getattr(runner, "_cooperative_restart_sessions", None) is None
    assert getattr(runner, "_cooperative_restart_steered_sessions", None) is None
    assert runner._restart_wind_down_accepted is False
    assert load_resume_allowlist() is None
    if runner._restart_task is not None:
        runner._restart_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner._restart_task


@pytest.mark.asyncio
async def test_opt_in_reaction_steers_marks_and_writes_allowlist(
    tmp_path, monkeypatch
):
    _patch_resume_home(monkeypatch, tmp_path)
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester

    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:telegram:dm:other"] = other
    runner.session_store.mark_resume_pending.return_value = True

    runner._begin_restart_cycle()
    runner._restart_wind_down_offer = {
        "generation": runner._restart_generation,
        "nonce": runner._restart_wind_down_nonce,
        "message_id": "m-1",
        "channel_id": "req",
        "requester_user_id": "u1",
    }

    result = await runner.accept_restart_wind_down_opt_in(
        message_id="m-1",
        channel_id="req",
        requester_user_id="u1",
        emoji="⏸️",
        generation=runner._restart_generation,
        nonce=runner._restart_wind_down_nonce,
    )

    assert result == {
        "accepted": True,
        "accepted_count": 1,
        "steered": ["agent:main:telegram:dm:other"],
    }
    other.steer.assert_called_once_with(COOPERATIVE_RESTART_STEER)
    runner.session_store.mark_resume_pending.assert_called_once_with(
        "agent:main:telegram:dm:other", COOPERATIVE_RESTART_REASON
    )
    assert load_resume_allowlist() == {"agent:main:telegram:dm:other"}
    # The offer is spent — one accepted transition per cycle.
    assert runner._restart_wind_down_offer is None
    assert runner._restart_wind_down_accepted is True


def test_cooperative_reason_is_auto_resumed():
    assert COOPERATIVE_RESTART_REASON in GatewayRunner._AUTO_RESUME_REASONS


def test_cooperative_resume_note_continues_parked_work():
    note = build_resume_recovery_note(
        COOPERATIVE_RESTART_REASON, "", interactive=True
    )
    assert "cooperative gateway restart" in note
    assert "CONTINUE the parked task" in note
    assert "ask what they would like to do next" not in note
    assert "Do not ask what next" in note


def test_session_store_accepts_cooperative_reason(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore, SessionSource
    from gateway.config import Platform

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1",
        chat_type="dm",
        user_id="u1",
    )
    entry = store.get_or_create_session(source)
    assert store.mark_resume_pending(entry.session_key, COOPERATIVE_RESTART_REASON)
    assert store._entries[entry.session_key].resume_reason == COOPERATIVE_RESTART_REASON
    assert store._entries[entry.session_key].resume_pending is True


def test_session_entry_roundtrip_keeps_cooperative_reason():
    now = datetime.now()
    entry = SessionEntry(
        session_key="agent:main:telegram:dm:1",
        session_id="sid",
        created_at=now,
        updated_at=now,
        resume_pending=True,
        resume_reason=COOPERATIVE_RESTART_REASON,
        last_resume_marked_at=now,
    )
    restored = SessionEntry.from_dict(entry.to_dict())
    assert restored.resume_reason == COOPERATIVE_RESTART_REASON
    assert restored.resume_pending is True


def test_empty_active_set_writes_empty_resume_allowlist(tmp_path, monkeypatch):
    from gateway.restart_wind_down import (
        consume_resume_allowlist,
        load_resume_allowlist,
        write_resume_allowlist,
    )

    _patch_resume_home(monkeypatch, tmp_path)
    write_resume_allowlist([])
    assert load_resume_allowlist() == set()
    assert consume_resume_allowlist() == set()
    assert load_resume_allowlist() is None


def test_missing_allowlist_means_crash_path():
    from gateway.restart_wind_down import should_auto_resume_session

    assert should_auto_resume_session("any", None) is True
    assert should_auto_resume_session("live", {"live"}) is True
    assert should_auto_resume_session("stale", {"live"}) is False
    assert should_auto_resume_session("stale", set()) is False


def test_all_targets_finished_before_reaction_is_a_terminal_no_op(
    tmp_path, monkeypatch
):
    """Nobody left to ask → no steer, no mark, and no empty allowlist."""
    from gateway.restart_wind_down import load_resume_allowlist

    _patch_resume_home(monkeypatch, tmp_path)
    runner, _adapter = make_restart_runner()
    runner._restart_requested = True
    runner._begin_restart_cycle()
    runner._restart_wind_down_offer = {
        "generation": runner._restart_generation,
        "nonce": runner._restart_wind_down_nonce,
        "message_id": "m-1",
        "channel_id": "req",
        "requester_user_id": "u1",
    }

    result = asyncio.run(
        runner.accept_restart_wind_down_opt_in(
            message_id="m-1",
            channel_id="req",
            requester_user_id="u1",
            emoji="⏸️",
            generation=runner._restart_generation,
            nonce=runner._restart_wind_down_nonce,
        )
    )

    assert result == {
        "accepted": True,
        "no_targets": True,
        "accepted_count": 0,
    }
    runner.session_store.mark_resume_pending.assert_not_called()
    assert load_resume_allowlist() is None
    # Consumed: a later reaction on the same message can never re-run it.
    assert runner._restart_wind_down_offer is None


def test_snapshot_includes_active_chats_even_when_steer_is_rejected(
    tmp_path, monkeypatch
):
    from gateway.restart_wind_down import load_resume_allowlist

    _patch_resume_home(monkeypatch, tmp_path)
    runner, _adapter = make_restart_runner()
    stubborn = MagicMock()
    stubborn.steer.return_value = False
    runner._running_agents["agent:main:telegram:dm:live"] = stubborn

    steered = runner._request_cooperative_restart_wind_down()
    assert steered == []
    assert load_resume_allowlist() == {"agent:main:telegram:dm:live"}
    assert runner._cooperative_restart_sessions == ["agent:main:telegram:dm:live"]


@pytest.mark.asyncio
async def test_startup_resume_only_revives_snapshotted_active_chats(
    tmp_path, monkeypatch
):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.restart_wind_down import write_resume_allowlist

    _patch_resume_home(monkeypatch, tmp_path)
    write_resume_allowlist(["agent:main:telegram:dm:live"])

    runner, adapter = make_restart_runner()
    runner._persist_active_agents = MagicMock()
    live_source = make_restart_source(chat_id="live")
    stale_source = make_restart_source(chat_id="stale")
    now = datetime.now()
    live = SessionEntry(
        session_key="agent:main:telegram:dm:live",
        session_id="sid-live",
        created_at=now,
        updated_at=now,
        origin=live_source,
        platform=live_source.platform,
        chat_type="dm",
        resume_pending=True,
        resume_reason=COOPERATIVE_RESTART_REASON,
        last_resume_marked_at=now,
    )
    stale = SessionEntry(
        session_key="agent:main:telegram:dm:stale",
        session_id="sid-stale",
        created_at=now,
        updated_at=now,
        origin=stale_source,
        platform=stale_source.platform,
        chat_type="dm",
        resume_pending=True,
        resume_reason=COOPERATIVE_RESTART_REASON,
        last_resume_marked_at=now,
    )
    runner.session_store._entries = {
        live.session_key: live,
        stale.session_key: stale,
    }
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_called_once()
    event = adapter.handle_message.call_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.source == live_source
    assert event.message_type == MessageType.TEXT


# ── opt-in reaction: authorization and one-shot ──────────────────────────


def _armed_runner(tmp_path, monkeypatch, *, running=("agent:main:telegram:dm:other",)):
    """A runner whose restart cycle has one actionable ⏸️ offer armed."""
    _patch_resume_home(monkeypatch, tmp_path)
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester
    for session_key in running:
        agent = MagicMock()
        agent.steer.return_value = True
        runner._running_agents[session_key] = agent
    runner.session_store.mark_resume_pending.return_value = True
    runner._begin_restart_cycle()
    runner._restart_wind_down_offer = {
        "generation": runner._restart_generation,
        "nonce": runner._restart_wind_down_nonce,
        "message_id": "m-1",
        "channel_id": "req",
        "requester_user_id": "u1",
    }
    return runner, adapter


async def _react(runner, **overrides):
    """Submit one ⏸️ reaction event with any field overridden."""
    offer = {
        "message_id": "m-1",
        "channel_id": "req",
        "requester_user_id": "u1",
        "emoji": "⏸️",
        "generation": runner._restart_generation,
        "nonce": runner._restart_wind_down_nonce,
    }
    offer.update(overrides)
    return await runner.accept_restart_wind_down_opt_in(**offer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"requester_user_id": "u2"}, "wrong_user"),
        ({"emoji": "🔥"}, "wrong_emoji"),
        ({"emoji": "<:pause:123>"}, "wrong_emoji"),
        ({"message_id": "m-other"}, "wrong_message"),
        ({"channel_id": "elsewhere"}, "wrong_channel"),
        ({"generation": 999}, "stale_generation"),
        ({"nonce": "not-the-nonce"}, "stale_nonce"),
        ({"nonce": None}, "stale_nonce"),
    ],
)
async def test_invalid_reaction_events_do_nothing(
    tmp_path, monkeypatch, overrides, reason
):
    runner, _adapter = _armed_runner(tmp_path, monkeypatch)

    result = await _react(runner, **overrides)

    assert result == {"accepted": False, "reason": reason}
    runner._running_agents["agent:main:telegram:dm:other"].steer.assert_not_called()
    runner.session_store.mark_resume_pending.assert_not_called()
    assert load_resume_allowlist() is None
    # Still armed: an invalid event must not retire the offer.
    assert runner._restart_wind_down_offer is not None


@pytest.mark.asyncio
async def test_reaction_without_an_offer_or_a_restart_does_nothing(tmp_path, monkeypatch):
    _patch_resume_home(monkeypatch, tmp_path)
    runner, _adapter = make_restart_runner()
    runner._running_agents["agent:main:telegram:dm:other"] = MagicMock()

    no_offer = await runner.accept_restart_wind_down_opt_in(
        message_id="m-1",
        channel_id="req",
        requester_user_id="u1",
        emoji="⏸️",
        generation=1,
        nonce="n",
    )
    assert no_offer == {"accepted": False, "reason": "no_offer"}

    runner._begin_restart_cycle()
    runner._restart_wind_down_offer = {
        "generation": runner._restart_generation,
        "nonce": runner._restart_wind_down_nonce,
        "message_id": "m-1",
        "channel_id": "req",
        "requester_user_id": "u1",
    }
    not_restarting = await _react(runner)
    assert not_restarting == {"accepted": False, "reason": "not_restarting"}
    assert load_resume_allowlist() is None


@pytest.mark.asyncio
async def test_bare_variation_selector_emoji_is_accepted(tmp_path, monkeypatch):
    """Discord delivers ⏸ without U+FE0F on some clients — same emoji."""
    runner, _adapter = _armed_runner(tmp_path, monkeypatch)

    result = await _react(runner, emoji="⏸")

    assert result["accepted"] is True
    assert result["accepted_count"] == 1


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_valid_reactions_yield_one_transition(
    tmp_path, monkeypatch
):
    runner, _adapter = _armed_runner(tmp_path, monkeypatch)

    # Remove/re-add: the same event submitted twice.
    first = await _react(runner)
    second = await _react(runner)

    assert first["accepted"] is True
    assert second == {"accepted": False, "reason": "already_accepted"}
    assert (
        runner._running_agents["agent:main:telegram:dm:other"].steer.call_count == 1
    )

    # Concurrent: both submitted before either completes.
    runner2, _a2 = _armed_runner(tmp_path, monkeypatch)
    results = await asyncio.gather(_react(runner2), _react(runner2))
    assert [r.get("accepted") for r in results] == [True, False]
    assert (
        runner2._running_agents["agent:main:telegram:dm:other"].steer.call_count == 1
    )
    runner2.session_store.mark_resume_pending.assert_called_once_with(
        "agent:main:telegram:dm:other", COOPERATIVE_RESTART_REASON
    )


@pytest.mark.asyncio
async def test_runner_side_latch_is_authoritative_even_if_adapter_dedup_fails(
    tmp_path, monkeypatch
):
    """A reaction re-submitted by a broken adapter still cannot re-steer."""
    runner, _adapter = _armed_runner(tmp_path, monkeypatch)
    offer = dict(runner._restart_wind_down_offer)

    assert (await _react(runner))["accepted"] is True
    # Simulate the adapter's registry losing the "already handled" state.
    runner._restart_wind_down_offer = dict(offer)
    assert (await _react(runner)) == {
        "accepted": False,
        "reason": "already_accepted",
    }
    assert (
        runner._running_agents["agent:main:telegram:dm:other"].steer.call_count == 1
    )


# ── offer eligibility and prompt lifecycle ───────────────────────────────


def _discord_runner(tmp_path, monkeypatch, *, user_id="111222333444555666", **kwargs):
    _patch_resume_home(monkeypatch, tmp_path)
    adapter = MagicMock()
    adapter.send_restart_wind_down_offer = AsyncMock(return_value="m-1")
    adapter.finalize_restart_wind_down_offer = AsyncMock(return_value=True)
    runner, _ = make_restart_runner(adapter=adapter, platform=Platform.DISCORD)
    source = make_restart_source(
        chat_id="9001",
        chat_type="thread",
        thread_id="9001",
        platform=Platform.DISCORD,
        user_id=user_id,
    )
    for key, value in kwargs.items():
        setattr(source, key, value)
    # begin_user_restart records the requester's routing before it offers, so
    # the wind-down snapshot knows whose turn to skip.
    runner._restart_command_source = source
    return runner, adapter, source


@pytest.mark.asyncio
async def test_offer_needs_native_discord_numeric_requester_and_a_live_peer(
    tmp_path, monkeypatch
):
    runner, adapter, source = _discord_runner(tmp_path, monkeypatch)

    # No live non-requester chat → no offer.
    assert await runner._send_restart_wind_down_prompt(source) is False
    adapter.send_restart_wind_down_offer.assert_not_awaited()

    # Requester's own turn is live — still nobody to ask.
    runner._running_agents[runner._session_key_for_source(source)] = MagicMock()
    assert await runner._send_restart_wind_down_prompt(source) is False
    adapter.send_restart_wind_down_offer.assert_not_awaited()

    # A second live chat → exactly one offer.
    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:discord:thread:other"] = other
    assert await runner._send_restart_wind_down_prompt(source) is True
    adapter.send_restart_wind_down_offer.assert_awaited_once()
    assert adapter.send_restart_wind_down_offer.await_args.kwargs["channel_id"] == "9001"
    assert adapter.send_restart_wind_down_offer.await_args.kwargs[
        "requester_user_id"
    ] == "111222333444555666"
    assert adapter.send_restart_wind_down_offer.await_args.kwargs["generation"] == 1
    assert runner._restart_wind_down_offer == {
        "generation": 1,
        "nonce": runner._restart_wind_down_nonce,
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"delivered_via_upstream_relay": True},
        {"user_id": None},
        {"user_id": "alice"},
    ],
)
async def test_no_offer_for_relay_or_non_numeric_requester(
    tmp_path, monkeypatch, kwargs
):
    runner, adapter, source = _discord_runner(tmp_path, monkeypatch, **kwargs)
    runner._running_agents["agent:main:discord:thread:other"] = MagicMock()

    assert await runner._send_restart_wind_down_prompt(source) is False
    adapter.send_restart_wind_down_offer.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_offer_for_a_non_discord_requester(tmp_path, monkeypatch):
    _patch_resume_home(monkeypatch, tmp_path)
    adapter = MagicMock()
    adapter.send_restart_wind_down_offer = AsyncMock(return_value="m-1")
    runner, _telegram = make_restart_runner(adapter=adapter)
    source = make_restart_source(chat_id="42", user_id="111222333444555666")
    runner._running_agents["agent:main:telegram:dm:other"] = MagicMock()

    assert await runner._send_restart_wind_down_prompt(source) is False
    adapter.send_restart_wind_down_offer.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_prompt_send_leaves_the_restart_waiting_naturally(
    tmp_path, monkeypatch
):
    runner, adapter, source = _discord_runner(tmp_path, monkeypatch)
    runner._running_agents["agent:main:discord:thread:other"] = MagicMock()
    adapter.send_restart_wind_down_offer = AsyncMock(return_value=None)

    assert await runner._send_restart_wind_down_prompt(source) is False
    assert runner._restart_wind_down_offer is None


# ── terminal closing of the prompt ───────────────────────────────────────


@pytest.mark.asyncio
async def test_natural_drain_before_reaction_terminally_closes_the_prompt(
    tmp_path, monkeypatch
):
    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)
    runner._running_agents["agent:main:discord:thread:other"] = MagicMock()
    runner._restart_requested = True
    runner._restart_wind_down_offer = {
        "generation": 1,
        "nonce": "n",
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
    }

    await runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_DRAINED)

    # Local state went first, so a late reaction is a no-op even though the
    # adapter below will fail.
    assert runner._restart_wind_down_offer is None
    assert (await _react(runner)) == {"accepted": False, "reason": "no_offer"}
    adapter.finalize_restart_wind_down_offer.assert_awaited_once_with(
        message_id="m-1",
        channel_id="9001",
        spec=restart_wind_down_terminal_spec(WIND_DOWN_TERMINAL_DRAINED),
    )
    assert load_resume_allowlist() is None


@pytest.mark.asyncio
async def test_cleanup_failure_still_leaves_the_local_offer_invalid(
    tmp_path, monkeypatch
):
    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)
    runner._restart_wind_down_offer = {
        "generation": 1,
        "nonce": "n",
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
    }
    adapter.finalize_restart_wind_down_offer = AsyncMock(
        side_effect=RuntimeError("discord is down")
    )

    await runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_DRAINED)

    assert runner._restart_wind_down_offer is None
    assert (await _react(runner)) == {"accepted": False, "reason": "no_offer"}


@pytest.mark.asyncio
async def test_finalize_without_an_offer_is_a_no_op(tmp_path, monkeypatch):
    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)

    await runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_DRAINED)

    adapter.finalize_restart_wind_down_offer.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_offer_restart_still_clears_a_stale_receipt(tmp_path, monkeypatch):
    """A cycle that never had an embed must not leak an older cycle's receipt.

    Telegram /restart, signal, update, and API restarts never send an offer,
    and a Discord restart with no live peer sends none either — all of them
    finalize with ``offer=None``.
    """
    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)
    write_resume_allowlist(["agent:main:discord:thread:old"])

    await runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_DRAINED)

    adapter.finalize_restart_wind_down_offer.assert_not_awaited()
    assert load_resume_allowlist() is None


@pytest.mark.asyncio
async def test_after_turn_wait_finalizes_the_prompt_on_drain_and_on_cap(
    tmp_path, monkeypatch
):
    from gateway.restart import DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT

    # Natural drain: the only live work finishes while we wait.
    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)
    runner._restart_after_turn_timeout = DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    runner._restart_wind_down_offer = {
        "generation": 1,
        "nonce": "n",
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
    }
    agent = MagicMock()
    agent.steer.return_value = True
    runner._running_agents["agent:main:discord:thread:other"] = agent

    async def _finish_soon():
        await asyncio.sleep(0)
        runner._running_agents.clear()

    await asyncio.gather(
        runner._await_active_work_before_restart(), _finish_soon()
    )
    adapter.finalize_restart_wind_down_offer.assert_awaited_once()

    # Safety cap: the cap elapses with work still active.
    runner2, adapter2, _s2 = _discord_runner(tmp_path, monkeypatch)
    runner2._restart_after_turn_timeout = 0.0
    runner2._restart_wind_down_offer = {
        "generation": 1,
        "nonce": "n",
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
    }
    runner2._running_agents["agent:main:discord:thread:other"] = MagicMock()

    await runner2._await_active_work_before_restart()

    adapter2.finalize_restart_wind_down_offer.assert_awaited_once()
    assert (
        adapter2.finalize_restart_wind_down_offer.await_args.kwargs["spec"]["title"]
        == "⏳ Restart proceeding"
    )


@pytest.mark.asyncio
async def test_adapters_stay_connected_through_the_after_turn_wait(
    tmp_path, monkeypatch
):
    """Reactions can only arrive while the Discord socket is still open."""
    from gateway.restart import DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT

    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)
    runner._restart_after_turn_timeout = DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    agent = MagicMock()
    agent.steer.return_value = True
    runner._running_agents["agent:main:discord:thread:other"] = agent

    async def _finish_soon():
        await asyncio.sleep(0)
        runner._running_agents.clear()

    await asyncio.gather(runner._await_active_work_before_restart(), _finish_soon())

    adapter.disconnect.assert_not_called()


# ── prompt/finalize race ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_send_racing_finalization_leaves_no_actionable_prompt(
    tmp_path, monkeypatch
):
    """Drain completes while the embed is still being sent."""
    runner, adapter, source = _discord_runner(tmp_path, monkeypatch)
    runner._running_agents["agent:main:discord:thread:other"] = MagicMock()
    send_started = asyncio.Event()
    finalize_done = asyncio.Event()

    async def _slow_send(**kwargs):
        send_started.set()
        await finalize_done.wait()
        return "m-1"

    adapter.send_restart_wind_down_offer = _slow_send

    send_task = asyncio.create_task(runner._send_restart_wind_down_prompt(source))
    await send_started.wait()
    # Natural drain wins while the message is in flight.
    finalize_task = asyncio.create_task(
        runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_DRAINED)
    )
    await asyncio.sleep(0)
    finalize_done.set()

    assert await send_task is False
    await finalize_task
    adapter.finalize_restart_wind_down_offer.assert_awaited_once()

    # The just-sent prompt is inert: the runner recorded it only after the
    # finalize had already retired the cycle, so it must be re-retired.
    assert runner._restart_wind_down_offer is None
    assert (await _react(runner)) == {"accepted": False, "reason": "no_offer"}


@pytest.mark.asyncio
async def test_valid_reaction_during_prompt_send_waits_and_is_accepted(
    tmp_path, monkeypatch
):
    """A requester ⏸️ that beats the runner's registration is not lost.

    The adapter registers the offered message before its seeded-reaction
    round trip, so a fast reaction can reach the runner while the offer send
    has not yet returned and ``_restart_wind_down_offer`` is still None. It
    must wait for the send to land and then be accepted exactly once — not
    be answered ``no_offer`` and leave a live offer behind.
    """
    runner, adapter, source = _discord_runner(tmp_path, monkeypatch)
    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:discord:thread:other"] = other
    runner.session_store.mark_resume_pending.return_value = True

    send_in_flight = asyncio.Event()
    release_send = asyncio.Event()

    async def _slow_send(**kwargs):
        send_in_flight.set()
        await release_send.wait()
        return "m-1"

    adapter.send_restart_wind_down_offer = _slow_send

    send_task = asyncio.create_task(runner._send_restart_wind_down_prompt(source))
    await send_in_flight.wait()
    # The restart proceeds and the requester reacts to a message id only the
    # adapter knows yet — the runner's own offer registration is still pending.
    runner._restart_requested = True
    reaction = {
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
        "emoji": "⏸️",
        "generation": runner._restart_generation,
        "nonce": runner._restart_wind_down_nonce,
    }
    react_task = asyncio.create_task(runner.accept_restart_wind_down_opt_in(**reaction))
    await asyncio.sleep(0)
    release_send.set()

    assert await send_task is True
    assert (await react_task)["accepted"] is True
    other.steer.assert_called_once_with(COOPERATIVE_RESTART_STEER)
    # The reaction consumed the just-registered offer; nothing stale remains.
    assert runner._restart_wind_down_offer is None
    assert runner._restart_wind_down_accepted is True
    # A remove/re-add of the same ⏸️ can never re-run the wind-down.
    assert (await runner.accept_restart_wind_down_opt_in(**reaction)) == {
        "accepted": False,
        "reason": "already_accepted",
    }


# ── stale receipts must not leak across cycles ───────────────────────────


@pytest.mark.asyncio
async def test_no_opt_in_cycle_clears_a_stale_allowlist_from_another_cycle(
    tmp_path, monkeypatch
):
    runner, adapter, _source = _discord_runner(tmp_path, monkeypatch)
    write_resume_allowlist(["agent:main:discord:thread:old"])
    runner._restart_wind_down_offer = {
        "generation": 1,
        "nonce": "n",
        "message_id": "m-1",
        "channel_id": "9001",
        "requester_user_id": "111222333444555666",
    }

    await runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_DRAINED)

    assert load_resume_allowlist() is None


@pytest.mark.asyncio
async def test_opt_in_allowlist_survives_finalization(tmp_path, monkeypatch):
    runner, _adapter = _armed_runner(tmp_path, monkeypatch)

    assert (await _react(runner))["accepted"] is True
    await runner._finalize_restart_wind_down_offer(WIND_DOWN_TERMINAL_OPTED_IN)

    assert load_resume_allowlist() == {"agent:main:telegram:dm:other"}


def test_clear_resume_allowlist_tolerates_a_missing_file(tmp_path, monkeypatch):
    _patch_resume_home(monkeypatch, tmp_path)
    assert clear_resume_allowlist() is True


# ── copy and emoji helpers ───────────────────────────────────────────────


def test_pause_emoji_normalization():
    assert normalize_pause_emoji("⏸") == "⏸️"
    assert normalize_pause_emoji("⏸️") == "⏸️"
    assert normalize_pause_emoji("🔥") is None
    assert normalize_pause_emoji("<:pause:1>") is None
    assert normalize_pause_emoji("") is None
    assert normalize_pause_emoji(None) is None


def test_prompt_spec_copy_and_footer():
    spec = restart_wind_down_prompt_spec()
    assert spec["title"] == "⏳ Waiting for active sessions"
    assert spec["description"] == (
        "The gateway will restart when active sessions finish. "
        "React with ⏸️ to ask them to pause safely now."
    )
    # A distinct wind-down marker, never the resolve-ticket one.
    assert "restart wind-down" in spec["footer"]
    assert "ticket" not in spec["footer"]
    # The agent-facing steer text must never surface in the user embed.
    assert COOPERATIVE_RESTART_STEER not in spec["description"]
    assert COOPERATIVE_RESTART_STEER not in spec["title"]


def test_terminal_specs_name_session_counts_without_internal_keys():
    opted_in = restart_wind_down_terminal_spec(WIND_DOWN_TERMINAL_OPTED_IN, accepted=2)
    assert opted_in["title"] == "⏸️ Pausing 2 active sessions"
    assert "2 active sessions accepted" in opted_in["description"]
    assert "agent:" not in opted_in["description"]

    single = restart_wind_down_terminal_spec(WIND_DOWN_TERMINAL_OPTED_IN, accepted=1)
    assert single["title"] == "⏸️ Pausing 1 active session"

    # Zero accepted steers: honest count, no vacuous auto-continue promise.
    none = restart_wind_down_terminal_spec(WIND_DOWN_TERMINAL_OPTED_IN, accepted=0)
    assert none["title"] == "⏸️ Pause requested"
    assert "No active sessions accepted" in none["description"]
    assert "will continue automatically" not in none["description"]
    assert "agent:" not in none["description"]

    drained = restart_wind_down_terminal_spec(WIND_DOWN_TERMINAL_DRAINED)
    assert "finished" in drained["description"]
    assert "proceeding" in drained["description"]

    for kind in (
        WIND_DOWN_TERMINAL_OPTED_IN,
        WIND_DOWN_TERMINAL_DRAINED,
        "closed",
        "safety_cap",
        "no_targets",
    ):
        spec = restart_wind_down_terminal_spec(kind, accepted=3)
        assert "agent:" not in spec["description"] + spec["title"]
        assert COOPERATIVE_RESTART_STEER not in spec["description"]
