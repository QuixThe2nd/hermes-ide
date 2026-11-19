"""Repeat papercut sightings stay in the journal with no side effects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def papercuts_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Leftover legacy flag: must have no effect on behaviour.
    (home / "config.yaml").write_text(
        "papercuts:\n  threads:\n    enabled: true\n",
        encoding="utf-8",
    )
    return home


def _log(observed: str, *, turn: str = "t1") -> dict:
    from plugins.papercuts import handle_papercuts

    return json.loads(
        handle_papercuts(
            {
                "action": "log",
                "summary": "The same workflow friction",
                "observed": observed,
                "suggested_fix": "Remove the repeated detour",
                "component": "test",
            },
            session_id="s1",
            turn_id=turn,
            task_id="s1",
        )
    )


def test_repeat_sighting_is_journaled_as_occurrence(papercuts_home):
    first = _log("initial sighting", turn="t1")
    item_id = first["item"]["id"]

    repeated = _log("the same thing happened again", turn="t2")

    assert repeated["success"] is True
    assert repeated["deduplicated"] is True
    assert repeated["reopened"] is False
    assert repeated["item"]["id"] == item_id
    assert repeated["item"]["occurrences"] == 2
    assert "warning" not in repeated


def test_closed_papercut_reopens_on_new_sighting(papercuts_home):
    from plugins.papercuts import handle_papercuts

    item_id = _log("initial sighting", turn="t1")["item"]["id"]
    closed = json.loads(
        handle_papercuts({"action": "resolve", "id": item_id}, session_id="s1", turn_id="t2")
    )
    assert closed["changed"] is True
    assert "warning" not in closed

    repeated = _log("the friction returned", turn="t3")

    assert repeated["success"] is True
    assert repeated["deduplicated"] is True
    assert repeated["reopened"] is True
    assert repeated["item"]["occurrences"] == 2
    assert repeated["item"]["status"] == "open"
    assert "warning" not in repeated
