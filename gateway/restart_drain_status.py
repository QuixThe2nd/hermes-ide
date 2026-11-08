"""Predecessor-owned restart drain progress during an overlapping handoff."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

_DRAIN_STATUS_FILENAME = "gateway_restart_drain.json"


def _path(home: Optional[Path] = None) -> Path:
    return (Path(home) if home is not None else get_hermes_home()) / _DRAIN_STATUS_FILENAME


def write_restart_drain_status(
    *, active_agents: int, active_work: list[dict[str, Any]],
) -> None:
    """Publish the old generation's count without taking the shared runtime identity."""
    atomic_json_write(
        _path(),
        {
            "pid": os.getpid(),
            "restart_requested": True,
            "active_agents": max(0, int(active_agents)),
            "active_work": active_work,
            "updated_at": time.time(),
        },
        indent=None,
    )


def read_restart_drain_status(home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Read a drain snapshot; corruption is treated as absent, never as drained."""
    try:
        payload = json.loads(_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None
