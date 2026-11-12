"""A blocking delegation wait that times out while the worker is still running adopts the
worker onto the async-delegation rail instead of stopping it: exactly one completion event
(carried by the durable row's delivery claim) wakes the parent when the worker settles —
completed, failed, or stalled after the 30-minute adoption cap."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from tools import async_delegation
from tools.delegate_tool_child_run import _ChildRun
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


class _SlowChild:
    """Child double whose run_conversation outlives the (patched) wait timeout."""

    provider = "openrouter"
    model = "m/fast"
    session_id = "child-session-1"

    def __init__(self, delay: float, outcome="ok"):
        self.delay = delay
        self.outcome = outcome
        self.closed = False
        self.stopped = False

    def run_conversation(self, user_message=None, **kw):
        time.sleep(self.delay)
        if self.outcome == "boom":
            raise RuntimeError("worker exploded after the timeout")
        return {"final_response": "late but done", "completed": True}

    def get_activity_summary(self):
        return {"api_call_count": 2}

    def close(self):
        self.closed = True


class _Parent:
    _current_task_id = None


@pytest.fixture(autouse=True)
def _clean_rail():
    async_delegation._reset_for_tests()
    yield
    async_delegation._reset_for_tests()


def _await_with_timeout(child, timeout=0.2):
    run = _ChildRun(child, _Parent(), 0, "Do slow work", None, None)
    with patch("tools.delegate_tool._get_child_timeout", return_value=timeout):
        result, failure, close_deferred = run.await_child()
    return run, result, failure, close_deferred


def _drain_one_event(delegation_id, timeout=10.0):
    """Pull the terminal event for ``delegation_id`` off the shared completion queue."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            evt = process_registry.completion_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if evt.get("type") == "async_delegation" and evt.get("delegation_id") == delegation_id:
            return evt
    pytest.fail(f"no completion event for {delegation_id} within {timeout}s")


def test_timeout_while_running_adopts_worker_and_delivers_once():
    child = _SlowChild(delay=1.0)
    _, result, failure, _ = _await_with_timeout(child)

    assert result is None and failure is not None
    assert failure["status"] == "timeout"
    error = failure["error"]
    # The immediate tool result names the handle and promises the wake-up.
    assert "still running" in error and "will report back" in error
    delegation_id = failure.get("delegation_id")
    assert delegation_id, f"timeout error must carry the delegation id: {error!r}"
    assert not child.stopped, "an adopted worker must not be stopped by the timeout path"

    evt = _drain_one_event(delegation_id)
    assert evt["delegation_id"] == delegation_id
    assert evt["summary"] == "late but done"
    assert child.closed, "the adopted runner closes the child once its worker settles"
    rendered = format_process_notification(evt)
    assert rendered and "ASYNC DELEGATION COMPLETE" in rendered and delegation_id in rendered

    # The durable row dedups delivery: one claim wins, the second fails.
    assert async_delegation.claim_completion_delivery(delegation_id, "consumer-1") is True
    assert async_delegation.claim_completion_delivery(delegation_id, "consumer-2") is False
    assert async_delegation.complete_completion_delivery(delegation_id, "consumer-1") is True

    # A restart replays nothing for a delivered completion.
    replay = queue.Queue()
    restored = async_delegation.restore_undelivered_completions(replay)
    replayed_ids = set()
    while restored > 0:
        try:
            replayed_ids.add(replay.get_nowait().get("delegation_id"))
        except queue.Empty:
            break
        restored -= 1
    assert delegation_id not in replayed_ids


def test_worker_failure_after_timeout_still_wakes_parent_once():
    child = _SlowChild(delay=0.8, outcome="boom")
    _, _, failure, _ = _await_with_timeout(child)
    delegation_id = failure.get("delegation_id")
    assert delegation_id

    evt = _drain_one_event(delegation_id)
    assert evt["error"] and "worker exploded" in evt["error"]


def test_already_done_future_registers_no_phantom_unit():
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: {"final_response": "already finished", "completed": True})
        future.result(timeout=5)

    run = _ChildRun(_SlowChild(delay=0.0), _Parent(), 0, "Done already", None, None)
    assert run._adopt_running_worker(future) is None
    assert async_delegation.active_count() == 0


def test_second_settle_does_not_enqueue_a_second_event():
    child = _SlowChild(delay=0.8)
    _, _, failure, _ = _await_with_timeout(child)
    delegation_id = failure.get("delegation_id")
    assert delegation_id

    first = _drain_one_event(delegation_id)
    assert first["summary"] == "late but done"
    # Settle the same worker again (a late duplicate settle): the record is
    # terminal, so the chokepoint must refuse a second publication.
    async_delegation._finalize(delegation_id, {"status": "completed", "summary": "dup"}, "completed")
    time.sleep(0.3)
    with pytest.raises(queue.Empty):
        process_registry.completion_queue.get(timeout=0.5)
