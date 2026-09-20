"""Z.AI manual reset cards: read-only list fetch, parsing, and plumbing.

The reset-card list rides alongside the normal quota/limit read and matches
the window the z.ai row represents: the weekly row (unit 6) counts
``weekResets`` — a weekly reset refills both the weekly and the 5h window —
while a 5h-only payload (unit 3) counts ``fiveHourResets`` and never scores
a 5h reset as a weekly full wallet. Card expiry timestamps are naive Z.AI
platform times, read consistently as UTC+8.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plugins.fallback_quota_reorder.core import (
    QuotaReading,
    is_low_quota,
    score_provider,
)
from plugins.quota_channels import core as core
from plugins.quota_channels.core import (
    QuotaChannelsError,
    ResetCredits,
    _zai_platform_epoch,
    fetch_zai_reset_list,
    format_zai_name,
    load_state,
    parse_zai_reset_cards,
    parse_zai_usage,
    quota_display_ranks,
    run_provider_quota,
    run_tick,
    run_zai_provider,
    state_path,
    validate_quota_config,
    zai_reset_cards,
)

DAY = 86400
WEEK = 7 * DAY
BULLET = "•"

# fixed clock: 2026-09-02 04:00:00 UTC = 12:00:00 in UTC+8 platform time
NOW = datetime(2026, 9, 2, 4, 0, 0, tzinfo=timezone.utc).timestamp()

# sanitized observed shape: one usable weekly card with a naive expiry
OBSERVED_EXPIRE_TIME = "2026-10-01 23:59:59"
# the platform timestamp is UTC+8 — the UTC reading of the same wall time
# would be eight hours later
OBSERVED_EXPIRE_EPOCH = datetime(
    2026, 10, 1, 23, 59, 59, tzinfo=timezone(timedelta(hours=8))
).timestamp()
OBSERVED_EXPIRY_SECS = OBSERVED_EXPIRE_EPOCH - NOW

_WEEKLY_WINDOW = {"unit": 6, "number": 1}
_FIVE_HOUR_WINDOW = {"unit": 3, "number": 5}


def _card(expire_time=OBSERVED_EXPIRE_TIME, available=True) -> dict:
    return {"recordId": 111111, "expireTime": expire_time, "available": available}


def _reset_list_body(week_cards=(), five_hour_cards=(), **data_extra) -> str:
    data = {
        "targetType": "PERSONAL",
        "lastFiveHourResetTime": None,
        "lastWeekResetTime": None,
        "fiveHourResets": list(five_hour_cards),
        "weekResets": list(week_cards),
    }
    data.update(data_extra)
    return json.dumps(
        {"code": 200, "msg": "Operation successful", "data": data, "success": True}
    )


def _usage_body(windows) -> str:
    return json.dumps({"data": {"limits": windows}})


def _weekly_window(percentage: int = 100, reset_in: float = 4 * DAY) -> dict:
    return {
        "type": "CREDIT_LIMIT",
        "unit": 6,
        "number": 1,
        "usage": 140000,
        "currentValue": 140000,
        "remaining": 0,
        "percentage": percentage,
        "nextResetTime": int((NOW + reset_in) * 1000),
    }


def _five_hour_window(percentage: int = 0, reset_in: float = 3 * 3600) -> dict:
    return {
        "type": "CREDIT_LIMIT",
        "unit": 3,
        "number": 5,
        "usage": 28000,
        "currentValue": 0,
        "remaining": 28000,
        "percentage": percentage,
        "nextResetTime": int((NOW + reset_in) * 1000),
    }


def _write_zai_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "zai.env").write_text("ZAI_API_KEY=raw-zai-key\n")


def _discord(req):
    method = getattr(req, "method", None) or req.get_method()
    if method == "GET":
        return 200, json.dumps({"name": "old-name"}).encode()
    if method == "PATCH":
        return 200, json.dumps({"name": "patched"}).encode()
    raise AssertionError((method, req.full_url))


class TestFetchContract:
    def test_reset_list_fetch_uses_raw_key_and_personal_target(self):
        seen = []

        def fake_http(req, timeout=25.0):
            seen.append(
                (
                    req.full_url,
                    getattr(req, "method", None) or req.get_method(),
                    req.headers.get("Authorization"),
                )
            )
            return 200, _reset_list_body().encode()

        status, _ = fetch_zai_reset_list("raw-zai-key", http_fn=fake_http)
        assert status == 200
        [(url, method, auth)] = seen
        assert url == (
            "https://api.z.ai/api/biz/customer-package-reset/list"
            "?targetType=PERSONAL"
        )
        assert method == "GET"
        # the existing raw-key convention, NOT a Bearer prefix
        assert auth == "raw-zai-key"


class TestPlatformTimestamp:
    def test_naive_platform_time_reads_as_utc_plus_8(self):
        assert _zai_platform_epoch(OBSERVED_EXPIRE_TIME) == OBSERVED_EXPIRE_EPOCH
        # eight hours ahead of the (wrong) UTC reading of the same wall time
        utc_reading = datetime(
            2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc
        ).timestamp()
        assert _zai_platform_epoch(OBSERVED_EXPIRE_TIME) == utc_reading - 8 * 3600

    @pytest.mark.parametrize(
        "value",
        [
            "2026-10-01T23:59:59",  # T separator is not the platform shape
            "2026-10-01 23:59:59+08:00",  # tz-qualified strings are not read
            "not-a-date",
            "",
            None,
            1782977799,
        ],
        ids=["t_separator", "tz_qualified", "garbage", "empty", "null", "epoch_int"],
    )
    def test_unparseable_values_are_unreadable(self, value):
        assert _zai_platform_epoch(value) is None


class TestObservedPayloadShape:
    def test_observed_weekly_card_counts_with_its_real_horizon(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card()]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(
            1, OBSERVED_EXPIRY_SECS, (OBSERVED_EXPIRY_SECS,)
        )

    def test_empty_lists_are_zero_pending(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(), _WEEKLY_WINDOW, now_fn=lambda: NOW
        )
        assert resets == ResetCredits(0, None, ())

    def test_each_usable_card_keeps_its_own_horizon_earliest_first(self):
        later = (
            datetime.fromtimestamp(NOW + 9 * DAY, tz=timezone(timedelta(hours=8)))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        sooner = (
            datetime.fromtimestamp(NOW + 3 * DAY, tz=timezone(timedelta(hours=8)))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card(later), _card(sooner)]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(2, 3 * DAY, (3 * DAY, 9 * DAY))


class TestCardFiltering:
    def test_unavailable_cards_do_not_count(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(
                week_cards=[_card(available=False), {"expireTime": OBSERVED_EXPIRE_TIME}]
            ),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        # available=false is unusable; a missing available flag is not `true`
        assert resets == ResetCredits(0, None, ())

    def test_expired_cards_do_not_count(self):
        past = (
            datetime.fromtimestamp(NOW - DAY, tz=timezone(timedelta(hours=8)))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card(past), _card()]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(1, OBSERVED_EXPIRY_SECS, (OBSERVED_EXPIRY_SECS,))

    @pytest.mark.parametrize(
        "expire_time",
        ["not-a-date", "", None, 17, "2026-10-01T23:59:59"],
        ids=["garbage", "empty", "null", "numeric", "t_separator"],
    )
    def test_malformed_expiry_keeps_the_card_without_a_clock(self, expire_time):
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card(expire_time)]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(1, None, ())

    def test_missing_expiry_key_counts_the_card_without_a_horizon(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[{"available": True}]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(1, None, ())

    def test_non_mapping_cards_are_skipped(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card(), "junk", 3]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(1, OBSERVED_EXPIRY_SECS, (OBSERVED_EXPIRY_SECS,))


class TestWindowMatching:
    def test_weekly_window_counts_week_resets_only(self):
        # a weekly reset refills both windows, so weekResets applies to the
        # weekly row; fiveHourResets next to it are not double-counted
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card()], five_hour_cards=[_card(), _card()]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets == ResetCredits(1, OBSERVED_EXPIRY_SECS, (OBSERVED_EXPIRY_SECS,))

    def test_five_hour_only_window_counts_five_hour_resets(self):
        # a payload whose longest window is the 5h one (unit 3) is refilled
        # by fiveHourResets; a weekly card must not score as a 5h refill
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card()], five_hour_cards=[_card(), _card()]),
            _FIVE_HOUR_WINDOW,
            now_fn=lambda: NOW,
        )
        assert resets.count == 2
        assert resets.expiry_horizons == (OBSERVED_EXPIRY_SECS,) * 2

    def test_legacy_window_without_unit_reads_week_resets(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card()], five_hour_cards=[_card()]),
            {},
            now_fn=lambda: NOW,
        )
        assert resets.count == 1

    def test_metrics_match_the_window_parse_zai_usage_selects(
        self, monkeypatch, tmp_path
    ):
        # end-to-end: the 5h-only usage payload must drive the fiveHourResets
        # lookup, so the weekly card stays out of the 5h row
        _write_zai_key(monkeypatch, tmp_path)
        usage = _usage_body([_five_hour_window(percentage=0)])

        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, usage.encode()
            assert "customer-package-reset/list" in req.full_url
            return 200, _reset_list_body(
                week_cards=[_card()], five_hour_cards=[_card()]
            ).encode()

        remaining, reset_secs, resets, error = core._zai_quota_metrics(
            http_fn=fake_http, now_fn=lambda: NOW
        )
        assert (remaining, reset_secs) == parse_zai_usage(usage, now_fn=lambda: NOW)
        assert error is None
        # only the 5h card counts — the weekly one is a different wallet
        assert resets.count == 1

    def test_metrics_weekly_row_reads_week_resets(self, monkeypatch, tmp_path):
        _write_zai_key(monkeypatch, tmp_path)
        usage = _usage_body([_five_hour_window(), _weekly_window()])

        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, usage.encode()
            assert "customer-package-reset/list" in req.full_url
            return 200, _reset_list_body(
                week_cards=[_card()], five_hour_cards=[_card(), _card()]
            ).encode()

        _, _, resets, error = core._zai_quota_metrics(
            http_fn=fake_http, now_fn=lambda: NOW
        )
        assert error is None
        assert resets == ResetCredits(1, OBSERVED_EXPIRY_SECS, (OBSERVED_EXPIRY_SECS,))


class TestEnvelopeStrictness:
    @pytest.mark.parametrize(
        "body",
        [
            "not-json",
            json.dumps([]),
            json.dumps({"code": 500, "msg": "boom", "data": None}),
            json.dumps({"code": 200, "data": "bad"}),
            json.dumps({"code": 200, "data": {}}),
            json.dumps({"code": 200, "data": {"weekResets": "bad"}}),
        ],
        ids=[
            "invalid_json",
            "non_dict",
            "error_code",
            "data_not_mapping",
            "missing_list",
            "list_not_a_list",
        ],
    )
    def test_malformed_envelope_raises(self, body):
        with pytest.raises(QuotaChannelsError):
            parse_zai_reset_cards(body, _WEEKLY_WINDOW, now_fn=lambda: NOW)


class TestFailureDegradation:
    def _http(self, *, reset_status: int = 200, reset_body: bytes = b""):
        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, _usage_body([_weekly_window(percentage=100)]).encode()
            if "customer-package-reset/list" in req.full_url:
                return reset_status, reset_body
            return _discord(req)

        return fake_http

    def test_reset_http_failure_keeps_quota_fresh_and_records_error(
        self, monkeypatch, tmp_path
    ):
        _write_zai_key(monkeypatch, tmp_path)
        label, reset_secs, name, rename, info = run_provider_quota(
            "zai",
            "303",
            {"Authorization": "Bot test"},
            http_fn=self._http(reset_status=500, reset_body=b"reset down"),
            now_fn=lambda: NOW,
        )
        assert label == "z.ai"
        assert reset_secs == 4 * DAY
        # the segment is dropped, never invented from the quota clock
        assert name == f"z.ai: 0% {BULLET} 4d left"
        assert rename == "renamed"
        assert "z.ai reset-list endpoint returned 500" in info["reset_error"]
        assert "reset_count" not in info

    def test_reset_shape_failure_degrades_the_same_way(self, monkeypatch, tmp_path):
        _write_zai_key(monkeypatch, tmp_path)
        _, _, name, _, info = run_provider_quota(
            "zai",
            "303",
            {"Authorization": "Bot test"},
            http_fn=self._http(reset_body=b"<html>not json</html>"),
            now_fn=lambda: NOW,
        )
        assert name == f"z.ai: 0% {BULLET} 4d left"
        assert "invalid reset-list payload JSON" in info["reset_error"]

    def test_reset_network_error_never_stales_the_quota_read(
        self, monkeypatch, tmp_path
    ):
        _write_zai_key(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, _usage_body([_weekly_window(percentage=100)]).encode()
            if "customer-package-reset/list" in req.full_url:
                raise QuotaChannelsError("network error: TimeoutError: timed out")
            return _discord(req)

        _, reset_secs, name, rename, info = run_provider_quota(
            "zai",
            "303",
            {"Authorization": "Bot test"},
            http_fn=fake_http,
            now_fn=lambda: NOW,
        )
        assert reset_secs == 4 * DAY
        assert name == f"z.ai: 0% {BULLET} 4d left"
        assert rename == "renamed"
        assert "network error" in info["reset_error"]

    def test_reset_error_never_leaks_the_api_key(self, monkeypatch, tmp_path):
        _write_zai_key(monkeypatch, tmp_path)
        _, _, _, _, info = run_provider_quota(
            "zai",
            "303",
            {"Authorization": "Bot test"},
            http_fn=self._http(reset_status=500, reset_body=b"echo raw-zai-key"),
            now_fn=lambda: NOW,
        )
        assert "raw-zai-key" not in info["reset_error"]
        assert "[redacted]" in info["reset_error"]

    def test_zai_reset_cards_helper_never_raises(self, monkeypatch, tmp_path):
        _write_zai_key(monkeypatch, tmp_path)

        def boom(req, timeout=25.0):
            raise QuotaChannelsError("network error: URLError: down")

        resets, error = zai_reset_cards(
            "raw-zai-key", _WEEKLY_WINDOW, http_fn=boom, now_fn=lambda: NOW
        )
        assert resets is None
        assert "network error" in error


class TestLabel:
    def test_observed_card_renders_the_compact_reset_segment(self):
        resets = parse_zai_reset_cards(
            _reset_list_body(week_cards=[_card()]),
            _WEEKLY_WINDOW,
            now_fn=lambda: NOW,
        )
        # 29.5 days out, so the granular countdown rounds up to 30d
        assert format_zai_name(0, 4 * DAY, resets=resets) == (
            f"z.ai: 0% {BULLET} 4d left {BULLET} 1 reset in 30d"
        )

    def test_resets_segment_sits_after_the_token_segment(self):
        assert format_zai_name(
            74, 4 * DAY, tokens_7d=250_000_000, resets=ResetCredits(2, 5 * DAY)
        ) == f"z.ai: 74% {BULLET} 250.0M tok/7d {BULLET} 4d left {BULLET} 2 resets in 5d"

    def test_no_resets_keeps_the_legacy_name(self):
        assert format_zai_name(74, 4 * DAY, resets=None) == (
            f"z.ai: 74% {BULLET} 4d left"
        )
        assert format_zai_name(74, 4 * DAY) == f"z.ai: 74% {BULLET} 4d left"

    def test_provider_run_renders_the_observed_label(self, monkeypatch, tmp_path):
        _write_zai_key(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, _usage_body([_weekly_window(percentage=100)]).encode()
            assert "customer-package-reset/list" in req.full_url
            return 200, _reset_list_body(week_cards=[_card()]).encode()

        name, reset_secs, label = run_zai_provider(
            http_fn=fake_http, now_fn=lambda: NOW
        )
        assert label == "z.ai"
        assert reset_secs == 4 * DAY
        assert name == f"z.ai: 0% {BULLET} 4d left {BULLET} 1 reset in 30d"


class TestStatePersistence:
    def test_tick_persists_reset_fields_and_debug_output(self, monkeypatch, tmp_path):
        _write_zai_key(monkeypatch, tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, _usage_body([_weekly_window(percentage=100)]).encode()
            if "customer-package-reset/list" in req.full_url:
                return 200, _reset_list_body(week_cards=[_card()]).encode()
            return _discord(req)

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "303"},
                "enabled_providers": ["zai"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=fake_http,
        )

        assert result["success"] is True
        provider = result["providers"]["z.ai 1"]
        assert provider["reset_count"] == 1
        assert provider["reset_expiry_seconds"] == OBSERVED_EXPIRY_SECS
        assert provider["reset_expiry_horizons"] == [OBSERVED_EXPIRY_SECS]
        assert "reset_error" not in provider
        state = load_state()
        assert state["readings"]["zai"] == {
            "pct": 0,
            "reset_seconds": float(4 * DAY),
            "label": "z.ai",
            "reset_count": 1,
            "reset_expiry_seconds": OBSERVED_EXPIRY_SECS,
            "reset_expiry_horizons": [OBSERVED_EXPIRY_SECS],
        }
        assert state["readings"]["zai:legacy-env"]["pct"] == 0

    def test_tick_with_failed_reset_lookup_persists_no_reset_fields(
        self, monkeypatch, tmp_path
    ):
        _write_zai_key(monkeypatch, tmp_path)
        monkeypatch.setattr(core, "discord_headers", lambda: {"Authorization": "Bot x"})
        monkeypatch.setattr(core, "TOKEN_FETCHERS", {})
        monkeypatch.setattr(core, "sort_voice_channels", lambda *a, **k: False)
        monkeypatch.setattr(core, "update_category", lambda *a, **k: "renamed")

        def fake_http(req, timeout=25.0):
            if "quota/limit" in req.full_url:
                return 200, _usage_body([_weekly_window(percentage=40)]).encode()
            if "customer-package-reset/list" in req.full_url:
                return 503, b"reset down"
            return _discord(req)

        result = run_tick(
            validate_quota_config({
                "guild_id": "guild",
                "category_id": "cat",
                "channel_ids": {"zai": "303"},
                "enabled_providers": ["zai"],
            }),
            force=True,
            sleep_fn=lambda _: None,
            now_fn=lambda: NOW,
            http_fn=fake_http,
        )

        assert result["success"] is True
        provider = result["providers"]["z.ai 1"]
        assert provider["remaining"] == 60  # the normal quota read stayed fresh
        assert "z.ai reset-list endpoint returned 503" in provider["reset_error"]
        state = load_state()
        # the reading carries fresh quota and no invented reset fields
        assert state["readings"]["zai"] == {
            "pct": 60,
            "reset_seconds": float(4 * DAY),
            "label": "z.ai",
        }
        assert state["readings"]["zai:legacy-env"]["pct"] == 60
        assert state_path().exists()


class TestSharedRanking:
    def _reading(self, pct, reset_seconds, **extra):
        return QuotaReading(
            channel_key="zai",
            provider="zai",
            channel_name="",
            pct=pct,
            reset_seconds=reset_seconds,
            reset_count=extra.get("reset_count", 0),
            reset_expiry_seconds=extra.get("reset_expiry_seconds"),
            reset_expiry_horizons=extra.get("reset_expiry_horizons"),
        )

    def test_zai_reset_scores_a_full_wallet_on_its_own_clock(self):
        reading = self._reading(
            0, WEEK, reset_count=1, reset_expiry_seconds=WEEK
        )
        assert score_provider(reading) == pytest.approx(1.0)

    def test_zai_reset_lifts_the_low_quota_sink(self):
        assert not is_low_quota(self._reading(0, WEEK, reset_count=1))

    def test_zai_unknown_expiry_scores_nothing_but_stays_counted(self):
        reading = self._reading(0, WEEK, reset_count=1)
        assert score_provider(reading) == 0.0
        assert not is_low_quota(reading)

    def test_zai_per_credit_horizons_score_each_clock_once(self):
        reading = self._reading(
            0,
            WEEK,
            reset_count=2,
            reset_expiry_seconds=3 * DAY,
            reset_expiry_horizons=(3 * DAY, 9 * DAY),
        )
        assert score_provider(reading) == pytest.approx(
            168.0 / 72.0 + 168.0 / 216.0
        )

    def test_display_ranks_keep_a_reset_backed_zai_row_healthy(self):
        readings = {
            "zai": {
                "pct": 0,
                "reset_seconds": float(WEEK),
                "label": "z.ai",
                "reset_count": 1,
                "reset_expiry_seconds": float(WEEK),
                "reset_expiry_horizons": [float(WEEK)],
            },
            # no resets: sinks into the low-quota bucket
            "kimi": {"pct": 0, "reset_seconds": float(WEEK), "label": "Kimi"},
        }
        ranks = quota_display_ranks(readings)
        assert ranks["zai"] < ranks["kimi"]
