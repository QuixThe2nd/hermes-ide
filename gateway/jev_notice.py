"""Concise chat notice lines for every real Jev classification request.

Troubleshooting surface only: one outbound line per actual Jev HTTP attempt
(success, negative decision, low confidence, no-reaction or failure alike),
carrying purpose, classified subject, decision/confidence and a sanitized
error class — never request/response bodies, credentials, message content or
prompt text. Notices never enter model context and never change a decision.

The ONLY formatter is the Go package ``internal/jevnotices``, executed as the
``cmd/jevnotice`` binary: this module contains no formatting logic, it maps
decision objects to record dicts and pipes them through the binary
(stdin JSON → stdout line). The binary is resolved once per process from the
``$HERMES_HOME/cache/jevnotices`` cache, building it with the local Go
toolchain from the repo checkout when missing (content-hashed filename, so a
tree update can never serve a stale formatter). When no binary can be
produced (no Go toolchain, build failure) the gateway posts NO notice and
logs once — an omitted troubleshooting line is the safe failure; a wrong or
duplicated formatter is not.
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

PURPOSE_GOAL_INTENT = "goal-intent"
PURPOSE_RESPONSE_GATE = "response-gate"
PURPOSE_REACTION_GATE = "reaction-gate"

_BINARY_SOURCES = ("notice.go", "switch.go", "cmd/jevnotice/main.go")

_bridge_lock = threading.Lock()
_bridge_binary: Optional[str] = None
# A SUCCESSFUL resolution is pinned for the process. A failed one is retried
# after this cooldown: the "notices disabled until it succeeds" log is a
# promise, so a later toolchain install (or a transient build failure) starts
# producing notices again without a gateway restart. The cooldown keeps a
# permanently-broken environment from re-attempting a 300 s build per message.
_BRIDGE_RETRY_COOLDOWN_SECONDS = 300.0
_bridge_failed_at: Optional[float] = None
_unavailable_logged = False


def _repo_module_dir() -> Optional[Path]:
    """The internal/jevnotices checkout this file's tree ships, when present."""
    module_dir = Path(__file__).resolve().parents[1] / "internal" / "jevnotices"
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

    return get_hermes_home() / "cache" / "jevnotices" / f"jevnotice-{digest}"


def _resolve_go_binary() -> Optional[str]:
    """Find a usable cmd/jevnotice binary: cached, or built from the checkout.

    Never raises: source hashing and cache-path resolution can fail (unreadable
    checkout, unwritable home) and a diagnostic must not take the gate down
    with it — every failure path returns None (notices skipped).
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
            ["go", "build", "-o", str(staging), "./cmd/jevnotice"],
            cwd=module_dir, capture_output=True, text=True, timeout=300,
        )
        if build.returncode == 0 and staging.is_file():
            os.replace(staging, binary)  # atomic: concurrent gateways converge
            return str(binary)
        logger.warning(
            "jev notice formatter build failed (go build exit %s); notices disabled "
            "until it succeeds", build.returncode,
        )
    except Exception as exc:
        logger.warning("jev notice formatter build failed: %s", exc)
    return None


def go_formatter_binary() -> Optional[str]:
    """The resolved Go formatter binary path, or None when unavailable.

    A successful resolution is cached for the process; a failed one is retried
    after ``_BRIDGE_RETRY_COOLDOWN_SECONDS`` so a later Go toolchain (or a
    recovered build failure) revives notices without a restart.
    """
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


def _notices_setting(profile_home: Path) -> Any:
    """Raw ``display.jev_notices`` from *profile_home*'s config (None = unset); Go decides its meaning."""
    from gateway.run import _load_gateway_config

    display = _load_gateway_config(Path(profile_home) / "config.yaml").get("display")
    return display.get("jev_notices") if isinstance(display, dict) else None


def format_notice(record: Dict[str, Any], profile_home: Optional[Path] = None) -> Optional[str]:
    """Render one record through the Go formatter binary.

    Returns the notice line, or None when the binary is unavailable, the
    render failed, or *profile_home* (the profile OWNING the message, never
    the default) sets ``display.jev_notices`` off — callers skip the notice
    then; they never substitute a local rendering. NEVER raises: resolution
    (source hashing, cache paths) and rendering are diagnostics, and a
    diagnostic failure must never change the gate decision or the reaction
    it was reporting.
    """
    global _unavailable_logged
    try:
        binary = go_formatter_binary()
        if binary is None:
            if not _unavailable_logged:
                _unavailable_logged = True
                logger.warning(
                    "jev notices unavailable: no cached formatter binary and no Go "
                    "toolchain to build one (notices are skipped, decisions "
                    "unaffected; retried until a build succeeds)"
                )
            return None
        if profile_home is not None:
            record = {**record, "notices": _notices_setting(profile_home)}
        run = subprocess.run(
            [binary], input=json.dumps(record),
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        logger.debug("jev notice render failed: %s", exc)
        return None
    if run.returncode != 0:
        logger.debug("jev notice formatter exited %s: %s", run.returncode, run.stderr.strip())
        return None
    return run.stdout.rstrip("\n") or None


async def format_notice_async(
    record: Dict[str, Any], profile_home: Optional[Path] = None,
) -> Optional[str]:
    """:func:`format_notice` off the event loop.

    The cold build (``go build``, up to 300 s), every render subprocess and the
    process-global bridge lock are blocking work: async callers (the Discord
    response/reaction paths) must not stall the gateway's event loop behind a
    diagnostic. The thread hop carries the caller's contextvars, so a profile
    scope stays bound.
    """
    return await asyncio.to_thread(format_notice, record, profile_home)


# ── record builders: one per owning call site ────────────────────────────
# Each builder turns one real decision object into a record dict for
# :func:`format_notice` (the Go binary sanitizes every field). The no-call
# paths (bypass, missing credential, suppression) never construct a record —
# no notice may claim an HTTP request that was not made.


def goal_intent_attempt_record(
    *, confidence: Optional[float], error: Optional[str],
) -> Dict[str, Any]:
    """One goal-intent Jev attempt as a fenced status record.

    The fenced status renders only the confidence readout or the sanitized
    failure class — attempt numbering, subject, author and label are not
    record inputs anymore, so the builder must not emit them either.
    """
    record: Dict[str, Any] = {"purpose": PURPOSE_GOAL_INTENT}
    if error:
        record["error"] = error
    else:
        record["confidence"] = confidence
    return record


def response_gate_record(decision: Any, *, author: str = "") -> Optional[Dict[str, Any]]:
    """One response-gate consultation, or None when no HTTP attempt was made.

    ``missing_credential`` is the only evaluate() outcome that never posted a
    request; every other outcome (including timeouts and HTTP errors) did.
    """
    if getattr(decision, "error", None) == "missing_credential":
        return None
    record: Dict[str, Any] = {
        "purpose": PURPOSE_RESPONSE_GATE,
        "subject": "user message",
        "author": author,
    }
    if decision.error:
        record["error"] = decision.error
        return record
    record["allowed"] = bool(decision.allowed)
    record["scores"] = dict(getattr(decision, "scores", None) or {})
    return record


def reaction_gate_record(decision: Any, *, author: str = "") -> Optional[Dict[str, Any]]:
    """One reaction-gate consultation, or None when no HTTP attempt was made."""
    if getattr(decision, "error", None) == "missing_credential":
        return None
    record: Dict[str, Any] = {
        "purpose": PURPOSE_REACTION_GATE,
        "subject": "user message",
        "author": author,
    }
    if decision.error:
        record["error"] = decision.error
        return record
    if decision.emoji is not None:
        record["emoji"] = str(decision.emoji)
    if decision.abstain_sum is not None:
        record["abstain"] = decision.abstain_sum
    return record
