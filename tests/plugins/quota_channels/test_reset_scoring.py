"""Pending usage-limit resets in the shared display score and persisted state."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from plugins.fallback_quota_reorder.core import REFERENCE_HOURS
from plugins.quota_channels import core as core
from plugins.quota_channels.core import (
    ResetCredits,
    load_state,
    quota_display_ranks,
    run_tick,
    save_state,
    state_path,
    validate_quota_config,
)

DAY = 86400
WEEK = 7 * DAY
NOW = 1_800_000.0


def _reading(pct: int, reset_seconds: float, **extra) -> dict:
    entry = {"pct": pct, "reset_seconds": reset_seconds, "label": "unused"}
    entry.update(extra)
    return entry


def _fake_discord(req, timeout=25.0):
    assert "discord.com" in req.full_url, f"non-discord request: {req.full_url}"
    method = getattr(req, "method", None) or req.get_method()
    if method == "GET":
        return 200, json.dumps({"name": "old-name"}).encode()
    return 200, json.dumps({"name": "patched"}).encode()


def _codex_http_with_details(expires_in: float | list[float | None] = 2 * DAY):
    """Codex transport serving the usage payload plus its credit details.

    ``expires_in`` is the horizon of one credit, or the list of them — a
    ``None`` entry is a credit whose expiry the payload cannot express.
    """
    horizons = [expires_in] if isinstance(expires_in, (int, float)) else list(expires_in)

    def fake_http(req, timeout=25.0):
        url = req.full_url
        if "wham/usage" in url:
            return 200, json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 0,
                            "reset_after_seconds": WEEK,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": len(horizons)},
                }
            ).encode()
        if "rate-limit-reset-credits" in url:
            credits = []
            for horizon in horizons:
                credit = {
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-08-28T00:00:00Z",
                    "title": "Usage limit reset",
                }
                if horizon is not None:
                    credit["expires_at"] = (
                        datetime.fromtimestamp(NOW + horizon, tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                credits.append(credit)
            return 200, json.dumps(
                {"available_count": len(credits), "credits": credits}
            ).encode()
        return _fake_discord(req, timeout=timeout)

    return fake_http


def _write_codex_auth(tmp_path):
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "codex-tok",
                            "refresh_token": "codex-ref",
                        }
                    }
                }
            }
        )
    )


class TestDisplayRanksUseResetCredits:
    def test_emptied_grok_with_soon_reset_outranks_full_codex(self):
        # Discord order is the failover score: a 0% wallet whose pending
        # reset expires in 1h (168.0) beats a 100% weekly wallet (1.0)
        readings = {
            "codex": _reading(100, WEEK),
            "grok": _reading(0, WEEK, reset_count=1, reset_expiry_seconds=3600),
        }
        ranks = quota_display_ranks(readings)
        assert ranks["grok"] < ranks["codex"]

    def test_pending_reset_lifts_a_row_out_of_the_low_quota_bucket(self):
        readings = {
            "grok": _reading(0, WEEK, reset_count=1, reset_expiry_seconds=3600),
            "zai": _reading(40, 22 * DAY),  # weakest healthy score ~0.127
        }
        assert (
            quota_display_ranks(readings)["grok"] < quota_display_ranks(readings)["zai"]
        )

    def test_unknown_reset_expiry_never_borrows_the_quota_clock(self):
        # the credit stays visible and out of the sink, but urgency is
        # unmeasurable, so it ranks last among the healthy rows
        readings = {
            "codex": _reading(0, WEEK, reset_count=1),
            "zai": _reading(40, 22 * DAY),
            "kimi": _reading(0, WEEK),  # no resets: sinks
        }
        ranks = quota_display_ranks(readings)
        assert ranks["zai"] < ranks["codex"] < ranks["kimi"]

    def test_per_credit_horizons_score_each_clock_once(self):
        # 3d + 9d credits rank by 168/72 + 168/216, never 2 * 168/72: the
        # earliest countdown the name shows is display-only
        readings = {
            "codex": _reading(
                0,
                WEEK,
                reset_count=2,
                reset_expiry_seconds=3 * DAY,
                reset_expiry_horizons=[3 * DAY, 9 * DAY],
            )
        }
        rank = quota_display_ranks(readings)["codex"]
        assert rank == pytest.approx(
            -(REFERENCE_HOURS / 72.0 + REFERENCE_HOURS / 216.0)
        )
        assert rank != pytest.approx(-2 * REFERENCE_HOURS / 72.0)

    def test_legacy_single_clock_keeps_count_times_one_clock(self):
        # without the richer list the same row keeps its legacy meaning —
        # exactly what a channel name or a pre-list state row reports
        legacy = _reading(0, WEEK, reset_count=2, reset_expiry_seconds=3 * DAY)
        precise = _reading(
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            reset_expiry_horizons=[3 * DAY, 9 * DAY],
        )
        ranks = quota_display_ranks({"codex": legacy, "grok": precise})
        # the 9d credit dilutes, so the precise row ranks after the legacy one
        assert ranks["grok"] > ranks["codex"]
        assert ranks["codex"] == pytest.approx(-2 * REFERENCE_HOURS / 72.0)

    def test_known_plus_unknown_horizons_score_only_the_known_credit(self):
        # the count stays 2, but only the readable clock earns a wallet
        readings = {
            "codex": _reading(
                0,
                WEEK,
                reset_count=2,
                reset_expiry_seconds=3 * DAY,
                reset_expiry_horizons=[3 * DAY],
            )
        }
        assert quota_display_ranks(readings)["codex"] == pytest.approx(
            -REFERENCE_HOURS / 72.0
        )

    def test_malformed_horizon_entries_are_dropped_not_fatal(self):
        # junk in a hand-edited state row never fails the tick: the one
        # readable horizon still scores and nothing else is invented
        readings = {
            "codex": _reading(
                0,
                WEEK,
                reset_count=2,
                reset_expiry_seconds=3 * DAY,
                reset_expiry_horizons=[3 * DAY, "soon", None, -DAY, True],
            )
        }
        assert quota_display_ranks(readings)["codex"] == pytest.approx(
            -REFERENCE_HOURS / 72.0
        )

    def test_horizons_on_a_row_without_a_resets_api_are_inert(self):
        # kimi has no resets API: a polluted horizons list moves nothing
        readings = {
            "kimi": _reading(0, WEEK, reset_count=1, reset_expiry_horizons=[WEEK]),
            "zai": _reading(40, 22 * DAY),
        }
        ranks = quota_display_ranks(readings)
        # the polluted 0% row still sinks behind the healthy zai row
        assert ranks["zai"] < ranks["kimi"]

    def test_zero_pct_without_resets_still_sinks(self):
        readings = {
            "grok": _reading(0, WEEK),  # 0 resets: low-quota bucket
            "zai": _reading(40, 22 * DAY),
        }
        ranks = quota_display_ranks(readings)
        assert ranks["zai"] < ranks["grok"]

    def test_legacy_entries_without_reset_fields_rank_unchanged(self):
        readings = {"codex": _reading(100, WEEK), "kimi": _reading(60, 25 * 3600)}
        ranks = quota_display_ranks(readings)
        # kimi 0.6 * 168/25 = 4.03 still beats codex's neutral 1.0
        assert ranks["kimi"] < ranks["codex"]

    def test_injected_resets_on_a_non_reset_provider_stay_inert(self):
        # Kimi has no resets API: even a polluted state row with reset fields
        # scores the remaining term only and still sinks at 0%
        readings = {
            "kimi": _reading(0, WEEK, reset_count=4, reset_expiry_seconds=60),
            "zai": _reading(40, 22 * DAY),
        }
        ranks = quota_display_ranks(readings)
        assert ranks["zai"] < ranks["kimi"]


class TestStatePersistsResets:
    def test_save_state_round_trips_reset_fields(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state(
            {
                "grok": _reading(
                    46,
                    3 * DAY,
                    label="Grok",
                    reset_count=1,
                    reset_expiry_seconds=2 * DAY,
                ),
                "codex": _reading(100, WEEK, label="Codex", reset_count=2),
                "kimi": _reading(80, WEEK, label="Kimi"),
            },
            now_fn=lambda: NOW,
        )

        assert load_state()["readings"] == {
            "grok": {
                "pct": 46,
                "reset_seconds": float(3 * DAY),
                "label": "Grok",
                "reset_count": 1,
                "reset_expiry_seconds": float(2 * DAY),
            },
            "codex": {
                "pct": 100,
                "reset_seconds": float(WEEK),
                "label": "Codex",
                "reset_count": 2,
            },
            # rows without reset credits keep the legacy shape
            "kimi": {"pct": 80, "reset_seconds": float(WEEK), "label": "Kimi"},
        }


class TestRunTickWritesResetReadings:
    def _config(self) -> dict:
        return validate_quota_config({
            "guild_id": "guild",
            "category_id": "cat",
            "channel_ids": {"codex": "c1", "grok": "c5"},
            "enabled_providers": ["codex", "grok"],
        })

    def test_credits_reach_state_and_debug_output(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {
                # live codex metrics return the trailing ResetCredits
                "codex": lambda http_fn=None, now_fn=None: (
                    100,
                    WEEK,
                    ResetCredits(2),
                ),
                "grok": lambda http_fn=None, now_fn=None: (46, 3 * DAY),
            },
        )
        monkeypatch.setattr(
            core,
            "grok_reset_credits",
            lambda **kwargs: (ResetCredits(1, 2 * DAY), None),
        )
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            self._config(),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_fake_discord,
        )

        assert result["success"] is True
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 2,
        }
        assert state["readings"]["grok"] == {
            "pct": 46,
            "reset_seconds": float(3 * DAY),
            "label": "Grok",
            "reset_count": 1,
            "reset_expiry_seconds": float(2 * DAY),
        }
        # the debug output carries the same numbers
        assert result["providers"]["Codex"]["reset_count"] == 2
        assert result["providers"]["Grok"]["reset_count"] == 1
        assert result["providers"]["Grok"]["reset_expiry_seconds"] == float(2 * DAY)

    def test_degraded_grok_fetch_persists_an_explicit_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {"grok": lambda http_fn=None, now_fn=None: (46, 3 * DAY)},
        )
        monkeypatch.setattr(
            core,
            "grok_reset_credits",
            lambda **kwargs: (ResetCredits(0), "grok resets endpoint returned 500"),
        )
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"grok": "c5"},
                "enabled_providers": ["grok"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_fake_discord,
        )

        assert result["success"] is True
        assert (
            "grok resets endpoint returned 500"
            in result["providers"]["Grok"]["reset_error"]
        )
        state = json.loads(state_path().read_text(encoding="utf-8"))
        # "we asked and got zero" persists as zero, with no expiry clock
        assert state["readings"]["grok"] == {
            "pct": 46,
            "reset_seconds": float(3 * DAY),
            "label": "Grok",
            "reset_count": 0,
        }

    def test_codex_real_expiry_reaches_state_and_debug_output(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_codex_auth(tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "c1"},
                "enabled_providers": ["codex"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_codex_http_with_details(),
        )

        assert result["success"] is True
        state = json.loads(state_path().read_text(encoding="utf-8"))
        # the real reset-credit expiry rides into the persisted reading the
        # fallback reorder scores from: the earliest as the display clock plus
        # the per-credit horizons list
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 1,
            "reset_expiry_seconds": float(2 * DAY),
            "reset_expiry_horizons": [float(2 * DAY)],
        }
        assert result["providers"]["Codex"]["reset_expiry_seconds"] == float(2 * DAY)
        assert result["providers"]["Codex"]["reset_expiry_horizons"] == [float(2 * DAY)]
        assert "reset_error" not in result["providers"]["Codex"]

    def test_codex_per_credit_horizons_reach_state(self, monkeypatch, tmp_path):
        # two credits expiring in 3d and 9d: state keeps both clocks for the
        # fallback score, while reset_expiry_seconds keeps only the earliest
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_codex_auth(tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "c1"},
                "enabled_providers": ["codex"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_codex_http_with_details([3 * DAY, 9 * DAY]),
        )

        assert result["success"] is True
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 2,
            "reset_expiry_seconds": float(3 * DAY),
            "reset_expiry_horizons": [float(3 * DAY), float(9 * DAY)],
        }
        provider = result["providers"]["Codex"]
        assert provider["reset_count"] == 2
        assert provider["reset_expiry_seconds"] == float(3 * DAY)
        assert provider["reset_expiry_horizons"] == [float(3 * DAY), float(9 * DAY)]
        assert "reset_error" not in provider

    def test_codex_known_plus_unknown_expiry_persists_one_horizon(
        self, monkeypatch, tmp_path
    ):
        # the unreadable credit stays counted but contributes no clock, so it
        # never borrows its sibling's 3d expiry
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_codex_auth(tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "c1"},
                "enabled_providers": ["codex"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_codex_http_with_details([None, 3 * DAY]),
        )

        assert result["success"] is True
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 2,
            "reset_expiry_seconds": float(3 * DAY),
            "reset_expiry_horizons": [float(3 * DAY)],
        }

    def test_codex_all_unknown_expiries_persist_no_clocks(self, monkeypatch, tmp_path):
        # no readable expiry anywhere: the count survives, but neither a
        # display clock nor a horizons list is invented — the quota window is
        # never borrowed as a stand-in
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_codex_auth(tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "c1"},
                "enabled_providers": ["codex"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_codex_http_with_details([None, None]),
        )

        assert result["success"] is True
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 2,
        }

    def test_codex_expired_credit_leaves_only_the_future_horizon(
        self, monkeypatch, tmp_path
    ):
        # an already-expired credit is not spendable and not counted; the
        # future one keeps its own clock alone
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_codex_auth(tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "c1"},
                "enabled_providers": ["codex"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_codex_http_with_details([-DAY, 3 * DAY]),
        )

        assert result["success"] is True
        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 1,
            "reset_expiry_seconds": float(3 * DAY),
            "reset_expiry_horizons": [float(3 * DAY)],
        }

    def test_unreadable_codex_credits_persist_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(
            core,
            "QUOTA_METRICS",
            {
                # stubs may still return the legacy 2-tuple: no credits block
                "codex": lambda http_fn=None, now_fn=None: (100, WEEK),
            },
        )
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"codex": "c1"},
                "enabled_providers": ["codex"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=_fake_discord,
        )

        state = json.loads(state_path().read_text(encoding="utf-8"))
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
        }
