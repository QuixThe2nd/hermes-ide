"""Bridge from the Hindsight memory provider to the Go retain-backlog runtime.

The provider keeps only what is inherently host-side: classification of the
provider rate-limit failure, the Hindsight SDK network calls, and thread
scheduling. Queue state, persistence, cross-process locking, and the
reconcile table live in the Go runtime (``internal/retainbacklog``), which
this bridge invokes as the ``cmd/retainbacklog`` binary over line-delimited
JSON on stdio. That binary is THE implementation — the Python side never
touches the backlog file.

The incident this owns: Hindsight's retain path can fail with an HTTP 500
whose body wraps the upstream model provider's 429 —
``{"detail": "Fact extraction deferred by provider quota: ...
ProviderRateLimitResetError: ... retry at 2026-11-17T05:36:33.557843+00:00:
HTTP 429: ..."}``. A direct 429 is rate-limited too. Everything else (other
500s, 4xx, network errors) propagates unchanged and queues nothing.

Replay semantics are the SERVER's, verified against the live container and
OpenAPI (``.review-evidence/server-semantics.md``):

- ``submit_async_retain`` with a caller-supplied ``operation_id`` is
  idempotent: a repeated id returns the original operation and creates no
  new work (RetainRequest.operation_id description, live spec).
- ``POST /operations/{id}/retry`` re-queues failed AND cancelled rows —
  the server-owned durable retry surface. The drain never deletes a row to
  retry it.
- A streaming retain replays resumably: already-committed chunks are found
  by ``(document_id, content hash)`` and skipped, so a replay after a
  partial commit never duplicates committed chunks. This REQUIRES a stable
  ``document_id`` across the original attempt and every replay — the server
  auto-generates a fresh one per request when omitted (MemoryItem schema),
  which would defeat recovery. The provider therefore mints a document_id
  before the ORIGINAL retain call and the entry persists it.
- A sync retain ignores ``operation_id`` and may commit earlier mini-batches
  before the quota raise; the entry — not the ack — is the durable record.

Contract phrasing: idempotent, resumable replay with no duplicate chunk
commits. NOT "exactly once" — the server does not guarantee that for the
original sync call, and the client must not claim it.

Failure containment: when the Go runtime cannot be produced (no prebuilt
binary, no Go toolchain) enqueue raises :class:`BacklogRuntimeUnavailable`
and the caller surfaces the original provider failure — behavior degrades
to pre-feature, never a silent drop.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import queue as _queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

BACKLOG_FILENAME = "retain-backlog.jsonl"
CORRUPT_SUFFIX = ".corrupt"

_RETRY_AT_RE = re.compile(
    r"retry at (\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)", re.IGNORECASE
)

_RATE_LIMIT_NEEDLES = ("providerratelimitreseterror", "deferred by provider quota")
_DEFAULT_RETRY_DELAY = timedelta(hours=1)

# Total wall-clock budget for one drain pass: the child is waiting on OUR
# SDK replies (each bounded by the provider timeout), so this only fires on
# a wedged child. On expiry the child is killed; every queue mutation is
# durably saved before the next protocol step, so a killed pass leaves a
# consistent queue.
_DRAIN_DEADLINE_S = 600.0
_ENQUEUE_TIMEOUT_S = 120.0

_GO_PACKAGE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "internal" / "retainbacklog"


class BacklogRuntimeUnavailable(RuntimeError):
    """The Go backlog runtime could not be produced (no prebuilt binary and
    no Go toolchain to build one). Rate-limited retains then surface their
    original failure instead of being silently dropped."""


def classify_rate_limit(
    status, body, *, now: datetime | None = None
) -> tuple[bool, datetime | None]:
    """(rate_limited, retry_at) for one failed retain; *retry_at* is UTC-aware."""
    if status != 429 and not body:
        return False, None
    text = f"{status} {body or ''}".lower()
    if status != 429 and not any(n in text for n in _RATE_LIMIT_NEEDLES):
        return False, None
    return True, _parse_retry_at(text, now=now)


def classify_exception(
    exc: BaseException, *, now: datetime | None = None
) -> datetime | None:
    """``retry_at`` when *exc* is a provider rate-limit failure (HTTP status/body
    of the wrapped 429), else None. Never raises."""
    status = getattr(exc, "status", None)
    body = getattr(exc, "body", None) or str(exc)
    try:
        rate_limited, retry_at = classify_rate_limit(status, body, now=now)
    except Exception:  # defensive: classification must never mask the drain
        return None
    return retry_at if rate_limited else None


def format_retry_at(retry_at: datetime) -> str:
    """Stable UTC rendering for user-facing queued messages."""
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    retry_at = retry_at.astimezone(timezone.utc)
    hours, rem = divmod(int((retry_at - datetime.now(timezone.utc)).total_seconds()), 3600)
    return f"{retry_at.isoformat()} ({hours}h)"


def _parse_retry_at(text: str, *, now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    m = _RETRY_AT_RE.search(text)
    if m:
        with contextlib.suppress(ValueError, TypeError):
            stamp = datetime.fromisoformat(m.group(1).upper().replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone.utc)
    return base + _DEFAULT_RETRY_DELAY


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class BacklogRemote(Protocol):
    """The network half of the drain, owned by the provider (SDK + secret
    scope). Mirrors ``retainbacklog.Remote`` in the Go runtime."""

    def status(self, bank_id: str, op_id: str) -> str:
        """Lowercase server-side operation status; "not_found" on 404."""
        ...

    def retry_operation(self, bank_id: str, op_id: str) -> str:
        """"ok" once re-queued, "gone" (404) or "conflict" (409)."""
        ...

    def submit(self, entry: dict) -> None:
        """Replay the entry's item as an async retain under its persisted
        operation id; raise on failure (rate-limit shape is classified)."""
        ...


# -- Go runtime production ----------------------------------------------------

_binary_lock = threading.Lock()
_binary_cache: dict[str, str] = {}


def _source_fingerprint() -> str:
    """Stable id over the Go sources + go.mod, so a source change forces a
    rebuild and stale binaries are never reused against a new protocol."""
    digest = hashlib.sha256()
    files = sorted(p for p in _GO_PACKAGE_DIR.rglob("*.go"))
    mod = _GO_PACKAGE_DIR / "go.mod"
    if mod.exists():
        files.append(mod)
    if not files:
        return "missing"
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _prebuilt_binary() -> str | None:
    exe = "retainbacklog.exe" if os.name == "nt" else "retainbacklog"
    path = _GO_PACKAGE_DIR / "bin" / exe
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _build_binary(dest: Path, home: Path) -> None:
    """Compile once into dest's temp name; atomic rename publishes it.
    GOCACHE/GOPATH stay inside the live home so isolated/hermetic
    environments never touch the user's real caches."""
    go = shutil.which("go")
    if go is None:
        raise BacklogRuntimeUnavailable(
            "no Go toolchain found to build the retain-backlog runtime "
            f"(looked in PATH; sources at {_GO_PACKAGE_DIR})"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    cache_root = home / "cache"
    tmp = dest.with_name(dest.name + f".build-{os.getpid()}")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GOCACHE": str(cache_root / "go-build"),
        "GOPATH": str(cache_root / "go-path"),
        "GOPROXY": "off",
        "GOFLAGS": os.environ.get("GOFLAGS", ""),
    }
    try:
        subprocess.run(
            [go, "build", "-o", str(tmp), "./cmd/retainbacklog"],
            cwd=str(_GO_PACKAGE_DIR), env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _runtime_binary(home: Path) -> str:
    """Locate (building once per source fingerprint per process) the runtime
    binary for this home. Order: per-home cache -> process-global build ->
    repo prebuilt -> BacklogRuntimeUnavailable."""
    fp = _source_fingerprint()
    with _binary_lock:
        cached = _binary_cache.get(fp)
        if cached and os.access(cached, os.X_OK):
            return cached
        per_home = home / "cache" / "retainbacklog" / f"retainbacklog-{fp}"
        if per_home.is_file() and os.access(per_home, os.X_OK):
            _binary_cache[fp] = str(per_home)
            return str(per_home)
        prebuilt = _prebuilt_binary()
        try:
            _build_binary(per_home, home)
            _binary_cache[fp] = str(per_home)
            return str(per_home)
        except BacklogRuntimeUnavailable:
            if prebuilt:
                logger.warning(
                    "Hindsight retain-backlog: using prebuilt runtime %s "
                    "(no Go toolchain to rebuild from source)", prebuilt)
                _binary_cache[fp] = prebuilt
                return prebuilt
            raise


class RetainBacklog:
    """One profile's backlog, delegated to the Go runtime. ``home_resolver``
    is invoked per operation so a multiplexed process keys every binary
    invocation by the live profile."""

    def __init__(self, home_resolver: Callable[[], Path], *, binary: str | None = None):
        self._home_resolver = home_resolver
        self._binary_override = binary
        self._may_exist = False

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

    def _spawn(self, *args: str) -> subprocess.Popen:
        binary = self._binary_override or _runtime_binary(self._home_resolver())
        return subprocess.Popen(
            [binary, *args, "--home", str(self._home_resolver())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Minimal env: the runtime needs no secrets (the host does all
            # network) and must not inherit another profile's scope.
            env={}, text=True,
        )

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
        """Persist one already-built retain item via the Go runtime; returns
        the entry id — the server-side ``operation_id``, minted and persisted
        by the runtime before any remote submission, never regenerated."""
        entry = {
            "bank_id": bank_id,
            "item": item,
            "document_id": document_id,
            "queued_at": _to_iso(now or datetime.now(timezone.utc)),
            "retry_after": _to_iso(retry_after),
        }
        proc = self._spawn("enqueue")
        try:
            out, err = proc.communicate(
                json.dumps(entry) + "\n", timeout=_ENQUEUE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        if proc.returncode != 0:
            raise RuntimeError(
                f"retainbacklog enqueue failed ({proc.returncode}): {err.strip()}")
        reply = json.loads(out)
        self._may_exist = True
        return reply["id"]

    def peek(self) -> tuple[datetime | None, int]:
        """(earliest retry_after among queued entries, entry count) — the
        instants a later drain may make progress. (None, 0) when empty."""
        proc = self._spawn("peek")
        out, err = proc.communicate(timeout=_ENQUEUE_TIMEOUT_S)
        if proc.returncode != 0:
            raise RuntimeError(
                f"retainbacklog peek failed ({proc.returncode}): {err.strip()}")
        done = json.loads(out)
        self._may_exist = done["remaining"] > 0
        return _parse_done_retry(done), done["remaining"]

    def drain(self, remote: BacklogRemote) -> datetime | None:
        """One FIFO settle pass via the Go runtime, answering its protocol
        requests with *remote*. The runtime holds the cross-process flock for
        the whole pass and applies the reconcile table (remove only on
        observed "completed"; pending/processing poll; failed/cancelled
        retry_operation; not_found submit idempotently by operation id).

        Returns the earliest ``retry_after`` among entries still queued after
        the pass (None when empty) so the caller can arm a wakeup."""
        if not self._may_exist:
            return None
        proc = self._spawn("drain")
        try:
            return self._drain_protocol(proc, remote)
        finally:
            if proc.poll() is None:
                proc.kill()

    # -- drain wire protocol ---------------------------------------------------

    def _drain_protocol(self, proc: subprocess.Popen, remote: BacklogRemote) -> datetime | None:
        assert proc.stdin is not None and proc.stdout is not None
        lines: "queue.Queue[str | None]" = _queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)  # EOF

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = time.monotonic() + _DRAIN_DEADLINE_S
        done: dict[str, Any] | None = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("retainbacklog drain protocol exceeded its deadline")
                try:
                    line = lines.get(timeout=remaining)
                except _queue.Empty:
                    raise TimeoutError("retainbacklog drain protocol exceeded its deadline")
                if line is None:
                    err = (proc.stderr.read() if proc.stderr else "") or ""
                    raise RuntimeError(f"retainbacklog runtime exited mid-drain: {err.strip()}")
                msg = json.loads(line)
                if msg.get("type") == "done":
                    done = msg
                    break
                reply = self._answer(msg, remote)
                proc.stdin.write(json.dumps(reply) + "\n")
                proc.stdin.flush()
        except BrokenPipeError:
            err = (proc.stderr.read() if proc.stderr else "") or ""
            raise RuntimeError(f"retainbacklog runtime exited mid-drain: {err.strip()}") from None
        proc.stdin.close()
        proc.wait(timeout=30)
        self._may_exist = done["remaining"] > 0
        if done.get("error"):
            logger.warning("Hindsight retain-backlog drain reported: %s", done["error"])
        return _parse_done_retry(done)

    def _answer(self, msg: dict, remote: BacklogRemote) -> dict:
        """Serve one runtime request; failures are classified on the wire —
        rate limits carry a retry hint, everything else is a plain error the
        runtime backs off exponentially."""
        kind = msg.get("type")
        entry_id = msg.get("id") or (msg.get("entry") or {}).get("id", "")
        try:
            if kind == "status":
                return {"id": entry_id,
                        "status": remote.status(msg["bank_id"], msg["id"])}
            if kind == "retry_op":
                return {"id": entry_id,
                        "result": remote.retry_operation(msg["bank_id"], msg["id"])}
            if kind == "submit":
                remote.submit(msg["entry"])
                return {"id": entry_id, "result": "ok"}
        except Exception as exc:
            retry_at = classify_exception(exc)
            if retry_at is not None:
                return {"id": entry_id, "error": "rate",
                        "retry_after": _to_iso(retry_at)}
            logger.warning(
                "Hindsight retain-backlog: %s for %s failed: %s", kind, entry_id, exc)
            return {"id": entry_id, "error": f"{type(exc).__name__}: {exc}"[:500]}
        return {"id": entry_id, "error": f"protocol: unknown request type {kind!r}"}


def _parse_done_retry(done: dict[str, Any]) -> datetime | None:
    raw = done.get("next_retry")
    if not raw:
        return None
    stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)
