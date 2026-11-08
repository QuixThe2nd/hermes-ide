"""All user-restart doors drive the ONE shared core.

Contract: the ``/restart`` slash command, the agent-callable ``restart`` tool,
and ``hermes gateway restart`` (control-socket transport) all reach
``gateway.restart.queue_user_restart`` — never a private restart choreography
of their own — so requester routing, preflight, and the drain hand-off cannot
drift apart between entry points.

The SIGUSR1 arm covers the resolution seam the gateway's signal handler
delegates to (``gateway.restart._runner_restart_platforms``): the handler
itself is a closure installed inside ``start_gateway`` and cannot be invoked
outside a real gateway boot, but its entire restart-platforms decision is
``_runner_restart_platforms(runner)``, tested here against the runner's config
loader with a fail-open default.
"""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.event import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)

# ── (a) slash: /restart → queue_user_restart ─────────────────────────────────


@pytest.mark.asyncio
async def test_restart_slash_command_reaches_shared_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    queued = AsyncMock(
        return_value={"status": "restarting", "active_agents": 0, "via_service": False}
    )
    # _handle_restart_command imports queue_user_restart from gateway.restart
    # inside the method, so the defining module attribute is the patch seam.
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)

    runner, _adapter = make_restart_runner()
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=None,
    )

    result = await runner._handle_restart_command(event)

    queued.assert_awaited_once()
    args, kwargs = queued.await_args
    assert args[0] is runner
    assert args[1] is event.source
    assert args[2] == "m1"
    assert "Restarting" in str(result) or result  # reply derived from the status


@pytest.mark.asyncio
async def test_restart_slash_command_reports_chained_on_incoming_generation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    queued = AsyncMock(return_value={
        "status": "chained", "active_agents": 0, "via_service": False,
    })
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._overlap_restart_successor_started = True
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m-chain",
        platform_update_id=None,
    )

    result = await runner._handle_restart_command(event)

    queued.assert_awaited_once()
    assert "chained" in str(result)
    assert "incoming generation" in str(result)


# ── (b) tool: plugins.gateway_restart.tool.handle_restart → queue_user_restart ─


class _LoopThread:
    """A real event loop running in a worker thread, like the gateway loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5.0)
        self.loop.close()


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


def _tool_runner(loop):
    runner, adapter = make_restart_runner()
    runner._gateway_loop = loop
    return runner, adapter


def _run_handle_restart(monkeypatch, runner, queued=None):
    queued = queued or AsyncMock(
        return_value={"status": "restarting", "active_agents": 0, "via_service": False}
    )
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)
    # handle_restart resolves the runner through gateway.run._gateway_runner_ref
    # (imported inside the function) — patch at the defining module.
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    from plugins.gateway_restart.tool import handle_restart

    return handle_restart({}), queued


def test_restart_tool_reaches_shared_queue(monkeypatch, loop_thread):
    runner, _adapter = _tool_runner(loop_thread.loop)
    result_raw, queued = _run_handle_restart(monkeypatch, runner)

    queued.assert_awaited_once()
    args, _kwargs = queued.await_args
    assert args[0] is runner
    payload = json.loads(result_raw)
    assert payload["success"] is True
    assert payload["status"] == "restarting"


# ── (c) CLI: hermes gateway restart --no-platforms → control socket ──────────


def test_cli_restart_leads_with_control_socket_no_platforms(monkeypatch):
    import hermes_cli.gateway as cli_gateway

    requested = MagicMock(
        return_value={"queued": True, "status": "restarting", "active_agents": 0}
    )
    # _restart_via_control_socket imports request_gateway_restart from
    # gateway.control_socket inside the function — patch the defining module.
    monkeypatch.setattr("gateway.control_socket.request_gateway_restart", requested)
    # Neutralize everything that is not the control-socket door.
    monkeypatch.setattr(cli_gateway, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_gateway, "_guard_named_profile_under_multiplexer", lambda **k: None
    )
    monkeypatch.setattr(
        cli_gateway, "_dispatch_all_via_service_manager_if_s6", lambda *a, **k: False
    )
    monkeypatch.setattr(
        cli_gateway, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False
    )
    monkeypatch.setattr(cli_gateway, "_installed_service_kind_for", lambda *a, **k: None)

    args = SimpleNamespace(system=False, all=False, force=False, no_platforms=True)
    cli_gateway._cmd_restart(args)

    requested.assert_called_once()
    kwargs = requested.call_args.kwargs
    assert kwargs.get("restart_platforms") is False


def test_control_socket_transport_passes_flag_through(monkeypatch):
    import hermes_cli.gateway as cli_gateway

    requested = MagicMock(return_value={"queued": True, "status": "restarting"})
    monkeypatch.setattr("gateway.control_socket.request_gateway_restart", requested)

    assert cli_gateway._restart_via_control_socket(False) is True
    requested.assert_called_once()
    assert requested.call_args.kwargs.get("restart_platforms") is False


def test_control_socket_chained_reply_names_incoming_generation(monkeypatch, capsys):
    import hermes_cli.gateway as cli_gateway

    requested = MagicMock(return_value={"queued": True, "status": "chained"})
    monkeypatch.setattr("gateway.control_socket.request_gateway_restart", requested)

    assert cli_gateway._restart_via_control_socket(True) is True
    output = capsys.readouterr().out
    assert "chained" in output
    assert "incoming generation" in output


# ── (d) SIGUSR1: restart_platforms resolved from the runner's config ─────────


def test_runner_restart_platforms_resolution(monkeypatch):
    from gateway.restart import _runner_restart_platforms

    loader = MagicMock(return_value={"gateway": {"restart_platforms": False}})
    monkeypatch.setattr("gateway.run._load_gateway_config", loader)
    assert _runner_restart_platforms(object()) is False

    loader_true = MagicMock(return_value={"gateway": {"restart_platforms": True}})
    monkeypatch.setattr("gateway.run._load_gateway_config", loader_true)
    assert _runner_restart_platforms(object()) is True

    def _boom():
        raise RuntimeError("config unreadable")

    # A config read failure must never block a restart: default True.
    monkeypatch.setattr("gateway.run._load_gateway_config", _boom)
    assert _runner_restart_platforms(object()) is True


@pytest.mark.asyncio
async def test_request_restart_stores_resolved_platforms_for_the_signal_path(monkeypatch):
    """The SIGUSR1 handler hands ``_runner_restart_platforms(runner)`` straight to
    ``request_restart``; the core must persist that decision on the runner so the
    overlap spawn honours it (asserted end-to-end in test_restart_platforms_flag)."""
    from unittest.mock import patch

    runner, _adapter = make_restart_runner()
    spawn = AsyncMock(return_value=None)
    monkeypatch.setattr("gateway.overlap_restart.spawn_successor", spawn)
    # stop()'s browser reaper mkdirs a state dir under the isolated home,
    # which the hermetic guard rejects on hosts whose sandbox home sits under
    # the real root — the reaper is orthogonal to this contract.
    monkeypatch.setattr("tools.browser_tool.cleanup_all_browsers", MagicMock())
    monkeypatch.setattr("tools.terminal_tool.cleanup_all_environments", MagicMock())
    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        assert runner.request_restart(
            detached=False, via_service=True, restart_platforms=False
        )
        assert runner._restart_platforms is False
        await asyncio.wait_for(runner._restart_task, timeout=5.0)
    spawn.assert_awaited_once()
    assert spawn.await_args.kwargs.get("restart_platforms") is False


# The shared adapter double is intentionally unused here but kept imported for
# parity with sibling restart tests that need a fresh adapter instance.
_ = RestartTestAdapter
