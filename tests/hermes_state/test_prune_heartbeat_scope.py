"""Prune scope must honor a recent heartbeat when the last message is old.

``prune_sessions`` / ``list_prune_candidates`` compute recency with the single
heartbeat-aware expression (freshest of ``last_activity_at`` and the latest
message timestamp, else ``started_at``). A facade-local copy of the filter that
read only the latest message timestamp once deleted ended sessions whose
heartbeat was inside the retention window — data loss for sessions that were
touched without landing a new message row.
"""

import time
from contextlib import closing

import pytest

from hermes_state import SessionDB

DAY = 86400.0


def _build_five_rows(db, now: float) -> None:
    """The five-row scope table, all timestamps injected relative to *now*."""

    def _age_row(sid: str, *, message_ts: float, ended: bool, pinned: bool = False,
                 heartbeat_ts: float | None = None, started_ts: float | None = None) -> None:
        db.create_session(sid, source="cli")
        db.append_message(sid, "user", f"msg {sid}", timestamp=message_ts)
        old_started = started_ts if started_ts is not None else message_ts
        ended_at = message_ts if ended else None
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET started_at = ?, ended_at = ?, pinned = ? WHERE id = ?",
                (old_started, ended_at, int(pinned), sid),
            )
            db._conn.commit()
        if heartbeat_ts is not None:
            db.touch_session_activity(sid, ts=heartbeat_ts)

    _age_row("ended_old_messages_only", message_ts=now - 100 * DAY, ended=True)
    _age_row("ended_old_but_heartbeat_2d", message_ts=now - 100 * DAY, ended=True,
             heartbeat_ts=now - 2 * DAY)
    _age_row("ended_1h_ago_messages", message_ts=now - 3600.0, ended=True)
    _age_row("open_old_heartbeat_2d", message_ts=now - 100 * DAY, ended=False,
             heartbeat_ts=now - 2 * DAY)
    _age_row("ended_old_pinned_heartbeat_2d", message_ts=now - 100 * DAY, ended=True,
             pinned=True, heartbeat_ts=now - 2 * DAY)


def test_prune_heartbeat_scope_deletes_only_old_message_only_rows(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _build_five_rows(db, now)

        # The dry-run listing agrees with the delete: only the row that is old
        # by EVERY recency signal is a candidate.
        candidates = db.list_prune_candidates(older_than_days=30)
        assert [c["id"] for c in candidates] == ["ended_old_messages_only"]

        # list_prune_candidates reports heartbeat recency, not message-only.
        by_id = {c["id"]: c for c in db.list_prune_candidates(older_than_days=None)}
        assert by_id["ended_old_but_heartbeat_2d"]["last_active"] == pytest.approx(
            now - 2 * DAY, abs=1.0
        )

        removed = db.prune_sessions(older_than_days=30)
        assert removed == 1
        assert db.get_session("ended_old_messages_only") is None
        assert db.get_session("ended_old_but_heartbeat_2d") is not None
        assert db.get_session("ended_1h_ago_messages") is not None
        assert db.get_session("open_old_heartbeat_2d") is not None
        assert db.get_session("ended_old_pinned_heartbeat_2d") is not None
