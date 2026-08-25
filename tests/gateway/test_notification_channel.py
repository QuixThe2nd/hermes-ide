"""Notification channels: route lifecycle broadcasts away from the home chat.

A platform's ``notification_channel`` (same HomeChannel shape as
``home_channel``) becomes the destination for gateway shutdown/startup
broadcasts so the home channel stays free for conversation (e.g. a dedicated
"#gateway-restarts" channel). Covers the config round-trip, the per-platform
routing decision in both broadcast paths — with the existing suppression,
dedup, and thread-metadata contracts preserved — and the /setnotify +
/clearnotify handlers.
"""

import json
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    clear_notification_channel,
    persist_notification_channel,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import build_session_key
from hermes_cli.commands import (
    GATEWAY_KNOWN_COMMANDS,
    gateway_help_lines,
    resolve_command,
    telegram_bot_commands,
)
from hermes_cli.config import load_config
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _notify_channel(
    platform: Platform = Platform.TELEGRAM,
    chat_id: str = "restarts-chat",
    thread_id: str | None = None,
) -> HomeChannel:
    return HomeChannel(
        platform=platform,
        chat_id=chat_id,
        name="gateway-restarts",
        thread_id=thread_id,
    )


def _home_channel(chat_id: str = "home-chat") -> HomeChannel:
    return HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        name="Telegram Home",
    )


def _sent_chat_ids(adapter) -> list[str]:
    return [chat_id for chat_id, _content, _metadata in adapter.sent_calls]


# ── config schema ────────────────────────────────────────────────────────


class TestNotificationChannelConfig:
    def test_platform_config_roundtrip(self):
        pc = PlatformConfig(
            enabled=True,
            home_channel=_home_channel(),
            notification_channel=_notify_channel(thread_id="99"),
        )
        restored = PlatformConfig.from_dict(pc.to_dict())

        assert restored.notification_channel is not None
        assert restored.notification_channel.chat_id == "restarts-chat"
        assert restored.notification_channel.thread_id == "99"
        assert restored.notification_channel.platform == Platform.TELEGRAM
        # Home channel survives alongside the notification channel.
        assert restored.home_channel is not None
        assert restored.home_channel.chat_id == "home-chat"

    def test_absent_notification_channel_roundtrips_to_none(self):
        d = PlatformConfig().to_dict()
        assert "notification_channel" not in d
        assert PlatformConfig.from_dict(d).notification_channel is None

    def test_get_notification_channel_mirrors_home_lookup(self):
        config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    home_channel=_home_channel(),
                    notification_channel=_notify_channel(),
                ),
                Platform.DISCORD: PlatformConfig(home_channel=_home_channel("d-home")),
            }
        )

        assert config.get_notification_channel(Platform.TELEGRAM).chat_id == "restarts-chat"
        # Platform without one keeps current behavior.
        assert config.get_notification_channel(Platform.DISCORD) is None
        # Unknown platform mirrors get_home_channel's None.
        assert config.get_notification_channel(Platform.SLACK) is None

    def test_persist_writes_and_clear_removes(self):
        """persist → config.yaml round-trip through the real loader."""
        persist_notification_channel(_notify_channel())

        raw = load_config()
        assert raw["platforms"]["telegram"]["notification_channel"]["chat_id"] == "restarts-chat"

        # Clearing removes the key entirely, leaving the rest of the
        # platform section (and other platforms) intact.
        clear_notification_channel(Platform.TELEGRAM)
        raw = load_config()
        assert "notification_channel" not in raw["platforms"]["telegram"]
        assert "telegram" in raw["platforms"]

    def test_clear_without_persisted_channel_is_noop(self):
        clear_notification_channel(Platform.MATRIX)
        assert "matrix" not in load_config().get("platforms", {})


# ── shutdown broadcast routing ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_broadcast_routes_to_notification_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = _notify_channel()

    await runner._notify_active_sessions_of_shutdown()

    # Exactly one broadcast — to the notification channel, never the home chat.
    assert _sent_chat_ids(adapter) == ["restarts-chat"]
    # The ♻️ comeback marker pairs with where the ⚠️ actually landed.
    marker = json.loads((tmp_path / ".shutdown_notify.json").read_text())
    assert marker["targets"] == [{"platform": "telegram", "chat_id": "restarts-chat"}]


@pytest.mark.asyncio
async def test_shutdown_broadcast_falls_back_to_home_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()

    await runner._notify_active_sessions_of_shutdown()

    assert _sent_chat_ids(adapter) == ["home-chat"]


@pytest.mark.asyncio
async def test_shutdown_suppression_flag_still_honored_with_notification_channel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = _notify_channel()
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent_calls == []


@pytest.mark.asyncio
async def test_shutdown_dedup_when_notification_chat_already_pinged(tmp_path, monkeypatch):
    """An active session IN the notification chat gets one ⚠️, not two."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = _notify_channel()

    source = make_restart_source(chat_id="restarts-chat")
    session_key = build_session_key(source)
    runner._running_agents = {session_key: MagicMock()}
    runner._cache_session_source(session_key, source)

    await runner._notify_active_sessions_of_shutdown()

    # Active-session interrupt ping only — the broadcast is deduped against
    # it, and the home channel is not used as a second destination.
    assert _sent_chat_ids(adapter) == ["restarts-chat"]


@pytest.mark.asyncio
async def test_shutdown_drain_marker_suppression_still_honored(tmp_path, monkeypatch):
    """A suppress_notification drain marker mutes the broadcast — routing
    to a notification channel must not open a side door around it."""
    import gateway.drain_control as dc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = _notify_channel()
    runner._running_agents["agent:main:telegram:dm:999"] = MagicMock()
    dc.write_drain_request(principal="nas", suppress_notification=True)

    await runner._notify_active_sessions_of_shutdown()

    # Only the active-session ping survives; neither the notification
    # channel nor the home channel gets the broadcast.
    sent_chat_ids = set(_sent_chat_ids(adapter))
    assert sent_chat_ids == {"999"}


# ── startup broadcast routing ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_broadcast_routes_to_notification_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = _notify_channel(
        thread_id="99"
    )

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("telegram", "restarts-chat", "99")}
    assert _sent_chat_ids(adapter) == ["restarts-chat"]
    # Thread target keeps its routing metadata on the routed destination.
    _chat_id, _content, metadata = adapter.sent_calls[0]
    assert metadata["thread_id"] == "99"


@pytest.mark.asyncio
async def test_startup_broadcast_falls_back_to_home_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("telegram", "home-chat", None)}
    assert _sent_chat_ids(adapter) == ["home-chat"]


@pytest.mark.asyncio
async def test_startup_suppression_flag_still_honored_with_notification_channel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = _notify_channel()
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == set()
    assert adapter.sent_calls == []


# ── /setnotify and /clearnotify ──────────────────────────────────────────


def _notify_event(source) -> MessageEvent:
    return MessageEvent(
        text="/setnotify",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-notify",
    )


@pytest.mark.asyncio
async def test_setnotify_persists_and_updates_running_config(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="restarts-42", thread_id="topic-7")
    source.chat_name = "Gateway Restarts"

    result = await runner._handle_set_notify_command(_notify_event(source))

    assert "Gateway Restarts" in result
    channel = runner.config.get_notification_channel(Platform.TELEGRAM)
    assert channel is not None
    assert channel.chat_id == "restarts-42"
    assert channel.thread_id == "topic-7"
    assert channel.name == "Gateway Restarts"
    # Persisted through the real config path (isolated HERMES_HOME).
    raw = load_config()
    assert raw["platforms"]["telegram"]["notification_channel"]["chat_id"] == "restarts-42"


@pytest.mark.asyncio
async def test_setnotify_leaves_home_channel_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = _home_channel()

    await runner._handle_set_notify_command(
        _notify_event(make_restart_source(chat_id="restarts-42"))
    )

    assert runner.config.get_home_channel(Platform.TELEGRAM).chat_id == "home-chat"
    raw = load_config()
    assert "home_channel" not in raw["platforms"]["telegram"]


@pytest.mark.asyncio
async def test_clearnotify_removes_persisted_and_running_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="restarts-42")
    await runner._handle_set_notify_command(_notify_event(source))
    assert runner.config.get_notification_channel(Platform.TELEGRAM) is not None

    result = await runner._handle_clear_notify_command(
        MessageEvent(
            text="/clearnotify",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m-clear",
        )
    )

    assert runner.config.get_notification_channel(Platform.TELEGRAM) is None
    raw = load_config()
    assert "notification_channel" not in raw["platforms"]["telegram"]
    assert "cleared" in result.lower()


@pytest.mark.asyncio
async def test_setnotify_rejects_relay_target_it_cannot_authenticate(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="restarts-42")
    source.delivered_via_upstream_relay = True
    # No _adapter_for_source on the runner → relay provenance unverifiable.

    result = await runner._handle_set_notify_command(_notify_event(source))

    assert "Failed to save notification channel" in result
    assert runner.config.get_notification_channel(Platform.TELEGRAM) is None


# ── registration across gateway surfaces ─────────────────────────────────


def test_setnotify_and_clearnotify_registered_across_gateway_surfaces():
    for name in ("setnotify", "clearnotify"):
        command = resolve_command(name)
        assert command is not None, f"/{name} must be registered"
        assert command.gateway_only, f"/{name} is a gateway command"
        assert name in GATEWAY_KNOWN_COMMANDS
        assert any(f"/{name}" in line for line in gateway_help_lines())
        assert name in {menu_name for menu_name, _desc in telegram_bot_commands()}

    # Hyphenated aliases resolve to the canonical names, like set-home → sethome.
    assert resolve_command("set-notify").name == "setnotify"
    assert resolve_command("clear-notify").name == "clearnotify"
