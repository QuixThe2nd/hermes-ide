"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes three checkpoints around the send:

    record_obligation()   state='pending'     before any send attempt
    mark_attempting()     state='attempting'  immediately before the await
    mark_delivered() /    state='delivered'   only on SendResult.success
    mark_failed()         state='failed'      on a definitive rejection

On startup, ``sweep_recoverable()`` claims rows whose owning process is
dead and hands them to the gateway for redelivery. After a platform adapter
reconnects without a process restart, ``sweep_failed_for_runtime()`` may claim
only the same live process's explicitly allowlisted transient failures. Crash
semantics are explicit about ambiguity (the contract review of the earlier
delivery-outbox attempt, #61790, closed it for silently resending ambiguous
sends):

- ``pending``     — the send never started: redeliver plainly, no dup risk.
- ``attempting``  — crashed mid-await: the platform MAY already have the
  message. Redelivered WITH a visible recovered-reply marker so the
  contract is honest at-least-once, never a silent duplicate.
- ``failed``      — definitively rejected once; the restart is a natural
  retry boundary. Also carries the marker.
- ``delivered``   — nothing to do; retention prunes.

Poison rows cannot spin: attempts are capped, stale rows expire, and both
transition to ``abandoned`` (kept briefly for inspection, then pruned).

The same table also carries important non-final notices
(``kind='notice'``): user-facing status/warning notices the status rail
FAILED to deliver transiently (adapter marked the send degraded/retryable).
Unlike final responses, notices are recorded only AFTER a transient
failure — a delivered notice never enters the ledger, so nothing that was
seen can replay. ``record_notice()`` writes them straight to ``failed``
with the retryable marker so both reconnect recovery
(``sweep_failed_notices_for_runtime``) and crash recovery
(``sweep_recoverable``) can claim them. Notice replay never touches
resume/active-turn state and routes to the exact persisted
platform/chat/thread. On bubble-editing rails (adapters implementing
``send_or_update_status``) a rail shows one current state, so a newer
undelivered notice supersedes older undelivered ones on the same rail
(state ``superseded``) — stale "running"-era text cannot reappear after a
newer state exists.

Everything here is best-effort by design: ledger failures must never block
or delay an actual send. Callers wrap every call in try/except.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# Redelivery policy knobs (module constants; deliberately not config — the
# ledger itself is gated by ``gateway.delivery_ledger`` and these bounds
# only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500

# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)

# Runtime recovery uses a distinct marker because no gateway restart occurred.
# Keep the ambiguity explicit: a network rejection normally means the platform
# did not accept the message, but an acknowledgement can be lost independently.
RECONNECTED_MARKER = (
    "♻️ Recovered reply — the messaging platform reconnected after the original "
    "delivery failed, so this may be a duplicate:\n\n"
)

# Notice-row kinship: final answers and notices share the table but never the
# identity space, the markers, or the resume semantics.
FINAL_KIND = "final"
NOTICE_KIND = "notice"

# Visible markers for replayed notices (same honest at-least-once contract as
# final responses, worded for a status notice rather than a reply).
RECOVERED_NOTICE_MARKER = (
    "♻️ Recovered notice — the gateway restarted before this status could be "
    "delivered, so this may be a duplicate:\n\n"
)
RECONNECTED_NOTICE_MARKER = (
    "♻️ Recovered notice — the messaging platform reconnected after the "
    "original delivery failed, so this may be a duplicate:\n\n"
)

# Runtime replay is deliberately fail-closed. Only errors whose send contract
# proves they are transient reconnect failures belong here; permanent rejects
# (blocked bot, bad auth, missing chat) must not be retried merely because an
# adapter reconnected.
_RUNTIME_RETRYABLE_ERRORS = frozenset({"send_path_degraded"})


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (delivery_ledger)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            adapter_profile TEXT,
            kind TEXT NOT NULL DEFAULT 'final',
            status_key TEXT,
            send_metadata TEXT
        )"""
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
    }
    if "adapter_profile" not in columns:
        try:
            conn.execute(
                "ALTER TABLE delivery_obligations ADD COLUMN adapter_profile TEXT"
            )
        except sqlite3.OperationalError as exc:
            # Concurrent first-use connections can both observe the old schema.
            if "duplicate column" not in str(exc).lower():
                raise
    # kind/status_key/send_metadata carry the notice rows added for reconnect
    # notice recovery. Defaults keep pre-migration final-response rows valid.
    for column, ddl in (
        ("kind", "ALTER TABLE delivery_obligations ADD COLUMN kind TEXT NOT NULL DEFAULT 'final'"),
        ("status_key", "ALTER TABLE delivery_obligations ADD COLUMN status_key TEXT"),
        ("send_metadata", "ALTER TABLE delivery_obligations ADD COLUMN send_metadata TEXT"),
    ):
        if column not in columns:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every call, deferring the close to the garbage collector. On a long-running
    gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of this
    bug was #69567 / PR #69594). ``record_obligation`` runs on every outbound
    final response, so this ledger is the highest-frequency leaker.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from gateway.status import get_process_start_time

        current_start = get_process_start_time(pid)
    except Exception:
        current_start = None
    if current_start is None:
        # No such process (or unreadable) — treat unreadable-but-extant
        # processes as alive only if the pid exists. Route through the
        # cross-platform probe: ``os.kill(pid, 0)`` on Windows is NOT a
        # no-op (bpo-14484 — CPython maps sig=0 to
        # ``GenerateConsoleCtrlEvent(0, pid)``), so a raw probe here could
        # Ctrl+C the gateway's own console group whenever psutil failed to
        # read the start time of a live pid. ``_pid_exists`` keeps the
        # EPERM-means-alive semantics (exists but owned by another user).
        try:
            from gateway.status import _pid_exists
        except Exception:
            if os.name == "nt":
                # Never fall back to a raw sig-0 probe on Windows.
                return False
            try:
                os.kill(pid, 0)  # windows-footgun: ok — POSIX-only fallback branch
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            return True
        try:
            return bool(_pid_exists(pid))
        except Exception:
            return False
    if started_at is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while
    distinct threads/topics on the same chat can never collide (the
    session_key carries platform, chat and thread; ``message_ref`` is the
    triggering inbound message id, distinguishing turns in one session)."""
    payload = f"{session_key}|{message_ref}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def compute_notice_id(session_key: str, status_key: str, content: str) -> str:
    """Stable id for notice rows, in a SEPARATE namespace from final answers.

    The ``notice|`` prefix guarantees a notice id can never equal a
    ``compute_obligation_id`` hash of the same text in the same session, and
    the session_key in the payload keeps distinct sessions/profiles from
    colliding on one shared chat surface.
    """
    payload = f"notice|{session_key}|{status_key}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    adapter_profile: Optional[str] = None,
) -> None:
    """Record a final response as owed to the platform (state='pending')."""
    now = time.time()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, adapter_profile)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (obligation_id, session_key, platform, str(chat_id),
             str(thread_id) if thread_id else None, content, now, now,
             pid, started, stored_profile),
        )
    _prune()


def record_notice(
    *,
    notice_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    status_key: str,
    error: str = "",
    send_metadata: Optional[Dict[str, Any]] = None,
    adapter_profile: Optional[str] = None,
    mutable_rail: bool = False,
) -> None:
    """Record an important notice whose delivery just failed transiently.

    Written ONLY after a transient transport failure (caller-classified:
    ``send_path_degraded`` or ``retryable``), directly to ``failed`` with the
    normalized retryable error so the reconnect and crash sweeps can claim
    it. A notice that was delivered never enters the ledger, so nothing the
    user already saw can replay.

    ``status_key`` is the status rail (the event type the status producer
    keyed the send on); ``send_metadata`` preserves the exact routing
    metadata (thread id, reply anchor) as JSON for replay.

    ``mutable_rail=True`` marks an adapter that renders the rail as ONE
    editable bubble (``send_or_update_status``): only the newest undelivered
    state on that rail is worth replaying, so older undelivered rows on the
    same (platform, profile, chat, thread, status_key) rail transition to
    ``superseded`` — a stale "running"-era line cannot reappear after a
    newer state was emitted. Append-only rails (Discord and every adapter
    without the edit capability) keep each distinct notice; the shared
    attempts cap, stale cutoff, retention, and row bound apply either way.
    """
    now = time.time()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    pid, started = _owner_stamp()
    metadata_json = (
        json.dumps(send_metadata, ensure_ascii=False) if send_metadata else None
    )
    with _DB_LOCK, _transaction() as conn:
        if mutable_rail:
            conn.execute(
                """UPDATE delivery_obligations
                   SET state='superseded', updated_at=?
                   WHERE kind='notice' AND platform=? AND chat_id=?
                     AND status_key=? AND state IN ('pending', 'failed')
                     AND thread_id IS ? AND adapter_profile=?""",
                (now, platform, str(chat_id), str(status_key or ""),
                 str(thread_id) if thread_id else None, stored_profile),
            )
        conn.execute(
            """INSERT OR REPLACE INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, adapter_profile,
                kind, status_key, send_metadata, last_error)
               VALUES (?, ?, ?, ?, ?, ?, 'failed', 0, ?, ?, ?, ?, ?,
                       'notice', ?, ?, ?)""",
            (notice_id, session_key, platform, str(chat_id),
             str(thread_id) if thread_id else None, content, now, now,
             pid, started, stored_profile,
             str(status_key or ""), metadata_json,
             (error or "send_path_degraded")[:500]),
        )
    _prune()


def mark_attempting(obligation_id: str) -> None:
    _update_state(obligation_id, "attempting")


def mark_delivered(obligation_id: str) -> None:
    _update_state(obligation_id, "delivered")


def mark_failed(obligation_id: str, error: str = "") -> None:
    _update_state(obligation_id, "failed", error=error)


def release_runtime_claim(obligation_id: str, error: str = "") -> bool:
    """Return an unsent runtime claim to ``failed`` without spending an attempt.

    Runtime recovery claims before clearing ``resume_pending`` so that two
    reconnect paths cannot send the same row. If the session flag cannot be
    cleared, no platform send was attempted and the claim must not consume the
    bounded redelivery budget. Release is fail-closed to the exact current
    process instance and the ``attempting`` state.
    """
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', attempts=CASE
                       WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=?, last_error=?
               WHERE obligation_id=? AND state='attempting'
                 AND owner_pid IS ? AND owner_started_at IS ?""",
            (time.time(), error[:500] if error else None,
             obligation_id, pid, started),
        )
    return bool(cursor.rowcount)


def _update_state(obligation_id: str, state: str, error: str = "") -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=?""",
            (state, time.time(), error[:500] if error else None, obligation_id),
        )


def sweep_recoverable(
    now: Optional[float] = None,
    *,
    deliverable_platforms: Optional[set] = None,
    deliverable_targets: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Claim undelivered rows owned by dead processes; return them for
    redelivery.

    Claiming atomically re-stamps the owner to THIS process and increments
    ``attempts``, so a second gateway racing the same sweep cannot
    double-claim (the UPDATE is guarded on the previous owner stamp).
    Rows over the attempts cap or older than the stale cutoff transition to
    'abandoned' instead of being returned.

    ``deliverable_platforms`` (platform value strings) restricts claiming to
    platforms the caller can actually send on this boot.  ``attempts`` is the
    redelivery budget, so it must only be spent on a real send: a platform
    that failed to connect would otherwise burn one attempt per boot and hit
    the cap having never been sent once.  Rows for absent platforms are left
    untouched for a later boot; the stale cutoff still bounds them.

    ``deliverable_targets`` further scopes multiplexed gateways by exact
    ``(platform, adapter_profile)`` identity, preventing one connected bot from
    spending another disconnected bot's retry budget.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, created_at,
                      owner_pid, owner_started_at, adapter_profile,
                      kind, status_key, send_metadata
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')"""
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state,
             attempts, created_at, owner_pid, owner_started_at,
             adapter_profile, kind, status_key, send_metadata) in rows:
            if _owner_alive(owner_pid, owner_started_at):
                continue  # a live gateway still owns this row
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=? WHERE obligation_id=?""",
                    (now, oid),
                )
                continue
            if (
                deliverable_platforms is not None
                and platform not in deliverable_platforms
            ):
                # No adapter for this platform this boot — the caller cannot
                # send, so claiming would spend an attempt on a no-op.
                continue
            if (
                deliverable_targets is not None
                and (platform, adapter_profile) not in deliverable_targets
            ):
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+1,
                       updated_at=?
                   WHERE obligation_id=? AND (owner_pid IS ? OR owner_pid=?)""",
                (pid, started, now, oid, owner_pid, owner_pid),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    # pending = send never started, redeliver plainly;
                    # attempting/failed = ambiguous or rejected, carry marker.
                    "needs_marker": state != "pending",
                    "profile": adapter_profile,
                    "attempts": attempts + 1,
                    "kind": kind or FINAL_KIND,
                    "status_key": status_key,
                    "send_metadata": send_metadata,
                })
    return claimed


def sweep_failed_for_runtime(
    platform: str,
    now: Optional[float] = None,
    *,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Claim this process's reconnect-retryable failed FINAL rows for one adapter.

    Notice rows (``kind='notice'``) are deliberately out of scope here —
    :func:`sweep_failed_notices_for_runtime` owns them, so a replayed notice
    can never ride the final-answer path (wrong marker, and the caller's
    resume-clearing treats final claims as turns that gated a session).

    ``profile`` scopes multiplexed gateways to the bot identity that actually
    owned the failed send; ``None`` means the primary/default adapter. The
    persisted adapter owner is independent of the routed session namespace.

    Startup recovery intentionally ignores rows owned by a live gateway. That
    protects concurrent processes, but it also means a final response rejected
    with ``send_path_degraded`` remains stranded when only the platform adapter
    reconnects. This runtime sweep closes that gap without weakening ownership:

    - only rows stamped to this exact process instance are eligible;
    - only explicitly allowlisted transient errors are eligible;
    - attempts/staleness bounds match startup recovery;
    - every update is guarded by the prior owner stamp and ``failed`` state.

    Unowned rows and rows owned by another process are left untouched for the
    normal startup/dead-owner sweep. Claimed rows always carry the reconnect
    marker because the failed send's acknowledgement is not safe to infer.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    if started is None:
        # PID equality alone cannot distinguish this process from a stale row
        # left by an earlier process incarnation after PID reuse. Runtime replay
        # is optional recovery, so fail closed when the process fingerprint is
        # unavailable; startup recovery remains the durable fallback.
        return []
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, attempts, created_at, owner_pid,
                      owner_started_at, last_error, adapter_profile
               FROM delivery_obligations
               WHERE state='failed' AND platform=? AND kind='final'""",
            (platform,),
        ).fetchall()
        for (
            oid,
            session_key,
            row_platform,
            chat_id,
            thread_id,
            content,
            attempts,
            created_at,
            owner_pid,
            owner_started_at,
            last_error,
            adapter_profile,
        ) in rows:
            expected_profile = (
                "default" if not profile or profile == "default" else str(profile)
            )
            if adapter_profile != expected_profile:
                continue
            # Runtime reconnect recovery may act only on its own rows. Exact
            # process-start matching prevents PID reuse from stealing work.
            if owner_pid != pid or owner_started_at != started:
                continue
            if str(last_error or "").strip().lower() not in _RUNTIME_RETRYABLE_ERRORS:
                continue
            owner_guard = (oid, owner_pid, owner_started_at)
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?
                       WHERE obligation_id=? AND state='failed'
                         AND owner_pid IS ? AND owner_started_at IS ?""",
                    (now, *owner_guard),
                )
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', attempts=attempts+1, updated_at=?
                   WHERE obligation_id=? AND state='failed'
                     AND owner_pid IS ? AND owner_started_at IS ?""",
                (now, *owner_guard),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": row_platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    "needs_marker": True,
                    "marker": RECONNECTED_MARKER,
                    "profile": adapter_profile,
                    "runtime_recovery": True,
                    "attempts": attempts + 1,
                })
    return claimed


def sweep_failed_notices_for_runtime(
    platform: str,
    now: Optional[float] = None,
    *,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Claim this process's reconnect-retryable failed NOTICE rows for one
    adapter.

    Same ownership contract as :func:`sweep_failed_for_runtime` — exact
    process-instance owner, allowlisted transient error only, attempts/stale
    bounds, atomic claiming — but scoped to ``kind='notice'`` rows, which the
    status rail recorded after a transient delivery failure. Callers replay
    them WITHOUT touching resume/active-turn state: a notice never gated a
    turn, so its recovery must not either.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    if started is None:
        # Same fail-closed rule as runtime final-answer recovery: without the
        # process fingerprint, PID reuse could steal another incarnation's
        # rows. Startup recovery remains the durable fallback.
        return []
    expected_profile = "default" if not profile or profile == "default" else str(profile)
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, attempts, created_at, owner_pid,
                      owner_started_at, last_error, adapter_profile,
                      status_key, send_metadata
               FROM delivery_obligations
               WHERE state='failed' AND platform=? AND kind='notice'""",
            (platform,),
        ).fetchall()
        for (
            oid,
            session_key,
            row_platform,
            chat_id,
            thread_id,
            content,
            attempts,
            created_at,
            owner_pid,
            owner_started_at,
            last_error,
            adapter_profile,
            status_key,
            send_metadata,
        ) in rows:
            if adapter_profile != expected_profile:
                continue
            if owner_pid != pid or owner_started_at != started:
                continue
            if str(last_error or "").strip().lower() not in _RUNTIME_RETRYABLE_ERRORS:
                continue
            owner_guard = (oid, owner_pid, owner_started_at)
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?
                       WHERE obligation_id=? AND state='failed'
                         AND owner_pid IS ? AND owner_started_at IS ?""",
                    (now, *owner_guard),
                )
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', attempts=attempts+1, updated_at=?
                   WHERE obligation_id=? AND state='failed'
                     AND owner_pid IS ? AND owner_started_at IS ?""",
                (now, *owner_guard),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": row_platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    "needs_marker": True,
                    "marker": RECONNECTED_NOTICE_MARKER,
                    "profile": adapter_profile,
                    "runtime_recovery": True,
                    "attempts": attempts + 1,
                    "kind": NOTICE_KIND,
                    "status_key": status_key,
                    "send_metadata": send_metadata,
                })
    return claimed


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE state IN ('delivered', 'abandoned', 'superseded')
                     AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations"
            ).fetchone()[0]
            excess = max(0, total - _MAX_ROWS)
            if excess:
                conn.execute(
                    """DELETE FROM delivery_obligations WHERE obligation_id IN (
                         SELECT obligation_id FROM delivery_obligations
                         ORDER BY CASE state
                                    WHEN 'delivered' THEN 0
                                    WHEN 'abandoned' THEN 1
                                    WHEN 'superseded' THEN 1
                                    ELSE 2
                                  END, updated_at ASC
                         LIMIT ?)""",
                    (excess,),
                )
    except Exception:
        logger.debug("delivery ledger prune failed", exc_info=True)


def ledger_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        gw = config.get("gateway") or {}
        value = gw.get("delivery_ledger", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)
    except Exception:
        return True


def debug_rows(limit: int = 20) -> str:
    """Human-readable dump for ad-hoc inspection (sqlite3-free path)."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, state, attempts,
                      created_at, updated_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return json.dumps(
        [
            {
                "id": r[0], "session": r[1], "state": r[2], "attempts": r[3],
                "created_at": r[4], "updated_at": r[5], "last_error": r[6],
            }
            for r in rows
        ],
        indent=2,
    )
