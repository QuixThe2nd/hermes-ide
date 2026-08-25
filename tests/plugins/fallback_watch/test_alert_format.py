"""Alert formatting contracts, including the suppressed-events note."""

from __future__ import annotations

from plugins.fallback_watch.core import format_alert, parse_fallback_line
from tests.plugins.fallback_watch._helpers import NO_SESSION_LINE, SAMPLE_LINE


def _event():
    event = parse_fallback_line(SAMPLE_LINE)
    assert event is not None
    return event


def _event_without_session():
    event = parse_fallback_line(NO_SESSION_LINE)
    assert event is not None
    return event


class TestFormatAlert:
    def test_first_line_is_the_warning_header(self):
        assert format_alert(_event()).splitlines()[0] == (
            "⚠️ Hermes primary model fallback activated"
        )

    def test_fields_are_present_and_backticked(self):
        lines = format_alert(_event()).splitlines()
        assert "Primary: `stealth/ox-alpha`" in lines
        assert "Fallback: `grok-4.6` via `xai-oauth`" in lines
        assert "Session: `20260825_153208_64d08c2b`" in lines
        assert "Time: `2026-08-25 15:32:15,579`" in lines

    def test_unknown_session_renders_as_unknown(self):
        assert (
            "Session: `unknown`" in format_alert(_event_without_session()).splitlines()
        )

    def test_no_note_line_when_nothing_suppressed(self):
        assert "suppressed" not in format_alert(_event(), suppressed=0)

    def test_suppressed_count_appears_in_note_line(self):
        message = format_alert(_event(), suppressed=3)
        note = [line for line in message.splitlines() if "suppressed" in line]
        assert note == [
            "Note: `3` additional fallback event(s) were suppressed during cooldown."
        ]
