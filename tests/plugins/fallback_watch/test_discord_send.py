"""Token resolution and Discord REST send contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.fallback_watch import core
from plugins.fallback_watch.core import (
    DISCORD_API_BASE,
    FallbackWatchError,
    format_alert,
    parse_fallback_line,
    send_discord_alert,
)
from tests.plugins.fallback_watch._helpers import (
    CHAT_ID,
    SAMPLE_LINE,
    fake_http_recorder,
)


class TestTokenResolution:
    def test_token_reads_from_env_file(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text(
            'DISCORD_BOT_TOKEN="token-from-env"\n', encoding="utf-8"
        )
        monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path)
        assert core.discord_token() == "token-from-env"

    def test_token_falls_back_to_secrets_discord_env(self, tmp_path: Path, monkeypatch):
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text(
            "DISCORD_BOT_TOKEN=token-from-secrets\n", encoding="utf-8"
        )
        monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path)
        assert core.discord_token() == "token-from-secrets"

    def test_env_file_wins_over_secrets(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=primary\n", encoding="utf-8")
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "discord.env").write_text(
            "DISCORD_BOT_TOKEN=secondary\n", encoding="utf-8"
        )
        monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path)
        assert core.discord_token() == "primary"

    def test_missing_everywhere_names_both_locations(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(core, "_hermes_home", lambda: tmp_path)
        with pytest.raises(FallbackWatchError) as excinfo:
            core.discord_token()
        message = str(excinfo.value)
        assert "DISCORD_BOT_TOKEN" in message
        assert ".env" in message
        assert "discord.env" in message


class TestSendDiscordAlert:
    def test_posts_to_channel_messages_with_parse_empty_mentions(self):
        http_fn, captured = fake_http_recorder()
        send_discord_alert("hello", CHAT_ID, token="tok", http_fn=http_fn)
        assert captured["url"] == f"{DISCORD_API_BASE}/channels/{CHAT_ID}/messages"
        assert captured["data"] == {
            "content": "hello",
            "allowed_mentions": {"parse": []},
        }
        assert captured["headers"]["Authorization"] == "Bot tok"

    def test_non_2xx_raises_without_the_token(self):
        def denied(req, timeout=25.0):
            return 403, b'{"message": "Missing Access"}'

        with pytest.raises(FallbackWatchError, match="403") as excinfo:
            send_discord_alert("hello", CHAT_ID, token="secret-tok", http_fn=denied)
        assert "secret-tok" not in str(excinfo.value)

    def test_full_alert_rides_as_message_content(self):
        http_fn, captured = fake_http_recorder()
        event = parse_fallback_line(SAMPLE_LINE)
        assert event is not None
        send_discord_alert(format_alert(event), CHAT_ID, token="tok", http_fn=http_fn)
        assert captured["data"]["content"].startswith(
            "⚠️ Hermes primary model fallback activated"
        )
        assert json.loads(json.dumps(captured["data"])) == captured["data"]
