"""Notification helper behavior."""

from __future__ import annotations

import pytest

from plugins.drift_watch.notify import emit_notification, notifications_log_path


def test_notify_writes_under_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    seen: list[tuple[object, str]] = []

    emit_notification(
        "drift detected", write_text=lambda path, message: seen.append((path, message))
    )
    assert seen == [(notifications_log_path(), "drift detected")]


def test_notify_failure_is_non_fatal(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    def boom(path, message):
        raise OSError("disk full")

    emit_notification("hello", write_text=boom)


def test_notify_skips_empty_messages():
    assert emit_notification("") is None


def test_notify_appends_real_lines(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    emit_notification("first")
    emit_notification("second\n")
    log = (home / "drift-watch" / "notifications.log").read_text(encoding="utf-8")
    assert log == "first\nsecond\n"
