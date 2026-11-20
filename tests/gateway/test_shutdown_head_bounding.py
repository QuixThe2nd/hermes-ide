"""The shutdown head must bound its hub-RPC awaits.

2026-11-20 incident: after "Stopping gateway for restart..." the
predecessor produced no further shutdown-phase logs for 60s. stop()'s
head awaits two hub-RPC-backed calls unbounded — the active-session
notify and the restart channel rename — and the hub deliberately parks
platform REST calls (channel rename/edit) in rate-limit sleeps for
minutes, so the predecessor's shutdown wedged until the watchdog
hard-exited and systemd killed the live successor's cgroup. These tests
wedge exactly those awaits and require shutdown to move on within the
bounded adapter-RPC timeout instead of hanging.
"""

from __future__ import annotations

import asyncio

from unittest.mock import patch

import pytest

from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.restart_test_helpers import make_restart_runner


async def _never_returns(*_args, **_kwargs) -> None:
    """A hub-RPC await that never lands (rate-limit sleep with no floor)."""
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_stop_survives_wedged_shutdown_notify(monkeypatch):
    """A notify that never returns must not hold the whole of stop()."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.5")
    runner, _adapter = make_restart_runner()
    # The helper snaps the real method onto the runner instance, so the
    # wedge replaces the instance attribute (a class-level patch is shadowed).
    with patch.object(runner, "_notify_active_sessions_of_shutdown", _never_returns):
        # On the wedge, an unbounded head never reaches teardown and this
        # wait_for is what times out — the failure shape the incident had.
        await asyncio.wait_for(runner.stop(), timeout=15.0)


@pytest.mark.asyncio
async def test_stop_survives_wedged_channel_rename(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.5")
    runner, _adapter = make_restart_runner()
    with patch("gateway.restart_channel_rename.rename_on_shutdown", _never_returns):
        await asyncio.wait_for(runner.stop(), timeout=15.0)


@pytest.mark.asyncio
async def test_phased_twin_bounds_wedged_notify(monkeypatch):
    """The phased shutdown twin holds the same bound on its notify await."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.5")
    runner, _adapter = make_restart_runner()
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    with patch.object(runner, "_notify_active_sessions_of_shutdown", _never_returns):
        await asyncio.wait_for(runner._stop_begin_teardown(ctx), timeout=15.0)
