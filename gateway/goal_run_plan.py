"""Delivery plan for one turn of an active persistent goal.

A running goal must not post one answer per turn. The policy lives in the Go
package ``internal/goalrun``; this module only transports one turn's facts and
resolves the binary like ``gateway/jev_notice.py`` (content-hashed cache, built
from the checkout when missing). Fail-open: no toolchain, no suppression — a
broken build never silences a goal run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SOURCES = ("goalrun.go", "cmd/goalrun/main.go")
_CACHE = "goalrun"
_RETRY_COOLDOWN_S = 300.0
_lock = threading.Lock()
_binary: Optional[str] = None
_failed_at: Optional[float] = None

EMPTY_PLAN: Dict[str, Any] = {"suppress": False, "final": False, "objective": "", "line": ""}


def _module_dir() -> Optional[Path]:
    """The ``internal/goalrun`` checkout this file's tree ships, when present."""
    module_dir = Path(__file__).resolve().parents[1] / "internal" / "goalrun"
    return module_dir if all((module_dir / name).is_file() for name in _SOURCES) else None


def _cache_path() -> Path:
    """Cache location, keyed by a hash of the Go sources (staleness-proof)."""
    from hermes_constants import get_hermes_home

    module_dir = _module_dir()
    digest = "built"
    if module_dir is not None:
        hasher = hashlib.sha256()
        for name in _SOURCES:
            hasher.update((module_dir / name).read_bytes())
        digest = hasher.hexdigest()[:12]
    return get_hermes_home() / "cache" / _CACHE / f"{_CACHE}-{digest}"


def _resolve() -> Optional[str]:
    """Cached binary, else build one. Never raises: a plan is bookkeeping."""
    try:
        binary = _cache_path()
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    except Exception:
        return None
    module_dir = _module_dir()
    if module_dir is None or shutil.which("go") is None:
        return None
    try:
        binary.parent.mkdir(parents=True, exist_ok=True)
        staging = binary.with_suffix(".tmp")
        build = subprocess.run(
            ["go", "build", "-o", str(staging), "./cmd/goalrun"],
            cwd=module_dir, capture_output=True, text=True, timeout=300,
        )
        if build.returncode == 0 and staging.is_file():
            os.replace(staging, binary)  # atomic: concurrent gateways converge
            return str(binary)
        logger.warning(
            "goalrun plan build failed (go build exit %s); goal turns stay loud "
            "until it succeeds", build.returncode,
        )
    except Exception as exc:
        logger.warning("goalrun plan build failed: %s", exc)
    return None


def _binary_path() -> Optional[str]:
    """Resolved binary, or None while unavailable (a failure is retried on cooldown)."""
    global _binary, _failed_at
    with _lock:
        if _binary is not None:
            return _binary
        if _failed_at is not None and (time.monotonic() - _failed_at) < _RETRY_COOLDOWN_S:
            return None
        resolved = _resolve()
        if resolved is not None:
            _binary = resolved
        else:
            _failed_at = time.monotonic()
        return resolved


def plan_for_turn(
    *,
    active: bool,
    should_continue: bool,
    turn: int = 0,
    max_turns: int = 0,
    objective: str = "",
    last_step: str = "",
) -> Dict[str, Any]:
    """One turn's plan through the Go binary; :data:`EMPTY_PLAN` when it cannot run."""
    record = {
        "active": bool(active),
        "should_continue": bool(should_continue),
        "turn": int(turn or 0),
        "max_turns": int(max_turns or 0),
        "objective": str(objective or ""),
        "last_step": str(last_step or ""),
    }
    try:
        binary = _binary_path()
        if binary is None:
            logger.debug("goal run plan unavailable: no binary and no Go toolchain")
            return dict(EMPTY_PLAN)
        run = subprocess.run(
            [binary], input=json.dumps(record),
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        logger.debug("goal run plan failed: %s", exc)
        return dict(EMPTY_PLAN)
    if run.returncode != 0:
        logger.debug("goalrun binary exited %s: %s", run.returncode, run.stderr.strip())
        return dict(EMPTY_PLAN)
    try:
        plan = json.loads(run.stdout)
    except Exception:
        logger.debug("goalrun binary produced no plan: %r", run.stdout[:200])
        return dict(EMPTY_PLAN)
    return {**EMPTY_PLAN, **plan} if isinstance(plan, dict) else dict(EMPTY_PLAN)


async def plan_for_turn_async(**kwargs: Any) -> Dict[str, Any]:
    """:func:`plan_for_turn` off the event loop (cold build, subprocess, lock are blocking)."""
    return await asyncio.to_thread(plan_for_turn, **kwargs)
