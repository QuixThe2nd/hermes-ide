"""Fallback-line parsing and session-extraction contracts."""

from __future__ import annotations

import pytest

from plugins.fallback_watch.core import parse_fallback_line
from tests.plugins.fallback_watch._helpers import NO_SESSION_LINE, SAMPLE_LINE


class TestParseAccept:
    def test_real_shaped_line_parses_every_field(self):
        event = parse_fallback_line(SAMPLE_LINE)
        assert event is not None
        assert event.timestamp == "2026-08-25 15:32:15,579"
        assert event.from_model == "stealth/ox-alpha"
        assert event.to_model == "grok-4.6"
        assert event.provider == "xai-oauth"
        assert event.session == "20260825_153208_64d08c2b"

    def test_session_defaults_to_unknown_without_bracket(self):
        event = parse_fallback_line(NO_SESSION_LINE)
        assert event is not None
        assert event.session == "unknown"

    def test_line_is_preserved_for_replay_dedup(self):
        event = parse_fallback_line(SAMPLE_LINE)
        assert event is not None
        assert event.line == SAMPLE_LINE

    @pytest.mark.parametrize(
        "line",
        [
            # provider slugs with hyphens and dots
            "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] x: "
            "Fallback activated: a → b (openai-codex)",
            "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] x: "
            "Fallback activated: provider/model-name → other/model-name (us.vendor.api)",
            # millisecond timestamp at a different level (WARNING)
            "2026-08-25 15:32:15,579 WARNING [20260825_153208_ab] x: "
            "Fallback activated: a → b (p)",
        ],
    )
    def test_provider_and_model_shapes(self, line: str):
        event = parse_fallback_line(line)
        assert event is not None
        assert event.provider
        assert event.from_model
        assert event.to_model


class TestParseReject:
    @pytest.mark.parametrize(
        "line",
        [
            # ordinary log lines
            "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] agent: nothing to see",
            "",
            # missing timestamp prefix
            "[20260825_153208_64d08c2b] Fallback activated: a → b (p)",
            # ASCII arrow instead of →
            "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] x: "
            "Fallback activated: a -> b (p)",
            # missing provider parens
            "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] x: "
            "Fallback activated: a → b",
            # different message, similar words
            "2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] x: "
            "Fallback deactivated: a → b (p)",
        ],
        ids=[
            "ordinary_line",
            "empty",
            "no_timestamp",
            "ascii_arrow",
            "no_provider",
            "deactivated",
        ],
    )
    def test_returns_none(self, line: str):
        assert parse_fallback_line(line) is None
