"""Durable single-file publication for security-relevant updater state.

One strict helper, used for the two pieces of on-disk state the auto-update
activation later trusts: the named update receipts and the authoritative
prepared-generation record. Publication is a transaction:

1. the complete payload is serialized to bytes up front (oversize refused);
2. a unique temp file is created in the SAME directory with exclusive
   creation, so two publishers can never share a staging file;
3. the full payload is written with a short-write loop — a partial
   ``os.write`` is retried, never silently truncated;
4. the temp file is flushed and ``fsync``ed — failures propagate;
5. the temp file is atomically ``os.replace``d onto the canonical path;
6. on POSIX the parent directory is opened and ``fsync``ed so the rename
   itself is durable — failures propagate (the Windows branch is explicit:
   directory fsync is not available there, not silently swallowed here);
7. the canonical path is reopened and read in full; success requires exact
   byte equality with the intended payload.

Any step that fails raises :class:`OSError` and the caller must treat the
publication as failed — fail closed, never authorize on a maybe-write. The
staged sibling is removed on every failure path so no ``*.tmp.*`` litter
survives. A failure AFTER the rename (directory fsync, read-back) cannot
un-write the file — there is no crash rollback through arbitrary filesystem
``EIO`` — so the error propagates and the caller must not claim success.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: Upper bound for one published state file. The prepared-generation record
#: is a few hundred bytes; a receipt carrying a full runtime plan is still
#: far under this. Anything larger is corruption, not state.
MAX_STATE_BYTES = 4 << 20  # 4 MiB

#: Directory fsync is a POSIX facility. Elsewhere (Windows) the step is
#: skipped EXPLICITLY via this flag rather than attempted and its errors
#: swallowed. Module-level so the platform decision is made once, and so the
#: compatibility branch itself is exercisable on any platform.
_SUPPORTS_DIR_FSYNC = os.name == "posix"


def durable_publish_bytes(path: Path, payload: bytes) -> None:
    """Atomically and durably publish *payload* at *path*.

    Raises :class:`OSError` on any failure — oversize payload, staging
    failure, short write that cannot progress, file or directory ``fsync``
    error, rename error, or a read-back that does not exactly equal the
    intended payload. Returns only when the payload is provably on disk.
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError(f"payload must be bytes, got {type(payload).__name__}")
    data = bytes(payload)
    if not data:
        raise OSError(f"refusing to publish an empty payload to {path}")
    if len(data) > MAX_STATE_BYTES:
        raise OSError(
            f"refusing to publish {len(data)} bytes to {path}"
            f" (limit {MAX_STATE_BYTES})"
        )
    path = Path(path)
    staged_name: str | None = None
    try:
        # Exclusive creation in the same directory: unique staging file, and
        # the rename below stays on one filesystem (atomic on POSIX and
        # Windows alike).
        staged_fd, staged_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".tmp."
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(staged_fd, view)
                if written <= 0:
                    raise OSError(
                        f"write to staging file for {path} made no progress"
                        f" ({len(data) - len(view)}/{len(data)} bytes)"
                    )
                view = view[written:]
            os.fsync(staged_fd)
        finally:
            os.close(staged_fd)
        os.replace(staged_name, path)
        staged_name = None  # consumed by the rename
        # The rename is only durable once the parent directory is fsynced.
        # Directory fsync is a POSIX facility: off POSIX the branch is
        # explicit — skip — rather than catching and swallowing real errors.
        if _SUPPORTS_DIR_FSYNC:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        # Read-back: the canonical path must now hold exactly the payload.
        actual = path.read_bytes()
        if actual != data:
            raise OSError(
                f"read-back of {path} does not match the published payload"
                f" ({len(actual)} != {len(data)} bytes)"
            )
    except OSError:
        if staged_name is not None:
            try:
                os.unlink(staged_name)
            except OSError:
                pass
        raise
