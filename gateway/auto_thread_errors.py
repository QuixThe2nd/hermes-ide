"""Thread-create error classification for Discord auto-thread adoption.

The decision "what does this ``message.create_thread`` failure mean" lives
in ``internal/autothread`` (Go) — one implementation, shared with the Go
test suite through ``testdata/classification_cases.json``. This module is
the thin bridge: it finds or builds the ``cmd/autothread`` binary (cached
under ``$HERMES_HOME/cache`` keyed by a source hash, like
``gateway/bot_allowlist.py``) and shells out to it on the failure path.
Helper unavailable or unreadable → None → caller keeps the historical
seed-fallback/retry behavior. Never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_BINARY_SOURCES = ("autothread.go", "cmd/autothread/main.go")
_HELPER_TIMEOUT_SECONDS = 10

_bridge_lock = threading.Lock()
_bridge_binary: Optional[str] = None
# A SUCCESSFUL resolution is pinned for the process; a failed one is retried
# after this cooldown so a broken environment does not re-attempt a 300 s
# build per failed thread creation.
_BRIDGE_RETRY_COOLDOWN_SECONDS = 300.0
_bridge_failed_at: Optional[float] = None
_warned_unavailable = False

_VALID_DECISIONS = frozenset({"adopt", "rate_limited", "retry", "fail"})


def _repo_module_dir() -> Optional[Path]:
    """The internal/autothread checkout this tree ships, when present."""
    module_dir = Path(__file__).resolve().parents[1] / "internal" / "autothread"
    if all((module_dir / name).is_file() for name in _BINARY_SOURCES):
        return module_dir
    return None


def _cached_binary_path() -> Path:
    """Cache location, keyed by a hash of the Go sources (staleness-proof)."""
    module_dir = _repo_module_dir()
    digest = "built"
    if module_dir is not None:
        hasher = hashlib.sha256()
        for name in _BINARY_SOURCES:
            hasher.update((module_dir / name).read_bytes())
        digest = hasher.hexdigest()[:12]
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "autothread" / f"autothread-{digest}"


def _resolve_go_binary() -> Optional[str]:
    """Find a usable cmd/autothread binary: cached, or built from the checkout.

    Never raises — the message path must not go down with its helper; every
    failure path returns None and the caller keeps the historical behavior.
    """
    try:
        binary = _cached_binary_path()
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    except Exception:
        return None
    module_dir = _repo_module_dir()
    if module_dir is None or shutil.which("go") is None:
        return None
    try:
        binary.parent.mkdir(parents=True, exist_ok=True)
        staging = binary.with_suffix(".tmp")
        build = subprocess.run(
            ["go", "build", "-o", str(staging), "./cmd/autothread"],
            cwd=module_dir, capture_output=True, text=True, timeout=300,
        )
        if build.returncode == 0 and staging.is_file():
            os.replace(staging, binary)  # atomic: concurrent gateways converge
            return str(binary)
        logger.warning(
            "autothread helper build failed (go build exit %s); auto-thread "
            "adoption is disabled until it succeeds", build.returncode,
        )
    except Exception as exc:
        logger.warning("autothread helper build failed: %s", exc)
    return None


def go_binary() -> Optional[str]:
    """The resolved helper binary path, or None when unavailable."""
    global _bridge_binary, _bridge_failed_at
    with _bridge_lock:
        if _bridge_binary is not None:
            return _bridge_binary
        if (
            _bridge_failed_at is not None
            and (time.monotonic() - _bridge_failed_at) < _BRIDGE_RETRY_COOLDOWN_SECONDS
        ):
            return None
        binary = _resolve_go_binary()
        if binary is not None:
            _bridge_binary = binary
        else:
            _bridge_failed_at = time.monotonic()
        return binary


def classify(status: int, code: int, message: str) -> Optional[str]:
    """Classify one thread-create error shape through the helper.

    Returns "adopt" | "rate_limited" | "retry" | "fail", or None when the
    helper is unavailable or returns something unreadable. Not memoized: this
    runs only on the failure path, so a process spawn per call is cheap
    enough and no cache can go stale.
    """
    global _warned_unavailable
    binary = go_binary()
    if binary is None:
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.warning(
                "autothread helper is unavailable (no cached binary and no Go "
                "toolchain to build one); auto-thread adoption is disabled until "
                "it succeeds"
            )
        return None
    try:
        run = subprocess.run(
            [binary],
            input=json.dumps({"status": status, "code": code, "message": message}),
            capture_output=True, text=True, timeout=_HELPER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("autothread helper invocation failed: %s", exc)
        return None
    if run.returncode != 0:
        logger.debug("autothread helper rejected the request: %s", run.stderr.strip())
        return None
    try:
        decision = json.loads(run.stdout).get("decision")
    except (ValueError, AttributeError) as exc:
        logger.debug("autothread helper returned an unreadable verdict: %s", exc)
        return None
    return decision if decision in _VALID_DECISIONS else None


def classify_thread_create_error(exc: BaseException) -> Optional[str]:
    """Classify a ``message.create_thread`` exception. Never raises.

    Defensive about duck-typed errors: discord.py's ``HTTPException`` carries
    ``.status``/``.code``; re-wrapped or mocked errors may expose the status
    only on ``.response.status`` or not at all (0 → classify on text alone).
    A None return means "no classification available" — callers MUST treat
    it as a plain failure so the historical behavior is preserved.
    """
    status = getattr(exc, "status", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = getattr(getattr(exc, "response", None), "status", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = 0
    code = getattr(exc, "code", None)
    if not isinstance(code, int) or isinstance(code, bool):
        code = 0
    try:
        message = str(exc)
    except Exception:
        message = ""
    return classify(status, code, message)
