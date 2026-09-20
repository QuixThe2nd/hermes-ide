"""Lifecycle-broadcast contract: with no notification channel configured the
startup broadcast is skipped with a single INFO log line — never a fallback
destination — and a configured channel still receives it."""

import logging

import pytest

from gateway.config import DeliveryTarget, Platform
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_startup_broadcast_without_notification_channel_logs_and_skips(caplog):
    runner, adapter = make_restart_runner()
    # No notification_channel configured anywhere.

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        delivered = await runner._send_notification_channel_startup_notifications()

    assert delivered == set()
    assert adapter.sent_calls == []
    skip_logs = [
        r for r in caplog.records
        if "no notification channel configured" in r.getMessage()
    ]
    assert len(skip_logs) == 1
    assert skip_logs[0].levelname == "INFO"


@pytest.mark.asyncio
async def test_startup_broadcast_still_sends_with_notification_channel(caplog):
    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].notification_channel = DeliveryTarget(
        platform=Platform.TELEGRAM,
        chat_id="restarts-42",
        name="Gateway Restarts",
    )

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        delivered = await runner._send_notification_channel_startup_notifications()

    assert delivered == {("telegram", "restarts-42", None)}
    assert [chat_id for chat_id, _content, _metadata in adapter.sent_calls] == ["restarts-42"]
    assert not [
        r for r in caplog.records
        if "no notification channel configured" in r.getMessage()
    ]
