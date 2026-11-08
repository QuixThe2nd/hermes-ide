"""Regression tests for overlap-successor shutdown exit codes."""

import asyncio
import json
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.restart import GATEWAY_OVERLAP_RESTART_EXIT_CODE, GATEWAY_SERVICE_RESTART_EXIT_CODE
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture(autouse=True)
def isolated_restart_home(tmp_path, monkeypatch):
    """Keep restart sidecar writes in the per-test home, not the session default."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path
    reset_hermes_home_override(token)


@pytest.fixture(autouse=True)
def successor_process_identity(monkeypatch):
    """Give fake successor PIDs a stable, live process fingerprint by default."""
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: 4242)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)


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


@pytest.mark.asyncio
async def test_pause_for_update_preserves_pre_swap_serial_flow(tmp_path):
    """The pre-update control verb drains without spawning onto the old tree."""
    import gateway.run as gateway_run

    runner = SimpleNamespace(
        request_restart=MagicMock(return_value=True),
        config=SimpleNamespace(multiplex_profiles=False),
    )
    server = await gateway_run._start_gateway_start_control_socket(runner)
    assert server is not None
    try:
        response = await asyncio.get_running_loop().run_in_executor(
            None,
            server.handle_request_line,
            json.dumps({"verb": "pause-for-update", "id": 1}).encode(),
        )
    finally:
        await server.stop()

    assert json.loads(response)["result"]["pausing"] is True
    runner.request_restart.assert_called_once_with(
        detached=False, via_service=True, overlap=False
    )


@pytest.mark.asyncio
async def test_restart_control_requests_the_same_overlap_flow(monkeypatch):
    """The control verb reaches the shared queue and returns main's ACK shape."""
    import gateway.run as gateway_run

    runner = SimpleNamespace(
        request_restart=MagicMock(return_value=True),
        config=SimpleNamespace(multiplex_profiles=False),
    )
    queued = AsyncMock(return_value={
        "status": "restarting", "active_agents": 0, "via_service": False,
    })
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)
    server = await gateway_run._start_gateway_start_control_socket(runner)
    assert server is not None
    try:
        response = await asyncio.get_running_loop().run_in_executor(
            None,
            server.handle_request_line,
            json.dumps({
                "verb": "restart", "id": 1,
                "params": {"restart_platforms": False},
            }).encode(),
        )
    finally:
        await server.stop()

    result = json.loads(response)["result"]
    assert result["queued"] is True
    assert result["status"] == "restarting"
    queued.assert_awaited_once_with(runner, None, None, restart_platforms=False)
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
async def test_sigusr1_requests_the_same_overlap_flow(tmp_path, monkeypatch):
    """ExecReload/SIGUSR1 is another entry to the canonical successor-first flow."""
    import gateway.run as gateway_run
    from gateway.config import GatewayConfig

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.run._start_gateway_claim_pid_file", lambda: True)
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)
    monkeypatch.setattr("gateway.code_skew.record_boot_fingerprint", lambda: None)
    monkeypatch.setattr("gateway.run._run_planned_stop_watcher", lambda *args: None)
    monkeypatch.setattr("gateway.run._ensure_windows_gateway_venv_imports", lambda: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda *args, **kwargs: None)

    async def no_control_socket(_runner):
        return None

    async def no_mcp_discovery(_config):
        return None

    monkeypatch.setattr("gateway.run._start_gateway_start_control_socket", no_control_socket)
    monkeypatch.setattr("gateway.run._discover_gateway_mcp_tools", no_mcp_discovery)

    handlers = {}
    loop = asyncio.get_running_loop()

    def capture_signal_handler(_loop, sig, handler, *args):
        handlers[sig] = (handler, args)

    monkeypatch.setattr(type(loop), "add_signal_handler", capture_signal_handler)
    request_restart = MagicMock(return_value=True)

    class SignalRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self.request_restart = request_restart
            self._gateway_health_export_runtime = None

        async def start(self):
            handler, args = handlers[signal.SIGUSR1]
            handler(*args)
            return False

    monkeypatch.setattr("gateway.run.GatewayRunner", SignalRunner)
    assert await gateway_run.start_gateway(config=GatewayConfig(), verbosity=None) is False


@pytest.mark.asyncio
async def test_service_cgroup_successor_receives_main_pid_at_final_teardown(monkeypatch):
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    adapter._last_overlap_successor_service_managed = True
    monkeypatch.setenv("INVOCATION_ID", "systemd-overlap-restart")
    handover = MagicMock(return_value=True)
    monkeypatch.setattr("gateway.systemd_notify.handover_main_pid", handover, raising=False)

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=False, via_service=True) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    assert runner._exit_code == GATEWAY_OVERLAP_RESTART_EXIT_CODE
    assert runner._overlap_successor_systemd_managed is True
    handover.assert_called_once_with(5150)


@pytest.mark.asyncio
async def test_hub_cgroup_successor_never_receives_systemd_main_pid(monkeypatch):
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    adapter._last_overlap_successor_service_managed = False
    handover = MagicMock(return_value=True)
    monkeypatch.setattr("gateway.systemd_notify.handover_main_pid", handover, raising=False)

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=True, via_service=False) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    handover.assert_not_called()


@pytest.mark.asyncio
async def test_spawned_pid_survives_readiness_failure_without_detached_duplicate(monkeypatch):
    """A post-Popen readiness error must not schedule a second manual gateway."""
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.hub_client import DiscordHubClientAdapter

    client = DiscordHubClientAdapter(PlatformConfig(token="fake-token"))
    runner, adapter = make_restart_runner(adapter=client)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    async def rpc(method, _params):
        assert method == "spawn_gateway"
        return {"pid": 5150}

    adapter._call = rpc
    adapter.wait_for_overlap_successor = AsyncMock(side_effect=RuntimeError("attach lost"))
    watcher = AsyncMock()
    runner._launch_detached_restart_watcher = watcher

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=True, via_service=False) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    assert runner._overlap_restart_successor_started is True
    assert runner._overlap_restart_successor_pid == 5150
    watcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_main_pid_transfer_waits_for_existing_successor(monkeypatch):
    """A transient notify failure must not exit into serial or untracked restart."""
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    adapter._last_overlap_successor_service_managed = True
    monkeypatch.setenv("INVOCATION_ID", "systemd-overlap-restart")
    handover = MagicMock(side_effect=[False, True])
    sleep = MagicMock()
    monkeypatch.setattr("gateway.systemd_notify.handover_main_pid", handover, raising=False)
    monkeypatch.setattr("gateway.overlap_restart.time.sleep", sleep)

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=False, via_service=True) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    assert handover.call_count == 2
    sleep.assert_called_once()
    assert runner._shutdown_watchdog_done.is_set()
    assert runner._exit_code == GATEWAY_OVERLAP_RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_proven_dead_successor_allows_serial_supervisor_replacement(monkeypatch):
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    adapter._last_overlap_successor_service_managed = True
    monkeypatch.setenv("INVOCATION_ID", "systemd-overlap-restart")
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    handover = MagicMock(return_value=True)
    monkeypatch.setattr("gateway.systemd_notify.handover_main_pid", handover, raising=False)

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=False, via_service=True) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    handover.assert_not_called()
    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_reused_successor_identity_allows_serial_supervisor_replacement(monkeypatch):
    runner, adapter = make_restart_runner()
    adapter.supports_overlap_restart = True
    adapter.spawn_overlap_successor = AsyncMock(return_value=5150)
    adapter.wait_for_overlap_successor = AsyncMock()
    adapter.mark_overlap_successor_ready = MagicMock()
    adapter._last_overlap_successor_service_managed = True
    monkeypatch.setenv("INVOCATION_ID", "systemd-overlap-restart")
    starts = iter((1000, 2000))
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: next(starts))
    handover = MagicMock(return_value=True)
    monkeypatch.setattr("gateway.systemd_notify.handover_main_pid", handover, raising=False)

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        assert runner.request_restart(detached=False, via_service=True) is True
        await asyncio.wait_for(runner._restart_task, timeout=5.0)

    handover.assert_not_called()
    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_modular_detached_watcher_skips_existing_overlap_successor():
    from gateway.run_shutdown import GatewayShutdownMixin

    runner, _adapter = make_restart_runner()
    runner._restart_requested = True
    runner._restart_detached = True
    runner._overlap_restart_successor_started = True
    runner._launch_detached_restart_command = AsyncMock()
    runner._finalize_shutdown_agents = AsyncMock()
    runner._cancel_process_completion_batch_tasks = AsyncMock()
    runner._agent_cache_lock = None
    runner._agent_cache = None
    runner.adapters = {}
    runner._profile_adapters = {}

    await GatewayShutdownMixin._stop_finalize_agents_and_adapters(
        runner, SimpleNamespace(active_agents={}, elapsed=lambda: 0.0),
    )

    runner._launch_detached_restart_command.assert_not_awaited()
