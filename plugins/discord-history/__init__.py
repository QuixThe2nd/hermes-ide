"""Hermes registration entry point for the Discord history plugin."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from typing import Any

try:  # Hermes loads this file as a package; pytest also collects it as a module.
    from .discord_history.tool import DISCORD_HISTORY_SCHEMA as _PARAMETERS_SCHEMA
except ImportError:  # pragma: no cover - exercised by source-tree collection
    from discord_history.tool import DISCORD_HISTORY_SCHEMA as _PARAMETERS_SCHEMA

DISCORD_HISTORY_SCHEMA = {
    "name": "discord_history",
    "description": "Search the owner-authorized PostgreSQL Discord archive.",
    "parameters": deepcopy(_PARAMETERS_SCHEMA),
}


def check_requirements() -> bool:
    """Visibility gate only. The handler performs authorization again."""
    try:
        try:
            from .discord_history.doctor import requirements_ready
        except ImportError:
            from discord_history.doctor import requirements_ready
        return requirements_ready()
    except Exception:
        return False


def handle_discord_history(arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """Lazy model-tool dispatch; service authorization is never bypassed."""
    try:
        from .discord_history.service import handle_discord_history as handler
    except ImportError:
        from discord_history.service import handle_discord_history as handler
    if arguments is None:
        arguments, kwargs = dict(kwargs), {}
    return handler(arguments=arguments, **kwargs)


def register(ctx: Any) -> None:
    try:
        from .discord_history.cli import cli_entry, setup_cli
    except ImportError:
        from discord_history.cli import cli_entry, setup_cli

    try:
        from importlib import import_module
        registry = import_module("tools.registry").registry
    except ImportError:
        registry = None
    if registry is not None and registry.get_entry("discord_history") is not None:
        raise RuntimeError("discord_history_tool_name_collision")

    ctx.register_tool(
        name="discord_history",
        toolset="discord_history",
        schema=DISCORD_HISTORY_SCHEMA,
        handler=handle_discord_history,
        check_fn=check_requirements,
        description="Search the owner-authorized PostgreSQL Discord archive.",
    )
    ctx.register_cli_command(
        name="discord-history",
        help="Ingest, search, and verify the Discord history archive",
        setup_fn=setup_cli,
        handler_fn=cli_entry,
        description="Manage the PostgreSQL/DCE Discord history archive.",
    )
    ctx.register_skill(
        name="discord-history",
        path=Path(__file__).parent / "skills" / "discord-history" / "SKILL.md",
        description="Recall owner-authorized Discord history with exact citations.",
    )
