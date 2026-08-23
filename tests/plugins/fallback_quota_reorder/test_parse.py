"""Strict channel-name parsing contracts for fallback_quota_reorder."""

from __future__ import annotations

import pytest

from plugins.fallback_quota_reorder.core import parse_channel_name

BULLET = "\u2022"


class TestParseChannelNameAccept:
    def test_grok_standard_form(self):
        reading = parse_channel_name("grok", f"Grok: 75% {BULLET} 8h left")
        assert reading is not None
        assert reading.provider == "xai-oauth"
        assert reading.pct == 75
        assert reading.reset_seconds == 8 * 3600

    def test_cursor_dual_percent_form(self):
        reading = parse_channel_name("cursor", f"Cursor: 76%/58% {BULLET} 25d left")
        assert reading is not None
        assert reading.provider == "cursor"
        assert reading.pct == 58
        assert reading.reset_seconds == 25 * 86400


class TestParseChannelNameReject:
    @pytest.mark.parametrize(
        "channel_key,name",
        [
            (
                "codex",
                f"Codex: 99% {BULLET} 2.2B tok/7d {BULLET} 7d le",
            ),
            ("grok", f"Grok: 75% | 8h left"),
            ("grok", "Grok: 75% - 8h left"),
            ("grok", f"Grok: 75% {BULLET} 8h"),
            ("grok", f"Grok: -5% {BULLET} 8h left"),
            ("grok", f"Grok: abc% {BULLET} 8h left"),
            ("grok", ""),
            ("cursor", f"Grok: 75% {BULLET} 8h left"),
            ("grok", f"Cursor: 76%/58% {BULLET} 25d left"),
        ],
        ids=[
            "truncated_mid_token",
            "wrong_separator_pipe",
            "wrong_separator_dash",
            "missing_left",
            "negative_pct",
            "garbage_pct",
            "empty_name",
            "standard_form_on_cursor_key",
            "cursor_form_on_standard_key",
        ],
    )
    def test_returns_none(self, channel_key: str, name: str):
        assert parse_channel_name(channel_key, name) is None
