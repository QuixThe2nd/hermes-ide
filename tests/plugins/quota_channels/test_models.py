"""Models-category behavior: the virtual OpenRouter row, score-based channel
ordering (same policy as fallback_quota_reorder), and backward-compatible
config."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime

import pytest

from plugins.fallback_quota_reorder.reliability import ReliabilityRates
from plugins.quota_channels import core as core
from plugins.quota_channels.core import (
    OPENROUTER_RESET_SECONDS,
    PROVIDER_SPECS,
    category_name,
    fmt_time,
    fmt_ts,
    format_openrouter_name,
    plan_position_moves,
    quota_display_ranks,
    run_provider_quota,
    run_tick,
    state_path,
    validate_quota_config,
)

DAY = 86400
WEEK = 7 * DAY


def _reading(pct: int, reset_seconds: float) -> dict:
    return {"pct": pct, "reset_seconds": reset_seconds, "label": "unused"}


def _five_id_section() -> dict:
    return {
        "guild_id": "guild",
        "category_id": "cat",
        "channel_ids": {
            "codex": "c1",
            "kimi": "c2",
            "zai": "c3",
            "cursor": "c4",
            "grok": "c5",
        },
    }


# ---------------------------------------------------------------------------
# Backward-compatible config
# ---------------------------------------------------------------------------


class TestConfigCompatibility:
    def test_legacy_five_id_config_without_enabled_providers_still_valid(self):
        config = validate_quota_config(_five_id_section())
        assert [key for key, _, _ in config["providers"]] == [
            "codex",
            "kimi",
            "zai",
            "cursor",
            "grok",
        ]
        assert "openrouter" not in config["channel_ids"]

    def test_openrouter_auto_enables_when_its_channel_id_exists(self):
        section = _five_id_section()
        section["channel_ids"]["openrouter"] = "c6"
        config = validate_quota_config(section)
        assert [key for key, _, _ in config["providers"]] == [
            "codex",
            "kimi",
            "zai",
            "cursor",
            "grok",
            "openrouter",
        ]
        assert config["channel_ids"]["openrouter"] == "c6"

    def test_explicit_provider_list_still_controls_openrouter(self):
        section = _five_id_section()
        section["channel_ids"]["openrouter"] = "c6"
        section["enabled_providers"] = ["codex", "kimi"]
        config = validate_quota_config(section)
        assert [key for key, _, _ in config["providers"]] == ["codex", "kimi"]

    def test_explicit_provider_map_can_disable_openrouter(self):
        section = _five_id_section()
        section["channel_ids"]["openrouter"] = "c6"
        section["enabled_providers"] = {
            "codex": True,
            "kimi": True,
            "zai": True,
            "cursor": True,
            "grok": True,
            "openrouter": False,
        }
        config = validate_quota_config(section)
        assert [key for key, _, _ in config["providers"]] == [
            "codex",
            "kimi",
            "zai",
            "cursor",
            "grok",
        ]


# ---------------------------------------------------------------------------
# Dynamic category naming
# ---------------------------------------------------------------------------


class TestModelsCategoryNaming:
    def test_prefix_is_models_with_timestamp_and_next(self):
        last = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        now = last + 60
        expected = f"Models • {fmt_ts(last)} • Next: {fmt_time(last + 1800)}"
        assert category_name(last, 1800, now_fn=lambda: now) == expected

    def test_never_updated_is_models_never(self):
        assert (
            category_name(0, 1800, now_fn=lambda: 1_000_000.0)
            == "Models • never • Next: Due"
        )

    def test_due_boundary_is_models_due(self):
        last = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        name = category_name(last, 1800, now_fn=lambda: last + 1800)
        assert name == f"Models • {fmt_ts(last)} • Next: Due"


# ---------------------------------------------------------------------------
# The virtual OpenRouter row
# ---------------------------------------------------------------------------


class TestVirtualOpenRouterRow:
    def test_managed_name_is_static_full_wallet(self):
        assert format_openrouter_name() == "OpenRouter: 100% • Unlimited"

    def test_no_quota_fetch_only_discord_renames(self):
        requests = []

        def fake_http(req, timeout=25.0):
            url = req.full_url
            assert "discord.com" in url, f"non-discord request: {url}"
            requests.append(url)
            method = getattr(req, "method", None) or "GET"
            if method == "GET":
                return 200, json.dumps({"name": "OpenRouter: 41% • 2h left"}).encode()
            return 200, json.dumps({"name": "patched"}).encode()

        label, reset_secs, name, rename, token_info = run_provider_quota(
            "openrouter", "c6", {"Authorization": "Bot x"}, http_fn=fake_http
        )
        assert label == "OpenRouter"
        assert reset_secs == float(OPENROUTER_RESET_SECONDS)
        assert name == "OpenRouter: 100% • Unlimited"
        assert rename == "renamed"
        assert token_info == {}
        assert requests  # only Discord channel GET/PATCH happened


# ---------------------------------------------------------------------------
# Score ordering (shared fallback policy)
# ---------------------------------------------------------------------------


class TestScoreOrdering:
    def test_current_expected_example(self):
        # Every provider neutral uptime; the current primary (say Codex)
        # stays in the display and simply sorts by its own score.
        readings = {
            "codex": _reading(95, WEEK),
            "kimi": _reading(1, 5 * DAY),  # <5%: low-quota bucket
            "zai": _reading(23, 3 * DAY),
            "cursor": _reading(38, 22 * DAY),
            "grok": _reading(81, 5 * DAY),
            "openrouter": _reading(100, WEEK),
        }
        ranks = quota_display_ranks(readings)
        assert sorted(readings, key=lambda key: ranks[key]) == [
            "grok",  # 0.81 * 168/120 = 1.134
            "openrouter",  # synthetic 100% @ 168h = 1.0
            "codex",  # 0.95 * 1.0 = 0.95
            "zai",  # 0.23 * 168/72 = 0.537
            "cursor",  # 0.38 * 168/528 = 0.121
            "kimi",  # low-quota bucket, after all healthy entries
        ]

    def test_uptime_can_derate_openrouter_below_a_healthier_model(self):
        readings = {
            "openrouter": _reading(100, WEEK),
            "codex": _reading(95, WEEK),
        }
        reliability = {"openrouter": ReliabilityRates(rate_24h=0.4, rate_1h=1.0)}
        ranks = quota_display_ranks(readings, reliability)
        # derated ox-alpha 1.0*0.4 = 0.4 loses to codex's neutral 0.95
        assert ranks["codex"] < ranks["openrouter"]

    def test_low_quota_bucket_sinks_behind_every_healthy_entry(self):
        readings = {
            "kimi": _reading(4, 60),  # would score ~403 if healthy
            "cursor": _reading(38, 22 * DAY),  # weakest healthy score 0.121
        }
        ranks = quota_display_ranks(readings)
        assert ranks["cursor"] < ranks["kimi"]

    def test_equal_scores_keep_spec_order(self):
        readings = {
            key: _reading(90, 7200) for key, _ in PROVIDER_SPECS
        }
        ranks = quota_display_ranks(readings)
        assert sorted(readings, key=lambda key: ranks[key]) == [
            key for key, _ in PROVIDER_SPECS
        ]

    def test_reliability_maps_quota_keys_to_routing_providers(self):
        # zai uptime must derate the z.ai channel, not some other slug.
        readings = {"zai": _reading(100, WEEK), "codex": _reading(100, WEEK)}
        reliability = {"zai": ReliabilityRates(rate_24h=0.5, rate_1h=1.0)}
        ranks = quota_display_ranks(readings, reliability)
        assert ranks["codex"] < ranks["zai"]


# ---------------------------------------------------------------------------
# Full tick: names, reading, category label, and position moves
# ---------------------------------------------------------------------------


class _FakeDiscord:
    """Discord transport for run_tick: names, renames, guild positions."""

    def __init__(self, guild_channels):
        self.guild_channels = guild_channels
        self.renames = []  # (channel_id, body)
        self.position_patch = None

    def __call__(self, req, timeout=25.0):
        assert "discord.com" in req.full_url, f"non-discord request: {req.full_url}"
        method = (getattr(req, "method", None) or req.get_method()).upper()
        path = urllib.parse.urlsplit(req.full_url).path
        body = json.loads(req.data.decode()) if req.data else None
        if method == "GET" and path.endswith("/channels"):
            return 200, json.dumps(self.guild_channels).encode()
        if method == "GET":
            return 200, json.dumps({"name": "old-name"}).encode()
        if method == "PATCH" and path.endswith("/channels"):
            self.position_patch = body
            return 200, b"[]"
        if method == "PATCH":
            self.renames.append((path.rsplit("/", 1)[-1], body))
            return 200, json.dumps({"name": body.get("name")}).encode()
        raise AssertionError((method, path))


class TestRunTickModelsIntegration:
    @pytest.fixture
    def wired(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {
                "codex": lambda http_fn=None, now_fn=None: (95, WEEK),
                "kimi": lambda http_fn=None, now_fn=None: (1, 5 * DAY),
                "zai": lambda http_fn=None, now_fn=None: (23, 3 * DAY),
                "cursor": lambda http_fn=None, now_fn=None: (38, 50, 22 * DAY),
                "grok": lambda http_fn=None, now_fn=None: (81, 5 * DAY),
            },
        )
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        section = _five_id_section()
        section["channel_ids"]["openrouter"] = "c6"
        return validate_quota_config(section)

    def test_names_reading_category_and_order(self, wired, tmp_path):
        discord = _FakeDiscord(
            [
                {"id": "c1", "position": 13},
                {"id": "c2", "position": 10},
                {"id": "c3", "position": 12},
                {"id": "c4", "position": 11},
                {"id": "c5", "position": 15},
                {"id": "c6", "position": 14},
            ]
        )
        now = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        result = run_tick(
            wired, force=True, now_fn=lambda: now, http_fn=discord, sleep_fn=lambda _: None
        )

        assert result["success"] is True
        # Virtual row: managed label, no metrics function was consulted.
        assert result["providers"]["OpenRouter"] == {
            "remaining": 100,
            "reset_seconds": float(OPENROUTER_RESET_SECONDS),
            "rename": "renamed",
        }
        renames = dict(discord.renames)
        assert renames["c6"] == {"name": "OpenRouter: 100% • Unlimited"}
        assert renames["c5"] == {"name": "Grok: 81% • 5d left"}
        # State reading for the virtual row is the synthetic full wallet.
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["openrouter"] == {
            "pct": 100,
            "reset_seconds": float(OPENROUTER_RESET_SECONDS),
            "label": "OpenRouter",
        }
        # Category label carries the Models prefix.
        assert renames["cat"]["name"].startswith("Models • ")
        # Channels land in the score order, healthiest slot first.
        assert result["sorted"] is True
        assert discord.position_patch == [
            {"id": "c5", "position": 10},  # grok 1.134
            {"id": "c6", "position": 11},  # openrouter 1.0
            {"id": "c1", "position": 12},  # codex 0.95
            {"id": "c3", "position": 13},  # z.ai 0.537
            {"id": "c4", "position": 14},  # cursor 0.121
            {"id": "c2", "position": 15},  # kimi: low-quota bucket
        ]

    def test_no_moves_when_positions_already_match(self, wired):
        discord = _FakeDiscord(
            [
                {"id": "c5", "position": 10},
                {"id": "c6", "position": 11},
                {"id": "c1", "position": 12},
                {"id": "c3", "position": 13},
                {"id": "c4", "position": 14},
                {"id": "c2", "position": 15},
            ]
        )
        now = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        result = run_tick(
            wired, force=True, now_fn=lambda: now, http_fn=discord, sleep_fn=lambda _: None
        )
        assert result["sorted"] is False
        assert discord.position_patch is None


class TestPlanPositionMovesByRank:
    def test_ascending_rank_takes_the_lowest_slot(self):
        entries = [
            ("Kimi", "c2", 1e9 - 0.01),  # low-quota bucket
            ("Grok", "c5", -1.13),
            ("OpenRouter", "c6", -1.0),
        ]
        guild_channels = [
            {"id": "c2", "position": 2},
            {"id": "c5", "position": 3},
            {"id": "c6", "position": 1},
        ]
        assert plan_position_moves(entries, guild_channels) == [
            {"id": "c5", "position": 1},
            {"id": "c6", "position": 2},
            {"id": "c2", "position": 3},
        ]
