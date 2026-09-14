"""Gateway restart plugin — the agent-callable ``restart`` tool."""

from __future__ import annotations


def register(ctx) -> None:
    """Register ``restart``. Called once by the plugin loader."""
    from plugins.gateway_restart.tool import (
        RESTART_SCHEMA,
        check_restart_requirements,
        handle_restart,
    )

    ctx.register_tool(
        name="restart",
        toolset="gateway",
        schema=RESTART_SCHEMA,
        handler=handle_restart,
        check_fn=check_restart_requirements,
        emoji="♻️",
    )
