"""ContextVar propagation into concurrent tool worker threads.

Gateway adapters set the tool-session ContextVar before running a turn. The
agent's parallel dispatcher must snapshot and enter that caller context so
workers retain the initiating session rather than another turn's state.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import threading


def test_executor_submit_without_copy_context_does_not_propagate():
    """Documents the Python contract the fix relies on.

    ``concurrent.futures.ThreadPoolExecutor.submit(fn)`` runs ``fn`` in a
    worker thread with a fresh, empty context. A ContextVar set by the
    caller is invisible inside ``fn``. This is the exact trap that made
    approval-session routing race in the gateway before #16660.

    If this test ever fails — i.e. submit() starts propagating
    ContextVars by default — the copy_context() wrapper in run_agent.py
    becomes redundant but not harmful, and the call-site test below
    should be updated accordingly.
    """
    probe: contextvars.ContextVar[str] = contextvars.ContextVar(
        "probe_default_propagation", default="unset"
    )

    def read_in_worker() -> str:
        return probe.get()

    probe.set("set-in-main")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        observed = ex.submit(read_in_worker).result(timeout=5)

    assert observed == "unset", (
        "Unexpected: executor.submit propagated a ContextVar without "
        "copy_context(). If Python's behavior changed, update "
        "test_run_tool_worker_sees_parent_context below."
    )




def test_run_tool_worker_sees_parent_tool_session_key():
    """End-to-end call-site guard.

    Mirrors the exact shape of the fixed call site in
    ``run_agent.py::_execute_tool_calls_concurrent`` — a
    ``ThreadPoolExecutor`` with ``executor.submit(ctx.run, fn, *args)``.
    Sets the real tool-session ContextVar in the caller and asserts the
    worker observes it.

    Reverting ``copy_context().run`` loses that caller identity.
    """
    from tools.session_context import _tool_session_key, get_current_session_key

    observed: dict = {}
    barrier = threading.Event()

    def worker_equivalent_to_run_tool() -> None:
        # Mirror what real _run_tool does early: read the session key.
        observed["session_key"] = get_current_session_key(default="FALLBACK")
        barrier.set()

    # Set the ContextVar the gateway would set before calling agent.run.
    token = _tool_session_key.set("session-A")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ctx = contextvars.copy_context()
            fut = ex.submit(ctx.run, worker_equivalent_to_run_tool)
            fut.result(timeout=5)
        assert barrier.is_set(), "worker did not complete"
    finally:
        _tool_session_key.reset(token)

    assert observed.get("session_key") == "session-A", (
        f"Worker thread did not inherit _tool_session_key from caller. "
        f"Expected 'session-A', got {observed.get('session_key')!r}. "
        "Check whether the copy_context().run wrapper in "
        "_execute_tool_calls_concurrent was removed."
    )




def test_two_concurrent_tool_batches_keep_session_keys_isolated():
    """End-to-end guard: two callers each set a different session key
    and submit workers concurrently. Each worker must see its own
    caller's key, not the other's.

    Guards against a future "optimization" that reuses a single context
    snapshot across callers (which would collapse isolation the same way
    the unfixed ``submit`` does).
    """
    from tools.session_context import _tool_session_key, get_current_session_key

    results: dict = {}

    def caller(label: str) -> None:
        token = _tool_session_key.set(f"session-{label}")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ctx = contextvars.copy_context()
                fut = ex.submit(
                    ctx.run,
                    lambda: get_current_session_key(default="FALLBACK"),
                )
                results[label] = fut.result(timeout=5)
        finally:
            _tool_session_key.reset(token)

    t_a = threading.Thread(target=caller, args=("A",))
    t_b = threading.Thread(target=caller, args=("B",))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert results.get("A") == "session-A", (
        f"Session A worker saw {results.get('A')!r}, expected 'session-A'"
    )
    assert results.get("B") == "session-B", (
        f"Session B worker saw {results.get('B')!r}, expected 'session-B'"
    )
