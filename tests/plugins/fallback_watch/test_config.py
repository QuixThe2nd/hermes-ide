"""Config parsing and validation contracts for fallback_watch."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.fallback_watch.core import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_PLATFORM,
    DEFAULT_POLL_SECONDS,
    FallbackWatchError,
    load_config,
    load_config_section,
    load_watch_config,
)
from tests.plugins.fallback_watch._helpers import CHAT_ID


class TestDefaults:
    def test_absent_section_is_disabled_with_defaults(self):
        config = load_watch_config({})
        assert config.enabled is False
        assert config.platform == DEFAULT_PLATFORM
        assert config.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS
        assert config.poll_seconds == DEFAULT_POLL_SECONDS

    def test_empty_section_is_disabled(self):
        assert load_watch_config({"fallback_watch": {}}).enabled is False

    def test_enabled_section_resolves_overrides(self):
        config = load_watch_config({
            "fallback_watch": {
                "enabled": True,
                "chat_id": CHAT_ID,
                "cooldown_seconds": 45,
                "poll_seconds": 0.25,
            }
        })
        assert config.enabled is True
        assert config.chat_id == CHAT_ID
        assert config.cooldown_seconds == 45
        assert config.poll_seconds == 0.25

    def test_platform_defaults_to_discord_when_blank(self):
        config = load_watch_config({"fallback_watch": {"platform": ""}})
        assert config.platform == "discord"

    def test_cooldown_garbage_falls_back_to_default(self):
        config = load_watch_config({"fallback_watch": {"cooldown_seconds": "soon"}})
        assert config.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS

    def test_cooldown_negative_clamps_to_zero(self):
        config = load_watch_config({"fallback_watch": {"cooldown_seconds": -5}})
        assert config.cooldown_seconds == 0

    def test_enabled_accepts_truthy_strings(self):
        config = load_watch_config({
            "fallback_watch": {"enabled": "true", "chat_id": CHAT_ID}
        })
        assert config.enabled is True


class TestValidation:
    def test_enabled_without_chat_id_is_rejected(self):
        with pytest.raises(FallbackWatchError, match="chat_id"):
            load_watch_config({"fallback_watch": {"enabled": True}})

    def test_chat_id_ignored_when_disabled(self):
        config = load_watch_config({"fallback_watch": {"enabled": False}})
        assert config.enabled is False
        assert config.chat_id == ""

    @pytest.mark.parametrize("platform", ["telegram", "slack", "discord-webhook"])
    def test_unsupported_platform_is_rejected(self, platform: str):
        with pytest.raises(FallbackWatchError, match="platform"):
            load_watch_config({
                "fallback_watch": {"platform": platform, "chat_id": CHAT_ID}
            })

    def test_platform_is_case_and_space_normalized(self):
        config = load_watch_config({
            "fallback_watch": {"platform": " Discord ", "chat_id": CHAT_ID}
        })
        assert config.platform == "discord"

    @pytest.mark.parametrize("poll_seconds", [0, -1])
    def test_non_positive_poll_seconds_is_rejected(self, poll_seconds: float):
        with pytest.raises(FallbackWatchError, match="poll_seconds"):
            load_watch_config({"fallback_watch": {"poll_seconds": poll_seconds}})

    def test_poll_seconds_garbage_falls_back_to_default(self):
        config = load_watch_config({"fallback_watch": {"poll_seconds": "fast"}})
        assert config.poll_seconds == DEFAULT_POLL_SECONDS

    def test_non_mapping_section_is_rejected(self):
        with pytest.raises(FallbackWatchError, match="mapping"):
            load_watch_config({"fallback_watch": "yes"})


class TestLoadConfigFromPath:
    def test_explicit_config_path_round_trips(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "fallback_watch:\n"
            "  enabled: true\n"
            f'  chat_id: "{CHAT_ID}"\n'
            "  cooldown_seconds: 60\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.enabled is True
        assert config.chat_id == CHAT_ID
        assert config.cooldown_seconds == 60

    def test_missing_file_raises_with_path(self, tmp_path: Path):
        with pytest.raises(FallbackWatchError, match="cannot read"):
            load_config(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises_with_path(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text("fallback_watch: [unclosed", encoding="utf-8")
        with pytest.raises(FallbackWatchError, match="cannot parse"):
            load_config(path)

    def test_non_mapping_document_acts_as_empty(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        assert load_config_section(path) == {}
