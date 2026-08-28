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


class TestParseResetsSegment:
    """Trailing pending-reset segment rendered by quota_channels (Codex/Grok)."""

    def test_zero_resets(self):
        reading = parse_channel_name(
            "grok", f"Grok: 43% {BULLET} 3d left {BULLET} 0 resets"
        )
        assert reading is not None
        assert reading.pct == 43
        assert reading.reset_seconds == 3 * 86400
        assert reading.reset_count == 0
        assert reading.reset_expiry_seconds is None

    def test_codex_count_only_has_no_expiry_clock(self):
        reading = parse_channel_name(
            "codex",
            f"Codex: 100% {BULLET} 571.4M tok/7d {BULLET} 7d left {BULLET} 1 reset",
        )
        assert reading is not None
        assert reading.pct == 100
        assert reading.reset_seconds == 7 * 86400
        assert reading.reset_count == 1
        # no `in <t>` countdown: the reset term falls back to the usage clock
        assert reading.reset_expiry_seconds is None

    def test_grok_count_with_expiry(self):
        reading = parse_channel_name(
            "grok", f"Grok: 46% {BULLET} 3d left {BULLET} 1 reset in 2d"
        )
        assert reading is not None
        assert reading.pct == 46
        assert reading.reset_seconds == 3 * 86400
        assert reading.reset_count == 1
        assert reading.reset_expiry_seconds == 2 * 86400

    def test_plural_count_with_hour_expiry(self):
        reading = parse_channel_name(
            "grok", f"Grok: 46% {BULLET} 3d left {BULLET} 2 resets in 5h"
        )
        assert reading is not None
        assert reading.reset_count == 2
        assert reading.reset_expiry_seconds == 5 * 3600

    def test_cursor_form_stays_resets_free(self):
        # Cursor has no resets API, so its regex never grows the segment
        assert (
            parse_channel_name(
                "cursor", f"Cursor: 76%/58% {BULLET} 25d left {BULLET} 1 reset"
            )
            is None
        )

    def test_kimi_resets_segment_parses_but_stays_inert(self):
        # Kimi has no resets API: the shared regex still accepts the segment
        # so a polluted name stays readable, but only Codex/Grok extract it
        reading = parse_channel_name(
            "kimi", f"Kimi: 10% {BULLET} 7d left {BULLET} 3 resets"
        )
        assert reading is not None
        assert reading.pct == 10
        assert reading.reset_seconds == 7 * 86400
        assert not reading.reset_count
        assert reading.reset_expiry_seconds is None

    def test_zai_resets_segment_with_expiry_stays_inert(self):
        reading = parse_channel_name(
            "zai", f"z.ai: 70% {BULLET} 7d left {BULLET} 1 reset in 2d"
        )
        assert reading is not None
        assert reading.pct == 70
        assert not reading.reset_count
        assert reading.reset_expiry_seconds is None

    def test_garbage_resets_segment_is_rejected(self):
        assert (
            parse_channel_name("grok", f"Grok: 46% {BULLET} 3d left {BULLET} resets")
            is None
        )


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
