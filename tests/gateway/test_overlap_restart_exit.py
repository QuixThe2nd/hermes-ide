"""Regression tests for overlap-successor shutdown exit codes."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.restart import GATEWAY_OVERLAP_RESTART_EXIT_CODE
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_hub_successor_exits_overlap_under_systemd_without_service_request_flag(monkeypatch):
    """A detached request must not make systemd start a third gateway."""
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    monkeypatch.setenv("INVOCATION_ID", "systemd-overlap-restart")

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=True, via_service=False) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    adapter.spawn_overlap_successor.assert_awaited_once_with(
        via_service=False, predecessor_pid=os.getpid()
    )
    assert runner._overlap_restart_successor_started is True
    assert runner._exit_code == GATEWAY_OVERLAP_RESTART_EXIT_CODE
