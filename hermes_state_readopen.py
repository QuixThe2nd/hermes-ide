"""Robust SessionDB opens at an explicit path with schema heal.

Moved from the removed dashboard's ``hermes_cli/web_server_sessions.py``; the
TUI gateway's cross-profile session reader is the surviving consumer.
"""

import logging
import sqlite3
import threading
from pathlib import Path

_log = logging.getLogger("hermes_state")

# Serialises the one-time writable schema bootstrap for read-only opens, so
# concurrent first-load polls don't open mode=ro against a half-written schema
# ("no such table: sessions").
_session_db_bootstrap_lock = threading.Lock()


def _session_db_read_probe_statements() -> tuple:
    """Stale-schema probes for read-only opens (which skip _reconcile_columns()).
    Derived from SCHEMA_SQL so a new column is probed automatically — a
    hand-written list once went stale and emptied the sidebar after update."""
    from hermes_state_schema import schema_read_probe_statements

    return schema_read_probe_statements()


# Stores where a heal WRITABLE OPEN SUCCEEDED but the read probe still failed:
# one reconciliation cannot fix them (e.g. a NOT-NULL-without-default column),
# so they fall back to the raw read-only open until restart instead of paying
# a writable init per poll. A FAILED writable open (transient lock) is NOT
# recorded — the next poll retries the heal.
_session_db_heal_exhausted: set = set()

# Deduplicates the heal-failure warning per store per process.
_session_db_heal_warned: set = set()


def _is_stale_schema_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "no such table" in message or "no such column" in message


def _open_session_db_at_path(db_path: Path, *, read_only: bool):
    """Open a SessionDB at an explicit path with an explicit access mode.

    Read-only opens bootstrap a missing/zero-byte store once and heal a stale or
    malformed schema through ONE writable open before reopening read-only; the
    healthy read path never takes a write lock.  Tables outside SCHEMA_SQL
    (telemetry ``tel_*``, FTS shadow tables) are outside both probe and heal.
    """
    from hermes_state import SessionDB, is_malformed_schema_error
    from hermes_state_registry import acquire, release_or_close

    # Read-only file/sidecar preflight (port of kilocode#12508): repair-or-refuse BEFORE the first
    # connection so users get an actionable message instead of an opaque "attempt to write a readonly
    # database" from deep inside _init_schema.
    if not read_only:
        return acquire(db_path)

    def _needs_bootstrap() -> bool:
        try:
            return db_path.stat().st_size == 0
        except FileNotFoundError:
            return True
        except OSError:
            return False

    if _needs_bootstrap():
        with _session_db_bootstrap_lock:
            if _needs_bootstrap():
                db = acquire(db_path)
                release_or_close(db)

    def _open_probed():
        db = SessionDB(db_path=db_path, read_only=True)
        # Unit-test fakes may replace SessionDB without exposing a raw
        # connection. Probe only real connections.
        conn = getattr(db, "_conn", None)
        if conn is not None and str(db_path) not in _session_db_heal_exhausted:
            try:
                for statement in _session_db_read_probe_statements():
                    conn.execute(statement).fetchone()
            except BaseException:
                db.close()
                raise
        return db

    try:
        return _open_probed()
    except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError = pysqlite could not decode SQLite's own error
        # message because corrupt file bytes were embedded in it; the
        # one-writable-open heal is the only repair path, so treat it as
        # malformed schema.
        if not (
            _is_stale_schema_error(exc)
            or is_malformed_schema_error(exc)
            or isinstance(exc, UnicodeDecodeError)):
            raise
        db = acquire(db_path)
        release_or_close(db)
        try:
            return _open_probed()
        except (sqlite3.DatabaseError, UnicodeDecodeError) as still_stale:
            if not _is_stale_schema_error(still_stale):
                raise
            # Writable open succeeded but the store is STILL behind the probe:
            # serve reads without the probe (only queries touching the broken
            # part fail) and stop paying the writable init per poll.
            _session_db_heal_exhausted.add(str(db_path))
            if str(db_path) not in _session_db_heal_warned:
                _session_db_heal_warned.add(str(db_path))
                _log.warning(
                    "state.db at %s is missing schema that a writable "
                    "reconcile could not add (%s); read paths may partially "
                    "fail until the store is repaired",
                    db_path,
                    still_stale)
            return _open_probed()
