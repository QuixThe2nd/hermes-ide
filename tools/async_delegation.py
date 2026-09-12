#!/usr/bin/env python3
"""Async (background) delegation registry behind ``delegate_task(background=true)``.

Backs ``delegate_agent(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.

Delivery-mode contract (uniform delegation lifecycle)
-----------------------------------------------------
Every delegation tool advertises ``background: bool = false``. The mode is
decided ONLY by that argument — never by nesting, platform, session type, or
delivery capability:

* ``background=false`` (the default) blocks until a terminal outcome and
  returns the final result inline. A foreground executor-owned run never
  touches this registry: no record, no durable row, no completion event.
  (The one exception is the foreground mission wait — an externally-driven
  unit — which registers an inline record plus an ``external`` durable row
  so its cross-process takeover is exactly-once; see
  ``register_inline_wait`` / ``claim_inline_takeover``.)
* ``background=true`` returns the shared acceptance envelope (see
  :func:`build_background_acceptance_envelope`) immediately and later
  delivers exactly ONE terminal result through the completion rail.

Exactly one delivery channel per delegation is enforced structurally at
:func:`publish_terminal_event`: it is the only producer of
``type="async_delegation"`` on the completion queue, and it atomically
reroutes a terminal event into a registered inline waiter instead (used by
the foreground ``delegate_assistant`` mission wait).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# ── Module-level state ──────────────────────────────────────────────────────
# Persistent daemon executor (never a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat async); daemon workers can't hang a hard exit.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict; kept for the run plus a short completed tail.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# Completed records retained (in memory and in the ledger) for status queries.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# Cap retried deliveries so an unroutable row converges to terminal 'dropped'.
_MAX_DELIVERY_ATTEMPTS = 8
# Pending completions older than this are dropped on restart replay instead of
# re-run as a full-context turn; 48h keeps weekend results deliverable.
_MAX_COMPLETION_REPLAY_AGE_S = 48 * 3600.0
_DB_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Uniform delivery modes
# ---------------------------------------------------------------------------
# Every delegation record carries a ``delivery_mode``. The default is the
# event rail (the normal background contract). A foreground waiter that needs
# the terminal result handed back inline registers itself first and the mode
# is CAS'd to ``inline`` under ``_records_lock`` — see
# ``register_inline_wait`` / ``publish_terminal_event``.
DELIVERY_MODE_EVENT = "event"
DELIVERY_MODE_INLINE = "inline"

# What kind of work a delegation wraps. Used by the acceptance envelope
# (``result_kind``) and by the completion formatter so a claude-code run, a
# Cursor cloud run, or an assistant mission does not render with subagent
# phrasing.
RESULT_KIND_SUBAGENT = "subagent_batch"
RESULT_KIND_CLI_AGENT = "cli_agent"
RESULT_KIND_CLOUD_AGENT = "cloud_agent"
RESULT_KIND_MISSION = "mission"

# ---------------------------------------------------------------------------
# Stale-delegation detection (progress-based, on by default)
# ---------------------------------------------------------------------------
# A detached runner that wedges before returning (e.g. stuck inside its first
# model API call — #60203) never reaches its ``finally`` finalizer, so no
# completion event is ever published: the delegation shows "dispatched"
# forever and the owning session looks silent until a process restart. We do
# NOT fix this with a wall-clock timeout — legitimate heavy subagent work
# (deep reviews, research fan-outs, slow reasoning models) must never be
# killed for taking long (see delegate_tool.DEFAULT_CHILD_TIMEOUT rationale).
# Instead a single monitor thread watches per-dispatch PROGRESS (api-call
# count + current tool, via an injected ``progress_fn``): a child that is
# advancing is left alone forever; a child with NO progress past the stale
# threshold is interrupted, given a grace window to unwind and deliver its
# partial results through the normal finalize path, and only force-finalized
# with a terminal ``stalled`` event if it never returns.
#
# Thresholds mirror the sync-path heartbeat staleness monitor in
# delegate_tool: idle (not inside a tool) stays tight so a wedged first API
# call is caught quickly; in-tool is much higher so legitimately slow tools
# (long terminal commands, big fetches) get time to finish.
_STALE_CHECK_INTERVAL = 30.0  # seconds between monitor sweeps
_STALE_IDLE_SECONDS = 450.0  # no progress, no current tool → stalled
_STALE_IN_TOOL_SECONDS = 1200.0  # no progress while inside a tool → stalled
_STALL_GRACE_SECONDS = 120.0  # after interrupt, time for the runner to return

_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()

_LIVE_STATES = {"running", "stalling", "finalizing"}
_ACTIVE_STATES = ("running", "stalling")
# Routing origin persisted at dispatch so a restart-recovered completion can
# reconstruct a full SessionSource (scope_id drives relay tenant egress).
_ROUTING_KEYS = ("scope_id", "user_id", "user_name")
# Structured stall metadata — additive, present only on stall finalizations.
_STALL_META_KEYS = ("stalled_after_quiet_seconds", "stall_threshold_seconds", "stall_phase", "stall_grace_seconds")
# Private stall bookkeeping on the record -> public field in list_async_delegations().
_STALL_FIELD_MAP = (("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
                    ("_stall_threshold_seconds", "stall_threshold_seconds"), ("_stall_in_tool", "stall_in_tool"))


# ── Durable ledger (state.db / async_delegations) ───────────────────────────
def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        conn.close()  # don't leak the connection on PRAGMA/DDL failure
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state_repair import apply_durability_barriers
    # Preserve the journal mode SessionDB configured on state.db: forcing WAL from
    # every short-lived connection collides with live transcript/FTS writers.
    apply_durability_barriers(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT ''
        )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    # origin_session_id: raw api_server session id of the ORIGINATING request
    # (wake target); without it restart-recovered completions are unroutable there.
    for name, sql_type in (("owner_pid", "INTEGER"), ("owner_started_at", "INTEGER"), ("task_json", "TEXT"),
                           ("delivery_claim", "TEXT"), ("delivery_claimed_at", "REAL"), ("origin_session_id", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it (``with conn:``
    alone leaks the connection and WAL/SHM fds until GC).

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the transaction; they do not
    close the connection. Using ``with _connect()`` alone therefore leaks a connection — and its WAL/SHM
    file descriptors — on every durable dispatch, completion, and delivery-claim, deferring the close to the
    garbage collector. On a long-running gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of
    this bug was #69567 / PR #69594).
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _capture_routing_origin() -> Dict[str, Any]:
    """Snapshot scope_id/user_id/user_name on the PARENT thread (the daemon worker
    has no contextvars) so a restart-replayed completion can rebuild a SessionSource.
    Best-effort: empty values are omitted."""
    try:
        from gateway.session_context import get_session_env
        return {k: v for k in _ROUTING_KEYS if (v := get_session_env(f"HERMES_SESSION_{k.upper()}", ""))}
    except Exception:  # noqa: BLE001 - routing origin is additive, never fatal
        return {}


def _persist_dispatch(record: Dict[str, Any], *, replace: bool = True) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal", "goals", "context", "toolsets", "role", "model", "is_batch", "task_indexes",
            "tool", "result_kind", "external",
            # Routing origin (scope_id/user_id/user_name): persisted so a
            # restart-recovered completion can reconstruct a full
            # SessionSource — see _capture_routing_origin.
            "scope_id", "user_id", "user_name",
        )
        if key in record
    }
    with _DB_LOCK, _transaction() as conn:
        # ``replace``=False keeps an already-written row intact: a delegation
        # registered at accept time (``register_background_delegation``)
        # carries routing origin + delivery bookkeeping that a terminal-side
        # re-insert from the closing process must not wipe.
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        conn.execute(
            f"""{verb} INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""), record.get("origin_ui_session_id", ""),
             record.get("parent_session_id"), record["dispatched_at"], now, os.getpid(), owner_started_at,
             json.dumps(task_payload), record.get("origin_session_id", "")))
    _prune_durable_records()


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    cutoff = time.time() - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?", (cutoff,))
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('running','finalizing')").fetchone()[0]
        if terminal_count > _MAX_RETAINED_COMPLETED:
            conn.execute("""DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""", (terminal_count - _MAX_RETAINED_COMPLETED,))
        pending_count = conn.execute("""SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'""").fetchone()[0]
        if pending_count > _MAX_DURABLE_PENDING:
            conn.execute("""DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""", (pending_count - _MAX_DURABLE_PENDING,))


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute("""UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?
                 AND (delivery_state IS NULL OR delivery_state!='delivered')""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]))


def record_unit_child(delegation_id: str, entry: Dict[str, Any]) -> None:
    """Durably record ONE finished child of a still-running multi-child unit on the unit's own row, so a crash before
    the unit joins loses only the children that had not finished. Stored in ``result_json`` (overwritten by the real
    result at finalize); ``recover_abandoned_delegations`` replays it. Best-effort: a failed write costs recovery
    fidelity, never the live result."""
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND state='running'",
                               (delegation_id,)).fetchone()
            if row is None:
                return
            partial = json.loads(row[0] or "{}") or {}
            results = [r for r in partial.get("results") or [] if r.get("task_index") != entry.get("task_index")]
            results.append(entry)
            conn.execute("UPDATE async_delegations SET result_json=?, updated_at=? WHERE delegation_id=? AND state='running'",
                         (json.dumps({"results": results, "partial": True}), time.time(), delegation_id))
    except Exception:  # noqa: BLE001 — recovery bookkeeping must never fail a live child
        logger.warning("Async delegation %s: could not record finished child %s", delegation_id, entry.get("task_index"), exc_info=True)


def _recovered_results(task: Dict[str, Any], result_json: Optional[str], error: str) -> Optional[List[Dict[str, Any]]]:
    """Per-task results for an abandoned unit: recorded children as they finished, the rest ``unknown``."""
    partial = json.loads(result_json or "{}") or {}
    if not (task.get("is_batch") and partial.get("partial") and partial.get("results")):
        return None
    recorded = {r["task_index"]: r for r in partial["results"] if isinstance(r.get("task_index"), int)}
    indexes = task.get("task_indexes") or list(range(len(task.get("goals") or [])))
    return [recorded.get(i) or {"task_index": i, "status": "unknown", "summary": None, "error": error} for i in indexes]


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown; children a multi-child unit had already
    recorded (``record_unit_child``) are replayed with their real results."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now, recovered = time.time(), 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id, result_json
               FROM async_delegations WHERE state IN ('running','finalizing')""").fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json, origin_sid, result_json = row
            task = json.loads(task_json or "{}")
            if task.get("external"):
                # Externally-driven delegation (an assistant mission, see
                # register_background_delegation): the "runner" is a human
                # conversation in whatever process serves that chat, so NO
                # process ever owns the outcome — the accepting process dying
                # says nothing about whether the work finished. The mission
                # store on disk is the source of truth; its own terminalization
                # publishes the real outcome. Recovering it here would fire a
                # spurious "outcome unknown" turn at every gateway restart
                # while the mission is still active.
                continue
            if pid and _pid_exists(int(pid)) and (started is None or get_process_start_time(int(pid)) == int(started)):
                continue
            error = "Delegation owner exited before recording a terminal result; outcome unknown."
            recovered_results = _recovered_results(task, result_json, error)
            if recovered_results:
                done = sum(1 for r in recovered_results if r.get("status") != "unknown")
                error = (f"Delegation owner exited before the unit finished; {done}/{len(recovered_results)} child "
                         "results were recorded and are included below, the rest are unknown.")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id, "session_key": session_key,
                "origin_ui_session_id": origin_ui, "origin_session_id": origin_sid or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""), "goals": task.get("goals"),
                "context": task.get("context"), "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None, "error": error,
                **({"results": recovered_results} if recovered_results else {}),
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            # Tool provenance (see _stamp_event_provenance) so a recovered
            # claude/cursor/mission event still labels itself correctly.
            for _k in ("tool", "result_kind"):
                if task.get(_k):
                    event[_k] = task[_k]
            event["background"] = True
            # Routing origin persisted at dispatch (see _capture_routing_origin):
            # restores scope_id/user_id for the reconstructed SessionSource so
            # relay egress priming works after a restart.
            for _k in ("scope_id", "user_id", "user_name"):
                if task.get(_k):
                    event[_k] = task[_k]
            result = {"status": "unknown", "summary": None, "error": event["error"],
                      **({"results": recovered_results} if recovered_results else {})}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""", (now, now, json.dumps(event), json.dumps(result), delegation_id))
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.
    Restored events are stamped ``restored=True`` in memory only: they came from a PREVIOUS
    process, so drains without an ownership filter must leave them for a consumer that can
    prove ownership. Rows older than ``_MAX_COMPLETION_REPLAY_AGE_S`` are terminally dropped
    instead of replaying a turn nobody is waiting on.

    Every restored event is stamped ``restored=True`` (in-memory only — the stamp is added after the durable
    payload is deserialized and is never persisted). Restored events originate from a *previous* process, so
    no consumer in THIS process implicitly owns them: drain paths that run without an ownership filter (the
    legacy single-session behavior) must leave them queued for a consumer that can positively prove
    ownership, otherwise a brand-new session adopts a dead session's delegation results seconds after boot
    (#64484).
    """
    recover_abandoned_delegations()
    now, restored = time.time(), 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id""").fetchall()
        for delegation_id, payload, completed_at, dispatched_at in rows:
            age_basis = completed_at or dispatched_at
            if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
                conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL,
                              updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
                logger.warning("Async delegation %s: pending completion is %.1fh old "
                               "(cap %.1fh); terminally dropping the replay (result remains queryable).",
                               delegation_id, (now - age_basis) / 3600.0, _MAX_COMPLETION_REPLAY_AGE_S / 3600.0)
                continue
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
            restored += 1
    return restored


def _update_delivery(sql: str, params: tuple) -> bool:
    """Run one UPDATE on the ledger; True iff exactly one row changed."""
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(sql, params).rowcount == 1


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    return _update_delivery(
        """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
           WHERE delegation_id=? AND delivery_state!='delivered'""", (now, now, delegation_id))


def finalize_inline_delivery(delegation_id: str, status: str) -> bool:
    """Retire a durable row whose outcome was delivered INLINE, not on the rail.

    A foreground wait leaves no row of its own, but the terminal side inserts
    one (``ensure_durable_delegation``) BEFORE the chokepoint decides inline vs
    rail, so a claimed inline result strands a ``running``/``pending`` row.
    This flips it to terminal + delivered so it stops being recovery/replay
    bait and becomes prunable. An already-terminal row (a rail publish owns
    it) is left untouched.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=COALESCE(completed_at, ?),
                      updated_at=?, delivery_state='delivered', delivered_at=?
               WHERE delegation_id=? AND state IN ('running','finalizing')""",
            (status, now, now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute("""UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300))
        return cur.rowcount == 1


def claim_inline_takeover(delegation_id: str, status: str = "completed") -> bool:
    """Atomically let an INLINE reader own one terminal delivery.

    Cross-process exactly-once for foreground mission waits: the closing
    process cannot see this process's inline record, so it publishes on its
    own rail — a durable ``pending`` row plus a queue event a consumer must
    claim. A foreground waiter that notices the terminal mission FILE on disk
    may return the outcome inline only if it wins ownership of that delivery
    here; otherwise a later consumer claim or restart replay re-delivers the
    same outcome as a second model turn.

    The decision a rail consumer faces, resolved in one transaction:

    - no row → ``True`` AND a terminal ``delivered`` tombstone row is
      inserted (a foreground wait's registration row may not exist — the
      store refused it, or a legacy waiter never wrote one — and "return
      True" alone leaves nothing durable for a LATE publisher to collide
      with: its ``ensure_durable_delegation`` would then create a fresh
      ``pending`` row and deliver the outcome a second time). The
      tombstone is atomic with the win (one transaction) and terminal, so
      the late publisher's insert no-ops, its completion UPDATE refuses to
      resurrect a delivered row, every consumer claim fails, and restart
      replay finds nothing pending;
    - ``delivered`` → ``False`` (a rail delivery already happened — inline
      must not duplicate it);
    - ``dropped`` → ``True`` (the rail terminally gave up; inline is the
      only delivery that will ever exist);
    - a ``pending`` row holding a fresh foreign claim (< 300 s, the same
      steal window :func:`claim_completion_delivery` honours) → ``False``
      (a live rail consumer owns the outcome right now);
    - anything else (unclaimed/stale ``pending``, a not-yet-published
      ``running``/``finalizing`` row, a legacy NULL row) → the row is
      claimed AND acknowledged ``delivered`` atomically → ``True``.

    Winning also holds against a LATE rail publish: ``_persist_completion``
    never resurrects a delivered row back to ``pending``, and any consumer
    that later picks up the already-queued event fails its
    :func:`claim_event_delivery` and skips it.
    """
    now = time.time()
    claim_id = (
        f"inline-takeover:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    )
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            # Durable tombstone for a no-row win — see docstring. INSERT OR
            # IGNORE keeps the win and the tombstone atomic (one
            # transaction) without ever stomping a row a concurrent writer
            # slipped in behind the SELECT: an ignored insert falls through
            # and that fresh row is judged by the normal takeover rules.
            cur = conn.execute(
                """INSERT OR IGNORE INTO async_delegations
                   (delegation_id, origin_session, origin_ui_session_id,
                    parent_session_id, state, dispatched_at, completed_at,
                    updated_at, delivered_at, delivery_state, delivery_claim,
                    delivery_claimed_at, task_json)
                   VALUES (?, '', '', NULL, ?, ?, ?, ?, ?, 'delivered', ?, ?, ?)""",
                (delegation_id, status, now, now, now, now, claim_id, now,
                 json.dumps({"external": True})),
            )
            if cur.rowcount == 1:
                return True
            row = conn.execute(
                "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                # Ignored yet still absent (the concurrent row vanished
                # again): nothing durable exists to replay either way.
                return True
        state = row[0]
        if state == "delivered":
            return False
        if state == "dropped":
            return True
        cur = conn.execute(
            """UPDATE async_delegations
               SET state=CASE WHEN state IN ('running','finalizing') THEN ?
                              ELSE state END,
                   completed_at=COALESCE(completed_at, ?),
                   delivery_state='delivered', delivered_at=?, updated_at=?,
                   delivery_claim=?, delivery_claimed_at=?
               WHERE delegation_id=? AND (
                   state IN ('running','finalizing')
                   OR delivery_state IS NULL
                   OR (delivery_state='pending'
                       AND (delivery_claim IS NULL OR delivery_claimed_at < ?))
               )""",
            (status, now, now, now, claim_id, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1

def is_interim_delegation_event(evt: Dict[str, Any]) -> bool:
    """An early per-task notice for a batch that is still running. It shares the batch's
    ``delegation_id`` but is NOT the durable completion: it must never claim, acknowledge or
    dedup against the final result's row (independent review reproduced exactly that loss)."""
    return evt.get("type") == "async_delegation" and bool(evt.get("task_failure_notice"))


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events (and interim notices) need no token."""
    if is_interim_delegation_event(evt):
        return ""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry. Attempts are
    counted at claim time; once the budget is exhausted the row converges to
    terminal ``dropped`` (only pending rows replay on restart)."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS))
        if capped.rowcount == 1:
            logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                           "marking terminally dropped (result remains queryable).",
                           delegation_id, _MAX_DELIVERY_ATTEMPTS)
            return True
        cur = conn.execute("""UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""", (now, delegation_id, claim_id))
        return cur.rowcount == 1


def defer_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Return an unadmitted completion to pending without spending a delivery attempt."""
    return _update_delivery("""UPDATE async_delegations SET delivery_claim=NULL,
                  delivery_claimed_at=NULL, delivery_attempts=MAX(0, delivery_attempts-1),
                  updated_at=?
           WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?""",
        (time.time(), delegation_id, claim_id))


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion whose target is permanently gone (the
    spawning session ended at an explicit user boundary such as /new or reset).
    ``dropped`` — not ``delivered`` — keeps the ack honest; not ``pending`` keeps
    restart recovery from replaying it into a fail-closed drop forever."""
    return _update_delivery("""UPDATE async_delegations SET delivery_state='dropped',
                  updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (time.time(), delegation_id, claim_id))


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    return _update_delivery("""UPDATE async_delegations SET delivery_state='delivered',
                  delivered_at=?, updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (now, now, delegation_id, claim_id))


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    _event_delivery(complete_completion_delivery, evt, claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    _event_delivery(release_completion_delivery, evt, claim_id)


def _event_delivery(fn, evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        fn(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute("""SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()
    return None if row is None else {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1], "dispatched_at": row[2],
        "completed_at": row[3], "result": json.loads(row[4]) if row[4] else None, "delivery_state": row[5],
        "delivery_attempts": row[6], "origin_session_id": row[7] or ""}


# ── In-memory registry queries ──────────────────────────────────────────────
def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow in place, never shrink) the shared daemon executor. Raising
    ``_max_workers`` is enough: the next ``submit`` spawns threads up to the new cap."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None:
            _executor = DaemonThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="async-delegate")
            _executor_max_workers = max_workers
        elif max_workers > _executor_max_workers:
            _executor._max_workers = max_workers
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of live async delegation UNITS (one per completion message: a task group or an ungrouped task)."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)


def active_task_count() -> int:
    """Number of running child subagents (a batch of N contributes N; a batch with
    no goal list counts 1) — the truthful observability figure, unlike slots."""
    with _records_lock:
        return sum(
            len(r.get("task_indexes") or r["goals"])
            if r.get("is_batch") and isinstance(r.get("goals"), (list, tuple)) and r["goals"] else 1
            for r in _records.values() if r.get("status") in {"running", "finalizing"})


def _session_records(statuses, session_key: str, origin_ui_session_id: str, parent_session_id: str) -> list:
    """Records in ``statuses`` owned by a session: any non-empty selector claims the
    record — ``origin_ui_session_id`` (TUI tab), ``session_key`` (routing key at
    dispatch), or ``parent_session_id`` (spawner's durable id — the right one for
    gateway chats, whose session_key survives ``/new`` while the session id rotates)."""
    selectors = [(field, wanted) for field, wanted in (
        ("origin_ui_session_id", origin_ui_session_id), ("session_key", session_key),
        ("parent_session_id", parent_session_id)) if wanted]
    if not selectors:
        return []
    with _records_lock:
        return [r for r in _records.values() if r.get("status") in statuses
                and any(str(r.get(field) or "") == wanted for field, wanted in selectors)]


def has_live_for_session(session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "") -> bool:
    """Whether a session still owns any live (running/stalling/finalizing) delegation."""
    return bool(_session_records(_LIVE_STATES, session_key, origin_ui_session_id, parent_session_id))


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the cap. Caller holds ``_records_lock``."""
    completed = [(rid, r) for rid, r in _records.items() if r.get("status") != "running"]
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: max(0, len(completed) - _MAX_RETAINED_COMPLETED)]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``. ``HERMES_SESSION_ID``
    is unsafe here: building the child agent calls ``set_current_session_id(child.session_id)``
    just before dispatch, so the wake would self-post into the subagent's own session. The
    request-scoped ``HERMES_SESSION_CHAT_ID`` (raw X-Hermes-Session-Id on api_server) survives
    child construction; on push platforms chat_id is a chat, not a session => ``""``."""
    try:
        from gateway.session_context import get_session_env
        is_api = get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server"
        return (get_session_env("HERMES_SESSION_CHAT_ID", "") or "") if is_api else ""
    except Exception:
        return ""


# ── Dispatch ────────────────────────────────────────────────────────────────
def _single_crash(error: str, duration: float) -> Dict[str, Any]:
    return {"status": "error", "summary": None, "error": error, "api_calls": 0, "duration_seconds": duration}


def _batch_crash(error: str, duration: float) -> Dict[str, Any]:
    return {"results": [], "error": error, "total_duration_seconds": duration}


def _batch_status(combined: Dict[str, Any]) -> str:
    """Batch status: completed unless every child errored/was interrupted."""
    child_results = combined.get("results") or []
    ok = ("completed", "success")
    return "error" if child_results and all(r.get("status") not in ok for r in child_results) else "completed"


def _dispatch(
    *, delegation_id: str, goal: str, goals: Optional[List[str]], context: Optional[str],
    toolsets: Optional[List[str]], role: str, model: Optional[str], session_key: str,
    parent_session_id: Optional[str], runner: Callable[[], Dict[str, Any]], origin_ui_session_id: str,
    origin_session_id: str, interrupt_fn: Optional[Callable[[], None]], max_async_children: int,
    progress_fn: Optional[Callable[[], tuple]], capacity_error: str, slot_key: Optional[str] = None,
    task_indexes: Optional[List[int]] = None, tool: str = "", result_kind: str = "",
) -> Dict[str, Any]:
    """Shared dispatch core for single (``goals is None``) and batch units. Capacity check +
    record insert happen under ONE lock hold so concurrent dispatches can't both pass the check
    and exceed the cap. At capacity the dispatch is REJECTED (never queued) so a runaway model
    can't pile up unbounded background work. ``slot_key`` names the pool slot the unit occupies
    (default: its own id); the units of one delegate_task call share the first unit's id so
    splitting a call into per-group completions never consumes more capacity than the call did."""
    is_batch = goals is not None
    label = " batch" if is_batch else ""
    classify = _batch_status if is_batch else (lambda r: r.get("status") or "completed")
    crash_result = _batch_crash if is_batch else _single_crash
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id, "goal": goal, **({"goals": list(goals)} if is_batch else {}),
        "context": context, "toolsets": list(toolsets) if toolsets else None, "role": role, "model": model,
        "session_key": session_key, "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id, "parent_session_id": parent_session_id,
        **_capture_routing_origin(),
        "tool": tool,
        "result_kind": result_kind,
        "status": "running", "dispatched_at": dispatched_at, "completed_at": None,
        "interrupt_fn": interrupt_fn, **({"is_batch": True} if is_batch else {}), "progress_fn": progress_fn,
        "slot_key": slot_key or delegation_id,
        # Which of the call's ``goals`` this unit runs (None = all of them).
        **({"task_indexes": list(task_indexes)} if task_indexes is not None else {}),
        # Delivery-mode bookkeeping (see DELIVERY_MODE_*). ``runner_tid`` is
        # filled in by the worker so interrupt_all/interrupt_for_session can
        # set the per-thread interrupt bit on the thread actually running the
        # child, making cooperative _check_interrupted() hooks fire.
        "delivery_mode": DELIVERY_MODE_EVENT,
        "inline_event": None,
        "inline_result": None,
        "inline_claimed": False,
        "runner_tid": None,
        # Stale-monitor bookkeeping (see _stale_monitor_loop).
        "_progress_token": None, "_progress_ts": dispatched_at, "_interrupted_at": None}
    with _records_lock:
        existing = _records.get(delegation_id)
        if existing is not None and existing.get("status") in (
            "running", "stalling", "finalizing"
        ):
            # Idempotent re-arm: the caller passed a pre-minted id (the
            # Cursor receipt spine re-arms a background poll after a
            # restart) and this unit is already live — never start a second
            # runner for the same handle.
            return {"status": "duplicate", "delegation_id": delegation_id}
        active_slots = {r.get("slot_key") or r["delegation_id"] for r in _records.values() if r.get("status") in _ACTIVE_STATES}
        if record["slot_key"] not in active_slots and len(active_slots) >= max_async_children:
            return {"status": "rejected", "error": capacity_error}
        _records[delegation_id] = record
        live_units = sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)
    _persist_dispatch(record)
    # Units of one call share a slot, so live units can exceed slots: size the pool by units or a
    # unit queues behind a full pool and the stale monitor kills it before its child ever starts.
    executor = _get_executor(max(max_async_children, live_units))

    def _worker() -> None:
        from tools.interrupt import clear_current_thread_interrupt

        result: Dict[str, Any] = {}
        status = "error"
        with _records_lock:
            rec = _records.get(delegation_id)
            if rec is not None:
                # The stall clock starts when the runner starts; a unit queued behind a full pool is not stalled.
                rec.update(_started=True, _progress_ts=time.time())
        try:
            with _records_lock:
                record["runner_tid"] = threading.current_thread().ident
            # Recycled-thread hygiene: the daemon pool reuses this thread for
            # later delegations, and the interrupt bit is a process-global
            # per-thread flag. A previous delegation's /stop or gateway
            # shutdown may have left it set HERE, which would make the next
            # runner begin already interrupted. Clear only the CURRENT
            # thread's bit — another runner's thread is never touched.
            clear_current_thread_interrupt()
            result = runner() or {}
            status = classify(result)
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception(f"Async delegation{label} %s crashed", delegation_id)
            result = crash_result(f"{type(exc).__name__}: {exc}", round(time.time() - dispatched_at, 2))
        finally:
            try:
                _finalize(delegation_id, result, status)
            finally:
                # Post-finalize the record is terminal, so no interrupt path
                # can re-target this thread through it — wipe the bit again
                # so the next delegation scheduled onto this recycled worker
                # starts clean even if a set landed mid-run and went
                # unobserved. Nested try/finally (not one flat block): a
                # raising _finalize must not skip this wipe, or the pooled
                # thread carries a set interrupt bit into its NEXT task.
                clear_current_thread_interrupt()

    try:
        # Propagate the dispatching profile so the detached child resolves get_hermes_home() correctly.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        with _DB_LOCK, _transaction() as conn:
            conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
        return {"status": "rejected", "error": f"Failed to schedule async delegation{label}: {exc}"}
    if progress_fn is not None:
        _ensure_stale_monitor()
    return {"status": "dispatched", "delegation_id": delegation_id}


def dispatch_async_delegation(
    *, goal: str, context: Optional[str], toolsets: Optional[List[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, delegation_id: Optional[str] = None,
    tool: str = "", result_kind: str = "", progress_fn: Optional[Callable[[], tuple]] = None,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.
    ``session_key``/``parent_session_id`` are captured on the parent thread (the worker carries
    no contextvars) and route the completion back to the spawning session. A caller-minted
    ``delegation_id`` (the Cursor receipt spine re-arms under the SAME id) is honored as-is;
    ``tool``/``result_kind`` stamp the record for delivery routing.
    ``progress_fn() -> (token, in_tool)`` enables stale monitoring; omitted = unmonitored.
    Returns ``{"status": "dispatched", "delegation_id"}`` or ``{"status": "rejected", "error"}``."""
    delegation_id = delegation_id or _new_delegation_id()
    handle = _dispatch(
        delegation_id=delegation_id, goal=goal, goals=None, context=context,
        toolsets=toolsets, role=role, model=model, session_key=session_key,
        parent_session_id=parent_session_id, runner=runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        interrupt_fn=interrupt_fn, max_async_children=max_async_children,
        tool=tool, result_kind=result_kind, progress_fn=progress_fn,
        capacity_error=(
            f"Async delegation capacity reached ({max_async_children} running). Wait for one to finish "
            "(its result will re-enter the chat), or run this task synchronously (background=false). "
            "Raise delegation.max_concurrent_children in config.yaml to allow more concurrent background subagents."))
    if handle["status"] == "dispatched":
        logger.info("Dispatched async delegation %s (session_key=%s): %s",
                    delegation_id, session_key or "<cli>", (goal or "")[:80])
    return handle


def dispatch_async_delegation_batch(
    *, goals: List[str], context: Optional[str], toolsets: Optional[List[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, delegation_id: Optional[str] = None,
    tool: str = "", result_kind: str = "", progress_fn: Optional[Callable[[], tuple]] = None,
    slot_key: Optional[str] = None, task_indexes: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Dispatch a fan-out unit (a whole batch, or one ``group`` of a delegate_task call) as ONE
    background unit: ``runner`` runs its tasks and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict. The unit occupies ONE async slot — or joins the slot named
    by ``slot_key`` (in-unit parallelism is bounded separately) — and produces a SINGLE completion
    event carrying per-task ``results``."""
    delegation_id = delegation_id or _new_delegation_id()
    # ``goals`` is the whole call (result task_index indexes it); the unit's own goals label the record.
    unit_goals = [goals[i] for i in task_indexes] if task_indexes is not None else list(goals)
    n = len(unit_goals)
    combined_goal = unit_goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in unit_goals)
    handle = _dispatch(
        delegation_id=delegation_id, goal=combined_goal, goals=goals, context=context,
        toolsets=toolsets, role=role, model=model, session_key=session_key,
        parent_session_id=parent_session_id, runner=runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        interrupt_fn=interrupt_fn, max_async_children=max_async_children,
        tool=tool, result_kind=result_kind, progress_fn=progress_fn, slot_key=slot_key,
        task_indexes=task_indexes,
        capacity_error=(
            f"Async delegation capacity reached ({max_async_children} running). Wait for one to finish "
            "(its result will re-enter the chat), or raise delegation.max_concurrent_children in "
            "config.yaml to allow more concurrent background units."))
    if handle["status"] == "dispatched":
        logger.info("Dispatched async delegation batch %s (%d task(s), session_key=%s)",
                    delegation_id, n, session_key or "<cli>")
    return handle


# ── Finalization + completion events ────────────────────────────────────────
def _finalize(delegation_id: str, result: Any, status: str) -> None:
    """Atomically claim terminal delivery, push the completion event, then mark ``status``.
    ``result`` is a dict or a callable receiving the record snapshot (stall path). The record
    stays active ("finalizing") until durable persistence and queue publication finish; otherwise
    process shutdown can kill this daemon worker after status flips but before SQLite commits.
    A second call for the same id (late runner return after a forced stall) is a no-op."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in _ACTIVE_STATES:
            return
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["progress_fn"] = None  # stop stale-monitor sampling
        snapshot = dict(record)
    _push_completion_event(snapshot, result(snapshot) if callable(result) else result, status)
    with _records_lock:
        if delegation_id in _records:
            _records[delegation_id]["status"] = status
        _prune_completed_locked()


def publish_terminal_event(evt: Dict[str, Any], result: Dict[str, Any]) -> bool:
    """Deliver ONE terminal delegation event. The only queue producer.

    This is the single chokepoint that turns a finished delegation into a
    delivered outcome, so "exactly one delivery channel" is structural rather
    than conventional:

    - If a live record registered an INLINE wait (foreground
      ``delegate_assistant``), the event is handed to that waiter under
      ``_records_lock`` and ``False`` is returned — nothing is persisted as
      pending and nothing is placed on the completion queue, so no second
      turn is spawned.
    - Otherwise the event is persisted durably and pushed onto the shared
      ``process_registry.completion_queue``; ``True`` is returned.

    The mode swap and the result hand-off both happen under the same
    ``_records_lock`` hold, which makes the linearization point unambiguous:
    exactly one of {inline hand-off, queue put} runs for a delegation id.
    """
    delegation_id = str(evt.get("delegation_id") or "")
    with _records_lock:
        record = _records.get(delegation_id) if delegation_id else None
        if (
            record is not None
            and record.get("delivery_mode") == DELIVERY_MODE_INLINE
        ):
            record["inline_result"] = evt
            waiter = record.get("inline_event")
            if waiter is not None:
                waiter.set()
            return False

    _persist_completion(evt, result)
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            delegation_id, exc,
        )
        return False
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            delegation_id, exc,
        )
        return False
    return True


def _push_completion_event(record: Dict[str, Any], result: Dict[str, Any], status: str) -> None:
    """Push a type='async_delegation' event onto the shared completion queue. Batch records
    (``is_batch``) carry the per-task ``results`` list (plus live transcript paths, the
    full-fidelity record of each child's run) instead of a single summary. Best-effort: failure
    must not crash the worker, but it WOULD mean a silently-lost result, so we log loudly."""
    is_batch = bool(record.get("is_batch"))
    label = " batch" if is_batch else ""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(f"Async delegation{label} %s finished but process_registry import failed; "
                     "result lost: %s", record.get("delegation_id"), exc)
        return
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()
    if is_batch:
        payload = {
            "is_batch": True, "results": result.get("results") or [],
            "live_transcripts": result.get("live_transcripts"), "error": result.get("error"),
            "total_duration_seconds": result.get("total_duration_seconds"),
            **({"group": result["group"]} if result.get("group") is not None else {})}
    else:
        payload = {
            "summary": result.get("summary"), "error": result.get("error"), "api_calls": result.get("api_calls", 0),
            "duration_seconds": result.get("duration_seconds", round(completed_at - dispatched_at, 2))}
    evt = {
        "type": "async_delegation", "delegation_id": record.get("delegation_id"),
        # session_key routes back to the originating gateway session; "" => CLI.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""), **({"goals": record.get("goals")} if is_batch else {}),
        "context": record.get("context"), "toolsets": record.get("toolsets"), "role": record.get("role"),
        "model": record.get("model") if is_batch else (result.get("model") or record.get("model")),
        "status": status, **payload, "dispatched_at": dispatched_at, "completed_at": completed_at,
        **({} if is_batch else {"exit_reason": result.get("exit_reason")}),
        **{k: record[k] for k in _ROUTING_KEYS if record.get(k)},
        **{k: result[k] for k in _STALL_META_KEYS if k in result}}
    # Uniform lifecycle: run artifacts a cli/cloud delegation finishes with
    # (the run log, the child session id, cost) ride along additively so the
    # completion block can point the caller at them. Absent on subagent
    # results, which is fine — consumers key off presence.
    for _k in ("log_path", "child_session_id", "cost_usd", "warnings", "models_used"):
        if result.get(_k):
            evt[_k] = result[_k]
    _stamp_event_provenance(evt, record)
    _persist_completion(evt, result)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(f"Async delegation{label} %s: failed to enqueue completion event; "
                     "result lost: %s", record.get("delegation_id"), exc)


def push_task_failure_notice(delegation_id: str, entry: Dict[str, Any], *, n_tasks: int) -> None:
    """Surface ONE failed child of a still-running detached batch to the parent now, instead of
    when the slowest sibling finishes. In a 1,393-agent run every wave-1 child died in a 401 storm
    at 08:29 and the parent learned of it at 09:36, when the batch's "unknown outcome" block finally
    arrived: 66 minutes of a dead wave with nothing running. The notice rides the same
    ``type="async_delegation"`` event shape as the batch result (so every drain/route/format path
    treats it identically) with ``task_failure_notice=True`` and a single-entry ``results`` list; the
    batch record is NOT finalized and its consolidated result still arrives as before."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in _ACTIVE_STATES:
            return
        snapshot = dict(record)
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error("Async delegation batch %s: task failure notice dropped (process_registry import): %s", delegation_id, exc)
        return
    evt = {
        "type": "async_delegation", "task_failure_notice": True, "is_batch": True, "n_tasks": n_tasks,
        "delegation_id": delegation_id, "results": [entry],
        "session_key": snapshot.get("session_key", ""),
        "origin_ui_session_id": snapshot.get("origin_ui_session_id", ""),
        "origin_session_id": snapshot.get("origin_session_id", ""),
        "parent_session_id": snapshot.get("parent_session_id"),
        "goal": snapshot.get("goal", ""), "goals": snapshot.get("goals"), "context": snapshot.get("context"),
        "toolsets": snapshot.get("toolsets"), "role": snapshot.get("role"), "model": snapshot.get("model"),
        "status": "running", "dispatched_at": snapshot.get("dispatched_at") or time.time(), "completed_at": time.time(),
        **{k: snapshot[k] for k in _ROUTING_KEYS if snapshot.get(k)}}
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error("Async delegation batch %s: failed to enqueue task failure notice: %s", delegation_id, exc)


def _stamp_event_provenance(evt: Dict[str, Any], record: Dict[str, Any]) -> None:
    """Add routing-origin + tool provenance keys onto a terminal event.

    Additive only: consumers key off the fields that were already there, so
    unknown/absent provenance degrades to today's behavior.
    """
    # Routing origin captured at dispatch (see _capture_routing_origin):
    # additive, lets the gateway reconstruct a full SessionSource (incl.
    # scope_id for relay tenant egress) when its own caches are cold.
    for _k in ("scope_id", "user_id", "user_name"):
        if record.get(_k):
            evt[_k] = record[_k]
    # Uniform delegation lifecycle: which tool commissioned this work and what
    # kind of result it carries, so renderers can label it correctly.
    if record.get("tool"):
        evt["tool"] = record["tool"]
    if record.get("result_kind"):
        evt["result_kind"] = record["result_kind"]
    evt["background"] = True


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    _push_batch_completion_event(event_record, combined, status)
    _finish_finalization(delegation_id, status)


def _push_batch_completion_event(
    event_record: Dict[str, Any], combined: Dict[str, Any], status: str
) -> None:
    """Build + publish a combined async-delegation batch completion event."""
    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": event_record.get("delegation_id"),
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_session_id": event_record.get("origin_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    _stamp_event_provenance(evt, event_record)
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in combined:
            evt[_k] = combined[_k]
    publish_terminal_event(evt, combined)



# ── Stale monitor ───────────────────────────────────────────────────────────
def _ensure_stale_monitor() -> None:
    """Start (once) the stale-delegation monitor thread. One daemon thread serves
    every dispatch; it exits when no monitorable records remain and is restarted
    by the next dispatch with a ``progress_fn``."""
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop, name="async-delegate-stale-monitor", daemon=True)
        _monitor_thread.start()


def _sweep_stale_locked(now: float):
    """One monitor pass over ``_records``; caller holds ``_records_lock``. Returns
    ``(stalled, expired, any_monitorable)``: newly-stalling ``(delegation_id, quiet_for, in_tool)``
    tuples, stalling ids past the grace window, and whether anything is left to monitor."""
    stalled, expired, any_monitorable = [], [], False  # (delegation_id, quiet_for, in_tool) / ids past grace
    for record in _records.values():
        status = record.get("status")
        if status == "stalling":
            any_monitorable = True
            if now - (record.get("_interrupted_at") or now) >= _STALL_GRACE_SECONDS:
                expired.append(record["delegation_id"])
            continue
        progress_fn = record.get("progress_fn")
        if status != "running" or progress_fn is None:
            continue
        any_monitorable = True
        if not record.get("_started"):
            continue  # queued behind a full pool: not stalled, but keep the monitor alive for when it starts
        try:
            token, in_tool = progress_fn()
        except Exception:
            # An unreadable child must not look permanently healthy —
            # keep the last timestamp running instead of refreshing it.
            token, in_tool = record.get("_progress_token"), False
        if token != record.get("_progress_token"):
            record.update(_progress_token=token, _progress_ts=now)
            continue
        quiet_for = now - (record.get("_progress_ts") or now)
        limit = _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
        if quiet_for >= limit:
            # Stall context feeds the terminal event and status listings.
            record.update(
                status="stalling", _interrupted_at=now, _stall_quiet_seconds=round(quiet_for, 2),
                _stall_threshold_seconds=limit, _stall_in_tool=bool(in_tool))
            stalled.append((record["delegation_id"], quiet_for, in_tool))
    return stalled, expired, any_monitorable


def _call_interrupt(fn, msg: str, *args) -> bool:
    """Invoke an ``interrupt_fn``; True on success, else debug-log ``msg`` (+ exc)."""
    if not callable(fn):
        return False
    try:
        fn()
        return True
    except Exception as exc:
        logger.debug(msg, *args, exc)
        return False


def _stale_monitor_loop() -> None:
    """Sweep running delegations for stalled progress. A changed progress token refreshes the
    record's timestamp; a frozen token past the idle/in-tool threshold marks the record
    ``stalling`` and calls ``interrupt_fn``; a ``stalling`` record still unreturned after the
    grace window is force-finalized with a terminal ``stalled`` event."""
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        now = time.time()
        with _records_lock:
            stalled, expired, any_monitorable = _sweep_stale_locked(now)
        for delegation_id, quiet_for, in_tool in stalled:
            logger.warning("Async delegation %s made no progress for %.0fs "
                           "(in_tool=%s) — interrupting; grace window %.0fs",
                           delegation_id, quiet_for, in_tool, _STALL_GRACE_SECONDS)
            with _records_lock:
                fn = (_records.get(delegation_id) or {}).get("interrupt_fn")
            _call_interrupt(fn, "Async delegation %s stall interrupt failed: %s", delegation_id)
        for delegation_id in expired:
            _finalize(delegation_id, lambda rec, d=delegation_id: _stalled_result(d, rec), "stalled")
        if not any_monitorable:
            return


def _stalled_result(delegation_id: str, event_record: Dict[str, Any]) -> Dict[str, Any]:
    """Synthetic terminal result for a stalling delegation whose runner never returned."""
    completed_at = event_record.get("completed_at") or time.time()
    duration = round(completed_at - (event_record.get("dispatched_at") or completed_at), 2)
    error = (
        f"Async delegation {delegation_id} stalled: the detached subagent stopped making progress "
        "(no new API calls, tool activity, or streamed tokens), did not respond to interruption, and never "
        "produced a completion event. The worker may be wedged inside a model API call — this is a known "
        "failure mode of long-lived gateway processes (#60203). Re-dispatch the task if it is still needed.")
    logger.error("Async delegation %s force-finalized as stalled after %.0fs", delegation_id, duration)
    # Structured stall metadata lets parents/UIs distinguish a stall-monitor
    # kill from other failures without parsing the error string.
    stall_in_tool = event_record.get("_stall_in_tool")
    stall_meta = {
        "stalled_after_quiet_seconds": event_record.get("_stall_quiet_seconds"),
        "stall_threshold_seconds": event_record.get("_stall_threshold_seconds"),
        "stall_phase": "in_tool" if stall_in_tool else "idle" if stall_in_tool is not None else None,
        "stall_grace_seconds": _STALL_GRACE_SECONDS}
    if event_record.get("is_batch"):
        return {**_batch_crash(error, duration), **stall_meta}
    return {**_single_crash(error, duration), "status": "stalled", "exit_reason": "stalled", **stall_meta}


# ── Observability + control ─────────────────────────────────────────────────
def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Parse a progress token into per-child activity dicts (best-effort): delegate_tool
    emits one ``(api_call_count, current_tool, last_activity_ts)`` tuple per child;
    foreign token shapes degrade to ``None`` entries."""
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if not (isinstance(part, (list, tuple)) and len(part) >= 2):
            out.append(None)
            continue
        entry: Dict[str, Any] = {"api_calls": part[0], "current_tool": part[1]}
        if len(part) >= 3 and isinstance(part[2], (int, float)):
            entry["seconds_since_activity"] = round(max(0.0, now - float(part[2])), 1)
        out.append(entry)
    return out


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed) without callables or private
    monitor bookkeeping; adds computed live fields for UIs (``seconds_since_progress``,
    ``children_activity``/``in_tool`` sampled from ``progress_fn``) and stall context once tripped.

    Safe to call from any thread. See #51690.
    """
    now = time.time()
    samplers: Dict[str, Callable] = {}
    with _records_lock:
        items = []
        for r in _records.values():
            item = {k: v for k, v in r.items() if k not in {"interrupt_fn", "progress_fn"} and not k.startswith("_")}
            status = r.get("status")
            if status in _ACTIVE_STATES:
                if r.get("_progress_ts"):
                    item["seconds_since_progress"] = round(now - r["_progress_ts"], 1)
                if callable(r.get("progress_fn")):
                    samplers[r["delegation_id"]] = r["progress_fn"]
            if status in ("stalling", "stalled"):
                for src, dst in _STALL_FIELD_MAP:
                    if r.get(src) is not None:
                        item[dst] = r.get(src)
            items.append(item)
    # Sample OUTSIDE the lock — progress_fn reads child-agent attributes and a
    # slow/broken sampler must not block every dispatch/finalize.
    for item in items:
        fn = samplers.get(item.get("delegation_id"))
        if fn is None:
            continue
        try:
            token, in_tool = fn()
        except Exception:
            continue
        activity = _children_activity_from_token(token, now)
        if activity is not None:
            item["children_activity"] = activity
        item["in_tool"] = bool(in_tool)
    return items


def _signal_runner_interrupt(record: Dict[str, Any], reason: str) -> bool:
    """Signal one delegation to stop. Returns True if any channel fired.

    Two independent channels, both required for the uniform lifecycle:

    - ``runner_tid``: set the per-thread interrupt bit on the worker thread
      so cooperative ``_check_interrupted()`` hooks INSIDE the runner (the
      claude/cursor subprocess poll loops) unwind on ``/stop`` and gateway
      shutdown, not just the injected ``interrupt_fn``.
    - ``interrupt_fn``: the tool-specific cancellation (hard child-agent
      interrupt, cloud-run cancel, process-group kill).
    """
    signalled = False
    runner_tid = record.get("runner_tid")
    if runner_tid:
        try:
            from tools.interrupt import set_interrupt

            set_interrupt(True, runner_tid, reason=reason)
            signalled = True
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "interrupt: %s thread-bit failed: %s",
                record.get("delegation_id"), exc,
            )
    fn = record.get("interrupt_fn")
    if callable(fn):
        try:
            fn()
            signalled = True
        except Exception as exc:
            logger.debug(
                "interrupt: %s interrupt_fn failed: %s",
                record.get("delegation_id"), exc,
            )
    return signalled


def _interrupt_records(targets: List[Dict[str, Any]], caller: str, reason: str, msg: str) -> int:
    """Signal each record to stop via ``_signal_runner_interrupt`` (fork dual channel: the
    worker-thread interrupt bit so cooperative hooks unwind, plus ``interrupt_fn``); log ``msg``
    once; returns how many were signalled."""
    count = sum(
        1 for r in targets
        if _signal_runner_interrupt(r, reason))

    if count:
        logger.info(msg, count, reason)
    return count


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop (``/stop``, shutdown). Returns how
    many. The child still emits a completion event (status='interrupted') via the
    normal finalize path."""
    with _records_lock:
        targets = [r for r in _records.values() if r.get("status") in _ACTIVE_STATES]
    return _interrupt_records(targets, "interrupt_all", reason, "Interrupted %d async delegation(s) (%s)")


def interrupt_for_session(
    session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "", reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE ending session to stop (any
    selector matches, see ``_session_records``). Returns how many."""
    targets = _session_records(_ACTIVE_STATES, session_key, origin_ui_session_id, parent_session_id)
    return _interrupt_records(
        targets, "interrupt_for_session", reason, "Interrupted %d async delegation(s) for ending session (%s)")


# ---------------------------------------------------------------------------
# Uniform lifecycle: acceptance envelope, capability gate, inline waits
# ---------------------------------------------------------------------------

def build_background_acceptance_envelope(
    *,
    tool: str,
    result_kind: str,
    delegation_id: str,
    count: int = 1,
    goals: Optional[List[str]] = None,
    note: str = "",
    control_hint: str = "",
    subagent_ids: Optional[List[str]] = None,
    live_transcripts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The ONE acceptance shape every background delegation returns inline.

    A background call must not return a terminal result inline, so this
    envelope is all the caller gets: status, mode, the handle, and how to
    observe or steer the work. Tool-specific keys (``subagent_ids``,
    ``live_transcripts``) are added only when meaningful; ``tool`` and
    ``result_kind`` are always present so UIs can label the unit without
    sniffing the id format.
    """
    payload: Dict[str, Any] = {
        "status": "dispatched",
        "mode": "background",
        "delegation_id": delegation_id,
        "tool": tool,
        "result_kind": result_kind,
        "count": count,
    }
    if goals:
        payload["goals"] = list(goals)
    if note:
        payload["note"] = note
    if subagent_ids:
        payload["subagent_ids"] = list(subagent_ids)
    if control_hint:
        payload["control_hint"] = control_hint
    if live_transcripts:
        payload["live_transcripts"] = list(live_transcripts)
    return payload


def background_delivery_supported() -> tuple:
    """Whether THIS session can receive a late background completion.

    Wraps ``gateway.session_context.async_delivery_supported`` plus the
    api-server self-post escape: on a stateless HTTP session the adapter
    cannot push, but a bound raw session id still lets ``gateway.wake``
    self-POST a fresh turn, which IS a supported delivery channel. Returns
    ``(True, "")`` when delivery is possible and ``(False, reason)``
    otherwise — the reason is user-facing and must say that no work was
    started.

    Callers must run this BEFORE building any child or side effect: an
    unsupported channel is a hard error, never a silent foreground run.
    """
    try:
        from gateway.session_context import async_delivery_supported
    except Exception as exc:  # pragma: no cover — gateway always importable
        logger.debug("background_delivery_supported: context unavailable: %s", exc)
        return True, ""

    if async_delivery_supported():
        return True, ""

    wake_sid = _current_origin_session_id()
    if wake_sid:
        # The adapter cannot push, but the bound raw session id can be woken
        # by a self-post once the delegation completes (gateway.wake).
        return True, ""

    return (
        False,
        "background=true is not available in this session: it cannot receive "
        "a detached result after the turn ends (a one-shot runner such as "
        "`hermes -z`, a cron job, a Kanban worker, or a stateless HTTP "
        "endpoint). NO WORK WAS STARTED. Omit `background` (or pass "
        "background=false) to run the task in the foreground this turn "
        "instead.",
    )


def register_inline_wait(
    delegation_id: str,
    *,
    session_key: str = "",
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    parent_session_id: Optional[str] = None,
    goal: str = "",
    context: Optional[str] = None,
    tool: str = "",
    result_kind: str = "",
) -> "threading.Event":
    """Register a foreground waiter for ``delegation_id``; returns its Event.

    Creates the record when it does not exist and atomically flips
    ``delivery_mode`` to ``inline`` under ``_records_lock``, so a terminal
    event racing this call linearizes on one side of the swap. A freshly
    created wait record is externally-driven — the "runner" is a human
    conversation in whatever process serves it — so it is ALSO registered as
    a durable ``running`` row (``external``, never clobbering a row the
    closing process already inserted) BEFORE this thread blocks: that row is
    what a cross-process :func:`claim_inline_takeover` retires atomically,
    and marking it ``external`` keeps a restart from classifying a live
    mission as outcome-unknown. Best-effort — if the durable store refuses,
    the wait still runs and the no-row takeover writes its own delivered
    tombstone instead.

    The returned Event is set by :func:`publish_terminal_event` when the
    terminal outcome lands; the waiter then reads it with
    :func:`claim_inline_result`.
    """
    waiter = threading.Event()
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            record = {
                "delegation_id": delegation_id,
                "goal": goal,
                "context": context,
                "toolsets": None,
                "role": "",
                "model": None,
                "session_key": session_key,
                "origin_ui_session_id": origin_ui_session_id,
                "origin_session_id": origin_session_id,
                "parent_session_id": parent_session_id,
                **_capture_routing_origin(),
                "tool": tool,
                "result_kind": result_kind,
                # Externally-driven (see recover_abandoned_delegations): the
                # wait has no runner of ours, so the durable row below must
                # never be recovered as a dead-owner "outcome unknown".
                "external": True,
                "status": "running",
                "dispatched_at": time.time(),
                "completed_at": None,
                "interrupt_fn": None,
                "progress_fn": None,
                "delivery_mode": DELIVERY_MODE_EVENT,
                "inline_event": None,
                "inline_result": None,
                "inline_claimed": False,
                "runner_tid": None,
                "_progress_token": None,
                "_progress_ts": time.time(),
                "_interrupted_at": None,
            }
            _records[delegation_id] = record
        record["delivery_mode"] = DELIVERY_MODE_INLINE
        record["inline_event"] = waiter
        record["inline_result"] = None
        record["inline_claimed"] = False
    try:
        # INSERT OR IGNORE: an executor-owned delegation's dispatch row (or a
        # terminal-side row the closing process just inserted) is never
        # overwritten — only a missing row is created.
        _persist_dispatch(record, replace=False)
    except Exception:
        logger.warning(
            "Foreground inline wait for %s: durable registration failed; "
            "the no-row takeover tombstone is the durability fallback.",
            delegation_id,
            exc_info=True,
        )
    return waiter


def claim_inline_result(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Consume the terminal event parked for an inline waiter, if any.

    Non-blocking: the caller polls its Event between interrupt checks and
    calls this once the Event is set (or opportunistically). The result is
    consumed exactly once — a second claim returns ``None``.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return None
        evt = record.get("inline_result")
        if evt is None:
            return None
        record["inline_result"] = None
        record["inline_claimed"] = True
        # No live waiter anymore (matches inline_wait_pending's contract): a
        # later publisher for the same id must see "no one is waiting" and
        # take the rail, not report a phantom inline hand-off.
        record["inline_event"] = None
        return evt


def abandon_inline_wait(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Give up a foreground wait without losing the terminal result.

    Returns a parked-but-unconsumed terminal event for the CALLER to publish
    on the event rail (the mission already finished; the waiter is leaving;
    dropping it would lose the outcome). When no result has landed yet the
    record is flipped back to ``delivery_mode="event"`` and KEPT LIVE, so the
    eventual terminalization publishes through the rail as a fresh turn.

    Either way exactly one delivery channel survives this call.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return None
        if record.get("delivery_mode") != DELIVERY_MODE_INLINE:
            return None
        parked = record.get("inline_result")
        if record.get("inline_claimed"):
            parked = None
        record["inline_result"] = None
        record["inline_event"] = None
        if parked is not None:
            # Caller is leaving with an unconsumed terminal result: drop the
            # record so a later publish cannot double-deliver, and hand the
            # event back for rail publication.
            _records.pop(delegation_id, None)
            return parked
        record["delivery_mode"] = DELIVERY_MODE_EVENT
        record["inline_claimed"] = False
        return None


def inline_wait_pending(delegation_id: str) -> bool:
    """Whether a live foreground waiter is registered for ``delegation_id``.

    Lets a terminal-side publisher tell "handed to a waiting caller" apart
    from "failed to publish" — :func:`publish_terminal_event` returns
    ``False`` for both. Stays ``True`` until the waiter claims or abandons,
    which is exactly the window the publisher cares about.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        return bool(
            record is not None
            and record.get("delivery_mode") == DELIVERY_MODE_INLINE
            and record.get("inline_event") is not None
        )


def drop_inline_wait(delegation_id: str) -> None:
    """Drop a foreground-wait record outright — the outcome arrived elsewhere.

    Cross-process terminalization: the closing process could not see THIS
    process's inline record, so it published the outcome on its own
    completion rail. Nothing will ever be published for this id here, and
    keeping the record live would pin a phantom ``running`` delegation
    against ``active_count()`` (capacity accounting, scale-to-zero) forever.
    Idempotent; never touches a record that is not an inline wait.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("delivery_mode") != DELIVERY_MODE_INLINE:
            return
        _records.pop(delegation_id, None)


def abandon_all_inline_waits(reason: str = "shutdown") -> int:
    """Abandon every live foreground wait. Returns how many.

    Shutdown fan-out (alongside :func:`interrupt_all`): the waiter threads
    die with the process, so each inline claim must be released without
    losing its outcome —

    - a parked-but-unconsumed result is published on the event rail (the
      durable row makes it replay after the restart), and the record drops;
    - a still-pending wait flips back to ``delivery_mode="event"`` and stays
      live, so the work's own terminalization publishes normally later.

    Either way exactly one delivery channel survives per delegation.
    """
    parked_events: List[tuple] = []
    with _records_lock:
        targets = [
            rid
            for rid, r in _records.items()
            if r.get("delivery_mode") == DELIVERY_MODE_INLINE
        ]
        for rid in targets:
            record = _records.get(rid)
            if record is None:
                continue
            evt = record.get("inline_result")
            if record.get("inline_claimed"):
                evt = None
            record["inline_result"] = None
            record["inline_event"] = None
            if evt is not None:
                _records.pop(rid, None)
                parked_events.append((rid, evt))
            else:
                record["delivery_mode"] = DELIVERY_MODE_EVENT
                record["inline_claimed"] = False
    for delegation_id, evt in parked_events:
        # Outside _records_lock: publish_terminal_event takes the same lock.
        # The durable row already exists — terminalization persists before
        # parking — so this UPDATE lands and the replay survives the restart.
        try:
            publish_terminal_event(evt, {"status": evt.get("status")})
        except Exception as exc:  # pragma: no cover — must not break shutdown
            logger.error(
                "Abandoned inline wait %s: failed to publish its parked "
                "result (%s); outcome lost",
                delegation_id, exc,
            )
    if targets:
        logger.info(
            "Abandoned %d foreground delegation wait(s) (%s)",
            len(targets), reason,
        )
    return len(targets)


def finalize_external_delegation(delegation_id: str, status: str) -> None:
    """Retire an externally-driven delegation's in-memory record.

    ``register_background_delegation`` / ``register_inline_wait`` records
    have no worker of ours, so nothing ever runs ``_begin_finalization`` for
    them: whoever publishes the terminal event must also retire the record
    here, or it pins a phantom ``running`` unit against ``active_count()``
    (capacity accounting, scale-to-zero) for the life of the process.

    Mirrors ``_finish_finalization``: terminal status, then the same
    bounded-retention prune executor-owned records get.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in ("running", "stalling"):
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        record["progress_fn"] = None
        _prune_completed_locked()


def register_background_delegation(
    *,
    delegation_id: str,
    session_key: str = "",
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    parent_session_id: Optional[str] = None,
    goal: str = "",
    goals: Optional[List[str]] = None,
    context: Optional[str] = None,
    tool: str = "",
    result_kind: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Register an EXTERNALLY-driven background unit (no runner of ours).

    Used by delegations whose work is not a callable we own — the assistant
    mission, whose "runner" is a human conversation in another session. The
    record is live in the registry (so capacity accounting, the typing
    supervisor, and scale-to-zero see it) and has a durable row, so its
    completion is claimable/dedup-able and replays after a restart. The
    terminal event is delivered by whatever finishes the work calling
    :func:`publish_terminal_event`.
    """
    now = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "goals": list(goals) if goals else None,
        "context": context,
        "toolsets": None,
        "role": "",
        "model": None,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        **_capture_routing_origin(),
        "tool": tool,
        "result_kind": result_kind,
        # Externally-driven (see recover_abandoned_delegations): no process
        # owns this unit's outcome, so a dead owner_pid must not classify it
        # as abandoned at the next boot.
        "external": True,
        "status": "running",
        "dispatched_at": now,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "progress_fn": None,
        "delivery_mode": DELIVERY_MODE_EVENT,
        "inline_event": None,
        "inline_result": None,
        "inline_claimed": False,
        "runner_tid": None,
        "_progress_token": None,
        "_progress_ts": now,
        "_interrupted_at": None,
    }
    with _records_lock:
        existing = _records.get(delegation_id)
        if existing is not None and existing.get("status") in (
            "running", "stalling", "finalizing"
        ):
            return {"status": "duplicate", "delegation_id": delegation_id}
        _records[delegation_id] = record
    _persist_dispatch(record)
    return {"status": "dispatched", "delegation_id": delegation_id}


def ensure_durable_delegation(
    *,
    delegation_id: str,
    session_key: str = "",
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    parent_session_id: Optional[str] = None,
    goal: str = "",
    goals: Optional[List[str]] = None,
    context: Optional[str] = None,
    tool: str = "",
    result_kind: str = "",
    dispatched_at: Optional[float] = None,
) -> None:
    """Create the durable ``running`` row for an externally-driven delegation.

    Terminal-side companion to :func:`register_background_delegation`: an
    externally-driven unit (an assistant mission) typically terminalizes in a
    DIFFERENT process than the one that accepted it — the assistant profile's
    own turn — so the row may not exist when :func:`publish_terminal_event`
    UPDATEs it. Inserting it here, idempotently and never clobbering an
    existing row, gives the completion the same durability,
    ``claim_completion_delivery`` dedup, and restart replay every
    executor-owned delegation gets. No in-memory record is created: the
    closing process must not hold a phantom live delegation.
    """
    _persist_dispatch(
        {
            "delegation_id": delegation_id,
            "session_key": session_key,
            "origin_ui_session_id": origin_ui_session_id,
            "origin_session_id": origin_session_id,
            "parent_session_id": parent_session_id,
            "goal": goal,
            "goals": goals,
            "context": context,
            "tool": tool,
            "result_kind": result_kind,
            # Externally-driven (see recover_abandoned_delegations): the
            # closing process inserts this row itself, so it is never a
            # candidate for dead-owner recovery either.
            "external": True,
            "dispatched_at": dispatched_at if dispatched_at is not None else time.time(),
        },
        replace=False,
    )


def dispatch_background_delegation(
    *,
    tool: str,
    result_kind: str,
    goal: str,
    runner: Callable[[], Dict[str, Any]],
    session_key: str = "",
    parent_session_id: Optional[str] = None,
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    goals: Optional[List[str]] = None,
    context: Optional[str] = None,
    role: str = "",
    model: Optional[str] = None,
    delegation_id: Optional[str] = None,
    note: str = "",
    control_hint: str = "",
    progress_fn: Optional[Callable[[], tuple]] = None,
) -> Dict[str, Any]:
    """Thin single chokepoint for a background dispatch + acceptance envelope.

    Wraps :func:`dispatch_async_delegation` / ``_batch`` (they own the record,
    the durable row, the executor slot, and the terminal publisher) and
    returns the shared acceptance envelope on success. On rejection the raw
    ``{"status": "rejected", ...}`` dict is returned unchanged so the caller
    can fail clearly WITHOUT having started any work.
    """
    if goals and len(goals) > 1:
        dispatch = dispatch_async_delegation_batch(
            goals=list(goals),
            context=context,
            toolsets=None,
            role=role,
            model=model,
            session_key=session_key,
            parent_session_id=parent_session_id,
            runner=runner,
            origin_ui_session_id=origin_ui_session_id,
            origin_session_id=origin_session_id,
            interrupt_fn=interrupt_fn,
            max_async_children=max_async_children,
            delegation_id=delegation_id,
            tool=tool,
            result_kind=result_kind,
            progress_fn=progress_fn,
        )
    else:
        dispatch = dispatch_async_delegation(
            goal=goal,
            context=context,
            toolsets=None,
            role=role,
            model=model,
            session_key=session_key,
            parent_session_id=parent_session_id,
            runner=runner,
            origin_ui_session_id=origin_ui_session_id,
            origin_session_id=origin_session_id,
            interrupt_fn=interrupt_fn,
            max_async_children=max_async_children,
            delegation_id=delegation_id,
            tool=tool,
            result_kind=result_kind,
            progress_fn=progress_fn,
        )
    if dispatch.get("status") != "dispatched":
        return dispatch
    return build_background_acceptance_envelope(
        tool=tool,
        result_kind=result_kind,
        delegation_id=dispatch["delegation_id"],
        count=len(goals) if goals else 1,
        goals=goals,
        note=note,
        control_hint=control_hint,
    )


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor + monitor."""
    global _executor, _executor_max_workers, _monitor_thread
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    _monitor_stop.set()
    with _monitor_lock:
        thread, _monitor_thread = _monitor_thread, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with _records_lock:
        _records.clear()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def active_for_session(origin_ui_session_id: str) -> int:
    """Number of live async delegations owned by one UI session."""
    if not origin_ui_session_id:
        return 0
    with _records_lock:
        return sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
            and str(r.get("origin_ui_session_id") or "")
            == origin_ui_session_id
        )
# ---- END PLUGIN-COMPAT ----
