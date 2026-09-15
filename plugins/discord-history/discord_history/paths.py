"""Profile-aware filesystem locations for the Discord history plugin."""
from __future__ import annotations

from pathlib import Path

from hermes_constants import get_hermes_home

DCE_VERSION = "2.47.3"


def state_root() -> Path:
    return get_hermes_home() / "discord-history"


def secrets_path() -> Path:
    return get_hermes_home() / "secrets" / "discord-history.env"


def deployed_plugin_root() -> Path:
    return get_hermes_home() / "plugins" / "discord-history"


def dce_binary() -> Path:
    return state_root() / "bin" / "current" / "DiscordChatExporter.Cli"


def dce_archive() -> Path:
    return (
        state_root()
        / "bin"
        / f"DiscordChatExporter.Cli.linux-x64-{DCE_VERSION}.zip"
    )


def denial_log_path() -> Path:
    return state_root() / "logs" / "access-denied.jsonl"
