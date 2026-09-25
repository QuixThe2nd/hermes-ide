from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .paths import denial_log_path
DENIAL_MAX_BYTES = 10 * 1024 * 1024
DENIAL_ARCHIVES = 5


class AuditError(RuntimeError):
    def __init__(self, code: str = "denial_audit_failed"):
        self.code = code
        super().__init__(code)


def safe_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_error_json() -> str:
    return safe_json({"error": "authorization_failed"})


def _validate_file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise AuditError()
    return info


def _open_active(path: Path) -> int:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        _validate_file(fd)
        return fd
    except Exception as exc:
        if isinstance(exc, AuditError):
            raise
        raise AuditError() from exc


def _rotate(path: Path) -> None:
    oldest = Path(f"{path}.{DENIAL_ARCHIVES}")
    oldest.unlink(missing_ok=True)
    for number in range(DENIAL_ARCHIVES - 1, 0, -1):
        source = Path(f"{path}.{number}")
        if source.exists():
            os.replace(source, Path(f"{path}.{number + 1}"))
    os.replace(path, Path(f"{path}.1"))


def _open_lock(path: Path) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    _validate_file(fd)
    return fd


def append_denial_event(
    reason: str,
    *,
    platform_present: bool,
    presented_user_id: str | None,
    audit_hmac_key: bytes,
    path: str | os.PathLike[str] | None = None,
) -> None:
    """Append one bounded, redacted denial event without touching PostgreSQL."""
    if not isinstance(audit_hmac_key, bytes) or len(audit_hmac_key) != 32:
        raise AuditError()
    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reason": reason,
        "platform_present": bool(platform_present),
    }
    if presented_user_id:
        event["user_id_hmac"] = hmac.new(audit_hmac_key, presented_user_id.encode("utf-8"), hashlib.sha256).hexdigest()
    payload = (safe_json(event) + "\n").encode("utf-8")
    if len(payload) > DENIAL_MAX_BYTES:
        raise AuditError()

    active = denial_log_path() if path is None else Path(path)
    fd = -1
    lock_fd = -1
    try:
        lock_fd = _open_lock(active)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = _open_active(active)
        info = _validate_file(fd)
        if info.st_size + len(payload) > DENIAL_MAX_BYTES:
            # The stable sidecar lock remains held while the active pathname is
            # renamed and recreated. Locking only the old inode leaves a race.
            os.close(fd)
            fd = -1
            _rotate(active)
            fd = _open_active(active)
        written = os.write(fd, payload)
        if written != len(payload):
            raise AuditError()
        os.fsync(fd)
    except Exception as exc:
        print("discord-history: denial audit failed", file=sys.stderr)
        if isinstance(exc, AuditError):
            raise
        raise AuditError() from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
