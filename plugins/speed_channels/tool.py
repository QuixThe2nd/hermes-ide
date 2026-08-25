"""speed_channels_tick tool registration and handler."""

from __future__ import annotations

import json
from typing import Any

from plugins.speed_channels.core import (
    SpeedChannelsError,
    check_minimum_config_from_mapping,
    load_speed_config,
    run_tick,
)

SPEED_CHANNELS_TICK_SCHEMA = {
    "name": "speed_channels_tick",
    "description": (
        "Run one Discord speed-channel tick: optionally poll qBittorrent, "
        "SABnzbd and slskd and rename their voice channels (self-gated), "
        "always refresh the Speeds category label."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": "Bypass the poll-interval gate and poll all three downloaders.",
            }
        },
        "additionalProperties": False,
    },
}


def _load_config_mapping() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly()
    except Exception:
        return {}


def check_speed_channels_requirements() -> bool:
    """True when minimum speed_channels config exists (no network/secrets)."""
    return check_minimum_config_from_mapping(_load_config_mapping())


def handle_speed_channels_tick(args: dict, **_: Any) -> str:
    force = bool(args.get("force"))
    try:
        config = load_speed_config()
        result = run_tick(config, force=force)
        return json.dumps(result, separators=(",", ":"))
    except SpeedChannelsError as exc:
        return json.dumps({"success": False, "error": str(exc)}, separators=(",", ":"))
