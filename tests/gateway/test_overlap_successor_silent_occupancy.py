"""Regression: overlap-restart successor must not post occupancy refusals."""

from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    try_acquire_active_session,
)
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
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

    runner, _adapter = make_restart_runner(platform=Platform.DISCORD)
    runner._overlap_successor_pending = True
    runner._overlap_predecessor_pid = predecessor_pid

    event = MessageEvent(
        text="hello during overlap handoff",
        message_type=MessageType.TEXT,
        source=source,
        message_id="d-overlap-1",
    )

    async def fail_if_agent_runs(self_inner, ev, src, qk, generation):
        raise AssertionError("_handle_message_with_agent must not run while predecessor holds the lease")

    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        result = await runner._handle_message(event)

    assert result is None
    assert "This chat is open in another Hermes window/terminal" not in str(result or "")

    held = active_session_registry_snapshot()
    assert len(held) == 1
    assert held[0].get("session_id") == session_key
    assert held[0].get("pid") == predecessor_pid


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
