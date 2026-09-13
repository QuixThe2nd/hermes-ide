"""Tests for gateway/shutdown_flush.py — pending message durability (#72680)."""

import json
import logging
import os
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.shutdown_flush import (
    TRANSCRIPT_CAP_DROP_REASON,
    _serialise_value,
    flush_overflow_to_file,
    flush_pending_to_file,
    recover_pending_to_db,
)
from hermes_state import SessionDB


def _make_flush_dir(tmp_path: Path) -> Path:
    """Create a temp flush dir and monkeypatch _get_flush_dir to use it."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def test_flush_writes_string_pending_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    pending = {"agent:main:telegram:supergroup:123": "hello world"}
    count = flush_pending_to_file(pending, reason="shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_key"] == "agent:main:telegram:supergroup:123"
    assert payload["reason"] == "shutdown"
    assert payload["data"]["text"] == "hello world"
    assert ":" not in files[0].name
    assert "telegram" not in files[0].name


def test_flush_writes_message_event_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = MagicMock()
    event.text = "user message"
    event.session_id = "20260728_120000_abc"
    event.platform = "telegram"
    event.sender_id = "456"
    event.sender_name = "Alice"
    event.reply_to = None
    event.media = None
    event.raw_event = None

    count = flush_pending_to_file({"session_key_1": event}, reason="adapter_shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "user message"
    assert payload["data"]["session_id"] == "20260728_120000_abc"


def test_recover_inserts_via_append_message_and_deletes_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    ts = int(time.time())
    # Write a flush file with session_id
    payload = {
        "session_key": "agent:main:telegram:supergroup:123",
        "reason": "shutdown",
        "ts": ts,
        "data": {
            "text": "lost message",
            "session_id": "20260728_120000_abc",
        },
    }
    flush_file = flush_dir / "test_session_123.json"
    flush_file.write_text(json.dumps(payload), encoding="utf-8")

    mock_db = MagicMock()
    count = recover_pending_to_db(mock_db)

    assert count == 1
    mock_db.append_message.assert_called_once_with(
        session_id="20260728_120000_abc",
        role="user",
        content="lost message",
        timestamp=ts,
    )
    assert not flush_file.exists()


def test_recover_closes_owned_db_when_unexpected_exception_escapes(
    tmp_path, monkeypatch
):
    """Owned SessionDB must close even when recovery is interrupted."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    (flush_dir / "pending.json").write_text(
        json.dumps(
            {
                "session_key": "agent:main:telegram:123",
                "data": {"text": "message", "session_id": "sid"},
            }
        ),
        encoding="utf-8",
    )

    class InterruptingDB:
        released = False

        def append_message(self, **_kwargs):
            raise KeyboardInterrupt

    db = InterruptingDB()
    monkeypatch.setattr("hermes_state_registry.acquire", lambda: db)
    monkeypatch.setattr(
        "hermes_state_registry.release_or_close", lambda _: setattr(db, "released", True)
    )

    with pytest.raises(KeyboardInterrupt):
        recover_pending_to_db()

    assert db.released is True


def test_serialise_object_with_text():
    obj = MagicMock()
    obj.text = "msg"
    obj.session_id = "sid"
    obj.platform = None
    obj.sender_id = None
    obj.sender_name = None
    obj.reply_to = None
    obj.media = None
    obj.raw_event = None
    result = _serialise_value(obj)
    assert result is not None
    assert result["text"] == "msg"
    assert result["session_id"] == "sid"


def test_get_flush_dir_uses_get_hermes_home(tmp_path, monkeypatch):
    """Flush dir must use get_hermes_home(), not hardcoded Path.home()."""
    import gateway.shutdown_flush as mod

    captured = {}

    def fake_get_hermes_home():
        from pathlib import Path
        captured["called"] = True
        return tmp_path

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", fake_get_hermes_home
    )
    result = mod._get_flush_dir()
    assert captured.get("called") is True
    assert result == tmp_path / "pending_messages"




# ── FIFO overflow tail durability (#99882) ─────────────────────────────


def _overflow_event(text: str, session_id: str = "20260901_120000_fifo"):
    event = MagicMock()
    event.text = text
    event.session_id = session_id
    event.platform = "telegram"
    event.sender_id = "1572286605"
    event.sender_name = "tester"
    event.reply_to = None
    event.media = None
    event.raw_event = None
    return event


def test_flush_overflow_writes_one_payload_per_event_in_arrival_order(tmp_path, monkeypatch):
    """The FIFO tail (queued_events) must survive shutdown like the slot does.

    Each overflow entry is its own recover_pending_to_db-compatible payload,
    with ``seq`` recording arrival order inside the session.
    """
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)

    count = flush_overflow_to_file(
        {
            "agent:main:telegram:dm:1": [
                _overflow_event("follow-up B"),
                _overflow_event("follow-up C"),
            ],
            "agent:main:telegram:dm:2": [],
            "": [_overflow_event("keyless — skipped")],
        },
        reason="shutdown",
    )
    assert count == 2
    payloads = sorted(
        (json.loads(f.read_text(encoding="utf-8")) for f in flush_dir.glob("*.json")),
        key=lambda p: p["seq"],
    )
    assert [p["data"]["text"] for p in payloads] == ["follow-up B", "follow-up C"]
    assert {p["session_key"] for p in payloads} == {"agent:main:telegram:dm:1"}
    assert all(p["reason"] == "shutdown" for p in payloads)


def test_flushed_overflow_is_replayed_by_recover_pending_to_db(tmp_path, monkeypatch):
    """Round-trip: overflow payloads use the slot-flush shape, so the existing
    startup recovery inserts them as user rows without any new reader."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    flush_overflow_to_file({"agent:main:telegram:dm:1": [_overflow_event("orphan-1")]})

    db = MagicMock()
    recovered = recover_pending_to_db(session_db=db)
    assert recovered == 1
    db.append_message.assert_called_once()
    kwargs = db.append_message.call_args.kwargs
    assert kwargs["session_id"] == "20260901_120000_fifo"
    assert kwargs["role"] == "user"
    assert kwargs["content"] == "orphan-1"
    assert list(flush_dir.glob("*.json")) == []


def test_flush_overflow_noop_on_empty():
    assert flush_overflow_to_file({}) == 0
    assert flush_overflow_to_file({"k": []}) == 0


# ── session_key → session_id resolution for plain-string slots ───────────


def _write_flush_file(flush_dir: Path, payload: dict, name: str = "pending-test.json") -> Path:
    path = flush_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _message_rows(db, session_id: str):
    return [tuple(r) for r in db._conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ?", (session_id,)
    ).fetchall()]


def test_recover_resolves_string_slot_via_session_key(tmp_path, monkeypatch):
    """Plain-string slots carry no session_id; recovery resolves the newest session
    row for the routing key instead of dropping the message."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("20260910_100000_aaa", "telegram",
                      session_key="agent:main:discord:thread:111:222")
    flush_file = _write_flush_file(flush_dir, {
        "session_key": "agent:main:discord:thread:111:222",
        "reason": "shutdown", "ts": int(time.time()),
        "data": {"text": "plain string slot"},
    })

    count = recover_pending_to_db(db)

    assert count == 1
    assert not flush_file.exists()
    assert _message_rows(db, "20260910_100000_aaa") == [("user", "plain string slot")]


def test_recover_keeps_file_when_session_key_has_no_session(tmp_path, monkeypatch, caplog):
    """No session row for the key: warn (resolution attempted, none found) and keep the file."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    db = SessionDB(tmp_path / "state.db")
    flush_file = _write_flush_file(flush_dir, {
        "session_key": "agent:main:telegram:dm:nobody",
        "reason": "shutdown", "ts": int(time.time()),
        "data": {"text": "orphaned"},
    })

    with caplog.at_level(logging.WARNING, logger="gateway.shutdown_flush"):
        count = recover_pending_to_db(db)

    assert count == 0
    assert flush_file.exists()
    assert "found no session for the key" in caplog.text


def test_recover_prefers_serialised_session_id_over_key_resolution(tmp_path, monkeypatch):
    """MessageEvent-shaped payloads (session_id present in data) are unchanged — the
    serialised session_id stays authoritative even when the key resolves elsewhere."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("key-resolved-sid", "telegram",
                      session_key="agent:main:telegram:supergroup:123")
    db.create_session("data-sid", "telegram")
    flush_file = _write_flush_file(flush_dir, {
        "session_key": "agent:main:telegram:supergroup:123",
        "reason": "shutdown", "ts": int(time.time()),
        "data": {"text": "event message", "session_id": "data-sid"},
    })

    count = recover_pending_to_db(db)

    assert count == 1
    assert not flush_file.exists()
    assert _message_rows(db, "data-sid") == [("user", "event message")]
    assert _message_rows(db, "key-resolved-sid") == []


def test_recover_transcript_cap_drop_payload_unchanged(tmp_path, monkeypatch):
    """Transcript cap-drop payloads still replay the spooled message dict directly."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("cap-sid", "telegram")
    ts = int(time.time())
    flush_file = _write_flush_file(flush_dir, {
        "session_key": "cap-sid",
        "reason": TRANSCRIPT_CAP_DROP_REASON, "ts": ts, "seq": 0,
        "data": {"session_id": "cap-sid",
                 "message": {"role": "assistant", "content": "evicted turn",
                             "timestamp": ts}},
    })

    count = recover_pending_to_db(db)

    assert count == 1
    assert not flush_file.exists()
    assert _message_rows(db, "cap-sid") == [("assistant", "evicted turn")]


def test_recover_uses_session_key_directly_when_it_is_a_session_id(tmp_path, monkeypatch):
    """Guard: a session_key that already IS a raw session id is used directly, so
    transcript-spool-shaped payloads without data.session_id are not rerouted."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("raw-sid", "telegram")
    db.create_session("other-sid", "telegram", session_key="raw-sid")
    _write_flush_file(flush_dir, {
        "session_key": "raw-sid", "reason": "shutdown", "ts": int(time.time()),
        "data": {"text": "spooled"},
    })

    count = recover_pending_to_db(db)

    assert count == 1
    assert _message_rows(db, "raw-sid") == [("user", "spooled")]
    assert _message_rows(db, "other-sid") == []


def test_recover_resolves_newest_session_for_key(tmp_path, monkeypatch):
    """Multiple session rows for one key: the newest started_at wins (same rule as
    SessionDB.list_gateway_sessions)."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    db = SessionDB(tmp_path / "state.db")
    key = "agent:main:telegram:dm:5"
    db.create_session("old-sid", "telegram", session_key=key)
    db.create_session("new-sid", "telegram", session_key=key)
    with db._lock:
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (1000, "old-sid"))
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (2000, "new-sid"))
        db._conn.commit()
    _write_flush_file(flush_dir, {
        "session_key": key, "reason": "shutdown", "ts": int(time.time()),
        "data": {"text": "goes to newest"},
    })

    count = recover_pending_to_db(db)

    assert count == 1
    assert _message_rows(db, "new-sid") == [("user", "goes to newest")]
    assert _message_rows(db, "old-sid") == []
