"""Quota Channels plugin — Discord voice-channel quota display."""

from __future__ import annotations


def register(ctx) -> None:
    """Register quota_channels_tick. Called once by the plugin loader."""
    from plugins.quota_channels.tool import (
        QUOTA_CHANNELS_TICK_SCHEMA,
        check_quota_channels_requirements,
        handle_quota_channels_tick,
    )

    ctx.register_tool(
        name="quota_channels_tick",
        toolset="quota_channels",
        schema=QUOTA_CHANNELS_TICK_SCHEMA,
        handler=handle_quota_channels_tick,
        check_fn=check_quota_channels_requirements,
        emoji="📊",
    )
