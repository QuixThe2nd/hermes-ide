"""Transcript persistence for SessionDB: message append/replace/rewind, reactions, resume assembly,
replayed-user dedupe. Mixin bound via the MRO, built on SessionDB's _read_ctx/_execute_write/_read_* primitives."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.context_compressor import (
    _DB_PERSISTED_MARKER as _DB_PERSISTED_MARKER_KEY, _is_checkpoint_item, _newest_checkpoint_carrier,
    split_user_originated_turn)
from agent.memory_manager import sanitize_context
from agent.message_sanitization import _sanitize_surrogates
from hermes_cli.timefmt import coerce_epoch
from hermes_state_common import (
    _COMPRESSION_LOCK_ROW_SQL, _ENDED_ROW_SQL, _RESET_END_REASONS, _RESET_END_REASONS_SQL, _ended_by_compression,
    _legacy_reset_child_sql, _placeholders, _sql_json_extract)

logger = logging.getLogger("hermes_state")  # caplog tests pin the origin module's name

# One INSERT shape for every message writer (append, batch, replace, compact, import).
_INSERT_MESSAGE_SQL = """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, effect_disposition, timestamp, token_count, finish_reason,
                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                   codex_message_items, platform_message_id, observed, _compressed_summary, active, api_content, display_kind,
                   display_metadata, display_identity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
_BUMP_GENERATION_SQL = """
            INSERT INTO conversation_generations (source, session_key, generation)
            VALUES (?, ?, 1)
            ON CONFLICT(source, session_key) DO UPDATE
                SET generation = conversation_generations.generation + 1
            """

_TURN_LEASE_ROW_SQL = "SELECT holder, expires_at FROM session_turn_leases WHERE conversation_id = ?"
_DELETE_COMPRESSION_LOCK_SQL = "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?"
_DISPLAY_ACTIVE_CLAUSE = " AND (active = 1 OR compacted = 1)"
_DISPLAY_META_ROW_SQL = "SELECT display_metadata FROM messages WHERE id = ? AND session_id IN ({ids})" + _DISPLAY_ACTIVE_CLAUSE
_ACTIVE_IDS_SQL = "SELECT id FROM messages WHERE session_id = ? AND active = 1 ORDER BY id"
_SET_COUNTERS_SQL = "UPDATE sessions SET message_count = ?, tool_call_count = ?"
_RESET_COUNTERS_SQL = "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?"
_SET_DISPLAY_META_SQL = "UPDATE messages SET display_metadata = ? WHERE id = ?"
_ARCHIVE_ACTIVE_SQL = "UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1"
_SHADOWED_CHECKPOINT_ROWS_SQL = ("SELECT id, codex_reasoning_items FROM messages WHERE session_id = ? AND active = 1 "
    "AND role = 'assistant' AND id < ? AND codex_reasoning_items LIKE '%\"compaction\"%'")
_SET_CODEX_REASONING_SQL = "UPDATE messages SET codex_reasoning_items = ? WHERE id = ?"
_INVALID = object()  # _json_or sentinel where the fallback must be distinguishable from JSON null


def _json_or(raw: Any, fallback: Any, warning: str) -> Any:
    """``json.loads(raw)``; on failure log *warning* and return *fallback*."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(warning)
        return fallback


def _coerce_timestamp(value: Any, default: float) -> float:
    """Explicit message timestamp (datetime or number) or *default* when invalid or outside the sane
    epoch window — the write-side twin of the readers' ``coerce_epoch``: a bad row is never persisted."""
    result = coerce_epoch(value, field="message timestamp")
    if result is None:
        return default
    # SQLite reads both signed zero spellings back as 0.0. Hash the value
    # in that stored form so -0.0 and 0.0 retain the historical SQL/Python
    # identity equality.
    return 0.0 if result == 0.0 else result


def _parse_tool_calls(tool_calls: Any) -> Any:
    """tool_calls is a list (live agent) or JSON string (import/export); parse so json.dumps never double-encodes."""
    if not isinstance(tool_calls, str):
        return tool_calls
    try:
        return json.loads(tool_calls)
    except (json.JSONDecodeError, TypeError):
        return []


def _tool_calls_count(tool_calls: Any) -> int:
    return 0 if tool_calls is None else (len(tool_calls) if isinstance(tool_calls, list) else 1)


def _tool_calls_len(raw: Any, scalar: int = 0) -> int:
    """Count of a stored ``tool_calls`` column: list length, *scalar* for a truthy non-list, else 0."""
    parsed = _parse_tool_calls(raw)
    return len(parsed) if isinstance(parsed, list) else (scalar if parsed else 0)


def _scrub_surrogates(value: Any) -> Any:
    """Lone surrogates make sqlite3 raise UnicodeEncodeError and abort the whole write."""
    return _sanitize_surrogates(value) if isinstance(value, str) else value


def _stale_holder(row, now: float) -> bool:
    """A lock/lease row whose holder is expired or a provably dead local process."""
    from hermes_state import _compression_lock_holder_process_is_dead
    return float(row["expires_at"]) <= now or _compression_lock_holder_process_is_dead(row["holder"])


class SessionMessagesMixin:
    """Message append/replace/rewind, reactions, resume conversations, replay dedupe."""

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Serialize list/dict content (multimodal parts) as a sentinel-prefixed JSON string (sqlite3 binds
        only scalars). Lone UTF-16 surrogates (unsanitized web-scraped tool results) are scrubbed here: left
        raw, sqlite3 raises UnicodeEncodeError and the session silently stops persisting. Pairs with
        :meth:`_decode_content`."""
        if isinstance(content, str):
            return _sanitize_surrogates(content)
        if content is None or isinstance(content, (bytes, int, float)):
            return content
        try:
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)  # ensure_ascii escapes surrogates: bindable
        except (TypeError, ValueError):
            return _sanitize_surrogates(str(content))

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            return _json_or(content[len(cls._CONTENT_JSON_PREFIX):], content,
                "Failed to decode JSON-encoded message content; returning raw string")
        return content

    @staticmethod
    def _encode_display_metadata(display_metadata: Any) -> Optional[str]:
        """Serialize ``display_metadata`` for its TEXT column; an already-serialized JSON string
        (import/replace paths) is not double-encoded."""
        if not display_metadata:
            return None
        if isinstance(display_metadata, str):
            display_metadata = _json_or(display_metadata, _INVALID, "Ignoring non-JSON display metadata on write")
            if display_metadata is _INVALID:
                return None
            if not isinstance(display_metadata, dict):
                logger.warning("Ignoring non-object display metadata on write")
                return None
        elif not isinstance(display_metadata, dict):
            logger.warning("Ignoring unexpected display metadata type on write: %s", type(display_metadata).__name__)
            return None
        return json.dumps(display_metadata)

    @staticmethod
    def _decode_display_metadata(raw: Any) -> Optional[Dict[str, Any]]:
        """Decode a ``display_metadata`` column to a dict (never raw TEXT: the desktop does ``'task_count'
        in meta``). Pre-guard rows are double-encoded, so a second string layer is unwrapped."""
        if raw is None:
            return None
        meta = raw
        for _ in range(2):  # pre-guard rows carry a second string layer
            if isinstance(meta, str):
                meta = _json_or(meta, _INVALID, "Ignoring invalid display metadata on message row")
        if meta is _INVALID:
            return None
        if not isinstance(meta, dict):
            logger.warning("Ignoring non-object display metadata on message row")
            return None
        return meta

    @staticmethod
    def _reasoning_json_text(value: Any) -> Optional[str]:
        """Serialize a structured reasoning field for its TEXT column. Strings are stored as-is: round-trips
        (get_messages -> replace_messages) hand back raw TEXT; re-dumping would double-encode it and
        reasoning-replay consumers (``isinstance(..., list)``) would drop it."""
        return None if not value else (value if isinstance(value, str) else json.dumps(value))

    def _message_row_params(self, session_id: str, role: str, msg: Dict[str, Any], tool_calls: Any,
        message_timestamp: float, *, keep_reasoning: bool) -> tuple:
        """Bind values for ``_INSERT_MESSAGE_SQL`` from one message dict (*tool_calls* already parsed;
        *keep_reasoning* False NULLs every reasoning column). ``platform_message_id`` falls back to
        ``message_id`` when ``platform_message_id`` is absent."""
        _str_or_none = lambda v: _scrub_surrogates(v) if isinstance(v, str) else None  # noqa: E731
        _reasoning = lambda key: msg.get(key) if keep_reasoning else None  # noqa: E731
        encoded_content = self._encode_content(msg.get("content"))
        encoded_tool_calls = json.dumps(tool_calls) if tool_calls else None
        encoded_tool_name = _scrub_surrogates(msg.get("tool_name"))
        display_metadata = self._encode_display_metadata(msg.get("display_metadata"))
        identity_row = {
            "role": role, "content": encoded_content, "timestamp": message_timestamp,
            "tool_call_id": msg.get("tool_call_id"), "tool_calls": encoded_tool_calls,
            "tool_name": encoded_tool_name, "display_kind": msg.get("display_kind"),
            "display_metadata": display_metadata,
        }
        return (session_id, role, encoded_content, msg.get("tool_call_id"),
            encoded_tool_calls, encoded_tool_name,
            msg.get("effect_disposition"), message_timestamp, msg.get("token_count"), msg.get("finish_reason"),
            _scrub_surrogates(_reasoning("reasoning")), _scrub_surrogates(_reasoning("reasoning_content")),
            *(self._reasoning_json_text(_reasoning(k))
              for k in ("reasoning_details", "codex_reasoning_items", "codex_message_items")),
            msg.get("platform_message_id") or msg.get("message_id"),
            1 if msg.get("observed") else 0, 1 if msg.get("_compressed_summary") else 0, 1,
            _str_or_none(msg.get("api_content")), _str_or_none(msg.get("display_kind")),
            display_metadata, self._display_identity(self._display_dedupe_key(identity_row)))

    @staticmethod
    def _bump_session_counters(conn, session_id: str, inserted: int, tool_calls: int, *, unit: bool) -> None:
        """Bump sessions.* counters after an insert; *unit* bakes the ``+ 1`` literal into the SQL."""
        inc, params = ("1", ()) if unit else ("?", (inserted,))
        if tool_calls > 0:
            conn.execute(
                f"""UPDATE sessions SET message_count = message_count + {inc},
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                (*params, tool_calls, session_id))
        elif inserted > 0:
            conn.execute(
                f"UPDATE sessions SET message_count = message_count + {inc} WHERE id = ?", (*params, session_id))

    def append_delegation_delivery(self, session_id: str, content: str, metadata: Dict[str, Any]) -> int:
        """Record a detached API result once, between client turns, including replay after rotation.

        The event's unit id, not its text or active flag, is the identity. Check and insert
        share the writer transaction, so independent gateway processes cannot duplicate it.
        """
        delegation_id = metadata.get("delegation_id")
        if not delegation_id:
            raise ValueError("Delegation delivery requires a stable delegation_id")
        msg = {"content": content,
               "display_kind": "hidden" if metadata.get("presentation_suppressed") else "async_delegation_complete",
               "display_metadata": metadata}
        params = self._message_row_params(session_id, "user", msg, None, time.time(), keep_reasoning=True)

        def _do(conn):
            existing = conn.execute(
                """WITH RECURSIVE lineage(id) AS (
                    SELECT ? UNION
                    SELECT s.parent_session_id FROM sessions s JOIN lineage l ON s.id = l.id
                    JOIN sessions p ON p.id = s.parent_session_id WHERE p.end_reason = 'compression'
                ) SELECT m.id FROM messages m JOIN lineage l ON m.session_id = l.id
                WHERE m.display_kind IN ('async_delegation_complete', 'hidden')
                AND json_extract(m.display_metadata, '$.delegation_id') = ?
                AND coalesce(json_extract(m.display_metadata, '$.delivery_notice'), '') = ? LIMIT 1""",
                (session_id, delegation_id, metadata.get("delivery_notice", ""))).fetchone()
            if existing is not None:
                return existing[0]
            self._check_transcript_write_guards(conn, session_id, None, reject_active_turn_lease=True)
            msg_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
            self._bump_session_counters(conn, session_id, 1, 0, unit=True)
            return msg_id

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

    def _reaction_list(self, meta: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Well-formed (dict) reactions stored under ``REACTIONS_METADATA_KEY``."""
        reactions = (meta or {}).get(self.REACTIONS_METADATA_KEY)
        return [r for r in reactions if isinstance(r, dict)] if isinstance(reactions, list) else []

    def _reaction_row_query(self, session_id: str, message_row_id: int) -> Tuple[str, tuple]:
        """A reaction addresses a row the client can SEE, and a display resume materializes the whole
        compression lineage (active + compacted rows, with row ids) — so a row is "in this session" when
        its owner is any lineage segment, not only the tip, and a rewound row is not. Explicit ``/branch``
        copies keep their own rows (``_resume_lineage_ids``)."""
        lineage = self._resume_lineage_ids(session_id)
        return _DISPLAY_META_ROW_SQL.format(ids=_placeholders(lineage)), (message_row_id, *lineage)

    def latest_conversation_role(self, session_id: str) -> Optional[str]:
        """Role of the newest active user/assistant/tool row, or ``None``. ``session_meta`` /
        ``system`` rows are transcript bookkeeping stripped before the model sees history, so
        they must not hide an open user tail from the failed-turn boundary check."""
        row = self._read_one(
            "SELECT role FROM messages WHERE session_id = ? AND active = 1 "
            "AND role NOT IN ('session_meta', 'system') ORDER BY id DESC LIMIT 1", (session_id,))
        return row[0] if row else None

    def _drop_shadowed_checkpoint_rows(self, conn, session_id: str, carrier_row_id: int) -> int:
        """Rewrite older active assistant rows so only the row *carrier_row_id* keeps a ``type: "compaction"``
        checkpoint (durable twin of ``context_compressor.drop_shadowed_checkpoints``). Under native compaction
        every assistant response re-persists a ~120 KB checkpoint the wire builder will never replay once a
        newer one lands, and local compaction — the only other prune site — rarely fires (#102374).
        Non-checkpoint items stay; returns rows rewritten."""
        rewritten = 0
        for row_id, raw in conn.execute(_SHADOWED_CHECKPOINT_ROWS_SQL, (session_id, carrier_row_id)).fetchall():
            items = _json_or(raw, None, "Ignoring malformed codex_reasoning_items on message row")
            if not isinstance(items, list) or not any(_is_checkpoint_item(item) for item in items):
                continue
            kept = [item for item in items if not _is_checkpoint_item(item)]
            conn.execute(_SET_CODEX_REASONING_SQL, (self._reasoning_json_text(kept), row_id))
            rewritten += 1
        return rewritten

    def _tail_rows_after_watermark(self, conn, sql: str, params) -> Tuple[List[int], int]:
        """``(ids, tool_call_count)`` of the concurrent-tail rows selected by *sql* (``SELECT id, tool_calls``)."""
        rows = conn.execute(sql, params).fetchall()
        return [int(r["id"]) for r in rows], sum(_tool_calls_len(r["tool_calls"]) for r in rows)

    def _clone_message_rows(self, conn, tail_ids: List[int], *, session_id: Optional[str] = None) -> None:
        """Pure-SQL clone of *tail_ids* as fresh live rows (new id/display order, active=1, compacted=0;
        message payload columns stay byte-exact and FTS triggers index the clones), into *session_id* when given."""
        retarget = session_id is not None
        # A clone is a newly positioned display generation. Copy its indexed
        # identity, but let the insert trigger assign order from rows that are
        # still display-visible (the source may just have become rewind-only).
        skip = ("id", "active", "compacted", "display_order") + (("session_id",) if retarget else ())
        col_list = ", ".join(c for c in self._message_column_names(conn) if c not in skip)
        conn.execute(
            f"INSERT INTO messages ({col_list}, {'session_id, ' if retarget else ''}active, compacted) "
            f"SELECT {col_list}, {'?, ' if retarget else ''}1, 0 FROM messages "
            f"WHERE id IN ({_placeholders(tail_ids)}) ORDER BY id",
            [session_id, *tail_ids] if retarget else tail_ids)

    def set_message_api_content(
        self, session_id: str, row_id: int, content: Any, api_content: str
    ) -> int:
        """Backfill the ``api_content`` sidecar onto ONE known durable row.

        Row-addressed counterpart to :meth:`set_latest_user_api_content`: the
        caller passes the ``_row_id`` the write path stamped on the live
        message dict, so the update cannot drift onto a neighbouring row that
        merely carries the same text.

        Used by the turn prologue whenever the current turn's user row was
        already materialized before the sidecar could be composed (in-place
        preflight compaction, a close/early flush that raced the prologue).
        The crash persist then marker-skips that message, so this is the only
        way the stamped bytes reach the store.

        ``active = 1`` and the ``content`` match stay as defensive guards: a
        row the compaction archived, or one a racing rewrite changed, is left
        untouched.
        """
        if not session_id or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
            return 0
        return self._write_rowcount(
            "UPDATE messages SET api_content = ? WHERE id = ? AND session_id = ? "
            "AND role = 'user' AND active = 1 AND content IS ?",
            (_scrub_surrogates(api_content), row_id, session_id, self._encode_content(content)))

    def set_user_message_content(self, session_id: str, row_id: int, content: Any) -> int:
        """Rewrite the content of ONE known active user row. Used when a user turn was written at submit
        time (before the agent ran) and the turn prologue then rewrote the prompt it persists (@-file
        expansion, native image parts): the early row must show what the transcript will replay, not the
        raw keystrokes, and the turn must not append a second row for the same input."""
        if not session_id or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
            return 0
        return self._write_rowcount(
            "UPDATE messages SET content = ? WHERE id = ? AND session_id = ? AND role = 'user' AND active = 1",
            (self._encode_content(content), row_id, session_id))

    def _display_dedupe_key(self, row) -> Tuple[Any, ...]:
        """Historical display identity, including normalized live content from user handoff carriers."""
        dedupe_content = row["content"]
        if row["role"] == "user":
            handoff, live_view = split_user_originated_turn({
                "role": "user", "content": self._decode_content(row["content"]),
                "display_kind": row["display_kind"],
                "display_metadata": self._decode_display_metadata(row["display_metadata"])})
            if handoff is not None and live_view is not None:
                dedupe_content = self._encode_content(live_view.get("content"))
        return (row["role"], dedupe_content, row["timestamp"],
                row["tool_call_id"], row["tool_calls"], row["tool_name"])

    @staticmethod
    def _display_identity(key: Tuple[Any, ...]) -> bytes:
        """Fixed-width durable identity for indexed display-generation lookup."""
        return hashlib.sha256(repr(key).encode("utf-8", "surrogatepass")).digest()

    def _ensure_display_order(self, session_id: str) -> bool:
        """Backfill one legacy session once, preserving the pre-index display identity exactly."""
        with self._read_ctx() as conn:
            columns = set(self._message_column_names(conn))
        if not {"display_order", "display_identity"} <= columns:
            return False
        missing_sql = (
            "SELECT 1 FROM messages WHERE session_id = ? AND (active = 1 OR compacted = 1) "
            "AND (display_order IS NULL OR display_identity IS NULL) LIMIT 1")
        if self._read_one(missing_sql, (session_id,)) is None:
            return True
        if getattr(self, "read_only", False):
            return False

        def _do(conn):
            missing = conn.execute(missing_sql, (session_id,)).fetchone()
            if missing is None:
                return True
            first_id: Dict[bytes, int] = {}
            last_id = 0
            while True:
                rows = conn.execute(
                    "SELECT id, role, content, timestamp, tool_call_id, tool_calls, tool_name, "
                    "display_kind, display_metadata, display_order, display_identity "
                    "FROM messages INDEXED BY idx_messages_session_id "
                    "WHERE session_id = ? AND id > ? AND (active = 1 OR compacted = 1) "
                    "ORDER BY id LIMIT 1000",
                    (session_id, last_id))
                batch_start = last_id
                updates = []
                for row in rows:
                    last_id = row["id"]
                    identity = self._display_identity(self._display_dedupe_key(row))
                    order = first_id.setdefault(identity, last_id)
                    if order != row["display_order"] or identity != row["display_identity"]:
                        updates.append((order, identity, last_id))
                rows.close()
                if last_id == batch_start:
                    break
                conn.executemany(
                    "UPDATE messages SET display_order = ?, display_identity = ? WHERE id = ?", updates)
            return True

        return bool(self._execute_write(_do))

    def _legacy_display_page(self, session_id: str, *, active_clause: str, limit: Optional[int], offset: int,
                             latest: bool) -> List[Any]:
        """Project a legacy read-only display page without retaining transcript payloads."""
        representatives: Dict[bytes, Tuple[int, int]] = {}
        with self._read_ctx() as conn:
            conn.execute("BEGIN")
            try:
                has_session_index = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                    ("idx_messages_session_id",),
                ).fetchone() is not None
                index_hint = "INDEXED BY idx_messages_session_id" if has_session_index else "NOT INDEXED"
                rows = conn.execute(
                    "SELECT id, role, content, timestamp, tool_call_id, tool_calls, tool_name, active, "
                    f"display_kind, display_metadata FROM messages {index_hint} "
                    f"WHERE session_id = ?{active_clause} ORDER BY id ASC",
                    (session_id,))
                for row in rows:
                    identity = self._display_identity(self._display_dedupe_key(row))
                    current = representatives.get(identity)
                    candidate = (row["active"], row["id"])
                    if current is None or candidate > current:
                        representatives[identity] = candidate
                rows.close()

                identities = list(representatives)
                identities = identities[::-1][offset:][:limit][::-1] if latest else identities[offset:][:limit]
                selected_ids = [representatives[identity][1] for identity in identities]
                selected = {}
                for start in range(0, len(selected_ids), 900):
                    chunk = selected_ids[start:start + 900]
                    selected.update({row["id"]: row for row in conn.execute(
                        f"SELECT * FROM messages WHERE session_id = ?{active_clause} "
                        f"AND id IN ({_placeholders(chunk)})",
                        (session_id, *chunk))})
                return [selected[row_id] for row_id in selected_ids if row_id in selected]
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")

    def _row_to_message_dict(self, row, *, warn_context: str, summary_flag: bool) -> Dict[str, Any]:
        """``dict(row)`` with content/tool_calls/display_metadata decoded; *summary_flag* keeps
        ``_compressed_summary`` only as ``True``."""
        msg = dict(row)
        msg.pop("display_identity", None)
        msg.pop("display_order", None)
        if summary_flag and msg.pop("_compressed_summary", 0):
            msg["_compressed_summary"] = True
        msg["content"] = self._decode_content(msg["content"])
        if msg.get("tool_calls"):
            msg["tool_calls"] = _json_or(
                msg["tool_calls"], [], f"Failed to deserialize tool_calls in {warn_context}, falling back to []")
        if msg.get("display_metadata") is not None:
            msg["display_metadata"] = self._decode_display_metadata(msg["display_metadata"])
        return msg

    @staticmethod
    def _active_clause(include_inactive: bool, include_compacted: bool) -> str:
        """Audit: every row; display: active plus compaction-archived (never Undo/Rewind rows); default: live."""
        return "" if include_inactive else (_DISPLAY_ACTIVE_CLAUSE if include_compacted else " AND active = 1")

    def _fetch_conversation_rows(self, session_ids: List[str], active_clause: str, *, with_session_id: bool):
        """``_CONVERSATION_ROW_COLUMNS`` rows for *session_ids* ORDER BY id (timestamps are not monotonic
        and would break tool-call adjacency)."""
        return self._read_all(
            f"SELECT {'session_id, ' if with_session_id else ''}{self._CONVERSATION_ROW_COLUMNS} "
            f"FROM messages WHERE session_id IN ({_placeholders(session_ids)})"
            f"{active_clause} ORDER BY id", tuple(session_ids))

    def _dedupe_replayed_user(self, messages, msg, exact_user_clones) -> Tuple[bool, Any]:
        """Ancestor-lineage dedupe of one decoded user *msg* -> ``(skip, exact_clone_key)``. Rotation
        column-clones the concurrent tail into the child, so copies need not be adjacent: the exact
        ``(timestamp, canonical content)`` clone index is checked first, then the adjacent heuristic. A
        rotated child carrier wins over the ancestor copy."""
        canonical_content = self._canonical_replayed_user_content(msg)[0]
        exact_clone_key = self._exact_replayed_user_clone_key(msg.get("timestamp"), canonical_content)
        previous_exact = exact_user_clones.get(exact_clone_key) if exact_clone_key is not None else None
        duplicate = None
        if previous_exact is not None:
            previous_index = next((i for i, candidate in enumerate(messages) if candidate is previous_exact), None)
            if previous_index is not None:
                duplicate = (previous_index, True)
        if duplicate is None:
            duplicate = self._find_duplicate_replayed_user_message(messages, msg)
        if duplicate is None:
            return False, exact_clone_key
        duplicate_index, prefer_current = duplicate
        if prefer_current:
            messages.pop(duplicate_index)
        return not prefer_current, exact_clone_key

    def _resume_count_scope(self, session_id: str, tip_only: bool) -> Tuple[List[str], str]:
        """``tip_only``: the tip's ACTIVE rows (model restore); else the full-lineage DISPLAY set."""
        if tip_only:
            return [session_id], "active = 1"
        return self._resume_lineage_ids(session_id), "(active = 1 OR compacted = 1)"

    @staticmethod
    def _canonical_replayed_user_content(msg: Dict[str, Any]) -> Tuple[Any, bool]:
        """Return canonical live content and whether *msg* is composite."""
        if msg.get("role") != "user":
            return None, False
        handoff, live_view = split_user_originated_turn(msg)
        is_composite = handoff is not None and live_view is not None
        return live_view.get("content") if is_composite else msg.get("content"), is_composite

    @staticmethod
    def _exact_replayed_user_clone_key(timestamp: Any, content: Any) -> Optional[Tuple[Any, str]]:
        """Return a hashable key for a column-exact rotation clone."""
        if timestamp is None or content in (None, "", []):
            return None
        try:
            return timestamp, json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _find_duplicate_replayed_user_message(messages: List[Dict[str, Any]],
                                              msg: Dict[str, Any]) -> Optional[Tuple[int, bool]]:
        """Adjacent replay duplicate ``(index, prefer_current)`` or None. Rotation may persist the current ask
        in the parent and again inside a composite child carrier: carriers compare by canonical live payload,
        ordinary users by exact string. The child carrier wins (it owns the durable row id and scaffold)."""
        if msg.get("role") != "user":
            return None
        canonical = SessionMessagesMixin._canonical_replayed_user_content
        content, prefer_current = canonical(msg)
        if content in (None, "", []):
            return None
        for index in range(len(messages) - 1, -1, -1):
            prev = messages[index]
            if prev.get("role") == "user":
                prev_content, prev_is_composite = canonical(prev)
                if prev_content == content and (prefer_current or prev_is_composite or isinstance(content, str)):
                    return index, prefer_current
            elif prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                return None
        return None

    # ========================================================================= Rewind (soft-delete) — see
    # /rewind slash command + issue #21910
    # =========================================================================

    @staticmethod
    def _active_transcript_counts(conn, session_id: str) -> tuple[int, int]:
        """Return active message/tool-call counts inside the caller's txn."""
        rows = conn.execute("SELECT tool_calls FROM messages WHERE session_id = ? AND active = 1", (session_id,)).fetchall()
        return len(rows), sum(_tool_calls_len(row[0], scalar=1) for row in rows)

    def _split_rewind_target(self, target_row: Dict[str, Any], expected_target_content: Any, preserve_compaction_handoff: bool):
        """Validate an active rewind target; return its handoff scaffold (or None). ``ValueError``: inactive /
        non-user-originated / missing composite carrier; ``RuntimeError``: live payload changed."""
        if not target_row.get("active"):
            raise ValueError("rewind target is not active")
        handoff, live_view = split_user_originated_turn({
            **target_row, "content": self._decode_content(target_row.get("content")),
            "display_metadata": self._decode_display_metadata(target_row.get("display_metadata"))})
        if live_view is None:
            raise ValueError("rewind target is not a user-originated turn")
        live_content = live_view.get("content")
        if isinstance(live_content, str):
            live_content = sanitize_context(live_content).strip()
        if expected_target_content is not None and live_content != expected_target_content:
            raise RuntimeError("rewind target changed before it could be persisted")
        if preserve_compaction_handoff and handoff is None:
            raise ValueError("preserve_compaction_handoff requires an active composite carrier")
        return handoff if preserve_compaction_handoff else None

    def has_gateway_input_owner(self, session_id: str, owner: str) -> bool:
        """Probe the accepted-input marker without allocating message bodies or archives."""
        return self._read_one(
            "SELECT 1 FROM messages WHERE session_id = ? AND role = 'user' "
            "AND observed = 0 AND (active = 1 OR compacted = 1) "
            "AND CASE WHEN json_valid(display_metadata) "
            "THEN json_extract(display_metadata, '$.gateway_input_owner') END = ? LIMIT 1",
            (session_id, owner)) is not None
