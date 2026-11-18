"""Update/SIGUSR1 restarts ride the same overlap core as ``/restart``.

``hermes update`` SIGUSR1s every gateway after the code swap, and
``hermes gateway restart`` falls back to the same signal when the control
socket cannot answer. The gateway's signal entry must run the ONE shared
restart sequence ``/restart`` uses — transport flags derived from the host,
not hardcoded — so an overlap-capable adapter (the Discord hub client)
spawns the successor and drain-window events hand off to it instead of
earning the "not accepting new work" refusal on hosts with no service
manager.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import gateway.run as gateway_run
import pytest

from gateway.overlap_restart import handoff_overlap_event
from gateway.restart import queue_signal_restart, queue_user_restart
from gateway.status import write_update_restart_marker
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def _manual_host(monkeypatch):
    """Host conditions of a manually-launched gateway: no supervisor, no container."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)


def _overlap_runner():
    runner, adapter = make_restart_runner()
    runner._adapter_for_source = lambda _source: adapter
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=os.getpid())
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    return runner, adapter


def _neutral_stop_machinery(monkeypatch):
    monkeypatch.setattr("gateway.overlap_restart.watch_successor", AsyncMock())
    monkeypatch.setattr(
        "tools.browser_tool_lifecycle.cleanup_all_browsers", MagicMock()
    )
    monkeypatch.setattr(
        "tools.terminal_tool_lifecycle.cleanup_all_environments", MagicMock()
    )


async def _wait_successor_latched(runner, timeout: float = 5.0) -> None:
    async def _latched() -> None:
        while not getattr(runner, "_overlap_restart_successor_started", False):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_latched(), timeout=timeout)


@pytest.mark.asyncio
async def test_update_signal_restart_manual_gateway_spawns_successor(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    _manual_host(monkeypatch)
    _neutral_stop_machinery(monkeypatch)
    runner, adapter = _overlap_runner()
    assert write_update_restart_marker(os.getpid())

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        await asyncio.wait_for(queue_signal_restart(runner), timeout=5.0)
        assert runner._restart_from_update is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    adapter.spawn_overlap_successor.assert_awaited_once()
    assert adapter.spawn_overlap_successor.await_args.kwargs["via_service"] is False
    assert runner._overlap_restart_successor_pid == os.getpid()
    assert runner._restart_via_service is False
    assert runner._restart_detached is True


@pytest.mark.asyncio
async def test_signal_restart_matches_slash_restart_transport_flags(
    tmp_path, monkeypatch
):
    """Same host ⇒ same transport decision: the signal door cannot drift."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    _manual_host(monkeypatch)
    _neutral_stop_machinery(monkeypatch)
    monkeypatch.setattr(
        "gateway.restart_preflight.warn_before_user_restart", AsyncMock(return_value=None)
    )
    signal_runner, signal_adapter = _overlap_runner()
    slash_runner, slash_adapter = _overlap_runner()

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        await asyncio.wait_for(queue_signal_restart(signal_runner), timeout=5.0)
        status = await queue_user_restart(
            slash_runner, make_restart_source(), "m1"
        )
        assert status["status"] == "restarting"
        await asyncio.wait_for(slash_runner._restart_task, timeout=5.0)

    assert signal_runner._restart_via_service == slash_runner._restart_via_service
    assert signal_runner._restart_detached == slash_runner._restart_detached
    assert signal_runner._restart_from_update is False
    for adapter in (signal_adapter, slash_adapter):
        adapter.spawn_overlap_successor.assert_awaited_once()
        assert adapter.spawn_overlap_successor.await_args.kwargs["via_service"] is False


@pytest.mark.asyncio
async def test_signal_restart_on_supervised_gateway_keeps_service_route(
    tmp_path, monkeypatch
):
    """Under a service manager the signal path still exits to the supervisor."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: True)
    _neutral_stop_machinery(monkeypatch)
    runner, adapter = _overlap_runner()

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        await asyncio.wait_for(queue_signal_restart(runner), timeout=5.0)
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    assert runner._restart_via_service is True
    assert runner._restart_detached is False
    adapter.spawn_overlap_successor.assert_awaited_once()
    assert adapter.spawn_overlap_successor.await_args.kwargs["via_service"] is True


@pytest.mark.asyncio
async def test_update_signal_drain_window_event_hands_off_to_successor(
    tmp_path, monkeypatch
):
    """No ⏳ refusal: the update drain has a successor to hand events to."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    _manual_host(monkeypatch)
    _neutral_stop_machinery(monkeypatch)
    runner, adapter = _overlap_runner()
    assert write_update_restart_marker(os.getpid())
    # One live work unit keeps the drain open (stop() not reached yet).
    runner._running_agents["sess"] = object()

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        await asyncio.wait_for(queue_signal_restart(runner), timeout=5.0)
        await _wait_successor_latched(runner)
        assert runner._draining is True

        adapter.handoff_event = AsyncMock(return_value=True)
        event = SimpleNamespace(source=make_restart_source(), _gateway_accepted=False)
        handed_off = await handoff_overlap_event(
            runner, event, "sess", require_successor_ready=True
        )

        assert handed_off is True
        runner._restart_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner._restart_task, timeout=5.0)


@pytest.mark.asyncio
async def test_signal_restart_without_marker_keeps_ordinary_drain_semantics(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    _manual_host(monkeypatch)
    _neutral_stop_machinery(monkeypatch)
    runner, _adapter = _overlap_runner()

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        await asyncio.wait_for(queue_signal_restart(runner), timeout=5.0)
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    assert runner._restart_from_update is False
