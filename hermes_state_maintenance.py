"""Retention pruning, stale-session archiving and VACUUM policy mixin for SessionDB."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_state_common import (
    AUTO_VACUUM_MIN_FREELIST_RATIO, _id_chunks, _placeholders, _sql_session_last_active, escape_like as _escape_like
)
from hermes_startup_watchdog import report_startup_progress

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

_LAST_ACTIVE_SQL = _sql_session_last_active("s")
_TOKENS_SQL = "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0))"
_COST_SQL = "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0)"


def _like(value: str) -> str:
    return f"%{_escape_like(value.lower())}%"


def _cwd_prefix_filter(value: str) -> Tuple[List[str], list]:
    from hermes_state_sessions import _cwd_prefix_clause
    clause, params = _cwd_prefix_clause(value)
    return [clause], list(params)


def _one(clause: str, conv=None):
    return lambda v: ([clause], [conv(v) if conv else v])


def _seconds_since(now: float, raw) -> Optional[float]:
    """Age of a state_meta timestamp; None when unset or corrupt (= no prior run)."""
    try:
        return now - float(raw) if raw else None
    except (TypeError, ValueError):
        return None


# Prune/archive filters in evaluation order: (kwarg, applies-when, builder -> (clauses, params)).
# ``applies-when``: "notnone" (numeric/time bounds; 0 is a real bound) or "truthy" ("" = unset).
_PRUNE_FILTERS = (
    # Orphan-swept rows age from the sweep, not old activity, or the next prune deletes them before recovery.
    ("last_active_before", "notnone", lambda v: (
        [_LAST_ACTIVE_SQL + " < ?",
         "(COALESCE(s.end_reason, '') != 'startup_orphan_reap' OR s.ended_at < ?)"],
        [v, v])),
    ("last_active_after", "notnone", _one(_LAST_ACTIVE_SQL + " >= ?")),
    ("started_before", "notnone", _one("s.started_at < ?")),
    ("started_after", "notnone", _one("s.started_at >= ?")),
    ("source", "truthy", _one("s.source = ?")),
    ("title_like", "truthy", _one("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'", _like)),
    ("end_reason", "truthy", _one("s.end_reason = ?")),
    ("cwd_prefix", "truthy", _cwd_prefix_filter),
    ("min_messages", "notnone", _one("s.message_count >= ?")),
    ("max_messages", "notnone", _one("s.message_count <= ?")),
    ("model_like", "truthy", _one("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'", _like)),
    ("provider", "truthy", _one("LOWER(COALESCE(s.billing_provider, '')) = ?", str.lower)),
    ("user_id", "truthy", _one("s.user_id = ?")),
    ("chat_id", "truthy", _one("s.chat_id = ?")),
    ("chat_type", "truthy", _one("s.chat_type = ?")),
    ("branch_like", "truthy", _one("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'", _like)),
    ("min_tokens", "notnone", _one(_TOKENS_SQL + " >= ?")),
    ("max_tokens", "notnone", _one(_TOKENS_SQL + " <= ?")),
    ("min_cost", "notnone", _one(_COST_SQL + " >= ?")),
    ("max_cost", "notnone", _one(_COST_SQL + " <= ?")),
    ("min_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) >= ?")),
    ("max_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) <= ?")),
)
_PRUNE_FILTER_NAMES = frozenset(name for name, _, _ in _PRUNE_FILTERS) | {"archived", "include_pinned"}


class SessionMaintenanceMixin:
    """Retention pruning, stale-session archiving and VACUUM policy for SessionDB."""

    def _write_guards_reject(self, conn, sid: str, **kwargs) -> bool:
        """True when a live turn lease / compression lock protects ``sid``; expired or
        dead-holder guards are reclaimed and fenced as a side effect."""
        from hermes_state import SessionCompressionInProgressError
        from hermes_state_errors import SessionTurnLeaseLostError
        try:
            self._check_transcript_write_guards(
                conn, sid, compression_lock_holder=None, turn_lease_holder=None,
                reject_active_turn_lease=True, reject_active_compression_lock=True, **kwargs)
        except (SessionCompressionInProgressError, SessionTurnLeaseLostError):
            return True
        return False

    @staticmethod
    def _prune_filter_where(*, archived: Optional[bool] = None, include_pinned: bool = False,
                            **filters) -> Tuple[str, list]:
        """Shared WHERE clause for bulk prune/archive selection (alias ``s``): ``_PRUNE_FILTERS``
        AND together, only ended sessions are ever candidates, ``archived`` is tri-state
        (None = both), ``*_like`` are case-insensitive substrings, the rest exact."""
        unknown = set(filters) - _PRUNE_FILTER_NAMES
        if unknown:
            raise TypeError("SessionMaintenanceMixin._prune_filter_where() got an unexpected "
                            f"keyword argument {sorted(unknown)[0]!r}")
        clauses = ["s.ended_at IS NOT NULL"]
        params: list = []
        for name, applies, build in _PRUNE_FILTERS:
            value = filters.get(name)
            if (value is not None) if applies == "notnone" else bool(value):
                new_clauses, new_params = build(value)
                clauses.extend(new_clauses)
                params.extend(new_params)
        if isinstance(archived, bool):
            clauses.append(f"s.archived = {int(archived)}")
        # Pinned is a durable "keep" flag: bulk prune/delete/archive exclude pinned rows unless opted in.
        if not include_pinned:
            clauses.append("COALESCE(s.pinned, 0) = 0")
        return " AND ".join(clauses), params

    def _prune_where(self, older_than_days, source, filters) -> Tuple[str, list]:
        """Translate the legacy age window into the shared activity filter, then build WHERE."""
        if (older_than_days is not None and filters.get("last_active_before") is None
                and filters.get("started_before") is None):
            filters["last_active_before"] = time.time() - (older_than_days * 86400)
        return self._prune_filter_where(source=source, **filters)

    def _page_pragmas(self, names: Tuple[str, ...], fail_msg: str) -> Optional[list]:
        """Integer PRAGMAs over the existing connection (never a byte probe); None + debug log on failure."""
        try:
            with self._read_ctx() as conn:
                if self._conn is None:
                    return None
                return [int(conn.execute(f"PRAGMA {name}").fetchone()[0]) for name in names]
        except Exception as exc:
            logger.debug(fail_msg, exc)
            return None

    def _try_checkpoint(self, mode: str, fail_msg: str) -> None:
        try:
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except Exception as exc:
            logger.debug(fail_msg, exc)
