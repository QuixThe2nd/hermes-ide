"""Speed Channels plugin — Discord voice-channel download walls."""

from __future__ import annotations


def register(ctx) -> None:
    """Register speed_channels_tick. Called once by the plugin loader."""
    from plugins.speed_channels.tool import (
        SPEED_CHANNELS_TICK_SCHEMA,
        check_speed_channels_requirements,
        handle_speed_channels_tick,
    )

    ctx.register_tool(
        name="speed_channels_tick",
        toolset="speed_channels",
        schema=SPEED_CHANNELS_TICK_SCHEMA,
        handler=handle_speed_channels_tick,
        check_fn=check_speed_channels_requirements,
        emoji="⚡",
    )
