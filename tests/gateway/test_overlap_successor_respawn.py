"""A predecessor that is still draining replaces a successor that died."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import overlap_restart


def _runner_and_adapter(spawn_pids):
    adapter = MagicMock()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(side_effect=list(spawn_pids))
    adapter.wait_for_overlap_successor = AsyncMock()
    runner = MagicMock(spec=[])
    runner.adapters = {"discord": adapter}
    runner._profile_adapters = {}
    runner._draining = True
    runner._restart_requested = True
    runner._stop_task = None
    runner._shutdown_event = asyncio.Event()
    runner._overlap_restart_successor_started = False
    runner._overlap_restart_successor_pid = None
    return runner, adapter


@pytest.mark.asyncio
async def test_dead_successor_is_replaced_while_draining(monkeypatch):
    runner, adapter = _runner_and_adapter([100, 200])
    alive = {100: True, 200: True}
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: alive.get(pid, False))

    assert await overlap_restart.spawn_successor(runner, via_service=True) == 100
    task = asyncio.create_task(overlap_restart.watch_successor(
        runner, via_service=True, poll_seconds=0.01, backoff=(0.01,)))
    await asyncio.sleep(0.05)
    assert adapter.spawn_overlap_successor.await_count == 1

    alive[100] = False  # e.g. the hub restart killed it
    for _ in range(100):
        await asyncio.sleep(0.01)
        if runner._overlap_restart_successor_pid == 200:
            break
    assert runner._overlap_restart_successor_pid == 200
    assert runner._overlap_restart_successor_started is True
    adapter.mark_overlap_successor_lost.assert_called_once()
    assert adapter.mark_overlap_successor_ready.call_count == 2

    runner._stop_task = object()  # stop() began: watcher must exit
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_failed_respawn_leaves_legacy_restart_state_and_retries(monkeypatch):
    runner, adapter = _runner_and_adapter([100, RuntimeError("hub down"), None, 300])
    alive = {100: False, 300: True}
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: alive.get(pid, False))
    await overlap_restart.spawn_successor(runner, via_service=True)

    task = asyncio.create_task(overlap_restart.watch_successor(
        runner, via_service=True, poll_seconds=0.01, backoff=(0.01,)))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runner._overlap_restart_successor_pid == 300:
            break
    assert runner._overlap_restart_successor_pid == 300
    assert adapter.spawn_overlap_successor.await_count == 4
    runner._draining = False
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_lost_successor_clears_started_flag(monkeypatch):
    runner, adapter = _runner_and_adapter([100])
    await overlap_restart.spawn_successor(runner, via_service=True)
    overlap_restart.mark_successor_lost(runner)
    assert runner._overlap_restart_successor_started is False
    assert runner._overlap_restart_successor_pid is None
