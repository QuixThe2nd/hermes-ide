"""Pending usage-limit resets in the shared display score and persisted state."""

from __future__ import annotations

import json
from datetime import datetime, timezone

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


def _codex_http_with_details(expires_in: float = 2 * DAY):
    """Codex transport serving the usage payload plus its credit details."""
    expires_at = (
        datetime.fromtimestamp(NOW + expires_in, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

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
                    "rate_limit_reset_credits": {"available_count": 1},
                }
            ).encode()
        if "rate-limit-reset-credits" in url:
            return 200, json.dumps(
                {
                    "available_count": 1,
                    "credits": [
                        {
                            "reset_type": "codex_rate_limits",
                            "status": "available",
                            "granted_at": "2026-08-28T00:00:00Z",
                            "expires_at": expires_at,
                            "title": "Usage limit reset",
                        }
                    ],
                }
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
        # fallback reorder scores from
        assert state["readings"]["codex"] == {
            "pct": 100,
            "reset_seconds": float(WEEK),
            "label": "Codex",
            "reset_count": 1,
            "reset_expiry_seconds": float(2 * DAY),
        }
        assert result["providers"]["Codex"]["reset_expiry_seconds"] == float(2 * DAY)
        assert "reset_error" not in result["providers"]["Codex"]

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
