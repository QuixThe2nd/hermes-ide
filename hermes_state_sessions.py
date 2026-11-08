"""Session lifecycle mixin for SessionDB: row upsert/inheritance, lifecycle
flags (end/reopen/archive/pin/hide/read), model_config patching, listing and
counting, delete cascades, and the auto-archive sweep."""

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from agent.session_activity import (
    ActivityProvenance, bound_activity_description, normalize_activity_provenance,
)
from hermes_startup_watchdog import report_startup_progress
from hermes_state_common import (
    _LISTABLE_CHILD_SQL, _PREVIEW_ELIGIBLE_SQL, _PREVIEW_RAW_SELECT, _RECOVERABLE_END_REASONS,
    _RECOVERABLE_END_REASONS_SQL, _RESET_CHILD_SQL, _RESET_END_REASONS, _legacy_reset_child_sql, _shape_preview,
    _sql_json_extract, _sql_session_last_active, _sql_session_last_active_by_id, escape_like as _escape_like,
    _SQL_IN_CHUNK, _id_chunks, _placeholders as _session_ids_placeholders,
)

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")


def workspace_key(row: Dict[str, Any]) -> Optional[str]:
    """Workspace grouping key: git repo root, else cwd, else None (branch excluded: a checkout must not
    fragment history)."""
    return (row.get("git_repo_root") or "").strip() or (row.get("cwd") or "").strip() or None


def _delegate_from_json(col: str = "model_config") -> str:
    return _sql_json_extract(col, "$._delegate_from")


# _merge_model_config_json's "no such row" result — distinct from the legal None
# ("merged config is empty → store NULL").
_MODEL_CONFIG_ROW_MISSING = object()


def _parse_model_config(raw: Any) -> Dict[str, Any]:
    """Tolerant ``model_config`` decode: JSON text or dict -> dict copy; anything else -> {}."""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _cwd_prefix_clause(cwd_prefix: str) -> Tuple[str, List[str]]:
    prefix = cwd_prefix.rstrip("/\\") or cwd_prefix
    # ``_``/``%`` are LIKE wildcards but ordinary path characters: unescaped, a
    # prefix also matches sibling directories. The ``=`` arm keeps the raw prefix.
    esc = _escape_like(prefix)
    return (
        "(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\' OR s.cwd LIKE ? ESCAPE '\\')",
        [prefix, f"{esc}/%", f"{esc}\\\\%"],
    )


def _workspace_key_clause(key: str) -> Tuple[str, List[str]]:
    """WHERE for ``workspace_key(row) == key``: git_repo_root equals ``key``, or (rows predating
    per-session git metadata) cwd is at/under ``key``."""
    prefix = key.rstrip("/\\") or key
    cwd_clause, cwd_params = _cwd_prefix_clause(prefix)
    return (
        f"(s.git_repo_root = ? OR (COALESCE(s.git_repo_root, '') = '' AND {cwd_clause}))",
        [prefix, *cwd_params],
    )


# First user message of a session, shaped by _shape_preview() in Python.
# The indentation is part of the list_sessions_rich SQL text.
_PREVIEW_COL_SQL = f"""COALESCE(
                        (SELECT {_PREVIEW_RAW_SELECT}
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                           AND {_PREVIEW_ELIGIBLE_SQL}
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw"""


def _where_sql(clauses: List[str], lead: str = "") -> str:
    """``WHERE a AND b`` (with *lead* prefix) or "" when there are no clauses."""
    return f"{lead}WHERE {' AND '.join(clauses)}" if clauses else ""


def _session_filter_where(
    *, exclude_children: bool = False, source: str = None, sources: List[str] = None,
    session_key: str = None, exclude_sources: List[str] = None, cwd_prefix: str = None,
    min_message_count: int = 0, archived_only: bool = False, include_archived: bool = False,
) -> Tuple[List[str], List[Any]]:
    """Shared ``sessions s`` WHERE builder so counts line up with listed rows. ``exclude_children``
    hides sub-agent runs and compression continuations but keeps branch/reset children
    (``_LISTABLE_CHILD_SQL``). Clause order is part of the SQL text contract."""
    where: List[str] = []
    params: List[Any] = []
    if exclude_children:
        where += [_LISTABLE_CHILD_SQL, f"{_delegate_from_json('s.model_config')} IS NULL"]
    # Show roots and user-visible branch/reset sessions, while still hiding sub-agent runs and compression
    # continuations. All four carry parent_session_id, so the shared predicate classifies the edge from
    # stable markers plus legacy-compatible parent metadata. Branch sessions are identified two ways, OR'd
    # for robustness: 1. A stable ``_branched_from`` marker in model_config, written by /branch at creation
    # time. This survives the parent being reopened and re-ended with a different end_reason (e.g.
    # tui_shutdown overwriting 'branched'), which otherwise hides the branch — see issue #20856. 2. The
    # legacy heuristic (parent ended with 'branched' before the child started), covering branch sessions
    # created before the marker existed.
    include_sources = [source] if source else list(sources or [])
    for clause, values in (
        (f"s.source IN ({_session_ids_placeholders(include_sources)})", include_sources),
        ("s.session_key = ?", [session_key] if session_key else []),
        (f"s.source NOT IN ({_session_ids_placeholders(exclude_sources or ())})", exclude_sources or []),
        (_cwd_prefix_clause(cwd_prefix) if cwd_prefix else ("", [])),
        ("s.message_count >= ?", [min_message_count] if min_message_count > 0 else []),
    ):
        if values:
            where.append(clause)
            params.extend(values)
    if archived_only:
        where.append("s.archived = 1")
    elif not include_archived:
        where.append("s.archived = 0")
    return where, params


def _collect_delegate_child_ids(conn, parent_ids: List[str]) -> List[str]:
    """Delegate-subagent ids (``_delegate_from`` marker, walked recursively) to cascade-delete with
    *parent_ids*; untagged children stay orphaned, not deleted."""
    df = _delegate_from_json()
    seeds = {sid for sid in parent_ids if sid}
    # Seed visited with the parents: a marker chain can loop back onto a parent,
    # which would then be collected as its own descendant. Never return parents.
    # A delegation marker chain can loop back onto a parent — a cycle, or a parent that is also another
    # parent's delegate child when several ids are deleted at once — and without this guard that parent
    # would be collected as one of its own descendants and cascade-deleted along with all of its messages.
    # Callers delete the parents separately, so parents must never appear in the returned child set.
    # (#49148)
    found: set[str] = set(seeds)
    frontier = list(seeds)
    while frontier:
        next_frontier: List[str] = []
        for chunk in _id_chunks(frontier, _SQL_IN_CHUNK // 2):  # each id is bound twice below
            ph = _session_ids_placeholders(chunk)
            cursor = conn.execute(
                f"SELECT id FROM sessions WHERE {df} IN ({ph}) "
                f"OR (parent_session_id IN ({ph}) AND {df} IS NOT NULL)", chunk + chunk,
            )
            for row in cursor.fetchall():
                if row["id"] not in found:
                    found.add(row["id"])
                    next_frontier.append(row["id"])
        frontier = next_frontier
    return [sid for sid in found if sid not in seeds]


def _delete_delegate_children(conn, parent_ids: List[str]) -> List[str]:
    ids = _collect_delegate_child_ids(conn, parent_ids)
    for chunk in _id_chunks(ids):
        ph = _session_ids_placeholders(chunk)
        conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", chunk)
        # FK safety: orphan any untagged stragglers pointing at a doomed row.
        conn.execute(f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({ph})", chunk)
        conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", chunk)
    return ids


# Lifecycle statuses surfaced by session pickers; classified from the final
# message row ONLY so it stays O(1) per session.
# Sessions that are not human conversations (historical kanban workers, third-party tool integrations, finite one-shot
# runs): every human picker — TUI/Desktop session lists, ``/sessions`` in the CLI, ``sessions list`` in the
# console — excludes them. A deny-list, so new interactive platforms surface automatically.
INTERNAL_LISTING_SOURCES = ("tool", "oneshot")

SESSION_STATUS_COMPLETE = "complete"
SESSION_STATUS_INTERRUPTED = "interrupted"
SESSION_STATUS_ERROR = "error"
SESSION_STATUS_EMPTY = "empty"

# finish_reason values meaning the turn ended in a provider/agent error.
_ERROR_FINISH_REASONS = frozenset({"error", "agent_error", "content_filter"})


def classify_session_status(role: Optional[str], has_tool_calls: bool, finish_reason: Optional[str]) -> str:
    """Error finish → ``error``; assistant with pending tool_calls or a trailing user/tool row →
    ``interrupted``; otherwise ``complete`` (benign default: pickers must not alarm on unknown shapes)."""
    if (finish_reason or "").strip().lower() in _ERROR_FINISH_REASONS:
        return SESSION_STATUS_ERROR
    r = (role or "").strip().lower()
    if r in {"user", "tool"} or (r == "assistant" and has_tool_calls):
        return SESSION_STATUS_INTERRUPTED
    return SESSION_STATUS_COMPLETE


# Parent→child profile_name inheritance fence: keyless rows inherit freely; two
# ``agent:<ns>:...`` keyed rows must agree on the namespace.
# ``agent:<ns>:...`` gateway keys encode the profile namespace; a keyless row (CLI / subagent lineage)
# carries none and inherits freely. Two keyed rows must agree on ``agent:<ns>:`` — a default child
# (``agent:main:``) forked from a sibling profile's row must not be durably mislabelled as that profile's.
# See #88381.
_SAME_KEY_NAMESPACE_SQL = (
    "p.session_key IS NULL OR sessions.session_key IS NULL"
    " OR substr(p.session_key, 1, instr(substr(p.session_key, 7), ':') + 6)"
    "  = substr(sessions.session_key, 1, instr(substr(sessions.session_key, 7), ':') + 6)"
)


# Upsert tail of _insert_session_row: every routing/metadata column keeps the value an
# earlier writer set (whitespace is part of the SQL text).
_UPSERT_KEEP_EXISTING_SQL = ",\n".join(
    f"                       {col} = COALESCE(sessions.{col}, excluded.{col})" for col in (
        "session_key", "chat_id", "chat_type", "thread_id", "parent_session_id", "cwd", "profile_name",
        "transport_profile", "git_repo_root", "origin_json", "display_name",
    )
)


def _inherit_col_sql(col: str, extra: str = "") -> str:
    """``col = COALESCE(sessions.col, (SELECT p.col FROM parent))`` (whitespace is part of the SQL text)."""
    pad = " " * (30 + len(col))
    return (
        f"{col} = COALESCE(sessions.{col},\n{pad}(SELECT p.{col} FROM sessions p\n"
        f"{pad}  WHERE p.id = sessions.parent_session_id{extra}))"
    )


_INHERIT_SEP = ",\n" + " " * 27
_INHERIT_PARENT_META_SQL = (
    "UPDATE sessions\n                       SET "
    + _INHERIT_SEP.join((
        *(_inherit_col_sql(c) for c in ("cwd", "git_repo_root", "git_branch")),
        _inherit_col_sql("profile_name", "\n" + " " * 46 + f"AND ({_SAME_KEY_NAMESPACE_SQL})"),
    ))
    + "\n                     WHERE id = ? AND parent_session_id IS NOT NULL"
)
_INHERIT_PARENT_ROUTING_SQL = (
    "UPDATE sessions\n                       SET "
    + _INHERIT_SEP.join(_inherit_col_sql(c) for c in (
        "user_id", "session_key", "chat_id", "chat_type", "thread_id", "display_name", "origin_json",
        "transport_profile",
    ))
    + "\n                     WHERE id = ? AND parent_session_id IS NOT NULL\n"
    "                       AND EXISTS (\n"
    "                           SELECT 1 FROM sessions p\n"
    "                           WHERE p.id = sessions.parent_session_id\n"
    "                             AND p.end_reason = 'compression'\n"
    "                       )"
)


class SessionSessionsMixin:
    """Session rows: create/inherit, lifecycle flags, model_config, listing, deletion."""

    @staticmethod
    def _inherit_parent_session_metadata(conn, session_id: str) -> None:
        """NULL-fill a child's cwd/git/profile from its parent (profile_name only within the same
        ``agent:<ns>:`` namespace). Gateway routing columns are inherited ONLY by compression forks
        (a crash before the gateway re-records the peer would strand the child unroutable); delegate
        children must NOT inherit them (peer recovery could repoint traffic into a subagent's session)."""
        conn.execute(_INHERIT_PARENT_META_SQL, (session_id,))
        conn.execute(_INHERIT_PARENT_ROUTING_SQL, (session_id,))

    # Orphaned gateway-session repair: widest plausible gap between a keyed predecessor going
    # quiet and its unkeyed successor (incident was ~60s; 15 min without spanning conversations).
    _ORPHAN_ADOPTION_MAX_GAP_S = 900.0

    # Children that are NOT compression continuations (branches, delegates, reset forks, tool
    # sessions). Markers are bound to the queried parent id: continuations inherit model_config
    # verbatim, so presence-matching misclassified them as delegates. Callers bind the parent id
    # three times for this filter.
    _NON_CONTINUATION_CHILD_FILTER_SQL = (
        f"  AND COALESCE({_sql_json_extract('{alias}model_config', '$._branched_from')}, '') != ?\n"
        f"  AND COALESCE({_sql_json_extract('{alias}model_config', '$._delegate_from')}, '') != ?\n"
        f"  AND COALESCE({_sql_json_extract('{alias}model_config', '$._reset_from')}, '') != ?\n"
        "  AND COALESCE({alias}source, '') != 'tool'\n"
    )

    def _end_and_bump(self, conn, sql: str, params: tuple, session_id: str, reason: str) -> int:
        """Run an end-stamp UPDATE; only a boundary this call actually wrote advances the
        conversation generation (a no-op must not rotate the peer). Returns rowcount."""
        changed = conn.execute(sql, params).rowcount
        if changed:
            self._bump_conversation_generation(conn, session_id, reason)
        return changed

    # Observation-only write: never let it ride the full routine write-patience budget (#76354 review S1).
    # Under contention a heartbeat that waits ~20s would delay the response-critical path it is merely
    # observing; give up after a sub-second budget instead (the next due window retries naturally).

    def _write_model_config_patch(
        self, session_id: str, patch: Dict[str, Any],
        sql: str = "UPDATE sessions SET model_config = ? WHERE id = ?",
        params: Optional[Callable[[Optional[str]], tuple]] = None,
    ) -> None:
        """Merge ``patch`` into model_config then run ``sql`` with ``params(merged)`` in one write
        transaction; no-op when the row doesn't exist. Custom ``sql`` (prompt-nulling) also GCs prompts."""
        def _do(conn):
            merged = self._merge_model_config_json(conn, session_id, patch)
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute(sql, params(merged) if params else (merged, session_id))
            if params is not None:
                self._delete_unreferenced_system_prompts(conn)
        self._execute_write(_do)

    @staticmethod
    def session_yolo_enabled(session_meta: Optional[Dict[str, Any]]) -> bool:
        """Persisted YOLO flag; False on any parse failure (resume must never enable the bypass)."""
        return bool(_parse_model_config((session_meta or {}).get("model_config")).get("yolo_mode"))

    def get_recent_session_model_route(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Most recently used main-loop model route as one coherent per-call tuple
        (``session_model_usage`` keeps model+provider together; ``sessions`` mixes route changes).
        Recency, not lifetime call count: on a long session a route retired weeks ago can hold the
        highest ``api_call_count`` forever, and /status and /usage would keep calling it current.
        ``rowid DESC`` breaks same-timestamp ties toward the route that first appeared later; without
        it SQLite's temp-sort order is unspecified and the retired route can win."""
        self.flush_token_counts()
        row = self._read_one(
            """SELECT model, billing_provider, billing_base_url, billing_mode,
                      api_call_count
                 FROM session_model_usage
                WHERE session_id = ?
                  AND task = ''
                  AND model <> 'unknown'
                  AND billing_provider <> ''
                ORDER BY last_seen DESC, rowid DESC
                LIMIT 1""",
            (session_id,),
        )
        return dict(row) if row else None

    def _set_lineage_column(self, column: str, session_id: str, value: Any) -> bool:
        """Set one ``sessions`` column across a whole compression lineage: Desktop projects roots
        forward to their tip, so updating only the tip would let the root resurrect it on refresh."""
        return self._write_rowcount(
            f"""
            WITH RECURSIVE
              ancestors(id) AS (
                SELECT ?
                UNION
                SELECT parent.id
                FROM ancestors a
                JOIN sessions child ON child.id = a.id
                JOIN sessions parent ON parent.id = child.parent_session_id
                WHERE parent.end_reason = 'compression'
              ),
              descendants(id) AS (
                SELECT ?
                UNION
                SELECT child.id
                FROM descendants d
                JOIN sessions parent ON parent.id = d.id
                JOIN sessions child ON child.parent_session_id = parent.id
                WHERE parent.end_reason = 'compression'
              ),
              lineage(id) AS (
                SELECT id FROM ancestors
                UNION
                SELECT id FROM descendants
              )
            UPDATE sessions
            SET {column} = ?
            WHERE id IN (SELECT id FROM lineage)
            """,
            (session_id, session_id, value),
        ) > 0

    # Accidental end reasons recovery treats as resumable (also interpolated into
    # the recovery/promotion SQL so literals cannot drift).
    RECOVERABLE_END_REASONS = _RECOVERABLE_END_REASONS

    @staticmethod
    def session_unread(session_row: Dict[str, Any]) -> bool:
        """Unread = activity postdates the ``last_read_at`` watermark (NULL = read)."""
        last_read = session_row.get("last_read_at")
        if last_read is None:
            return False
        last_active = session_row.get("last_active") or session_row.get("started_at")
        return float(last_active or 0) > float(last_read)

    # compact_rows excludes only payload-heavy blobs no list consumer renders.
    _SESSION_COMPACT_EXCLUDED = frozenset(
        {"system_prompt", "system_prompt_hash", "git_metadata_generation"}
    )
    _session_compact_cols_sql: Optional[str] = None

    @staticmethod
    def _chain_search_where(where_sql: str, id_needle: str, search_needle: str) -> Tuple[str, List[Any]]:
        """Extend ``where_sql`` with the id_query / search_query filters: a row is admitted when its own
        id or any id in its forward compression chain matches (search also matches titles and a
        punctuation-stripped form so ``an94`` finds ``AN-94``); chain membership bounds the LIKE."""
        params: List[Any] = []
        clauses: List[str] = []
        def like(needle: str) -> str:
            return f"%{_escape_like(needle)}%"
        if id_needle:
            clauses.append(
                "EXISTS (SELECT 1 FROM chain cq        WHERE cq.root_id = s.id"
                "          AND LOWER(cq.cur_id) LIKE ? ESCAPE '\\')"
            )
            params.append(like(id_needle))
        if search_needle:
            compact_needle = re.sub(r"[\W_]+", "", search_needle)
            search_clause = (
                "EXISTS (SELECT 1 FROM chain cq JOIN sessions cs ON cs.id = cq.cur_id"
                " WHERE cq.root_id = s.id AND (LOWER(COALESCE(cs.title, '')) LIKE ? ESCAPE '\\'"
                " OR LOWER(cq.cur_id) LIKE ? ESCAPE '\\'"
            )
            params.extend([like(search_needle)] * 2)
            if compact_needle:
                search_clause += (
                    " OR REPLACE(REPLACE(REPLACE(REPLACE(LOWER(COALESCE(cs.title, '')),"
                    " '-', ''), '_', ''), '.', ''), ' ', '') LIKE ? ESCAPE '\\'"
                )
                params.append(like(compact_needle))
            clauses.append(search_clause + "))")
        if not clauses:
            return where_sql, params
        combined = " AND ".join(clauses)
        return (f"{where_sql} AND {combined}" if where_sql else f"WHERE {combined}"), params

    def _project_compression_tips(self, sessions: List[Dict[str, Any]], compact_rows: bool) -> List[Dict[str, Any]]:
        """Replace each compression root's surfaced fields with its live tip's (root ``started_at`` kept
        for stable ordering), one batched query. ``_lineage_ids`` carries every chain id (a tile may
        hold a MIDDLE segment's id)."""
        chain_by_root: Dict[str, List[str]] = {}  # only roots whose tip differs from themselves
        for s in sessions:
            if s.get("end_reason") == "compression":
                chain = self.get_compression_chain(s["id"])
                if chain and chain[-1] != s["id"]:
                    chain_by_root[s["id"]] = chain
        tip_rows = (
            self._get_session_rich_rows_batch(
                {chain[-1] for chain in chain_by_root.values()}, compact_rows=compact_rows,
            ) if chain_by_root else {}
        )
        projected = []
        for s in sessions:
            chain = chain_by_root.get(s["id"])
            tip_row = tip_rows.get(chain[-1]) if chain else None
            if not tip_row:
                projected.append(s)
                continue
            merged = dict(s)
            for key in (
                "id", "ended_at", "end_reason", "message_count", "tool_call_count", "title", "last_active",
                "preview", "model", "system_prompt", "cwd", "git_branch", "git_repo_root",
            ):
                if key in tip_row:
                    merged[key] = tip_row[key]
            if merged.get("title") is None:
                # The title is carried root->tip AFTER the publish transaction; a rotation cut off in
                # between leaves it on the ended root, and exact-title lookups (`hermes peer dm` ->
                # canonical "Bot Chat") must still see the lineage under its name (#106165).
                merged["title"] = s.get("title")
            merged["_lineage_root_id"] = s["id"]
            merged["_lineage_ids"] = chain
            projected.append(merged)
        return projected

    @classmethod
    def _list_row(cls, row: sqlite3.Row) -> Dict[str, Any]:
        """Project a list_sessions_rich row: shape the preview, drop internal ordering columns."""
        s = cls._session_row_dict(row)
        s["preview"] = _shape_preview(s.pop("_preview_raw", ""))
        s.pop("_effective_last_active", None)
        return s

    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove ``<id>.json``/``.jsonl`` and gateway ``request_dump_<id>_*.json``; OSError is swallowed
        so a filesystem hiccup never blocks a DB operation."""
        if sessions_dir is None:
            return
        targets = [sessions_dir / f"{session_id}{suffix}" for suffix in (".json", ".jsonl")]
        try:
            targets.extend(sessions_dir.glob(f"request_dump_{session_id}_*.json"))
        except OSError:
            pass
        for p in targets:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # Shared by count_empty_sessions / delete_empty_sessions so badge and sweep agree. message_count
    # counts live rows only (rewind/compaction keep dropped turns as active = 0): NOT EXISTS is authority.
    # The ``NOT EXISTS`` probe is the authority; : ``message_count = 0`` stays as a cheap prefilter. Same
    # shape as every : other emptiness guard in this module. (#95868)
    _EMPTY_SESSION_WHERE = (
        "message_count = 0 AND ended_at IS NOT NULL AND archived = 0 AND NOT EXISTS ("
        "SELECT 1 FROM messages WHERE messages.session_id = sessions.id)"
    )
