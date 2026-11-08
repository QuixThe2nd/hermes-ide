"""Telegram DM topic-mode mixin for :class:`hermes_state.SessionDB`."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

from hermes_state_common import _PREVIEW_ELIGIBLE_SQL, _PREVIEW_RAW_SELECT, _sql_session_last_active
from hermes_state_errors import StateDbReplacedError

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")


def _normalize_telegram_topic_profile_name(profile_name: Optional[str] = None) -> str:
    """Empty/missing → ``"default"`` (single namespace for non-multiplexed gateways).
    Multiplexed callers must pass the *routed* profile (``source.profile``), never the
    process-global active profile."""
    name = str(profile_name or "").strip()
    return name if name else "default"


# (table, column list, DDL body). profile_name leads the PK: a private chat_id is the
# user id, identical across bots sharing one state.db.
_TOPIC_TABLES = (
    (
        "telegram_dm_topic_mode",
        "profile_name, chat_id, user_id, enabled, activated_at, updated_at, "
        "has_topics_enabled, allows_users_to_create_topics, capability_checked_at, intro_message_id, pinned_message_id",
        """
                    profile_name TEXT NOT NULL DEFAULT 'default',
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    activated_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    has_topics_enabled INTEGER,
                    allows_users_to_create_topics INTEGER,
                    capability_checked_at REAL,
                    intro_message_id TEXT,
                    pinned_message_id TEXT,
                    PRIMARY KEY (profile_name, chat_id)
                """,
    ),
    (
        "telegram_dm_topic_bindings",
        "profile_name, chat_id, thread_id, user_id, session_key, session_id, managed_mode, linked_at, updated_at",
        """
                    profile_name TEXT NOT NULL DEFAULT 'default',
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    managed_mode TEXT NOT NULL DEFAULT 'auto',
                    linked_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (profile_name, chat_id, thread_id)
                """,
    ),
)

# Shared SELECT for the unlinked-session listing; the profile/bindings clauses are
# spliced in only when the bindings table exists.
_UNLINKED_SELECT_HEAD = f"""
                    SELECT s.*,
                        COALESCE(sp.prompt, s.system_prompt)
                            AS _system_prompt_resolved,
                        COALESCE(
                            (SELECT {_PREVIEW_RAW_SELECT}
                             FROM messages m
                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                               AND {_PREVIEW_ELIGIBLE_SQL}
                             ORDER BY m.timestamp, m.id LIMIT 1),
                            ''
                        ) AS _preview_raw,
                        {_sql_session_last_active("s")} AS last_active
                    FROM sessions s
                    LEFT JOIN system_prompts sp
                      ON sp.hash = s.system_prompt_hash
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
"""
_UNLINKED_SELECT_TAIL = """                    ORDER BY last_active DESC, s.started_at DESC
                    LIMIT ?
                    """
# sessions.profile_name is NULL/empty for legacy rows → treat as default.
_UNLINKED_SCOPE_CLAUSES = """                      AND COALESCE(NULLIF(TRIM(s.profile_name), ''), 'default') = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_dm_topic_bindings b
                          WHERE b.session_id = s.id
                      )
"""


class SessionTelegramTopicsMixin:
    """Telegram DM topic-mode tables, bindings and lookups. Read paths tolerate absent
    tables (nobody ran ``/topic``) by returning their empty value and never create them;
    pre-v3 tables left by an upgrade are self-healed on read."""

    def _topic_read(self, read, empty):
        """Run ``read()``; an absent table reads as ``empty``. A pre-v3 table left by an upgrade
        (#103363) raises ``no such column: profile_name`` — heal it and retry, otherwise the
        write-only migration is never reached and topic mode silently reads as off forever."""
        try:
            return read()
        except sqlite3.OperationalError as exc:
            if "no such column: profile_name" not in str(exc):
                return empty
        try:
            self.apply_telegram_topic_migration()
        except (sqlite3.Error, StateDbReplacedError):
            logger.warning("telegram topic tables are pre-v3 and the heal failed; reading as empty", exc_info=True)
            return empty
        return read()

    def _topic_read_one(self, sql: str, params):
        return self._topic_read(lambda: self._read_one(sql, params), None)

    def _topic_read_all(self, sql: str, params):
        return self._topic_read(lambda: self._read_all(sql, params), [])

