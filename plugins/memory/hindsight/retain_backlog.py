"""Durable per-profile backlog for retains the Hindsight provider rejected
with a provider rate limit / quota error.

The real failure arrives as an HTTP 500 whose body wraps the upstream 429:
``{"detail": "Fact extraction deferred by provider quota: ... ProviderRateLimitResetError:
... retry at 2026-11-17T05:36:33.557843+00:00: HTTP 429: ..."}``. A direct 429 is
rate-limited too. Everything else (other 500s, 4xx, network errors) must
propagate unchanged.

Semantics (mirrored by the Go contract ``internal/retainbacklog``):

- rate-limited iff status 429 or the body carries a provider-quota marker
  (``ProviderRateLimitResetError`` / ``deferred by provider quota``);
- ``retry at <ISO>`` parsed when present, else fallback ``now + 1h``;
- JSONL backlog at ``<hermes_home>/hindsight/retain-backlog.jsonl`` — 0600,
  atomic tmp+rename rewrites (file fsync + directory fsync), flock-serialized;
  the home is resolved PER CALL (never cached) so a multiplexed process keys
  state by the live profile;
- an entry's ``id`` is a UUID generated at ENQUEUE time and persisted with the
  entry — before any remote submission. The id doubles as the Hindsight
  ``operation_id``: the drain submits every entry ``async`` with that id, and
  the server treats a repeated id as a replay of the original operation (no
  new work), which is what makes a crash between server ack and local removal
  harmless (verify the reconcile step below on restart, never blind-resubmit);
- FIFO drain, oldest first, only once ``now >= earliest retry_after``;
- per entry the caller's ``settle(entry)`` reconciles against server-side
  operation state and returns when the operation is durably server-owned (or
  stored); the entry is removed only after settle succeeds (exactly-once
  under the lock). A settle that raises a rate-limit failure pushes every
  earlier ``retry_after`` forward to the new hint and stops the pass; any
  other raise keeps the entry and stops the pass (logged by the caller);
- ``drain`` returns the earliest ``retry_after`` among entries still queued
  after the pass (None when empty) so the caller can arm a wakeup;
- lines that are not valid entries (unparseable JSON, missing/unusable
  ``id``) are moved to ``<path>.corrupt`` (append + fsync) and logged — never
  silently dropped.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore[assignment]
logger = logging.getLogger(__name__)

BACKLOG_FILENAME = "retain-backlog.jsonl"
CORRUPT_SUFFIX = ".corrupt"
_RATE_LIMIT_NEEDLES = ("providerratelimitreseterror", "deferred by provider quota")
_RETRY_AT_RE = re.compile(r"retry at (\S+)", re.IGNORECASE)
_DEFAULT_RETRY_DELAY = timedelta(hours=1)


def classify_rate_limit(
    status, body, *, now: datetime | None = None
) -> tuple[bool, datetime | None]:
    """(rate_limited, retry_at) for one failed retain; *retry_at* is UTC-aware."""
    text = body or ""
    if status != 429 and not any(
        needle in text.lower() for needle in _RATE_LIMIT_NEEDLES
    ):
        return False, None
    return True, _parse_retry_at(text, now=now)


def classify_exception(
    exc: BaseException, *, now: datetime | None = None
) -> datetime | None:
    """``retry_at`` when *exc* is a provider rate-limit failure (HTTP status/body
    attributes when the SDK exception carries them, else its text), else None."""
    rate_limited, retry_at = classify_rate_limit(
        getattr(exc, "status", None), getattr(exc, "body", None) or str(exc), now=now
    )
    return retry_at if rate_limited else None


def format_retry_at(retry_at: datetime) -> str:
    """Stable UTC rendering for user-facing queued messages."""
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return (
        retry_at
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_retry_at(text: str, *, now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    match = _RETRY_AT_RE.search(text)
    if match:
        # The signature body continues "…+00:00: HTTP 429…" — \S+ swallows the
        # trailing colon (and other prose punctuation); an ISO timestamp never
        # legitimately ends with one.
        raw = match.group(1).rstrip(":,;)]}.>")
        if raw.endswith(("Z", "z")):  # fromisoformat before 3.11 rejects Z
            raw = raw[:-1] + "+00:00"
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(raw).astimezone(timezone.utc)
    return base + _DEFAULT_RETRY_DELAY


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _entry_retry_after(entry: dict) -> datetime:
    """Parsed ``retry_after``; corrupt/missing values count as long overdue."""
    with contextlib.suppress(ValueError, KeyError, TypeError):
        value = datetime.fromisoformat(str(entry["retry_after"]))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _entry_is_valid(entry: Any) -> bool:
    """A replayable entry: a dict whose ``id`` is a usable operation UUID and
    whose payload still names a bank and an item. Anything else is corrupt."""
    return (
        isinstance(entry, dict)
        and _is_uuid(entry.get("id"))
        and isinstance(entry.get("bank_id"), str)
        and bool(entry.get("bank_id"))
        and isinstance(entry.get("item"), dict)
    )


class RetainBacklog:
    """One profile's backlog. ``home_resolver`` is invoked per operation so a
    shared process never keys backlog state by a stale home."""

    def __init__(self, home_resolver: Callable[[], Path]):
        self._home_resolver = home_resolver
        self._may_exist = False
        self._io_lock = threading.RLock()
        self._quarantined: set[int] = set()

    @property
    def path(self) -> Path:
        return self._home_resolver() / "hindsight" / BACKLOG_FILENAME

    @property
    def may_exist(self) -> bool:
        """Cheap gate for hot paths: True only when a backlog file might exist."""
        return self._may_exist

    def refresh_may_exist(self) -> bool:
        """One stat; True when a backlog file exists for the live home."""
        self._may_exist = self.path.exists()
        return self._may_exist

    def entries(self) -> list[dict]:
        """Locked read of the live entries (FIFO file order)."""
        with self._locked():
            return self._load()

    def earliest_retry(self) -> datetime | None:
        """Earliest ``retry_after`` among queued entries; None when empty."""
        with self._locked():
            entries = self._load()
        return min((_entry_retry_after(e) for e in entries), default=None)

    def enqueue(
        self,
        *,
        bank_id: str,
        item: dict,
        retry_after: datetime,
        document_id: str | None = None,
        retain_async: bool | None = None,
        now: datetime | None = None,
    ) -> str:
        """Persist one already-built retain item; returns the entry id.

        The id is the server-side ``operation_id`` for every later replay of
        this entry — generated here, persisted before any submission, and
        never regenerated. An id already present is never duplicated."""
        entry = {
            "id": str(uuid.uuid4()),
            "bank_id": bank_id,
            "item": item,
            "document_id": document_id,
            "retain_async": retain_async,
            "queued_at": _to_iso(now or datetime.now(timezone.utc)),
            "retry_after": _to_iso(retry_after),
        }
        with self._locked():
            current = self._load()
            if not any(e.get("id") == entry["id"] for e in current):
                self._save(current + [entry])
        return entry["id"]

    def drain(
        self, settle: Callable[[dict], Any], *, now: datetime | None = None
    ) -> datetime | None:
        """One FIFO pass over due entries, holding the lock for the whole pass
        so concurrent drainers and enqueuers serialize.

        ``settle(entry)`` owns the remote side: it reconciles the entry's
        operation against server state and returns once the operation is
        durably server-owned (or stored). Returning removes the entry (saved
        immediately, still under the lock). A rate-limit raise pushes every
        pending ``retry_after`` forward to the new hint and stops the pass;
        any other raise keeps the entry and stops the pass.

        Returns the earliest ``retry_after`` among entries still queued after
        the pass — the instant a later drain may make progress (None when the
        backlog is empty) — so the caller can arm a wakeup."""
        if not self._may_exist:
            return None
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        with self._locked():
            entries = self._load()
            if not entries:
                return None
            if min(_entry_retry_after(e) for e in entries) > now:
                return min(_entry_retry_after(e) for e in entries)
            for entry in entries:
                try:
                    settle(entry)
                except Exception as exc:
                    retry_at = classify_exception(exc, now=now)
                    if retry_at is None:
                        logger.warning(
                            "Hindsight retain-backlog: entry %s kept after non-rate-limit failure: %s",
                            entry.get("id"),
                            exc,
                        )
                        return self._earliest_of(entries)
                    for pending in entries:
                        if _entry_retry_after(pending) < retry_at:
                            pending["retry_after"] = _to_iso(retry_at)
                    self._save(entries)
                    logger.info(
                        "Hindsight retain-backlog: provider still rate-limited; "
                        "next drain after %s",
                        format_retry_at(retry_at),
                    )
                    return retry_at
                entries = [e for e in entries if e.get("id") != entry.get("id")]
                self._save(entries)  # removed only after success, still under the lock
            return self._earliest_of(entries)

    @staticmethod
    def _earliest_of(entries: list[dict]) -> datetime | None:
        return min((_entry_retry_after(e) for e in entries), default=None)

    # -- file I/O ----------------------------------------------------------------

    def _load(self) -> list[dict]:
        """Parse the backlog file; invalid lines are quarantined (see module
        docstring) so a torn or hand-mangled line neither blocks the drain
        nor silently disappears."""
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            self._may_exist = False
            return []
        entries: list[dict] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            entry: Any = None
            with contextlib.suppress(ValueError):
                entry = json.loads(line)
            if _entry_is_valid(entry):
                entries.append(entry)
            else:
                self._quarantine(line)
        return entries

    def _quarantine(self, line: str) -> None:
        """Append one invalid line to ``<path>.corrupt`` exactly once per
        process (hash-guarded); recoverable by an operator, never replayed."""
        key = hash(line)
        if key in self._quarantined:
            return
        self._quarantined.add(key)
        corrupt_path = Path(str(self.path) + CORRUPT_SUFFIX)
        try:
            corrupt_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(corrupt_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # The queue must stay drainable even when the quarantine sidecar
            # cannot be written; the line is still dropped from the live file
            # only by the next successful _save, and the warning names it.
            logger.warning(
                "Hindsight retain-backlog: corrupt line could not be quarantined to %s: %r",
                corrupt_path,
                line[:200],
                exc_info=True,
            )
            return
        logger.warning(
            "Hindsight retain-backlog: quarantined corrupt line to %s: %r",
            corrupt_path,
            line[:200],
        )

    def _save(self, entries: list[dict]) -> None:
        path = self.path
        if not entries:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            self._may_exist = False
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        payload = "".join(
            json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        self._fsync_dir(path.parent)
        self._may_exist = True

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Persist the rename itself: fsync the containing directory so a
        crash right after os.replace cannot lose the new file name. No-op
        where the platform refuses directory fsync (or non-POSIX)."""
        if fcntl is None:
            return
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _locked(self):
        """In-process mutex + best-effort cross-process flock on ``<path>.lock``.
        A lock file that cannot be created degrades to in-process serialization
        (a missing advisory lock must not drop memories)."""
        with self._io_lock:
            fd: int | None = None
            try:
                lock_path = Path(str(self.path) + ".lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                elif msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            except OSError:
                logger.warning(
                    "Hindsight retain-backlog lock unavailable; serializing in-process only",
                    exc_info=True,
                )
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                    fd = None
            try:
                yield
            finally:
                if fd is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        elif msvcrt is not None:
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    finally:
                        os.close(fd)
