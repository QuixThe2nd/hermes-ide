"""Tests for the agent-callable ``restart`` tool (plugins/gateway_restart).

The tool is the ``/restart`` slash command's twin: it queues immediately,
persists the same ``.restart_notify.json`` payload, uses the same
supervisor/container routing, and makes the same ``request_restart(...)``
drain call. The blocked shell/systemctl path still points at this tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
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
def _neutral_source_preflight(monkeypatch):
    """Keep these queue tests independent of checkout drift and git subprocesses."""
    async def _no_warning(runner, source):
        return None

    monkeypatch.setattr(
        "gateway.restart.restart_preflight.warn_before_user_restart", _no_warning
    )


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
    the real threadsafe hop rather than calling request_restart inline.
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


def _bind_session(runner, **kwargs):
    """Bind tool context and seed the runner's cached comeback source."""
    set_session_vars(**kwargs)
    source = make_restart_source(
        chat_id=str(kwargs.get("chat_id") or ""),
        chat_type=str(kwargs.get("chat_type") or "dm"),
        thread_id=kwargs.get("thread_id"),
        user_id=kwargs.get("user_id"),
    )
    runner._cache_session_source(kwargs.get("session_key", ""), source)


# A gateway chat session: platform + chat + session key all bound.
_TELEGRAM_SESSION = {
    "platform": "telegram",
    "chat_id": "42",
    "chat_type": "dm",
    "session_key": "tg-42",
}


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


def test_schema_describes_immediate_natural_drain():
    from plugins.gateway_restart.tool import RESTART_SCHEMA

    description = RESTART_SCHEMA["description"]
    assert "queued immediately" in description
    assert "finish naturally" in description
    assert "comeback notice" in description
    # Spell the removed interaction vocabulary without retaining its wording.
    removed_terms = (
        "con" + "sent",
        "exact" + " word",
        "ping" + "ing",
        "can" + "cellation",
    )
    assert all(term not in description.lower() for term in removed_terms)


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

    _bind_session(runner, cron_session="1", **_TELEGRAM_SESSION)
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

    _bind_session(runner, **_TELEGRAM_SESSION)
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

    _bind_session(runner, **_TELEGRAM_SESSION)
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

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["via_service"] is True
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


def test_handler_reports_committed_restart_without_second_restart(
    gateway_loop, monkeypatch, tmp_path
):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop, _restart_requested=True)

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "already_in_progress"
    runner.request_restart.assert_not_called()
    # A restart already in flight owns the notify payload — no second write.
    assert not (tmp_path / ".restart_notify.json").exists()


def test_handler_refuses_generic_shutdown_drain(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop, _draining=True)
    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert "shutdown" in result["error"].lower()
    runner.request_restart.assert_not_called()


def test_handler_persists_restart_notify_from_session_context(
    gateway_loop, monkeypatch, tmp_path
):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)

    _bind_session(
        runner,
        platform="telegram",
        chat_id="99",
        chat_type="group",
        thread_id="topic-7",
        user_id="u1",
        message_id="m5",
        session_key="tg-99",
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


def test_notify_marker_write_is_synchronous_on_the_gateway_loop(
    gateway_loop, monkeypatch
):
    """The queued restart's marker write runs on the gateway loop, not a worker.

    A thread-pool write keeps running after the hand-off future is cancelled
    on timeout, so it can publish ``.restart_notify.json`` for a restart that
    never started — the next gateway boot would read it as a restart that
    succeeded. A write with no await point cannot outlive its hand-off that
    way: it runs inside the coroutine's own step on the gateway loop.
    """
    import gateway.restart as gateway_restart
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    write_loop: dict = {}

    def _sync_only_write(path, data, **kwargs):
        try:
            write_loop["value"] = asyncio.get_running_loop()
        except RuntimeError:
            # A thread-pool worker has no running loop — the phantom-write
            # failure mode this test pins out.
            write_loop["value"] = None

    monkeypatch.setattr(gateway_restart, "atomic_json_write", _sync_only_write)

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert write_loop["value"] is gateway_loop


def test_notify_uses_cached_relay_source(gateway_loop, monkeypatch, tmp_path):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="55",
        chat_type="thread",
        thread_id="999",
        user_id="123456789012345678",
        delivered_via_upstream_relay=True,
    )
    runner._cache_session_source("discord-55", source)
    set_session_vars(
        platform="discord",
        chat_id="55",
        chat_type="thread",
        thread_id="999",
        user_id="123456789012345678",
        session_key="discord-55",
        message_id="m9",
    )
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    notify = json.loads((tmp_path / ".restart_notify.json").read_text())
    assert notify["delivered_via_upstream_relay"] is True
    assert notify["user_id"] == "123456789012345678"
    assert notify["thread_id"] == "999"
    assert notify["message_id"] == "m9"
    assert runner._restart_command_source.delivered_via_upstream_relay is True



# ── loop and runner guards ───────────────────────────────────────────────


def test_handler_survives_runner_without_a_loop(monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner, _adapter = make_restart_runner()  # no _gateway_loop attribute set
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert result["error"]
    runner.request_restart.assert_not_called()


def test_handler_refuses_closed_gateway_loop(monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    loop = asyncio.new_event_loop()
    loop.close()
    runner = _live_runner(monkeypatch, loop)
    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert "event loop" in result["error"]
    runner.request_restart.assert_not_called()


def test_handler_refuses_unsupported_runner(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = SimpleNamespace(_gateway_loop=gateway_loop, request_restart=MagicMock())
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert "restart api" in result["error"].lower()
    runner.request_restart.assert_not_called()


def test_handler_refuses_direct_gateway_loop_invocation(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)

    async def _call_on_gateway_loop():
        return json.loads(handle_restart({}))

    future = asyncio.run_coroutine_threadsafe(_call_on_gateway_loop(), gateway_loop)
    result = future.result(timeout=5)

    assert result["success"] is False
    assert "worker thread" in result["error"]
    runner.request_restart.assert_not_called()



# ── immediate queueing beside other work ────────────────────────────────


def test_other_session_live_queues_the_drain_immediately(gateway_loop, monkeypatch):
    """The drain queues while the other session is still live."""
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(
        monkeypatch,
        gateway_loop,
        _running_agents={"tg-42": object(), "tg-99": object()},
    )
    queued = runner.request_restart

    def _drain_queued(**kwargs):
        # The shared drain owns the wait for the other session: it is queued
        # while that session is still running, not after it clears.
        assert "tg-99" in runner._running_agents
        return queued(**kwargs)

    runner.request_restart = MagicMock(side_effect=_drain_queued)
    slept = MagicMock()
    monkeypatch.setattr(time, "sleep", slept)

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"
    assert result["active_agents"] == 2
    assert result["via_service"] is False
    from agent.turn_control import turn_control_field_for

    expected_fields = {
        "success", "status", "draining", "active_agents", "via_service",
        *turn_control_field_for("restarting"),
    }
    assert set(result) == expected_fields
    runner.request_restart.assert_called_once()
    # No plugin-side idle poll ran before the drain.
    slept.assert_not_called()


@pytest.mark.parametrize("busy", ["cron", "api"])
def test_extra_cron_or_api_work_queues_drain_immediately(
    gateway_loop, monkeypatch, busy
):
    """The calling session is the only chat agent, but background work runs."""
    from gateway.config import Platform
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop, _running_agents={"tg-42": object()})

    if busy == "cron":
        monkeypatch.setattr("cron.scheduler.get_running_job_ids", lambda: {"job-7"})
    else:
        api_adapter = SimpleNamespace(active_agent_work_count=lambda: 1)
        runner.adapters = {**runner.adapters, Platform.API_SERVER: api_adapter}

    def _extra_work_count() -> int:
        if busy == "cron":
            return runner._active_cron_job_count()
        return runner._active_api_run_count()

    queued = runner.request_restart

    def _drain_queued(**kwargs):
        # The background work is still running when the drain is queued —
        # the shared drain owns the wait for it, not the tool.
        assert _extra_work_count() > 0
        return queued(**kwargs)

    runner.request_restart = MagicMock(side_effect=_drain_queued)
    slept = MagicMock()
    monkeypatch.setattr(time, "sleep", slept)

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    runner.request_restart.assert_called_once()
    # The queue hand-off has no plugin-side idle wait beside it.
    slept.assert_not_called()



# ── a failed begin rolls its provisional state back ─────────────────────────


def test_plugin_to_stop_route_spawns_exactly_one_direct_watcher(
    gateway_loop, monkeypatch
):
    """The watcher probe over the real route: handle_restart →
    _queue_user_restart → request_restart(detached=True) → _run_restart →
    stop(). stop() is the watcher's sole owner — the drain loop launches
    nothing before it — so exactly ONE watcher process spawns even when a
    duplicate stop-side launch fires, and its argv is the direct
    ``gateway.run`` bootstrap, never the legacy CLI."""
    import shutil
    import subprocess

    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop, _running_agents={})
    # The REAL request_restart: this is the production route under test.
    runner.request_restart = gateway_run.GatewayRunner.request_restart.__get__(
        runner, gateway_run.GatewayRunner
    )

    real_launch = runner._launch_detached_restart_watcher
    launches = {"before_stop": 0, "calls": 0}
    order: list[str] = []

    async def _recording_launch():
        launches["calls"] += 1
        if "stop" not in order:
            launches["before_stop"] += 1
        await real_launch()

    runner._launch_detached_restart_watcher = _recording_launch

    stop_completed = threading.Event()

    async def _stop(**_kwargs):
        order.append("stop")
        # The real _stop_impl gate: only a detached restart spawns a watcher,
        # and a duplicate stop into the same teardown must stay idempotent —
        # the latch inside the launcher enforces exactly-one.
        if runner._restart_requested and runner._restart_detached:
            await runner._launch_detached_restart_watcher()
            await runner._launch_detached_restart_watcher()
        # Set only after BOTH awaited launches finished: the main thread
        # synchronizes on this event, never on the "stop" marker that was
        # appended before the awaits (polling it raced the second launch).
        stop_completed.set()

    runner.stop = _stop

    popen_calls = []
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **kw: popen_calls.append(cmd)
    )
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"

    # The drain runs on the gateway loop after the tool returned; wait for
    # stop() to have COMPLETED — both watcher-launch awaits included — on the
    # event _stop sets at its end (bounded — a hang here is a failure, not a
    # wait). Polling only for the "stop" marker raced the second launch.
    assert stop_completed.wait(timeout=5.0), "the drain never reached stop()"
    assert "stop" in order

    # One watcher contract: nothing launched before stop(), and the duplicate
    # stop-side launch collapsed into the same single spawn.
    assert launches["before_stop"] == 0
    assert launches["calls"] == 2
    assert len(popen_calls) == 1
    argv = popen_calls[0]
    project_root = str(
        Path(gateway_run.__file__).resolve().parent.parent
    )
    assert argv[0] == gateway_run.sys.executable
    assert argv[1] == "-c"
    assert "run_detached_restart_watcher" in argv[2]
    assert argv[-2:] == ["321", project_root]
    # No legacy CLI / service-manager surface anywhere in the spawn.
    joined = " ".join(argv)
    assert "gateway restart" not in joined
    assert "--replace" not in argv
    assert "systemctl" not in joined and "launchctl" not in joined


def test_plugin_to_stop_route_supervised_spawns_no_watcher(
    gateway_loop, monkeypatch
):
    """The same real route under a supervisor: via_service=True keeps the
    exit-75 service-owned path and starts ZERO watchers."""
    import shutil
    import subprocess

    from gateway.restart import EXTERNAL_GATEWAY_SUPERVISOR_ENV
    from plugins.gateway_restart.tool import handle_restart

    monkeypatch.setenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")

    runner = _live_runner(monkeypatch, gateway_loop, _running_agents={})
    runner.request_restart = gateway_run.GatewayRunner.request_restart.__get__(
        runner, gateway_run.GatewayRunner
    )

    launches = {"n": 0}

    async def _counting_launch():
        launches["n"] += 1
        raise AssertionError("supervised restart must not launch a watcher")

    runner._launch_detached_restart_watcher = _counting_launch

    async def _stop(**_kwargs):
        # Same gate as the unsupervised route test: it reads the REAL flags
        # request_restart set, which must say "service-owned, not detached".
        if runner._restart_requested and runner._restart_detached:
            await runner._launch_detached_restart_watcher()

    runner.stop = _stop

    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)

    _bind_session(runner, **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"
    assert result["via_service"] is True
    assert runner._restart_via_service is True
    assert runner._restart_detached is False

    # Let the drain run to its stop() on the gateway loop, then verify.
    deadline = time.monotonic() + 5.0
    while runner._restart_task and not runner._restart_task.done():
        if time.monotonic() > deadline:
            break
        time.sleep(0.01)
    assert launches["n"] == 0
    popen.assert_not_called()


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
