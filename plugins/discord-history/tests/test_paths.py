from __future__ import annotations

import pytest

from discord_history.paths import (
    dce_archive,
    dce_binary,
    denial_log_path,
    deployed_plugin_root,
    secrets_path,
    state_root,
)


def test_paths_follow_non_default_hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "archive"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert state_root() == home / "discord-history"
    assert secrets_path() == home / "secrets" / "discord-history.env"
    assert deployed_plugin_root() == home / "plugins" / "discord-history"
    assert dce_binary() == home / "discord-history" / "bin" / "current" / "DiscordChatExporter.Cli"
    assert dce_archive().name == "DiscordChatExporter.Cli.linux-x64-2.47.3.zip"
    assert denial_log_path() == home / "discord-history" / "logs" / "access-denied.jsonl"


def test_load_secrets_default_path_uses_hermes_home(monkeypatch, tmp_path):
    import base64
    import os

    from discord_history.config import ConfigError, load_secrets

    home = tmp_path / "alt-home"
    secrets_dir = home / "secrets"
    secrets_dir.mkdir(parents=True)
    secret_path = secrets_dir / "discord-history.env"
    key = b"k" * 32
    secret_path.write_text(
        "\n".join(
            [
                "DISCORD_HISTORY_DATABASE_URL=postgresql://archive:pw@localhost/archive",
                f"DISCORD_HISTORY_AUDIT_HMAC_KEY={base64.b64encode(key).decode()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    secret_path.chmod(0o600)

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("DISCORD_HISTORY_DATABASE_URL", raising=False)

    loaded = load_secrets()
    assert loaded.database_url.startswith("postgresql://")
    assert loaded.audit_hmac_key == key
    assert os.environ.get("DISCORD_HISTORY_DATABASE_URL") is None

    missing = home / "secrets" / "missing.env"
    with pytest.raises(ConfigError):
        load_secrets(missing)
