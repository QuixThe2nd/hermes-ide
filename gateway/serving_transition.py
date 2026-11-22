"""Durable per-session serving identity: the process that served the last turn.

The transition note (:func:`gateway.serving_identity.serving_transition_note`)
needs to know which process served the session's PREVIOUS turns. That
knowledge must survive the process transition itself — in-memory state dies
with the predecessor — so it lives in the session database's ``state_meta``
KV store: one ``served_by:<session_id>`` key per session holding
``{"pid": ..., "start_time": ..., "note": ...}`` — the note is the exact
per-turn note the session last saw, the proof that suppresses an
unchanged repeat (durable for the same reason as the start fingerprint).

Write discipline: the record is written only when it CHANGES (process or
note wording), never per turn — an ordinary same-process turn is one cheap
point read. Every failure path degrades to "no transition claimed": a note
that cannot be proven is never guessed, and persistence never breaks a turn.
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


def _record_write_needed(stored_raw: Any, pid: Any, start_time: Any,
                         note: Optional[str] = None) -> bool:
    """Pure write-on-change predicate for the served-by record.

    ``False`` only when the stored record parses and already carries exactly
    this pid + start fingerprint + note (an ordinary same-process turn whose
    note wording did not change). Corrupt or absent records are writes — the
    durable predecessor must be learnable. Shared contract with
    ``internal/servingidentity`` (Go twin); pinned by the shared honesty
    table.
    """
    try:
        stored = json.loads(stored_raw) if isinstance(stored_raw, str) else None
    except Exception:
        stored = None
    return not (
        isinstance(stored, dict)
        and stored.get("pid") == pid
        and stored.get("start_time") == start_time
        and stored.get("note") == note
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


def record_served_identity(
    store: Any, session_id: Optional[str], identity: Mapping[str, Any],
    note: Optional[str] = None) -> bool:
    """Persist *identity* and the *note* the session saw, on change only. Never raises.

    Returns True when a write happened. A missing/invalid session id or an
    unreachable store is a silent no-op — the transition note is advisory.
    """
    if not _valid_session_id(session_id):
        return False
    db = _resolve_db(store, session_id)
    if db is None:
        return False
    record = {"pid": identity.get("pid"), "start_time": identity.get("start_time"),
              "note": note}
    try:
        if not _record_write_needed(
            db.get_meta(_meta_key(session_id)),
            record["pid"], record["start_time"], record["note"],
        ):
            return False
        db.set_meta(_meta_key(session_id), json.dumps(record))
        return True
    except Exception:
        logger.debug("serving transition: record write failed", exc_info=True)
        return False


def session_turn_notes(
    store: Any, session_id: Optional[str], runner: Any
) -> Tuple[Optional[str], Optional[str]]:
    """``(identity note | None, transition note | None)`` with the durable
    comparison inside.

    Reads the session's last-served record, renders the pair via
    ``gateway.serving_identity.serving_turn_notes`` (a note the record
    proves seen is suppressed), then records THIS process as the last
    server (on change only). Read-before-write ordering matters: the note
    must describe the state BEFORE this turn's record lands; persistence
    failure never breaks the turn — unproven notes still render.
    """
    from gateway.serving_identity import serving_identity_snapshot, serving_turn_notes

    # The persisted last-served record — ``None`` when absent, corrupt, or
    # unreachable: no provable predecessor means "no transition", not an error.
    previous: Optional[dict] = None
    db = _resolve_db(store, session_id) if _valid_session_id(session_id) else None
    if db is not None:
        try:
            raw = db.get_meta(_meta_key(session_id))
            record = json.loads(raw) if isinstance(raw, str) else None
            previous = record if isinstance(record, dict) else None
        except Exception:
            logger.debug("serving transition: last-served record unreadable", exc_info=True)
    identity_note, transition_note = serving_turn_notes(runner, previous)
    # The record remembers the note the conversation saw THIS turn: the one
    # just emitted, or — on a suppressed repeat — the stored one it matched.
    try:
        record_served_identity(
            store, session_id, serving_identity_snapshot(runner),
            identity_note or (previous or {}).get("note"))
    except Exception:
        logger.debug("serving transition: record step failed", exc_info=True)
    return identity_note, transition_note
