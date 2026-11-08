"""``gateway.restart_platforms`` — bounce platform connections on restart or not.

Contract:
* ``restart_platforms_enabled`` defaults True and accepts bool / string forms;
* ``queue_user_restart(..., restart_platforms=False)`` forwards the flag to
  ``request_restart``;
* ``request_restart`` stores it on the runner (``_restart_platforms``) and the
  overlap spawn honours it;
* ``spawn_successor(restart_platforms=False)`` returns None without touching any
  adapter — platform transports ride the predecessor — while True drives
  adapters that support overlap restart.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.overlap_restart import spawn_successor
from gateway.restart import queue_user_restart, restart_platforms_enabled
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, True),
        ({}, True),
        ({"gateway": {}}, True),
        ({"gateway": {"restart_platforms": True}}, True),
        ({"gateway": {"restart_platforms": False}}, False),
        ({"gateway": {"restart_platforms": "false"}}, False),
        ({"gateway": {"restart_platforms": "off"}}, False),
        ({"gateway": {"restart_platforms": "no"}}, False),
        ({"gateway": {"restart_platforms": "yes"}}, True),
        ({"gateway": {"restart_platforms": "1"}}, True),
        ({"gateway": {"restart_platforms": 0}}, False),
    ],
)
def test_restart_platforms_enabled_forms(config, expected):
    assert restart_platforms_enabled(config) is expected


def test_restart_platforms_default_matches_shipping_default():
    """The runtime default must agree with the shipped config default."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert restart_platforms_enabled(None) is bool(
        DEFAULT_CONFIG["gateway"]["restart_platforms"]
    )


@pytest.fixture
def queueable_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.restart_preflight.warn_before_user_restart", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    runner, adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    return runner, adapter


@pytest.mark.asyncio
async def test_queue_user_restart_forwards_false_platforms(queueable_runner):
    runner, _adapter = queueable_runner
    status = await queue_user_restart(
        runner, make_restart_source(), "m1", restart_platforms=False
    )
    assert status["status"] == "restarting"
    runner.request_restart.assert_called_once_with(
        detached=True, via_service=False, restart_platforms=False
    )


@pytest.mark.asyncio
async def test_queue_user_restart_default_resolves_from_runner_config(
    queueable_runner, monkeypatch
):
    """No explicit flag: the runner's own config decides (the ``--no-platforms``
    default path)."""
    runner, _adapter = queueable_runner
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda *a, **k: {"gateway": {"restart_platforms": False}},
    )
    await queue_user_restart(runner, make_restart_source(), "m1")
    runner.request_restart.assert_called_once_with(
        detached=True, via_service=False, restart_platforms=False
    )


@pytest.mark.asyncio
async def test_request_restart_stores_and_passes_false_platforms(monkeypatch):
    runner, _adapter = make_restart_runner()
    spawn = AsyncMock(return_value=None)
    # request_restart imports spawn_successor from gateway.overlap_restart
    # inside its restart task — the defining module is the patch seam.
    monkeypatch.setattr("gateway.overlap_restart.spawn_successor", spawn)
    # Neutralize stop()'s browser reaper (mkdirs under the isolated home;
    # orthogonal to this contract and trips the hermetic guard on hosts whose
    # sandbox home sits under the real root).
    monkeypatch.setattr("tools.browser_tool.cleanup_all_browsers", MagicMock())
    monkeypatch.setattr("tools.terminal_tool.cleanup_all_environments", MagicMock())
    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        assert runner.request_restart(
            detached=True, via_service=False, restart_platforms=False
        )
        # Stored synchronously, before any drain work — the spawn reads it back.
        assert runner._restart_platforms is False
        await asyncio.wait_for(runner._restart_task, timeout=5.0)
    spawn.assert_awaited_once()
    assert spawn.await_args.kwargs.get("restart_platforms") is False


@pytest.mark.asyncio
async def test_spawn_successor_false_never_touches_adapters():
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=4242)

    assert await spawn_successor(runner, via_service=False, restart_platforms=False) is None
    adapter.spawn_overlap_successor.assert_not_awaited()
    assert getattr(runner, "_overlap_restart_successor_started", False) is not True


@pytest.mark.asyncio
async def test_spawn_successor_true_drives_overlap_adapters():
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=4242)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()

    pid = await spawn_successor(runner, via_service=True, restart_platforms=True)
    assert pid == 4242
    adapter.spawn_overlap_successor.assert_awaited_once_with(
        via_service=True, predecessor_pid=os.getpid()
    )
    assert runner._overlap_restart_successor_started is True
    assert runner._overlap_restart_successor_pid == 4242
    adapter.mark_overlap_successor_ready.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_successor_idempotent_once_started():
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=111)
    first = await spawn_successor(runner, via_service=False, restart_platforms=True)
    second = await spawn_successor(runner, via_service=False, restart_platforms=True)
    assert first == second == 111
    adapter.spawn_overlap_successor.assert_awaited_once()
