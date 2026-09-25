"""Staleness freeze behavior contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.fallback_quota_reorder.core import (
    QuotaReading,
    is_frozen,
    run_reorder,
    save_state,
    state_path,
    update_staleness_state,
)
from tests.plugins.fallback_quota_reorder._helpers import (
    CHANNEL_IDS,
    default_channel_names,
    fake_http_for_names,
    write_hermes_home,
    write_quota_config_path,
)


def _reading(provider: str, pct: int, reset_seconds: int, channel_key: str) -> QuotaReading:
    return QuotaReading(
        channel_key=channel_key,
        provider=provider,
        channel_name="unused",
        pct=pct,
        reset_seconds=reset_seconds,
    )


class TestUpdateStalenessState:
    def test_two_identical_short_dated_ticks_freeze(self):
        names = {
            "codex": "Codex: 90% • 7d left",
            "kimi": "Kimi: 80% • 7d left",
            "zai": "z.ai: 70% • 7d left",
            "grok": "Grok: 60% • 1h left",
            "cursor": "Cursor: 90%/85% • 25d left",
        }
        readings = {
            "xai-oauth": _reading("xai-oauth", 60, 3600, "grok"),
        }
        state = {
            "last_names": dict(names),
            "last_timestamp": 1_000,
            "consecutive_stale": 1,
        }
        new_state = update_staleness_state(
            names, readings, state, 1800, now_fn=lambda: 1_100
        )
        assert new_state["consecutive_stale"] == 2
        assert is_frozen(new_state) is True

    def test_name_change_resets_staleness(self):
        names = {
            "codex": "Codex: 90% • 7d left",
            "kimi": "Kimi: 80% • 7d left",
            "zai": "z.ai: 70% • 7d left",
            "grok": "Grok: 60% • 1h left",
            "cursor": "Cursor: 90%/85% • 25d left",
        }
        previous_names = dict(names)
        previous_names["grok"] = "Grok: 61% • 1h left"
        readings = {
            "xai-oauth": _reading("xai-oauth", 60, 3600, "grok"),
        }
        state = {
            "last_names": previous_names,
            "last_timestamp": 1_000,
            "consecutive_stale": 2,
        }
        new_state = update_staleness_state(
            names, readings, state, 1800, now_fn=lambda: 1_100
        )
        assert new_state["consecutive_stale"] == 0
        assert is_frozen(new_state) is False

    def test_long_dated_readings_never_count_as_stale(self):
        names = default_channel_names()
        readings = {
            "openai-codex": _reading("openai-codex", 90, 7 * 86400, "codex"),
            "kimi-coding": _reading("kimi-coding", 80, 7 * 86400, "kimi"),
            "zai": _reading("zai", 70, 7 * 86400, "zai"),
            "xai-oauth": _reading("xai-oauth", 60, 7 * 86400, "grok"),
            "cursor": _reading("cursor", 85, 25 * 86400, "cursor"),
        }
        state = {
            "last_names": dict(names),
            "last_timestamp": 1_000,
            "consecutive_stale": 1,
        }
        new_state = update_staleness_state(
            names, readings, state, 1800, now_fn=lambda: 1_100
        )
        assert new_state["consecutive_stale"] == 0
        assert is_frozen(new_state) is False


class TestRunReorderStalenessFreeze:
    def _setup_frozen_reorder(
        self, monkeypatch, tmp_path: Path
    ) -> tuple[Path, bytes, dict[str, str]]:
        names = default_channel_names()
        names["grok"] = "Grok: 60% • 1h left"
        write_hermes_home(
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "xai-oauth", "model": "grok"},
                {"provider": "kimi-coding", "model": "kimi"},
                {"provider": "openai-codex", "model": "codex"},
            ],
        )
        quota_config = tmp_path / "quota-config.yaml"
        write_quota_config_path(quota_config)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config_bytes = (tmp_path / "config.yaml").read_bytes()
        save_state(
            {
                "last_names": {key: names.get(key, "") for key in CHANNEL_IDS},
                "last_timestamp": 1_000_000,
                "consecutive_stale": 2,
            }
        )
        return quota_config, config_bytes, names

    def test_frozen_run_skips_config_write(self, monkeypatch, tmp_path: Path):
        quota_config, config_bytes, names = self._setup_frozen_reorder(
            monkeypatch, tmp_path
        )
        run_reorder(
            config_path=quota_config,
            force_quota=False,
            http_fn=fake_http_for_names(names),
            now_fn=lambda: 1_000_100,
        )
        assert (tmp_path / "config.yaml").read_bytes() == config_bytes

    def test_force_quota_bypasses_freeze_and_writes(self, monkeypatch, tmp_path: Path):
        quota_config, config_bytes, names = self._setup_frozen_reorder(
            monkeypatch, tmp_path
        )
        result = run_reorder(
            config_path=quota_config,
            force_quota=True,
            http_fn=fake_http_for_names(names),
            now_fn=lambda: 1_000_100,
        )
        assert result["frozen"] is False
        assert (tmp_path / "config.yaml").read_bytes() != config_bytes
        assert result["would_change"] is True
