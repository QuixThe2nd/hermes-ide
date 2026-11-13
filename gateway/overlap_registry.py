"""Hub-owned attestation for overlapping gateway successor processes.

The registry lives inside the transport hub.  The local supervision helper
reads it through the authenticated same-user hub IPC; an environment marker or
a PID file is not proof that the hub spawned a child.

When given a ``state_path`` the hub also mirrors its records into that file
(inside the hub's 0700 state directory, next to its socket) so a hub restart
does not orphan a successor it spawned.  A reloaded record only counts while
the exact process it names (PID plus kernel start time) is still alive, so a
stale or hand-written entry cannot vouch for a reused or foreign PID.
"""

from __future__ import annotations

import json
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

    def __init__(self, *, startup_grace_seconds: float = _STARTUP_GRACE_SECONDS,
                 state_path: Optional[Path] = None) -> None:
        self._records: Dict[int, _Successor] = {}
        self._startup_grace_seconds = float(startup_grace_seconds)
        self._state_path = Path(state_path) if state_path is not None else None
        self._load()

    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            rows = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now_mono, now_wall = time.monotonic(), time.time()
        for row in rows if isinstance(rows, list) else []:
            try:
                pid = int(row["pid"])
                start_time = row.get("start_time")
                start_time = int(start_time) if start_time is not None else None
                # Without a fingerprint a reloaded PID proves nothing.
                if not self._live_start(pid, start_time):
                    continue
                age = max(0.0, now_wall - float(row.get("spawned_wall") or now_wall))
                self._records[pid] = _Successor(
                    pid=pid,
                    start_time=start_time,
                    gateway_home=str(row["gateway_home"]),
                    predecessor_pid=int(row["predecessor_pid"]),
                    spawned_at=now_mono - age,
                    generations=set(row.get("generations") or []),
                    attached=bool(row.get("attached")),
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._save()

    def _save(self) -> None:
        if self._state_path is None:
            return
        now_mono, now_wall = time.monotonic(), time.time()
        rows = [{
            "pid": r.pid,
            "start_time": r.start_time,
            "gateway_home": r.gateway_home,
            "predecessor_pid": r.predecessor_pid,
            "spawned_wall": now_wall - max(0.0, now_mono - r.spawned_at),
            "generations": sorted(r.generations),
            "attached": r.attached,
        } for r in self._records.values()]
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

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
        self._save()

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
        self._save()

    def snapshot(self) -> list[Dict[str, Any]]:
        """Return only live, boot-grace or attached records; prune everything else."""
        now = time.monotonic()
        before = len(self._records)
        result: list[Dict[str, Any]] = []
        for pid, record in list(self._records.items()):
            age = max(0.0, now - record.spawned_at)
            if record.start_time is None:
                # Popen can return before the child's fingerprint is available.
                # Keep the spawn proof for attach during boot grace, but do not
                # attest an unverified process as live.
                if age > self._startup_grace_seconds:
                    self._records.pop(pid, None)
                continue
            if not self._live_start(pid, record.start_time):
                self._records.pop(pid, None)
                continue
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
        if len(self._records) != before:
            self._save()
        return result
