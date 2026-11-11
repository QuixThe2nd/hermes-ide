"""Behavior contracts for cua-driver 0.10 permission-mode integration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from tools.computer_use import cua_backend_driver


@pytest.fixture(autouse=True)
def _reset_computer_use_state():
    from tools.computer_use.tool import reset_backend_for_tests

    reset_backend_for_tests()
    yield
    reset_backend_for_tests()


def test_normal_hermes_session_maps_to_standard_mode():
    from tools.computer_use import tool as computer_use

    # No bypass surface remains; the mode is always the configured one (standard here).
    assert computer_use._cua_permission_mode("session-a") == "standard"


def test_release_seam_stops_backend_and_clears_session_state():
    from tools.computer_use import tool as computer_use

    backend = Mock()
    computer_use._backends["session-a"] = backend
    computer_use._backend_call_locks["session-a"] = computer_use.threading.RLock()
    computer_use._backend_permission_modes["session-a"] = "unrestricted"

    assert computer_use.release_computer_use_session("session-a") is True
    assert computer_use.release_computer_use_session("session-a") is False
    backend.stop.assert_called_once_with()
    assert "session-a" not in computer_use._backend_permission_modes


def test_unrestricted_embedded_daemon_uses_private_socket_and_two_part_ack():
    from tools.computer_use import cua_backend

    process = Mock()
    process.poll.return_value = None
    process.stderr = []
    process.wait.return_value = 0
    status = SimpleNamespace(returncode=0, stdout="running", stderr="")
    stopped = SimpleNamespace(returncode=0, stdout="", stderr="")

    daemon = cua_backend._EmbeddedCuaDaemon("cua-driver", "unrestricted")
    with patch.object(cua_backend.sys, "platform", "linux"), patch.object(
        cua_backend_driver,
        "_resolve_mcp_invocation",
        return_value=("/opt/cua-driver", ["mcp"]),
    ), patch.object(
        # This test pins the socket/ack contract, not overlay policy. Pin the
        # policy off so the environment-dependent auto-detect (headless CI vs
        # Wayland dev box) can't add a `--help` capability-probe subprocess.run
        # call that the fixed two-entry side_effect below doesn't budget for.
        cua_backend, "_cua_no_overlay", return_value=False,
    ), patch.object(cua_backend.subprocess, "Popen", return_value=process) as popen, patch.object(
        cua_backend.subprocess, "run", side_effect=[status, stopped]
    ):
        daemon.start()
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        proxy_command, proxy_args = daemon.proxy_invocation()
        daemon.stop()

    assert command[:2] == ["/opt/cua-driver", "serve"]
    assert "--embedded" in command
    assert command[command.index("--permission-mode") + 1] == "unrestricted"
    assert "--dangerously-bypass-approvals" in command
    assert env["CUA_DRIVER_PERMISSION_MODE"] == "unrestricted"
    assert env["CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS"] == "1"
    assert proxy_command == "/opt/cua-driver"
    assert proxy_args == ["mcp", "--embedded", "--socket", daemon.socket_path]


def test_standard_backend_does_not_spawn_an_embedded_daemon():
    from tools.computer_use.cua_backend import CuaDriverBackend

    standard = CuaDriverBackend(permission_mode="standard")
    unrestricted = CuaDriverBackend(permission_mode="unrestricted")

    assert standard._embedded_daemon is None
    assert unrestricted._embedded_daemon is not None


def test_retired_browser_grant_cannot_change_standard_runtime(tmp_path, monkeypatch):
    from tools.computer_use.cua_backend_session import _AsyncBridge, _CuaDriverSession

    (tmp_path / "config.yaml").write_text(
        "computer_use:\n  grant_existing_profile: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = _CuaDriverSession(_AsyncBridge())
    captured = {}

    async def drive_lifecycle():
        def capture_params(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch(
            "tools.computer_use.cua_backend_driver.resolve_cua_driver_cmd",
            return_value="/opt/cua-driver",
        ), patch(
            "tools.computer_use.cua_backend_driver._resolve_mcp_invocation",
            return_value=("/opt/cua-driver", ["mcp"]),
        ), patch(
            "mcp.StdioServerParameters", side_effect=capture_params
        ), patch(
            "mcp.client.stdio.stdio_client"
        ) as stdio_client, patch(
            "mcp.ClientSession"
        ) as client_session:
            stdio_client.return_value.__aenter__ = AsyncMock(
                return_value=(MagicMock(), MagicMock())
            )
            stdio_client.return_value.__aexit__ = AsyncMock(return_value=None)
            live_session = MagicMock()
            live_session.initialize = AsyncMock()
            live_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
            client_session.return_value.__aenter__ = AsyncMock(
                return_value=live_session
            )
            client_session.return_value.__aexit__ = AsyncMock(return_value=None)

            async def stop_when_ready():
                while session._shutdown_event is None:
                    await asyncio.sleep(0)
                session._shutdown_event.set()

            stop_task = asyncio.create_task(stop_when_ready())
            try:
                await session._lifecycle_coro()
            finally:
                await stop_task

    asyncio.run(drive_lifecycle())

    assert captured["command"] == "/opt/cua-driver"
    assert captured["args"] == ["mcp"]


def test_transport_reset_invalidates_native_capabilities():
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend(permission_mode="standard")
    backend._active_pid = 10
    backend._active_window_id = 20
    backend._snapshot_tokens = {1: "old-token"}

    backend._handle_transport_reset()

    assert backend._active_pid is None
    assert backend._active_window_id is None
    assert backend._snapshot_tokens == {}


# ── the escalation is at least audible ──────────────────────────────────


