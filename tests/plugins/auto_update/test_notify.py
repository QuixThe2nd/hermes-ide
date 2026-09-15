"""Notification helper behavior."""

from __future__ import annotations

import pytest

from plugins.auto_update.notify import emit_notification


def test_notify_failure_is_non_fatal(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    def boom(path, message):
        raise OSError("disk full")

    emit_notification("hello", write_text=boom)
