"""Idle gate behavior against real sqlite state.db files."""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB
from plugins.auto_update.idle import evaluate_idle


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "state.db"
    SessionDB(db_path=path)
    return path


def test_missing_db_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    snap = evaluate_idle(idle_minutes=8, db_path=home / "state.db")
    assert snap.idle is False
    assert snap.blockers[0].code == "db_missing"


def test_recent_activity_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-recent", "cli")
    db.append_message(sid, role="user", content="hello")
    db.append_message(sid, role="assistant", content="hi", finish_reason="stop")
    snap = evaluate_idle(idle_minutes=60, db_path=db_path)
    assert snap.idle is False
    assert any(b.code == "recent_activity" for b in snap.blockers)


def test_unanswered_user_work_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-unanswered", "cli")
    db.append_message(sid, role="user", content="still waiting")
    now = time.time()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "unanswered" for b in snap.blockers)


def test_streaming_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-stream", "cli")
    db.append_message(sid, role="user", content="go")
    db.append_message(sid, role="assistant", content="partial", finish_reason=None)
    now = time.time()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "streaming" for b in snap.blockers)


def test_stale_streaming_does_not_block(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-stale-stream", "cli")
    db.append_message(sid, role="user", content="go")
    db.append_message(sid, role="assistant", content="orphaned", finish_reason=None)
    now = time.time()
    stale_ts = now - (8 * 60) - 60
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = ?", (stale_ts, sid)
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
        (stale_ts, stale_ts, sid),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is True
    assert not any(b.code == "streaming" for b in snap.blockers)


def test_stale_unanswered_does_not_block(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-stale-unanswered", "cli")
    db.append_message(sid, role="user", content="old question")
    now = time.time()
    stale_ts = now - (8 * 60) - 60
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = ?", (stale_ts, sid)
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
        (stale_ts, stale_ts, sid),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is True
    assert not any(b.code == "unanswered" for b in snap.blockers)


def test_fresh_streaming_still_blocks_alongside_stale(db_path):
    db = SessionDB(db_path=db_path)
    stale_sid = db.create_session("sess-stale-stream-2", "cli")
    db.append_message(stale_sid, role="user", content="go")
    db.append_message(
        stale_sid, role="assistant", content="orphaned", finish_reason=None
    )
    fresh_sid = db.create_session("sess-fresh-stream", "cli")
    db.append_message(fresh_sid, role="user", content="go")
    db.append_message(
        fresh_sid, role="assistant", content="partial", finish_reason=None
    )
    now = time.time()
    stale_ts = now - (8 * 60) - 60
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = ?",
        (stale_ts, stale_sid),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
        (stale_ts, stale_ts, stale_sid),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "streaming" for b in snap.blockers)


def test_compression_lock_blocks(db_path):
    db = SessionDB(db_path=db_path)
    sid = db.create_session("sess-compress", "cli")
    assert db.try_acquire_compression_lock(sid, holder="pid=123", ttl_seconds=60)
    snap = evaluate_idle(idle_minutes=0, db_path=db_path, now=time.time())
    assert snap.idle is False
    assert any(b.code == "compression" for b in snap.blockers)


def test_idle_when_no_active_sessions(db_path):
    snap = evaluate_idle(idle_minutes=8, db_path=db_path)
    assert snap.idle is True


def test_live_turn_lease_blocks(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO session_turn_leases
            (conversation_id, holder, acquired_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        ("conv-live", "holder-1", now, now + 60.0),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "live_turn" for b in snap.blockers)


def test_expired_turn_lease_does_not_block(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO session_turn_leases
            (conversation_id, holder, acquired_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        ("conv-expired", "holder-1", now - 120.0, now - 1.0),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is True


def test_dispatched_delegation_blocks(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-dispatched", "sess-1", "dispatched", now, now),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "live_delegation" for b in snap.blockers)


def test_running_delegation_blocks(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-running", "sess-1", "running", now, now),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "live_delegation" for b in snap.blockers)


def test_finalizing_delegation_blocks(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-finalizing", "sess-1", "finalizing", now, now),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is False
    assert any(b.code == "live_delegation" for b in snap.blockers)


def test_terminal_delegation_state_does_not_block(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-done", "sess-1", "completed", now, now),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is True


def test_error_delegation_state_does_not_block(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-error", "sess-1", "error", now, now),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is True


def test_unknown_delegation_state_does_not_block(db_path):
    db = SessionDB(db_path=db_path)
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-unknown", "sess-1", "unknown", now, now),
    )
    db._conn.commit()
    snap = evaluate_idle(idle_minutes=8, db_path=db_path, now=now)
    assert snap.idle is True
