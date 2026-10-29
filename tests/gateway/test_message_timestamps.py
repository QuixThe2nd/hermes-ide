from datetime import datetime
from zoneinfo import ZoneInfo

from gateway.message_timestamps import (
    coerce_message_timestamp,
    render_user_content_with_timestamp,
    strip_leading_message_timestamps,
)
from run_agent import AIAgent


BERLIN = ZoneInfo("Europe/Berlin")


def _epoch(year, month, day, hour, minute, second):
    return datetime(year, month, day, hour, minute, second, tzinfo=BERLIN).timestamp()


def test_render_user_content_deduplicates_existing_timestamp_and_preserves_embedded_time():
    db_processing_ts = _epoch(2026, 4, 27, 15, 55, 36)
    stored_content = (
        "[Mon 2026-04-27 15:54:44 CEST] "
        "[Example User] This should go on our todo list"
    )

    rendered = render_user_content_with_timestamp(
        stored_content,
        db_processing_ts,
        tz=BERLIN,
    )

    assert rendered == stored_content
    assert rendered.count("2026-04-27") == 1


# ---------------------------------------------------------------------------
# Opt-in gate: gateway.message_timestamps.enabled (default OFF)
# ---------------------------------------------------------------------------


def test_message_timestamps_enabled_defaults_off():
    from gateway.run import _message_timestamps_enabled

    assert _message_timestamps_enabled(None) is False
    assert _message_timestamps_enabled({}) is False
    assert _message_timestamps_enabled({"gateway": {}}) is False
    assert (
        _message_timestamps_enabled({"gateway": {"message_timestamps": {}}}) is False
    )


def test_build_history_injects_only_when_enabled():
    from gateway.run import _build_gateway_agent_history

    history = [
        {"role": "user", "content": "hello", "timestamp": _epoch(2026, 4, 28, 13, 40, 53)},
        {"role": "assistant", "content": "hi"},
    ]

    # Default (off): user content stays clean, no timestamp prefix.
    agent_history, _, _ = _build_gateway_agent_history(history)
    assert agent_history[0]["content"] == "hello"

    # Enabled: user content gets exactly one timestamp prefix.
    agent_history, _, _ = _build_gateway_agent_history(history, inject_timestamps=True)
    assert agent_history[0]["content"].startswith("[")
    assert agent_history[0]["content"].endswith("hello")
    # Assistant message is never timestamped.
    assert agent_history[1]["content"] == "hi"


def test_split_leading_timestamp_prefix_human_shape():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    content = "[Tue 2026-04-28 13:40:53 CEST]\n\n<memory-context>observed fact</memory-context>"
    prefix, remainder = split_leading_timestamp_prefix(content)
    assert prefix == "[Tue 2026-04-28 13:40:53 CEST]"
    assert remainder == "<memory-context>observed fact</memory-context>"


def test_split_leading_timestamp_prefix_iso_shape():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    prefix, remainder = split_leading_timestamp_prefix("[2026-04-13T17:02:06+0200] body text")
    assert prefix == "[2026-04-13T17:02:06+0200]"
    assert remainder == "body text"


def test_split_leading_timestamp_prefix_no_prefix_passthrough():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    content = "plain text without a timestamp"
    assert split_leading_timestamp_prefix(content) == (None, content)


def test_split_leading_timestamp_prefix_mid_body_timestamp_untouched():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    content = "body [Tue 2026-04-28 13:40:53 CEST] more"
    assert split_leading_timestamp_prefix(content) == (None, content)


def test_split_leading_timestamp_prefix_pure_timestamp_empty_remainder():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    prefix, remainder = split_leading_timestamp_prefix("[Tue 2026-04-28 13:40:53 CEST]")
    assert prefix == "[Tue 2026-04-28 13:40:53 CEST]"
    assert remainder == ""


def test_split_leading_timestamp_prefix_leading_whitespace_still_peels():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    prefix, remainder = split_leading_timestamp_prefix("  [Tue 2026-04-28 13:40:53 CEST] tail")
    assert prefix == "[Tue 2026-04-28 13:40:53 CEST]"
    assert remainder == "tail"


def test_split_leading_timestamp_prefix_stacked_prefixes_peel_first_only():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    prefix, remainder = split_leading_timestamp_prefix(
        "[Tue 2026-04-28 13:40:53 CEST] [Wed 2026-04-29 14:41:54 CEST] body"
    )
    assert prefix == "[Tue 2026-04-28 13:40:53 CEST]"
    assert remainder == "[Wed 2026-04-29 14:41:54 CEST] body"


def test_split_leading_timestamp_prefix_impossible_date_does_not_peel():
    from gateway.message_timestamps import split_leading_timestamp_prefix

    content = "[Tue 2026-02-30 13:40:53 CEST] body"
    assert split_leading_timestamp_prefix(content) == (None, content)
