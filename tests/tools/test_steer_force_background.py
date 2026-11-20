"""A user steer arriving while the top-level parent is blocked on a SYNCHRONOUS delegation
force-backgrounds the still-running children: every live wait is adopted onto the
async-delegation rail (the worker is NEVER stopped), the blocking wait returns a
force-background entry naming the delegation id, and the worker's completion re-enters
the conversation through the ordinary rail drain — exactly once."""

import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import async_delegation, delegate_tool_forcebg as forcebg
from tools.delegate_tool_child_run import _ChildRun
from tools.process_registry import process_registry


class _SlowChild:
    """Child double whose run_conversation outlives the steer window."""

    provider = "openrouter"
    model = "m/fast"
    session_id = "child-session-1"

    def __init__(self, delay: float, outcome="ok"):
        self.delay = delay
        self.outcome = outcome
        self.closed = False

    def run_conversation(self, user_message=None, **kw):
        time.sleep(self.delay)
        if self.outcome == "boom":
            raise RuntimeError("worker exploded after adoption")
        return {"final_response": "late but done", "completed": True}

    def get_activity_summary(self):
        return {"api_call_count": 2}

    def close(self):
        self.closed = True


class _Parent:
    """Top-level parent double: depth 0, stashes steers like AIAgent.steer."""

    _current_task_id = None
    _delegate_depth = 0

    def __init__(self):
        self.steered = []

    def steer(self, text, *, on_accept=None):
        if not text or not text.strip():
            return False
        if on_accept is not None:
            on_accept("steer")
        self.steered.append(text.strip())
        return True


@pytest.fixture(autouse=True)
def _clean_rail():
    async_delegation._reset_for_tests()
    forcebg._reset_for_tests()
    yield
    async_delegation._reset_for_tests()
    forcebg._reset_for_tests()


def _await_in_thread(child, parent, timeout=None):
    """Run await_child on a worker thread; returns (result holder, thread)."""
    run = _ChildRun(child, parent, 0, "Do slow work", None, None)
    holder = {}

    def _work():
        with patch("tools.delegate_tool._get_child_timeout", return_value=timeout):
            holder["out"] = run.await_child()

    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    return holder, thread


def _wait_registered(parent, timeout=5.0):
    """Block until the parent has at least one registered blocking wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if forcebg.registered_wait_count(parent) > 0:
            return True
        time.sleep(0.05)
    return False


def _drain_one_event(delegation_id, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            evt = process_registry.completion_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if evt.get("type") == "async_delegation" and evt.get("delegation_id") == delegation_id:
            return evt
    pytest.fail(f"no completion event for {delegation_id} within {timeout}s")


def _stop_thread(thread, holder):
    """Unstick a test wait (timeout or adoption) and collect its outcome."""
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "await_child thread never finished"
    return holder.get("out")


def test_steer_releases_blocking_wait_and_adopts_worker():
    """The core contract: steer → wait returns a force-background entry naming the
    delegation id, the worker is NOT stopped, and its result re-enters via the rail."""
    child = _SlowChild(delay=4.0)
    parent = _Parent()
    holder, thread = _await_in_thread(child, parent, timeout=None)
    try:
        assert _wait_registered(parent), "blocking wait never registered"
        t0 = time.monotonic()
        accepted, ids = forcebg.steer_forcing_background(parent, "change of plans")
        assert accepted is True
        assert len(ids) == 1
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "blocking wait did not release after the steer"
        assert time.monotonic() - t0 < 3.0
    finally:
        out = _stop_thread(thread, holder)

    result, failure, close_deferred = out
    assert result is None
    assert failure is not None
    assert failure["status"] == "force_backgrounded"
    assert failure["exit_reason"] == "force_backgrounded"
    assert failure["delegation_id"] == ids[0]
    # The notice tells the model the user moved the child to the background.
    assert "NOT stopped" in failure["error"]
    assert ids[0] in failure["error"]
    # Adoption owns teardown: the deferred-close contract matches the timeout path.
    assert close_deferred is True
    # The worker was never stopped or closed by the release itself.
    assert not getattr(child, "_interrupt_requested", False)
    assert child.closed is False
    # The steer itself was accepted (delivered at the tool boundary).
    assert parent.steered == ["change of plans"]

    # The still-running worker settles later and reports through the rail exactly once.
    evt = _drain_one_event(ids[0])
    assert evt["summary"] == "late but done"


def test_steer_without_registered_waits_stashes_only():
    """No blocking waits (nothing to release): the steer is still accepted, no ids."""
    parent = _Parent()
    accepted, ids = forcebg.steer_forcing_background(parent, "hello")
    assert accepted is True
    assert ids == []
    assert parent.steered == ["hello"]


def test_empty_steer_text_is_refused():
    parent = _Parent()
    accepted, ids = forcebg.steer_forcing_background(parent, "   ")
    assert accepted is False
    assert ids == []
    assert parent.steered == []


def test_config_kill_switch_disables_force_background():
    """delegation.steer_force_background=false restores today's behavior: the wait
    never registers, steers cannot release it, and only the timeout ends it."""
    from tools.delegate_tool_config import _cfg as _real_cfg

    child = _SlowChild(delay=4.0)
    parent = _Parent()
    with patch(
        "tools.delegate_tool_config._cfg",
        lambda: {**dict(_real_cfg()), "steer_force_background": False},
    ):
        holder, thread = _await_in_thread(child, parent, timeout=0.5)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and forcebg.registered_wait_count(parent):
                time.sleep(0.05)
            assert forcebg.registered_wait_count(parent) == 0
            accepted, ids = forcebg.steer_forcing_background(parent, "nudge")
            assert (accepted, ids) == (True, [])
        finally:
            out = _stop_thread(thread, holder)
    _, failure, _ = out
    assert failure["status"] == "timeout"


def test_child_depth_parent_is_not_eligible():
    """Only the top-level parent the user is talking to may be force-backgrounded."""
    child = _SlowChild(delay=4.0)
    parent = _Parent()
    parent._delegate_depth = 1
    holder, thread = _await_in_thread(child, parent, timeout=0.5)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and forcebg.registered_wait_count(parent):
            time.sleep(0.05)
        assert forcebg.registered_wait_count(parent) == 0
    finally:
        out = _stop_thread(thread, holder)
    _, failure, _ = out
    assert failure["status"] == "timeout"


def test_delivery_incapable_session_is_not_eligible():
    """Sessions that cannot receive a late completion (one-shot/cron/stateless) keep
    today's behavior: the wait never registers, the steer waits for the batch."""
    child = _SlowChild(delay=4.0)
    parent = _Parent()
    with patch(
        "tools.async_delegation._omitted_default_delivery_supported",
        return_value=False,
    ):
        holder, thread = _await_in_thread(child, parent, timeout=0.5)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and forcebg.registered_wait_count(parent):
                time.sleep(0.05)
            assert forcebg.registered_wait_count(parent) == 0
        finally:
            out = _stop_thread(thread, holder)
    _, failure, _ = out
    assert failure["status"] == "timeout"


def test_rail_refusal_keeps_the_wait_blocked():
    """When the rail refuses the dispatch (capacity), the wait keeps today's blocking
    semantics and the steer stays stashed for the normal drain."""
    child = _SlowChild(delay=4.0)
    parent = _Parent()
    holder, thread = _await_in_thread(child, parent, timeout=1.5)
    try:
        assert _wait_registered(parent)
        with patch(
            "tools.async_delegation.dispatch_background_delegation",
            return_value={"status": "rejected", "error": "pool full"},
        ):
            accepted, ids = forcebg.steer_forcing_background(parent, "nudge")
        assert accepted is True
        assert ids == []
        # Steer stashed but the wait survives: only the timeout ends it.
        assert parent.steered == ["nudge"]
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and thread.is_alive():
            time.sleep(0.05)
        assert thread.is_alive(), "wait released despite rail refusal"
    finally:
        out = _stop_thread(thread, holder)
    _, failure, _ = out
    assert failure["status"] == "timeout"


def test_concurrent_steers_adopt_one_wait_exactly_once():
    child = _SlowChild(delay=4.0)
    parent = _Parent()
    holder, thread = _await_in_thread(child, parent, timeout=None)
    dispatches = []
    real_dispatch = async_delegation.dispatch_background_delegation

    def _counting_dispatch(*a, **kw):
        dispatches.append(kw.get("delegation_id"))
        return real_dispatch(*a, **kw)

    try:
        assert _wait_registered(parent)
        with patch(
            "tools.async_delegation.dispatch_background_delegation", _counting_dispatch
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(
                    lambda _: forcebg.steer_forcing_background(parent, "steer"),
                    range(2),
                ))
        assert all(accepted for accepted, _ in outcomes)
        ids = [i for _, got in outcomes for i in got]
        # One wait → exactly ONE dispatch; both racing steers learn that same id.
        assert len(set(ids)) == 1, f"one wait adopted exactly once, got {ids}"
        assert len(dispatches) == 1
        thread.join(timeout=3.0)
        assert not thread.is_alive()
    finally:
        out = _stop_thread(thread, holder)
    _, failure, _ = out
    assert failure["delegation_id"] == ids[0]


def test_one_steer_adopts_every_live_child_of_a_batch():
    """A parallel batch's still-running children are ALL adopted by one steer; each
    wait returns its own force-background entry with its own delegation id."""
    children = [_SlowChild(delay=4.0), _SlowChild(delay=4.5)]
    parent = _Parent()
    holders = []
    for idx, child in enumerate(children):
        run = _ChildRun(child, parent, idx, f"Task {idx}", None, None)
        holder = {}

        def _work(run=run, holder=holder):
            with patch("tools.delegate_tool._get_child_timeout", return_value=None):
                holder["out"] = run.await_child()

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        holders.append((holder, t))
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and forcebg.registered_wait_count(parent) < 2:
            time.sleep(0.05)
        assert forcebg.registered_wait_count(parent) == 2
        accepted, ids = forcebg.steer_forcing_background(parent, "all of you, background")
        assert accepted is True
        assert len(ids) == 2
        assert len(set(ids)) == 2
        for holder, t in holders:
            t.join(timeout=3.0)
            assert not t.is_alive()
    finally:
        for _, t in holders:
            t.join(timeout=10.0)
    statuses = [holder["out"][1]["status"] for holder, _ in holders]
    got_ids = {holder["out"][1]["delegation_id"] for holder, _ in holders}
    assert statuses == ["force_backgrounded", "force_backgrounded"]
    assert got_ids == set(ids)


def test_adopt_is_idempotent_across_callers():
    """The adopt primitive decides once per wait: a second caller (the timeout tail
    racing the steer listener) joins the in-flight adoption instead of re-dispatching."""
    parent = _Parent()
    run = SimpleNamespace(parent_agent=parent, _adopt_running_worker=lambda future: "deleg-once")
    settled = threading.Event()
    future = Future()
    wait = forcebg.register_blocking_wait(run, future, settled)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(forcebg.adopt, wait)
            time.sleep(0.05)
            second = pool.submit(forcebg.adopt, wait)
            assert first.result(timeout=5.0) == "deleg-once"
            assert second.result(timeout=5.0) == "deleg-once"
        assert wait.wake.is_set()
        assert settled.is_set()
        assert wait.delegation_id == "deleg-once"
    finally:
        forcebg.unregister_blocking_wait(run, wait)


def test_normal_completion_after_registration_leaves_no_phantom():
    """A wait whose worker settles normally never dispatches to the rail, and the
    registry drains so later steers see nothing to release."""
    child = _SlowChild(delay=0.05)
    parent = _Parent()
    holder, thread = _await_in_thread(child, parent, timeout=None)
    out = _stop_thread(thread, holder)
    result, failure, _ = out
    assert result is not None and failure is None
    assert forcebg.registered_wait_count(parent) == 0
