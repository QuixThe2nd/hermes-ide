"""Non-destructive request and progress observation for gateway restarts.

An acknowledged restart belongs to the gateway, not to this CLI process. The
observer therefore has no force-after-timeout branch: it reports successor
readiness and predecessor drain changes until the old PID exits, or detaches on
Ctrl-C while the gateway-owned handoff continues.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from gateway.control_socket import query_gateway_control
from gateway.restart_drain_status import read_restart_drain_status
from gateway.status import _pid_exists, read_runtime_status
from hermes_constants import get_hermes_home
from hermes_cli.update_cmd_drain_report import describe_active_work_unit


@dataclass(frozen=True)
class RestartOutcome:
    accepted: bool
    completed: bool = False
    attached: bool = False
    successor_pid: Optional[int] = None


def _home(home: Optional[Path]) -> Path:
    return Path(home) if home is not None else get_hermes_home()


def _state_path(home: Optional[Path]) -> Path:
    return _home(home) / "gateway_state.json"


def _read_status(home: Optional[Path]) -> dict:
    try:
        return read_runtime_status(_state_path(home)) or {}
    except (OSError, ValueError, TypeError):
        return {}


def _connected_platforms(record: dict) -> Optional[str]:
    platforms = record.get("platforms")
    if not isinstance(platforms, dict):
        return None
    connected = sorted(
        str(name) for name, entry in platforms.items()
        if (
            isinstance(entry, dict)
            and entry.get("state") == "connected"
            and entry.get("writer_pid") == record.get("pid")
        )
    )
    if connected:
        return f"{', '.join(connected)} connected"
    return "no platforms connected" if not platforms else None


def request_gateway_restart(
    old_pid: int, *, home: Optional[Path] = None, via_service: bool,
    restart_platforms: bool = True, from_update: bool = False,
) -> RestartOutcome:
    """Attach to a committed restart, else request one through control/SIGUSR1."""
    record = _read_status(home)
    drain_record = read_restart_drain_status(_state_path(home).parent)
    if (
        isinstance(drain_record, dict)
        and drain_record.get("pid") == old_pid
        and drain_record.get("restart_requested") is True
        and (
            _pid_exists(old_pid)
            or (
                isinstance(record.get("pid"), int)
                and record["pid"] > 0
                and record["pid"] != old_pid
            )
        )
    ):
        return RestartOutcome(accepted=True, attached=True)
    if record.get("pid") == old_pid and record.get("restart_requested") is True:
        return RestartOutcome(accepted=True, attached=True)

    control_home = _state_path(home).parent
    if from_update:
        # The post-swap fleet path uses SIGUSR1 so the gateway consumes the
        # update marker and applies update-drain semantics. The pre-swap
        # pause-for-update verb is intentionally a different, non-overlap flow.
        from gateway.status import write_update_restart_marker

        if not write_update_restart_marker(old_pid, target_home=control_home):
            print("  ⚠ Could not mark this restart as update-driven; using ordinary drain semantics")
    else:
        ack = query_gateway_control(
            control_home, "restart",
            params={"restart_platforms": bool(restart_platforms), "via_service": via_service},
        )
        if ack is not None:
            status = str(ack.get("status") or "")
            accepted = bool(
                ack.get("queued") or ack.get("restarting") or ack.get("accepted")
                or ack.get("already_stopping") or status == "already_in_progress"
            )
            return RestartOutcome(
                accepted=accepted,
                attached=bool(ack.get("already_stopping")) or status == "already_in_progress",
            )

    if not hasattr(signal, "SIGUSR1") or old_pid <= 0:
        return RestartOutcome(accepted=False)
    try:
        os.kill(old_pid, signal.SIGUSR1)  # windows-footgun: ok — guarded POSIX compatibility fallback
    except ProcessLookupError:
        return RestartOutcome(accepted=True, attached=True)
    except (PermissionError, OSError):
        return RestartOutcome(accepted=False)
    return RestartOutcome(accepted=True)


def observe_gateway_restart(
    old_pid: int, *, home: Optional[Path] = None,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.5,
) -> RestartOutcome:
    """Observe one handoff indefinitely; only explicit callers may force."""
    successor_pid: Optional[int] = None
    active_agents: Optional[int] = None
    while True:
        record = _read_status(home)
        record_pid = record.get("pid")
        state = record.get("gateway_state")
        if (
            isinstance(record_pid, int) and record_pid > 0 and record_pid != old_pid
            and state == "running" and successor_pid != record_pid
        ):
            platforms = _connected_platforms(record)
            if platforms is not None:
                successor_pid = record_pid
                emit(f"✓ New gateway PID {record_pid} is up ({platforms})")

        drain_record = read_restart_drain_status(_state_path(home).parent)
        if not (
            isinstance(drain_record, dict)
            and drain_record.get("pid") == old_pid
            and drain_record.get("restart_requested") is True
        ):
            drain_record = record if record_pid == old_pid and (
                record.get("restart_requested") is True or state == "draining"
            ) else None
        if drain_record is not None:
            count = drain_record.get("active_agents")
            if isinstance(count, int) and count >= 0 and count != active_agents:
                active_agents = count
                work = drain_record.get("active_work")
                details = [
                    describe_active_work_unit(unit, _state_path(home).parent)
                    for unit in work if isinstance(unit, dict)
                ] if isinstance(work, list) else []
                suffix = f": {', '.join(details)}" if details else ""
                emit(f"⏳ Old gateway PID {old_pid}: waiting on {count} active agent(s){suffix}")

        old_exited = not _pid_exists(old_pid)
        if successor_pid is not None and old_exited:
            emit(f"✓ Old gateway PID {old_pid} exited — restart complete")
            return RestartOutcome(
                accepted=True, completed=True, successor_pid=successor_pid,
            )
        sleep(max(poll_interval, 0.01))


def restart_gateway_with_progress(
    old_pid: int, *, home: Optional[Path] = None, via_service: bool,
    restart_platforms: bool = True, from_update: bool = False,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> RestartOutcome:
    """Request/attach and observe; Ctrl-C is a safe detach, never escalation."""
    requested = request_gateway_restart(
        old_pid, home=home, via_service=via_service,
        restart_platforms=restart_platforms, from_update=from_update,
    )
    if not requested.accepted:
        return requested
    try:
        observed = observe_gateway_restart(old_pid, home=home, emit=emit, sleep=sleep)
    except KeyboardInterrupt:
        emit(f"↳ Detached from restart progress; gateway PID {old_pid} continues draining.")
        return replace(requested, completed=False)
    return replace(observed, attached=requested.attached)
