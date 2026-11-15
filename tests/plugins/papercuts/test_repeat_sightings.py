"""Repeat papercut sightings stay in the journal without spamming Discord."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def threaded_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "papercuts:\n  threads:\n    enabled: true\n",
        encoding="utf-8",
    )
    from plugins.papercuts.lifecycle import threads_enabled

    assert threads_enabled() is True


def _log(observed: str) -> dict:
    from plugins.papercuts import handle_papercuts

    return json.loads(
        handle_papercuts(
            {
                "action": "log",
                "summary": "The same workflow friction",
                "observed": observed,
                "suggested_fix": "Remove the repeated detour",
                "component": "test",
            }
        )
    )


def _create_threaded_item() -> str:
    from plugins.papercuts.lifecycle import _append_stage_event

    item_id = _log("initial sighting")["item"]["id"]
    _append_stage_event(item_id, "triage", thread_id="thread-1")
    return item_id


def test_repeat_sighting_is_journaled_without_thread_post(threaded_home):
    item_id = _create_threaded_item()

    with (
        patch("plugins.papercuts.threads.post", return_value=True) as post,
        patch("plugins.papercuts.threads.reopen", return_value=True) as reopen,
    ):
        repeated = _log("the same thing happened again")

    post.assert_not_called()
    reopen.assert_not_called()
    assert repeated["success"] is True
    assert repeated["deduplicated"] is True
    assert repeated["reopened"] is False
    assert repeated["item"]["id"] == item_id
    assert repeated["item"]["occurrences"] == 2


def test_reopen_notification_omits_repeat_sighting(threaded_home):
    from plugins.papercuts import handle_papercuts

    item_id = _create_threaded_item()
    with patch("plugins.papercuts.threads.archive", return_value=True):
        closed = json.loads(handle_papercuts({"action": "resolve", "id": item_id}))
    assert closed["changed"] is True

    with (
        patch("plugins.papercuts.threads.post", return_value=True) as post,
        patch("plugins.papercuts.threads.reopen", return_value=True) as reopen,
    ):
        repeated = _log("the friction returned")

    reopen.assert_called_once_with("thread-1")
    post.assert_called_once_with("thread-1", "reopened — the friction came back")
    assert repeated["success"] is True
    assert repeated["deduplicated"] is True
    assert repeated["reopened"] is True
    assert repeated["item"]["occurrences"] == 2
    assert repeated["item"]["status"] == "open"
    assert repeated["item"]["stage"] == "triage"
