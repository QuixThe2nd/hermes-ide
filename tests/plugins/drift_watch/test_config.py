"""Drift-watch config defaults, overrides, and explicit-disable gates."""

from __future__ import annotations

import pytest
import yaml

from plugins.drift_watch.config import (
    DEFAULT_MAX_CAPTURES,
    DEFAULT_RETAIN_DAYS,
    default_schedule_calendar,
    load_drift_watch_config,
    plugin_explicitly_disabled,
    resolve_state_dir,
)


def test_default_schedule_is_twice_hourly_at_offset_minutes():
    assert default_schedule_calendar() == "*-*-* *:07,37:00"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _no_project_env(monkeypatch):
    monkeypatch.delenv("HERMES_PROJECT", raising=False)


def test_defaults_with_empty_raw_config(hermes_home, monkeypatch):
    _no_project_env(monkeypatch)
    cfg = load_drift_watch_config({})
    assert cfg == {
        "enabled": True,
        "tree": "",
        "state_dir": str(hermes_home / "state" / "drift-watch"),
        "schedule": "*-*-* *:07,37:00",
        "retain_days": DEFAULT_RETAIN_DAYS,
        "max_captures": DEFAULT_MAX_CAPTURES,
    }
    assert (cfg["retain_days"], cfg["max_captures"]) == (90, 50)


def test_tree_defaults_to_hermes_project_env(hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROJECT", "/srv/live-tree")
    cfg = load_drift_watch_config({})
    assert cfg["tree"] == "/srv/live-tree"
    # An explicit config tree wins over the environment.
    assert (
        load_drift_watch_config({"tree": "/other/tree"})["tree"] == "/other/tree"
    )


def test_explicit_overrides_win(hermes_home, monkeypatch):
    _no_project_env(monkeypatch)
    cfg = load_drift_watch_config(
        {
            "enabled": False,
            "tree": "/srv/live-tree",
            "state_dir": "/var/lib/drift",
            "schedule": "*-*-* 03:15:00",
            "retain_days": 7,
            "max_captures": 3,
        }
    )
    assert cfg == {
        "enabled": False,
        "tree": "/srv/live-tree",
        "state_dir": "/var/lib/drift",
        "schedule": "*-*-* 03:15:00",
        "retain_days": 7,
        "max_captures": 3,
    }


def test_coercions_are_lenient(hermes_home, monkeypatch):
    _no_project_env(monkeypatch)
    cfg = load_drift_watch_config(
        {"enabled": "true", "retain_days": "14", "max_captures": "5"}
    )
    assert cfg["enabled"] is True
    assert cfg["retain_days"] == 14
    assert cfg["max_captures"] == 5
    assert load_drift_watch_config({"retain_days": "bogus"})["retain_days"] == 90
    assert load_drift_watch_config({"max_captures": 0})["max_captures"] == 1
    assert load_drift_watch_config({"retain_days": None})["retain_days"] == 90


def test_blank_state_dir_falls_back_to_hermes_home(hermes_home):
    assert resolve_state_dir({}) == str(hermes_home / "state" / "drift-watch")
    assert resolve_state_dir({"state_dir": "  "}) == str(
        hermes_home / "state" / "drift-watch"
    )


def test_non_mapping_section_is_ignored(hermes_home, monkeypatch):
    _no_project_env(monkeypatch)
    assert load_drift_watch_config("nope")["enabled"] is True  # type: ignore[arg-type]


def test_explicit_disable_wins(hermes_home):
    _write_config(
        hermes_home,
        {
            "plugins": {"enabled": [], "disabled": ["drift_watch"]},
            "drift_watch": {"enabled": True},
        },
    )
    assert plugin_explicitly_disabled() is True


def test_config_enabled_false_wins(hermes_home):
    _write_config(hermes_home, {"drift_watch": {"enabled": False}})
    assert plugin_explicitly_disabled() is True


def test_enabled_string_false_counts_as_disabled():
    assert plugin_explicitly_disabled({"drift_watch": {"enabled": "false"}}) is True


def test_enabled_string_true_is_not_explicit_disable():
    assert plugin_explicitly_disabled({"drift_watch": {"enabled": "true"}}) is False


def test_no_config_is_not_disabled():
    assert plugin_explicitly_disabled({}) is False
    assert plugin_explicitly_disabled({"drift_watch": {"tree": "/x"}}) is False


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
