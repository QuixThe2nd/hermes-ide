"""Contract tests for the removed per-platform default-destination key.

A ``config.yaml`` written by an older gateway may still carry a stale
``home_channel`` platform key. Loading must tolerate it (ignored, never
crashed on) and a save cycle must drop it — while ``notification_channel``
(the kept lifecycle-broadcast target) persists via /setnotify and reloads
unchanged through the real config path.
"""

import pytest

from gateway.config import (
    DeliveryTarget,
    Platform,
    PlatformConfig,
    load_gateway_config,
)
from gateway.platforms.base import MessageEvent, MessageType
from hermes_cli.config import load_config
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


class TestStaleDefaultDestinationKeyTolerated:
    def test_from_dict_ignores_stale_platform_key(self):
        pc = PlatformConfig.from_dict({
            "enabled": True,
            "home_channel": {"platform": "telegram", "chat_id": "555", "name": "Home"},
        })

        assert pc.enabled is True
        assert pc.notification_channel is None

    def test_stale_key_loads_via_full_config_and_is_dropped_on_save(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    enabled: true\n"
            "    home_channel:\n"
            "      platform: telegram\n"
            "      chat_id: '555'\n"
            "      name: Home\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()  # must not raise

        telegram = config.platforms[Platform.TELEGRAM]
        assert telegram.enabled is True
        assert telegram.notification_channel is None
        assert "home_channel" not in telegram.to_dict()


@pytest.mark.asyncio
async def test_setnotify_persists_and_loads_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="restarts-42", thread_id="topic-7")
    source.chat_name = "Gateway Restarts"

    result = await runner._handle_set_notify_command(MessageEvent(
        text="/setnotify",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-notify",
    ))
    assert "Gateway Restarts" in result

    # Reloaded through the real loader, the persisted target is unchanged
    # (including the authenticated-user provenance captured with it).
    reloaded = load_gateway_config()
    assert reloaded.get_notification_channel(Platform.TELEGRAM) == DeliveryTarget(
        platform=Platform.TELEGRAM,
        chat_id="restarts-42",
        name="Gateway Restarts",
        thread_id="topic-7",
        user_id="u1",
    )
    raw = load_config()
    assert raw["platforms"]["telegram"]["notification_channel"]["thread_id"] == "topic-7"
