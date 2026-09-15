"""Models-category behavior: score-based channel ordering (same policy as
fallback_quota_reorder), the retired OpenRouter row staying absent, and
backward-compatible config."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

import pytest

from plugins.fallback_quota_reorder.reliability import ReliabilityRates
from plugins.quota_channels import core as core
from plugins.quota_channels.core import (
    PROVIDER_SPECS,
    category_name,
    fmt_time,
    fmt_ts,
    plan_position_moves,
    quota_display_ranks,
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

    def test_openrouter_channel_id_no_longer_activates_a_row(self):
        # the retired virtual row auto-enabled as soon as its channel ID was
        # wired; the leftover key is now inert — no row, no rename, no score
        section = _five_id_section()
        section["channel_ids"]["openrouter"] = "c6"
        config = validate_quota_config(section)
        assert [key for key, _, _ in config["providers"]] == [
            "codex",
            "kimi",
            "zai",
            "cursor",
            "grok",
        ]
        assert "openrouter" not in config["channel_ids"]

    def test_explicit_provider_list_still_controls_rows(self):
        section = _five_id_section()
        section["enabled_providers"] = ["codex", "kimi"]
        config = validate_quota_config(section)
        assert [key for key, _, _ in config["providers"]] == ["codex", "kimi"]

    def test_explicit_provider_map_cannot_resurrect_openrouter(self):
        # even an explicit enablement of the retired key activates nothing
        section = _five_id_section()
        section["channel_ids"]["openrouter"] = "c6"
        section["enabled_providers"] = {
            "codex": True,
            "kimi": True,
            "zai": True,
            "cursor": True,
            "grok": True,
            "openrouter": True,
        }
        config = validate_quota_config(section)
        assert [key for key, _, _ in config["providers"]] == [
            "codex",
            "kimi",
            "zai",
            "cursor",
            "grok",
        ]
        assert "openrouter" not in config["channel_ids"]


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
        }
        ranks = quota_display_ranks(readings)
        assert sorted(readings, key=lambda key: ranks[key]) == [
            "grok",  # 0.81 * 168/120 = 1.134
            "codex",  # 0.95 * 1.0 = 0.95
            "zai",  # 0.23 * 168/72 = 0.537
            "cursor",  # 0.38 * 168/528 = 0.121
            "kimi",  # low-quota bucket, after all healthy entries
        ]

    def test_uptime_can_derate_a_fatter_wallet_below_a_healthier_model(self):
        readings = {
            "grok": _reading(100, WEEK),
            "codex": _reading(95, WEEK),
        }
        # reliability is keyed by routing slug: the grok channel derates
        # through xai-oauth's uptime, not a literal "grok" key
        reliability = {"xai-oauth": ReliabilityRates(rate_24h=0.4, rate_1h=1.0)}
        ranks = quota_display_ranks(readings, reliability)
        # derated grok 1.0*0.4 = 0.4 loses to codex's neutral 0.95
        assert ranks["codex"] < ranks["grok"]

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
        # a leftover retired-row channel id must activate nothing
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
                # c6 (the retired OpenRouter row) is not a guild channel —
                # the tick neither renames nor repositions it
            ]
        )
        now = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        result = run_tick(
            wired, force=True, now_fn=lambda: now, http_fn=discord, sleep_fn=lambda _: None
        )

        assert result["success"] is True
        # no OpenRouter row exists to report, rename, or persist
        assert "OpenRouter" not in result["providers"]
        renames = dict(discord.renames)
        assert "c6" not in renames
        assert renames["c5"] == {"name": "Grok: 81% • 5d left • 0 resets"}
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert set(state["readings"]) == {"codex", "kimi", "zai", "cursor", "grok"}
        assert state["readings"]["grok"]["pct"] == 81
        assert state["readings"]["grok"]["reset_seconds"] == 5 * DAY
        assert state["readings"]["grok"]["label"] == "Grok"
        # Category label carries the Models prefix.
        assert renames["cat"]["name"].startswith("Models • ")
        # Channels land in the score order, healthiest slot first.
        assert result["sorted"] is True
        assert discord.position_patch == [
            {"id": "c5", "position": 10},  # grok 1.134
            {"id": "c1", "position": 11},  # codex 0.95
            {"id": "c4", "position": 13},  # cursor 0.121
            {"id": "c2", "position": 15},  # kimi: low-quota bucket
        ]  # z.ai keeps slot 12 — already in place

    def test_no_moves_when_positions_already_match(self, wired):
        discord = _FakeDiscord(
            [
                {"id": "c5", "position": 10},
                {"id": "c1", "position": 11},
                {"id": "c3", "position": 12},
                {"id": "c4", "position": 13},
                {"id": "c2", "position": 15},
            ]
        )
        now = datetime(2026, 8, 25, 14, 0, 0).timestamp()
        result = run_tick(
            wired, force=True, now_fn=lambda: now, http_fn=discord, sleep_fn=lambda _: None
        )
        assert result["sorted"] is False
        assert discord.position_patch is None

    def test_current_shape_kimi_response_scores_into_position(
        self, monkeypatch, tmp_path
    ):
        # Live dogfood shape: usage carries numeric-string limit/used and no
        # legacy `remaining`. Kimi must parse, rename, and land a scored
        # position instead of surfacing a provider error.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text('KIMI_API_KEY="kimi-key"\n', encoding="utf-8")
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        # grok joins as a plain stubbed row; kimi keeps its real HTTP path
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            dict(
                core.QUOTA_METRICS,
                grok=lambda http_fn=None, now_fn=None: (81, 5 * DAY),
            ),
        )
        reset = datetime(2026, 8, 26, 15, 0, 0, tzinfo=timezone.utc)
        now = reset.timestamp() - 25 * 3600
        discord = _FakeDiscord(
            [
                {"id": "c5", "position": 10},
                {"id": "c2", "position": 11},
            ]
        )

        def fake_http(req, timeout=25.0):
            if "kimi.com" in req.full_url:
                body = json.dumps(
                    {
                        "usage": {
                            "limit": "100",
                            "used": "40",
                            "resetTime": reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    }
                ).encode()
                return 200, body
            return discord(req, timeout)

        config = validate_quota_config(
            {
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"kimi": "c2", "grok": "c5"},
                "enabled_providers": ["kimi", "grok"],
            }
        )
        result = run_tick(
            config,
            force=True,
            now_fn=lambda: now,
            http_fn=fake_http,
            sleep_fn=lambda _: None,
        )

        assert result["providers"]["Kimi"] == {
            "remaining": 60,
            "reset_seconds": 25 * 3600.0,
            "rename": "renamed",
        }
        renames = dict(discord.renames)
        assert renames["c2"] == {"name": "Kimi: 60% • 25h left"}
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["kimi"] == {
            "pct": 60,
            "reset_seconds": 25 * 3600.0,
            "label": "Kimi",
        }
        # 60% resetting in 25h scores 0.6 × 168/25 = 4.03, beating grok's
        # 0.81 × 168/120 = 1.13, so Kimi takes the best slot.
        assert result["sorted"] is True
        assert discord.position_patch == [
            {"id": "c2", "position": 10},
            {"id": "c5", "position": 11},
        ]


class TestPlanPositionMovesByRank:
    def test_ascending_rank_takes_the_lowest_slot(self):
        entries = [
            ("Kimi", "c2", 1e9 - 0.01),  # low-quota bucket
            ("Grok", "c5", -1.13),
            ("z.ai", "c3", -1.0),
        ]
        guild_channels = [
            {"id": "c2", "position": 2},
            {"id": "c5", "position": 3},
            {"id": "c3", "position": 1},
        ]
        assert plan_position_moves(entries, guild_channels) == [
            {"id": "c5", "position": 1},
            {"id": "c3", "position": 2},
            {"id": "c2", "position": 3},
        ]
