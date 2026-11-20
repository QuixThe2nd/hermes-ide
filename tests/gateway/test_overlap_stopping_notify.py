"""A draining overlap predecessor must never sd_notify STOPPING=1.

2026-11-21 incident: the predecessor's stop path stopped the systemd
watchdog at the head of teardown, which emitted ``STOPPING=1`` while the
predecessor was still MAINPID — systemd flipped the still-``active
(running)`` unit to ``deactivating (stop-sigterm)`` with no stop job, and
after the overlap successor took MAINPID the unit STAYED deactivating:
``READY=1`` is ignored in stop states, so the 24h10min TimeoutStopSec
timer kept running against the cgroup the live successor serves in. The
notification must therefore be suppressed whenever the overlap evidence
that picks exit 76 says a successor carries the line; a normal stop keeps
notifying exactly as before, and the watchdog ping task stops either way.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

import pytest

import gateway.systemd_notify as systemd_notify
from gateway.run_shutdown import GatewayShutdownMixin


class _StopHeadDouble:
    """Bare double carrying only what the shutdown head reads."""

    def __init__(self, **attrs: Any):
        self._systemd_watchdog = None
        self._restart_requested = False
        self._running = True
        self._draining = False
        self._overlap_restart_successor_started = False
        self._overlap_restart_successor_pid = 0
        self._overlap_restart_successor_start_time = None
        self._overlap_successor_systemd_managed = False
        for key, value in attrs.items():
            setattr(self, key, value)
        self._stop_systemd_watchdog = (
            GatewayShutdownMixin._stop_systemd_watchdog.__get__(self)
        )

    def _clear_plugin_message_injector(self) -> None:
        pass

    async def _cancel_secondary_profile_reconnect_tasks(self) -> None:
        return None

    async def _notify_active_sessions_of_shutdown(self) -> None:
        return None


@pytest.fixture
def notify_sink(monkeypatch) -> list[str]:
    """Capture every sd_notify datagram the gateway would send."""
    sent: list[str] = []
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/hermes-test-stopping-notify")
    monkeypatch.setenv("WATCHDOG_USEC", "20000")
    monkeypatch.setattr(
        systemd_notify, "notify", lambda message: sent.append(message) or True
    )
    return sent


async def _armed_watchdog() -> systemd_notify.SystemdWatchdog:
    watchdog = systemd_notify.SystemdWatchdog(lag_tolerance_seconds=1.0)
    assert watchdog.start() is True
    return watchdog


def _assert_ping_task_stopped(
    sink: list[str], watchdog: systemd_notify.SystemdWatchdog
) -> None:
    assert "WATCHDOG=1" in sink  # the task was feeding systemd before the stop
    assert watchdog.task is None  # ... and the stop still ended it


@pytest.fixture
def live_successor():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield child
    finally:
        child.terminate()
        child.wait(timeout=10)


def _overlap_attrs(child) -> dict[str, Any]:
    from gateway.status import get_process_start_time

    return dict(
        _overlap_restart_successor_started=True,
        _overlap_restart_successor_pid=child.pid,
        _overlap_restart_successor_start_time=get_process_start_time(child.pid),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True], ids=["unmanaged", "systemd"])
async def test_stop_sends_no_stopping_beside_a_live_successor(
    notify_sink, live_successor, managed
):
    """The incident shape: STOPPING=1 from the still-MAINPID predecessor
    deactivates the unit under the successor's cgroup, so both stop twins
    must stay silent — the systemd-managed handover state changes nothing."""
    watchdog = await _armed_watchdog()
    await asyncio.sleep(0.04)
    runner = _StopHeadDouble(
        _systemd_watchdog=watchdog,
        _overlap_successor_systemd_managed=managed,
        **_overlap_attrs(live_successor),
    )

    await GatewayShutdownMixin._stop_systemd_watchdog(runner)

    assert "STOPPING=1" not in notify_sink
    _assert_ping_task_stopped(notify_sink, watchdog)


@pytest.mark.asyncio
async def test_phased_stop_twin_sends_no_stopping_beside_a_live_successor(
    notify_sink, live_successor
):
    """The phased twin (``_stop_begin_teardown``) funnels through the same
    watchdog stop; it must be silent for the same evidence."""
    watchdog = await _armed_watchdog()
    await asyncio.sleep(0.04)
    runner = _StopHeadDouble(_systemd_watchdog=watchdog, **_overlap_attrs(live_successor))

    await GatewayShutdownMixin._stop_begin_teardown(
        runner, GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    )

    assert "STOPPING=1" not in notify_sink
    _assert_ping_task_stopped(notify_sink, watchdog)


@pytest.mark.asyncio
async def test_normal_stop_sends_exactly_one_stopping(notify_sink):
    """No successor carries the line: the ordinary stop notifies systemd
    exactly as before."""
    watchdog = await _armed_watchdog()
    await asyncio.sleep(0.04)
    runner = _StopHeadDouble(_systemd_watchdog=watchdog)

    await GatewayShutdownMixin._stop_systemd_watchdog(runner)

    assert notify_sink.count("STOPPING=1") == 1
    _assert_ping_task_stopped(notify_sink, watchdog)


@pytest.mark.asyncio
async def test_phased_stop_twin_sends_exactly_one_stopping_on_normal_stop(notify_sink):
    watchdog = await _armed_watchdog()
    await asyncio.sleep(0.04)
    runner = _StopHeadDouble(_systemd_watchdog=watchdog)

    await GatewayShutdownMixin._stop_begin_teardown(
        runner, GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    )

    assert notify_sink.count("STOPPING=1") == 1
    _assert_ping_task_stopped(notify_sink, watchdog)


def test_dead_successor_keeps_notifying():
    """A provably dead successor is a serial replacement: the stop path
    notifies systemd exactly as a normal stop would."""
    from gateway.overlap_restart import stopping_notify_permitted

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    runner = _StopHeadDouble(**_overlap_attrs(child))

    assert stopping_notify_permitted(runner) is True


def test_unfingerprintable_successor_stays_silent(live_successor):
    """A successor we cannot fingerprint is never guessed about: silence
    cannot order the unit torn down, STOPPING=1 can."""
    from gateway.overlap_restart import stopping_notify_permitted

    runner = _StopHeadDouble(
        **dict(
            _overlap_attrs(live_successor),
            _overlap_restart_successor_start_time=None,
        )
    )

    assert stopping_notify_permitted(runner) is False
