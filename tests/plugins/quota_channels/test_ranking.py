"""Quota-channel spendability scoring contracts."""

from __future__ import annotations

import pytest

from plugins.quota_channels.reliability import ReliabilityRates
from plugins.quota_channels.scoring import (
    QuotaReading,
    REFERENCE_HOURS,
    is_low_quota,
    score_provider,
)


def _reading(
    provider: str,
    pct: int,
    reset_seconds: float,
    channel_key: str | None = None,
    *,
    reset_count: int = 0,
    reset_expiry_seconds: float | None = None,
    reset_expiry_horizons: tuple[float, ...] | None = None,
) -> QuotaReading:
    return QuotaReading(
        channel_key=channel_key or provider,
        provider=provider,
        channel_name="unused",
        pct=pct,
        reset_seconds=reset_seconds,
        reset_count=reset_count,
        reset_expiry_seconds=reset_expiry_seconds,
        reset_expiry_horizons=reset_expiry_horizons,
    )


WEEK = 7 * 86400


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


class TestOneResetEqualsAFullWallet:
    def test_zero_pct_one_reset_equals_full_wallet_on_equal_clocks(self):
        emptied = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=WEEK,
            channel_key="codex",
        )
        full = _reading("kimi-coding", 100, WEEK, channel_key="kimi")
        assert score_provider(emptied) == score_provider(full) == 1.0

    def test_equality_holds_under_uptime_derating(self):
        rates = ReliabilityRates(rate_24h=0.6, rate_1h=0.5)
        emptied = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=WEEK,
            channel_key="codex",
        )
        full = _reading("kimi-coding", 100, WEEK, channel_key="kimi")
        assert score_provider(emptied, rates) == score_provider(full, rates)
        assert score_provider(emptied, rates) == pytest.approx(0.3)

    def test_partial_wallet_plus_reset_is_additive_not_max(self):
        reading = _reading(
            "openai-codex",
            40,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=WEEK,
            channel_key="codex",
        )
        assert score_provider(reading) == pytest.approx(1.4)


class TestStacking:
    def test_each_reset_adds_one_full_wallet(self):
        scores = [
            score_provider(
                _reading(
                    "xai-oauth",
                    0,
                    WEEK,
                    reset_count=count,
                    reset_expiry_seconds=WEEK,
                )
            )
            for count in (0, 1, 2, 3)
        ]
        assert scores == [0.0, 1.0, 2.0, 3.0]

    def test_no_cap_on_many_resets(self):
        reading = _reading(
            "xai-oauth", 0, WEEK, reset_count=12, reset_expiry_seconds=WEEK
        )
        assert score_provider(reading) == 12.0

    def test_reset_clock_is_independent_of_remaining_clock(self):
        reading = _reading(
            "xai-oauth", 50, WEEK, reset_count=2, reset_expiry_seconds=3600
        )
        assert score_provider(reading) == pytest.approx(0.5 + 2 * REFERENCE_HOURS)

    def test_emptied_wallet_with_soon_reset_beats_a_full_weekly_wallet(self):
        emptied = _reading(
            "xai-oauth", 0, WEEK, reset_count=1, reset_expiry_seconds=3600
        )
        full = _reading("openai-codex", 100, WEEK, channel_key="codex")
        assert score_provider(emptied) > score_provider(full)
        assert score_provider(emptied) == pytest.approx(REFERENCE_HOURS)

    def test_zero_expiry_hours_floor_like_the_remaining_clock(self):
        reading = _reading(
            "xai-oauth", 0, WEEK, reset_count=1, reset_expiry_seconds=0
        )
        assert score_provider(reading) == pytest.approx(REFERENCE_HOURS * 60.0)


class TestResetGate:
    """Only Codex/Grok/z.ai have a resets API; reset fields elsewhere are inert."""

    @pytest.mark.parametrize("provider", ["kimi-coding", "cursor", "openrouter"])
    def test_injected_resets_score_exactly_like_zero(self, provider: str):
        polluted = _reading(
            provider, 0, WEEK, reset_count=1, reset_expiry_seconds=3600
        )
        clean = _reading(provider, 0, WEEK)
        assert score_provider(polluted) == score_provider(clean) == 0.0

    @pytest.mark.parametrize("provider", ["kimi-coding", "cursor", "openrouter"])
    def test_injected_resets_never_lift_the_low_quota_sink(self, provider: str):
        assert is_low_quota(_reading(provider, 0, WEEK, reset_count=1))
        assert is_low_quota(_reading(provider, 0, WEEK, reset_count=3))

    def test_gate_matches_the_provider_slug_case_insensitively(self):
        assert score_provider(
            _reading(
                "XAI-Oauth ", 0, WEEK, reset_count=1, reset_expiry_seconds=WEEK
            )
        ) == 1.0
        assert score_provider(
            _reading(
                "OpenAI-Codex", 0, WEEK, reset_count=2, reset_expiry_seconds=WEEK
            )
        ) == 2.0

    def test_codex_and_grok_still_score_their_resets(self):
        assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=1))
        assert not is_low_quota(_reading("openai-codex", 0, WEEK, reset_count=3))
        assert score_provider(
            _reading(
                "xai-oauth", 0, WEEK, reset_count=1, reset_expiry_seconds=WEEK
            )
        ) == 1.0


def test_is_low_quota_requires_empty_and_reset_free():
    assert is_low_quota(_reading("zai", 4, WEEK))
    assert not is_low_quota(_reading("zai", 5, WEEK))
    assert is_low_quota(_reading("kimi-coding", 0, WEEK, reset_count=1))
    assert not is_low_quota(_reading("zai", 0, WEEK, reset_count=1))
    assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=1))
    assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=3))
