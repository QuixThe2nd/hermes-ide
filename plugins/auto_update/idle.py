"""READ-ONLY SessionDB idle checks for the standalone oneshot runner.

``gateway.scale_to_zero.is_idle`` is intentionally NOT used here: it composes
gateway-process state (running agent count, inbound clock, background work from
the live gateway registry). The auto-update oneshot runs outside the gateway
process, so those inputs are unavailable and would always read as idle.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from hermes_constants import get_hermes_home
from hermes_state import classify_session_status
from hermes_state_common import _sql_session_last_active


@dataclass(frozen=True)
class IdleBlocker:
    code: str
    detail: str


@dataclass(frozen=True)
class IdleSnapshot:
    idle: bool
    blockers: tuple[IdleBlocker, ...]


def state_db_path() -> Path:
    return get_hermes_home() / "state.db"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def _active_session_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM sessions WHERE ended_at IS NULL"
    ).fetchall()
    return [str(row["id"]) for row in rows if row["id"]]


def _has_active_streaming(
    conn: sqlite3.Connection,
    session_ids: Iterable[str],
    *,
    now: float,
    idle_seconds: float,
) -> bool:
    ids = list(session_ids)
    if not ids:
        return False
    placeholders = ",".join("?" for _ in ids)
    cutoff = now - idle_seconds
    row = conn.execute(
        f"""
        SELECT 1
        FROM messages m
        JOIN (
            SELECT session_id, MAX(id) AS max_id
            FROM messages
            WHERE session_id IN ({placeholders})
            GROUP BY session_id
        ) latest ON m.id = latest.max_id
        WHERE m.role = 'assistant'
          AND (m.finish_reason IS NULL OR TRIM(m.finish_reason) = '')
          AND m.timestamp >= ?
        LIMIT 1
        """,
        (*ids, cutoff),
    ).fetchone()
    return row is not None


def _has_unanswered_user_work(
    conn: sqlite3.Connection,
    session_ids: Iterable[str],
    *,
    now: float,
    idle_seconds: float,
) -> bool:
    ids = list(session_ids)
    if not ids:
        return False
    placeholders = ",".join("?" for _ in ids)
    cutoff = now - idle_seconds
    rows = conn.execute(
        f"""
        SELECT m.session_id, m.role,
               m.tool_calls IS NOT NULL AS has_tool_calls,
               m.finish_reason
        FROM messages m
        JOIN (
            SELECT session_id, MAX(id) AS max_id
            FROM messages
            WHERE session_id IN ({placeholders})
            GROUP BY session_id
        ) latest ON m.id = latest.max_id
        WHERE m.timestamp >= ?
        """,
        (*ids, cutoff),
    ).fetchall()
    for row in rows:
        status = classify_session_status(
            role=row["role"],
            has_tool_calls=bool(row["has_tool_calls"]),
            finish_reason=row["finish_reason"],
        )
        if status == "interrupted":
            return True
    return False


def _has_recent_activity(
    conn: sqlite3.Connection, *, idle_seconds: float, now: float | None = None
) -> bool:
    now = time.time() if now is None else now
    cutoff = now - idle_seconds
    row = conn.execute(
        f"""
        SELECT 1
        FROM sessions s
        WHERE s.ended_at IS NULL
          AND {_sql_session_last_active("s")} >= ?
        LIMIT 1
        """,
        (cutoff,),
    ).fetchone()
    return row is not None


def _has_active_compression_lock(conn: sqlite3.Connection, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT 1 FROM compression_locks WHERE expires_at > ? LIMIT 1",
        (now,),
    ).fetchone()
    return row is not None


def _has_live_turn_lease(conn: sqlite3.Connection, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT 1 FROM session_turn_leases WHERE expires_at > ? LIMIT 1",
        (now,),
    ).fetchone()
    return row is not None


def _has_live_delegation(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM async_delegations
        WHERE state IN ('dispatched', 'running', 'finalizing')
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def evaluate_idle(
    *,
    idle_minutes: int,
    db_path: Path | None = None,
    now: float | None = None,
    connect_fn: Callable[[Path], sqlite3.Connection] = _connect_readonly,
) -> IdleSnapshot:
    """Return whether Hermes looks idle enough for an unattended update."""
    path = db_path or state_db_path()
    blockers: list[IdleBlocker] = []
    now = time.time() if now is None else now
    try:
        if not path.is_file():
            return IdleSnapshot(
                idle=False,
                blockers=(IdleBlocker("db_missing", "state.db missing or unreadable"),),
            )
        with connect_fn(path) as conn:
            session_ids = _active_session_ids(conn)
            idle_seconds = idle_minutes * 60.0
            if _has_active_streaming(
                conn, session_ids, now=now, idle_seconds=idle_seconds
            ):
                blockers.append(
                    IdleBlocker("streaming", "assistant response still streaming")
                )
            if _has_unanswered_user_work(
                conn, session_ids, now=now, idle_seconds=idle_seconds
            ):
                blockers.append(
                    IdleBlocker("unanswered", "recent unanswered user work")
                )
            if _has_recent_activity(
                conn, idle_seconds=idle_seconds, now=now
            ):
                blockers.append(
                    IdleBlocker("recent_activity", "message activity within idle window")
                )
            if _has_active_compression_lock(conn, now=now):
                blockers.append(
                    IdleBlocker("compression", "active compression lock")
                )
            if _has_live_turn_lease(conn, now=now):
                blockers.append(
                    IdleBlocker("live_turn", "active session turn lease")
                )
            if _has_live_delegation(conn):
                blockers.append(
                    IdleBlocker("live_delegation", "delegated agent running")
                )
    except (OSError, sqlite3.Error):
        return IdleSnapshot(
            idle=False,
            blockers=(IdleBlocker("db_unreadable", "state.db unreadable"),),
        )

    return IdleSnapshot(idle=not blockers, blockers=tuple(blockers))
