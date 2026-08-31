"""Tests for the agent-callable ``restart`` tool (plugins/gateway_restart).

The tool must be the ``/restart`` slash command's exact twin *after* the
requester confirms: same ``.restart_notify.json`` payload, same
supervisor/container routing, same ``request_restart(...)`` drain call. The
confirm gate itself pings the requester and only the exact word ``restart``
unlocks the bounce — never a second drain, never the blocked
shell/systemctl path, and the ``/restart`` slash command stays ungated.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
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


def _mock_confirm(monkeypatch, reply):
    """Stub the confirm gate: record the registration, return ``reply``.

    ``reply`` is what ``wait_for_response`` would hand the tool — the exact
    word, a wrong reply, or ``None`` when no entry exists. Registering is
    stubbed too so mocked waits don't leave real entries armed.
    """
    import tools.clarify_gateway as cg

    register = MagicMock(return_value=None)
    wait = MagicMock(return_value=reply)
    monkeypatch.setattr(cg, "register", register)
    monkeypatch.setattr(cg, "wait_for_response", wait)
    return register, wait


def _assert_unlimited_wait(wait: MagicMock) -> None:
    """``wait_for_response`` must be called with timeout <= 0 (unlimited)."""
    wait.assert_called_once()
    call = wait.call_args
    timeout = call.args[1] if len(call.args) > 1 else call.kwargs.get("timeout")
    assert timeout is not None
    assert float(timeout) <= 0.0


# A confirmable session: platform + chat + session key all bound.
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
    register, _wait = _mock_confirm(monkeypatch, "restart")

    _bind_session(cron_session="1", **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert "cron" in result["error"].lower()
    runner.request_restart.assert_not_called()
    # The cron refuse happens before any ping — no prompt is registered.
    register.assert_not_called()


def test_handler_uses_service_path_under_supervisor(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    register, wait = _mock_confirm(monkeypatch, "restart")
    monkeypatch.setenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"
    assert result["via_service"] is True
    assert "active_agents" in result
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)
    # The bounce only happened because the requester typed the exact word.
    register.assert_called_once()
    wait.assert_called_once()
    _assert_unlimited_wait(wait)


def test_handler_uses_detached_helper_when_unsupervised(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    _register, wait = _mock_confirm(monkeypatch, "restart")

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "restarting"
    assert result["via_service"] is False
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)
    wait.assert_called_once()
    _assert_unlimited_wait(wait)


def test_handler_uses_service_path_in_container(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    _mock_confirm(monkeypatch, "restart")
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: True)

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["via_service"] is True
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


@pytest.mark.parametrize("flag", ["_restart_requested", "_draining"])
def test_handler_reports_in_progress_without_second_restart(
    gateway_loop, monkeypatch, flag, tmp_path
):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop, **{flag: True})
    register, wait = _mock_confirm(monkeypatch, "restart")

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert result["status"] == "already_in_progress"
    runner.request_restart.assert_not_called()
    # Already draining → no ping and no confirm wait.
    register.assert_not_called()
    wait.assert_not_called()
    # A restart already in flight owns the notify payload — no second write.
    assert not (tmp_path / ".restart_notify.json").exists()


def test_handler_persists_restart_notify_from_session_context(
    gateway_loop, monkeypatch, tmp_path
):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    _mock_confirm(monkeypatch, "restart")

    _bind_session(
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


# ── the confirm gate ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply", ["yes", "Restart", "/restart", "restart please", "   ", ""]
)
def test_confirm_rejects_anything_but_the_exact_word(gateway_loop, monkeypatch, reply):
    """`yes`, `Restart`, `/restart`, extra words, whitespace — all cancel."""
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    register, wait = _mock_confirm(monkeypatch, reply)

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert result["error"]
    runner.request_restart.assert_not_called()
    register.assert_called_once()
    wait.assert_called_once()
    _assert_unlimited_wait(wait)


def test_confirm_none_reply_cancels_as_non_matching(gateway_loop, monkeypatch):
    """Missing clarify entry (``None``) cancels like any non-matching reply."""
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    _register, wait = _mock_confirm(monkeypatch, None)

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "not the exact word" in result["error"]
    assert "no confirmation" not in result["error"]
    runner.request_restart.assert_not_called()
    _assert_unlimited_wait(wait)


def test_confirm_prompt_mentions_the_discord_requester(gateway_loop, monkeypatch):
    """Discord prompts start with the requester's snowflake and stay in-thread."""
    from gateway.config import Platform
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    _mock_confirm(monkeypatch, "restart")

    _bind_session(
        platform="discord",
        chat_id="55",
        chat_type="thread",
        thread_id="999",
        user_id="123456789012345678",
        session_key="discord-55",
    )
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True

    chat_id, content, metadata = adapter.sent_calls[0]
    assert chat_id == "55"
    assert content.startswith("<@123456789012345678> ")
    assert "restart" in content
    assert (metadata or {}).get("thread_id") == "999"
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


def test_confirm_prompt_has_no_mention_prefix_off_discord(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    adapter = next(iter(runner.adapters.values()))
    _mock_confirm(monkeypatch, "restart")

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    # Telegram prompts are plain text — the <@id> ping is a Discord device.
    _chat_id, content, _metadata = adapter.sent_calls[0]
    assert not content.startswith("<@")


def test_confirm_send_failure_disarms_the_prompt_and_cancels(
    gateway_loop, monkeypatch
):
    """A failed prompt send cancels the restart and leaves no armed entry."""
    from gateway.platforms.base import SendResult
    from plugins.gateway_restart.tool import handle_restart
    from tools import clarify_gateway as cg

    runner, adapter = make_restart_runner()

    async def _failing_send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="boom")

    adapter.send = _failing_send
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "confirmation prompt" in result["error"]
    runner.request_restart.assert_not_called()
    # The registered entry was reaped, not left armed to eat the next message.
    assert cg.has_pending("tg-42") is False


def test_confirm_refuses_when_no_chat_session_is_bound(gateway_loop, monkeypatch):
    """No platform/chat bound (e.g. an api_server turn) — cannot confirm."""
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    register, _wait = _mock_confirm(monkeypatch, "restart")

    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert "cannot confirm" in result["error"].lower()
    runner.request_restart.assert_not_called()
    register.assert_not_called()
    assert runner._restart_command_source is None


def test_confirm_refuses_without_a_live_adapter_for_the_platform(
    gateway_loop, monkeypatch
):
    from plugins.gateway_restart.tool import handle_restart

    runner, _adapter = make_restart_runner()  # telegram-only adapters map
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    register, _wait = _mock_confirm(monkeypatch, "restart")

    _bind_session(platform="discord", chat_id="55", session_key="discord-55")
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert "no live adapter" in result["error"].lower()
    runner.request_restart.assert_not_called()
    register.assert_not_called()


def test_handler_survives_runner_without_a_loop(monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner, _adapter = make_restart_runner()  # no _gateway_loop attribute set
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    result = json.loads(handle_restart({}))

    assert result["success"] is False
    assert result["error"]
    runner.request_restart.assert_not_called()


# ── the Discord confirmation embed ──────────────────────────────────────────
#
# On Discord the confirm gate renders as ONE dedicated embed owned by the
# adapter's send_restart_confirmation — the plain prompt stays as the
# fallback for adapters without the capability (and for a failed embed
# send), never as a second message beside it.


def _discord_runner(gateway_loop, monkeypatch):
    """A live runner whose DISCORD adapter is the plain RestartTestAdapter."""
    from gateway.config import Platform

    runner, adapter = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    return runner, adapter


def _spy_confirm(monkeypatch, order, reply="restart"):
    """Stub the confirm gate while recording the registration into ``order``."""
    import tools.clarify_gateway as cg

    register = MagicMock(return_value=None)
    wait = MagicMock(return_value=reply)

    def _register_spy(**kwargs):
        order.append("register")
        return register(**kwargs)

    monkeypatch.setattr(cg, "register", _register_spy)
    monkeypatch.setattr(cg, "wait_for_response", wait)
    return register, wait


_DISCORD_SESSION = {
    "platform": "discord",
    "chat_id": "55",
    "chat_type": "thread",
    "thread_id": "999",
    "user_id": "123456789012345678",
    "session_key": "discord-55",
}


def test_discord_embed_is_the_single_confirmation_message(gateway_loop, monkeypatch):
    """Rich path: one embed, no plain prompt, registered before the send."""
    from gateway.platforms.base import SendResult
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order = []
    rich_calls = []

    async def _rich(**kwargs):
        order.append("rich")
        rich_calls.append(kwargs)
        return SendResult(success=True, message_id="e1")

    adapter.send_restart_confirmation = _rich
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    # Registration arms the text-intercept BEFORE any prompt can land.
    assert order == ["register", "rich"]
    assert len(rich_calls) == 1
    assert rich_calls[0]["chat_id"] == "55"
    assert "restart" in rich_calls[0]["prompt"]
    assert rich_calls[0]["requester_user_id"] == "123456789012345678"
    assert rich_calls[0]["metadata"] == {"thread_id": "999"}
    # No plain prompt rode along after the embed.
    assert adapter.sent_calls == []
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


def test_discord_embed_failure_falls_back_to_exactly_one_plain_prompt(
    gateway_loop, monkeypatch
):
    from gateway.platforms.base import SendResult
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order = []

    async def _rich(**kwargs):
        order.append("rich")
        return SendResult(success=False, error="embed blew up")

    adapter.send_restart_confirmation = _rich
    _plain = adapter.send

    async def _plain_spy(chat_id, content, reply_to=None, metadata=None):
        order.append("plain")
        return await _plain(chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter.send = _plain_spy
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    # The fallback prompt delivered, so the confirm gate still ran.
    assert result["success"] is True
    assert order == ["register", "rich", "plain"]
    assert len(adapter.sent_calls) == 1
    chat_id, content, metadata = adapter.sent_calls[0]
    assert chat_id == "55"
    assert content.startswith("<@123456789012345678> ")
    assert "restart" in content
    assert (metadata or {}).get("thread_id") == "999"
    runner.request_restart.assert_called_once()


def test_discord_embed_ambiguous_failure_does_not_duplicate_the_prompt(
    gateway_loop, monkeypatch
):
    """A timeout-class failure may still land the embed — no plain resend.

    Same boundary rule as clarify: only a definitive failure (no message
    created) falls back; an exception from the future is ambiguous.
    """
    from plugins.gateway_restart.tool import handle_restart
    from tools import clarify_gateway as cg

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order = []

    async def _rich(**kwargs):
        order.append("rich")
        raise asyncio.TimeoutError()

    adapter.send_restart_confirmation = _rich
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "confirmation prompt" in result["error"]
    assert order == ["register", "rich"]
    assert adapter.sent_calls == []
    runner.request_restart.assert_not_called()
    # No armed entry left to eat the requester's next message.
    assert cg.has_pending("discord-55") is False


def test_rich_path_is_discord_only(gateway_loop, monkeypatch):
    """A non-Discord adapter never takes the embed path, capability or not."""
    from gateway.platforms.base import SendResult
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    adapter = next(iter(runner.adapters.values()))
    rich_calls = []

    async def _rich(**kwargs):
        rich_calls.append(kwargs)
        return SendResult(success=True, message_id="e1")

    adapter.send_restart_confirmation = _rich
    _mock_confirm(monkeypatch, "restart")

    _bind_session(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert rich_calls == []
    assert len(adapter.sent_calls) == 1
    _chat_id, content, _metadata = adapter.sent_calls[0]
    assert content.startswith("Gateway restart requested")


# ── the temporary Restart Pending thread title ──────────────────────────────
#
# While the confirm gate waits, the calling Discord thread is retitled
# "Restart Pending" and restored to its exact original name on every exit —
# confirmation, cancellation, delivery failure, exception — always before
# the restart itself can be queued. The original name is captured by a
# read-only phase BEFORE the mutating rename runs, so a rename whose
# response is lost after Discord applied it still leaves the tool holding
# what to restore. The capability is optional adapter surface held in
# invocation-scoped state: adapters without it, DMs (no thread), and
# non-Discord platforms are untouched, and a failed rename or restore is
# cosmetic, never a gate on the restart.


class _TitleCapability:
    """A recording stand-in for the adapter's pending-title capability.

    Mirrors the two-phase lifecycle: ``capture`` is the read-only phase
    that hands the tool its restore token, ``begin`` the mutating rename,
    and ``end`` the restore.
    """

    def __init__(
        self,
        order,
        *,
        token="restore-token",
        capture_error=None,
        begin_error=None,
    ):
        self.order = order
        self.token = token
        self.capture_error = capture_error
        self.begin_error = begin_error
        self.capture_calls: list[str] = []
        self.begin_calls: list[object] = []
        self.end_calls: list[object] = []

    async def capture(self, thread_id):
        self.order.append("capture")
        self.capture_calls.append(thread_id)
        if self.capture_error is not None:
            raise self.capture_error
        return self.token

    async def begin(self, restore):
        self.order.append("rename")
        self.begin_calls.append(restore)
        if self.begin_error is not None:
            raise self.begin_error

    async def end(self, restore):
        self.order.append("restore")
        self.end_calls.append(restore)

    def attach(self, adapter):
        adapter.capture_restart_pending_thread_title = self.capture
        adapter.begin_restart_pending_thread_title = self.begin
        adapter.end_restart_pending_thread_title = self.end
        return adapter


def _rich_ok(order):
    from gateway.platforms.base import SendResult

    async def _rich(**kwargs):
        order.append("rich")
        return SendResult(success=True, message_id="e1")

    return _rich


def _request_restart_recording(runner, order):
    """Wrap request_restart so the restart queues visibly last."""
    inner = MagicMock(return_value=True)

    def _spy(**kwargs):
        order.append("request_restart")
        return inner(**kwargs)

    runner.request_restart = MagicMock(side_effect=_spy)
    return runner


def test_thread_renamed_before_the_prompt_and_restored_before_the_restart(
    gateway_loop, monkeypatch
):
    """Retitle lands before the confirmation; restoration beats the queueing."""
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []
    adapter.send_restart_confirmation = _rich_ok(order)
    title = _TitleCapability(order)
    title.attach(adapter)
    _request_restart_recording(runner, order)
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    # Armed, then the original name captured, then the rename, then the
    # prompt, then the reply-resolved restore, and only then the restart.
    assert order == [
        "register",
        "capture",
        "rename",
        "rich",
        "restore",
        "request_restart",
    ]
    # The exact calling thread, and only it — renamed and restored by its
    # own invocation-scoped token.
    assert title.capture_calls == ["999"]
    assert title.begin_calls == [title.token]
    assert title.end_calls == [title.token]


def test_cancelling_reply_restores_the_thread_title(gateway_loop, monkeypatch):
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []
    adapter.send_restart_confirmation = _rich_ok(order)
    title = _TitleCapability(order)
    title.attach(adapter)
    _request_restart_recording(runner, order)
    _spy_confirm(monkeypatch, order, reply="stop")

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert order == ["register", "capture", "rename", "rich", "restore"]
    assert title.end_calls == [title.token]
    runner.request_restart.assert_not_called()


def test_ambiguous_send_error_restores_the_thread_title(gateway_loop, monkeypatch):
    """A raising rich send cancels the restart but still restores the name."""
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []

    async def _rich(**kwargs):
        order.append("rich")
        raise asyncio.TimeoutError("response lost")

    adapter.send_restart_confirmation = _rich
    title = _TitleCapability(order)
    title.attach(adapter)
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "confirmation prompt" in result["error"]
    assert order == ["register", "capture", "rename", "rich", "restore"]
    assert title.end_calls == [title.token]


def test_delivery_failure_restores_the_thread_title(gateway_loop, monkeypatch):
    """Rich failure whose plain fallback also fails: cancelled, still restored."""
    from gateway.platforms.base import SendResult
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []

    async def _rich(**kwargs):
        order.append("rich")
        return SendResult(success=False, error="no embed for you")

    async def _failing_plain(chat_id, content, reply_to=None, metadata=None):
        order.append("plain")
        return SendResult(success=False, error="plain blew up too")

    adapter.send_restart_confirmation = _rich
    adapter.send = _failing_plain
    title = _TitleCapability(order)
    title.attach(adapter)
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "confirmation prompt" in result["error"]
    assert order == ["register", "capture", "rename", "rich", "plain", "restore"]
    assert title.end_calls == [title.token]
    runner.request_restart.assert_not_called()


def test_exception_while_waiting_still_restores_the_thread_title(
    gateway_loop, monkeypatch
):
    """The restore is a finally — it runs even when the wait itself raises."""
    import tools.clarify_gateway as cg
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []
    adapter.send_restart_confirmation = _rich_ok(order)
    title = _TitleCapability(order)
    title.attach(adapter)
    register = MagicMock(return_value=None)
    wait = MagicMock(side_effect=RuntimeError("worker thread died"))
    monkeypatch.setattr(cg, "register", register)
    monkeypatch.setattr(cg, "wait_for_response", wait)

    _bind_session(**_DISCORD_SESSION)
    try:
        with pytest.raises(RuntimeError, match="worker thread died"):
            handle_restart({})
    finally:
        clear_session_vars(None)
        cg.clear_session("discord-55")

    assert order == ["capture", "rename", "rich", "restore"]
    assert title.end_calls == [title.token]


def test_rename_capture_failure_is_non_fatal(gateway_loop, monkeypatch):
    """A failing capture never blocks the confirm gate or the restart."""
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []
    adapter.send_restart_confirmation = _rich_ok(order)
    title = _TitleCapability(order, capture_error=RuntimeError("no perms"))
    title.attach(adapter)
    _request_restart_recording(runner, order)
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    # The read-only capture failed, so nothing was renamed and there is
    # nothing to restore — and the restart proceeds anyway: the title is
    # cosmetic, never a gate.
    assert order == ["register", "capture", "rich", "request_restart"]
    assert title.begin_calls == []
    assert title.end_calls == []
    runner.request_restart.assert_called_once()


def test_rename_edit_failure_still_restores_the_title(gateway_loop, monkeypatch):
    """The mutating rename failing leaves the captured name in hand.

    The edit runs as its own round trip AFTER the capture returned, so its
    failure (or a response lost after Discord applied it — same class)
    cannot also lose the state: the restore still runs — idempotent if the
    edit never landed — and the restart is never gated by it.
    """
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []
    adapter.send_restart_confirmation = _rich_ok(order)
    title = _TitleCapability(order, begin_error=RuntimeError("edit exploded"))
    title.attach(adapter)
    _request_restart_recording(runner, order)
    _spy_confirm(monkeypatch, order)

    _bind_session(**_DISCORD_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert order == [
        "register",
        "capture",
        "rename",
        "rich",
        "restore",
        "request_restart",
    ]
    assert title.end_calls == [title.token]
    runner.request_restart.assert_called_once()


def test_no_thread_bound_means_no_rename(gateway_loop, monkeypatch):
    """A Discord DM has no thread to retitle — the capability never fires."""
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _discord_runner(gateway_loop, monkeypatch)
    order: list[str] = []
    adapter.send_restart_confirmation = _rich_ok(order)
    title = _TitleCapability(order)
    title.attach(adapter)
    _spy_confirm(monkeypatch, order)

    _bind_session(
        platform="discord",
        chat_id="55",
        chat_type="dm",
        user_id="123456789012345678",
        session_key="discord-55",
    )
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert title.capture_calls == []
    assert title.begin_calls == []
    assert title.end_calls == []


def test_pending_title_capability_is_discord_only(gateway_loop, monkeypatch):
    """A non-Discord adapter never retitles, capability or not."""
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    adapter = next(iter(runner.adapters.values()))
    order: list[str] = []
    title = _TitleCapability(order)
    title.attach(adapter)
    _spy_confirm(monkeypatch, order)

    _bind_session(thread_id="topic-7", **_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)

    assert result["success"] is True
    assert title.capture_calls == []
    assert title.begin_calls == []
    assert title.end_calls == []


# ── relay-delivered Discord threads keep their provenance ───────────────────
#
# The relay transport stamps inbound Discord events with the UNDERLYING
# platform plus delivered_via_upstream_relay=True — platform stays ``discord``
# for session keying while adapter resolution must pick the one relay adapter
# that owns the authenticated connector socket. The restart tool rebuilds its
# SessionSource from session context, so that flag has to survive the bridge:
# without it the rebuilt source defaults to False and, in a process running
# both adapters, the retitle capability would fire through the native Discord
# adapter for a thread the connector fronted — or, in a relay-only process,
# no adapter resolves at all and the confirmation is refused outright.


def _bind_session_via_the_gateway_bridge(runner, source, session_key="discord-55"):
    """Bind session context through the real GatewayRunner._set_session_env.

    That bridge (not a hand-rolled set_session_vars) is the path production
    takes from a relay-stamped SessionSource to the tool's session env, so
    it is the path the provenance has to survive.
    """
    from gateway.session import SessionContext

    tokens = runner._set_session_env(
        SessionContext(
            source=source,
            connected_platforms=list(runner.adapters),
            home_channels={},
            session_key=session_key,
        )
    )
    return tokens


def test_relay_provenance_survives_the_session_context_rebuild():
    """Real chain: relay source → session env → rebuilt source → adapter."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from plugins.gateway_restart.tool import _source_from_session_context
    from tests.gateway.restart_test_helpers import RestartTestAdapter

    runner, _telegram = make_restart_runner()
    native = RestartTestAdapter()
    relay = RestartTestAdapter()
    runner.adapters = {Platform.DISCORD: native, Platform.RELAY: relay}

    def _discord_thread_source(**extra):
        return SessionSource(
            platform=Platform.DISCORD,
            chat_id="55",
            chat_type="thread",
            thread_id="999",
            user_id="123456789012345678",
            **extra,
        )

    relay_source = _discord_thread_source(delivered_via_upstream_relay=True)
    tokens = _bind_session_via_the_gateway_bridge(runner, relay_source)
    try:
        rebuilt = _source_from_session_context()
        assert rebuilt is not None
        assert rebuilt.delivered_via_upstream_relay is True
        assert runner._adapter_for_source(rebuilt) is relay
    finally:
        runner._clear_session_env(tokens)

    # Control: the same Discord thread WITHOUT relay delivery keeps resolving
    # the native adapter — the flag is what selects, nothing else.
    native_source = _discord_thread_source()
    tokens = _bind_session_via_the_gateway_bridge(runner, native_source)
    try:
        rebuilt = _source_from_session_context()
        assert rebuilt is not None
        assert rebuilt.delivered_via_upstream_relay is False
        assert runner._adapter_for_source(rebuilt) is native
    finally:
        runner._clear_session_env(tokens)


def test_relay_delivered_discord_thread_takes_the_noop_retitle_path(
    gateway_loop, monkeypatch
):
    """Both adapters live: the relay one delivers, the native one is untouched."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from plugins.gateway_restart.tool import handle_restart
    from tests.gateway.restart_test_helpers import RestartTestAdapter

    runner, _telegram = make_restart_runner()
    native = RestartTestAdapter()
    relay = RestartTestAdapter()
    runner.adapters = {Platform.DISCORD: native, Platform.RELAY: relay}
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    # The native adapter HAS the retitle capability — reaching it through the
    # rebuilt source is exactly the wrong-adapter bug.
    order: list[str] = []
    title = _TitleCapability(order)
    title.attach(native)
    _mock_confirm(monkeypatch, "restart")

    relay_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="55",
        chat_type="thread",
        thread_id="999",
        user_id="123456789012345678",
        delivered_via_upstream_relay=True,
    )
    tokens = _bind_session_via_the_gateway_bridge(runner, relay_source)
    try:
        result = json.loads(handle_restart({}))
    finally:
        runner._clear_session_env(tokens)

    assert result["success"] is True
    # The retitle never fired: the relay adapter has no title capability.
    assert title.capture_calls == []
    assert title.begin_calls == []
    assert title.end_calls == []
    # The prompt went out over the relay socket, not the native adapter.
    assert native.sent_calls == []
    assert len(relay.sent_calls) == 1
    chat_id, content, metadata = relay.sent_calls[0]
    assert chat_id == "55"
    assert content.startswith("<@123456789012345678> ")
    assert (metadata or {}).get("thread_id") == "999"
    runner.request_restart.assert_called_once()


def test_relay_only_gateway_still_confirms_through_the_relay_adapter(
    gateway_loop, monkeypatch
):
    """No native Discord adapter registered — the relay adapter still resolves.

    Without the preserved flag the rebuilt source looked up the (absent)
    native adapter and the tool refused to confirm at all.
    """
    from gateway.config import Platform
    from gateway.session import SessionSource
    from plugins.gateway_restart.tool import handle_restart
    from tests.gateway.restart_test_helpers import RestartTestAdapter

    runner, _telegram = make_restart_runner()
    relay = RestartTestAdapter()
    runner.adapters = {Platform.RELAY: relay}
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    _mock_confirm(monkeypatch, "restart")

    relay_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="55",
        chat_type="thread",
        thread_id="999",
        user_id="123456789012345678",
        delivered_via_upstream_relay=True,
    )
    tokens = _bind_session_via_the_gateway_bridge(runner, relay_source)
    try:
        result = json.loads(handle_restart({}))
    finally:
        runner._clear_session_env(tokens)

    assert result["success"] is True
    assert "no live adapter" not in result.get("error", "").lower()
    assert len(relay.sent_calls) == 1
    runner.request_restart.assert_called_once()


# ── the submission boundary: a loop that closed mid-flight ──────────────────
#
# asyncio.run_coroutine_threadsafe RAISES — instead of returning a failed
# future — when the submission itself fails (a gateway loop that closed
# between the tool's liveness check and the threadsafe hop), and the
# coroutine it was handed never runs. Those submissions sit around the
# confirm gate, so a raise there must not escape past the registration or
# the restore: it is a logged cosmetic failure like every other title
# problem, and the coroutine is disposed rather than leaked un-awaited.


class _TrackingTitleCapability:
    """The pending-title capability, recording the coroutines it hands out.

    A submission that never schedules a coroutine would leave it un-awaited
    (a RuntimeWarning at GC and a half-armed call); recording the objects
    lets the test prove they were CLOSED.
    """

    def __init__(self, order, token="restore-token"):
        self.order = order
        self.token = token
        self.submitted: list = []
        self.capture_calls: list[str] = []
        self.begin_calls: list[object] = []
        self.end_calls: list[object] = []

    async def _capture(self, thread_id):
        self.order.append("capture")
        self.capture_calls.append(thread_id)
        return self.token

    async def _begin(self, restore):
        self.order.append("rename")
        self.begin_calls.append(restore)

    async def _end(self, restore):
        self.order.append("restore")
        self.end_calls.append(restore)

    def capture(self, thread_id):
        coro = self._capture(thread_id)
        self.submitted.append(coro)
        return coro

    def begin(self, restore):
        coro = self._begin(restore)
        self.submitted.append(coro)
        return coro

    def end(self, restore):
        coro = self._end(restore)
        self.submitted.append(coro)
        return coro

    def attach(self, adapter):
        adapter.capture_restart_pending_thread_title = self.capture
        adapter.begin_restart_pending_thread_title = self.begin
        adapter.end_restart_pending_thread_title = self.end
        return adapter


def test_capture_submit_failure_on_a_closed_loop_is_cosmetic(caplog):
    """The closed-loop probe: the submit itself raises, nothing else happens."""
    from gateway.config import Platform
    from plugins.gateway_restart.tool import _begin_restart_pending_thread_title

    loop = asyncio.new_event_loop()
    loop.close()  # the race: the loop died before the hop landed

    order: list[str] = []
    title = _TrackingTitleCapability(order)
    adapter = title.attach(SimpleNamespace())
    source = SimpleNamespace(platform=Platform.DISCORD, thread_id="999")

    with caplog.at_level(logging.WARNING, logger="plugins.gateway_restart.tool"):
        restore = _begin_restart_pending_thread_title(adapter, loop, source)

    assert restore is None
    # Neither phase ever ran — and no exception escaped the helper.
    assert order == []
    assert any(
        "capture could not be submitted" in r.getMessage() for r in caplog.records
    )
    # The never-scheduled coroutine was disposed, not leaked un-awaited.
    assert [inspect.getcoroutinestate(c) for c in title.submitted] == [
        inspect.CORO_CLOSED
    ]


def test_rename_submit_failure_keeps_the_captured_restore_state(
    gateway_loop, monkeypatch, caplog
):
    """The same race one hop later: the token is retained, not dropped."""
    from gateway.config import Platform
    from plugins.gateway_restart.tool import _begin_restart_pending_thread_title

    real_submit = asyncio.run_coroutine_threadsafe
    submitted: list = []

    def _second_submit_fails(coro, loop):
        submitted.append(coro)
        if len(submitted) == 2:  # the mutating rename hop
            raise RuntimeError("Event loop is closed")
        return real_submit(coro, loop)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _second_submit_fails)

    order: list[str] = []
    title = _TrackingTitleCapability(order)
    adapter = title.attach(SimpleNamespace())
    source = SimpleNamespace(platform=Platform.DISCORD, thread_id="999")

    with caplog.at_level(logging.WARNING, logger="plugins.gateway_restart.tool"):
        restore = _begin_restart_pending_thread_title(adapter, gateway_loop, source)

    # The captured state survives the failed submission so the idempotent
    # exit-time restore still runs.
    assert restore is title.token
    assert order == ["capture"]  # the rename itself never ran
    assert any(
        "rename could not be submitted" in r.getMessage() for r in caplog.records
    )
    assert inspect.getcoroutinestate(submitted[1]) is inspect.CORO_CLOSED


# ── /restart keeps using the shared path (and never waits for the word) ─────
#
# The user already typed /restart — the slash command stays ungated by the
# tool's confirm flow, so these tests mock begin_user_restart directly and
# assert no clarify machinery is involved.


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
