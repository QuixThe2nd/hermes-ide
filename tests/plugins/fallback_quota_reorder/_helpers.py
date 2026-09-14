"""Shared helpers for fallback_quota_reorder behavior-contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

import yaml

CHANNEL_IDS: Dict[str, str] = {
    "codex": "111111111111111111",
    "kimi": "222222222222222222",
    "zai": "333333333333333333",
    "grok": "444444444444444444",
    "cursor": "555555555555555555",
}

BULLET = "\u2022"


def write_hermes_home(
    tmp_path: Path,
    *,
    fallback_providers: list[dict[str, Any]],
    extra_config: Mapping[str, Any] | None = None,
) -> None:
    config: dict[str, Any] = {"fallback_providers": fallback_providers}
    if extra_config:
        config.update(dict(extra_config))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=test-token\n", encoding="utf-8")


def write_quota_config_path(
    path: Path,
    *,
    channel_ids: Mapping[str, str] | None = None,
    quota_interval_seconds: int = 1800,
) -> None:
    payload = {
        "quota_channels": {
            "channel_ids": dict(channel_ids or CHANNEL_IDS),
            "quota_interval_seconds": quota_interval_seconds,
        }
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def fake_http_for_names(names_by_key: Mapping[str, str]) -> Callable[..., tuple[int, bytes]]:
    ids = dict(CHANNEL_IDS)

    def fake_http(req, timeout=25.0):
        url = req.full_url
        for key, channel_id in ids.items():
            if channel_id in url:
                name = names_by_key.get(key, "")
                return 200, json.dumps({"name": name}).encode()
        raise AssertionError(f"unexpected request: {url}")

    return fake_http


def default_channel_names() -> Dict[str, str]:
    return {
        "codex": f"Codex: 90% {BULLET} 7d left",
        "kimi": f"Kimi: 80% {BULLET} 7d left",
        "zai": f"z.ai: 70% {BULLET} 7d left",
        "grok": f"Grok: 60% {BULLET} 7d left",
        "cursor": f"Cursor: 90%/85% {BULLET} 25d left",
    }
