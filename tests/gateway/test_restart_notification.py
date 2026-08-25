"""Tests for /restart notification — the gateway notifies the requester on comeback."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


# ── restart marker helpers ───────────────────────────────────────────────


def test_planned_restart_notification_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".restart_pending.json"

    assert gateway_run._planned_restart_notification_pending() is False
    marker.write_text("{}", encoding="utf-8")
    assert gateway_run._planned_restart_notification_pending() is True

    gateway_run._clear_planned_restart_notification()

    assert gateway_run._planned_restart_notification_pending() is False


# ── _handle_restart_command writes .restart_notify.json ──────────────────


@pytest.mark.asyncio
async def test_restart_command_writes_notify_file(tmp_path, monkeypatch):
    """When /restart fires, the requester's routing info is persisted to disk."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    result = await runner._handle_restart_command(event)
    assert "Restarting" in result

    notify_path = tmp_path / ".restart_notify.json"
    assert notify_path.exists()
    data = json.loads(notify_path.read_text(encoding="utf-8"))
    assert data["platform"] == "telegram"
    assert data["chat_id"] == "42"
    assert data["chat_type"] == "dm"
    assert data["message_id"] == "m1"
    assert "thread_id" not in data  # no thread → omitted


@pytest.mark.asyncio
async def test_restart_command_uses_atomic_json_writes_for_marker_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    calls = []

    def _fake_atomic_json_write(path, payload, **kwargs):
        calls.append((Path(path).name, payload, kwargs))

    # _handle_restart_command lives in gateway/slash_commands.py (extracted from
    # run.py); it uses that module's top-level atomic_json_write import.
    import gateway.slash_commands as gateway_slash
    monkeypatch.setattr(gateway_slash, "atomic_json_write", _fake_atomic_json_write)
    monkeypatch.setattr(gateway_run, "atomic_json_write", _fake_atomic_json_write)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    await runner._handle_restart_command(event)

    names = [name for name, _payload, _kwargs in calls]
    assert names == [".restart_notify.json", ".restart_last_processed.json"]
    assert calls[0][1]["chat_id"] == "42"
    assert calls[1][1]["platform"] == "telegram"


@pytest.mark.asyncio
async def test_sethome_updates_running_config_for_same_process_restart(tmp_path, monkeypatch):
    """/sethome persists to env and updates in-memory config before restart."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    saved = {}

    def _fake_save_env_value(key, value):
        saved[key] = value

    monkeypatch.setattr("hermes_cli.config.save_env_value", _fake_save_env_value)
    monkeypatch.setattr("gateway.slash_commands.persist_home_channel", lambda home, **kwargs: None)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="home-42")
    source.chat_name = "Ops Home"
    event = MessageEvent(
        text="/sethome",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-home",
    )

    result = await runner._handle_set_home_command(event)

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    assert "Home channel set" in result
    assert saved["TELEGRAM_HOME_CHANNEL"] == "home-42"
    assert home is not None
    assert home.chat_id == "home-42"
    assert home.name == "Ops Home"


@pytest.mark.asyncio
async def test_sethome_preserves_thread_target_for_same_process_restart(tmp_path, monkeypatch):
    """/sethome from a topic/thread stores the thread-aware home target."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    saved = {}

    def _fake_save_env_value(key, value):
        saved[key] = value

    monkeypatch.setattr("hermes_cli.config.save_env_value", _fake_save_env_value)
    monkeypatch.setattr("gateway.slash_commands.persist_home_channel", lambda home, **kwargs: None)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="parent-42", thread_id="topic-7")
    source.chat_name = "Ops Topic"
    event = MessageEvent(
        text="/sethome",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-home-thread",
    )

    result = await runner._handle_set_home_command(event)

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    assert "Home channel set" in result
    assert saved["TELEGRAM_HOME_CHANNEL"] == "parent-42"
    assert saved["TELEGRAM_HOME_CHANNEL_THREAD_ID"] == "topic-7"
    assert home is not None
    assert home.chat_id == "parent-42"
    assert home.thread_id == "topic-7"


# ── home-channel startup notifications ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_home_channel_startup_notification_preserves_thread_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="parent-42",
        name="Ops Topic",
        thread_id="777",
    )
    # Declare the DM-topic lookup on the adapter CLASS, not the instance.
    # _is_telegram_dm_topic_target resolves _get_dm_topic_info via type(adapter)
    # so a MagicMock auto-attribute (instance-level) is intentionally ignored;
    # a real adapter exposes the method on its class. Mirrors the fake-adapter
    # pattern in test_telegram_topic_mode.py.
    class _DmTopicAdapter(type(adapter)):
        def _get_dm_topic_info(self, chat_id, thread_id):
            return {"name": "Ops Topic"}

    adapter.__class__ = _DmTopicAdapter
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="home"))

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("telegram", "parent-42", "777")}
    adapter.send.assert_called_once_with(
        "parent-42",
        "♻️ Gateway online — Hermes is back and ready.",
        metadata={
            "thread_id": "777",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "777",
        },
    )


@pytest.mark.asyncio
async def test_relay_fronted_logical_home_gets_startup_notification(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(return_value=SendResult(success=True, message_id="home"))
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(
            enabled=False,
            home_channel=HomeChannel(
                platform=Platform.SLACK,
                chat_id="D123",
                name="Owner DM",
                user_id="U123",
                scope_id="T123",
            ),
        ),
    }

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("slack", "D123", None)}
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[:3] == (
        Platform.SLACK,
        "D123",
        "♻️ Gateway online — Hermes is back and ready.",
    )
    assert relay.send_for_platform.await_args.kwargs["metadata"]["user_id"] == "U123"
    assert relay.send_for_platform.await_args.kwargs["metadata"]["scope_id"] == "T123"


# ── _send_restart_notification ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_restart_notification_uses_logical_platform_and_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps(
            {
                "platform": "slack",
                "chat_id": "D123",
                "chat_type": "dm",
                "user_id": "U123",
                "scope_id": "T123",
                "delivered_via_upstream_relay": True,
            }
        )
    )

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(
        return_value=SendResult(success=True, message_id="restart")
    )
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(enabled=False),
    }

    delivered_target = await runner._send_restart_notification()

    assert delivered_target == ("slack", "D123", None)
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[0:2] == (Platform.SLACK, "D123")
    metadata = relay.send_for_platform.await_args.kwargs["metadata"]
    assert metadata["user_id"] == "U123"
    assert metadata["scope_id"] == "T123"
    assert not notify_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_logs_warning_on_sendresult_failure(
    tmp_path, monkeypatch, caplog
):
    """Adapter that returns SendResult(success=False) must log a WARNING, not INFO.

    Regression guard: adapter.send() catches provider errors (e.g. Telegram
    "Chat not found") and returns SendResult(success=False) rather than
    raising. The caller previously ignored the return value and always
    logged "Sent restart notification to ..." at INFO — masking real
    delivery failures behind a fake success line.
    """
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=False, error="Chat not found"),
    )

    with caplog.at_level("DEBUG", logger="gateway.run"):
        delivered_target = await runner._send_restart_notification()

    success_lines = [
        r for r in caplog.records
        if r.levelname == "INFO" and "Sent restart notification" in r.getMessage()
    ]
    warning_lines = [
        r for r in caplog.records
        if r.levelname == "WARNING"
        and "was not delivered" in r.getMessage()
        and "Chat not found" in r.getMessage()
    ]
    assert delivered_target is None
    assert not success_lines, (
        "Expected no INFO 'Sent restart notification' line when send failed, "
        f"got: {[r.getMessage() for r in success_lines]}"
    )
    assert warning_lines, (
        "Expected a WARNING line mentioning the failure; "
        f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # Still cleans up.
    assert not notify_path.exists()


@pytest.mark.asyncio
async def test_send_restart_notification_logs_info_on_sendresult_success(
    tmp_path, monkeypatch, caplog
):
    """Adapter returning SendResult(success=True) keeps the INFO log line."""
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-1"))

    with caplog.at_level("DEBUG", logger="gateway.run"):
        delivered_target = await runner._send_restart_notification()

    success_lines = [
        r for r in caplog.records
        if r.levelname == "INFO" and "Sent restart notification" in r.getMessage()
    ]
    assert delivered_target == ("telegram", "42", None)
    assert success_lines, (
        "Expected INFO 'Sent restart notification' when send succeeded; "
        f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert not notify_path.exists()


@pytest.mark.asyncio
async def test_shutdown_notifications_use_cached_live_thread_source_when_origin_missing():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="parent-42", chat_type="group", thread_id="topic-7")
    session_key = build_session_key(source)

    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=None)
    runner._cache_session_source(session_key, source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="shutdown"))

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_awaited_once_with(
        "parent-42",
        "⚠️ Gateway shutting down",
        metadata={"thread_id": "topic-7"},
    )


@pytest.mark.asyncio
async def test_restart_warning_displays_exact_llm_steer_for_accepted_session():
    from gateway.restart_wind_down import COOPERATIVE_RESTART_STEER

    runner, adapter = make_restart_runner()
    source = make_restart_source(
        chat_id="parent-42", chat_type="group", thread_id="topic-7"
    )
    session_key = build_session_key(source)
    runner._restart_requested = True
    runner._restart_command_source = source
    runner._running_agents[session_key] = object()
    runner._cooperative_restart_steered_sessions = [session_key]
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="shutdown")
    )

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_awaited_once_with(
        "parent-42",
        "⚠️ Gateway shutting down\n\n"
        "Message sent to the LLM:\n"
        f"```\n{COOPERATIVE_RESTART_STEER}\n```",
        metadata={"thread_id": "topic-7"},
    )


@pytest.mark.asyncio
async def test_restart_warning_does_not_claim_rejected_steer_was_sent():
    runner, adapter = make_restart_runner()
    source = make_restart_source(
        chat_id="parent-42", chat_type="group", thread_id="topic-7"
    )
    session_key = build_session_key(source)
    runner._restart_requested = True
    runner._restart_command_source = source
    runner._running_agents[session_key] = object()
    runner._cooperative_restart_steered_sessions = []
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="shutdown")
    )

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_awaited_once_with(
        "parent-42",
        "⚠️ Gateway shutting down",
        metadata={"thread_id": "topic-7"},
    )


@pytest.mark.asyncio
async def test_shutdown_notifications_are_fully_muted_when_flag_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="active-42", chat_type="group", thread_id="topic-7")
    session_key = build_session_key(source)

    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    adapter.send = AsyncMock()

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_not_awaited()
    # A muted platform is owed no comeback notice either: no marker is written.
    assert not (tmp_path / ".shutdown_notify.json").exists()


# ── shutdown warning → .shutdown_notify.json marker ────────────────────────


def test_shutdown_notification_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".shutdown_notify.json"

    assert gateway_run._shutdown_notification_pending() is False
    marker.write_text("{}", encoding="utf-8")
    assert gateway_run._shutdown_notification_pending() is True

    gateway_run._clear_shutdown_notification()

    assert gateway_run._shutdown_notification_pending() is False


def _active_shutdown_runner(tmp_path, monkeypatch, *, source):
    """Runner with one live session whose origin resolves to ``source``."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.drain_control.drain_notification_suppressed", lambda: False
    )
    runner, adapter = make_restart_runner()
    session_key = build_session_key(source)
    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    return runner, adapter


@pytest.mark.asyncio
async def test_shutdown_warning_persists_notified_targets(tmp_path, monkeypatch):
    """Chats that actually got the ⚠️ are persisted with routing for the ♻️ pair."""
    source = make_restart_source(chat_id="active-42", chat_type="group", thread_id="topic-7")
    runner, adapter = _active_shutdown_runner(tmp_path, monkeypatch, source=source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-warn"))

    await runner._notify_active_sessions_of_shutdown()

    marker = tmp_path / ".shutdown_notify.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert isinstance(data["requested_at"], float)
    assert len(data["targets"]) == 1
    entry = data["targets"][0]
    assert entry["platform"] == "telegram"
    assert entry["chat_id"] == "active-42"
    assert entry["thread_id"] == "topic-7"
    assert entry["chat_type"] == "group"
    assert "message_id" not in entry  # no reply anchor was available
    assert "user_id" not in entry  # not a relay target
    assert "delivered_via_upstream_relay" not in entry


@pytest.mark.asyncio
async def test_shutdown_marker_skips_targets_whose_warning_failed(tmp_path, monkeypatch):
    """SendResult(success=False) means the chat never saw the ⚠️ — no marker."""
    source = make_restart_source(chat_id="active-42", chat_type="group")
    runner, adapter = _active_shutdown_runner(tmp_path, monkeypatch, source=source)
    adapter.send = AsyncMock(
        return_value=SendResult(success=False, error="Chat not found")
    )

    await runner._notify_active_sessions_of_shutdown()

    assert not (tmp_path / ".shutdown_notify.json").exists()


@pytest.mark.asyncio
async def test_shutdown_marker_written_when_drain_suppresses_home_broadcast(
    tmp_path, monkeypatch
):
    """suppress_notification skips only the home broadcast — warned sessions still pair."""
    source = make_restart_source(chat_id="active-42", chat_type="group")
    runner, adapter = _active_shutdown_runner(tmp_path, monkeypatch, source=source)
    monkeypatch.setattr(
        "gateway.drain_control.drain_notification_suppressed", lambda: True
    )
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-warn"))

    await runner._notify_active_sessions_of_shutdown()

    # Active session got the ⚠️, home channel did not — and only the former
    # is owed a comeback.
    assert adapter.send.await_count == 1
    data = json.loads(
        (tmp_path / ".shutdown_notify.json").read_text(encoding="utf-8")
    )
    assert [t["chat_id"] for t in data["targets"]] == ["active-42"]


@pytest.mark.asyncio
async def test_shutdown_marker_written_for_in_chat_restart(tmp_path, monkeypatch):
    """Other live sessions are owed the ♻️ pair even on a chat-originated /restart."""
    source = make_restart_source(chat_id="active-42", chat_type="group")
    runner, adapter = _active_shutdown_runner(tmp_path, monkeypatch, source=source)
    runner._restart_requested = True
    runner._restart_command_source = make_restart_source(chat_id="requester-7")
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-warn"))

    await runner._notify_active_sessions_of_shutdown()

    # Home-channel broadcast skipped for in-chat restart; the warned session
    # is still persisted. The requester itself is deduped at boot against
    # .restart_notify.json.
    assert adapter.send.await_count == 1
    data = json.loads(
        (tmp_path / ".shutdown_notify.json").read_text(encoding="utf-8")
    )
    assert [t["chat_id"] for t in data["targets"]] == ["active-42"]


# ── boot-time ♻️ comeback notice ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_comeback_notice_sent_and_marker_unlinked(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".shutdown_notify.json"
    marker.write_text(
        json.dumps(
            {
                "requested_at": 1.0,
                "targets": [
                    {
                        "platform": "telegram",
                        "chat_id": "parent-42",
                        "thread_id": "topic-7",
                        "chat_type": "group",
                        "message_id": "m-9",
                    }
                ],
            }
        )
    )

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-back"))

    delivered = await runner._send_shutdown_comeback_notifications()

    assert delivered == {("telegram", "parent-42", "topic-7")}
    assert not marker.exists()
    assert adapter.send.await_count == 1
    assert adapter.send.await_args.args[:2] == (
        "parent-42",
        "♻️ Gateway back online — ready when you are.",
    )
    assert adapter.send.await_args.kwargs["metadata"] == {"thread_id": "topic-7"}


@pytest.mark.asyncio
async def test_shutdown_comeback_notice_skips_targets_notified_this_boot(
    tmp_path, monkeypatch
):
    """/restart or a home-channel notice that just fired suppresses the second ping."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".shutdown_notify.json"
    marker.write_text(
        json.dumps(
            {
                "requested_at": 1.0,
                "targets": [{"platform": "telegram", "chat_id": "42", "chat_type": "dm"}],
            }
        )
    )

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-back"))

    delivered = await runner._send_shutdown_comeback_notifications(
        skip_targets={("telegram", "42", None)}
    )

    assert delivered == set()
    adapter.send.assert_not_awaited()
    # Dedup still consumes the marker — the notice was delivered another way.
    assert not marker.exists()


@pytest.mark.asyncio
async def test_shutdown_comeback_notice_missing_marker_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True))

    delivered = await runner._send_shutdown_comeback_notifications()

    assert delivered == set()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_comeback_notice_failed_send_still_unlinks_marker(
    tmp_path, monkeypatch
):
    """A dead chat is dropped, not retried on every later boot."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".shutdown_notify.json"
    marker.write_text(
        json.dumps(
            {
                "requested_at": 1.0,
                "targets": [{"platform": "telegram", "chat_id": "42", "chat_type": "dm"}],
            }
        )
    )

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=False, error="Chat not found")
    )

    delivered = await runner._send_shutdown_comeback_notifications()

    assert delivered == set()
    assert not marker.exists()


@pytest.mark.asyncio
async def test_relay_shutdown_comeback_notice_preserves_owner_metadata(
    tmp_path, monkeypatch
):
    """Relay-fronted targets route via the logical platform with user/scope ids."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".shutdown_notify.json"
    marker.write_text(
        json.dumps(
            {
                "requested_at": 1.0,
                "targets": [
                    {
                        "platform": "slack",
                        "chat_id": "D123",
                        "user_id": "U123",
                        "scope_id": "T123",
                        "delivered_via_upstream_relay": True,
                    }
                ],
            }
        )
    )

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(
        return_value=SendResult(success=True, message_id="back")
    )
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(enabled=False),
    }

    delivered = await runner._send_shutdown_comeback_notifications()

    assert delivered == {("slack", "D123", None)}
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[0:3] == (
        Platform.SLACK,
        "D123",
        "♻️ Gateway back online — ready when you are.",
    )
    metadata = relay.send_for_platform.await_args.kwargs["metadata"]
    assert metadata["user_id"] == "U123"
    assert metadata["scope_id"] == "T123"
    assert not marker.exists()


# ── _boot_sends dedup wiring ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_sends_wire_comeback_dedup_against_restart_and_home_notices(
    tmp_path, monkeypatch
):
    """skip_targets flows restart → home-channel send → comeback, never double-pinging."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_startup_restore_drain_timeout_secs", lambda: 0)

    runner, _adapter = make_restart_runner()
    order: list[str] = []
    home_skip: list = []
    comeback_skip: list = []

    async def _restart_notify():
        order.append("restart")
        return ("telegram", "42", None)

    async def _home_notify(*, skip_targets=None):
        order.append("home")
        home_skip.append(set(skip_targets))
        return {("telegram", "home-1", None)}

    async def _comeback_notify(*, skip_targets=None):
        order.append("comeback")
        comeback_skip.append(set(skip_targets))
        return set()

    runner._send_restart_notification = _restart_notify
    runner._send_home_channel_startup_notifications = _home_notify
    runner._send_shutdown_comeback_notifications = _comeback_notify
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._redeliver_claimed_obligations = AsyncMock(return_value=None)

    await runner._await_startup_boot_sends(planned_restart_notification_pending=True)

    assert order == ["restart", "home", "comeback"]
    # The home-channel send must not re-ping the /restart chat...
    assert home_skip == [{("telegram", "42", None)}]
    # ...and the comeback skips everyone a boot notice already reached.
    assert comeback_skip == [{("telegram", "42", None), ("telegram", "home-1", None)}]


@pytest.mark.asyncio
async def test_boot_sends_comeback_runs_without_planned_marker(tmp_path, monkeypatch):
    """Raw SIGTERM boots have no planned marker — the comeback notice still fires."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_startup_restore_drain_timeout_secs", lambda: 0)

    runner, _adapter = make_restart_runner()
    comeback_skip: list = []

    async def _restart_notify():
        return None

    async def _home_notify(*, skip_targets=None):
        raise AssertionError("home-channel startup notice must not fire without a planned marker")

    async def _comeback_notify(*, skip_targets=None):
        comeback_skip.append(skip_targets)
        return set()

    runner._send_restart_notification = _restart_notify
    runner._send_home_channel_startup_notifications = _home_notify
    runner._send_shutdown_comeback_notifications = _comeback_notify
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._redeliver_claimed_obligations = AsyncMock(return_value=None)

    await runner._await_startup_boot_sends(planned_restart_notification_pending=False)

    assert comeback_skip == [set()]


