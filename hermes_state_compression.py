"""Compression lineage, cooldown/streak counters, locks and turn leases for SessionDB.

Mixin bound onto ``SessionDB`` via the MRO, built on its ``_read_ctx`` /
``_execute_write`` / ``_write_sql`` / ``_read_one`` primitives."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from hermes_state_common import (
    _BOUNDARY_END_REASONS, _COMPRESSION_LOCK_ROW_SQL as _LOCK_ROW_SQL, _ENDED_ROW_SQL, _ended_by_compression,
    _RESET_CHILD_SQL, _sql_json_extract, _sql_session_last_active, is_automatic_end_reason)

# Log-record parity with the origin module (caplog tests pin "hermes_state").
logger = logging.getLogger("hermes_state")

_COOLDOWN_ROW_SQL = (
    "SELECT compression_failure_cooldown_until, compression_failure_error FROM sessions WHERE id = ?"
)

# One forward step of get_compression_chain: the preferred continuation child of ``?``. A reset
# fork is a separate user-visible conversation (_LISTABLE_CHILD_SQL already surfaces it as its
# own row), so following it here would hijack the lineage tip projection onto the reset sibling
# and make the real continuation invisible (#114271).
_CHAIN_STEP_SQL = f"""
                    SELECT child.id
                    FROM sessions parent
                    JOIN sessions child ON child.parent_session_id = parent.id
                    WHERE parent.id = ?
                      AND parent.end_reason = 'compression'
                      AND {_sql_json_extract('child.model_config', '$._branched_from')} IS NULL
                      AND {_sql_json_extract('child.model_config', '$._delegate_from')} IS NULL
                      AND NOT ({_RESET_CHILD_SQL.format(a='child')})
                      AND COALESCE(child.source, '') != 'tool'
                    ORDER BY
                      CASE
                        WHEN child.end_reason = 'compression' THEN 0
                        WHEN child.ended_at IS NULL THEN 1
                        ELSE 2
                      END,
                      {_sql_session_last_active("child")} DESC,
                      child.started_at DESC,
                      child.id DESC
                    LIMIT 1
                    """


def _cooldown_row(exists: bool, cooldown_until, error) -> Dict[str, Any]:
    return {"session_exists": exists,
            "cooldown_until": float(cooldown_until) if cooldown_until is not None else None, "error": error}


def _claim_lease_row(conn, table: str, key_col: str, key: str, holder: str, now: float, expires_at: float,
                     stale) -> Tuple[bool, Optional[str]]:
    """Single-transaction lease claim: DELETE a stale holder's row (``stale(holder,
    expires_at)``), INSERT OR IGNORE ours, then SELECT to confirm ownership (INSERT OR
    IGNORE gives no rowcount signal). Returns ``(acquired, reclaimed_holder)``."""
    reclaimed_holder = None
    row = conn.execute(f"SELECT holder, expires_at FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    if row is not None and stale(row["holder"], row["expires_at"]):
        conn.execute(f"DELETE FROM {table} WHERE {key_col} = ? AND holder = ?", (key, row["holder"]))
        reclaimed_holder = row["holder"]
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({key_col}, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (key, holder, now, expires_at))
    owner = conn.execute(f"SELECT holder FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    return owner is not None and owner["holder"] == holder, reclaimed_holder


class SessionCompressionMixin:
    """Compression lineage, cooldown/streak counters, locks and turn leases."""

    def reopen_if_explicitly_closed(
        self, session_id: str, *, provenance: str, patience_s: Optional[float] = None,
    ) -> Optional[str]:
        """Clear an explicit-close stamp (``tui_close``, ``cli_close``, ``webhook_complete``, ...) from a
        session a HOST has just proven is still routed to it, returning the reason cleared or None (#106459).
        Narrow twin of ``reopen_session()``: automatic stamps are left to publish (#88197); ``compression``,
        boundary (reset reasons, CLI ``new_session``) and stamps with a published continuation own lineage
        elsewhere and are never touched. The UPDATE is conditional on the exact stamp read, so a close
        landing between read and write survives.

        Only the routing host can make this call. Publication cannot: ``end_session()`` is first-stamp-wins,
        so a close made during a turn that began on a stale stamp is a no-op write. Turn-lease admission
        cannot: the TUI starts its worker before it reaches ``run_conversation()``, so ``session.close`` can
        stamp ``tui_close`` in between and a late lease would clear a deliberate close. Call it under the
        lock that makes the host's registry claim atomic with its teardown (#54878 on the routing table)."""
        if not session_id:
            return None

        def _do(conn):
            row = conn.execute(_ENDED_ROW_SQL, (session_id,)).fetchone()
            if row is None or row["ended_at"] is None:
                return None
            reason = row["end_reason"]
            if is_automatic_end_reason(reason) or reason == "compression" or reason in _BOUNDARY_END_REASONS:
                return None
            superseded = conn.execute(
                "SELECT 1 FROM sessions WHERE parent_session_id = ?"
                + self._NON_CONTINUATION_CHILD_FILTER_SQL.format(alias="") + " LIMIT 1",
                (session_id,) * 4).fetchone()
            if superseded is not None:
                return None
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL "
                "WHERE id = ? AND ended_at = ? AND end_reason = ?",
                (session_id, row["ended_at"], reason))
            return str(reason)
        reason = self._execute_write(_do, patience_s=patience_s)
        if reason is not None:
            logger.warning(
                "Session %s carried a stale %r end stamp while %s; cleared so the conversation can "
                "compress and a later close is recorded (#106459)", session_id, reason, provenance)
        return reason

    def _publish_child_session_row(self, conn, parent, *, parent_session_id, child_session_id, source,
                                   model, model_config, system_prompt, cwd, profile_name) -> None:
        """INSERT the compression child's ``sessions`` row copied from *parent*. Same contract as
        _insert_session_row's compression-fork backfill: the child stays on the parent's profile and keeps
        gateway routing/origin columns; no owner on either side -> this store's profile."""
        system_prompt_hash = self._store_system_prompt(conn, system_prompt)
        conn.execute(
            """INSERT INTO sessions (
                   id, source, model, model_config, system_prompt,
                   system_prompt_hash,
                   parent_session_id, cwd, git_branch, git_repo_root,
                   profile_name, user_id, session_key, chat_id, chat_type,
                   thread_id, display_name, origin_json, started_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                child_session_id, source, model, json.dumps(model_config) if model_config else None,
                system_prompt_hash, parent_session_id, cwd or parent["cwd"], parent["git_branch"],
                parent["git_repo_root"],
                profile_name or parent["profile_name"] or self._own_profile_name(),
                parent["user_id"], parent["session_key"], parent["chat_id"], parent["chat_type"],
                parent["thread_id"], parent["display_name"], parent["origin_json"], time.time()),
        )

    def _write_sql_logged(self, op: str, session_id: str, sql: str, params) -> None:
        """``_write_sql`` that logs (never raises) on ``sqlite3.Error``."""
        try:
            self._write_sql(sql, params)
        except sqlite3.Error as exc:
            logger.warning("%s(%s) failed: %s", op, session_id, exc)

    def _read_session_number(self, column: str, session_id: str, cast: type, zero: Any) -> Any:
        """Read one numeric ``sessions`` column clamped at ``zero``; a missing session,
        NULL, or unparsable value also reads as ``zero``."""
        row = self._read_one(f"SELECT {column} FROM sessions WHERE id = ?", (session_id,)) if session_id else None
        try:
            return zero if row is None else max(zero, cast(row[0] or zero))
        except (TypeError, ValueError):
            return zero

    def _write_session_column(self, column: str, session_id: str, value: Any) -> None:
        self._write_sql(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, session_id))
