"""Generic adapter hooks for an overlapping gateway restart.

A transport that can keep two gateway generations attached opts in through the
adapter methods below.  The runner stays transport-agnostic: Discord's hub is
one implementation of this capability.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
    return bool(callable(predicate) and predicate(source))


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
        pid = await spawn(via_service=via_service)
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
