"""Generic adapter hooks for an overlapping gateway restart.

A transport that can keep two gateway generations attached opts in through the
adapter methods below.  The runner stays transport-agnostic: shared transport
hubs implement this capability without changing the agent loop.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

OVERLAP_SUCCESSOR_ENV = "HERMES_OVERLAP_SUCCESSOR"
OVERLAP_PREDECESSOR_PID_ENV = "HERMES_OVERLAP_PREDECESSOR_PID"
GATEWAY_CLAIM_SUCCESSOR_PENDING = "successor-pending"
SUCCESSOR_WATCH_POLL_SECONDS = 2.0
SUCCESSOR_RESPAWN_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
# Hub-capable adapters expose their own attach timeout; this is the fallback
# for adapters that do not.
DEFAULT_OVERLAP_SUCCESSOR_ATTACH_TIMEOUT_SECONDS = 300.0


# The launch markers are popped from os.environ right after the PID claim (so
# tool children never inherit them), but the hub attach that proves this
# process is an attested successor happens later, when adapters connect.
# Keep the predecessor here until this process becomes authoritative.
_pending_predecessor_pid: Optional[int] = None


def remember_pending_predecessor(pid: Optional[int]) -> None:
    global _pending_predecessor_pid
    _pending_predecessor_pid = pid if pid and pid > 0 else None


def forget_pending_predecessor() -> None:
    remember_pending_predecessor(None)


def overlap_predecessor_pid() -> Optional[int]:
    """Return the expected predecessor for a hub-spawned successor, else None."""
    if os.environ.get(OVERLAP_SUCCESSOR_ENV) != "1":
        return _pending_predecessor_pid
    try:
        pid = int(os.environ.get(OVERLAP_PREDECESSOR_PID_ENV, ""))
    except ValueError:
        return None
    return pid if pid > 0 else None


def overlap_attachment() -> dict[str, Any]:
    """Identify this process to the hub; the hub still must have spawned it."""
    from gateway.status import get_process_start_time
    from hermes_constants import get_process_hermes_home

    payload: dict[str, Any] = {
        "gateway_pid": os.getpid(),
        "gateway_start_time": get_process_start_time(os.getpid()),
        "gateway_home": str(get_process_hermes_home()),
    }
    predecessor = overlap_predecessor_pid()
    if predecessor is not None:
        payload["overlap_predecessor_pid"] = predecessor
    return payload


def defer_adapter_until_authoritative(runner: Any, adapter: Any) -> bool:
    """Only transports with a shared, multi-generation hub may connect early."""
    return bool(
        getattr(runner, "_overlap_successor_pending", False)
        and getattr(adapter, "supports_overlap_restart", False) is not True
    )


def _overlap_holder_pid(session_id: str) -> Optional[int]:
    from hermes_cli.active_sessions import active_session_registry_snapshot

    for entry in active_session_registry_snapshot() or []:
        if str(entry.get("session_id") or "") != session_id:
            continue
        try:
            pid = int(entry.get("pid") or -1)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None
    return None


def _runner_predecessor_pid(runner: Any) -> int:
    try:
        predecessor = int(getattr(runner, "_overlap_predecessor_pid", None) or 0)
    except (TypeError, ValueError):
        predecessor = 0
    if predecessor <= 0:
        predecessor = overlap_predecessor_pid() or 0
    return predecessor


def drop_inbound_session_refusal_during_overlap(
    runner: Any, refusal: Any, session_id: str = "",
) -> bool:
    """During overlap handoff the predecessor still holds the cross-process lease.

    The occupancy copy is for a real second writer (CLI vs gateway), not the
    same bot's successor process waiting on ``claim_when_predecessor_exits``.
    Silence only when the live holder is that known predecessor.
    """
    if not getattr(runner, "_overlap_successor_pending", False):
        return False
    from hermes_cli.active_sessions import SESSION_NOT_OWNED

    if getattr(refusal, "reason", None) != SESSION_NOT_OWNED:
        return False
    predecessor = _runner_predecessor_pid(runner)
    if predecessor <= 0 or not session_id:
        return False
    return _overlap_holder_pid(session_id) == predecessor


def defer_primary_adapter(runner: Any, pending: tuple) -> None:
    deferred = getattr(runner, "_overlap_deferred_primary_adapters", None)
    if deferred is None:
        deferred = runner._overlap_deferred_primary_adapters = []
    deferred.append(pending)
    platform = pending[0]
    logger.info(
        "%s deferred until overlap predecessor exits and this successor claims the gateway lock",
        platform.value,
    )


def defer_secondary_adapter(runner: Any, profile_name: str, platform: Any) -> None:
    runner._overlap_deferred_secondary_adapters = True
    logger.info(
        "%s deferred for profile '%s' until overlap predecessor exits and this successor claims the gateway lock",
        platform.value, profile_name,
    )


async def start_deferred_adapters(runner: Any) -> None:
    """Start adapters held back during the non-authoritative overlap window."""
    pending = getattr(runner, "_overlap_deferred_primary_adapters", [])
    runner._overlap_deferred_primary_adapters = []
    if pending:
        raw = await runner._start_connect_pending(pending)
        if raw:
            retryable: list[str] = []
            nonretryable: list[str] = []
            await runner._start_aggregate_connect_results(raw, retryable, nonretryable)
            for message in (*retryable, *nonretryable):
                logger.error("Deferred platform startup after overlap handoff: %s", message)
    if getattr(runner, "_overlap_deferred_secondary_adapters", False):
        runner._overlap_deferred_secondary_adapters = False
        await runner._start_secondary_profile_adapters()


def _shutdown_requested(runner: Any) -> bool:
    event = getattr(runner, "_shutdown_event", None)
    return bool(event is not None and event.is_set())


async def claim_when_predecessor_exits(
    runner: Any,
    predecessor_pid: int,
    on_authoritative: Callable[[], Awaitable[None]],
    *,
    poll_seconds: float = 0.2,
) -> bool:
    """Wait for the old process, then win the normal lock/PID claim or stop cleanly."""
    from gateway.status import _pid_exists

    while _pid_exists(predecessor_pid) and not _shutdown_requested(runner):
        await asyncio.sleep(poll_seconds)
    while not _shutdown_requested(runner):
        # Late import: run.py owns the single normal claim path and imports this module.
        from gateway.run import _start_gateway_claim_pid_file

        reason = "Overlap successor lost the gateway lock/PID claim to another gateway; exiting"
        try:
            claim = _start_gateway_claim_pid_file(predecessor_pid)
        except Exception:
            logger.exception("Overlap successor's deferred gateway lock/PID claim failed; exiting")
            runner._exit_with_failure = True
            reason = "Overlap successor could not complete the gateway lock/PID claim; exiting"
            claim = False
        if claim is True:
            runner._overlap_successor_pending = False
            forget_pending_predecessor()
            logger.warning(
                "Overlap predecessor PID %d exited; successor PID %d is now the authoritative gateway",
                predecessor_pid, os.getpid(),
            )
            try:
                await on_authoritative()
                from gateway.restart_chain import run_chained_restart

                await run_chained_restart(runner)
            except Exception:
                logger.exception("Overlap successor became authoritative but post-claim startup failed; exiting")
                runner._exit_with_failure = True
                stop = getattr(runner, "stop", None)
                if callable(stop):
                    result = stop()
                    if inspect.isawaitable(result):
                        await result
                return False
            return True
        if claim == GATEWAY_CLAIM_SUCCESSOR_PENDING:
            await asyncio.sleep(poll_seconds)
            continue
        logger.warning(reason)
        for adapter in _iter_adapters(runner):
            if getattr(adapter, "supports_overlap_restart", False) is not True:
                continue
            disconnect = getattr(adapter, "disconnect", None)
            if not callable(disconnect):
                continue
            try:
                result = disconnect()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Could not detach losing overlap successor from its hub")
        runner._exit_cleanly = True
        runner._exit_reason = reason
        stop = getattr(runner, "stop", None)
        if callable(stop):
            result = stop()
            if inspect.isawaitable(result):
                await result
        return False
    return False


def _source_adapter(runner: Any, source: Any) -> Optional[Any]:
    """The intake adapter serving *source*, else None when unresolvable."""
    resolver = getattr(runner, "_intake_adapter_for", None)
    adapter = resolver(source) if callable(resolver) else None
    if adapter is None:
        resolver = getattr(runner, "_adapter_for_source", None)
        adapter = resolver(source) if callable(resolver) else None
    return adapter


def _overlap_adapter(runner: Any, source: Any) -> Optional[Any]:
    adapter = _source_adapter(runner, source)
    return adapter if getattr(adapter, "supports_overlap_restart", False) is True else None


def admits_owned_work(runner: Any, source: Any) -> bool:
    adapter = _overlap_adapter(runner, source)
    if adapter is None:
        return False
    predicate = getattr(adapter, "admits_owned_work_while_draining", None)
    if not callable(predicate) or not predicate(source):
        return False
    session_key_for_source = getattr(runner, "_session_key_for_source", None)
    if not callable(session_key_for_source):
        return False
    session_key = session_key_for_source(source)
    return (getattr(runner, "_running_agents", None) or {}).get(session_key) is not None


def admits_local_drain_turn(runner: Any, source: Any) -> bool:
    """True when a drain-window event must run as a normal turn in THIS process.

    Only hub adapters (``supports_overlap_restart``) can hand an event to the
    overlap successor; every other adapter (WhatsApp bridge, webhook,
    api_server, ...) is deferred by the successor until the predecessor exits
    (``defer_primary_adapter`` / ``defer_secondary_adapter``). During the
    restart drain window — admission closed but adapters still connected
    (``_running``, no ``stop()`` task yet) — the draining process is therefore
    the only one that can serve those platforms, and refusing the event loses
    it for as long as the drain lasts. Hub adapters keep the handoff path.
    A source with NO resolvable adapter (restored row, hand-built/internal
    source) keeps the old refusal: nothing proves which transport would serve
    it, and those lanes have their own durable recovery. An external quiesce
    (``_external_drain_active``) and terminal shutdown (``_running`` cleared
    or ``stop()`` already tasked, adapters disconnecting) keep their refusal
    too — only the overlap-drain window opens.
    """
    if not getattr(runner, "_draining", False):
        return False
    if getattr(runner, "_external_drain_active", False):
        return False
    if not getattr(runner, "_running", False):
        return False
    if getattr(runner, "_stop_task", None) is not None or _shutdown_requested(runner):
        return False
    adapter = _source_adapter(runner, source)
    return adapter is not None and getattr(adapter, "supports_overlap_restart", False) is not True


async def handoff_overlap_event(
    runner: Any, event: Any, session_key: str, *,
    target_pid: Optional[int] = None,
    require_successor_ready: bool = False,
) -> bool:
    """Return an unowned event to its shared hub instead of dropping or queueing it.

    Before the successor generation has attached, the hub still accepts the
    handoff: it retains the record with no eligible delivery generation and
    replays it on the successor's attach. The handoff is therefore attempted
    immediately even while ``admits_owned_work_while_draining`` is still False.
    Only a handoff the hub itself refused (a newer delivery won the version
    fence, the hub connection died) is retried, bounded by the
    successor-attach deadline; after that deadline the event may fall through
    to the caller's durable drain queue — the crash-only fallback for a hub
    that will never route it.
    """
    adapter = _overlap_adapter(runner, event.source)
    if adapter is None:
        return False
    if (
        require_successor_ready
        and target_pid is None
        and getattr(runner, "_draining", False)
        and getattr(runner, "_restart_task_started", False)
        and not getattr(runner, "_overlap_restart_successor_started", False)
        and not getattr(runner, "_overlap_successor_spawning", False)
    ):
        # Successor-less drain: the hub would exclude this generation and
        # retain the event with no one left to deliver it to. Keep it local.
        return False
    ready = getattr(adapter, "admits_owned_work_while_draining", None)
    handoff = getattr(adapter, "handoff_event", None)
    if not callable(handoff):
        return False
    park_deadline: Optional[float] = None
    retry_delay = 0.05
    while True:
        if await handoff(event, session_key, target_pid=target_pid) is True:
            return True
        event._gateway_accepted = False
        if not require_successor_ready:
            return False
        if callable(ready) and ready(event.source):
            # Ready successor: an unbounded retry is correct below.
            park_deadline = None
        else:
            if park_deadline is None:
                park_deadline = time.monotonic() + float(getattr(
                    adapter, "overlap_successor_attach_timeout",
                    DEFAULT_OVERLAP_SUCCESSOR_ATTACH_TIMEOUT_SECONDS,
                ))
            elif time.monotonic() >= park_deadline:
                logger.error(
                    "Hub handoff for %s gave up after the successor-attach "
                    "deadline; leaving the event to the durable drain queue",
                    session_key,
                )
                return False
        # A newer delivery can win the version fence, or the hub connection can
        # fail transiently. Keep the event live and unacknowledged until routing
        # succeeds; falling through would recreate the drain ack/queue bug.
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 1.0)


async def handoff_to_overlap_predecessor(
    runner: Any, event: Any, session_key: str,
) -> bool:
    predecessor = _runner_predecessor_pid(runner)
    return predecessor > 0 and await handoff_overlap_event(
        runner, event, session_key, target_pid=predecessor,
    )


_RECONNECT_WAIT_SECONDS = 120.0


def _has_overlap_adapter(runner: Any) -> bool:
    return any(
        getattr(adapter, "supports_overlap_restart", False) is True
        for adapter in _iter_adapters(runner)
    )


_HUB_TRANSPORTS = frozenset({"discord", "matrix"})


def _reconnecting_overlap_adapter(runner: Any) -> bool:
    """True while a hub transport sits in the reconnect queue."""
    for platform in (getattr(runner, "_failed_platforms", None) or {}):
        if str(getattr(platform, "value", platform)).lower() in _HUB_TRANSPORTS:
            return True
    return False


async def _wait_for_reconnecting_overlap_adapters(
    runner: Any, timeout: float = _RECONNECT_WAIT_SECONDS, poll: float = 0.5,
) -> None:
    if _has_overlap_adapter(runner) or not _reconnecting_overlap_adapter(runner):
        return
    logger.info("Overlap restart waiting for hub adapters to reconnect before spawning a successor")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        if _has_overlap_adapter(runner):
            return
    logger.warning("Hub adapters did not reconnect within %.0fs", timeout)


def _iter_adapters(runner: Any):
    seen = set()
    collections = [getattr(runner, "adapters", {})]
    collections.extend((getattr(runner, "_profile_adapters", {}) or {}).values())
    for collection in collections:
        for adapter in collection.values():
            if id(adapter) not in seen:
                seen.add(id(adapter))
                yield adapter


def _overlap_successor_identity_state(runner: Any, successor_pid: int) -> str:
    """Return live, dead, or unknown without guessing on a missing fingerprint."""
    from gateway.status import _pid_exists, get_process_start_time

    if not _pid_exists(successor_pid):
        return "dead"
    expected_start = getattr(runner, "_overlap_restart_successor_start_time", None)
    current_start = get_process_start_time(successor_pid)
    if current_start is None or expected_start is None:
        return "unknown"
    # A different incarnation at the same PID proves our successor is gone.
    return "live" if current_start == expected_start else "dead"


def handover_systemd_overlap_successor(runner: Any, planned_exit_code: Optional[int]) -> bool:
    """Return whether exit 76 is safe for the original, still-live successor.

    A service-cgroup successor additionally needs a successful MAINPID transfer.
    If notify fails while that successor remains alive, keep the predecessor
    process parked here and retry rather than exiting into either a duplicate
    (75) or an untracked survivor (76). A dead/reused successor permits serial
    supervisor replacement by returning False.
    """
    from gateway.restart import GATEWAY_OVERLAP_RESTART_EXIT_CODE

    if planned_exit_code != GATEWAY_OVERLAP_RESTART_EXIT_CODE:
        return False
    successor_pid = int(getattr(runner, "_overlap_restart_successor_pid", 0))
    managed = getattr(runner, "_overlap_successor_systemd_managed", False) is True
    logged_wait = False
    while True:
        identity = _overlap_successor_identity_state(runner, successor_pid)
        if identity == "dead":
            logger.error(
                "Overlap successor PID %d is dead or its PID was reused; "
                "allowing serial supervisor replacement",
                successor_pid,
            )
            return False
        if identity == "live":
            if not managed:
                return True
            from gateway.systemd_notify import handover_main_pid

            if handover_main_pid(successor_pid):
                # Close the check/notify race: a successor that died during the
                # transfer must not authorize the predecessor's exit 76.
                identity = _overlap_successor_identity_state(runner, successor_pid)
                if identity == "live":
                    return True
                if identity == "dead":
                    return False
            if not logged_wait:
                logger.error(
                    "Could not hand systemd MainPID to live overlap successor PID %d; "
                    "keeping the predecessor parked and retrying without spawning another gateway",
                    successor_pid,
                )
                logged_wait = True
        elif not logged_wait:
            logger.error(
                "Overlap successor PID %d has no verifiable process fingerprint; "
                "keeping the predecessor parked rather than risking a duplicate",
                successor_pid,
            )
            logged_wait = True
        # The shutdown watchdog's hard exit would defeat the no-duplicate
        # recovery if notify stays unavailable. Parking is now intentional.
        watchdog_done = getattr(runner, "_shutdown_watchdog_done", None)
        if watchdog_done is not None:
            watchdog_done.set()
        time.sleep(0.2)


async def spawn_successor(
    runner: Any, *, via_service: bool, restart_platforms: bool = True,
) -> Optional[int]:
    """Start one successor for this gateway process, before its drain wait.

    ``restart_platforms=False`` (``gateway.restart_platforms: false``) skips the
    successor spawn through platform-adapter hubs entirely: platform gateway
    connections are not bounced as part of the restart — they stay attached to
    the predecessor through its drain and reconnect on whatever process follows.
    """
    if getattr(runner, "_overlap_restart_successor_started", False):
        return getattr(runner, "_overlap_restart_successor_pid", None)
    if not restart_platforms:
        logger.info(
            "restart_platforms=false: skipping the platform successor spawn — "
            "agent-core restarts without bouncing platform gateway connections"
        )
        return None
    # A hub bounce (or any transient hub drop) pulls hub adapters out of
    # runner.adapters until the reconnect watcher brings them back. Spawning
    # in that window used to find no adapter and silently fall back to a
    # successor-less drain, which hands every new event back to a hub with
    # nowhere to deliver it. Wait for the reconnect instead.
    await _wait_for_reconnecting_overlap_adapters(runner)
    for adapter in _iter_adapters(runner):
        if getattr(adapter, "supports_overlap_restart", False) is not True:
            continue
        spawn = getattr(adapter, "spawn_overlap_successor", None)
        if not callable(spawn):
            continue
        readiness_failed = False
        try:
            pid = await spawn(via_service=via_service, predecessor_pid=os.getpid())
        except Exception:
            # Adapters latch immediately after Popen, before registration and
            # readiness waits. Never forget a live child and spawn a second one.
            pid = getattr(adapter, "_last_overlap_successor_pid", None)
            if pid is None:
                raise
            readiness_failed = True
            logger.exception(
                "Overlap successor was spawned but readiness confirmation failed; "
                "retaining the child as the restart successor"
            )
        if pid is None:
            continue
        runner._overlap_restart_successor_started = True
        runner._overlap_restart_successor_pid = int(pid)
        runner._overlap_successor_systemd_managed = (
            getattr(adapter, "_last_overlap_successor_service_managed", False) is True
        )
        successor_start = getattr(adapter, "_last_overlap_successor_start_time", None)
        if successor_start is None:
            from gateway.status import get_process_start_time

            successor_start = get_process_start_time(int(pid))
        runner._overlap_restart_successor_start_time = successor_start
        if not readiness_failed:
            for candidate in _iter_adapters(runner):
                if getattr(candidate, "supports_overlap_restart", False) is not True:
                    continue
                wait_ready = getattr(candidate, "wait_for_overlap_successor", None)
                if callable(wait_ready):
                    await wait_ready()
                mark_ready = getattr(candidate, "mark_overlap_successor_ready", None)
                if callable(mark_ready):
                    mark_ready()
        logger.info("Overlapping gateway successor started as pid %s", pid)
        return int(pid)
    logger.warning(
        "Overlap restart found no connected hub adapter to spawn a successor; "
        "falling back to the legacy drain path"
    )
    return None
    return None


def _predecessor_still_draining(runner: Any) -> bool:
    """True while this process is a predecessor whose drain has not reached stop()."""
    return bool(
        getattr(runner, "_draining", False)
        and getattr(runner, "_restart_requested", False)
        and getattr(runner, "_stop_task", None) is None
        and not _shutdown_requested(runner)
    )


def mark_successor_lost(runner: Any) -> None:
    """Forget a dead successor so routing, exit codes and respawn see the truth.

    With the started flag cleared, ``resolve_planned_restart_exit_code`` falls
    back to the ordinary service-restart code, so even if no replacement can be
    started the supervisor revives the gateway instead of trusting a corpse.
    """
    for adapter in _iter_adapters(runner):
        if getattr(adapter, "supports_overlap_restart", False) is not True:
            continue
        lost = getattr(adapter, "mark_overlap_successor_lost", None)
        if callable(lost):
            lost()
    runner._overlap_restart_successor_started = False
    runner._overlap_restart_successor_pid = None


async def watch_successor(
    runner: Any,
    *,
    via_service: bool,
    poll_seconds: float = SUCCESSOR_WATCH_POLL_SECONDS,
    backoff: tuple = SUCCESSOR_RESPAWN_BACKOFF_SECONDS,
) -> None:
    """Keep exactly one live successor for as long as this predecessor drains.

    A successor can die underneath a long drain (hub restart kills its cgroup,
    the tripwire, OOM). Without this, the predecessor keeps answering every new
    message with the queued-for-next-turn notice until its last turn ends.
    """
    from gateway.status import _pid_exists

    attempt = 0
    delay = poll_seconds
    while True:
        # Always yield first: the successor needs a moment to exist, and a
        # respawn that "succeeds" with an instantly dead PID must not spin.
        await asyncio.sleep(delay)
        delay = poll_seconds
        if not _predecessor_still_draining(runner):
            return
        pid = getattr(runner, "_overlap_restart_successor_pid", None)
        if getattr(runner, "_overlap_restart_successor_started", False) and pid and _pid_exists(int(pid)):
            continue
        if pid:
            logger.error(
                "Overlap successor PID %s died while predecessor PID %d is still draining; "
                "starting a replacement successor", pid, os.getpid(),
            )
            mark_successor_lost(runner)
        try:
            new_pid = await spawn_successor(runner, via_service=via_service)
        except Exception:
            logger.exception("Replacement overlap successor could not start; retrying")
            new_pid = None
        if new_pid is not None:
            attempt = 0
            continue
        delay = backoff[min(attempt, len(backoff) - 1)]
        attempt += 1
