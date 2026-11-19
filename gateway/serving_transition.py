"""Durable per-session serving identity: the process that served the last turn.

The transition note (:func:`gateway.serving_identity.serving_transition_note`)
needs to know which process served the session's PREVIOUS turns. That
knowledge must survive the process transition itself — in-memory state dies
with the predecessor — so it lives in the session database's ``state_meta``
KV store: one ``served_by:<session_id>`` key per session holding
``{"pid": ..., "start_time": ...}``. The pid alone is not identity (PIDs get
reused); the start fingerprint is part of the record for the same reason.

Write discipline: the record is written only when it CHANGES (a process
transition), never per turn — an ordinary same-process turn is one cheap
point read. Every failure path degrades to "no transition claimed": a note
that cannot be proven is never guessed, and persistence trouble must never
break a turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

META_KEY_PREFIX = "served_by:"


def _meta_key(session_id: str) -> str:
    return f"{META_KEY_PREFIX}{session_id}"


def _valid_session_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and bool(session_id.strip())


def _record_write_needed(stored_raw: Any, pid: Any, start_time: Any) -> bool:
    """Pure write-on-change predicate for the served-by record.

    ``False`` only when the stored record parses and already carries exactly
    this pid + start fingerprint (an ordinary same-process turn). Corrupt or
    absent records are writes — the durable predecessor must be learnable.
    Shared contract with ``internal/servingidentity`` (Go twin); pinned by
    the shared honesty table.
    """
    try:
        stored = json.loads(stored_raw) if isinstance(stored_raw, str) else None
    except Exception:
        stored = None
    return not (
        isinstance(stored, dict)
        and stored.get("pid") == pid
        and stored.get("start_time") == start_time
    )


def _resolve_db(store: Any, session_id: Optional[str]) -> Any:
    """The SessionDB holding the session's meta keys, or ``None``.

    Accepts the runner's ``SessionStore`` (per-session and profile-scope
    aware via ``_db_for_session_id``) or a bare ``SessionDB``. Anything
    else — a test stand-in, ``None`` — means "no persistence available":
    callers degrade to no transition claim rather than raising.
    """
    resolver = getattr(store, "_db_for_session_id", None)
    if callable(resolver):
        try:
            return resolver(session_id)
        except Exception:
            logger.debug("serving transition: session db resolve failed", exc_info=True)
            return None
    return store if hasattr(store, "get_meta") and hasattr(store, "set_meta") else None


def last_served_identity(store: Any, session_id: Optional[str]) -> Optional[dict]:
    """The persisted ``{"pid", "start_time"}`` record, or ``None``.

    ``None`` for absent, corrupt, or unreachable records: the absence of a
    provable predecessor is "no transition", never an error.
    """
    if not _valid_session_id(session_id):
        return None
    db = _resolve_db(store, session_id)
    if db is None:
        return None
    try:
        raw = db.get_meta(_meta_key(session_id))
        record = json.loads(raw) if isinstance(raw, str) else None
    except Exception:
        logger.debug("serving transition: last-served record unreadable", exc_info=True)
        return None
    return record if isinstance(record, dict) else None


def record_served_identity(
    store: Any, session_id: Optional[str], identity: Mapping[str, Any]
) -> bool:
    """Persist *identity* when it differs from the stored record. Never raises.

    Returns True when a write happened. A missing/invalid session id or an
    unreachable store is a silent no-op — the transition note is advisory.
    """
    if not _valid_session_id(session_id):
        return False
    db = _resolve_db(store, session_id)
    if db is None:
        return False
    record = {
        "pid": identity.get("pid"),
        "start_time": identity.get("start_time"),
    }
    try:
        if not _record_write_needed(
            db.get_meta(_meta_key(session_id)),
            record["pid"], record["start_time"],
        ):
            return False
        db.set_meta(_meta_key(session_id), json.dumps(record))
        return True
    except Exception:
        logger.debug("serving transition: record write failed", exc_info=True)
        return False


def session_turn_notes(
    store: Any, session_id: Optional[str], runner: Any
) -> Tuple[str, Optional[str]]:
    """``(identity note, transition note | None)`` with the durable comparison inside.

    Reads the session's last-served record, renders the note pair via
    ``gateway.serving_identity.serving_turn_notes``, then records THIS
    process as the session's last server (on change only). Read-before-write
    ordering matters: the note must describe the state BEFORE this turn's
    record lands. Persistence failures never break the turn — the identity
    note still renders and no transition is claimed.
    """
    from gateway.serving_identity import serving_identity_snapshot, serving_turn_notes

    try:
        previous = last_served_identity(store, session_id)
    except Exception:
        logger.debug("serving transition: last-served read failed", exc_info=True)
        previous = None
    identity_note, transition_note = serving_turn_notes(runner, previous)
    try:
        record_served_identity(store, session_id, serving_identity_snapshot(runner))
    except Exception:
        logger.debug("serving transition: record step failed", exc_info=True)
    return identity_note, transition_note
