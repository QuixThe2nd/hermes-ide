"""Session housekeeping / stall / catalog-refresh watcher loops, bound onto ``GatewayRunner`` via the MRO.

``gateway.run`` internals are imported lazily inside method bodies (import cycle), so
``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from typing import Any, Dict, Optional

from gateway.session_stall import (
    format_session_stall_notification,
    resolve_session_idle_seconds_from_activity,
    should_clear_session_stall_notification,
    should_emit_session_stall_notification,
)

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

_SESSION_STORE_PRUNE_INTERVAL = 3600.0  # once per hour


async def _interruptible_sleep(runner, seconds: int) -> None:
    """Sleep in 1s increments so the watcher stops quickly when ``runner._running`` flips."""
    for _ in range(seconds):
        if not runner._running:
            break
        await asyncio.sleep(1)


class GatewaySessionWatchersMixin:
    """Session housekeeping / stall / catalog-refresh watcher loops for GatewayRunner."""

    async def _session_housekeeping_watcher(self, interval: int = 300):
        """Reclaim resources without ending durable conversations."""
        await asyncio.sleep(60)
        while self._running:
            try:
                await self._session_housekeeping()
            except Exception as e:
                logger.debug("Session housekeeping error: %s", e)
            await _interruptible_sleep(self, interval)

    async def _session_housekeeping(self) -> None:
        """Idle/pressure agent-cache sweeps plus the hourly SessionStore prune."""
        try:
            if evicted := self._sweep_idle_cached_agents():
                logger.info("Agent cache idle sweep: evicted %d agent(s)", evicted)
        except Exception as e:
            logger.debug("Idle agent sweep failed: %s", e)
        # Neither LRU cap nor idle TTL knows what a cached transcript costs in memory.
        try:
            # Neither the LRU cap nor the idle TTL is aware of how much memory a cached transcript costs, so
            # a busy gateway keeps every warm session's tool output resident until RSS hits the cgroup limit
            # (#80764). Shed LRU transcripts once the heap is over budget; they reload from the persisted
            # session on the next turn.
            self._sweep_agent_cache_under_pressure()
        except Exception as e:
            logger.debug("Agent cache pressure sweep failed: %s", e)
        # Prune stale SessionStore entries: the dict + sessions.json otherwise grow unbounded.
        prune_ts = getattr(self, "_last_session_store_prune_ts", 0.0)  # tests may omit
        if time.time() - prune_ts > _SESSION_STORE_PRUNE_INTERVAL:
            try:
                max_age = int(getattr(self.config, "session_store_max_age_days", 0) or 0)
                if max_age > 0 and (n := await self.async_session_store.prune_old_entries(max_age)):
                    logger.info("SessionStore prune: dropped %d stale entries", n)
            except Exception as e:
                logger.debug("SessionStore prune failed: %s", e)
            self._last_session_store_prune_ts = time.time()

    def _session_stall_timeout_seconds(self) -> float:
        """Return configured stall timeout (seconds); 0 disables the watchdog."""
        from gateway.run import _float_env

        return _float_env("HERMES_SESSION_STALL_TIMEOUT", 300)

    def _session_activity_for_stall(self, session_key: str) -> Optional[dict]:
        """Return the shared activity snapshot for stall progress (#72039).

        Single progress source: ``AIAgent.get_activity_summary()`` /
        ``agent.session_activity``. No turn-start or pending-inbound clocks.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL

        agent = (getattr(self, "_running_agents", None) or {}).get(session_key)
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return None
        if not hasattr(agent, "get_activity_summary"):
            return None
        try:
            summary = agent.get_activity_summary()
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None


    async def _check_session_stalls(self, timeout_seconds: float) -> int:
        """Scan pending inbound sessions and notify once per stall episode.

        Returns the number of notifications sent this pass (for tests).
        """
        from gateway.session_stall import (
            format_session_stall_notification,
            resolve_session_idle_seconds_from_activity,
            should_clear_session_stall_notification,
            should_emit_session_stall_notification,
        )
        from gateway.run import _STALL_NOTIFY_SEND_TIMEOUT_SECONDS

        notified_map = getattr(self, "_session_stall_notified", None)
        if notified_map is None:
            notified_map = {}
            self._session_stall_notified = notified_map

        sent = 0
        now = time.time()
        candidates: Dict[str, tuple[Any, Any]] = {}

        for adapter in self._iter_gateway_adapters():
            pending_slot = getattr(adapter, "_pending_messages", None) or {}
            for session_key, event in list(pending_slot.items()):
                if session_key and session_key not in candidates and event is not None:
                    candidates[session_key] = (adapter, event)

        for session_key, overflow in list(
            (getattr(self, "_queued_events", None) or {}).items()
        ):
            if not session_key or session_key in candidates or not overflow:
                continue
            event = overflow[0]
            source = getattr(event, "source", None)
            adapter = (
                self._adapter_for_source(source) if source is not None else None
            )
            if adapter is None:
                continue
            candidates[session_key] = (adapter, event)

        for session_key, (adapter, pending_event) in list(candidates.items()):
            has_pending = pending_event is not None
            activity = (
                self._session_activity_for_stall(session_key) if has_pending else None
            )
            idle_seconds = (
                resolve_session_idle_seconds_from_activity(activity, now=now)
                if has_pending
                else None
            )
            already = bool(notified_map.get(session_key))
            if should_clear_session_stall_notification(
                timeout_seconds=timeout_seconds,
                idle_seconds=idle_seconds,
                has_pending_inbound=has_pending,
            ):
                notified_map.pop(session_key, None)
                already = False
            if not should_emit_session_stall_notification(
                timeout_seconds=timeout_seconds,
                idle_seconds=idle_seconds,
                has_pending_inbound=has_pending,
                already_notified=already,
            ):
                continue

            if idle_seconds is None:
                continue
            mins = max(1, int(idle_seconds // 60))
            activity = activity or {}
            logger.warning(
                "Session stall detected: session=%s idle=%.0fs "
                "(timeout=%.0fs, ~%d min); pending inbound present "
                "| last_activity=%s | provenance=%s "
                "(agent.session_stall_timeout)",
                session_key,
                idle_seconds,
                timeout_seconds,
                mins,
                activity.get("last_activity_desc")
                or activity.get("last_activity_description")
                or "unknown",
                activity.get("provenance")
                or activity.get("last_activity_provenance")
                or "unknown",
            )
            source = getattr(pending_event, "source", None)
            chat_id = getattr(source, "chat_id", None) if source is not None else None
            if not chat_id:
                logger.warning(
                    "Session stall notify skipped (no chat_id): session=%s",
                    session_key,
                )
                # Cannot deliver; latch to avoid log spam every tick.
                notified_map[session_key] = True
                continue
            # #76354 review S2: re-read pending state + activity timestamp
            # IMMEDIATELY before delivery. The snapshot above ages while
            # earlier candidates in this pass await their sends; an agent
            # that made progress (or drained its queue) in that window must
            # not receive a false stall notice. Abort and leave the latch
            # un-set so the next tick re-evaluates from scratch.
            still_pending = (
                (getattr(adapter, "_pending_messages", None) or {}).get(
                    session_key
                )
                is not None
                or bool(
                    (getattr(self, "_queued_events", None) or {}).get(
                        session_key
                    )
                )
            )
            fresh_idle = resolve_session_idle_seconds_from_activity(
                self._session_activity_for_stall(session_key),
                now=time.time(),
            )
            if not still_pending or (
                fresh_idle is not None and fresh_idle < timeout_seconds
            ):
                logger.info(
                    "Session stall notify aborted (no longer stale): "
                    "session=%s pending=%s fresh_idle=%s",
                    session_key,
                    still_pending,
                    fresh_idle,
                )
                # Re-arm: drop any stale latch so a FUTURE genuine stall
                # episode notifies again.
                notified_map.pop(session_key, None)
                continue
            try:
                metadata = (
                    self._thread_metadata_for_source(source)
                    if source is not None and hasattr(self, "_thread_metadata_for_source")
                    else None
                )
                # Round-2 #2: bound the send. A wedged adapter transport
                # (network hang, dead websocket) must not block the whole
                # watcher pass — sibling candidates in this loop would never
                # be evaluated and the watcher itself would stop ticking.
                try:
                    result = await asyncio.wait_for(
                        adapter.send(
                            str(chat_id),
                            format_session_stall_notification(idle_seconds),
                            metadata=metadata,
                        ),
                        timeout=_STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session stall notify send timed out after %.0fs "
                        "for %s; will retry next tick",
                        _STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                        session_key,
                    )
                    continue  # do not latch; retry next tick
                # Adapters often return SendResult(success=False) instead of raising.
                if result is not None and getattr(result, "success", True) is False:
                    logger.warning(
                        "Session stall notify failed for %s: %s",
                        session_key,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue  # do not latch; retry next tick
                sent += 1
                notified_map[session_key] = True
            except Exception as exc:
                logger.warning(
                    "Session stall notify failed for %s: %s",
                    session_key,
                    exc,
                )
                # Do not latch — retry next watcher tick until delivery or episode clear.

        # Drop latches for sessions that no longer appear in any pending map.
        for key in list(notified_map.keys()):
            if key not in candidates:
                notified_map.pop(key, None)

        return sent


    async def _model_catalog_refresh_watcher(self) -> None:
        """Refresh the /model picker's remote catalogs every TTL window.

        The picker itself only refreshes on a cold or stale open, so a
        gateway that nobody opens ``/model`` in keeps serving whatever was
        cached. This loop calls ``model_catalog.refresh_catalogs()`` (manifest
        + OpenRouter live filter + Nous Portal recommendations) off-thread on
        the configured cadence (``model_catalog.ttl_minutes``, default 20) so
        the on-disk caches every surface reads are never older than one window.
        """
        from hermes_cli.model_catalog import refresh_catalogs, refresh_interval_seconds

        await asyncio.sleep(30)  # let startup settle
        while self._running:
            try:
                await asyncio.to_thread(refresh_catalogs)
            except Exception as exc:
                logger.debug("Model catalog refresh failed: %s", exc)
            try:
                interval = refresh_interval_seconds()
            except Exception:
                interval = 1200.0
            deadline = time.monotonic() + interval
            while self._running and time.monotonic() < deadline:
                await asyncio.sleep(min(30.0, max(0.0, deadline - time.monotonic())))

    async def _session_stall_watcher(self, interval: float = 30.0):
        """Periodic pending-inbound + stale-activity stall watchdog (#72016).

        Progress comes only from ``get_activity_summary()`` (#72039).
        Pending inbound is a notify policy gate, not a progress clock.
        Notify-only: does not kill the turn (contrast ``gateway_timeout`` /
        ``shutdown_watchdog``).
        """
        # Short initial delay so startup reconnect noise does not false-fire.
        await asyncio.sleep(min(30.0, max(1.0, float(interval))))
        while self._running:
            try:
                timeout = self._session_stall_timeout_seconds()
                if timeout > 0:
                    await self._check_session_stalls(timeout)
            except Exception as exc:
                logger.debug("Session stall watcher error: %s", exc)
            # Interruptible sleep
            steps = max(1, int(float(interval)))
            for _ in range(steps):
                if not self._running:
                    break
                await asyncio.sleep(1)
