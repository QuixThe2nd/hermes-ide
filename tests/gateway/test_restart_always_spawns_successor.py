"""Restarts always hand over to the overlap successor.

``gateway.restart_platforms`` was removed: every user-requested restart
(slash ``/restart``, the agent ``restart`` tool, ``hermes gateway restart``)
spawns the overlap successor through the platform-adapter hubs. A stale
``gateway.restart_platforms: false`` left in a user config.yaml is ignored.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import gateway.run as gateway_run
import pytest

from gateway.restart import queue_user_restart
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_restart_spawns_successor_despite_stale_config(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda *a, **k: {"gateway": {"restart_platforms": False}},
    )
    monkeypatch.setattr(
        "gateway.restart_preflight.warn_before_user_restart", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    # The successor watcher is stop()-owned machinery orthogonal to the
    # spawn contract; the drain must not depend on it here.
    monkeypatch.setattr("gateway.overlap_restart.watch_successor", AsyncMock())
    # Neutralize stop()'s browser reaper (mkdirs under the isolated home;
    # orthogonal to this contract and trips the hermetic guard on hosts whose
    # sandbox home sits under the real root).
    monkeypatch.setattr("tools.browser_tool.cleanup_all_browsers", MagicMock())
    monkeypatch.setattr("tools.terminal_tool.cleanup_all_environments", MagicMock())
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=os.getpid())

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        status = await queue_user_restart(runner, make_restart_source(), "m1")
        assert status["status"] == "restarting"
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    adapter.spawn_overlap_successor.assert_awaited_once()
    assert runner._overlap_restart_successor_pid == os.getpid()
