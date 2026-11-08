"""FTS5 index setup for SessionDB: the CJK-bigram (cjk_unicode61) index DDL and
tokenizer loader, per-table schema ensure with tokenizer-capability fallback,
FTS-scoped corruption detection and the atomic fail-open trigger detach."""

import logging
import os
import sqlite3
from pathlib import Path
from typing import Sequence

from hermes_constants import get_hermes_home
from hermes_state_common import (FTS_CJK_STALE_KEY, FTS_STALE_KEY, _FTS_CJK_TRIGGERS, _FTS_TRIGGERS,
    routed_sessions_setting)
from hermes_state_errors import is_fts_scoped_corruption_error

# caplog tests pin the "hermes_state" logger name.
logger = logging.getLogger("hermes_state")

# ── CJK-bigram FTS index (replaces the trigram index when available) ────
# Trigram needs >=3 chars per term, so 1-2 char CJK terms fell through to a LIKE
# table scan; ``cjk_unicode61`` (native/fts5_cjk/, loadable) re-emits CJK runs as
# overlapping bigrams. Same v23 discipline as the trigram table: external-content
# over a tool-row-excluding view, triggers gated on a DEDICATED marker pair
# (fts_cjk_rebuild_high_water / _progress). The table exists ONLY when the
# tokenizer loads; a process that cannot load it drops the cjk triggers (writes
# keep working; the index goes stale until the next optimize-storage).
#
# Split DDL: the table/view is safe to ensure any time; triggers are created ONLY
# while the index is complete-or-marker-gated. A stale index must keep its
# triggers DROPPED — an external-content 'delete' for a rowid the index never
# held is the canonical FTS5 corruption hazard.
# The trigram tokenizer needs >=3 chars per query term, so 1-2 char CJK terms (ubiquitous in Korean/Chinese:
# 일본, 구글, 项目, ...) fall through to a LIKE full-table scan — measured 3-6s CPU per query on multi-GB installs
# and the dominant base cost of session_search on CJK workloads. ``cjk_unicode61`` (native/fts5_cjk/, a
# ~250-line loadable FTS5 tokenizer with no dependencies) wraps unicode61: maximal CJK runs are re-emitted
# as overlapping character bigrams (Lucene CJKAnalyzer semantics), everything else passes through unchanged.
# FTS5 phrase semantics turn a query term's consecutive bigrams into exact substring matching down to 2
# chars at index speed. Contributed by Soju06 (PR #65544).
FTS_CJK_TABLE_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_cjk_src AS
    SELECT id, role, content, tool_name, tool_calls
    FROM messages
    WHERE role <> 'tool';

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_cjk_src',
    content_rowid='id',
    tokenize='cjk_unicode61'
);
"""

FTS_CJK_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_cjk_update
AFTER UPDATE OF content, tool_name, tool_calls, role ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_cjk_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_cjk(messages_fts_cjk, rowid, content, tool_name, tool_calls)
    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
    WHERE old.role <> 'tool';
    INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
    SELECT new.id, new.content, new.tool_name, new.tool_calls
    WHERE new.role <> 'tool';
END;
"""


def fts5_cjk_so_path() -> Path:
    """Location of the cjk_unicode61 loadable extension."""
    env = os.getenv("HERMES_FTS5_CJK_SO")
    return Path(env).expanduser() if env else get_hermes_home() / "lib" / "libfts5_cjk.so"


def _cjk_fts_config_enabled() -> bool:
    """config.yaml ``sessions.cjk_fts`` (default on) for the profile being served."""
    value = routed_sessions_setting("cjk_fts", "HERMES_CJK_FTS")
    return value is None or str(value).strip().lower() not in ("0", "false", "off", "no")


def load_fts5_cjk_extension(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the cjk_unicode61 tokenizer; False (never raises) when
    the .so is absent, ``sessions.cjk_fts`` is off, or loading is compiled out."""
    path = fts5_cjk_so_path()
    if not _cjk_fts_config_enabled() or not path.exists():
        return False
    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(str(path))
        finally:
            conn.enable_load_extension(False)
        return True
    except Exception:
        logger.warning("fts5_cjk extension load failed (%s)", path, exc_info=True)
        return False


# FTS5 shadow tables the virtual-table engine owns. `sqlite3 .recover` re-emits them
# as ordinary tables but cannot re-emit the CREATE VIRTUAL TABLE row, so every later
# CREATE VIRTUAL TABLE fails with "fts5: error creating shadow table <name>: table
# already exists" until the orphans are dropped (#103840).
_FTS5_SHADOW_SUFFIXES = ("content", "data", "docsize", "idx", "config")


def _drop_orphan_fts_shadow_tables(cursor: sqlite3.Cursor, families: Sequence[str]) -> list[str]:
    """Drop, per family, shadow tables whose virtual table row is absent from sqlite_master.

    Matches exact shadow names only (never a prefix LIKE, so the base family cannot reach
    ``messages_fts_trigram_*``) and leaves a family alone whenever its vtable is live. The
    shadows are derived index state; the caller recreates and rebuilds from ``messages``.
    Returns the families that were repaired.
    """
    repaired: list[str] = []
    for family in families:
        vtable_live = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%'",
            (family,),
        ).fetchone()
        if vtable_live:
            continue
        shadows = [f"{family}_{suffix}" for suffix in _FTS5_SHADOW_SUFFIXES]
        orphans = [row[0] for row in cursor.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({','.join('?' for _ in shadows)})",
            shadows,
        ).fetchall()]
        if not orphans:
            continue
        for name in orphans:
            cursor.execute(f'DROP TABLE "{name}"')
        logger.warning(
            "Dropped orphan FTS5 shadow tables of %s (%s); the index is recreated from messages",
            family, ", ".join(orphans),
        )
        repaired.append(family)
    return repaired


class SessionFtsSetupMixin:
    """FTS table/trigger lifecycle shared by schema init, optimize and the write path."""

    @staticmethod
    def _is_fts5_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        """No FTS5 module, or an optional tokenizer missing (same capability-error shape)."""
        err = str(exc).lower()
        return ("no such module" in err and "fts5" in err) or SessionFtsSetupMixin._is_trigram_unavailable_error(exc)

    @staticmethod
    def _is_trigram_unavailable_error(exc: sqlite3.OperationalError) -> bool:
        """Only an optional tokenizer is missing (trigram needs SQLite >= 3.34;
        cjk_unicode61 is loadable): "this one index can't be served", never "disable FTS"."""
        err = str(exc).lower()
        return "no such tokenizer: trigram" in err or "no such tokenizer: cjk_unicode61" in err

    @staticmethod
    def _db_has_legacy_inline_fts(cursor: sqlite3.Cursor) -> bool:
        """messages_fts exists in ANY pre-v23 shape: every legacy shape lacks
        tool_name, so "stored CREATE lacks tool_name" catches them all. False when absent.

        v23's messages_fts is external-content over THREE real columns (content, tool_name, tool_calls).
        Every pre-v23 shape lacks the tool_name/tool_calls columns — whether the old inline single-column
        form (v11..v22) or the even older external-content single-column form (v10-era, pre-#16751). We
        therefore detect "needs optimize" as "the stored CREATE lacks the tool_name column", which is the
        precise v23 marker and correctly catches BOTH legacy variants.
        """
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
        ).fetchone()
        return row is not None and "tool_name" not in (row[0] or "")

    @staticmethod
    def _db_has_trigram_tool_calls_projection(cursor: sqlite3.Cursor) -> bool:
        """True when the trigram vtable still includes the tool_calls payload (FTS_STORAGE_VERSION 1)."""
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts_trigram'"
        ).fetchone()
        return row is not None and "tool_calls" in (row[0] or "").lower()

    @classmethod
    def _db_needs_fts_storage_upgrade(cls, cursor: sqlite3.Cursor) -> bool:
        """True when the current FTS storage layout should be treated as stale (optimize-storage has work)."""
        return cls._db_has_legacy_inline_fts(cursor) or cls._db_has_trigram_tool_calls_projection(cursor)

    @staticmethod
    def _drop_fts_triggers(cursor: sqlite3.Cursor) -> None:
        for trigger in _FTS_TRIGGERS:
            try:
                cursor.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _is_fts_write_corruption_error(exc: sqlite3.DatabaseError) -> bool:
        """Corruption SQLite identifies as FTS-scoped (SQLITE_CORRUPT_VTAB, or an ``fts5:``
        report naming ``messages_fts*`` on builds without result codes); a bare malformed
        image is structural. One rule, shared with ``classify_persistence_error`` and the
        gateway transcript retry: see :func:`hermes_state_errors.is_fts_scoped_corruption_error`."""
        return is_fts_scoped_corruption_error(exc)

    # ── Chunked FTS rebuild engine (v23 opt-in optimize) ──
    # One blocking rebuild held the write lock ~16 min on a 25 GB DB, so the
    # backfill runs in small chunks (each its own short transaction, resumable from
    # fts_rebuild_progress, claimed by CAS). A greedy loop starved other writers:
    # a pause of max(MIN_PAUSE, chunk cost x DUTY_FACTOR) caps the duty cycle
    # cross-process, unlike any same-process activity stamp.
    _FTS_REBUILD_CHUNK_ROWS = 500
    _FTS_REBUILD_DUTY_FACTOR = 4.0      # sleep >= 4x chunk cost (≤20% duty)
    _FTS_REBUILD_MIN_PAUSE = 0.2        # seconds — floor between chunks

    # Demoted v22 FTS shadow tables awaiting teardown: DROP of a multi-GB vtable
    # blocks for minutes, so the v23 migration renames the orphaned shadow tables
    # to fts_v22_trash_*; the worker empties them in chunks, then drops.
    _FTS_TRASH_PREFIX = "fts_v22_trash_"

    # FTS5 tables merged on optimize; each is probed before touching (trigram may
    # be disabled, cjk exists only with the loadable tokenizer).
    _FTS_TABLES = ("messages_fts", "messages_fts_trigram", "messages_fts_cjk")
