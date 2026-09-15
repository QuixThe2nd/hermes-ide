"""Auto-update config defaults and schedule resolution."""

from __future__ import annotations

from plugins.auto_update.config import (
    DEFAULT_RANDOMIZED_DELAY_SEC,
    default_schedule_calendar,
    load_auto_update_config,
    plugin_explicitly_disabled,
    resolve_schedule,
)


def test_default_schedule_is_every_30_minutes_with_zero_delay():
    assert default_schedule_calendar() == "*-*-* *:00,30:00"
    cfg = load_auto_update_config({})
    assert cfg["schedule"] == "*-*-* *:00,30:00"
    assert cfg["randomized_delay_sec"] == DEFAULT_RANDOMIZED_DELAY_SEC
    assert cfg["randomized_delay_sec"] == 0
    assert cfg["accuracy_sec"] == "1s"
    assert cfg["idle_minutes"] == 8


def test_explicit_hour_window_overrides_default():
    schedule = resolve_schedule({"schedule_start_hour": 4, "schedule_end_hour": 8})
    assert schedule == "*-*-* 04,05,06,07:00:00"


def test_explicit_schedule_calendar_wins():
    custom = "*-*-* 02:15:00"
    schedule = resolve_schedule({"schedule": custom})
    assert schedule == custom


def test_explicit_randomized_delay_overrides_default():
    cfg = load_auto_update_config({"randomized_delay_sec": 900})
    assert cfg["randomized_delay_sec"] == 900


def test_explicit_schedule_wins_over_legacy_hour_keys():
    custom = "*-*-* 02:15:00"
    schedule = resolve_schedule(
        {
            "schedule": custom,
            "schedule_start_hour": 4,
            "schedule_end_hour": 8,
        }
    )
    assert schedule == custom


def test_enabled_string_false_counts_as_disabled():
    assert plugin_explicitly_disabled({"auto_update": {"enabled": "false"}}) is True


def test_enabled_string_true_is_not_explicit_disable():
    assert plugin_explicitly_disabled({"auto_update": {"enabled": "true"}}) is False
