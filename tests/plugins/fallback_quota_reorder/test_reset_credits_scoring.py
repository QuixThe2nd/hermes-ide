"""Pending usage-limit resets in the shared spendability score.

The additive term: each pending manual reset stacks one full wallet
(quota fraction 1.0) on its own expiry clock, with the invariant that one
pending reset at 0% remaining equals zero resets at 100% remaining when the
clocks match. An expiry the provider never reports adds nothing — the
usage-reset countdown is never borrowed as a stand-in. When the richer
per-credit horizons are known, every credit scores on its own clock exactly
once; the single earliest countdown the channel name shows is display-only.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from plugins.fallback_quota_reorder.core import (
    PrimarySlot,
    QuotaReading,
    REFERENCE_HOURS,
    compute_desired_order,
    compute_primary_slot,
    is_low_quota,
    load_precise_readings,
    load_precise_reset_fields,
    load_precise_reset_horizons,
    readings_from_names,
    run_reorder,
    score_provider,
)
from plugins.fallback_quota_reorder.reliability import ReliabilityRates
from plugins.quota_channels.core import save_state
from tests.plugins.fallback_quota_reorder._helpers import (
    fake_http_for_names,
    write_hermes_home,
    write_quota_config_path,
)

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
    reset_expiry_horizons: tuple[float, ...] | None = None,
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
        reset_expiry_horizons=reset_expiry_horizons,
    )


class TestOneResetEqualsAFullWallet:
    def test_zero_pct_one_reset_equals_full_wallet_on_equal_clocks(self):
        # the reset's own expiry clock matches the 100% wallet's usage clock
        emptied = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=WEEK,
            channel_key="codex",
        )
        full = _reading("kimi-coding", 100, WEEK, channel_key="kimi")
        assert score_provider(emptied) == score_provider(full)
        assert score_provider(emptied) == 1.0

    def test_equality_holds_under_uptime_derating(self):
        # the uptime factors multiply both terms, so they cannot break the
        # equivalence
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
        # 40% remaining + 1 reset on the same clock = 1.4, not max(0.4, 1.0)
        reading = _reading(
            "openai-codex",
            40,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=WEEK,
            channel_key="codex",
        )
        assert score_provider(reading) == pytest.approx(1.4)


class TestUnknownExpiryScoresNothing:
    """An unreadable reset expiry must never borrow the usage-reset clock."""

    def test_unknown_expiry_contributes_no_reset_term(self):
        # urgency is unmeasurable: 0% + 1 reset with no clock scores 0.0,
        # not the 1.0 a 7d usage clock would lend it
        assert score_provider(_reading("openai-codex", 0, WEEK, reset_count=1)) == 0.0

    def test_unknown_expiry_never_substitutes_the_usage_countdown(self):
        # a short usage countdown must not masquerade as reset urgency
        assert score_provider(_reading("xai-oauth", 0, 60, reset_count=3)) == 0.0

    def test_unknown_expiry_keeps_the_remaining_term(self):
        reading = _reading("openai-codex", 40, WEEK, reset_count=2)
        assert score_provider(reading) == pytest.approx(0.4)

    def test_unknown_expiry_stays_zero_under_uptime_derating(self):
        rates = ReliabilityRates(rate_24h=0.6, rate_1h=0.5)
        reading = _reading("xai-oauth", 0, WEEK, reset_count=1)
        assert score_provider(reading, rates) == 0.0

    def test_unknown_expiry_count_still_escapes_the_low_quota_sink(self):
        # the credit is real spendable capacity, so it stays healthy — it
        # just ranks by its (zero) score instead of sinking below everything
        assert not is_low_quota(_reading("openai-codex", 0, WEEK, reset_count=1))

    def test_unknown_expiry_ranks_last_among_healthy_but_above_the_sink(self):
        entries = [
            {"provider": "zai", "model": "zai"},
            {"provider": "kimi-coding", "model": "kimi"},
            {"provider": "openai-codex", "model": "codex"},
        ]
        readings = {
            "zai": _reading("zai", 3, 1800),
            "kimi-coding": _reading("kimi-coding", 80, WEEK, channel_key="kimi"),
            "openai-codex": _reading(
                "openai-codex", 0, WEEK, reset_count=1, channel_key="codex"
            ),
        }
        ordered = compute_desired_order(entries, readings)
        assert [entry["provider"] for entry in ordered] == [
            "kimi-coding",
            "openai-codex",
            "zai",
        ]


class TestStacking:
    def test_each_reset_adds_one_full_wallet(self):
        scores = [
            score_provider(
                _reading(
                    "xai-oauth", 0, WEEK, reset_count=count, reset_expiry_seconds=WEEK
                )
            )
            for count in (0, 1, 2, 3)
        ]
        assert scores == [0.0, 1.0, 2.0, 3.0]

    def test_no_cap_on_many_resets(self):
        assert (
            score_provider(
                _reading("xai-oauth", 0, WEEK, reset_count=12, reset_expiry_seconds=WEEK)
            )
            == 12.0
        )

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


class TestPerCreditExpiryScoring:
    """Each credit scores on its own clock, exactly once (PR #163 P2).

    ``reset_expiry_horizons`` is the richer per-credit state from the Codex
    details payload; ``reset_expiry_seconds`` is the earliest of them and is
    display-only while the list exists.
    """

    def test_three_and_nine_day_credits_score_as_two_separate_wallets(self):
        # 3d + 9d must score 168/72 + 168/216, never 2 * 168/72: the display
        # clock is the earliest expiry, not a clock the whole stack shares
        reading = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            reset_expiry_horizons=(3 * DAY, 9 * DAY),
            channel_key="codex",
        )
        assert score_provider(reading) == pytest.approx(
            REFERENCE_HOURS / 72.0 + REFERENCE_HOURS / 216.0
        )
        assert score_provider(reading) < 2 * REFERENCE_HOURS / 72.0

    def test_legacy_single_clock_still_multiplies_the_count(self):
        # without a richer list, count * one clock keeps its legacy meaning —
        # exactly what a channel name or a pre-list state row reports
        reading = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            channel_key="codex",
        )
        assert score_provider(reading) == pytest.approx(2 * REFERENCE_HOURS / 72.0)

    def test_known_plus_unknown_scores_only_the_known_credit(self):
        # the unknown credit must not borrow its sibling's clock; it stays
        # counted, so the wallet keeps its low-quota escape
        reading = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            reset_expiry_horizons=(3 * DAY,),
            channel_key="codex",
        )
        assert score_provider(reading) == pytest.approx(REFERENCE_HOURS / 72.0)
        assert reading.reset_count == 2
        assert not is_low_quota(reading)

    def test_all_unknown_horizons_score_nothing_but_stay_counted(self):
        reading = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=None,
            reset_expiry_horizons=(),
            channel_key="codex",
        )
        assert score_provider(reading) == 0.0
        # the credits are still real spendable capacity: no low-quota sink
        assert not is_low_quota(reading)

    def test_horizons_never_borrow_the_usage_reset_countdown(self):
        # a short usage countdown must not masquerade as reset urgency just
        # because the richer list exists but carries no clocks
        reading = _reading(
            "openai-codex",
            0,
            60,
            reset_count=2,
            reset_expiry_seconds=None,
            reset_expiry_horizons=(),
            channel_key="codex",
        )
        assert score_provider(reading) == 0.0

    def test_horizons_are_gated_to_reset_providers(self):
        # a horizons list injected into a provider without a resets API is
        # as inert as the count next to it
        polluted = _reading(
            "kimi-coding",
            0,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=WEEK,
            reset_expiry_horizons=(WEEK,),
            channel_key="kimi",
        )
        clean = _reading("kimi-coding", 0, WEEK, channel_key="kimi")
        assert score_provider(polluted) == score_provider(clean) == 0.0

    def test_horizons_stack_with_the_remaining_term_and_uptime_factors(self):
        # the reset term stays additive with the remaining term, and the
        # uptime factors still multiply the whole sum
        rates = ReliabilityRates(rate_24h=0.6, rate_1h=0.5)
        reading = _reading(
            "openai-codex",
            50,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            reset_expiry_horizons=(3 * DAY, 9 * DAY),
            channel_key="codex",
        )
        expected = (
            0.5 + REFERENCE_HOURS / 72.0 + REFERENCE_HOURS / 216.0
        ) * 0.6 * 0.5
        assert score_provider(reading, rates) == pytest.approx(expected)


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
        assert (
            score_provider(
                _reading(
                    "XAI-OAuth ", 0, WEEK, reset_count=1, reset_expiry_seconds=WEEK
                )
            )
            == 1.0
        )
        assert (
            score_provider(
                _reading(
                    "OpenAI-Codex", 0, WEEK, reset_count=2, reset_expiry_seconds=WEEK
                )
            )
            == 2.0
        )

    def test_codex_and_grok_still_score_their_resets(self):
        assert not is_low_quota(_reading("xai-oauth", 0, WEEK, reset_count=1))
        assert not is_low_quota(_reading("openai-codex", 0, WEEK, reset_count=3))
        assert (
            score_provider(
                _reading("xai-oauth", 0, WEEK, reset_count=1, reset_expiry_seconds=WEEK)
            )
            == 1.0
        )


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
            # 0% but a pending reset expiring within the hour: scores 168.0
            # and no longer sinks
            "xai-oauth": _reading(
                "xai-oauth", 0, WEEK, reset_count=1, reset_expiry_seconds=3600
            ),
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


class TestPerCreditOrdering:
    """The per-credit clocks must move the fallback order, not just the math."""

    def test_precise_horizons_swap_the_fallback_order(self):
        # legacy shape: codex's two resets on one 3d clock (4.67) beat grok's
        # single 2d wallet (3.5); per-credit reality (3d + 9d = 3.11) loses
        entries = [
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        grok = _reading(
            "xai-oauth",
            0,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=2 * DAY,
            channel_key="grok",
        )
        legacy = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            channel_key="codex",
        )
        precise = replace(legacy, reset_expiry_horizons=(3 * DAY, 9 * DAY))
        assert [e["provider"] for e in compute_desired_order(
            entries, {"openai-codex": legacy, "xai-oauth": grok}
        )] == ["openai-codex", "xai-oauth"]
        assert [e["provider"] for e in compute_desired_order(
            entries, {"openai-codex": precise, "xai-oauth": grok}
        )] == ["xai-oauth", "openai-codex"]

    def test_precise_horizons_decide_the_primary_race(self):
        # same readings through the primary slot: grok's single 2d wallet
        # takes over only once codex's second credit spends on its own
        # weaker clock instead of doubling the 3d one
        config = {"model": {"provider": "zai", "default": "zai"}}
        entries = [
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "zai", "model": "zai"},
        ]
        zai = _reading("zai", 40, 22 * DAY, channel_key="zai")
        grok = _reading(
            "xai-oauth",
            0,
            WEEK,
            reset_count=1,
            reset_expiry_seconds=2 * DAY,
            channel_key="grok",
        )
        legacy = _reading(
            "openai-codex",
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            channel_key="codex",
        )
        precise = replace(legacy, reset_expiry_horizons=(3 * DAY, 9 * DAY))
        assert compute_primary_slot(
            config, entries, {"openai-codex": legacy, "xai-oauth": grok, "zai": zai}
        ) == PrimarySlot(provider="openai-codex", model="codex")
        assert compute_primary_slot(
            config, entries, {"openai-codex": precise, "xai-oauth": grok, "zai": zai}
        ) == PrimarySlot(provider="xai-oauth", model="grok")


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

    def test_state_reset_expiry_reaches_the_score(self):
        # the same Codex row earns its extra wallet only when state carries
        # the real expiry the details endpoint reported
        names = {"codex": f"Codex: 100% {BULLET} 7d left {BULLET} 1 reset"}
        unknown = readings_from_names(
            names, {"codex": (100, WEEK)}, {"codex": (1, None)}
        )
        known = readings_from_names(
            names, {"codex": (100, WEEK)}, {"codex": (1, 2 * DAY)}
        )
        assert score_provider(unknown["openai-codex"]) == pytest.approx(1.0)
        assert score_provider(known["openai-codex"]) == pytest.approx(
            1.0 + REFERENCE_HOURS / 48.0
        )

    def test_state_horizons_round_trip_into_the_score(self, monkeypatch, tmp_path):
        # the exact PR #163 shape: state carries each credit's own expiry, so
        # the reorder scores 3d + 9d as two separate wallets, never the name's
        # one 3d countdown times two
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "codex": {
                    "pct": 100,
                    "reset_seconds": WEEK,
                    "label": "Codex",
                    "reset_count": 2,
                    "reset_expiry_seconds": 3 * DAY,
                    "reset_expiry_horizons": [3 * DAY, 9 * DAY],
                },
            },
            now_fn=lambda: NOW,
        )
        names = {"codex": f"Codex: 100% {BULLET} 7d left {BULLET} 2 resets in 3d"}
        readings = readings_from_names(
            names,
            load_precise_readings(1800, now_fn=lambda: NOW),
            load_precise_reset_fields(1800, now_fn=lambda: NOW),
            load_precise_reset_horizons(1800, now_fn=lambda: NOW),
        )
        reading = readings["openai-codex"]
        assert reading.reset_count == 2
        # the display clock survives for the name, the horizons for the score
        assert reading.reset_expiry_seconds == 3 * DAY
        assert reading.reset_expiry_horizons == (3 * DAY, 9 * DAY)
        assert score_provider(reading) == pytest.approx(
            1.0 + REFERENCE_HOURS / 72.0 + REFERENCE_HOURS / 216.0
        )

    def test_state_without_horizons_keeps_the_single_clock_meaning(
        self, monkeypatch, tmp_path
    ):
        # a pre-list state row (and every Grok row) keeps count * one clock
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "codex": {
                    "pct": 100,
                    "reset_seconds": WEEK,
                    "label": "Codex",
                    "reset_count": 2,
                    "reset_expiry_seconds": 3 * DAY,
                },
                "grok": {
                    "pct": 46,
                    "reset_seconds": 3 * DAY,
                    "label": "Grok",
                    "reset_count": 1,
                    "reset_expiry_seconds": 2 * DAY,
                },
            },
            now_fn=lambda: NOW,
        )
        assert load_precise_reset_horizons(1800, now_fn=lambda: NOW) == {}
        names = {
            "codex": f"Codex: 100% {BULLET} 7d left {BULLET} 2 resets in 3d",
            "grok": f"Grok: 46% {BULLET} 3d left {BULLET} 1 reset in 2d",
        }
        readings = readings_from_names(
            names,
            load_precise_readings(1800, now_fn=lambda: NOW),
            load_precise_reset_fields(1800, now_fn=lambda: NOW),
            load_precise_reset_horizons(1800, now_fn=lambda: NOW),
        )
        assert score_provider(readings["openai-codex"]) == pytest.approx(
            1.0 + 2 * REFERENCE_HOURS / 72.0
        )
        assert score_provider(readings["xai-oauth"]) == pytest.approx(
            0.46 * REFERENCE_HOURS / 72.0 + REFERENCE_HOURS / 48.0
        )

    def test_malformed_state_horizons_drop_only_the_bad_entries(
        self, monkeypatch, tmp_path
    ):
        # junk entries are dropped, never fatal: the one readable horizon
        # still scores, and nothing borrows the display clock for the rest
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "codex": {
                    "pct": 100,
                    "reset_seconds": WEEK,
                    "label": "Codex",
                    "reset_count": 2,
                    "reset_expiry_seconds": 3 * DAY,
                    "reset_expiry_horizons": [3 * DAY, "soon", None, -DAY, True],
                },
            },
            now_fn=lambda: NOW,
        )
        assert load_precise_reset_horizons(1800, now_fn=lambda: NOW) == {
            "codex": (3 * DAY,)
        }
        names = {"codex": f"Codex: 100% {BULLET} 7d left {BULLET} 2 resets in 3d"}
        readings = readings_from_names(
            names,
            load_precise_readings(1800, now_fn=lambda: NOW),
            load_precise_reset_fields(1800, now_fn=lambda: NOW),
            load_precise_reset_horizons(1800, now_fn=lambda: NOW),
        )
        # only the readable 3d credit scores; the count of 2 is untouched
        assert score_provider(readings["openai-codex"]) == pytest.approx(
            1.0 + REFERENCE_HOURS / 72.0
        )
        assert readings["openai-codex"].reset_count == 2

    def test_entirely_malformed_state_horizons_fall_back_to_legacy(
        self, monkeypatch, tmp_path
    ):
        # a richer list with no readable entry is "no richer list": scoring
        # falls back to the count + single display clock the tick recorded
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "codex": {
                    "pct": 100,
                    "reset_seconds": WEEK,
                    "label": "Codex",
                    "reset_count": 2,
                    "reset_expiry_seconds": 3 * DAY,
                    "reset_expiry_horizons": ["soon", None],
                },
            },
            now_fn=lambda: NOW,
        )
        assert load_precise_reset_horizons(1800, now_fn=lambda: NOW) == {}
        names = {"codex": f"Codex: 100% {BULLET} 7d left {BULLET} 2 resets in 3d"}
        readings = readings_from_names(
            names,
            load_precise_readings(1800, now_fn=lambda: NOW),
            load_precise_reset_fields(1800, now_fn=lambda: NOW),
            load_precise_reset_horizons(1800, now_fn=lambda: NOW),
        )
        assert score_provider(readings["openai-codex"]) == pytest.approx(
            1.0 + 2 * REFERENCE_HOURS / 72.0
        )

    def test_state_horizons_are_gated_to_reset_providers(
        self, monkeypatch, tmp_path
    ):
        # horizons polluting a non-Codex/Grok row never leave the state
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "kimi": {
                    "pct": 80,
                    "reset_seconds": WEEK,
                    "label": "Kimi",
                    "reset_count": 1,
                    "reset_expiry_seconds": WEEK,
                    "reset_expiry_horizons": [WEEK, WEEK],
                },
            },
            now_fn=lambda: NOW,
        )
        assert load_precise_reset_horizons(1800, now_fn=lambda: NOW) == {}
        names = {"kimi": f"Kimi: 80% {BULLET} 7d left"}
        readings = readings_from_names(
            names,
            load_precise_readings(1800, now_fn=lambda: NOW),
            load_precise_reset_fields(1800, now_fn=lambda: NOW),
            load_precise_reset_horizons(1800, now_fn=lambda: NOW),
        )
        assert readings["kimi-coding"].reset_count == 0
        assert score_provider(readings["kimi-coding"]) == pytest.approx(0.8)

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


class TestReorderUsesPerCreditState:
    """The full path: quota_channels state -> reorder order and primary slot."""

    def test_horizons_in_state_flip_the_primary_and_the_order(
        self, monkeypatch, tmp_path
    ):
        # codex shows an emptied wallet plus two resets with the earliest
        # (3d) countdown; grok one reset in 2d. Legacy single-clock scoring
        # makes codex the winner (2 * 168/72 = 4.67 vs grok's 3.5); the
        # per-credit horizons state records (168/72 + 168/216 = 3.11) hand
        # both races to grok.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        write_hermes_home(
            tmp_path,
            fallback_providers=[
                {"provider": "openrouter", "model": "or"},
                {"provider": "openai-codex", "model": "codex"},
                {"provider": "xai-oauth", "model": "grok"},
            ],
            extra_config={"model": {"provider": "kimi-coding", "default": "kimi"}},
        )
        quota_config = tmp_path / "quota-config.yaml"
        write_quota_config_path(quota_config)
        names = {
            "codex": f"Codex: 0% {BULLET} 7d left {BULLET} 2 resets in 3d",
            "kimi": f"Kimi: 80% {BULLET} 7d left",
            "grok": f"Grok: 0% {BULLET} 7d left {BULLET} 1 reset in 2d",
            "zai": "",
            "cursor": "",
        }
        codex_row = {
            "pct": 0,
            "reset_seconds": WEEK,
            "label": "Codex",
            "reset_count": 2,
            "reset_expiry_seconds": 3 * DAY,
        }
        grok_row = {
            "pct": 0,
            "reset_seconds": WEEK,
            "label": "Grok",
            "reset_count": 1,
            "reset_expiry_seconds": 2 * DAY,
        }
        kimi_row = {"pct": 80, "reset_seconds": WEEK, "label": "Kimi"}

        save_state(
            {"codex": codex_row, "grok": grok_row, "kimi": kimi_row},
            now_fn=lambda: NOW,
        )
        legacy = run_reorder(
            config_path=quota_config,
            dry_run=True,
            http_fn=fake_http_for_names(names),
            now_fn=lambda: NOW,
        )
        assert legacy["primary_desired"] == PrimarySlot(
            provider="openai-codex", model="codex"
        )
        # the promoted provider graduates out of the chain, so grok leads it
        assert [e["provider"] for e in legacy["desired_entries"]] == [
            "xai-oauth",
            "kimi-coding",
            "openrouter",
        ]
        assert legacy["scores"]["openai-codex"] == pytest.approx(
            2 * REFERENCE_HOURS / 72.0
        )

        save_state(
            {
                "codex": {
                    **codex_row,
                    "reset_expiry_horizons": [3 * DAY, 9 * DAY],
                },
                "grok": grok_row,
                "kimi": kimi_row,
            },
            now_fn=lambda: NOW,
        )
        precise = run_reorder(
            config_path=quota_config,
            dry_run=True,
            http_fn=fake_http_for_names(names),
            now_fn=lambda: NOW,
        )
        assert precise["primary_desired"] == PrimarySlot(
            provider="xai-oauth", model="grok"
        )
        # now codex stays in the chain it no longer tops
        assert [e["provider"] for e in precise["desired_entries"]] == [
            "openai-codex",
            "kimi-coding",
            "openrouter",
        ]
        assert precise["scores"]["openai-codex"] == pytest.approx(
            REFERENCE_HOURS / 72.0 + REFERENCE_HOURS / 216.0
        )
