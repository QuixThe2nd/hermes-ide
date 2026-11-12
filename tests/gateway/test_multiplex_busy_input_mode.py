"""Profile-specific busy-input behavior for multiplexed gateways."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    SessionSource,
    build_session_key,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner


class _ProfileAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {}


def _runner(*, default_mode: str = "interrupt") -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._busy_input_mode = default_mode
    runner._busy_text_mode = "queue" if default_mode == "queue" else "interrupt"
    runner._profile_adapters = {}
    runner.adapters = {}
    runner._sessions = {}
    runner._draining = False
    runner._restart_requested = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda source: True
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    return runner


def _event(*, profile: str | None) -> MessageEvent:
    return MessageEvent(
        text="follow up",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
            profile=profile,
        ),
        message_id="message-1",
    )


def _adapter() -> _ProfileAdapter:
    adapter = _ProfileAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.TELEGRAM,
    )
    return adapter


async def _load_profile_snapshot(
    runner: GatewayRunner,
    profile_home,
    mode: str | None,
    *,
    legacy_text_mode: str | None = None,
) -> _ProfileAdapter:
    display = "display:\n"
    if legacy_text_mode is not None:
        display += f"  busy_text_mode: {legacy_text_mode}\n"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(display, encoding="utf-8")

    assert await runner._start_one_profile_adapters("research", profile_home, {}) == 0
    adapter = _adapter()
    runner._profile_adapters["research"][Platform.TELEGRAM] = adapter
    runner._configure_profile_adapter(adapter, "research", Platform.TELEGRAM)
    return adapter


@pytest.mark.asyncio
async def test_secondary_legacy_busy_text_mode_is_profile_specific(tmp_path):
    runner = _runner(default_mode="interrupt")
    adapter = await _load_profile_snapshot(
        runner,
        tmp_path / "research",
        "interrupt",
        legacy_text_mode="queue",
    )
    event = _event(profile="research")
    session_key = runner._session_key_for_source(event.source)
    agent = MagicMock()
    agent._active_children = []
    runner._running_agents[session_key] = agent

    assert await runner._handle_active_session_busy_message(event, session_key) is False
    assert adapter._busy_text_mode == "queue"
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary_mode", [None, "not-a-mode"])
async def test_missing_or_invalid_secondary_mode_falls_back_to_gateway_default(
    tmp_path,
    secondary_mode,
):
    runner = _runner(default_mode="queue")
    await _load_profile_snapshot(runner, tmp_path / "research", secondary_mode)
    source = _event(profile="research").source

    assert runner._effective_busy_input_mode(source) == "queue"
    assert runner._effective_busy_text_mode(source) == "queue"
    assert runner._busy_input_mode == "queue"
    assert runner._busy_text_mode == "queue"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["steer", "interrupt"])
@pytest.mark.parametrize("secondary_privacy", [True, False])
async def test_primary_adapter_busy_origin_uses_routed_privacy(
    tmp_path, monkeypatch, mode, secondary_privacy,
):
    """The primary busy callback bypasses the scoped normal-message handler."""
    from dataclasses import asdict
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
    from hermes_constants import get_hermes_home_override
    from run_agent import AIAgent

    home = tmp_path / ".hermes"
    secondary = home / "profiles" / "research"
    secondary.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.run._hermes_home", home)
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    for directory, privacy in ((home, not secondary_privacy), (secondary, secondary_privacy)):
        (directory / "config.yaml").write_text(
            f"privacy:\n  redact_pii: {str(privacy).lower()}\n", encoding="utf-8",
        )
    runner = _runner(default_mode=mode)
    runner.config.profile_routes = [
        ProfileRoute(name="research-chat", platform="telegram", profile="research", chat_id="chat-1"),
    ]
    adapter = _adapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._wire_adapter_handlers(adapter)
    adapter.gateway_runner = runner
    event = MessageEvent(
        text="follow up", message_id="message-1",
        source=adapter.build_source(chat_id="chat-1", user_id="user-1"),
    )
    assert event.source.profile == "research"
    original = asdict(event.source)
    key = runner._session_key_for_source(event.source)
    agent = AIAgent(
        api_key="offline-test", base_url="http://127.0.0.1:1/v1", provider="openai-compat",
        model="test-model", enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
        skip_memory=True, save_trajectories=False, platform="cli",
    )
    agent._executing_tools = mode == "interrupt"
    runner._session_state(key).turn.agent = agent
    adapter._active_sessions[key] = asyncio.Event()
    ambient = get_hermes_home_override()
    await adapter._handle_message_while_active(event, key)
    messages = [{"role": "tool", "tool_call_id": "probe", "content": "Tool completed."}]
    apply_pending_steer_to_tool_results(agent, messages, 1)
    output = messages[-1]["content"]
    assert "follow up" in output
    for value in (event.source.chat_id, event.source.user_id, event.message_id, "research"):
        assert (value not in output) is secondary_privacy
    assert asdict(event.source) == original
    assert get_hermes_home_override() == ambient
    assert key not in adapter._pending_messages
