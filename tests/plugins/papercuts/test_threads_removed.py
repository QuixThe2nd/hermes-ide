"""Papercuts never touch Discord threads; legacy stage/steer journals still load.

The per-papercut inbox-thread feature (``papercuts.threads.*`` config, the
``hermes papercuts tick`` stage machine, and the ``advance``/``steer`` tool
actions) was removed outright. Papercuts are logged, listed, resolved, ignored,
counted and reopened purely in the journal. These tests pin that:

- no Discord/thread helper is ever called, even when a stale
  ``papercuts.threads.enabled: true`` block is still present in config.yaml
  (the flag must have no effect), and even for journal items that still carry
  legacy thread metadata;
- ``advance``/``steer`` are rejected as unknown actions;
- journals written by the old feature (``stage``/``steer`` events) fold and
  list without error, keeping their open/resolved status.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def papercuts_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The legacy flag stays set on purpose: it must have no effect.
    (home / "config.yaml").write_text(
        "papercuts:\n  threads:\n    enabled: true\n",
        encoding="utf-8",
    )
    return home


@pytest.fixture()
def discord_spy():
    """Record every thread helper call and every direct Discord REST attempt."""
    import plugins.hermes_starts.threads as hs_threads
    import plugins.hermes_starts as hermes_starts

    calls: list[tuple] = []

    def _record(name):
        def _spy(*args, **kwargs):
            calls.append((name, args, kwargs))
            return True

        return _spy

    patches = [
        patch.object(hs_threads, "open_inbox_thread", _record("open_inbox_thread"), create=True),
        patch.object(hs_threads, "post_to_thread", _record("post_to_thread"), create=True),
        patch.object(hs_threads, "update_inbox_thread", _record("update_inbox_thread"), create=True),
        patch.object(hermes_starts, "_post_channel_message", _record("_post_channel_message"), create=True),
        patch.object(hermes_starts, "_discord_request", _record("_discord_request"), create=True),
    ]
    for p in patches:
        p.start()
    yield calls
    for p in patches:
        p.stop()


def _call(args, **kwargs):
    from plugins.papercuts import handle_papercuts

    return json.loads(handle_papercuts(args, **kwargs))


def _log(observed="the detour happened again", *, turn="t1", **overrides):
    payload = {
        "action": "log",
        "summary": "The same workflow friction",
        "observed": observed,
        "suggested_fix": "Remove the repeated detour",
        "component": "test",
    }
    payload.update(overrides)
    return _call(payload, session_id="s1", turn_id=turn, task_id="s1")


def test_lifecycle_makes_no_discord_calls(papercuts_home, discord_spy):
    item_id = _log()["item"]["id"]

    # Simulate a legacy journal item that still carries thread metadata, as
    # written by the removed stage machine.
    events = Path(papercuts_home) / "papercuts" / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "kind": "stage",
                    "id": item_id,
                    "stage": "triage",
                    "thread_id": "thread-1",
                    "attempts": 1,
                    "ts": "2026-01-01T00:00:00+00:00",
                    "ts_epoch": 2.0,
                }
            )
            + "\n"
        )

    resolved = _call({"action": "resolve", "id": item_id, "note": "fixed"}, turn="t2")
    assert resolved["success"] is True
    assert resolved["changed"] is True
    assert "warning" not in resolved

    reopened = _log(turn="t3")
    assert reopened["success"] is True
    assert reopened["reopened"] is True
    assert reopened["item"]["status"] == "open"
    assert reopened["item"]["occurrences"] == 2
    assert "warning" not in reopened

    assert discord_spy == []


def test_advance_and_steer_actions_are_gone(papercuts_home):
    for action in ("advance", "steer"):
        result = _call({"action": action, "id": "pc_x", "message": "hi"})
        assert result["success"] is False
        assert result["error"]["code"] == "invalid_input"


def test_legacy_stage_and_steer_events_load_and_list(papercuts_home):
    events = Path(papercuts_home) / "papercuts" / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)

    def cut(item_id, ts_epoch):
        return {
            "kind": "cut",
            "id": item_id,
            "fingerprint": f"fingerprint-{item_id}",
            "summary": f"friction {item_id}",
            "observed": "observed",
            "workaround": "",
            "suggested_fix": "fix",
            "severity": "minor",
            "category": "tool",
            "component": "c",
            "ts": "2026-01-01T00:00:00+00:00",
            "ts_epoch": ts_epoch,
        }

    lines = [
        cut("pc_open", 1.0),
        cut("pc_threaded", 2.0),
        # A papercut the old stage machine threaded and steered.
        {"kind": "stage", "id": "pc_threaded", "stage": "triage", "thread_id": "th1", "attempts": 1, "ts": "t", "ts_epoch": 2.5},
        {"kind": "steer", "id": "pc_threaded", "message": "check the new parser", "ts": "t", "ts_epoch": 2.6},
        # A papercut closed via the old advance-to-closed path must not
        # resurrect as open.
        cut("pc_closed", 3.0),
        {"kind": "stage", "id": "pc_closed", "stage": "closed", "note": "fixed in abc123", "ts": "t", "ts_epoch": 3.5},
        # A papercut parked in needs_human with a steering note.
        cut("pc_parked", 4.0),
        {"kind": "stage", "id": "pc_parked", "stage": "needs_human", "resumes": "fixing", "ts": "t", "ts_epoch": 4.5},
        {"kind": "steer", "id": "pc_parked", "message": "backend owns it", "ts": "t", "ts_epoch": 4.6},
    ]
    with events.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")

    listed = _call({"action": "list", "status": "all"})
    assert listed["success"] is True
    assert listed["count"] == 4

    by_id = {item["id"]: item for item in listed["items"]}
    assert by_id["pc_open"]["status"] == "open"
    assert by_id["pc_threaded"]["status"] == "open"
    assert by_id["pc_closed"]["status"] == "resolved"
    assert by_id["pc_closed"]["closing_note"] == "fixed in abc123"
    assert by_id["pc_parked"]["status"] == "open"

    resolved = _call({"action": "list", "status": "resolved"})
    assert [item["id"] for item in resolved["items"]] == ["pc_closed"]

    stats = _call({"action": "stats"})
    assert stats["success"] is True
    assert stats["by_status"]["resolved"] == 1
