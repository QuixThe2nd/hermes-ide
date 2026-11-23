"""Per-bot allowlist verdicts for bot-authored messages (``discord.allowed_bots``).

The parse + membership decision lives ONLY in the Go module
``internal/botallowlist``, executed as the ``cmd/botallowlist`` binary
(stdin JSON → stdout JSON). This module is the thin bridge: it resolves or
builds the binary once per process from the ``$HERMES_HOME/cache/botallowlist``
cache (content-hashed filename, so a tree update never serves a stale
verdict), caches each (list, author ID) verdict in-process so a busy
channel never spawns a process per message, and owns the fail-closed
contract.

Contract for callers:

- list unset or empty (:func:`configured` is False) → admit; the helper is
  never invoked, so an operator who never sets the list sees byte-for-byte
  the old behaviour (no extra process, no extra latency);
- list set and author listed → admit (the platform's ``allow_bots`` mode
  rules still apply afterwards — the list only ever narrows);
- list set and author not listed → deny;
- list set but unevaluable (malformed value, no binary and no Go toolchain
  to build one) → deny, with one warning per process.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from gateway import gobuild

logger = logging.getLogger(__name__)

_BINARY_SOURCES = ("botallowlist.go", "cmd/botallowlist/main.go")
_HELPER_TIMEOUT_SECONDS = 10

# Resolution of the cmd/botallowlist binary (cache lookup + content hash +
# go build) is the shared gateway/gobuild.py helper; this module keeps only
# the allowlist-specific pin/cooldown seam and the fail-closed contract.
_HELPER = gobuild.HelperBinary(
    module="botallowlist",
    sources=_BINARY_SOURCES,
    build_target="./cmd/botallowlist",
    cache_dir="botallowlist",
    binary_stem="botallowlist",
    label="allowed_bots helper",
    failure_hint="bot messages are denied until it succeeds",
)

_bridge_lock = threading.Lock()
_bridge_binary: Optional[str] = None
# A SUCCESSFUL resolution is pinned for the process. A failed one is retried
# after this cooldown: a later toolchain install revives the list without a
# gateway restart, while a permanently broken environment does not
# re-attempt a 300 s build per message.
_BRIDGE_RETRY_COOLDOWN_SECONDS = 300.0
_bridge_failed_at: Optional[float] = None


_cache_lock = threading.Lock()
# (canonical list, author ID) → verdict. The list is small and stable, so
# entries are never evicted; a config change is a new canonical key.
_verdict_cache: dict = {}
# Canonical lists proven malformed (the helper rejected the shape). Deny
# every author without re-invoking the helper per message.
_malformed_lists: set = set()
_warned_unavailable = False
_warned_malformed = False


def _repo_module_dir() -> Optional[Path]:
    """The internal/botallowlist checkout this tree ships, when present."""
    return _HELPER.repo_module_dir()


def _cached_binary_path() -> Path:
    """Cache location, keyed by a hash of the Go sources (staleness-proof)."""
    return _HELPER.cached_binary_path()


def _resolve_go_binary() -> Optional[str]:
    """Find a usable cmd/botallowlist binary: cached, or built from the checkout.

    Never raises: source hashing, cache-path resolution and the build can all
    fail (unreadable checkout, unwritable home, no toolchain) and an
    authorization gate must not go down with its helper — every failure path
    returns None and the caller fails closed.
    """
    return _HELPER.resolve()


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


def configured(raw: Any) -> bool:
    """Whether the raw setting names at least one entry (cheap pre-filter).

    The helper runs only when this is True — an unset/empty list must not
    spawn a process. Every shape that parses to an empty set answers False
    here; the helper is the authority for everything else.
    """
    if raw is None:
        return False
    if isinstance(raw, (list, tuple, set)):
        return any(str(part).strip() for part in raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return False
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return True  # not a JSON array: CSV-shaped, the helper decides
            if isinstance(parsed, list):
                return any(str(part).strip() for part in parsed)
        return True
    return True  # numbers, dicts, ...: the helper evaluates (malformed fails closed)


def _canonical_key(raw: Any) -> str:
    """Stable cache key for one raw list value."""
    try:
        return json.dumps(raw, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(raw)


def admits(raw: Any, author_id: str) -> bool:
    """Whether the per-bot allowlist admits *author_id* (narrow-only).

    Unset/empty list → True without invoking the helper. Unevaluable list
    (malformed value, helper unavailable or failing) → False. Never raises:
    an authorization gate fails closed, not open, and never crashes the
    message path.
    """
    global _warned_unavailable, _warned_malformed
    if not configured(raw):
        return True
    key = _canonical_key(raw)
    cache_key = (key, author_id)
    with _cache_lock:
        if key in _malformed_lists:
            return False
        if cache_key in _verdict_cache:
            return _verdict_cache[cache_key]
    binary = go_binary()
    if binary is None:
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.warning(
                "allowed_bots is set but the helper is unavailable (no cached binary "
                "and no Go toolchain to build one); bot messages are denied until it "
                "succeeds"
            )
        return False
    try:
        run = subprocess.run(
            [binary],
            input=json.dumps({"allowed_bots": raw, "author_id": author_id}),
            capture_output=True, text=True, timeout=_HELPER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("allowed_bots helper invocation failed: %s", exc)
        return False
    if run.returncode != 0:
        # The helper rejects exactly one thing: a malformed list shape. The
        # verdict is a property of the list, so deny every author on it.
        with _cache_lock:
            _malformed_lists.add(key)
        if not _warned_malformed:
            _warned_malformed = True
            logger.warning(
                "allowed_bots has a malformed value (%s); bot messages are denied "
                "until it is fixed", run.stderr.strip() or "unparseable",
            )
        return False
    try:
        verdict = json.loads(run.stdout)
        count = int(verdict.get("count", 0))
        listed = bool(verdict.get("listed", False))
    except (ValueError, TypeError, AttributeError) as exc:
        logger.debug("allowed_bots helper returned an unreadable verdict: %s", exc)
        return False
    # A configured list that parses to zero IDs narrows nothing (e.g. a CSV
    # string of separators); treat it like an unset list.
    admitted = True if count == 0 else listed
    with _cache_lock:
        _verdict_cache[cache_key] = admitted
    return admitted
