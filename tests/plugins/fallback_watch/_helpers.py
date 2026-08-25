"""Shared helpers for fallback_watch behavior-contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

import yaml

CHAT_ID = "999888777666555444"

SAMPLE_LINE = (
    "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] "
    "agent.chat_completion_helpers: Fallback activated:"
    " stealth/ox-alpha → grok-4.6 (xai-oauth)"
)

NO_SESSION_LINE = (
    "2026-08-25 15:32:15,579 INFO agent.chat_completion_helpers:"
    " Fallback activated: kimi-k2 → glm-4.7 (zai)"
)


def write_home(
    tmp_path: Path,
    *,
    config: Mapping[str, Any] | None = None,
    env: str | None = "DISCORD_BOT_TOKEN=test-token\n",
    secrets_env: str | None = None,
) -> Path:
    """Materialize a HERMES_HOME with config.yaml and optional secret files."""
    home = tmp_path / "hermes-home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(dict(config or {}), sort_keys=False), encoding="utf-8"
    )
    if env is not None:
        (home / ".env").write_text(env, encoding="utf-8")
    if secrets_env is not None:
        secrets = home / "secrets"
        secrets.mkdir(exist_ok=True)
        (secrets / "discord.env").write_text(secrets_env, encoding="utf-8")
    return home


def fallback_config(**overrides: Any) -> Dict[str, Any]:
    section: Dict[str, Any] = {
        "enabled": True,
        "chat_id": CHAT_ID,
    }
    section.update(overrides)
    return {"fallback_watch": section}


class RecordingSend:
    """Drop-in ``send`` that records messages; can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: List[str] = []
        self.fail = fail

    def __call__(self, message: str) -> None:
        if self.fail:
            raise RuntimeError("discord is down")
        self.sent.append(message)


def fake_http_recorder() -> tuple[Callable[..., tuple[int, bytes]], Dict[str, Any]]:
    captured: Dict[str, Any] = {}

    def fake_http(req, timeout=25.0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return 200, b'{"id": "1"}'

    return fake_http, captured
