"""Opt-in Discord/Matrix hub bounce across a gateway restart — successor-side only.

``restart_hub`` (the ``restart`` tool, ``/restart --hub``, the control-socket
verb) asks the NEXT gateway process to bounce ``hermes-discord-hub.service``
after it comes up. The dying process must never run systemctl itself — a
SIGTERM to the gateway would kill the child mid-command — so the hand-off is a
small pending marker in HERMES_HOME, written before ``request_restart()`` and
consumed one-shot by the successor before its platform adapters attach.

The bounce is deliberately best-effort: a missing systemctl, a missing unit,
or a hub that never re-attaches is a warning in the restart report, never a
failed gateway restart. The report persists to
``{HERMES_HOME}/.restart_hub_result.json`` so the restart-result composer and
the comeback notice can read it even though they run in other functions; it is
honoured only while fresh (``_STALE_RESULT_MAX_AGE_S``) so a leftover file
from an old restart cycle never annotates an unrelated later one.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from utils import atomic_json_write

logger = logging.getLogger("gateway.run")

HUB_UNIT = "hermes-discord-hub.service"
PENDING_FILENAME = ".restart_hub_pending.json"
RESULT_FILENAME = ".restart_hub_result.json"
# discord/matrix hub_client.py both log "Attached generation %s to <Discord|Matrix> hub pid %s"
ATTACHED_MARKER = "Attached generation"

# Bounded wait for the restarted unit's new MainPID (~10s at 0.5s polls).
_PID_POLL_INTERVAL_S = 0.5
_PID_POLL_ATTEMPTS = 20
# Cap the post-attach marker scan: one restart cycle's log growth, not a whole log.
_LOG_SCAN_MAX_BYTES = 4 * 1024 * 1024
# A result older than this belongs to an earlier restart cycle and is ignored.
_STALE_RESULT_MAX_AGE_S = 300.0

# In-process two-phase hand-off: the bounce (pre-adapter) records the log
# offset it captured, and the attach verification (post-adapter) reads it.
_bounce: dict[str, Any] = {}


def hub_pending_path(home: Path) -> Path:
    return Path(home) / PENDING_FILENAME


def hub_result_path(home: Path) -> Path:
    return Path(home) / RESULT_FILENAME


def write_hub_pending(home: Path, *, requested_at: Optional[float] = None) -> None:
    """Persist the opt-in hand-off: the NEXT gateway process bounces the hub unit."""
    atomic_json_write(
        hub_pending_path(home),
        {"restart_hub": True, "requested_at": time.time() if requested_at is None else requested_at},
        indent=None,
    )


def _read_report(home: Path) -> Optional[dict]:
    try:
        report = json.loads(hub_result_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def _write_report(home: Path, report: dict) -> None:
    try:
        atomic_json_write(hub_result_path(home), report, indent=None)
    except Exception as exc:
        logger.warning("restart-hub: could not persist %s: %s", hub_result_path(home), exc)


def _record(
    home: Path, *, success: bool, warning: Optional[str],
    old_pid: Optional[int], new_pid: Optional[int],
    attached_generation: bool, log_offset: int, now: Callable[[], float] = time.time,
) -> dict:
    report = {
        "success": bool(success),
        "warning": warning,
        "old_pid": old_pid,
        "new_pid": new_pid,
        "attached_generation": bool(attached_generation),
        "at": now(),
    }
    _write_report(home, report)
    _bounce.clear()
    _bounce.update(
        home=Path(home), log_offset=int(log_offset), verify_pending=True,
    )
    return report


def _log_size(log_path: Path) -> int:
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def _read_main_pid(run: Callable[..., Any]) -> tuple[Optional[int], str]:
    """``(MainPID or None, non-empty detail when the probe failed)``."""
    proc = run(
        ["systemctl", "show", HUB_UNIT, "-p", "MainPID", "--value"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or f"exit {proc.returncode}")
        return None, str(detail).strip()
    value = (getattr(proc, "stdout", "") or "").strip()
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None, f"unexpected MainPID value {value!r}"
    if pid <= 0:
        return None, "unit is not running"
    return pid, ""


def consume_restart_hub_bounce(
    home: Path,
    *,
    run: Optional[Callable[..., Any]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    now: Callable[[], float] = time.time,
) -> bool:
    """One-shot successor-side bounce; a second call is a no-op.

    Consumes ``.restart_hub_pending.json`` (that consumption is the idempotence
    — a second call finds no marker), then bounces ``hermes-discord-hub.service``
    via systemctl through the injectable *run*/*sleep*. Never raises and never
    fails the restart: every failure path records a warning report instead.
    Returns True when a bounce was attempted.
    """
    run = subprocess.run if run is None else run
    sleep = time.sleep if sleep is None else sleep
    home = Path(home)
    pending = hub_pending_path(home)
    try:
        raw = pending.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("restart-hub: could not read %s: %s", pending, exc)
        return False
    try:
        pending.unlink()
    except FileNotFoundError:
        return False  # another claimant consumed it first
    except OSError as exc:
        logger.warning("restart-hub: could not consume %s: %s", pending, exc)
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or payload.get("restart_hub") is not True:
        return False  # consumed, but not an opt-in hub bounce marker

    log_offset = _log_size(home / "logs" / "gateway.log")
    logger.info(
        "restart-hub: bouncing %s for the restart requested by the predecessor",
        HUB_UNIT,
    )

    try:
        old_pid, detail = _read_main_pid(run)
    except FileNotFoundError:
        _record(home, success=False, warning="systemctl not found; hub unit was not bounced",
                old_pid=None, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True
    except Exception as exc:
        _record(home, success=False, warning=f"could not inspect {HUB_UNIT}: {exc}",
                old_pid=None, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True
    if old_pid is None:
        _record(home, success=False,
                warning=f"could not read MainPID for {HUB_UNIT} ({detail})",
                old_pid=None, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True

    try:
        proc = run(["systemctl", "restart", HUB_UNIT], capture_output=True, text=True)
    except FileNotFoundError:
        _record(home, success=False, warning="systemctl not found; hub unit was not bounced",
                old_pid=old_pid, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True
    except Exception as exc:
        _record(home, success=False, warning=f"could not run systemctl restart {HUB_UNIT}: {exc}",
                old_pid=old_pid, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True
    if proc.returncode != 0:
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "")
                  or f"exit {proc.returncode}").strip()
        _record(home, success=False, warning=f"systemctl restart {HUB_UNIT} failed: {detail}",
                old_pid=old_pid, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True

    new_pid: Optional[int] = None
    for _ in range(_PID_POLL_ATTEMPTS):
        try:
            pid, _detail = _read_main_pid(run)
        except Exception:
            pid = None
        if pid is not None and pid != old_pid:
            new_pid = pid
            break
        sleep(_PID_POLL_INTERVAL_S)
    if new_pid is None:
        # Restart exited 0 but the MainPID never changed — a warning, never a
        # failed gateway restart.
        _record(home, success=False,
                warning=f"MainPID unchanged after restarting {HUB_UNIT} (pid {old_pid})",
                old_pid=old_pid, new_pid=None, attached_generation=False,
                log_offset=log_offset, now=now)
        return True

    logger.info("restart-hub: %s bounced (pid %s -> %s)", HUB_UNIT, old_pid, new_pid)
    _record(home, success=True, warning=None, old_pid=old_pid, new_pid=new_pid,
            attached_generation=False, log_offset=log_offset, now=now)
    return True


def _log_has_attach_marker(home: Path, offset: int) -> bool:
    try:
        with (home / "logs" / "gateway.log").open("rb") as handle:
            handle.seek(max(0, int(offset)))
            data = handle.read(_LOG_SCAN_MAX_BYTES)
    except OSError:
        return False
    return ATTACHED_MARKER in data.decode("utf-8", errors="replace")


def complete_restart_hub_report(home: Optional[Path] = None, *, now: Callable[[], float] = time.time) -> None:
    """Post-adapter pass: verify the hub attach marker and finalize the report.

    Reads the log offset captured before the bounce (module state set by
    :func:`consume_restart_hub_bounce`), scans only the bytes written since,
    and stamps ``attached_generation`` onto the persisted report. Idempotent;
    never raises.
    """
    if not _bounce.get("verify_pending"):
        return
    _bounce["verify_pending"] = False
    bounce_home = Path(home) if home is not None else _bounce.get("home")
    if bounce_home is None:
        return
    marker_seen = _log_has_attach_marker(bounce_home, int(_bounce.get("log_offset") or 0))
    report = _read_report(bounce_home)
    if report is None:
        return
    report["attached_generation"] = marker_seen
    report["at"] = now()
    _write_report(bounce_home, report)
    logger.info(
        "restart-hub: hub attach marker %s in gateway.log after the bounce",
        "seen" if marker_seen else "not seen",
    )


def hub_report_line(report: Any) -> Optional[str]:
    """The one-line user-facing summary of a hub bounce report."""
    if not isinstance(report, dict) or not isinstance(report.get("success"), bool):
        return None
    old_pid, new_pid = report.get("old_pid"), report.get("new_pid")
    if old_pid and new_pid:
        pid_part = f"pid {old_pid} -> {new_pid}"
    elif new_pid:
        pid_part = f"pid {new_pid}"
    else:
        pid_part = "pid unknown"
    if report.get("success"):
        if report.get("attached_generation"):
            return f"restarted {HUB_UNIT} ({pid_part}); Attached generation seen"
        return (f"warning — restarted {HUB_UNIT} ({pid_part}) but Attached generation "
                f"not seen in gateway.log; gateway restart continued")
    reason = str(report.get("warning") or "hub bounce failed").strip() or "hub bounce failed"
    return f"warning — {reason}; gateway restart continued"


def load_hub_report_line(home: Path, *, now: Callable[[], float] = time.time) -> Optional[str]:
    """The report line for the hub bounce of THIS restart cycle, or None.

    None when no report exists, it is malformed, or it is older than
    ``_STALE_RESULT_MAX_AGE_S`` (a leftover from an earlier cycle must not
    annotate an unrelated restart).
    """
    report = _read_report(home)
    if report is None:
        return None
    try:
        age = now() - float(report.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > _STALE_RESULT_MAX_AGE_S:
        return None
    return hub_report_line(report)
