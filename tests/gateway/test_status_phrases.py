import random

from gateway.status_phrases import (
    classify_status_context,
    choose_status_phrase,
    resolve_status_phrase_catalog,
)


def test_long_running_context_uses_status_bucket():
    assert classify_status_context("status") == "status"
    assert classify_status_context("heartbeat") == "status"
    assert classify_status_context("long_running") == "status"


def test_status_phrase_does_not_leak_raw_preview_or_args():
    msg = choose_status_phrase(
        "status",
        preview="actual private scratch text should not be sent",
        args={"secret": "SECRET-123"},
        rng=random.Random(4),
    )

    assert "actual private scratch" not in msg
    assert "SECRET-123" not in msg
    assert msg


def test_status_phrase_path_can_load_relative_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    phrase_dir = tmp_path / "phrase-catalog"
    phrase_dir.mkdir()
    (phrase_dir / "01-status.yaml").write_text("status:\n  - relative dir status text\n", encoding="utf-8")

    catalog = resolve_status_phrase_catalog(
        {"display": {"status_phrases": {"path": "phrase-catalog"}}},
        "whatsapp",
    )

    assert "relative dir status text" in catalog["status"]


def test_choose_status_phrase_uses_custom_catalog_without_leaking_args():
    catalog = resolve_status_phrase_catalog(
        {"display": {"status_phrases": {"mode": "replace", "status": ["custom safe status text"]}}},
        "whatsapp",
    )

    msg = choose_status_phrase(
        "status",
        args={"query": "SECRET SEARCH"},
        catalog=catalog,
    )

    assert msg == "custom safe status text"
    assert "SECRET" not in msg


def test_discord_zero_config_heartbeat_is_phase_line():
    """No config set → Discord resolves phase mode and the heartbeat names
    the current wait (tool / model / packing) with its elapsed time — never
    an iteration counter or provider wait-notice essay."""
    from gateway.display_config import resolve_display_setting
    from gateway.status_phrases import format_phase_heartbeat

    assert (
        resolve_display_setting({}, "discord", "long_running_notifications", True)
        == "phase"
    )
    # Tool in flight: tool function name + elapsed on this tool.
    assert (
        format_phase_heartbeat(
            {"current_tool": "terminal", "seconds_since_activity": 102}
        )
        == "⏳ terminal 1m42s"
    )
    # Next LLM call: model id parsed from the wait notice (essay dropped).
    wait_notice = (
        "⏳ waiting on grok-4.6 — 30s with no response yet (provider may be "
        "slow or overloaded; auto-reconnect at 120s)"
    )
    assert (
        format_phase_heartbeat(
            {
                "current_tool": None,
                "last_activity_desc": wait_notice,
                "seconds_since_activity": 38,
            }
        )
        == "⏳ grok-4.6 38s"
    )
    # Last tool already returned, next LLM wait not started.
    assert (
        format_phase_heartbeat(
            {
                "current_tool": None,
                "last_activity_desc": "tool completed: terminal (1.2s)",
                "seconds_since_activity": 12,
            }
        )
        == "⏳ packing 12s"
    )
    # LLM wait without a parseable model id falls back to "model".
    assert (
        format_phase_heartbeat(
            {
                "current_tool": None,
                "last_activity_desc": "waiting for non-streaming API response",
                "seconds_since_activity": 40,
            }
        )
        == "⏳ model 40s"
    )
    for snapshot in (
        {"current_tool": "terminal", "seconds_since_activity": 102},
        {"current_tool": None, "last_activity_desc": wait_notice, "seconds_since_activity": 38},
        {"current_tool": None, "last_activity_desc": "tool completed: terminal (1.2s)", "seconds_since_activity": 12},
    ):
        line = format_phase_heartbeat(snapshot)
        assert "iteration" not in line
        assert "provider may be slow" not in line
        assert "auto-reconnect" not in line


def test_phase_heartbeat_elapsed_prefers_phase_seconds():
    """phase_seconds is the current wait's own clock; the liveness heartbeat
    refreshes seconds_since_activity every ~30s, so it must lose when both
    are present or long waits display as 0–30s forever."""
    from gateway.status_phrases import format_phase_heartbeat

    assert (
        format_phase_heartbeat(
            {
                "current_tool": "terminal",
                "phase_seconds": 95,
                "seconds_since_activity": 5,
            }
        )
        == "⏳ terminal 1m35s"
    )


def test_phase_heartbeat_elapsed_falls_back_to_liveness_clock():
    """Snapshots without a phase clock (hand-built, durable projection with
    no phase start) keep using seconds_since_activity."""
    from gateway.status_phrases import format_phase_heartbeat

    assert (
        format_phase_heartbeat(
            {
                "current_tool": None,
                "last_activity_desc": "tool completed: terminal (1.2s)",
                "phase_seconds": None,
                "seconds_since_activity": 12,
            }
        )
        == "⏳ packing 12s"
    )


def test_phase_heartbeat_elapsed_missing_or_invalid_is_zero():
    from gateway.status_phrases import format_phase_heartbeat

    assert format_phase_heartbeat({"current_tool": "terminal"}) == "⏳ terminal 0s"
    assert (
        format_phase_heartbeat(
            {"current_tool": "terminal", "phase_seconds": "soon"}
        )
        == "⏳ terminal 0s"
    )
