"""Config validation and loading contracts."""

from __future__ import annotations

import pytest

from plugins.speed_channels.core import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    SpeedChannelsError,
    check_minimum_config_from_mapping,
    load_speed_config,
    validate_speed_config,
)


def _section(**overrides):
    section = {
        "guild_id": "900000000000000001",
        "category_id": "cat",
        "channel_ids": {"qbittorrent": "cq", "sabnzbd": "cs", "slskd": "cl"},
    }
    section.update(overrides)
    return section


def test_valid_section_resolves_with_defaults():
    resolved = validate_speed_config(_section())
    assert resolved == {
        "guild_id": "900000000000000001",
        "category_id": "cat",
        "channel_ids": {"qbittorrent": "cq", "sabnzbd": "cs", "slskd": "cl"},
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
    }


def test_poll_interval_is_respected():
    assert validate_speed_config(_section(poll_interval_seconds=60))[
        "poll_interval_seconds"
    ] == 60


@pytest.mark.parametrize(
    "section, complaint",
    [
        (_section(guild_id=""), "guild_id"),
        (_section(category_id=None), "category_id"),
        (_section(channel_ids={"qbittorrent": "cq"}), "sabnzbd"),
        (_section(channel_ids="nope"), "channel_ids"),
        ("not a mapping", "mapping"),
    ],
)
def test_missing_pieces_are_named(section, complaint):
    with pytest.raises(SpeedChannelsError, match=complaint):
        validate_speed_config(section)


def test_load_from_hermes_home_config(hermes):
    resolved = load_speed_config()
    assert resolved["channel_ids"]["qbittorrent"] == "cq"


def test_missing_section_is_an_error(hermes):
    (hermes / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    with pytest.raises(SpeedChannelsError, match="speed_channels section"):
        load_speed_config()


def test_check_minimum_config_gates_dashboard_probes():
    assert check_minimum_config_from_mapping({"speed_channels": _section()}) is True
    assert check_minimum_config_from_mapping({"speed_channels": _section(guild_id="")}) is False
    assert check_minimum_config_from_mapping({}) is False
    assert check_minimum_config_from_mapping({"speed_channels": 3}) is False
