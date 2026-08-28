"""Pending usage-limit resets in the shared spendability score.

The additive term: each pending manual reset stacks one full wallet
(quota fraction 1.0) on its own expiry clock, with the invariant that one
pending reset at 0% remaining equals zero resets at 100% remaining when the
clocks match.
"""

from __future__ import annotations

import pytest

from plugins.fallback_quota_reorder.core import (
    QuotaReading,
    REFERENCE_HOURS,
    compute_desired_order,
    is_low_quota,
    load_precise_readings,
    load_precise_reset_fields,
    readings_from_names,
    score_provider,
)
from plugins.fallback_quota_reorder.reliability import ReliabilityRates
from plugins.quota_channels.core import save_state

BULLET = "•"
DAY = 86400
WEEK = 7 * DAY
NOW = 1_800_000.0


def _reading(
    provider: str,
    pct: int,
    reset_seconds: float,
    *,
    reset_count: int = 0,
    reset_expiry_seconds: float | None = None,
    channel_key: str | None = None,
) -> QuotaReading:
    return QuotaReading(
        channel_key=channel_key or provider,
        provider=provider,
        channel_name="unused",
        pct=pct,
        reset_seconds=reset_seconds,
        reset_count=reset_count,
        reset_expiry_seconds=reset_expiry_seconds,
    )


class TestOneResetEqualsAFullWallet:
    def test_zero_pct_one_reset_equals_full_wallet_on_equal_clocks(self):
        # Codex shape: no expiry clock, so the reset term reuses the 7d
        # usage-reset countdown — exactly the 100% wallet's clock
        emptied = _reading("openai-codex", 0, WEEK, reset_count=1, channel_key="codex")
        full = _reading("kimi-coding", 100, WEEK, channel_key="kimi")
        assert score_provider(emptied) == score_provider(full)
        assert score_provider(emptied) == 1.0

    def test_equality_holds_under_uptime_derating(self):
        # the uptime factors multiply both terms, so they cannot break the
        # equivalence
        rates = ReliabilityRates(rate_24h=0.6, rate_1h=0.5)
        emptied = _reading("openai-codex", 0, WEEK, reset_count=1, channel_key="codex")
        full = _reading("kimi-coding", 100, WEEK, channel_key="kimi")
        assert score_provider(emptied, rates) == score_provider(full, rates)
        assert score_provider(emptied, rates) == pytest.approx(0.3)

    def test_partial_wallet_plus_reset_is_additive_not_max(self):
        # 40% remaining + 1 reset on the same clock = 1.4, not max(0.4, 1.0)
        reading = _reading("openai-codex", 40, WEEK, reset_count=1, channel_key="codex")
        assert score_provider(reading) == pytest.approx(1.4)


class TestStacking:
    def test_each_reset_adds_one_full_wallet(self):
        scores = [
            score_provider(_reading("xai-oauth", 0, WEEK, reset_count=count))
            for count in (0, 1, 2, 3)
        ]
        assert scores == [0.0, 1.0, 2.0, 3.0]

    def test_no_cap_on_many_resets(self):
        assert score_provider(_reading("xai-oauth", 0, WEEK, reset_count=12)) == 12.0

    def test_reset_clock_is_independent_of_remaining_clock(self):
        # Grok shape: the reset expires in 1h while the usage window still
        # has a full 7d left — the remaining term uses 168h, each reset 1h
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
        # an already-expired reset still spends now: floored at one minute
        reading = _reading("xai-oauth", 0, WEEK, reset_count=1, reset_expiry_seconds=0)
        assert score_provider(reading) == pytest.approx(REFERENCE_HOURS * 60.0)


class TestNoResetCreditsIsUnchanged:
    def test_count_zero_scores_the_remaining_term_only(self):
        # an expiry without a count must not invent a term
        reading = _reading("zai", 43, 3 * DAY, reset_count=0, reset_expiry_seconds=3600)
        assert score_provider(reading) == pytest.approx(0.43 * REFERENCE_HOURS / 72.0)

    def test_default_fields_match_the_pre_change_formula(self):
        legacy = _reading("zai", 43, 3 * DAY)
        explicit = _reading(
            "zai", 43, 3 * DAY, reset_count=0, reset_expiry_seconds=None
        )
        assert score_provider(legacy) == score_provider(explicit)
        assert score_provider(legacy) == pytest.approx(0.43 * REFERENCE_HOURS / 72.0)

    def test_providers_without_a_resets_api_never_gain_a_term(self):
        # Kimi / z.ai / Cursor channels never carry a resets segment
        readings = readings_from_names({
            "kimi": f"Kimi: 80% {BULLET} 7d left",
            "zai": f"z.ai: 70% {BULLET} 7d left",
            "cursor": f"Cursor: 90%/85% {BULLET} 25d left",
        })
        assert readings["kimi-coding"].reset_count == 0
        assert readings["kimi-coding"].reset_expiry_seconds is None
        assert score_provider(readings["kimi-coding"]) == pytest.approx(0.8)
        assert score_provider(readings["zai"]) == pytest.approx(0.7)
        # cursor scores its weaker percentage over the 25d window
        assert score_provider(readings["cursor"]) == pytest.approx(
            0.85 * REFERENCE_HOURS / 600.0
        )


class TestResetGate:
    """Only Codex/Grok have a resets API; reset fields anywhere else are inert."""

    @pytest.mark.parametrize(
        "provider",
        ["kimi-coding", "zai", "cursor", "openrouter"],
    )
    def test_injected_resets_score_exactly_like_zero(self, provider: str):
        polluted = _reading(
            provider, 0, WEEK, reset_count=1, reset_expiry_seconds=3600
        )
        clean = _reading(provider, 0, WEEK)
        assert score_provider(polluted) == score_provider(clean) == 0.0

    @pytest.mark.parametrize(
        "provider",
        ["kimi-coding", "zai", "cursor", "openrouter"],
    )
    def test_injected_resets_never_lift_the_low_quota_sink(self, provider: str):
        assert is_low_quota(_reading(provider, 0, WEEK, reset_count=1))
        assert is_low_quota(_reading(provider, 0, WEEK, reset_count=3))

    @pytest.mark.parametrize(
        "provider",
        ["kimi-coding", "zai", "cursor", "openrouter"],
    )
    def test_injected_resets_stay_inert_in_the_desired_order(
        self, provider: str
    ):
        other = "zai" if provider != "zai" else "kimi-coding"
        entries = [
            {"provider": provider, "model": "m"},
            {"provider": other, "model": "other"},
        ]
        readings = {
            provider: _reading(provider, 0, WEEK, reset_count=5),
            other: _reading(other, 3, 1800),
        }
        ordered = compute_desired_order(entries, readings)
        # the polluted 0% wallet still sinks behind the low-but-real row
        assert [entry["provider"] for entry in ordered] == [other, provider]

    def test_gate_matches_the_provider_slug_case_insensitively(self):
        assert score_provider(_reading("XAI-OAuth ", 0, WEEK, reset_count=1)) == 1.0
        assert score_provider(_reading("OpenAI-Codex", 0, WEEK, reset_count=2)) == 2.0

    def test_codex_and_grok_still_score_their_resets(self):
        assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=1))
        assert not is_low_quota(_reading("openai-codex", 0, WEEK, reset_count=3))
        assert score_provider(_reading("xai-oauth", 0, WEEK, reset_count=1)) == 1.0


class TestLowQuotaSinkRule:
    def test_emptied_wallet_with_pending_reset_stays_healthy(self):
        entries = [
            {"provider": "zai", "model": "zai"},
            {"provider": "kimi-coding", "model": "kimi"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        readings = {
            "zai": _reading("zai", 3, 1800),
            "kimi-coding": _reading("kimi-coding", 80, WEEK, channel_key="kimi"),
            # 0% but a pending reset: scores 1.0, and no longer sinks
            "xai-oauth": _reading("xai-oauth", 0, WEEK, reset_count=1),
        }
        ordered = compute_desired_order(entries, readings)
        assert [entry["provider"] for entry in ordered] == [
            "xai-oauth",
            "kimi-coding",
            "zai",
        ]

    def test_emptied_wallet_without_resets_still_sinks(self):
        entries = [
            {"provider": "zai", "model": "zai"},
            {"provider": "kimi-coding", "model": "kimi"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        readings = {
            "zai": _reading("zai", 3, 1800),
            "kimi-coding": _reading("kimi-coding", 80, WEEK, channel_key="kimi"),
            "xai-oauth": _reading("xai-oauth", 0, WEEK),  # 0 resets: sinks
        }
        ordered = compute_desired_order(entries, readings)
        assert [entry["provider"] for entry in ordered] == [
            "kimi-coding",
            "zai",
            "xai-oauth",
        ]

    def test_is_low_quota_requires_empty_and_reset_free(self):
        assert is_low_quota(_reading("zai", 4, WEEK))
        assert not is_low_quota(_reading("zai", 5, WEEK))
        # resets only exist for Codex/Grok: an injected zai count is inert,
        # so the emptied wallet sinks anyway
        assert is_low_quota(_reading("zai", 0, WEEK, reset_count=1))
        assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=1))
        assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=3))


class TestPreciseStateResets:
    def test_state_reset_fields_reach_the_reading(self):
        names = {"grok": f"Grok: 46% {BULLET} 3d left {BULLET} 0 resets"}
        readings = readings_from_names(
            names, {"grok": (46, 3 * DAY)}, {"grok": (1, 2 * DAY)}
        )
        reading = readings["xai-oauth"]
        assert reading.pct == 46
        assert reading.reset_seconds == 3 * DAY
        assert reading.reset_count == 1
        assert reading.reset_expiry_seconds == 2 * DAY

    def test_state_without_reset_fields_keeps_the_name_parsed_resets(self):
        # a pre-upgrade state file must not erase resets the name still shows
        names = {"codex": f"Codex: 100% {BULLET} 7d left {BULLET} 1 reset"}
        readings = readings_from_names(names, {"codex": (100, WEEK)})
        assert readings["openai-codex"].reset_count == 1
        assert readings["openai-codex"].reset_expiry_seconds is None

    def test_explicit_zero_in_state_beats_the_name(self):
        # when state DOES carry the count, it is the fresher source
        names = {"codex": f"Codex: 100% {BULLET} 7d left {BULLET} 2 resets"}
        readings = readings_from_names(
            names, {"codex": (100, WEEK)}, {"codex": (0, None)}
        )
        assert readings["openai-codex"].reset_count == 0

    def test_quota_channels_state_round_trips_into_both_contracts(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "grok": {
                    "pct": 46,
                    "reset_seconds": 3 * DAY,
                    "label": "Grok",
                    "reset_count": 1,
                    "reset_expiry_seconds": 2 * DAY,
                },
                "codex": {
                    "pct": 100,
                    "reset_seconds": WEEK,
                    "label": "Codex",
                    "reset_count": 2,
                },
                "kimi": {"pct": 80, "reset_seconds": WEEK, "label": "Kimi"},
            },
            now_fn=lambda: NOW,
        )

        # the public 2-tuple contract stays reset-free...
        precise = load_precise_readings(1800, now_fn=lambda: NOW)
        assert precise["grok"] == (46, 3 * DAY)
        assert precise["codex"] == (100, WEEK)
        assert precise["kimi"] == (80, WEEK)

        # ...and reset credits ride the dedicated function instead
        resets = load_precise_reset_fields(1800, now_fn=lambda: NOW)
        assert resets["grok"] == (1, 2 * DAY)
        assert resets["codex"] == (2, None)
        # rows without resets keep the legacy absence, not a zero
        assert "kimi" not in resets

    def test_state_reset_fields_are_gated_to_reset_providers(
        self, monkeypatch, tmp_path
    ):
        # reset fields polluting a non-Codex/Grok row never leave the state
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "kimi": {
                    "pct": 80,
                    "reset_seconds": WEEK,
                    "label": "Kimi",
                    "reset_count": 4,
                    "reset_expiry_seconds": 60,
                },
                "zai": {
                    "pct": 70,
                    "reset_seconds": WEEK,
                    "label": "z.ai",
                    "reset_count": 2,
                },
            },
            now_fn=lambda: NOW,
        )

        assert load_precise_reset_fields(1800, now_fn=lambda: NOW) == {}

        names = {"kimi": f"Kimi: 80% {BULLET} 7d left"}
        readings = readings_from_names(
            names,
            load_precise_readings(1800, now_fn=lambda: NOW),
            load_precise_reset_fields(1800, now_fn=lambda: NOW),
        )
        assert readings["kimi-coding"].reset_count == 0
        assert readings["kimi-coding"].reset_expiry_seconds is None
        assert score_provider(readings["kimi-coding"]) == pytest.approx(0.8)

    def test_stale_state_hides_reset_fields_too(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "grok": {
                    "pct": 46,
                    "reset_seconds": 3 * DAY,
                    "label": "Grok",
                    "reset_count": 1,
                }
            },
            now_fn=lambda: NOW - 2 * 1800 - 1,
        )
        assert load_precise_reset_fields(1800, now_fn=lambda: NOW) == {}

    def test_unreadable_state_count_drops_the_row(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "grok": {
                    "pct": 46,
                    "reset_seconds": 3 * DAY,
                    "label": "Grok",
                    "reset_count": "many",
                    "reset_expiry_seconds": "soon",
                }
            },
            now_fn=lambda: NOW,
        )
        assert load_precise_reset_fields(1800, now_fn=lambda: NOW) == {}
