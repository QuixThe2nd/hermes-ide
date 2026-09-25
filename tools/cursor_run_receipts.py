"""Restart-safe cloud receipts for ``delegate_cursor_agent`` runs.

Persists binding metadata (never plaintext prompts or secrets) under
``$HERMES_HOME/cursor-runs`` so gateway resume can reconcile terminal cloud
runs or resume polling the same agent/run after interruption.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
RECEIPT_DIR_NAME = "cursor-runs"
LOCK_SUFFIX = ".lock"
RECEIPT_SUFFIX = ".receipt.json"
MAX_RECOVERY_ATTEMPTS = 3
CLOUD_EXECUTION_MODE = "cloud"

TERMINAL_OUTCOMES = frozenset({
    "success",
    "interrupted",
    "timeout",
    "stalled",
    "incomplete_output",
    "action_required",
    "failed",
    "cancelled",
    "error",
    "expired",
})


class ReceiptValidationError(Exception):
    """Fail-closed receipt validation or creation error."""


def cursor_runs_dir() -> Path:
    return get_hermes_home() / RECEIPT_DIR_NAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_boot_id() -> str:
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if boot_path.is_file():
            value = boot_path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    return f"pid-{os.getpid()}"


def hash_prompt(task: str) -> str:
    digest = hashlib.sha256(str(task or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonical_binding_key(hermes_session_id: str, tool_call_id: Optional[str]) -> str:
    sid = str(hermes_session_id or "").strip()
    if not sid:
        raise ReceiptValidationError("missing hermes_session_id")
    tcid = str(tool_call_id or "").strip()
    if not tcid:
        raise ReceiptValidationError("missing tool_call_id")
    return f"{sid}\0{tcid}"


def binding_hash(hermes_session_id: str, tool_call_id: Optional[str]) -> str:
    key = canonical_binding_key(hermes_session_id, tool_call_id)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def request_fingerprint(
    *,
    task: str,
    workdir: str,
    model: Optional[str],
    force: bool,
    timeout_seconds: int,
    prompt_hash: str,
) -> str:
    payload = {
        "task": str(task or "").strip(),
        "workdir": str(workdir or ""),
        "model": str(model or "").strip() or None,
        "force": bool(force),
        "timeout_seconds": int(timeout_seconds),
        "prompt_hash": str(prompt_hash or ""),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def deterministic_client_agent_id(hermes_session_id: str, tool_call_id: Optional[str]) -> str:
    digest = binding_hash(hermes_session_id, tool_call_id)
    # Cursor Cloud rejects bcIds whose UUID carries arbitrary version/variant
    # bits (e.g. ``bc-c3aab122-5063-c41c-ee07-d0892fffa577``); force the
    # version-5/RFC_4122 layout while keeping the value hash-derived.
    agent_uuid = uuid.UUID(digest[:32], version=5)
    return f"bc-{agent_uuid}"


def receipt_path_for_binding(hermes_session_id: str, tool_call_id: Optional[str]) -> Path:
    return cursor_runs_dir() / f"{binding_hash(hermes_session_id, tool_call_id)}{RECEIPT_SUFFIX}"


def lock_path_for_binding(hermes_session_id: str, tool_call_id: Optional[str]) -> Path:
    return cursor_runs_dir() / f"{binding_hash(hermes_session_id, tool_call_id)}{LOCK_SUFFIX}"


def _assert_path_within_runs_dir(path: Path) -> None:
    root = cursor_runs_dir().resolve()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ReceiptValidationError("invalid receipt path") from exc
    if resolved != root and root not in resolved.parents:
        raise ReceiptValidationError("path escape")


def _assert_regular_receipt_file(path: Path, *, must_exist: bool) -> None:
    _assert_path_within_runs_dir(path)
    if path.is_symlink():
        raise ReceiptValidationError("symlink receipt")
    if path.exists():
        if not path.is_file():
            raise ReceiptValidationError("non-regular receipt file")
    elif must_exist:
        raise ReceiptValidationError("missing receipt")


def _assert_receipt_permissions(path: Path) -> None:
    """Require restrictive 0600 semantics and current-user ownership."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ReceiptValidationError("receipt stat failed") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ReceiptValidationError("non-regular receipt file")
    if st.st_mode & 0o077 != 0:
        raise ReceiptValidationError("receipt permissions too permissive")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():  # windows-footgun: ok — hasattr-gated POSIX uid check
        raise ReceiptValidationError("receipt not owned by current user")


def _load_receipt_candidate(path: Path) -> Dict[str, Any]:
    """Load one receipt file with fail-closed validation for discovery."""
    _assert_path_within_runs_dir(path)
    if path.is_symlink():
        raise ReceiptValidationError("symlink receipt")
    if not path.is_file():
        raise ReceiptValidationError("non-regular receipt file")
    _assert_receipt_permissions(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReceiptValidationError("unreadable receipt") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReceiptValidationError("unparseable receipt") from exc
    if not isinstance(data, dict):
        raise ReceiptValidationError("malformed receipt schema")
    _validate_receipt_schema(data)
    return data


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_path_within_runs_dir(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def read_receipt(path: Path | str) -> Optional[Dict[str, Any]]:
    receipt_path = Path(path)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return None
    try:
        _assert_receipt_permissions(receipt_path)
    except ReceiptValidationError:
        logger.debug("receipt permission/ownership rejected: %s", receipt_path)
        return None
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _validate_receipt_schema(receipt: Dict[str, Any]) -> None:
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ReceiptValidationError("malformed receipt schema")
    for key in (
        "binding_hash",
        "hermes_session_id",
        "tool_call_id",
        "request_fingerprint",
        "workdir",
        "prompt_hash",
        "execution_mode",
        "state",
    ):
        if key not in receipt:
            raise ReceiptValidationError("malformed receipt schema")


def validate_receipt_binding(
    receipt: Dict[str, Any],
    *,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    request_fingerprint_value: Optional[str] = None,
) -> None:
    _validate_receipt_schema(receipt)
    if receipt.get("execution_mode") != CLOUD_EXECUTION_MODE:
        raise ReceiptValidationError("execution_mode is not cloud")
    if receipt.get("hermes_session_id") != hermes_session_id:
        raise ReceiptValidationError("foreign hermes_session_id binding")
    bound = receipt.get("tool_call_id")
    if not tool_call_id or not isinstance(bound, str) or bound != tool_call_id:
        raise ReceiptValidationError("foreign tool_call_id binding")
    expected_hash = binding_hash(hermes_session_id, tool_call_id)
    if receipt.get("binding_hash") != expected_hash:
        raise ReceiptValidationError("binding_hash mismatch")
    if request_fingerprint_value is not None:
        if receipt.get("request_fingerprint") != request_fingerprint_value:
            raise ReceiptValidationError("request fingerprint mismatch")
    client_id = receipt.get("client_agent_id")
    cloud_id = receipt.get("cloud_agent_id")
    if client_id and cloud_id and str(client_id) != str(cloud_id):
        raise ReceiptValidationError("conflicting cloud agent identities")


def create_receipt(
    *,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    workdir: str,
    prompt_hash: str,
    log_path: str,
    model: Optional[str],
    force: bool,
    timeout_seconds: int,
    task: str,
) -> Tuple[Path, Dict[str, Any]]:
    """Atomically create a restrictive cloud receipt before any external create."""
    if not tool_call_id or not str(tool_call_id).strip():
        raise ReceiptValidationError("tool_call_id is required")
    fingerprint = request_fingerprint(
        task=task,
        workdir=workdir,
        model=model,
        force=force,
        timeout_seconds=timeout_seconds,
        prompt_hash=prompt_hash,
    )
    bind_hash = binding_hash(hermes_session_id, tool_call_id)
    path = receipt_path_for_binding(hermes_session_id, tool_call_id)
    _assert_regular_receipt_file(path, must_exist=False)
    if path.exists():
        existing = _load_receipt_candidate(path)
        validate_receipt_binding(
            existing,
            hermes_session_id=hermes_session_id,
            tool_call_id=tool_call_id,
        )
        if existing.get("request_fingerprint") != fingerprint:
            raise ReceiptValidationError("request fingerprint mismatch")
        return path, existing

    attempt_id = uuid.uuid4().hex
    now = _utc_now_iso()
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "binding_hash": bind_hash,
        "attempt_id": attempt_id,
        "hermes_session_id": hermes_session_id,
        "tool_call_id": tool_call_id,
        "request_fingerprint": fingerprint,
        "workdir": workdir,
        "prompt_hash": prompt_hash,
        "state": "pending_create",
        "outcome": None,
        "created_at": now,
        "updated_at": now,
        "log_path": log_path,
        "owner_pid": os.getpid(),
        "owner_boot_id": get_boot_id(),
        "model": model,
        "force": bool(force),
        "timeout_seconds": int(timeout_seconds),
        "execution_mode": CLOUD_EXECUTION_MODE,
        "client_agent_id": deterministic_client_agent_id(hermes_session_id, tool_call_id),
        "cloud_agent_id": None,
        "cloud_run_id": None,
        "recovery_attempts": 0,
        "terminal_result": None,
    }
    try:
        _atomic_write_json(path, payload)
    except OSError as exc:
        raise ReceiptValidationError(f"receipt write failed: {exc}") from exc
    written = read_receipt(path)
    if written is None:
        raise ReceiptValidationError("receipt write failed")
    return path, written


def update_receipt(path: Path | str, **fields: Any) -> Optional[Dict[str, Any]]:
    receipt_path = Path(path)
    _assert_regular_receipt_file(receipt_path, must_exist=True)
    try:
        current = _load_receipt_candidate(receipt_path)
    except ReceiptValidationError:
        raise
    client_id = current.get("client_agent_id")
    new_cloud_id = fields.get("cloud_agent_id")
    if (
        client_id
        and new_cloud_id
        and str(new_cloud_id) != str(client_id)
        and current.get("cloud_agent_id")
        and str(current.get("cloud_agent_id")) != str(new_cloud_id)
    ):
        raise ReceiptValidationError("conflicting cloud agent identities")
    current.update(fields)
    current["updated_at"] = _utc_now_iso()
    _atomic_write_json(receipt_path, current)
    return current


def persist_cloud_ids(
    path: Path | str,
    *,
    cloud_agent_id: str,
    cloud_run_id: str,
) -> Optional[Dict[str, Any]]:
    if not cloud_agent_id or not cloud_run_id:
        raise ReceiptValidationError("missing cloud ids")
    return update_receipt(
        path,
        cloud_agent_id=str(cloud_agent_id),
        cloud_run_id=str(cloud_run_id),
        state="running",
    )


def finalize_receipt(
    path: Path | str,
    *,
    outcome: str,
    terminal_result: Optional[Dict[str, Any]] = None,
    log_path: Optional[str] = None,
    cloud_agent_id: Optional[str] = None,
    cloud_run_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    fields: Dict[str, Any] = {
        "state": "terminal",
        "outcome": outcome,
        "terminal_result": terminal_result,
    }
    if log_path is not None:
        fields["log_path"] = log_path
    if cloud_agent_id is not None:
        fields["cloud_agent_id"] = cloud_agent_id
    if cloud_run_id is not None:
        fields["cloud_run_id"] = cloud_run_id
    return update_receipt(path, **fields)


def is_terminal_receipt(receipt: Dict[str, Any]) -> bool:
    if receipt.get("state") == "terminal":
        return True
    outcome = receipt.get("outcome")
    return isinstance(outcome, str) and outcome in TERMINAL_OUTCOMES


def find_receipt_for_binding(
    hermes_session_id: str,
    tool_call_id: Optional[str],
    *,
    include_terminal: bool = True,
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """Locate the receipt for session + tool call identity; fail on ambiguity."""
    if not hermes_session_id or not tool_call_id:
        return None
    root = cursor_runs_dir()
    if not root.is_dir():
        return None

    matches: list[Tuple[Path, Dict[str, Any]]] = []
    for path in root.glob(f"*{RECEIPT_SUFFIX}"):
        data = _load_receipt_candidate(path)
        if data.get("hermes_session_id") != hermes_session_id:
            continue
        bound = data.get("tool_call_id")
        if not isinstance(bound, str) or bound != tool_call_id:
            continue
        matches.append((path, data))

    if not matches:
        return None
    if len(matches) > 1:
        raise ReceiptValidationError("ambiguous multiple receipts for binding")

    path, data = matches[0]
    try:
        validate_receipt_binding(data, hermes_session_id=hermes_session_id, tool_call_id=tool_call_id)
    except ReceiptValidationError:
        return None
    if not include_terminal and is_terminal_receipt(data):
        return None
    return path, data


def receipt_matches_binding(
    receipt: Dict[str, Any],
    *,
    hermes_session_id: str,
    tool_call_id: Optional[str],
    request_fingerprint_value: Optional[str] = None,
) -> bool:
    try:
        validate_receipt_binding(
            receipt,
            hermes_session_id=hermes_session_id,
            tool_call_id=tool_call_id,
            request_fingerprint_value=request_fingerprint_value,
        )
    except ReceiptValidationError:
        return False
    return True


@contextmanager
def binding_run_lock(hermes_session_id: str, tool_call_id: Optional[str]) -> Iterator[bool]:
    """Acquire an exclusive OS file lock for a canonical binding."""
    lock_path = lock_path_for_binding(hermes_session_id, tool_call_id or "")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _assert_path_within_runs_dir(lock_path)
    if lock_path.exists():
        try:
            st = os.lstat(lock_path)
        except OSError as exc:
            raise ReceiptValidationError("invalid lock path") from exc
        if not stat.S_ISREG(st.st_mode):
            raise ReceiptValidationError("non-regular lock file")

    open_flags = os.O_CREAT | os.O_RDWR
    nofollow = getattr(os, "O_NOFOLLOW", 0)  # windows-footgun: ok — getattr-gated O_NOFOLLOW
    if nofollow:
        open_flags |= nofollow
    fd = os.open(str(lock_path), open_flags, 0o600)
    acquired = False
    fd_closed = False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReceiptValidationError("non-regular lock file")
        if st.st_mode & 0o077 != 0:
            raise ReceiptValidationError("lock permissions too permissive")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():  # windows-footgun: ok — hasattr-gated POSIX uid check
            raise ReceiptValidationError("lock not owned by current user")
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if hasattr(fcntl, "flock"):  # windows-footgun: ok — hasattr-gated POSIX flock
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        else:
            acquired = True
        yield acquired
    except ReceiptValidationError:
        os.close(fd)
        fd_closed = True
        raise
    finally:
        if fd_closed:
            return
        if acquired and hasattr(fcntl, "flock"):  # windows-footgun: ok — hasattr-gated POSIX flock
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
