"""Hub-owned attestation for overlapping gateway successor processes.

The registry is deliberately volatile and lives inside the transport hub.  The
local supervision helper reads it through the authenticated same-user hub IPC;
an environment marker or a PID file is not proof that the hub spawned a child.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.status import get_process_start_time

_STARTUP_GRACE_SECONDS = 300.0


def _home_key(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve())


@dataclass
class _Successor:
    pid: int
    start_time: Optional[int]
    gateway_home: str
    predecessor_pid: int
    spawned_at: float
    generations: set[str] = field(default_factory=set)
    attached: bool = False

    @property
    def ready(self) -> bool:
        return self.attached and bool(self.generations)


class OverlapSuccessorRegistry:
    """Attest live hub children without giving stale/fake markers immunity."""

    def __init__(self, *, startup_grace_seconds: float = _STARTUP_GRACE_SECONDS) -> None:
        self._records: Dict[int, _Successor] = {}
        self._startup_grace_seconds = float(startup_grace_seconds)

    @staticmethod
    def _live_start(pid: int, expected: Optional[int]) -> bool:
        return expected is not None and expected > 0 and get_process_start_time(pid) == expected

    def record_spawn(self, pid: int, gateway_home: str, predecessor_pid: int) -> None:
        if pid <= 0 or predecessor_pid <= 0:
            raise ValueError("successor and predecessor PIDs must be positive")
        self._records[pid] = _Successor(
            pid=pid,
            start_time=get_process_start_time(pid),
            gateway_home=_home_key(gateway_home),
            predecessor_pid=predecessor_pid,
            spawned_at=time.monotonic(),
        )

    def attach(self, *, pid: int, start_time: Optional[int], gateway_home: str,
               generation_id: str, peer_pid: Optional[int] = None) -> None:
        record = self._records.get(pid)
        if record is None:
            raise RuntimeError("overlap successor was not spawned by this transport hub")
        if peer_pid is not None and peer_pid != pid:
            raise RuntimeError("overlap successor socket peer does not match its spawned PID")
        if not self._live_start(pid, start_time):
            raise RuntimeError("overlap successor PID/start identity is stale or unverifiable")
        if record.start_time is not None and record.start_time != start_time:
            raise RuntimeError("overlap successor PID was reused")
        if record.gateway_home != _home_key(gateway_home):
            raise RuntimeError("overlap successor profile home does not match its spawn")
        record.start_time = start_time
        record.generations.add(generation_id)
        record.attached = True

    def snapshot(self) -> list[Dict[str, Any]]:
        """Return only live, boot-grace or attached records; prune everything else."""
        now = time.monotonic()
        result: list[Dict[str, Any]] = []
        for pid, record in list(self._records.items()):
            if not self._live_start(pid, record.start_time):
                self._records.pop(pid, None)
                continue
            age = max(0.0, now - record.spawned_at)
            if not record.ready and age > self._startup_grace_seconds:
                self._records.pop(pid, None)
                continue
            result.append({
                "pid": record.pid,
                "start_time": record.start_time,
                "gateway_home": record.gateway_home,
                "predecessor_pid": record.predecessor_pid,
                "state": "attached" if record.ready else "starting",
                "startup_grace_remaining": max(0.0, self._startup_grace_seconds - age),
                "hub_pid": os.getpid(),
                "hub_start_time": get_process_start_time(os.getpid()),
            })
        return result
