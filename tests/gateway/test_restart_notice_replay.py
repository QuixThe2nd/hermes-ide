"""Planned-restart online notice survives an offline notification-channel transport at boot.

The ``.restart_pending.json`` marker used to be consumed in ``finally`` even when no live transport
existed for the notification channel, so the "Gateway online" notice was never sent and never replayed.
See #112109. Runs the real boot pass, marker helpers, notification-channel sender and DeliveryTransport.

Fork union: keeps upstream's single write-once marker pass (fcd4778e1b) and the fork's
per-delivery checkpointing, so acknowledgments survive a hung/cancelled boot pass and
``pending_targets`` bookkeeping remains observable while a later destination is still owed a notice.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import gateway.delivery as delivery
import gateway.run as gateway_run
from gateway.config import DeliveryTarget, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult

ONLINE_NOTICE = "♻️ Gateway online — Hermes is back and ready."


def _adapter():
    return SimpleNamespace(
        send_path_degraded=False,
        send=AsyncMock(return_value=SendResult(success=True, message_id="unit-test-notice")),
    )


@pytest.fixture
def boot_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    # Await the boot task to completion and propagate failures deterministically.
    monkeypatch.setattr(gateway_run, "_startup_restore_drain_timeout_secs", lambda: 0)
    runner = object.__new__(gateway_run.GatewayRunner)
    platform_config = PlatformConfig(
        enabled=True,
        gateway_restart_notification=True,
        notification_channel=DeliveryTarget(
            platform=Platform.DISCORD, chat_id="unit-test-home", name="Test home"
        ),
    )
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: platform_config},
        sessions_dir=tmp_path / "sessions",
    )
    runner.adapters = {}
    runner.delivery_router = SimpleNamespace(adapters=runner.adapters)
    runner._failed_platforms = {}
    runner._sync_voice_mode_state_to_adapter = Mock()
    runner._bind_voice_input_callback = Mock()
    runner._update_platform_runtime_status = Mock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    runner._schedule_resume_pending_sessions = Mock()
    monkeypatch.setattr("gateway.channel_directory.build_channel_directory", AsyncMock())
    # Unrelated conversation recovery and optional account-status text are isolated.
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._redeliver_claimed_obligations = AsyncMock(return_value=0)
    runner._free_tier_startup_line = Mock(return_value=None)
    # Keep the real requester-marker check; this case has only the planned marker.
    assert not (tmp_path / ".restart_notify.json").exists()
    marker = tmp_path / ".restart_pending.json"
    marker.write_text("{}", encoding="utf-8")
    return runner, platform_config, marker


async def _boot(runner):
    await runner._await_startup_boot_sends(
        planned_restart_notification_pending=gateway_run._planned_restart_notification_pending()
    )


async def _reconnect(runner, platform, adapter):
    runner._failed_platforms[platform] = {}
    await runner._install_reconnected_adapter(platform, adapter)
    await asyncio.gather(*runner._background_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True], ids=["offline-at-boot-replayed-on-reconnect", "live-at-boot"])
async def test_planned_restart_notice_reaches_notification_channel(boot_notice, monkeypatch, live):
    runner, platform_config, marker = boot_notice
    adapter = _adapter()
    if live:
        runner.adapters[Platform.DISCORD] = adapter
    transport = (
        delivery.DeliveryTransport(adapter, platform_config, Platform.DISCORD)
        if live else None
    )
    # Confirm the stub represents the real resolver's result for this adapter map.
    resolved = delivery.resolve_delivery_transport(
        Platform.DISCORD, runner.config, runner.adapters
    )
    assert resolved == transport
    resolver = Mock(return_value=transport)
    monkeypatch.setattr(delivery, "resolve_delivery_transport", resolver)
    assert marker.exists()
    assert gateway_run._planned_restart_notification_pending()

    await _boot(runner)

    resolver.assert_called_once_with(Platform.DISCORD, runner.config, runner.adapters)
    runner._claim_pending_obligations.assert_awaited_once_with()
    runner._redeliver_claimed_obligations.assert_awaited_once_with([])
    if live:
        adapter.send.assert_awaited_once_with("unit-test-home", ONLINE_NOTICE, metadata={"non_conversational": True})
    else:
        adapter.send.assert_not_called()
        assert marker.exists(), "marker must survive a boot with no live transport"
        resolver.return_value = delivery.DeliveryTransport(adapter, platform_config, Platform.DISCORD)
        await _reconnect(runner, Platform.DISCORD, adapter)
        adapter.send.assert_awaited_once_with("unit-test-home", ONLINE_NOTICE, metadata={"non_conversational": True})
    assert not marker.exists()
    assert not gateway_run._planned_restart_notification_pending()


@pytest.mark.asyncio
async def test_partial_delivery_is_persisted_and_not_repeated(boot_notice):
    runner, _, marker = boot_notice
    telegram, discord = _adapter(), _adapter()
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        notification_channel=DeliveryTarget(platform=Platform.TELEGRAM, chat_id="other-home", thread_id="7", name="Other"),
    )
    runner.config.platforms[Platform.SLACK] = PlatformConfig(
        enabled=True, gateway_restart_notification=False,
        notification_channel=DeliveryTarget(platform=Platform.SLACK, chat_id="muted-home", name="Muted"),
    )
    runner.adapters[Platform.TELEGRAM] = telegram

    await _boot(runner)

    telegram.send.assert_awaited_once()
    discord.send.assert_not_called()
    assert json.loads(marker.read_text(encoding="utf-8"))["delivered_targets"] == [["telegram", "other-home", "7"]]

    # A fresh process has no in-memory history: dedupe must come from the marker. The opted-out
    # Slack notification channel is never owed a notice, so Discord's delivery completes the set.
    recovered = object.__new__(gateway_run.GatewayRunner)
    recovered.__dict__.update(runner.__dict__)
    await _reconnect(recovered, Platform.DISCORD, discord)

    discord.send.assert_awaited_once()
    telegram.send.assert_awaited_once()
    assert not marker.exists()

    # Nothing pending: a later reconnect stays silent.
    await _reconnect(recovered, Platform.DISCORD, discord)
    discord.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", ["unavailable", "rejected", "exception", "cancelled"])
async def test_partial_notice_delivery_survives_restart_and_concurrent_replay(boot_notice, outage):
    """Fork coverage: acknowledgments persist per delivery and survive a hung/cancelled boot pass."""
    runner, _, marker = boot_notice
    other = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    # Deliver one destination first, then encounter an unavailable/failing/hung transport.
    runner.config.platforms = {
        Platform.TELEGRAM: PlatformConfig(
            enabled=True,
            notification_channel=DeliveryTarget(platform=Platform.TELEGRAM, chat_id="other-home", thread_id="7", name="Other home"),
        ),
        **runner.config.platforms,
        Platform.SLACK: PlatformConfig(
            enabled=True, gateway_restart_notification=False,
            notification_channel=DeliveryTarget(platform=Platform.SLACK, chat_id="muted-home", name="Muted home"),
        ),
    }
    runner.adapters[Platform.TELEGRAM] = other
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(*args, **kwargs):
        started.set()
        await release.wait()
        return SendResult(success=True)

    if outage != "unavailable":
        runner.adapters[Platform.DISCORD] = _adapter()
    if outage == "rejected":
        runner.adapters[Platform.DISCORD].send.return_value = SendResult(success=False, error="temporarily unavailable")
    elif outage == "exception":
        runner.adapters[Platform.DISCORD].send.side_effect = RuntimeError("transport disconnected")
    elif outage == "cancelled":
        runner.adapters[Platform.DISCORD].send.side_effect = slow_send

    boot = asyncio.create_task(runner._await_startup_boot_sends(planned_restart_notification_pending=True))
    if outage == "cancelled":
        await asyncio.wait_for(started.wait(), timeout=5)
        # Acknowledgments must already be on disk while a later destination is hung.
        marker_data = json.loads(marker.read_text())
        assert marker_data["delivered_targets"] == [["telegram", "other-home", "7"]]
        assert marker_data["pending_targets"] == [["discord", "unit-test-home", None]]
        boot.cancel()
        with pytest.raises(asyncio.CancelledError):
            await boot
    else:
        await boot
    data = json.loads(marker.read_text())
    assert data["delivered_targets"] == [["telegram", "other-home", "7"]]
    assert data["pending_targets"] == [["discord", "unit-test-home", None]]
    other.send.assert_awaited_once()

    # A new runner has no in-memory delivery history: dedupe must come from the marker.
    recovered = object.__new__(gateway_run.GatewayRunner)
    recovered.__dict__.update(runner.__dict__)
    recovered.__dict__.pop("_planned_restart_notice_lock", None)
    started.clear()
    fresh_discord = _adapter()
    fresh_discord.send.side_effect = slow_send
    recovered.adapters[Platform.DISCORD] = fresh_discord
    recovered._failed_platforms[Platform.DISCORD] = {}
    await asyncio.wait_for(recovered._install_reconnected_adapter(Platform.DISCORD, fresh_discord), timeout=5)
    await asyncio.wait_for(started.wait(), timeout=5)
    # Installation completed even though notification delivery is still blocked.
    assert marker.exists()
    concurrent = asyncio.create_task(recovered._replay_pending_planned_restart_notification())
    release.set()
    await asyncio.gather(concurrent, *recovered._background_tasks)
    assert not marker.exists()
    fresh_discord.send.assert_awaited_once()
    other.send.assert_awaited_once()

    recovered._failed_platforms[Platform.DISCORD] = {}
    await recovered._install_reconnected_adapter(Platform.DISCORD, fresh_discord)
    await asyncio.gather(*recovered._background_tasks)
    fresh_discord.send.assert_awaited_once()
    other.send.assert_awaited_once()
