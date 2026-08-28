"""Tests for the agent-callable ``restart`` tool (plugins/gateway_restart).

The tool must be the ``/restart`` slash command's exact twin: same
``.restart_notify.json`` payload, same supervisor/container routing, same
``request_restart(...)`` drain call. Never a second drain, never the blocked
shell/systemctl path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import MessageEvent, MessageType
from gateway.restart import EXTERNAL_GATEWAY_SUPERVISOR_ENV
from gateway.session_context import clear_session_vars, set_session_vars
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

# The block message the terminal/execute_code guards must point at the tool
# with (same drain path as /restart) instead of only "use another shell".
_RESTART_TOOL_MENTION = "`restart` tool"


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """HERMES_HOME + the module-level marker dir isolated to tmp."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    yield hermes_home


@pytest.fixture(autouse=True)
def _unsupervised_env(monkeypatch):
    """Neutral supervisor/container detection (mirrors test_restart_service_detection).

    On a containerized CI runner ``/.dockerenv`` exists and would route every
    case via_service=True regardless of the markers under test.
    """
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: False)


@pytest.fixture(autouse=True)
def _restore_plugin_modules():
    """Drop gateway_restart/plugin-manager modules between tests (quota pattern)."""
    prefixes = ("plugins.gateway_restart", "hermes_cli.plugins")
    saved = {k: m for k, m in sys.modules.items() if k.startswith(prefixes)}
    yield
    for key in list(sys.modules):
        if key.startswith(prefixes):
            del sys.modules[key]
    sys.modules.update(saved)
    for key, mod in saved.items():
        if "." in key:
            parent_name, attr = key.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr, mod)


class _GatewayLoop:
    """A live event loop on a background thread, standing in for the gateway loop.

    Tool handlers run in a worker thread and must hop onto the gateway event
    loop (request_restart calls asyncio.create_task), so the tests exercise
    the real threadsafe hop rather than calling begin_user_restart inline.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest.fixture
def gateway_loop():
    holder = _GatewayLoop()
    yield holder.loop
    holder.close()


def _live_runner(monkeypatch, gateway_loop, **overrides):
    """A restart runner installed on the live-runner weakref."""
    runner, _adapter = make_restart_runner()
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    for key, value in overrides.items():
        setattr(runner, key, value)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    return runner


def _bind_session(**kwargs):
    set_session_vars(**kwargs)


# ── registration ────────────────────────────────────────────────────────────


def test_register_exposes_restart_tool_on_gateway_toolset():
    from plugins.gateway_restart import register

    registered = {}

    class _Ctx:
        def register_tool(self, **kwargs):
            registered.update(kwargs)

    register(_Ctx())

    assert registered["name"] == "restart"
    assert registered["toolset"] == "gateway"
    assert callable(registered["handler"])
    assert callable(registered["check_fn"])
    assert registered["emoji"] == "♻️"
    schema = registered["schema"]
    assert schema["name"] == "restart"
    # /restart takes no arguments — this is not a new API surface.
    assert schema["parameters"].get("properties") in (None, {})
    assert schema["parameters"]["additionalProperties"] is False


def test_plugin_discovery_loads_tool_on_gateway_toolset(_isolate_hermes_home):
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    mgr = PluginManager()
    mgr.discover_and_load(force=True)

    loaded = mgr._plugins.get("gateway_restart")
    assert loaded is not None, "gateway_restart plugin was not discovered"
    assert loaded.error is None, f"gateway_restart failed to load: {loaded.error}"
    assert loaded.enabled is True
    assert loaded.manifest.default_enabled is True
    assert loaded.manifest.kind == "backend"
    assert "restart" in loaded.tools_registered

    entry = registry.get_entry("restart")
    assert entry is not None
    assert entry.toolset == "gateway"
    assert callable(entry.check_fn)


# ── check_fn ────────────────────────────────────────────────────────────────


def test_check_fn_true_only_with_live_runner(monkeypatch):
    from plugins.gateway_restart.tool import check_restart_requirements

    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    assert check_restart_requirements() is False

    runner, _adapter = make_restart_runner()
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    assert check_restart_requirements() is True


# ── handler contract ────────────────────────────────────────────────────────


def test_handler_errors_without_live_runner():
    from plugins.gateway_restart.tool import handle_restart

    result = json.loads(handle_restart({}))
    assert result["success"] is False
    assert result["error"]


def test_handler_refuses_cron_sessions(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)

    _bind_session(cron_session="1")
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert "cron" in result["error"].lower()
    runner.request_restart.assert_not_called()


def test_handler_uses_service_path_under_supervisor(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    monkeypatch.setenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")

    _bind_session(platform="telegram", chat_id="42", chat_type="dm")
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"
    assert result["via_service"] is True
    assert "active_agents" in result
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_handler_uses_detached_helper_when_unsupervised(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)

    _bind_session(platform="telegram", chat_id="42", chat_type="dm")
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"
    assert result["via_service"] is False
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


def test_handler_uses_service_path_in_container(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: True)

    result = json.loads(handle_restart({}))

    assert result["success"] is True
    assert result["via_service"] is True
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


@pytest.mark.parametrize("flag", ["_restart_requested", "_draining"])
def test_handler_reports_in_progress_without_second_restart(
    gateway_loop, monkeypatch, flag, tmp_path
):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop, **{flag: True})

    result = json.loads(handle_restart({}))

    assert result["success"] is True
    assert result["status"] == "already_in_progress"
    runner.request_restart.assert_not_called()
    # A restart already in flight owns the notify payload — no second write.
    assert not (tmp_path / ".restart_notify.json").exists()


def test_handler_persists_restart_notify_from_session_context(
    gateway_loop, monkeypatch, tmp_path
):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)

    _bind_session(
        platform="telegram",
        chat_id="99",
        chat_type="group",
        thread_id="topic-7",
        user_id="u1",
        message_id="m5",
    )
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True

    notify_path = tmp_path / ".restart_notify.json"
    assert notify_path.exists()
    data = json.loads(notify_path.read_text(encoding="utf-8"))
    assert data["platform"] == "telegram"
    assert data["chat_id"] == "99"
    assert data["chat_type"] == "group"
    assert data["thread_id"] == "topic-7"
    assert data["message_id"] == "m5"

    # The requester's source is kept for the shutdown-warning routing, same
    # as /restart.
    source = runner._restart_command_source
    assert source is not None
    assert source.chat_id == "99"
    assert source.message_id == "m5"

    # The tool is not a Telegram update — the redelivery dedup marker stays
    # a /restart-slash-path-only write.
    assert not (tmp_path / ".restart_last_processed.json").exists()


def test_handler_restarts_without_a_messaging_session(gateway_loop, monkeypatch):
    """No platform/chat bound (e.g. an api_server turn) — restart still works."""
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)

    result = json.loads(handle_restart({}))

    assert result["success"] is True
    assert result["status"] == "restarting"
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)
    assert runner._restart_command_source is None


def test_handler_survives_runner_without_a_loop(monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner, _adapter = make_restart_runner()  # no _gateway_loop attribute set
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert result["error"]
    runner.request_restart.assert_not_called()


# ── /restart keeps using the shared path ────────────────────────────────────


@pytest.mark.asyncio
async def test_slash_restart_delegates_to_begin_user_restart():
    """/restart and the tool share one entry point — the slash handler delegates."""
    runner, _adapter = make_restart_runner()
    runner.begin_user_restart = AsyncMock(
        return_value={"status": "restarting", "active_agents": 0, "via_service": False}
    )

    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="42"),
        message_id="m1",
        platform_update_id=777,
    )

    result = await runner._handle_restart_command(event)

    runner.begin_user_restart.assert_awaited_once_with(
        source=event.source,
        message_id="m1",
        platform_update_id=777,
        write_redelivery_marker=True,
    )
    assert "Restarting" in result


@pytest.mark.asyncio
async def test_slash_restart_surfaces_in_progress_from_shared_helper():
    runner, _adapter = make_restart_runner()
    runner.begin_user_restart = AsyncMock(
        return_value={
            "status": "already_in_progress",
            "active_agents": 2,
            "via_service": None,
        }
    )

    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="42"),
        message_id="m1",
    )

    result = await runner._handle_restart_command(event)

    runner.begin_user_restart.assert_awaited_once()
    assert "2" in result


@pytest.mark.asyncio
async def test_shared_begin_user_restart_routes_and_notifies(tmp_path):
    """The shared helper itself: notify file + routing + single request_restart."""
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    status = await runner.begin_user_restart(
        source=make_restart_source(chat_id="7"), message_id="m9"
    )

    assert status == {
        "status": "restarting",
        "active_agents": 0,
        "via_service": False,
    }
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)

    data = json.loads((tmp_path / ".restart_notify.json").read_text(encoding="utf-8"))
    assert data["platform"] == "telegram"
    assert data["chat_id"] == "7"
    assert data["message_id"] == "m9"
    assert runner._restart_command_source is not None
    # The shared helper without write_redelivery_marker writes no dedup file:
    # the marker is a property of a Telegram update, and this call isn't one.
    assert not (tmp_path / ".restart_last_processed.json").exists()


@pytest.mark.asyncio
async def test_shared_begin_user_restart_writes_redelivery_marker_before_request_restart(
    tmp_path,
):
    """The dedup marker is on disk before request_restart() is invoked.

    request_restart() creates the drain task that can enter stop() as soon
    as the slash handler awaits (a slash command is not an agent turn), so a
    marker written by the caller *after* begin_user_restart may never land —
    re-opening the Telegram redelivery restart loop the marker exists to
    prevent. Spy: by the time request_restart runs, the file must exist.
    """
    runner, _adapter = make_restart_runner()

    def _request_restart_spy(**_kwargs):
        marker_path = tmp_path / ".restart_last_processed.json"
        assert marker_path.exists(), (
            ".restart_last_processed.json must be on disk before request_restart()"
        )
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        assert data["platform"] == "telegram"
        assert data["update_id"] == 4242
        assert isinstance(data["requested_at"], float)
        return True

    runner.request_restart = MagicMock(side_effect=_request_restart_spy)

    status = await runner.begin_user_restart(
        source=make_restart_source(chat_id="42"),
        message_id="m1",
        platform_update_id=4242,
        write_redelivery_marker=True,
    )

    assert status["status"] == "restarting"
    runner.request_restart.assert_called_once()
    # Notify write still precedes the marker: both land before the drain.
    assert (tmp_path / ".restart_notify.json").exists()


# ── the blocked shell paths point at the tool ───────────────────────────────


def test_execute_code_block_message_mentions_restart_tool(monkeypatch):
    import tools.code_execution_tool as cet
    import tools.process_registry as process_registry

    monkeypatch.setattr(cet, "SANDBOX_AVAILABLE", True)
    monkeypatch.setattr(
        process_registry, "_is_supervised_gateway_process", lambda: True
    )

    result = cet.execute_code('import os\nos.system("hermes gateway restart")')

    # Still blocked — the guard is not weakened, only re-pointed.
    assert "Blocked" in result
    assert _RESTART_TOOL_MENTION in result


def test_terminal_block_message_mentions_restart_tool(monkeypatch, tmp_path):
    import tools.terminal_tool as tt
    import tools.process_registry as process_registry

    fake_env = SimpleNamespace(cwd=str(tmp_path))
    monkeypatch.setattr(
        tt,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "docker_image": "",
            "cwd": str(tmp_path),
            "timeout": 30,
            "local_persistent": False,
        },
    )
    monkeypatch.setattr(tt, "_resolve_task_host_cwd", lambda config, task_id: None)
    monkeypatch.setattr(
        process_registry, "_is_supervised_gateway_process", lambda: True
    )
    tt._active_environments["default"] = fake_env
    tt._last_activity["default"] = time.time()
    try:
        result = tt.terminal_tool(command="systemctl restart hermes-gateway")
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "Blocked" in result
    assert _RESTART_TOOL_MENTION in result
    parsed = json.loads(result)
    assert parsed["status"] == "error"
