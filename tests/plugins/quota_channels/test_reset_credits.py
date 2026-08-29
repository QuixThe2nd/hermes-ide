"""Pending usage-limit reset counter tests for quota_channels."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from plugins.quota_channels.core import (
    QuotaChannelsError,
    ResetCredits,
    _remaining_from_name,
    fetch_grok_resets,
    format_codex_name,
    format_grok_name,
    format_resets_segment,
    grok_reset_credits,
    parse_codex_reset_credits,
    parse_grok_resets,
    parse_token_segment_from_name,
    run_codex_provider,
    run_grok_provider,
    run_provider_quota,
)
from tests.plugins.quota_channels.test_parsers import (
    _build_grok_grpc_body,
    _pb_length_delimited,
    _pb_varint_field,
)

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400


def _build_grok_resets_token(expiry_epoch: int | None = None) -> bytes:
    token = _pb_length_delimited(2, b"reset-token")
    if expiry_epoch is not None:
        validity = _pb_varint_field(1, expiry_epoch) + _pb_varint_field(2, 278414000)
        token += _pb_length_delimited(30, validity)
    return token


def _build_grok_resets_body(tokens: list[bytes]) -> bytes:
    message = b"".join(_pb_length_delimited(10, token) for token in tokens)
    return b"\x00" + len(message).to_bytes(4, "big") + message


def _write_grok_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {
                            "access_token": "grok-tok",
                            "refresh_token": "grok-ref",
                        }
                    }
                }
            }
        )
    )


def _write_codex_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {"access_token": "codex-tok", "refresh_token": "codex-ref"}
                    }
                }
            }
        )
    )


class TestResetsSegmentFormat:
    @pytest.mark.parametrize(
        ("resets", "expected"),
        [
            (ResetCredits(0), "0 resets"),
            (ResetCredits(1), "1 reset"),
            (ResetCredits(2), "2 resets"),
            (ResetCredits(1, 2 * DAY), "1 reset in 2d"),
            (ResetCredits(2, 5 * 3600), "2 resets in 5h"),
            (ResetCredits(2, 45 * 60 + 1), "2 resets in 46m"),
            (ResetCredits(0, 2 * DAY), "0 resets"),
            (ResetCredits(2, None), "2 resets"),
        ],
        ids=[
            "zero",
            "singular",
            "plural",
            "singular_with_expiry",
            "plural_hours",
            "plural_minutes_rounds_up",
            "zero_ignores_expiry",
            "no_expiry_is_count_only",
        ],
    )
    def test_segment_shapes(self, resets, expected):
        assert format_resets_segment(resets) == expected


class TestCodexResets:
    def test_count_flows_from_usage_payload(self):
        payload = json.dumps(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 0,
                        "reset_after_seconds": 7 * DAY,
                    }
                },
                "rate_limit_reset_credits": {"available_count": 2},
            }
        )
        assert parse_codex_reset_credits(payload) == ResetCredits(2)
        assert format_codex_name(100, 7 * DAY, resets=ResetCredits(2)) == (
            "Codex: 100% \u2022 7d left \u2022 2 resets"
        )

    def test_segment_sits_after_token_segment(self):
        assert (
            format_codex_name(
                99, 7 * DAY, tokens_7d=2_234_567_890, resets=ResetCredits(2)
            )
            == "Codex: 99% \u2022 2.2B tok/7d \u2022 7d left \u2022 2 resets"
        )

    @pytest.mark.parametrize(
        "payload",
        [
            json.dumps({"rate_limit": {"primary_window": {}}}),
            json.dumps({"rate_limit_reset_credits": {"available_count": "x"}}),
            json.dumps({"rate_limit_reset_credits": "bad"}),
            json.dumps({"rate_limit_reset_credits": {"available_count": 1e999}}),
            "not-json",
            json.dumps([]),
        ],
        ids=[
            "key_absent",
            "count_not_numeric",
            "credits_not_a_mapping",
            "count_overflow",
            "invalid_json",
            "non_dict",
        ],
    )
    def test_missing_or_bad_block_drops_the_segment(self, payload):
        # graceful degradation: no resets segment, never a failed tick
        assert parse_codex_reset_credits(payload) is None
        assert format_codex_name(99, 7 * DAY, resets=None) == "Codex: 99% \u2022 7d left"

    def test_present_block_without_count_is_zero(self):
        # the block exists, so the counter renders \u2014 just at zero, as upstream does
        payload = json.dumps({"rate_limit_reset_credits": {}})
        assert parse_codex_reset_credits(payload) == ResetCredits(0)

    def test_negative_count_clamps_to_zero(self):
        payload = json.dumps({"rate_limit_reset_credits": {"available_count": -3}})
        assert parse_codex_reset_credits(payload) == ResetCredits(0)

    def test_provider_run_renders_resets(self, monkeypatch, tmp_path):
        _write_codex_auth(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            assert "wham/usage" in req.full_url
            return 200, json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "reset_after_seconds": 7 * DAY,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 1},
                }
            ).encode()

        name, reset_secs, label = run_codex_provider(
            http_fn=fake_http, now_fn=lambda: NOW
        )
        assert label == "Codex"
        assert reset_secs == 7 * DAY
        assert name == "Codex: 95% \u2022 7d left \u2022 1 reset"

    def test_provider_run_without_block_keeps_legacy_name(self, monkeypatch, tmp_path):
        _write_codex_auth(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            return 200, json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "reset_after_seconds": 7 * DAY,
                        }
                    }
                }
            ).encode()

        name, _, _ = run_codex_provider(http_fn=fake_http, now_fn=lambda: NOW)
        assert name == "Codex: 95% \u2022 7d left"


class TestGrokResetsParsing:
    def test_two_tokens_with_one_expiry(self):
        body = _build_grok_resets_body(
            [
                _build_grok_resets_token(int(NOW + 2 * DAY)),
                _build_grok_resets_token(),
            ]
        )
        resets = parse_grok_resets(body, now_fn=lambda: NOW)
        assert resets == ResetCredits(2, 2 * DAY)

    def test_soonest_expiry_wins(self):
        body = _build_grok_resets_body(
            [
                _build_grok_resets_token(int(NOW + 5 * DAY)),
                _build_grok_resets_token(int(NOW + 2 * DAY)),
            ]
        )
        assert parse_grok_resets(body, now_fn=lambda: NOW) == ResetCredits(2, 2 * DAY)

    def test_empty_frame_is_zero_pending(self):
        resets = parse_grok_resets(b"\x00\x00\x00\x00\x00", now_fn=lambda: NOW)
        assert resets == ResetCredits(0, None)

    def test_past_expiry_clamps_to_zero(self):
        body = _build_grok_resets_body([_build_grok_resets_token(int(NOW - DAY))])
        assert parse_grok_resets(body, now_fn=lambda: NOW) == ResetCredits(1, 0.0)

    def test_truncated_frame_raises(self):
        body = _build_grok_resets_body(
            [_build_grok_resets_token(int(NOW + 2 * DAY))]
        )
        with pytest.raises(QuotaChannelsError):
            parse_grok_resets(body[:6], now_fn=lambda: NOW)

    def test_garbage_bytes_raise(self):
        with pytest.raises(QuotaChannelsError):
            parse_grok_resets(b"\xff\xfe\xfd\xfc\xfb", now_fn=lambda: NOW)

    def test_fetch_uses_the_resets_service_method(self):
        seen = []

        def fake_http(req, timeout=25.0):
            seen.append((req.full_url, req.method, req.data))
            return 200, b"\x00\x00\x00\x00\x00"

        status, body = fetch_grok_resets("grok-tok", http_fn=fake_http)
        assert status == 200
        assert body == b"\x00\x00\x00\x00\x00"
        url, method, data = seen[0]
        assert url == "https://grok.com/prod_mc_billing.ConsumerUiSvc/GetRemainingResets"
        assert method == "POST"
        assert data == b"\x00\x00\x00\x00\x00"


class TestGrokResetsResilience:
    def _billing_body(self):
        return _build_grok_grpc_body(54.0, int(NOW + 3 * DAY))

    def _no_calls(self, req, timeout=25.0):
        raise AssertionError(req.full_url)

    def test_provider_run_renders_resets_with_expiry(self, monkeypatch, tmp_path):
        _write_grok_auth(monkeypatch, tmp_path)
        resets_body = _build_grok_resets_body(
            [_build_grok_resets_token(int(NOW + 2 * DAY))]
        )

        def fake_http(req, timeout=25.0):
            if "GetGrokCreditsConfig" in req.full_url:
                return 200, self._billing_body()
            if "GetRemainingResets" in req.full_url:
                return 200, resets_body
            raise AssertionError(req.full_url)

        name, reset_secs, label = run_grok_provider(
            http_fn=fake_http, now_fn=lambda: NOW
        )
        assert label == "Grok"
        assert reset_secs == 3 * DAY
        assert name == "Grok: 46% \u2022 3d left \u2022 1 reset in 2d"

    def test_non_200_degrades_to_zero_without_failing_the_run(
        self, monkeypatch, tmp_path
    ):
        _write_grok_auth(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            if "GetGrokCreditsConfig" in req.full_url:
                return 200, self._billing_body()
            if "GetRemainingResets" in req.full_url:
                return 503, b"resets down"
            raise AssertionError(req.full_url)

        resets, error = grok_reset_credits(http_fn=fake_http, now_fn=lambda: NOW)
        assert resets == ResetCredits(0)
        assert "grok resets endpoint returned 503" in error

        name, reset_secs, _ = run_grok_provider(
            http_fn=fake_http, now_fn=lambda: NOW
        )
        assert reset_secs == 3 * DAY
        assert name == "Grok: 46% \u2022 3d left \u2022 0 resets"

    def test_unparseable_body_degrades_to_zero(self, monkeypatch, tmp_path):
        _write_grok_auth(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            if "GetGrokCreditsConfig" in req.full_url:
                return 200, self._billing_body()
            if "GetRemainingResets" in req.full_url:
                return 200, b"\xff\xfe\xfd\xfc\xfb"
            raise AssertionError(req.full_url)

        resets, error = grok_reset_credits(http_fn=fake_http, now_fn=lambda: NOW)
        assert resets == ResetCredits(0)
        assert error

    def test_missing_token_degrades_to_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "auth.json").write_text(json.dumps({"providers": {}}))
        resets, error = grok_reset_credits(
            http_fn=self._no_calls, now_fn=lambda: NOW
        )
        assert resets == ResetCredits(0)
        assert "no xai-oauth access token" in error


class TestNameConsumersUnaffected:
    # the resets segment is last, so percent/token parsers keep working

    def test_remaining_from_name_splits_on_first_segment(self):
        assert (
            _remaining_from_name("Grok: 46% \u2022 3d left \u2022 1 reset in 2d", "Grok") == 46
        )
        assert (
            _remaining_from_name("Codex: 99% \u2022 2.2B tok/7d \u2022 7d left \u2022 2 resets", "Codex")
            == 99
        )

    def test_token_segment_still_parseable(self):
        assert (
            parse_token_segment_from_name(
                "Codex: 99% \u2022 2.2B tok/7d \u2022 7d left \u2022 2 resets"
            )
            == "2.2B tok/7d"
        )


class TestProviderQuotaIntegration:
    HEADERS = {"Authorization": "Bot test", "Content-Type": "application/json"}

    def _discord(self, req):
        method = getattr(req, "method", None) or req.get_method()
        if method == "GET":
            return 200, json.dumps({"name": "old-name"}).encode()
        if method == "PATCH":
            return 200, json.dumps({"name": "patched"}).encode()
        raise AssertionError((method, req.full_url))

    def test_grok_reset_failure_recorded_and_quota_still_renames(
        self, monkeypatch, tmp_path
    ):
        _write_grok_auth(monkeypatch, tmp_path)
        billing = _build_grok_grpc_body(54.0, int(NOW + 3 * DAY))

        def fake_http(req, timeout=25.0):
            if "GetGrokCreditsConfig" in req.full_url:
                return 200, billing
            if "GetRemainingResets" in req.full_url:
                return 500, b"resets down"
            return self._discord(req)

        label, reset_secs, name, rename, info = run_provider_quota(
            "grok", "305", self.HEADERS, http_fn=fake_http, now_fn=lambda: NOW
        )
        assert label == "Grok"
        assert reset_secs == 3 * DAY
        assert name == "Grok: 46% \u2022 3d left \u2022 0 resets"
        assert rename == "renamed"
        assert "grok resets endpoint returned 500" in info["reset_error"]

    def test_codex_resets_rendered_with_token_segment(self, monkeypatch, tmp_path):
        _write_codex_auth(monkeypatch, tmp_path)

        def fake_http(req, timeout=25.0):
            url = req.full_url
            if "wham/usage" in url:
                return 200, json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 1,
                                "reset_after_seconds": 7 * DAY,
                            }
                        },
                        "rate_limit_reset_credits": {"available_count": 2},
                    }
                ).encode()
            if "profiles/me" in url:
                return 200, json.dumps(
                    {
                        "stats": {
                            "daily_usage_buckets": [
                                {
                                    "start_date": datetime.fromtimestamp(
                                        NOW, tz=timezone.utc
                                    ).date().isoformat(),
                                    "tokens": 2_234_567_890,
                                }
                            ]
                        }
                    }
                ).encode()
            return self._discord(req)

        label, reset_secs, name, rename, info = run_provider_quota(
            "codex", "301", self.HEADERS, http_fn=fake_http, now_fn=lambda: NOW
        )
        assert label == "Codex"
        assert reset_secs == 7 * DAY
        assert name == "Codex: 99% \u2022 2.2B tok/7d \u2022 7d left \u2022 2 resets"
        assert rename == "renamed"
        assert info["tokens_7d"] == 2_234_567_890
