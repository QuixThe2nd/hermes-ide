"""Gateway-facing SessionDB persistence: routing index, peers, orphans, heartbeats, handoffs.

Mixin bound onto ``SessionDB`` via the MRO; built on its ``_read_ctx`` /
``_execute_write`` / ``_write_sql`` / ``_read_all`` primitives."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_state_common import (
    _RECOVERABLE_END_REASONS_SQL, _RESET_CHILD_SQL, _RESET_END_REASONS_SQL, _sql_json_extract,
    _sql_session_last_active)

# Log-record parity with the origin module (caplog tests pin "hermes_state").
logger = logging.getLogger("hermes_state")

# Recursive CTE naming a session plus its compression ancestors (rows a
# resume must keep on one routing peer); branch/delegate/tool/reset rows stop it
# (same membership rule as get_compression_lineage / _CHAIN_STEP_SQL, #114271).
_COMPRESSION_LINEAGE_CTE = f"""
                    WITH RECURSIVE compression_lineage(id) AS (
                        SELECT ?
                        UNION
                        SELECT parent.id
                        FROM compression_lineage lineage
                        JOIN sessions child ON child.id = lineage.id
                        JOIN sessions parent ON parent.id = child.parent_session_id
                        WHERE parent.end_reason = 'compression'
                          AND {_sql_json_extract('child.model_config', '$._branched_from')} IS NULL
                          AND {_sql_json_extract('child.model_config', '$._delegate_from')} IS NULL
                          AND NOT ({_RESET_CHILD_SQL.format(a='child')})
                          AND COALESCE(child.source, '') != 'tool'
                    )
                """

# Projection shared by both peer-recovery queries (exact key, then peer tuple).
_PEER_SELECT_HEAD = """
                SELECT s.*,
                       COALESCE(sp.prompt, s.system_prompt)
                           AS _system_prompt_resolved,
                       (COALESCE(s.message_count, 0) > 0 OR EXISTS (
                           SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1
                       )) AS _has_messages
                FROM sessions s
                LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash
"""
_PEER_BY_KEY_SQL = f"""{_PEER_SELECT_HEAD}                WHERE s.session_key = ?
                  AND s.source = ?
                  AND (s.ended_at IS NULL OR s.end_reason IN ({_RECOVERABLE_END_REASONS_SQL}))
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions b
                      WHERE b.session_key = s.session_key
                        AND b.source = s.source
                        AND b.ended_at IS NOT NULL
                        AND b.end_reason IN ({_RESET_END_REASONS_SQL})
                        AND b.ended_at
                            > COALESCE(s.last_activity_at, s.started_at)
                  )
                ORDER BY _has_messages DESC,
                         COALESCE(s.last_activity_at, s.started_at) DESC
                LIMIT 1
                """
_PEER_BY_TUPLE_SQL = f"""{_PEER_SELECT_HEAD}                WHERE s.source = ?
                  AND COALESCE(s.user_id, '') = COALESCE(?, '')
                  AND COALESCE(s.chat_id, '') = COALESCE(?, '')
                  AND COALESCE(s.chat_type, '') = COALESCE(?, '')
                  AND COALESCE(s.thread_id, '') = COALESCE(?, '')
                  AND (? IS NULL OR COALESCE(s.profile_name, ?) = ?)
                  AND (s.ended_at IS NULL OR s.end_reason IN ({_RECOVERABLE_END_REASONS_SQL}))
                  AND (COALESCE(s.message_count, 0) > 0 OR EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = s.id LIMIT 1
                  ))
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions b
                      WHERE b.source = s.source
                        AND COALESCE(b.user_id, '') = COALESCE(s.user_id, '')
                        AND COALESCE(b.chat_id, '') = COALESCE(s.chat_id, '')
                        AND COALESCE(b.chat_type, '') = COALESCE(s.chat_type, '')
                        AND COALESCE(b.thread_id, '') = COALESCE(s.thread_id, '')
                        AND b.ended_at IS NOT NULL
                        AND b.end_reason IN ({_RESET_END_REASONS_SQL})
                        AND b.ended_at
                            > COALESCE(s.last_activity_at, s.started_at)
                  )
                ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC
                LIMIT 1
                """

_ORPHAN_DONOR_COLUMNS = (
    "d.id, d.session_key, d.chat_id, d.chat_type, d.thread_id, "
    "d.user_id, d.origin_json, d.display_name, d.end_reason"
)
_ORPHANS_SQL = f"""
                SELECT o.id, o.source, o.user_id, o.started_at,
                       o.parent_session_id,
                       {_sql_session_last_active("o")} AS last_active,
                       (SELECT COUNT(*) FROM messages m
                         WHERE m.session_id = o.id) AS message_count
                FROM sessions o
                WHERE o.session_key IS NULL
                  AND EXISTS (SELECT 1 FROM messages m
                               WHERE m.session_id = o.id)
                  AND COALESCE(o.source, '') != 'tool'
                  AND {_sql_json_extract('o.model_config', '$._branched_from')} IS NULL
                  AND {_sql_json_extract('o.model_config', '$._delegate_from')} IS NULL
                ORDER BY o.started_at ASC
                """
_ORPHAN_LINEAGE_DONOR_SQL = f"""
                        SELECT {_ORPHAN_DONOR_COLUMNS}
                        FROM sessions d
                        WHERE d.id = ?
                          AND d.session_key IS NOT NULL
                          AND COALESCE(d.source, '') = COALESCE(?, '')
                        """
_ORPHAN_CONTIGUITY_DONORS_SQL = f"""
                        SELECT {_ORPHAN_DONOR_COLUMNS}, {_sql_session_last_active("d")} AS last_active
                        FROM sessions d
                        WHERE d.session_key IS NOT NULL
                          AND d.id != ?
                          AND COALESCE(d.source, '') = COALESCE(?, '')
                          AND (COALESCE(d.user_id, '') = ''
                               OR COALESCE(?, '') = ''
                               OR d.user_id = ?)
                          AND {_sql_session_last_active("d")} BETWEEN ? AND ?
                          AND {_sql_session_last_active("d")} < ?
                        ORDER BY last_active DESC
                        LIMIT 2
                        """
_OPTIONAL_TABLE_NAMES = (
    "telegram_dm_topic_mode", "telegram_dm_topic_bindings", "delivery_obligations")


def _optional_table_columns(conn) -> Dict[str, Set[str]]:
    """Live column sets of the lazily-created tables that exist (``{}`` when none do).

    ``apply_telegram_topic_migration`` runs only on explicit ``/topic`` opt-in, so a store
    can hold supported v1/v2 tables without ``profile_name`` for its whole life; likewise the
    delivery ledger adds ``delivery_obligations.adapter_profile`` only when a gateway opens
    it. Identity settlement must gate its column SQL on the column actually being there
    rather than on table existence, and must not force those migrations (#113757).
    """
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?)",
        _OPTIONAL_TABLE_NAMES)}
    return {
        table: {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")}
        for table in _OPTIONAL_TABLE_NAMES if table in existing
    }


_HANDOFF_FAIL_SQL = "UPDATE sessions SET handoff_state = 'failed', handoff_error = ? WHERE "


class SessionGatewayMixin:
    """Routing index, session peers/orphans, hygiene streaks, heartbeats, handoffs."""

    def gateway_routing_entry_for_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """The routing entry (any scope) whose current owner is *session_id*, or None. The id lives
        only inside ``entry_json``, so matching is done in Python; an archived/rotated row has none."""
        for row in self._read_all("SELECT entry_json FROM gateway_routing"):
            try:
                entry = json.loads(row["entry_json"] or "{}")
            except Exception:
                continue
            if isinstance(entry, dict) and entry.get("session_id") == session_id:
                return entry
        return None

    def rekey_profile_state(self, old_name: str, new_name: str) -> Dict[str, int]:
        """Atomically rewrite exact profile identity in this state database."""
        old, new = (old_name or "").strip(), (new_name or "").strip()
        counts: Dict[str, int] = {}
        if not old or not new or old == new:
            return counts
        old_ns, new_ns = f"agent:{old}:", f"agent:{new}:"
        ns_len = len(old_ns)

        def _do(conn):
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            topic_columns = _optional_table_columns(conn)
            collision = conn.execute(
                "SELECT old.scope, ? || substr(old.session_key, ?) "
                "FROM gateway_routing AS old JOIN gateway_routing AS target "
                "ON target.scope = old.scope "
                "AND target.session_key = ? || substr(old.session_key, ?) "
                "WHERE substr(old.session_key, 1, ?) = ? LIMIT 1",
                (new_ns, ns_len + 1, new_ns, ns_len + 1, ns_len, old_ns),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    f"profile routing collision in scope {collision[0]!r}: {collision[1]!r}")
            for table, columns in (
                ("telegram_dm_topic_mode", ("chat_id",)),
                ("telegram_dm_topic_bindings", ("chat_id", "thread_id")),
            ):
                if "profile_name" not in topic_columns.get(table, set()):
                    continue
                equality = " AND ".join(
                    f"target.{column} = old.{column}" for column in columns)
                collision = conn.execute(
                    f"SELECT 1 FROM {table} AS old JOIN {table} AS target "
                    f"ON target.profile_name = ? AND {equality} "
                    "WHERE old.profile_name = ? LIMIT 1", (new, old)).fetchone()
                if collision is not None:
                    raise ValueError(f"profile identity collision in {table}")

            counts["sessions_profile_name"] = conn.execute(
                "UPDATE sessions SET profile_name = ? WHERE profile_name = ?", (new, old)).rowcount
            counts["gateway_heartbeats_profile"] = conn.execute(
                "UPDATE gateway_heartbeats SET profile = ? WHERE profile = ?", (new, old)).rowcount
            counts["sessions_session_key"] = conn.execute(
                "UPDATE sessions SET session_key = ? || substr(session_key, ?) "
                "WHERE substr(session_key, 1, ?) = ?",
                (new_ns, ns_len + 1, ns_len, old_ns)).rowcount

            origin_count = 0
            for session_id, origin_json in conn.execute(
                    "SELECT id, origin_json FROM sessions WHERE origin_json IS NOT NULL").fetchall():
                try:
                    payload = json.loads(origin_json)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get("profile") == old:
                    payload["profile"] = new
                    conn.execute("UPDATE sessions SET origin_json = ? WHERE id = ?",
                                 (json.dumps(payload, ensure_ascii=False), session_id))
                    origin_count += 1
            counts["sessions_origin_json"] = origin_count

            if "delivery_obligations" in existing:
                if "adapter_profile" in topic_columns.get("delivery_obligations", set()):
                    counts["delivery_obligations_adapter_profile"] = conn.execute(
                        "UPDATE delivery_obligations SET adapter_profile = ? WHERE adapter_profile = ?",
                        (new, old)).rowcount
                counts["delivery_obligations_session_key"] = conn.execute(
                    "UPDATE delivery_obligations SET session_key = ? || substr(session_key, ?) "
                    "WHERE substr(session_key, 1, ?) = ?",
                    (new_ns, ns_len + 1, ns_len, old_ns)).rowcount
            for table in ("telegram_dm_topic_mode", "telegram_dm_topic_bindings"):
                if "profile_name" in topic_columns.get(table, set()):
                    counts[f"{table}_profile_name"] = conn.execute(
                        f"UPDATE {table} SET profile_name = ? WHERE profile_name = ?",
                        (new, old)).rowcount
            if "session_key" in topic_columns.get("telegram_dm_topic_bindings", set()):
                counts["telegram_dm_topic_bindings_session_key"] = conn.execute(
                    "UPDATE telegram_dm_topic_bindings "
                    "SET session_key = ? || substr(session_key, ?) "
                    "WHERE substr(session_key, 1, ?) = ?",
                    (new_ns, ns_len + 1, ns_len, old_ns)).rowcount

            routing = conn.execute(
                "SELECT rowid, session_key, entry_json FROM gateway_routing "
                "WHERE substr(session_key, 1, ?) = ?", (ns_len, old_ns)).fetchall()
            for rowid, session_key, entry_json in routing:
                new_session_key = new_ns + session_key[ns_len:]
                new_json = entry_json
                if entry_json:
                    try:
                        payload = json.loads(entry_json)
                    except (ValueError, TypeError):
                        payload = None
                    if isinstance(payload, dict):
                        if isinstance(payload.get("session_key"), str) and payload["session_key"].startswith(old_ns):
                            payload["session_key"] = new_ns + payload["session_key"][ns_len:]
                        origin = payload.get("origin")
                        if isinstance(origin, dict) and origin.get("profile") == old:
                            origin["profile"] = new
                        new_json = json.dumps(payload, ensure_ascii=False)
                conn.execute(
                    "UPDATE gateway_routing SET session_key = ?, entry_json = ? WHERE rowid = ?",
                    (new_session_key, new_json, rowid))
            counts["gateway_routing"] = len(routing)

        self._execute_write(_do)
        return counts

    def purge_profile_state(self, profile: str) -> Dict[str, int]:
        """Delete exact profile identity from this state database (#111926, delete side).

        The mirror of :meth:`rekey_profile_state`: a rename must rekey a profile's identity, a
        delete must purge it. ``agent:<name>:*`` routing keys, ``gateway_heartbeats.profile``,
        ``delivery_obligations`` and the telegram topic tables are bookkeeping for a profile that
        no longer exists — left behind, every inbound event on a chat keyed to the deleted name
        enters the routing index, resolves a profile whose directory is gone, and logs
        ``Profile '<name>' does not exist`` on each event for the life of the store.

        What each store gets, and why:

        * Routing keys and heartbeat rows are hard-deleted — pure bookkeeping for a dead name.
        * ``delivery_obligations`` rows are **terminalized** (``state='abandoned'``), not deleted:
          a pending obligation is delivery state that should not vanish silently, and the ledger's
          own retention prunes abandoned rows. Delivered history is left as it was.
        * ``sessions`` rows are not deleted here: this helper settles identity, not history, and it
          does not decide what a delete leaves of a profile's conversation record — ``delete_profile``
          removes the profile's own home, ``state.db`` included, with the directory. Rows in a shared
          store keep whatever ownership they had; re-binding or archiving them belongs to the flow
          that recreates the name, not to this purge.

        Idempotent.
        """
        name = (profile or "").strip()
        counts: Dict[str, int] = {}
        if not name:
            return counts
        ns, ns_len = f"agent:{name}:", len(f"agent:{name}:")

        def _do(conn):
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            topic_columns = _optional_table_columns(conn)
            if "gateway_routing" in existing:
                counts["gateway_routing"] = conn.execute(
                    "DELETE FROM gateway_routing WHERE substr(session_key, 1, ?) = ?",
                    (ns_len, ns)).rowcount
            if "gateway_heartbeats" in existing:
                counts["gateway_heartbeats"] = conn.execute(
                    "DELETE FROM gateway_heartbeats WHERE profile = ?", (name,)).rowcount
            if "delivery_obligations" in existing:
                # Terminalize, never hard-delete: a pending obligation is delivery state someone may
                # still care about, and the ledger's own retention prunes abandoned rows. Only
                # non-terminal rows are touched — delivered history is left exactly as it was.
                # A ledger created before ``adapter_profile`` existed matches on namespace alone.
                by_profile = ("adapter_profile = ? OR "
                              if "adapter_profile" in topic_columns.get("delivery_obligations", set())
                              else "")
                params = (time.time(), name, ns_len, ns) if by_profile else (time.time(), ns_len, ns)
                counts["delivery_obligations"] = conn.execute(
                    "UPDATE delivery_obligations SET state='abandoned', updated_at=? "
                    f"WHERE ({by_profile}substr(session_key, 1, ?) = ?) "
                    "AND state NOT IN ('delivered', 'abandoned')", params).rowcount
            if "profile_name" in topic_columns.get("telegram_dm_topic_mode", set()):
                counts["telegram_dm_topic_mode"] = conn.execute(
                    "DELETE FROM telegram_dm_topic_mode WHERE profile_name = ?", (name,)).rowcount
            binding_columns = topic_columns.get("telegram_dm_topic_bindings", set())
            if "session_key" in binding_columns:
                # A rename rewrites a binding's session_key namespace as well as its profile_name
                # (:meth:`rekey_profile_state`), so matching on one alone leaves the other behind.
                # Legacy v1/v2 bindings have no profile_name but keep the ``agent:<name>:`` namespace
                # in session_key, so the namespace match alone is the exact cleanup there.
                by_profile = "profile_name = ? OR " if "profile_name" in binding_columns else ""
                params = (name, ns_len, ns) if by_profile else (ns_len, ns)
                counts["telegram_dm_topic_bindings"] = conn.execute(
                    "DELETE FROM telegram_dm_topic_bindings "
                    f"WHERE {by_profile}substr(session_key, 1, ?) = ?", params).rowcount

        self._execute_write(_do)
        return counts

    @staticmethod
    def session_gateway_runtime(session_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Read the persisted runtime route off a session row dict (``model_config`` as
        JSON string or parsed dict). Precedence: nested ``gateway_runtime`` (gateway sync /
        CLI ``/model``), then top-level ``provider``/``base_url``/``api_mode`` (TUI), then
        ``billing_provider`` so sessions that never ran ``/model`` still restore the
        provider that served them. Empty dict on parse failure — resume uses ambient config."""
        from hermes_state import _BARE_BILLING_PROVIDERS
        raw = (session_meta or {}).get("model_config")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        runtime = raw.get("gateway_runtime")
        # Filter None: the persist path writes or-None to trigger deletion in the top-level
        # merge, but gateway_runtime is replaced whole (not deep-merged), so None survives here.
        if isinstance(runtime, dict) and runtime.get("provider"):
            return {k: v for k, v in runtime.items() if v is not None}
        top_level = {key: raw.get(key) for key in ("provider", "base_url", "api_mode") if raw.get(key)}
        if top_level:
            return top_level
        # billing_provider is COALESCE-written on the first accounted API call — the only durable
        # record for sessions that never ran /model. Bare buckets ("auto"/"custom") are not
        # routable identities; filter them so resume falls back to the ambient default.
        billing_provider = str((session_meta or {}).get("billing_provider") or "").strip()
        if billing_provider and billing_provider.lower() not in _BARE_BILLING_PROVIDERS:
            return {"provider": billing_provider}
        if not isinstance(runtime, dict):
            return {}
        return {k: v for k, v in runtime.items() if v is not None}

    def has_pending_handoffs(self) -> bool:
        """Bounded existence probe for the handoff watcher's idle gate (the gate fails open on error)."""
        return self._read_one("SELECT 1 FROM sessions WHERE handoff_state = 'pending' LIMIT 1") is not None

