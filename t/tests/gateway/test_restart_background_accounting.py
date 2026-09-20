"""``/bg`` and its executor worker inside the restart accounting (#77184).

``_background_tasks`` used to be invisible to ``_active_work_count()`` and
to the restart tool's confirmation check, and cancelling the /bg coroutine
only cancelled the asyncio view of its executor work — the worker thread
kept running while every count read zero. These regressions pin all three
sides: a pending /bg blocks the count, a cancelled /bg with a live worker
stays counted through the deferred-worker registry, and the worker's
delayed completion is what finally unblocks it.
"""

import asyncio
import concurrent.futures
from unittest.mock import MagicMock

import pytest

import plugins.gateway_restart.tool as restart_tool
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


# ── the restart tool's confirmation sees the same accounting ────────────────


def test_pending_bg_keeps_the_exact_word_confirmation():
    """The calling session is the only RUNNING agent, but a pending /bg
    beside it is work the timing gate exists for — skipping the
    confirmation requires provably nothing else in flight."""
    runner, _adapter = make_restart_runner()
    runner._running_agents = {"tg-42": MagicMock()}
    runner._background_tasks.add(_pending_future())

    assert restart_tool._session_is_only_active_work(runner, "tg-42") is False
    assert restart_tool._other_active_work_in_flight(runner, "tg-42") is True


def test_cancelled_bg_with_live_worker_keeps_the_confirmation():
    """The /bg coroutine is gone from ``_background_tasks`` but its executor
    worker is still running — the confirmation must stay."""
    runner, _adapter = make_restart_runner()
    runner._running_agents = {"tg-42": MagicMock()}
    worker = concurrent.futures.Future()
    assert worker.set_running_or_notify_cancel() is True
    runner._track_deferred_agent_worker(worker, MagicMock())

    assert restart_tool._session_is_only_active_work(runner, "tg-42") is False
    assert restart_tool._other_active_work_in_flight(runner, "tg-42") is True


def test_bg_delayed_completion_restores_the_only_session_skip():
    runner, _adapter = make_restart_runner()
    runner._running_agents = {"tg-42": MagicMock()}
    runner._background_tasks.add(_pending_future())
    assert restart_tool._session_is_only_active_work(runner, "tg-42") is False

    # The background task finishes — now the requester really is alone and
    # the ping has nothing to time the bounce around.
    runner._background_tasks.clear()
    assert restart_tool._session_is_only_active_work(runner, "tg-42") is True
    assert restart_tool._other_active_work_in_flight(runner, "tg-42") is False
