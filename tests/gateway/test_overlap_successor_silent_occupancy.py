"""Regression: overlap-restart successor must not post occupancy refusals."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    try_acquire_active_session,
)
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)
from tests.gateway.test_max_concurrent_sessions import _silence_global_gateway_hooks


@pytest.fixture(autouse=True)
def _isolated_active_session_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def _install_live_lease(
    session_key: str, holder_pid: int, monkeypatch, *, surface: str = "gateway:discord",
) -> None:
    real_pid_exists = __import__("gateway.status", fromlist=["_pid_exists"])._pid_exists

    def pid_exists(pid: int) -> bool:
        if pid == holder_pid:
            return True
        return bool(real_pid_exists(pid))

    monkeypatch.setattr("gateway.status._pid_exists", pid_exists)

    lease, refused = try_acquire_active_session(
        session_id=session_key,
        surface=surface,
        config={},
        metadata={
            "platform": "discord",
            "live_session_id": session_key,
        },
    )
    assert lease is not None and refused is None

    from hermes_cli.active_sessions import _read_entries, _state_path, _write_entries

    entries = _read_entries(_state_path())
    for entry in entries:
        if str(entry.get("session_id") or "") == session_key:
            entry["pid"] = holder_pid
            entry["process_start_time"] = 1.0
            entry["surface"] = surface
    _write_entries(_state_path(), entries)


@pytest.mark.asyncio
async def test_overlap_successor_inbound_silent_while_predecessor_holds_lease(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    predecessor_pid = 4242
    source = make_restart_source(chat_id="42", platform=Platform.DISCORD, user_id="7")
    session_key = build_session_key(source)
    _install_live_lease(session_key, predecessor_pid, monkeypatch)

    class OverlapAdapter(RestartTestAdapter):
        supports_overlap_restart = True

    adapter = OverlapAdapter(Platform.DISCORD)
    adapter.handoff_event = AsyncMock(return_value=True)
    runner, _adapter = make_restart_runner(adapter, platform=Platform.DISCORD)
    runner._overlap_successor_pending = True
    runner._overlap_predecessor_pid = predecessor_pid

    event = MessageEvent(
        text="hello during overlap handoff",
        message_type=MessageType.TEXT,
        source=source,
        message_id="d-overlap-1",
    )

    async def fail_if_agent_runs(self_inner, ev, src, qk, generation):
        raise AssertionError("successor must hand off, not start a second agent")

    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        result = await runner._handle_message(event)

    assert result is None
    assert "This chat is open in another Hermes window/terminal" not in str(result or "")
    adapter.handoff_event.assert_awaited_once_with(
        event, session_key, target_pid=predecessor_pid
    )

    # Delivery to the owning generation joins its live turn as a queued follow-up.
    predecessor, predecessor_adapter = make_restart_runner(platform=Platform.DISCORD)
    predecessor._busy_input_mode = "queue"
    predecessor._running_agents[session_key] = MagicMock()
    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        assert await predecessor._handle_message(event) is None
    assert predecessor_adapter._pending_messages[session_key].text == event.text

    held = active_session_registry_snapshot()
    assert len(held) == 1
    assert held[0].get("session_id") == session_key
    assert held[0].get("pid") == predecessor_pid


@pytest.mark.asyncio
async def test_overlap_successor_retries_refused_handoff_without_dropping(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    predecessor_pid = 4242
    source = make_restart_source(chat_id="42", platform=Platform.DISCORD, user_id="7")
    session_key = build_session_key(source)
    _install_live_lease(session_key, predecessor_pid, monkeypatch)

    class OverlapAdapter(RestartTestAdapter):
        supports_overlap_restart = True

    adapter = OverlapAdapter(Platform.DISCORD)
    adapter.handoff_event = AsyncMock(side_effect=[False, False, True])
    runner, _adapter = make_restart_runner(adapter, platform=Platform.DISCORD)
    runner._overlap_successor_pending = True
    runner._overlap_predecessor_pid = predecessor_pid
    event = MessageEvent(
        text="retry until the predecessor accepts",
        message_type=MessageType.TEXT,
        source=source,
        message_id="d-overlap-retry",
    )

    async def fail_if_agent_runs(self_inner, ev, src, qk, generation):
        raise AssertionError("successor must keep routing to the owning generation")

    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        result = await runner._handle_message(event)

    assert result is None
    assert adapter.handoff_event.await_count == 3
    for handoff in adapter.handoff_event.await_args_list:
        assert handoff.args == (event, session_key)
        assert handoff.kwargs == {"target_pid": predecessor_pid}


@pytest.mark.asyncio
async def test_overlap_successor_runs_event_after_predecessor_lease_disappears(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    predecessor_pid = 4242
    source = make_restart_source(chat_id="42", platform=Platform.DISCORD, user_id="7")
    session_key = build_session_key(source)
    _install_live_lease(session_key, predecessor_pid, monkeypatch)

    class OverlapAdapter(RestartTestAdapter):
        supports_overlap_restart = True

    adapter = OverlapAdapter(Platform.DISCORD)

    async def detached_predecessor(*args, **kwargs):
        from hermes_cli.active_sessions import _state_path, _write_entries
        _write_entries(_state_path(), [])
        return False

    adapter.handoff_event = AsyncMock(side_effect=detached_predecessor)
    runner, _adapter = make_restart_runner(adapter, platform=Platform.DISCORD)
    runner._overlap_successor_pending = True
    runner._overlap_predecessor_pid = predecessor_pid
    event = MessageEvent(
        text="run after the predecessor exits",
        message_type=MessageType.TEXT,
        source=source,
        message_id="d-overlap-exited",
    )
    ran = []

    async def record_agent_run(self_inner, ev, src, qk, generation):
        ran.append(ev)
        return None

    with patch.object(GatewayRunner, "_handle_message_with_agent", record_agent_run):
        result = await runner._handle_message(event)

    assert result is None
    assert ran == [event]
    adapter.handoff_event.assert_awaited_once_with(
        event, session_key, target_pid=predecessor_pid,
    )


@pytest.mark.asyncio
async def test_overlap_successor_posts_occupancy_when_unrelated_owner_holds_lease(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    predecessor_pid = 4242
    cli_pid = 9999
    source = make_restart_source(chat_id="42", platform=Platform.DISCORD, user_id="7")
    session_key = build_session_key(source)
    _install_live_lease(session_key, cli_pid, monkeypatch, surface="cli")

    runner, _adapter = make_restart_runner(platform=Platform.DISCORD)
    runner._overlap_successor_pending = True
    runner._overlap_predecessor_pid = predecessor_pid

    event = MessageEvent(
        text="hello during overlap handoff",
        message_type=MessageType.TEXT,
        source=source,
        message_id="d-overlap-cli-1",
    )

    async def fail_if_agent_runs(self_inner, ev, src, qk, generation):
        raise AssertionError("_handle_message_with_agent must not run while a foreign lease is held")

    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        result = await runner._handle_message(event)

    assert result is not None
    assert "This chat is open in another Hermes window/terminal" in str(result)

    held = active_session_registry_snapshot()
    assert len(held) == 1
    assert held[0].get("session_id") == session_key
    assert held[0].get("pid") == cli_pid
    assert held[0].get("surface") == "cli"
