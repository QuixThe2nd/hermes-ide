"""Desired fallback order ranking contracts."""

from __future__ import annotations

import pytest

from plugins.fallback_quota_reorder.core import (
    QuotaReading,
    REFERENCE_HOURS,
    compute_desired_order,
    reading_for_entry,
    score_provider,
)
from plugins.fallback_quota_reorder.reliability import ReliabilityRates


def _reading(provider: str, pct: int, reset_seconds: int, channel_key: str) -> QuotaReading:
    return QuotaReading(
        channel_key=channel_key,
        provider=provider,
        channel_name="unused",
        pct=pct,
        reset_seconds=reset_seconds,
    )


class TestScoreProvider:
    def test_quota_divided_by_hours_remaining(self):
        reading = _reading("xai-oauth", 50, 7200, "grok")
        assert score_provider(reading) == 42.0  # 0.5 * 168/2

    def test_reliability_multiplies(self):
        reading = _reading("xai-oauth", 100, 3600, "grok")
        rates = ReliabilityRates(rate_24h=0.5, rate_1h=0.5)
        assert score_provider(reading, rates) == 42.0  # 1.0 * 168 * 0.25

    def test_unknown_reliability_is_neutral(self):
        reading = _reading("xai-oauth", 100, 3600, "grok")
        assert score_provider(reading) == REFERENCE_HOURS

    def test_zero_hours_floors_to_one_minute(self):
        reading = _reading("xai-oauth", 100, 0, "grok")
        assert score_provider(reading) == REFERENCE_HOURS * 60.0

    def test_reference_horizon_scores_quota_one_to_one(self):
        reading = _reading("xai-oauth", 70, int(REFERENCE_HOURS * 3600), "grok")
        assert score_provider(reading) == 0.7

    def test_reset_later_than_reference_scores_below_quota_frac(self):
        # 25d out: 0.85 * 168/600 — time still dilutes, never amplifies
        reading = _reading("cursor", 85, 25 * 86400, "cursor")
        assert score_provider(reading) == pytest.approx(0.85 * REFERENCE_HOURS / 600.0)


class TestScoreMonotonicity:
    """The three direction invariants of the score."""

    def test_shorter_time_until_reset_increases_score(self):
        pct, rates = 80, ReliabilityRates(rate_24h=0.9, rate_1h=0.8)
        scores = [
            score_provider(_reading("zai", pct, reset, "zai"), rates)
            for reset in (25 * 86400, 7 * 86400, 86400, 7200, 3600, 60, 0)
        ]
        # inverse time: the 25d -> 0 sequence is strictly increasing
        assert scores == sorted(scores)
        assert scores[0] < scores[-1]

    def test_less_remaining_quota_decreases_score(self):
        reset, rates = 4 * 3600, ReliabilityRates(rate_24h=0.7, rate_1h=1.0)
        scores = [
            score_provider(_reading("zai", pct, reset, "zai"), rates)
            for pct in (100, 80, 55, 30, 5, 0)
        ]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] > scores[-1]

    def test_worse_uptime_decreases_score(self):
        reading = _reading("zai", 90, 2 * 3600, "zai")
        assert score_provider(
            reading, ReliabilityRates(rate_24h=1.0, rate_1h=1.0)
        ) > score_provider(reading, ReliabilityRates(rate_24h=0.9, rate_1h=1.0))
        assert score_provider(
            reading, ReliabilityRates(rate_24h=0.9, rate_1h=1.0)
        ) > score_provider(reading, ReliabilityRates(rate_24h=0.9, rate_1h=0.4))
        assert score_provider(
            reading, ReliabilityRates(rate_24h=0.1, rate_1h=0.1)
        ) < score_provider(reading, ReliabilityRates(rate_24h=1.0, rate_1h=1.0))


class TestComputeDesiredOrder:
    def test_plain_openrouter_is_not_first_anymore(self):
        # no provider-wide openrouter precedence: without a reading it is an
        # ordinary unscored tail entry
        entries = [
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "openrouter", "model": "or"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        readings = {
            "xai-oauth": _reading("xai-oauth", 90, 3600, "grok"),
            "kimi-coding": _reading("kimi-coding", 90, 1800, "kimi"),
        }
        ordered = compute_desired_order(entries, readings)
        assert ordered[-1]["provider"] == "openrouter"
        # sooner reset wins: kimi 0.9*(168/0.5)=302.4 beats grok 0.9*168=151.2
        assert ordered[0]["provider"] == "kimi-coding"

    def test_healthy_entries_sort_by_soonest_reset(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        readings = {
            "openai-codex": _reading("openai-codex", 80, 86400, "codex"),
            "xai-oauth": _reading("xai-oauth", 70, 3600, "grok"),
            "kimi-coding": _reading("kimi-coding", 60, 7200, "kimi"),
        }
        ordered = compute_desired_order(entries, readings)
        providers = [entry["provider"] for entry in ordered]
        # 0.7*168=117.6, 0.6*84=50.4, 0.8*7=5.6; unscored openrouter tails
        assert providers == [
            "xai-oauth",
            "kimi-coding",
            "openai-codex",
            "openrouter",
        ]

    def test_low_pct_sinks_behind_all_healthy_entries(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "zai", "model": "zai"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        readings = {
            "zai": _reading("zai", 3, 1800, "zai"),
            "xai-oauth": _reading("xai-oauth", 50, 7200, "grok"),
        }
        ordered = compute_desired_order(entries, readings)
        providers = [entry["provider"] for entry in ordered]
        assert providers.index("xai-oauth") < providers.index("zai")

    def test_unreadable_entries_keep_relative_tail_order(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "custom-a", "model": "a"},
            {"provider": "custom-b", "model": "b"},
        ]
        readings = {
            "xai-oauth": _reading("xai-oauth", 90, 3600, "grok"),
        }
        ordered = compute_desired_order(entries, readings)
        providers = [entry["provider"] for entry in ordered]
        assert providers == ["xai-oauth", "openrouter", "custom-a", "custom-b"]

    def test_stability_preserves_original_index_on_ties(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex-a"},
            {"provider": "xai-oauth", "model": "grok-a"},
            {"provider": "kimi-coding", "model": "kimi-a"},
            {"provider": "zai", "model": "zai-a"},
        ]
        reset = 7200
        readings = {
            "openai-codex": _reading("openai-codex", 90, reset, "codex"),
            "xai-oauth": _reading("xai-oauth", 90, reset, "grok"),
            "kimi-coding": _reading("kimi-coding", 90, reset, "kimi"),
            "zai": _reading("zai", 90, reset, "zai"),
        }
        ordered = compute_desired_order(entries, readings)
        tied = [
            entry["provider"]
            for entry in ordered
            if entry["provider"] != "openrouter"
        ]
        assert tied == ["openai-codex", "xai-oauth", "kimi-coding", "zai"]

    def test_recent_failures_can_outrank_a_fatter_wallet(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        readings = {
            "openai-codex": _reading("openai-codex", 100, 86400, "codex"),
            "xai-oauth": _reading("xai-oauth", 50, 86400, "grok"),
        }
        reliability = {
            "openai-codex": ReliabilityRates(rate_24h=0.10, rate_1h=0.10),
            "xai-oauth": ReliabilityRates(rate_24h=1.0, rate_1h=1.0),
        }
        ordered = compute_desired_order(entries, readings, reliability=reliability)
        providers = [entry["provider"] for entry in ordered]
        # Codex 7*1.0*0.1*0.1=0.07; Grok 7*0.5=3.5
        assert providers == ["xai-oauth", "openai-codex", "openrouter"]


class TestRetiredOxAlphaRouteNotSynthesized:
    """The retired openrouter/stealth/ox-alpha route is never synthesized.

    It once scored through a synthetic 100%/168h wallet; now, like every
    other openrouter model, it has no reading of its own and tails unscored.
    """

    def test_no_synthetic_reading_for_the_exact_route(self):
        assert (
            reading_for_entry(
                {"provider": "openrouter", "model": "stealth/ox-alpha"}, {}
            )
            is None
        )
        # case variants of the retired pair match nothing either
        assert (
            reading_for_entry(
                {"provider": "OpenRouter", "model": "Stealth/OX-Alpha"}, {}
            )
            is None
        )

    def test_retired_route_stays_in_the_stable_unscored_tail(self):
        # regression: an unscored OpenRouter route never gains synthetic
        # quota — it keeps its relative order behind every scored entry
        entries = [
            {"provider": "openrouter", "model": "stealth/ox-alpha"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "custom-a", "model": "a"},
        ]
        readings = {"openai-codex": _reading("openai-codex", 90, 3600, "codex")}
        ordered = compute_desired_order(entries, readings)
        assert [entry["provider"] for entry in ordered] == [
            "openai-codex",
            "openrouter",
            "custom-a",
        ]

    def test_retired_route_loses_to_any_scored_provider_even_at_neutral(self):
        # the retired route once scored exactly 1.0 at the reference horizon
        # and beat a 0.9 codex; a real 0.9 wallet now wins outright
        entries = [
            {"provider": "openrouter", "model": "stealth/ox-alpha"},
            {"provider": "openai-codex", "model": "codex"},
        ]
        readings = {
            "openai-codex": _reading(
                "openai-codex", 90, int(REFERENCE_HOURS * 3600), "codex"
            )
        }
        reliability = {
            "openrouter": ReliabilityRates(rate_24h=0.4, rate_1h=1.0),
        }
        ordered = compute_desired_order(entries, readings, reliability=reliability)
        assert [entry["provider"] for entry in ordered] == [
            "openai-codex",
            "openrouter",
        ]

    def test_real_reading_still_scores_any_provider(self):
        # readings are keyed by provider alone: a genuine openrouter reading
        # (whenever one exists) still scores, model string irrelevant
        real = _reading("openrouter", 50, 3600, "grok")
        assert (
            reading_for_entry(
                {"provider": "openrouter", "model": "stealth/ox-alpha"},
                {"openrouter": real},
            )
            is real
        )

    def test_other_openrouter_models_stay_unscored(self):
        assert (
            reading_for_entry({"provider": "openrouter", "model": "stealth/other"}, {})
            is None
        )
        entries = [
            {"provider": "openrouter", "model": "stealth/other"},
            {"provider": "openai-codex", "model": "codex"},
        ]
        readings = {"openai-codex": _reading("openai-codex", 90, 3600, "codex")}
        ordered = compute_desired_order(entries, readings)
        assert [entry["provider"] for entry in ordered] == [
            "openai-codex",
            "openrouter",
        ]
