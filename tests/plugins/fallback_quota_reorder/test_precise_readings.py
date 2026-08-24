"""Precise-readings contracts: quota_channels state beats rounded channel names."""

from __future__ import annotations

import json
from pathlib import Path

from plugins.fallback_quota_reorder.core import (
    load_precise_readings,
    readings_from_names,
    run_reorder,
)
from plugins.quota_channels.core import (
    QuotaChannelsError,
    STATE_FILENAME,
    load_state,
    run_tick,
    save_state,
    validate_quota_config,
)
from tests.plugins.fallback_quota_reorder._helpers import (
    BULLET,
    default_channel_names,
    fake_http_for_names,
    write_hermes_home,
    write_quota_config_path,
)

NOW = 1_800_000.0
GROK_RESET = 535_958.0
CODEX_RESET = 592_315.0
DAY = 86400


def _tied_names() -> dict[str, str]:
    # both round up to "7d left" (6.20d and 6.86d) — the precision-loss case
    names = default_channel_names()
    names["codex"] = f"Codex: 100% {BULLET} 7d left"
    names["grok"] = f"Grok: 99% {BULLET} 7d left"
    return names


def _precise_fixture() -> dict[str, dict[str, object]]:
    return {
        "codex": {"pct": 100, "reset_seconds": CODEX_RESET, "label": "Codex"},
        "grok": {"pct": 99, "reset_seconds": GROK_RESET, "label": "Grok"},
    }


def _setup_reorder(monkeypatch, tmp_path: Path) -> Path:
    # config order puts codex ahead of grok, so the name-parsed 604800s tie
    # keeps that order — only precise seconds can swap them
    write_hermes_home(
        tmp_path,
        fallback_providers=[
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
        ],
    )
    quota_config = tmp_path / "quota-config.yaml"
    write_quota_config_path(quota_config)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return quota_config


def _desired_providers(result: dict) -> list[str]:
    return [entry["provider"] for entry in result["desired_entries"]]


class TestPreciseStateTieBreak:
    def test_both_7d_left_but_precise_seconds_put_grok_first(
        self, monkeypatch, tmp_path: Path
    ):
        quota_config = _setup_reorder(monkeypatch, tmp_path)
        save_state(_precise_fixture(), now_fn=lambda: NOW)

        result = run_reorder(
            config_path=quota_config,
            dry_run=True,
            http_fn=fake_http_for_names(_tied_names()),
            now_fn=lambda: NOW,
        )

        assert _desired_providers(result) == [
            "openrouter",
            "xai-oauth",
            "openai-codex",
        ]
        assert result["readings"]["xai-oauth"].reset_seconds == GROK_RESET
        assert result["readings"]["openai-codex"].reset_seconds == CODEX_RESET
        assert result["would_change"] is True


class TestMissingStateFallsBackToNames:
    def test_no_state_file_keeps_name_parsed_tie_order(
        self, monkeypatch, tmp_path: Path
    ):
        quota_config = _setup_reorder(monkeypatch, tmp_path)
        assert not (tmp_path / STATE_FILENAME).exists()

        result = run_reorder(
            config_path=quota_config,
            dry_run=True,
            http_fn=fake_http_for_names(_tied_names()),
            now_fn=lambda: NOW,
        )

        assert _desired_providers(result) == [
            "openrouter",
            "openai-codex",
            "xai-oauth",
        ]
        assert result["readings"]["openai-codex"].reset_seconds == 7 * DAY
        assert result["readings"]["xai-oauth"].reset_seconds == 7 * DAY
        assert result["would_change"] is False


class TestStaleStateFallsBackToNames:
    def test_state_older_than_two_intervals_is_ignored(
        self, monkeypatch, tmp_path: Path
    ):
        quota_config = _setup_reorder(monkeypatch, tmp_path)
        save_state(_precise_fixture(), now_fn=lambda: NOW - 2 * 1800 - 1)

        result = run_reorder(
            config_path=quota_config,
            dry_run=True,
            http_fn=fake_http_for_names(_tied_names()),
            now_fn=lambda: NOW,
        )

        assert _desired_providers(result) == [
            "openrouter",
            "openai-codex",
            "xai-oauth",
        ]
        assert result["readings"]["xai-oauth"].reset_seconds == 7 * DAY
        assert result["readings"]["openai-codex"].reset_seconds == 7 * DAY
        assert result["would_change"] is False


class TestLoadPreciseReadings:
    def test_fresh_at_exactly_two_intervals(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(_precise_fixture(), now_fn=lambda: NOW)
        assert load_precise_readings(1800, now_fn=lambda: NOW + 2 * 1800) == {
            "codex": (100, CODEX_RESET),
            "grok": (99, GROK_RESET),
        }

    def test_corrupt_state_file_returns_empty(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / STATE_FILENAME).write_text("((", encoding="utf-8")
        assert load_precise_readings(1800, now_fn=lambda: NOW) == {}

    def test_state_without_readings_key_returns_empty(
        self, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(now_fn=lambda: NOW)
        assert load_precise_readings(1800, now_fn=lambda: NOW) == {}

    def test_non_numeric_entries_are_skipped(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        state = {
            "last_quota_success": int(NOW),
            "readings": {
                "codex": {"pct": 100, "reset_seconds": CODEX_RESET, "label": "Codex"},
                "grok": {"pct": "lots", "reset_seconds": GROK_RESET, "label": "Grok"},
                "kimi": "not-a-mapping",
            },
        }
        (tmp_path / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
        assert load_precise_readings(1800, now_fn=lambda: NOW) == {
            "codex": (100, CODEX_RESET)
        }


class TestPerProviderFallback:
    def test_incomplete_state_mixes_precise_and_name_parsed(self):
        readings = readings_from_names(_tied_names(), {"codex": (100, CODEX_RESET)})
        assert readings["openai-codex"].reset_seconds == CODEX_RESET
        assert readings["xai-oauth"].reset_seconds == 7 * DAY

    def test_precise_state_scores_strictly_unparseable_names(self):
        names = _tied_names()
        names["grok"] = "Grok: quota paused"
        readings = readings_from_names(names, {"grok": (99, GROK_RESET)})
        assert readings["xai-oauth"].reset_seconds == GROK_RESET
        assert readings["xai-oauth"].pct == 99


class TestQuotaChannelsStateRoundTrip:
    def test_save_state_round_trips_readings(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        fixture = _precise_fixture()

        ts = save_state(fixture, now_fn=lambda: NOW)

        assert ts == int(NOW)
        assert load_state() == {
            "last_quota_success": int(NOW),
            "readings": fixture,
        }

    def test_save_state_without_readings_keeps_legacy_shape(
        self, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(now_fn=lambda: NOW)
        assert load_state() == {"last_quota_success": int(NOW)}


class TestRunTickPersistsReadings:
    def test_successful_providers_written_failures_absent(
        self, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "ch1", "cursor": "ch2", "kimi": "ch3"},
                "enabled_providers": ["codex", "cursor", "kimi"],
            }
        )

        def fake_run_provider(key, channel_id, headers, http_fn=None, now_fn=None):
            if key == "kimi":
                raise QuotaChannelsError("kimi boom")
            if key == "cursor":
                return "Cursor", 2_000_000.0, "Cursor: 90%/85% • 25d left", "renamed", {}
            return "Codex", CODEX_RESET, "Codex: 100% • 7d left", "renamed", {}

        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.update_category", lambda *a, **k: "renamed"
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.discord_headers",
            lambda: {"Authorization": "Bot x"},
        )

        result = run_tick(
            config, force=True, sleep_fn=lambda _: None, now_fn=lambda: NOW
        )

        assert result["success"] is True
        state = json.loads(
            (tmp_path / STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert state["last_quota_success"] == int(NOW)
        assert state["readings"] == {
            "codex": {"pct": 100, "reset_seconds": CODEX_RESET, "label": "Codex"},
            # cursor scored at the weaker of auto/api remaining
            "cursor": {"pct": 85, "reset_seconds": 2_000_000.0, "label": "Cursor"},
        }
