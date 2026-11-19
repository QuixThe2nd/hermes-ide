"""Regression tests for the empty-response/restart guard pair.

Live incident: a 105-minute Discord turn (132 API calls) was killed by a
gateway restart; the user got the generic "Processing completed but no
response was generated" reply because the shutdown interrupt silenced the
final inference attempt, and the restart drain — deferred repeatedly —
proceeded anyway.

Covers:
- a shutdown/restart-interrupted turn produces an explicit
  "gateway restarted mid-task" reply pointing at /retry;
- a restart-from-update never proceeds while a genuinely active (non-idle)
  work unit exists;
- a unit idle past the inactivity timeout does NOT hold a
  restart-from-update (while it still holds a user-requested restart).
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from tests.gateway.restart_test_helpers import make_restart_runner


# ── CHANGE 1: shutdown-interrupted turn reply ───────────────────────────────


def test_shutdown_interrupted_turn_gets_restart_reply():
    result = gateway_run._normalize_empty_agent_response(
        {"interrupted": True, "api_calls": 132, "failed": False,
         "interrupt_message": "Gateway restarting"},
        "",
    )
    assert "/retry" in result
    assert "did not finish" in result
    assert "no response was generated" not in result


def test_shutdown_signal_interrupted_turn_gets_restart_reply():
    result = gateway_run._normalize_empty_agent_response(
        {"interrupted": True, "api_calls": 3, "failed": False,
         "interrupt_message": "Gateway shutting down"},
        "",
    )
    assert "/retry" in result


def test_user_stop_interrupt_keeps_existing_behavior():
    """Other interrupt causes are untouched: silence for a user /stop drain."""
    result = gateway_run._normalize_empty_agent_response(
        {"interrupted": True, "api_calls": 5, "failed": False,
         "interrupt_message": "User requested stop"},
        "",
    )
    assert result == ""


def test_uninterrupted_empty_turn_keeps_generic_reply():
    result = gateway_run._normalize_empty_agent_response(
        {"interrupted": False, "api_calls": 5, "failed": False},
        "",
    )
    assert "no response was generated" in result


def test_interrupted_zero_api_calls_keeps_existing_reply():
    result = gateway_run._normalize_empty_agent_response(
        {"interrupted": True, "api_calls": 0, "failed": False,
         "interrupt_message": "Gateway restarting"},
        "",
    )
    assert "interrupted before processing started" in result


# ── CHANGE 2: update-restart drain semantics ────────────────────────────────


def _hermetic_zero_probes(runner):
    runner._active_cron_job_count = lambda *, strict=False: 0
    runner._active_api_run_count = lambda *, strict=False: 0


def _active_agent(idle_s: float) -> MagicMock:
    agent = MagicMock()
    agent.get_activity_summary = lambda: {"seconds_since_activity": idle_s}
    return agent


@pytest.mark.asyncio
async def test_update_restart_with_active_unit_never_proceeds(monkeypatch, caplog):
    monkeypatch.setattr(gateway_run, "_RESTART_WAIT_RETRY_SLEEP_S", 0.01)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "1800")
    runner, _adapter = make_restart_runner()
    _hermetic_zero_probes(runner)
    stop = AsyncMock()
    runner.stop = stop
    runner._running_agents["chat:1"] = _active_agent(idle_s=2.0)

    assert runner.request_restart(from_update=True) is True

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await asyncio.sleep(0.3)  # many drain iterations, all still busy
    stop.assert_not_called()
    assert runner._draining is True
    # The existing defer log fires (first iteration) and keeps its format.
    assert any(
        "Restart deferred: still waiting on" in r.message and "active work" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_update_restart_idle_past_timeout_unit_does_not_block(monkeypatch):
    monkeypatch.setattr(gateway_run, "_RESTART_WAIT_RETRY_SLEEP_S", 0.01)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "10")
    runner, _adapter = make_restart_runner()
    _hermetic_zero_probes(runner)
    stop = AsyncMock()
    runner.stop = stop
    # Idle for 300s against a 10s inactivity timeout: wedged, does not hold
    # a restart-from-update.
    runner._running_agents["chat:1"] = _active_agent(idle_s=300.0)

    assert runner.request_restart(via_service=True, from_update=True) is True

    for _ in range(100):
        await asyncio.sleep(0.05)
        if stop.await_count:
            break
    stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_user_restart_still_held_by_idle_past_timeout_unit(monkeypatch):
    """Same wedged unit, ordinary restart: the wait stays unbounded on purpose."""
    monkeypatch.setattr(gateway_run, "_RESTART_WAIT_RETRY_SLEEP_S", 0.01)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "10")
    runner, _adapter = make_restart_runner()
    _hermetic_zero_probes(runner)
    stop = AsyncMock()
    runner.stop = stop
    runner._running_agents["chat:1"] = _active_agent(idle_s=300.0)

    assert runner.request_restart(from_update=False) is True

    await asyncio.sleep(0.3)
    stop.assert_not_called()
    assert runner._draining is True


# ── update-restart marker round-trip ────────────────────────────────────────


def test_update_restart_marker_round_trip(tmp_path, monkeypatch):
    from gateway.status import consume_update_restart_marker_for_self, write_update_restart_marker

    assert write_update_restart_marker(
        __import__("os").getpid(), target_home=tmp_path
    )
    monkeypatch.setattr(
        "gateway.status._get_process_hermes_home", lambda: tmp_path
    )
    assert consume_update_restart_marker_for_self() is True
    # Consumed: a second read (e.g. stray later SIGUSR1) is an ordinary restart.
    assert consume_update_restart_marker_for_self() is False
