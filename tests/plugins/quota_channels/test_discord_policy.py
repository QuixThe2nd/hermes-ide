"""Discord 429 tolerance policy tests."""

from __future__ import annotations

import json

import pytest

from plugins.quota_channels.core import QuotaChannelsError, apply_position_moves, rename_channel


def _headers():
    return {"Authorization": "Bot test", "Content-Type": "application/json"}


class TestDiscord429Policy:
    def test_category_rename_429_is_skipped(self):
        calls = {"patch": 0}

        def fake_http(req, timeout=25.0):
            method = getattr(req, "method", None) or "GET"
            if method == "GET":
                return 200, json.dumps({"name": "old-name"}).encode()
            if method == "PATCH":
                calls["patch"] += 1
                return 429, b'{"message":"rate limited"}'
            raise AssertionError((method, req.full_url))

        result = rename_channel(
            "cat-id",
            "Quotas \u2022 21/8 6:53pm \u2022 Next: 7:23pm",
            _headers(),
            skip_on_429=True,
            http_fn=fake_http,
        )
        assert result == "skipped"
        assert calls["patch"] == 1

    def test_voice_rename_429_is_error(self):
        def fake_http(req, timeout=25.0):
            method = getattr(req, "method", None) or "GET"
            if method == "GET":
                return 200, json.dumps({"name": "old-name"}).encode()
            if method == "PATCH":
                return 429, b'{"message":"rate limited"}'
            raise AssertionError((method, req.full_url))

        with pytest.raises(QuotaChannelsError, match="discord rename returned 429"):
            rename_channel(
                "voice-id",
                "Codex: 50% \u2022 1d left",
                _headers(),
                skip_on_429=False,
                http_fn=fake_http,
            )

    def test_position_patch_429_is_error(self):
        def fake_http(req, timeout=25.0):
            return 429, b'{"message":"rate limited"}'

        with pytest.raises(
            QuotaChannelsError, match="channel-position PATCH rate-limited"
        ):
            apply_position_moves(
                "guild",
                [{"id": "c1", "position": 2}],
                _headers(),
                http_fn=fake_http,
            )
