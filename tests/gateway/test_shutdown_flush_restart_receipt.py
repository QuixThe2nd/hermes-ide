"""Shutdown pending-flush routes carry the drain-restart receipt (#drain-receipt-flush).

A message durably flushed to ``pending_messages`` by ``flush_pending_to_file`` /
``flush_overflow_to_file`` while the gateway drained for a REQUESTED restart is
replayed by the successor process via ``recover_pending_to_db`` — the stored user
row is the only replay vehicle, so the receipt is proved and baked into the stored
content once, at recovery, using the drain queue's own proof/renderer
(``run_drain_queue.drain_restart_receipt_for_record`` /
``render_drain_restart_receipt``). Everything the proof cannot establish yields no
note and never blocks replay.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from gateway.shutdown_flush import (
    flush_overflow_to_file,
    flush_pending_to_file,
    recover_pending_to_db,
)

DEAD_PID = 999_999_007  # probed as dead via the monkeypatched tri-state helper


def _make_flush_dir(tmp_path: Path) -> Path:
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def _event(text: str):
    event = MagicMock()
    event.text = text
    event.session_id = "20261025_120000_flush"
    event.platform = "telegram"
    event.sender_id = "1"
    event.sender_name = "tester"
    event.reply_to = None
    event.media = None
    event.raw_event = None
    return event


def _probe_dead(monkeypatch, result):
    """Pin the non-signalling tri-state liveness probe to *result* (False=dead)."""
    monkeypatch.setattr("hermes_cli.process_identity._pid_alive_matches",
                        lambda pid, name: result)


def test_overflow_flush_during_restart_stamped_and_noted_on_recovery(
    tmp_path, monkeypatch
):
    """(1) An overflow-flushed message during a requested restart gets the note
    on recovery when the flushing pid is dead."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)

    # Flush for real, then rewrite the stamp pid to the (dead) foreign pid the
    # probe confirms — the successor-process recovery scenario.
    assert flush_overflow_to_file(
        {"agent:main:telegram:dm:1": [_event("orphan during restart")]},
        reason="shutdown",
        restart_requested=True,
    ) == 1
    payload_path = next(flush_dir.glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["restart_drain"]["requested"] is True
    assert isinstance(payload["restart_drain"]["pid"], int)
    payload["restart_drain"]["pid"] = DEAD_PID
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    _probe_dead(monkeypatch, False)
    db = MagicMock()
    assert recover_pending_to_db(db) == 1
    content = db.append_message.call_args.kwargs["content"]
    assert content.startswith("[Gateway restart completed")
    assert f"pid {DEAD_PID}" in content
    assert "orphan during restart" in content
    assert not list(flush_dir.glob("*.json"))


def test_slot_flush_during_restart_dead_pid_noted(tmp_path, monkeypatch):
    """Same as (1) for the queue-head slot flush route."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    assert flush_pending_to_file(
        {"agent:main:telegram:dm:1": _event("head during restart")},
        reason="shutdown",
        restart_requested=True,
    ) == 1
    payload_path = next(flush_dir.glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["restart_drain"]["pid"] = DEAD_PID
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    _probe_dead(monkeypatch, False)
    db = MagicMock()
    assert recover_pending_to_db(db) == 1
    content = db.append_message.call_args.kwargs["content"]
    assert content.startswith("[Gateway restart completed")
    assert "head during restart" in content


def test_plain_shutdown_flush_has_no_stamp_and_no_note(tmp_path, monkeypatch):
    """(2) A flush during plain shutdown (no restart) yields no stamp, no note."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    assert flush_overflow_to_file(
        {"agent:main:telegram:dm:1": [_event("plain shutdown")]}
    ) == 1
    payload = json.loads(next(flush_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert "restart_drain" not in payload

    _probe_dead(monkeypatch, False)  # even a dead-probe backend proves nothing
    db = MagicMock()
    assert recover_pending_to_db(db) == 1
    assert db.append_message.call_args.kwargs["content"] == "plain shutdown"


def test_same_pid_stamp_yields_no_note(tmp_path, monkeypatch):
    """(3) A stamp naming THIS process (cancelled restart, live reconnect) proves
    nothing — no note, replay unchanged."""
    import os

    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    payload = {
        "session_key": "agent:main:telegram:dm:1",
        "reason": "shutdown",
        "ts": 1700000000,
        "restart_drain": {"requested": True, "pid": os.getpid()},
        "data": {"text": "same process", "session_id": "sid-1"},
    }
    (flush_dir / "pending-same.json").write_text(json.dumps(payload), encoding="utf-8")

    _probe_dead(monkeypatch, False)
    db = MagicMock()
    assert recover_pending_to_db(db) == 1
    assert db.append_message.call_args.kwargs["content"] == "same process"


def test_note_renders_exactly_once_across_recovery_and_replay(tmp_path, monkeypatch):
    """(4) One recovery bakes the note into stored content once; a replay of that
    stored turn sees exactly one note (and the drained file cannot recover twice)."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    assert flush_overflow_to_file(
        {"agent:main:telegram:dm:1": [_event("once only")]},
        restart_requested=True,
    ) == 1
    payload_path = next(flush_dir.glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["restart_drain"]["pid"] = DEAD_PID
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    _probe_dead(monkeypatch, False)
    db = MagicMock()
    assert recover_pending_to_db(db) == 1
    db.append_message.assert_called_once()
    content = db.append_message.call_args.kwargs["content"]
    assert content.count("[Gateway restart completed") == 1
    # The turn-level seam (format_drain_restart_receipt) reads an event attribute
    # the flushed-recovery path never sets, so replaying the stored row adds none.
    from gateway.platforms.event import MessageEvent
    from gateway.run_drain_queue import format_drain_restart_receipt

    replayed = MessageEvent(text=content, source=MagicMock())
    assert format_drain_restart_receipt(replayed) is None
    assert content.count("[Gateway restart completed") == 1
    # The flush file is consumed — no second recovery is possible.
    assert not list(flush_dir.glob("*.json"))


def test_flushed_stamp_negative_classes_yield_no_note(tmp_path, monkeypatch):
    """(5) Malformed, oversized, and unavailable-backend stamps from
    drain_restart_receipt_for_record still apply to flushed payloads."""
    from gateway.run_drain_queue import _MAX_PROBEABLE_PID

    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    cases = {
        "malformed": {"requested": True, "pid": "not-an-int"},
        "oversized": {"requested": True, "pid": _MAX_PROBEABLE_PID + 1},
        "unavailable": {"requested": True, "pid": DEAD_PID},  # probe returns None
        "legacy": None,  # no stamp at all
    }
    for name, stamp in cases.items():
        payload = {
            "session_key": "agent:main:telegram:dm:1",
            "reason": "shutdown",
            "ts": 1700000000,
            "data": {"text": f"msg {name}", "session_id": f"sid-{name}"},
        }
        if stamp is not None:
            payload["restart_drain"] = stamp
        (flush_dir / f"pending-{name}.json").write_text(json.dumps(payload),
                                                        encoding="utf-8")

    # Unavailable liveness backend: the tri-state probe returns None (unknown).
    _probe_dead(monkeypatch, None)
    db = MagicMock()
    assert recover_pending_to_db(db) == len(cases)
    for call in db.append_message.call_args_list:
        assert "[Gateway restart completed" not in call.kwargs["content"]
    assert db.append_message.call_count == len(cases)
