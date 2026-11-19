"""``/bg`` and its executor worker inside the restart accounting (#77184).

``_background_tasks`` used to be invisible to ``_active_work_count()``, and
cancelling the /bg coroutine only cancelled the asyncio view of its executor
work — the worker thread kept running while every count read zero. These
regressions pin all three sides: a pending /bg blocks the count, a cancelled
/bg with a live worker stays counted through the deferred-worker registry,
and the worker's delayed completion is what finally unblocks it.
"""

import asyncio
import concurrent.futures
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


def _pending_future(*, watcher: bool = False) -> concurrent.futures.Future:
    future = concurrent.futures.Future()
    if watcher:
        future._hermes_supervised_watcher = True  # type: ignore[attr-defined]
    return future


# ── pending /bg tasks are counted work ──────────────────────────────────────


def test_pending_background_task_blocks_the_active_work_count():
    runner, _adapter = make_restart_runner()
    assert runner._active_work_count() == 0

    runner._background_tasks.add(_pending_future())
    # The tolerant count first: this is the regression that bit — a pending
    # /bg task read as zero active work.
    assert runner._active_work_count() == 1
    assert runner._authoritative_active_work_count() == 1

    # A second pending task is a second unit, and a completed one is none.
    done = concurrent.futures.Future()
    done.set_result(None)
    runner._background_tasks.add(done)
    runner._background_tasks.add(_pending_future())
    assert runner._active_work_count() == 2


def test_supervised_watchers_are_not_counted_as_restart_work():
    """Permanent supervised watchers share ``_background_tasks`` with /bg —
    counting them would wedge every restart at "1 active unit" forever
    (the same exclusion ``_scale_to_zero_has_live_background_work`` uses)."""
    runner, _adapter = make_restart_runner()
    runner._background_tasks.add(_pending_future(watcher=True))
    runner._background_tasks.add(_pending_future(watcher=True))
    assert runner._pending_background_task_count() == 0
    assert runner._authoritative_active_work_count() == 0

    # Transient work beside the watchers still counts.
    runner._background_tasks.add(_pending_future())
    assert runner._pending_background_task_count() == 1


@pytest.mark.asyncio
async def test_secondary_reconnect_does_not_pin_draining_predecessor(monkeypatch):
    """A permanent profile reconnect is service maintenance, not active work.

    The predecessor must still wait for a real turn, but completing that turn
    must release the drain even while the reconnect keeps retrying.
    """
    runner, adapter = make_restart_runner()
    runner._profile_failed_platforms = {}
    reconnect_started = asyncio.Event()
    drain_wait_started = asyncio.Event()
    real_wait = runner._await_active_work_before_restart

    async def observed_wait():
        drain_wait_started.set()
        return await real_wait()

    async def reconnect(_profile_name, _platform):
        reconnect_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_run_secondary_profile_reconnect", reconnect)
    monkeypatch.setattr(runner, "_await_active_work_before_restart", observed_wait)
    runner._running_agents["agent:main:discord:dm:1"] = MagicMock()
    assert runner._restart_wait_blocking_count() == 1
    runner._schedule_secondary_profile_reconnect("secondary", adapter.platform, adapter)
    reconnect_task = runner._profile_failed_platforms["secondary"][adapter.platform]
    runner.stop = AsyncMock()
    runner._launch_detached_restart_watcher = AsyncMock()

    try:
        await asyncio.wait_for(reconnect_started.wait(), timeout=2)
        assert runner.request_restart(detached=False, via_service=True) is True
        await asyncio.wait_for(drain_wait_started.wait(), timeout=2)
        runner.stop.assert_not_awaited()

        runner._running_agents.clear()
        assert runner._restart_wait_blocking_count() == 0
        await asyncio.wait_for(runner._restart_task, timeout=2)
        runner.stop.assert_awaited_once_with(
            restart=True, detached_restart=False, service_restart=True
        )
        assert not reconnect_task.done()
    finally:
        tasks = [reconnect_task, getattr(runner, "_restart_task", None)]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


# ── cancelling the coroutine must not lose the running worker ───────────────


class _SingleShotExecutor:
    """Executor double whose worker is already running when submitted."""

    def __init__(self, future: concurrent.futures.Future):
        self._future = future
        self.submitted = []

    def submit(self, fn, /, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return self._future


@pytest.mark.asyncio
async def test_cancelled_bg_keeps_its_live_executor_worker_counted():
    """The restart cancels the /bg coroutine; the worker thread never sees
    that cancellation. The still-running future must move into the
    deferred-worker registry so the drain keeps waiting for it."""
    runner, _adapter = make_restart_runner()
    worker = concurrent.futures.Future()
    assert worker.set_running_or_notify_cancel() is True  # cannot be cancelled now
    runner._get_executor = lambda: _SingleShotExecutor(worker)
    agent = MagicMock(name="bg-agent")
    agent_holder: dict = {"agent": agent}

    async def run_sync():  # pragma: no cover — the fake worker never runs it
        return {"final_response": ""}

    task = asyncio.create_task(
        runner._run_background_agent_executor_work(run_sync, agent_holder)
    )
    for _ in range(10):
        await asyncio.sleep(0)  # let the submit + wrap_future settle

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker survived the coroutine's cancellation and stays counted:
    # it is registered with its agent (so the interrupt hook can reach it)
    # and every accounting path still sees one unit of work.
    assert runner._deferred_agent_workers[worker] is agent
    assert worker.done() is False
    assert runner._active_deferred_agent_worker_count() == 1
    assert runner._pending_background_task_count() == 0  # coroutine is gone
    assert runner._authoritative_active_work_count() == 1


@pytest.mark.asyncio
async def test_delayed_worker_completion_finally_unblocks_the_count():
    """Continuation of the cancelled case: when the worker thread does finish,
    the registry entry drains and the count returns to zero — the block is
    bounded by the worker's real completion, never by a timeout."""
    runner, _adapter = make_restart_runner()
    worker = concurrent.futures.Future()
    assert worker.set_running_or_notify_cancel() is True
    runner._get_executor = lambda: _SingleShotExecutor(worker)
    agent_holder: dict = {"agent": MagicMock()}

    task = asyncio.create_task(
        runner._run_background_agent_executor_work(lambda: None, agent_holder)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner._active_deferred_agent_worker_count() == 1

    worker.set_result({"final_response": "late but finished"})
    for _ in range(10):
        await asyncio.sleep(0)  # let the done callbacks run

    assert worker.done() is True
    assert worker not in runner._deferred_agent_workers
    assert runner._active_deferred_agent_worker_count() == 0
    assert runner._authoritative_active_work_count() == 0
