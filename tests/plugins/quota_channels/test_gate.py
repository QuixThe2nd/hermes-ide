"""Quota gate logic tests."""

from __future__ import annotations

import json

import pytest

from plugins.quota_channels.core import (
    QuotaChannelsError,
    quota_due,
    run_tick,
    save_state,
    state_path,
    validate_quota_config,
)


def _minimal_config():
    return validate_quota_config(
        {
            "guild_id": "guild",
            "category_id": "cat",
            "channel_ids": {"codex": "ch1"},
            "enabled_providers": ["codex"],
        }
    )


class TestQuotaDue:
    def test_force_overrides_fresh_state(self):
        assert quota_due({"last_quota_success": 9999999999}, 1800, force=True) is True

    def test_fresh_state_skips(self):
        now = 1_000_000.0
        assert (
            quota_due({"last_quota_success": int(now - 10)}, 1800, force=False, now_fn=lambda: now)
            is False
        )

    def test_stale_state_runs(self):
        now = 1_000_000.0
        assert (
            quota_due({"last_quota_success": int(now - 2000)}, 1800, force=False, now_fn=lambda: now)
            is True
        )

    def test_missing_state_runs(self):
        assert quota_due({}, 1800, force=False) is True

    def test_corrupt_state_runs(self):
        assert quota_due({"last_quota_success": "bad"}, 1800, force=False) is True


class TestRunTickGate:
    def test_category_only_when_gate_closed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        state_path().write_text(json.dumps({"last_quota_success": 1_000_000}))
        calls = {"providers": 0, "category": 0}

        def fake_run_provider(*args, **kwargs):
            return "Codex", 1, "Codex: 99% \u2022 1d left", "renamed", {}

        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )

        def fake_update_category(*args, **kwargs):
            calls["category"] += 1
            return "renamed"

        monkeypatch.setattr(
            "plugins.quota_channels.core.update_category", fake_update_category
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.discord_headers", lambda: {"Authorization": "Bot x"}
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 1_000_000},
        )

        config = _minimal_config()
        now = 1_000_010.0
        result = run_tick(
            config,
            force=False,
            now_fn=lambda: now,
            sleep_fn=lambda _: None,
        )
        assert result["did_quota"] is False
        assert calls["providers"] == 0
        assert calls["category"] == 1

    def test_force_runs_quota(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        calls = {"providers": 0}

        def fake_run_provider(*args, **kwargs):
            calls["providers"] += 1
            return "Codex", 2, "Codex: 50% \u2022 2d left", "renamed", {}

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
            "plugins.quota_channels.core.discord_headers", lambda: {"Authorization": "Bot x"}
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 123
        )

        result = run_tick(
            _minimal_config(),
            force=True,
            sleep_fn=lambda _: None,
        )
        assert result["did_quota"] is True
        assert calls["providers"] == 1

    def test_single_category_update_per_tick(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        slept = []
        category_calls = []

        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota",
            lambda *a, **k: ("Codex", 1, "Codex: 1% \u2022 1d left", "renamed", {}),
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", lambda *a, **k: False
        )

        def fake_update_category(*args, **kwargs):
            category_calls.append((args, kwargs))
            return "renamed"

        monkeypatch.setattr(
            "plugins.quota_channels.core.update_category", fake_update_category
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.discord_headers", lambda: {"Authorization": "Bot x"}
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state", lambda *args, **kwargs: 1
        )

        config = _minimal_config()
        config["post_quota_delay_seconds"] = 7

        run_tick(config, force=True, sleep_fn=lambda s: slept.append(s))
        assert len(category_calls) == 1
        assert slept == []

        category_calls.clear()
        monkeypatch.setattr(
            "plugins.quota_channels.core.load_state",
            lambda: {"last_quota_success": 999_999_999},
        )
        run_tick(
            config,
            force=False,
            sleep_fn=lambda s: slept.append(s),
            now_fn=lambda: 1_000_000_000.0,
        )
        assert len(category_calls) == 1
        assert slept == []

    def test_mixed_provider_failure_isolation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "ch1", "kimi": "ch2"},
                "enabled_providers": ["codex", "kimi"],
            }
        )

        def fake_run_provider(key, channel_id, headers, http_fn=None, now_fn=None):
            if key == "codex":
                return "Codex", 2, "Codex: 50% \u2022 2d left", "renamed", {}
            raise QuotaChannelsError("kimi boom")

        sort_entries = []

        def fake_sort(cfg, entries, headers, http_fn=None):
            sort_entries.extend(entries)
            return False

        save_calls = []
        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels", fake_sort
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.update_category", lambda *a, **k: "renamed"
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.discord_headers", lambda: {"Authorization": "Bot x"}
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state",
            lambda *args, **kwargs: save_calls.append(True) or 123,
        )

        result = run_tick(config, force=True, sleep_fn=lambda _: None)
        assert result["success"] is True
        assert result["providers"]["Codex"]["remaining"] == 50
        assert "kimi boom" in result["providers"]["Kimi"]["error"]
        assert save_calls == [True]
        assert len(sort_entries) == 1
        assert sort_entries[0][0] == "Codex"

    def test_all_providers_fail_skips_state_and_sort(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        def fake_run_provider(*args, **kwargs):
            raise QuotaChannelsError("solo boom")

        sort_called = []
        save_called = []
        monkeypatch.setattr(
            "plugins.quota_channels.core.run_provider_quota", fake_run_provider
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.sort_voice_channels",
            lambda *a, **k: sort_called.append(True) or False,
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.update_category", lambda *a, **k: "renamed"
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.discord_headers", lambda: {"Authorization": "Bot x"}
        )
        monkeypatch.setattr(
            "plugins.quota_channels.core.save_state",
            lambda *args, **kwargs: save_called.append(True) or 123,
        )

        result = run_tick(_minimal_config(), force=True, sleep_fn=lambda _: None)
        assert result["success"] is True
        assert "solo boom" in result["providers"]["Codex"]["error"]
        assert save_called == []
        assert sort_called == []
        assert result["sorted"] is False
