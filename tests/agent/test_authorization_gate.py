"""Concurrent tool-authorization locking and sudo human-wait accounting.

Plugin callbacks may wedge, so lock acquisition is bounded and gate residency
does not extend a batch deadline. Actual interactive sudo waits do extend it,
with overlapping windows coalesced and wedged windows clamped.
"""

import threading
import time

import pytest

from agent.tool_executor import _ConcurrentToolAuthorizationGate
from tools import sudo_wait


@pytest.fixture(autouse=True)
def _clean_human_wait_state():
    with sudo_wait._human_wait_lock:
        sudo_wait._human_wait_states.clear()
    yield
    with sudo_wait._human_wait_lock:
        sudo_wait._human_wait_states.clear()


SESSION = "test-session-79719"


def _make_gate(**kwargs) -> _ConcurrentToolAuthorizationGate:
    # Pin the session key so contextvar/env noise from other tests can't
    # change which wait state the gate reads.
    return _ConcurrentToolAuthorizationGate(session_key=SESSION, **kwargs)


class TestHumanWaitTracker:
    def test_no_wait_reports_zero(self):
        assert sudo_wait.human_wait_seconds(SESSION) == 0.0

    def test_open_window_counts(self):
        opened = threading.Event()
        release = threading.Event()

        def _wait():
            with sudo_wait.human_wait_window(SESSION):
                opened.set()
                release.wait(timeout=5)

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        assert opened.wait(timeout=5)
        time.sleep(0.05)
        assert sudo_wait.human_wait_seconds(SESSION) > 0.0
        release.set()
        t.join(timeout=5)
        # Window closed: total is frozen (completed_seconds), not still growing.
        first = sudo_wait.human_wait_seconds(SESSION)
        time.sleep(0.05)
        assert sudo_wait.human_wait_seconds(SESSION) == pytest.approx(first)

    def test_overlapping_windows_coalesce(self):
        """Two concurrent windows on one session must not double-count wall clock."""
        release = threading.Event()
        started = threading.Barrier(3)

        def _wait():
            with sudo_wait.human_wait_window(SESSION):
                started.wait(timeout=5)
                release.wait(timeout=5)

        threads = [threading.Thread(target=_wait, daemon=True) for _ in range(2)]
        start = time.monotonic()
        for t in threads:
            t.start()
        started.wait(timeout=5)
        time.sleep(0.1)
        release.set()
        for t in threads:
            t.join(timeout=5)
        elapsed = time.monotonic() - start
        # Coalesced: recorded ≤ wall clock (a double count would be ~2×).
        assert sudo_wait.human_wait_seconds(SESSION) <= elapsed + 0.05

    def test_sessions_are_isolated(self):
        with sudo_wait.human_wait_window("other-session"):
            time.sleep(0.05)
            assert sudo_wait.human_wait_seconds(SESSION) == 0.0
        assert sudo_wait.human_wait_seconds("other-session") > 0.0

    def test_open_window_clamped_to_human_wait_ceiling(self):
        """A window that overstays the clamp ceiling is itself wedged and must
        stop extending the exclusion (belt-and-braces for #79719)."""
        with sudo_wait.human_wait_window(SESSION):
            state = sudo_wait._human_wait_states[SESSION]
            # Simulate a window that has been open for a full day.
            state.window_started = time.monotonic() - 86_400.0
            assert sudo_wait.human_wait_seconds(SESSION) <= 300.0 + 60.0

    def test_eviction_keeps_pending_sessions(self):
        with sudo_wait.human_wait_window(SESSION):
            for i in range(sudo_wait._HUMAN_WAIT_MAX_SESSIONS + 8):
                with sudo_wait.human_wait_window(f"burst-{i}"):
                    pass
            # The active session survived the eviction pressure and the table
            # stayed at (or under) its cap.
            assert SESSION in sudo_wait._human_wait_states
            assert sudo_wait._human_wait_states[SESSION].pending == 1
            assert (
                len(sudo_wait._human_wait_states)
                <= sudo_wait._HUMAN_WAIT_MAX_SESSIONS
            )

    def test_late_close_of_wedged_window_is_clamped(self):
        """A wedged window that eventually CLOSES must not retroactively inject
        its full overstay into completed_seconds (close-side clamp)."""
        with sudo_wait.human_wait_window(SESSION):
            state = sudo_wait._human_wait_states[SESSION]
            # Simulate the window having been open for a full day before close.
            state.window_started = time.monotonic() - 86_400.0
        assert sudo_wait.human_wait_seconds(SESSION) <= 300.0 + 60.0


class TestAuthorizationGate:
    def test_serializes_callbacks(self):
        gate = _make_gate()
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def _callback():
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
            finally:
                with state_lock:
                    active -= 1

        threads = [
            threading.Thread(target=lambda: gate.run(_callback), daemon=True)
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert max_active == 1

    def test_lock_timeout_degrades_to_unserialized(self):
        """A wedged lock holder must not park later callers forever."""
        gate = _make_gate(lock_timeout=0.1)
        holder_in = threading.Event()
        release = threading.Event()

        def _wedged():
            holder_in.set()
            release.wait(timeout=10)

        holder = threading.Thread(target=lambda: gate.run(_wedged), daemon=True)
        holder.start()
        assert holder_in.wait(timeout=5)

        done = threading.Event()
        result = {}

        def _second():
            result["value"] = gate.run(lambda: "ran-unserialized")
            done.set()

        t = threading.Thread(target=_second, daemon=True)
        start = time.monotonic()
        t.start()
        assert done.wait(timeout=5), "second caller starved behind wedged holder"
        assert result["value"] == "ran-unserialized"
        assert time.monotonic() - start < 2.0
        release.set()
        holder.join(timeout=5)

    def test_wedged_callback_contributes_nothing_to_exclusion(self):
        """THE #79719 regression: gate residency is not deadline exclusion."""
        gate = _make_gate()
        wedged_in = threading.Event()
        release = threading.Event()

        def _wedged():
            wedged_in.set()
            release.wait(timeout=10)

        t = threading.Thread(target=lambda: gate.run(_wedged), daemon=True)
        t.start()
        assert wedged_in.wait(timeout=5)
        time.sleep(0.15)
        # No human prompt is pending — the wedge is invisible to the deadline.
        assert gate.excluded_seconds() == 0.0
        release.set()
        t.join(timeout=5)

    def test_deadline_arithmetic_converges_with_wedged_worker(self):
        """The issue's repro: remaining must DECREASE while a worker is wedged.

        Pre-fix, ``remaining = deadline - window_started`` was constant for
        the life of the wedge (24h simulated in the issue). Now the exclusion
        stays 0 for a wedge, so remaining tracks wall clock down to zero.
        """
        gate = _make_gate()
        wedged_in = threading.Event()
        release = threading.Event()

        def _wedged():
            wedged_in.set()
            release.wait(timeout=10)

        t = threading.Thread(target=lambda: gate.run(_wedged), daemon=True)
        t.start()
        assert wedged_in.wait(timeout=5)

        timeout_s = 0.3
        deadline = time.monotonic() + timeout_s
        first = deadline + gate.excluded_seconds() - time.monotonic()
        time.sleep(0.15)
        second = deadline + gate.excluded_seconds() - time.monotonic()
        assert second < first, "remaining is constant — deadline never fires (#79719)"
        time.sleep(0.25)
        assert deadline + gate.excluded_seconds() - time.monotonic() <= 0, (
            "deadline never became due despite the wedge"
        )
        release.set()
        t.join(timeout=5)

    def test_human_wait_is_excluded(self):
        """A genuine sudo wait during the batch extends the deadline."""
        gate = _make_gate()
        with sudo_wait.human_wait_window(SESSION):
            time.sleep(0.1)
        assert gate.excluded_seconds() >= 0.09

    def test_baseline_ignores_waits_before_batch(self):
        """Sudo waits from before this batch must not extend its deadline."""
        with sudo_wait.human_wait_window(SESSION):
            time.sleep(0.1)
        gate = _make_gate()
        assert gate.excluded_seconds() == 0.0

    def test_other_sessions_wait_not_excluded(self):
        gate = _make_gate()
        with sudo_wait.human_wait_window("unrelated-session"):
            time.sleep(0.05)
        assert gate.excluded_seconds() == 0.0


