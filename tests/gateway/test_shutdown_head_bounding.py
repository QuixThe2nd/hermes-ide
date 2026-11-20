"""The shutdown head must bound its hub-RPC awaits.

2026-11-20 incident: after "Stopping gateway for restart..." the
predecessor produced no further shutdown-phase logs for 60s. stop()'s
head awaits two hub-RPC-backed calls unbounded — the active-session
notify and the restart channel rename — and the hub deliberately parks
platform REST calls (channel rename/edit) in rate-limit sleeps for
minutes, so the predecessor's shutdown wedged until the watchdog
hard-exited and systemd killed the live successor's cgroup. These tests
wedge exactly those awaits — on both the facade's stop() and the phased
shutdown twin — and require shutdown to move on within the bounded
adapter-RPC timeout instead of hanging.
"""

from __future__ import annotations

import asyncio

from unittest.mock import patch

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.restart_test_helpers import make_restart_runner


async def _never_returns(*_args, **_kwargs) -> None:
    """A hub-RPC await that never lands (rate-limit sleep with no floor)."""
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wedge, drive",
    [
        # The helper snaps the real method onto the runner instance, so the
        # notify wedge must replace the instance attribute (a class-level
        # patch is shadowed).
        (lambda r: patch.object(r, "_notify_active_sessions_of_shutdown", _never_returns),
         lambda r: r.stop()),
        (lambda r: patch("gateway.restart_channel_rename.rename_on_shutdown", _never_returns),
         lambda r: r.stop()),
        (lambda r: patch.object(r, "_notify_active_sessions_of_shutdown", _never_returns),
         lambda r: r._stop_begin_teardown(
             GatewayShutdownMixin._StopContext(deferred_count=lambda: 0))),
    ],
    ids=["stop-notify", "stop-rename", "phased-notify"],
)
async def test_stop_head_survives_wedged_hub_rpc(monkeypatch, wedge, drive):
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.5")
    runner, _adapter = make_restart_runner()
    # On the wedge, an unbounded head never reaches teardown and this
    # wait_for is what times out — the failure shape the incident had.
    with wedge(runner):
        await asyncio.wait_for(drive(runner), timeout=15.0)
