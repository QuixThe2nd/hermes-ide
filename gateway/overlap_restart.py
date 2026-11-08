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
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

OVERLAP_SUCCESSOR_ENV = "HERMES_OVERLAP_SUCCESSOR"
OVERLAP_PREDECESSOR_PID_ENV = "HERMES_OVERLAP_PREDECESSOR_PID"
GATEWAY_CLAIM_SUCCESSOR_PENDING = "successor-pending"
SUCCESSOR_WATCH_POLL_SECONDS = 2.0
SUCCESSOR_RESPAWN_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)


def overlap_predecessor_pid() -> Optional[int]:
    """Return the expected predecessor for a hub-spawned successor, else None."""
    if os.environ.get(OVERLAP_SUCCESSOR_ENV) != "1":
        return None
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
            logger.warning(
                "Overlap predecessor PID %d exited; successor PID %d is now the authoritative gateway",
                predecessor_pid, os.getpid(),
            )
            try:
                await on_authoritative()
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


def _overlap_adapter(runner: Any, source: Any) -> Optional[Any]:
    resolver = getattr(runner, "_intake_adapter_for", None)
    adapter = resolver(source) if callable(resolver) else None
    if adapter is None:
        resolver = getattr(runner, "_adapter_for_source", None)
        adapter = resolver(source) if callable(resolver) else None
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


async def handoff_overlap_event(
    runner: Any, event: Any, session_key: str, *,
    target_pid: Optional[int] = None,
    require_successor_ready: bool = False,
) -> bool:
    """Return an unowned event to its shared hub instead of dropping or queueing it."""
    adapter = _overlap_adapter(runner, event.source)
    if adapter is None:
        return False
    ready = getattr(adapter, "admits_owned_work_while_draining", None)
    if require_successor_ready and (not callable(ready) or not ready(event.source)):
        return False
    handoff = getattr(adapter, "handoff_event", None)
    if not callable(handoff):
        return False
    retry_delay = 0.05
    while True:
        if await handoff(event, session_key, target_pid=target_pid) is True:
            return True
        event._gateway_accepted = False
        if not require_successor_ready or not ready(event.source):
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


def _iter_adapters(runner: Any):
    seen = set()
    collections = [getattr(runner, "adapters", {})]
    collections.extend((getattr(runner, "_profile_adapters", {}) or {}).values())
    for collection in collections:
        for adapter in collection.values():
            if id(adapter) not in seen:
                seen.add(id(adapter))
                yield adapter


async def spawn_successor(runner: Any, *, via_service: bool) -> Optional[int]:
    """Start one successor for this gateway process, before its drain wait."""
    if getattr(runner, "_overlap_restart_successor_started", False):
        return getattr(runner, "_overlap_restart_successor_pid", None)
    for adapter in _iter_adapters(runner):
        if getattr(adapter, "supports_overlap_restart", False) is not True:
            continue
        spawn = getattr(adapter, "spawn_overlap_successor", None)
        if not callable(spawn):
            continue
        pid = await spawn(via_service=via_service, predecessor_pid=os.getpid())
        if pid is None:
            continue
        runner._overlap_restart_successor_started = True
        runner._overlap_restart_successor_pid = int(pid)
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
