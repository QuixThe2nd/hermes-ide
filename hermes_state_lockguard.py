"""Hold a state.db writer's WAL-mode file locks on descriptors SQLite does not own.

SQLite protects a live WAL generation with two POSIX advisory locks: a SHARED lock on the main
file's lock range and a shared lock on the DMS byte of ``state.db-shm``. A sibling process may
checkpoint and unlink ``-wal``/``-shm`` at its close only after taking both EXCLUSIVE. POSIX locks
are per process, so any ``open()``/``close()`` of those two files inside the holder — a raw probe,
a plugin, a stray ``head -c`` in-process — cancels both (sqlite.org/howtocorrupt.html §2.2) and
the next foreign close strands the holder on a deleted generation (``DeletedWalGenerationError``).

This module re-holds the same two ranges as *open file description* locks (``F_OFD_SETLK``) on
private descriptors that are never closed. OFD locks belong to the description, not the process:
a stray ``close()`` elsewhere cannot cancel them, and releasing them with ``F_UNLCK`` never
disturbs SQLite's own locks. Both lock types conflict with a foreign EXCLUSIVE, so the sibling's
close-time unlink is refused for as long as a writer handle is open here. The same refusal applies
to THIS process's close: the last writer no longer deletes the sidecars, which is the
``SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`` behaviour on runtimes whose ``sqlite3`` cannot arm it.

One guard per database path per process, refcounted across writer handles; descriptors are
retired (never closed) when the path is re-pointed at a new inode. No-op on Windows and on
runtimes without OFD locks.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("hermes_state")

# SQLite's unix VFS lock geometry (os_unix.c): the SHARED range on the main file and the
# deadman-switch byte of the -shm file.
_PENDING_BYTE = 0x40000000
_SHARED_FIRST = _PENDING_BYTE + 2
_SHARED_SIZE = 510
_SHM_DMS_BYTE = 128

try:
    import fcntl
    # CPython exports F_OFD_SETLK only from 3.12. The kernel ABI values are stable: 37 on every
    # Linux arch (asm-generic/fcntl.h), 90 on XNU (bsd/sys/fcntl.h, documented in fcntl(2)).
    _F_OFD_SETLK: Optional[int] = getattr(
        fcntl, "F_OFD_SETLK", {"linux": 37, "darwin": 90}.get(sys.platform.rstrip("0123456789")))
    _F_RDLCK, _F_UNLCK, _SEEK_SET = fcntl.F_RDLCK, fcntl.F_UNLCK, os.SEEK_SET
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    _F_OFD_SETLK = None
    _F_RDLCK = _F_UNLCK = _SEEK_SET = 0

# struct flock differs per libc: glibc/musl put type+whence first, Darwin/BSD last.
_FLOCK_FORMAT = "@qqihh" if sys.platform == "darwin" or "bsd" in sys.platform else "@hhqqi"


def _flock(lock_type: int, start: int, length: int) -> bytes:
    if _FLOCK_FORMAT == "@qqihh":
        return struct.pack(_FLOCK_FORMAT, start, length, 0, lock_type, _SEEK_SET)
    return struct.pack(_FLOCK_FORMAT, lock_type, _SEEK_SET, start, length, 0)


def _ofd_lock(fd: int, lock_type: int, start: int, length: int) -> bool:
    """Apply a non-blocking OFD lock; False when the range is held EXCLUSIVE elsewhere."""
    assert fcntl is not None and _F_OFD_SETLK is not None
    try:
        fcntl.fcntl(fd, _F_OFD_SETLK, _flock(lock_type, start, length))
    except BlockingIOError:
        return False
    return True


class _PathGuard:
    __slots__ = ("main_fd", "main_ident", "shm_fd", "shm_ident", "refs", "main_locked", "shm_locked")

    def __init__(self) -> None:
        self.main_fd = self.shm_fd = -1
        self.main_ident = self.shm_ident = None
        self.refs = 0
        self.main_locked = self.shm_locked = False


_LOCK = threading.Lock()
_GUARDS: Dict[str, _PathGuard] = {}
_RETIRED_FDS: List[int] = []  # descriptors for re-pointed paths; closing one would cancel SQLite's locks


def supported() -> bool:
    return _F_OFD_SETLK is not None


def _bind_fd(path: str, fd: int, ident) -> tuple:
    """Return ``(fd, ident)`` for *path*, reusing *fd* while it still names the path's inode."""
    try:
        st = os.stat(path)
    except OSError:
        return fd, ident
    current = (st.st_dev, st.st_ino)
    if fd >= 0 and ident == current:
        return fd, ident
    if fd >= 0:
        _RETIRED_FDS.append(fd)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return -1, None
    return fd, current


def _apply_locked(guard: _PathGuard, db_path: str) -> None:
    guard.main_fd, guard.main_ident = _bind_fd(db_path, guard.main_fd, guard.main_ident)
    if guard.main_fd >= 0:
        guard.main_locked = _ofd_lock(guard.main_fd, _F_RDLCK, _SHARED_FIRST, _SHARED_SIZE)
    guard.shm_fd, guard.shm_ident = _bind_fd(db_path + "-shm", guard.shm_fd, guard.shm_ident)
    if guard.shm_fd >= 0:
        guard.shm_locked = _ofd_lock(guard.shm_fd, _F_RDLCK, _SHM_DMS_BYTE, 1)


def hold(db_path: Path) -> None:
    """Take (or add a reference to) the guard for *db_path*. Call once per writer handle after
    its connection is open in WAL mode; pair with :func:`release`."""
    if not supported():
        return
    key = os.fspath(db_path)
    with _LOCK:
        guard = _GUARDS.setdefault(key, _PathGuard())
        guard.refs += 1
        try:
            _apply_locked(guard, key)
        except OSError:
            logger.debug("WAL lock guard unavailable for %s", key, exc_info=True)


def refresh(db_path: Path) -> None:
    """Re-arm a held guard: a ``-shm`` that did not exist at :func:`hold` time, or a path re-pointed
    at a new inode since. Cheap when everything is in place (one ``stat`` per file)."""
    if not supported():
        return
    key = os.fspath(db_path)
    with _LOCK:
        guard = _GUARDS.get(key)
        if guard is None or guard.refs <= 0:
            return
        try:
            _apply_locked(guard, key)
        except OSError:
            logger.debug("WAL lock guard refresh failed for %s", key, exc_info=True)


def release(db_path: Path) -> None:
    """Drop one reference; the last one unlocks both ranges. Call BEFORE closing the handle's own
    connection so SQLite's close-time reset sees only real holders (a sibling process's intact
    locks still refuse the unlink; a true last close ends the generation normally, so a later
    ``state.db`` replace never pairs with a stale WAL). Descriptors are closed by
    :func:`retire_idle` once no connection to the path remains."""
    if not supported():
        return
    key = os.fspath(db_path)
    with _LOCK:
        guard = _GUARDS.get(key)
        if guard is None or guard.refs <= 0:
            return
        guard.refs -= 1
        if guard.refs:
            return
        for fd, start, length in ((guard.main_fd, _SHARED_FIRST, _SHARED_SIZE), (guard.shm_fd, _SHM_DMS_BYTE, 1)):
            if fd >= 0:
                try:
                    _ofd_lock(fd, _F_UNLCK, start, length)
                except OSError:
                    logger.debug("WAL lock guard unlock failed for %s", key, exc_info=True)
        guard.main_locked = guard.shm_locked = False


def retire_idle(db_path: Path) -> None:
    """Close the guard descriptors once no tracked SQLite connection to *db_path* remains in this
    process. Closing then cancels nothing, and a lingering fd on the path would make another
    process's holder scan (``hermes doctor`` repair, snapshot restore) count this one as live.
    While any connection is still open the descriptors stay put: closing would cancel its locks."""
    if not supported():
        return
    key = os.fspath(db_path)
    with _LOCK:
        guard = _GUARDS.get(key)
        if guard is None or guard.refs or _path_has_live_connection(key):
            return
        del _GUARDS[key]
        for fd in (guard.main_fd, guard.shm_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _path_has_live_connection(key: str) -> bool:
    try:
        from hermes_cli.sqlite_safe_read import has_live_connection
    except ImportError:
        return True  # cannot prove quiescence: keep the descriptors
    return has_live_connection(key)


def held(db_path: Path) -> bool:
    """Both ranges currently guarded for *db_path* (diagnostics and tests)."""
    with _LOCK:
        guard = _GUARDS.get(os.fspath(db_path))
        return bool(guard and guard.refs and guard.main_locked and guard.shm_locked)


def owned_fds() -> frozenset:
    """Every descriptor this module keeps open (active and retired). Deleted-sidecar holder scans
    must skip these: a retired guard fd on an unlinked ``-shm`` is not a SQLite connection reading
    a dead generation, and reporting it would refuse every later open in this process."""
    with _LOCK:
        fds = set(_RETIRED_FDS)
        for guard in _GUARDS.values():
            fds.update(fd for fd in (guard.main_fd, guard.shm_fd) if fd >= 0)
        return frozenset(fds)
