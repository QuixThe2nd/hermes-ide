"""Metadata-only JSONL collector for the cache-trace plugin.

Design constraints (see plugin README):

- **Allowlist storage.** The hooks DO receive the full lifecycle payload
  (including raw request/response kwargs); each event type copies ONLY the
  fields listed in ``_ALLOWLIST`` out of it. Anything else in the payload —
  prompts, tool arguments, response text, exception messages, headers — is
  dropped by construction and never persisted.
- **Bounded.** One JSONL file, rotated at ``_MAX_BYTES`` keeping
  ``_ROTATIONS`` backups; in-memory prefix state is capped too.
- **Fail-open.** Every entry point swallows exceptions (one rate-limited
  warning per process); a collector failure must never fail an API call.
- **Profile-aware.** The output path resolves ``get_hermes_home()`` per
  call, so a multiplexed process writes into the owning profile's logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_BYTES = 2 * 1024 * 1024  # hard cap: a trace file never exceeds 2 MiB UTF-8 bytes
_ROTATIONS = 3                # trace.jsonl.1 .. .3
_MAX_FIELD_BYTES = 4096       # larger string fields are hash-represented, not stored
_MAX_TRACKED_SESSIONS = 256   # bound on in-memory prefix state

_WARNED: set = set()

# Serializes state mutation + file writes: lifecycle callbacks may arrive from
# concurrent turn threads in one multiplexed process. stdlib Lock, no service.
_STATE_LOCK = threading.Lock()

# (profile-home key, session_id) -> last pre-request per-message state
# ({"hashes", "gaps", "truncated"}). Keyed by the CANONICAL profile home, not
# the display label: one process serves several profiles, and the bare
# session_id collides across them.
_LAST_MESSAGE_HASHES: Dict[str, Dict[str, Any]] = {}


def _warn_once(key: str, exc: Exception) -> None:
    if key in _WARNED:
        logger.debug("cache_trace: %s failed again: %s", key, exc)
        return
    _WARNED.add(key)
    logger.warning("cache_trace: %s failed (%s); trace records may be missing", key, exc)


def _trace_path() -> Path:
    # Import at call time: HERMES_HOME may be a per-activity profile override.
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "cache-trace" / "trace.jsonl"


def _tighten(path: Path, mode: int) -> bool:
    """Chmod ``path`` to ``mode``. Returns False (after warning) if the
    filesystem refuses — the caller must then skip the write rather than
    append telemetry to a file that is not access-restricted."""
    try:
        os.chmod(path, mode)
        return True
    except OSError as exc:
        _warn_once("chmod", exc)
        return False


def _rotate(path: Path) -> bool:
    for idx in range(_ROTATIONS - 1, 0, -1):
        src = path.with_suffix(f".jsonl.{idx}")
        if src.exists():
            # Tighten BEFORE the rename so a preexisting permissive backup
            # never survives a rotation in its old mode.
            if not _tighten(src, 0o600):
                return False
            src.replace(path.with_suffix(f".jsonl.{idx + 1}"))
    path.replace(path.with_suffix(".jsonl.1"))
    return True


def _hash_represent(value: str) -> Dict[str, Any]:
    """Content-free stand-in for an oversized provider field: identifies the
    value by salt-free SHA-256 and reports only its encoded size, so normal
    request/turn/session correlation survives but no huge ID is persisted."""
    data = value.encode("utf-8", "surrogatepass")
    return {
        "__oversized__": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "utf8_bytes": len(data),
    }


def _bound_strings(obj: Any) -> Any:
    """Replace every string field whose UTF-8 encoding exceeds
    ``_MAX_FIELD_BYTES`` with a hash representation, recursively."""
    if isinstance(obj, str):
        return obj if len(obj.encode("utf-8", "surrogatepass")) <= _MAX_FIELD_BYTES \
            else _hash_represent(obj)
    if isinstance(obj, dict):
        return {k: _bound_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bound_strings(v) for v in obj]
    return obj


def _append(record: Dict[str, Any]) -> None:
    with _STATE_LOCK:
        path = _trace_path()
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if not _tighten(directory, 0o700):
            return
        # Tighten a preexisting active file BEFORE any append or rotation —
        # os.open(..., 0o600) only applies at creation and leaves an existing
        # 0644 file permissive.
        if path.exists() and not _tighten(path, 0o600):
            return
        # Preexisting backups may predate this policy (e.g. 0644); tighten
        # them alongside the active file, not only when a rotation moves them.
        for idx in range(1, _ROTATIONS + 1):
            backup = path.with_suffix(f".jsonl.{idx}")
            if backup.exists() and not _tighten(backup, 0o600):
                return
        # allow_nan=False: a record carrying NaN/Infinity is NOT valid JSON —
        # reject it here (ValueError) so the fail-open handler in ``_record``
        # drops the record instead of persisting a nonfinite line.
        data = (json.dumps(_bound_strings(record), ensure_ascii=False,
                           sort_keys=False, default=str, allow_nan=False)
                + "\n").encode("utf-8")
        if len(data) > _MAX_BYTES:
            # Last resort: the record is still oversized after per-field
            # bounding. Drop it and persist a bounded, content-free
            # diagnostic instead — never truncate JSON mid-line, never
            # exceed the cap, never fail the API call.
            diagnostic = {
                "event": "collector_oversize_drop",
                "recorded_at": time.time(),
                "dropped_bytes": len(data),
                "source_event": record.get("event"),
            }
            data = (json.dumps(diagnostic, allow_nan=False) + "\n").encode("utf-8")
            if len(data) > _MAX_BYTES:
                return
        if path.exists() and path.stat().st_size + len(data) > _MAX_BYTES:
            if not _rotate(path):
                return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # os.write may write fewer bytes than requested (RLIMIT_FSIZE,
            # ENOSPC, signal interruption). A partial JSONL line is corrupt,
            # so loop until the full record is written and roll the file back
            # to its pre-append length if the append cannot complete. This is
            # failure rollback, NOT atomicity against process death.
            orig_size = os.fstat(fd).st_size
            written = 0
            try:
                while written < len(data):
                    try:
                        n = os.write(fd, data[written:])
                    except InterruptedError:
                        continue  # EINTR: retry the same slice
                    if n <= 0:
                        # A zero-byte "successful" write of a nonempty buffer
                        # would spin forever; treat it as a failed append.
                        raise OSError("cache_trace: zero-byte write")
                    written += n
            except BaseException:
                if os.fstat(fd).st_size > orig_size:
                    os.ftruncate(fd, orig_size)
                    os.fsync(fd)
                raise
        finally:
            os.close(fd)


def _record(event: str, payload: Dict[str, Any], allowlist: tuple) -> None:
    try:
        record: Dict[str, Any] = {"event": event, "recorded_at": time.time()}
        for key in allowlist:
            if key in payload:
                record[key] = payload[key]
        _append(record)
    except Exception as exc:  # fail-open: never fail the API call
        _warn_once("write", exc)


def _prefix_comparison(session_id: str, messages_fp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Shared-prefix / first-changed-message vs the previous pre event in the session.

    ``coverage`` says what the comparison saw: ``complete`` when both sides
    were within the per-message bound AND had no per-message gaps, ``partial``
    otherwise (either side truncated by the collector bound, or either side
    skipped unserializable messages — the whole-component hash still covers
    them, but per-message indices past a gap are not comparable and the
    comparison halts there instead of reporting shifted indices).
    """
    per_message = messages_fp.get("per_message")
    if not isinstance(per_message, list):
        return None
    if not session_id:
        # Unowned session: there is nothing to key a comparison to, and a
        # shared bucket would mix unrelated requests.
        return None
    truncated = bool(messages_fp.get("per_message_truncated"))
    gaps = [g for g in (messages_fp.get("per_message_gaps") or []) if isinstance(g, int)]
    from hermes_constants import get_hermes_home, hermes_home_key

    scope_key = hermes_home_key(get_hermes_home()) + "\x00" + session_id
    entry = {"hashes": list(per_message), "gaps": gaps, "truncated": truncated}
    with _STATE_LOCK:
        previous = _LAST_MESSAGE_HASHES.get(scope_key)
        _LAST_MESSAGE_HASHES[scope_key] = entry
        if len(_LAST_MESSAGE_HASHES) > _MAX_TRACKED_SESSIONS:
            _LAST_MESSAGE_HASHES.pop(next(iter(_LAST_MESSAGE_HASHES)))
    prev_truncated = bool((previous or {}).get("truncated"))
    prev_gaps = [g for g in (previous or {}).get("gaps") or [] if isinstance(g, int)]
    prev_hashes = (previous or {}).get("hashes")
    if not isinstance(prev_hashes, list):
        prev_hashes = []
    coverage = "partial" if (truncated or prev_truncated or gaps or prev_gaps) else "complete"
    if previous is None:
        return {"shared_prefix_messages": 0, "first_request": True, "coverage": coverage}
    # ``per_message`` skips gap messages, so hash positions equal message
    # indices only strictly BEFORE the first gap on each side. Halt there.
    first_gap = min(
        [g for g in (gaps + prev_gaps) if g >= 0] or [float("inf")]
    )
    comparable = min(len(prev_hashes), len(per_message))
    if first_gap != float("inf"):
        comparable = min(comparable, int(first_gap))
    shared = 0
    for a, b in zip(prev_hashes[:comparable], per_message[:comparable]):
        if a != b:
            break
        shared += 1
    first_changed = shared if shared < comparable else None
    out: Dict[str, Any] = {
        "shared_prefix_messages": shared,
        "previous_message_count": len(prev_hashes),
        "first_changed_index": first_changed,
        "coverage": coverage,
    }
    if first_gap != float("inf"):
        out["partial_reason"] = "per-message gaps present; indices past the first gap are not comparable"
    return out


# Hook payload allowlists — storage is a copy of ONLY these keys.
_PRE_ALLOWLIST = (
    "task_id", "turn_id", "api_request_id", "session_id", "platform", "model",
    "provider", "base_url_host", "api_mode", "api_call_count", "retry_count",
    "message_count", "tool_count", "approx_input_tokens", "request_char_count",
    "max_tokens", "started_at", "request_metadata", "prefix_comparison",
)
_POST_ALLOWLIST = (
    "task_id", "turn_id", "api_request_id", "session_id", "platform", "model",
    "provider", "api_mode", "api_call_count", "api_duration", "started_at",
    "ended_at", "first_chunk_at", "finish_reason", "response_model",
    "response_id", "assistant_content_chars", "assistant_tool_call_count",
    "usage_cache_presence",
)
_ERROR_ALLOWLIST = (
    "task_id", "turn_id", "api_request_id", "session_id", "model", "provider",
    "api_mode", "api_call_count", "api_duration", "started_at", "ended_at",
    "status_code", "retry_count", "max_retries", "retryable", "reason",
    "error_type",
)


def _base_url_host(payload: Dict[str, Any]) -> Optional[str]:
    from urllib.parse import urlparse

    base_url = payload.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        return None
    return urlparse(base_url).hostname


def on_pre_api_request(**payload: Any) -> None:
    """``pre_api_request``: request-shape fingerprints + prefix comparison."""
    payload = dict(payload)
    try:
        host = _base_url_host(payload)
        if host is not None:
            payload["base_url_host"] = host
        session_id = str(payload.get("session_id") or "")
        metadata = payload.get("request_metadata")
        if isinstance(metadata, dict):
            messages_fp = metadata.get("messages")
            if isinstance(messages_fp, dict):
                comparison = _prefix_comparison(session_id, messages_fp)
                if comparison is not None:
                    payload["prefix_comparison"] = comparison
    except Exception as exc:
        _warn_once("pre", exc)
    _record("pre_api_request", payload, _PRE_ALLOWLIST)


def on_post_api_request(**payload: Any) -> None:
    """``post_api_request``: observed usage/cache presence + timing (no content)."""
    # Explicit availability marker: API duration is NOT TTFT; first-token time
    # is only present when the transport actually reported a chunk.
    payload = dict(payload)
    payload["first_chunk_available"] = payload.get("first_chunk_at") is not None
    _record("post_api_request", payload, _POST_ALLOWLIST + ("first_chunk_available",))


def on_api_request_error(**payload: Any) -> None:
    """``api_request_error``: error TYPE/status only — never the message or exception."""
    payload = dict(payload)
    error = payload.get("error")
    if isinstance(error, dict):
        payload["error_type"] = error.get("type")
    _record("api_request_error", payload, _ERROR_ALLOWLIST)
