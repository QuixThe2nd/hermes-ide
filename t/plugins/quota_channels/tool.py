"""quota_channels_tick tool registration and handler."""

from __future__ import annotations

import json
from typing import Any

from plugins.quota_channels.core import (
    QuotaChannelsError,
    check_minimum_config_from_mapping,
    load_quota_config,
    run_tick,
)

QUOTA_CHANNELS_TICK_SCHEMA = {
    "name": "quota_channels_tick",
    "description": (
        "Run one Discord quota-channel tick: optionally refresh provider usage "
        "and voice-channel names (self-gated), always refresh the Models "
        "category label."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": "Bypass the quota-interval gate and fetch all providers.",
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


def check_quota_channels_requirements() -> bool:
    """True when minimum quota_channels config exists (no network/secrets)."""
    return check_minimum_config_from_mapping(_load_config_mapping())


def handle_quota_channels_tick(args: dict, **_: Any) -> str:
    force = bool(args.get("force"))
    try:
        config = load_quota_config()
        result = run_tick(config, force=force)
        return json.dumps(result, separators=(",", ":"))
    except QuotaChannelsError as exc:
        return json.dumps({"success": False, "error": str(exc)}, separators=(",", ":"))
