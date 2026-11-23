"""Durable per-session serving identity: the process that served the last turn.

The transition note (:func:`gateway.serving_identity.serving_transition_note`)
needs to know which process served the session's PREVIOUS turns. That
knowledge must survive the process transition itself — in-memory state dies
with the predecessor — so it lives in the session database's ``state_meta``
KV store: one ``served_by:<session_id>`` key per session holding
``{"pid": ..., "start_time": ..., "note": ..., "code_sha": ...}`` — the
note is the exact per-turn note the session last saw, the proof that
suppresses an unchanged repeat, and the ``code_sha`` is the build the
previous turns ran, which lets the next process render the code delta
between the two builds (:mod:`gateway.serving_codedelta`).

Write discipline: the record is written only when it CHANGES (process or
note wording), never per turn — an ordinary same-process turn is one cheap
point read. Every failure path degrades to "no transition claimed": a note
that cannot be proven is never guessed, and persistence never breaks a turn.

The note AND code-sha proofs are acknowledgement-gated: staging a turn
records the serving process (pid + start fingerprint) but does NOT advance
either proof — the notes have not been delivered yet, and the delta channel
settles only once the delta note's api_content sidecar stamp is persisted
(``agent.turn_context._persist_turn_user_api_content`` →
:func:`confirm_served_note`), so a turn that crashes before the stamp
re-emits the note and the delta on the next turn instead of suppressing
them forever.
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
    note: Optional[str] = None, code_sha: Any = None) -> bool:
    """Persist *identity*, the *note* the session saw, and the *code_sha*
    proof, on change only. Never raises.

    Returns True when a write happened. A missing/invalid session id or an
    unreachable store is a silent no-op — the transition note is advisory.
    """
    if not _valid_session_id(session_id):
        return False
    db = _resolve_db(store, session_id)
    if db is None:
        return False
    record = {"pid": identity.get("pid"), "start_time": identity.get("start_time"),
              "note": note, "code_sha": code_sha}
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


def confirm_served_note(
    store: Any, session_id: Optional[str], note: Optional[str],
    code_sha: Any = None) -> bool:
    """Advance the durable note + code-sha proofs once the notes' sidecar
    stamps landed. Never raises.

    The acknowledgement half of the note gate: staging
    (:func:`session_turn_notes`) records the serving process but leaves both
    proofs at their previous values; only the persisted api_content sidecar
    stamp proves the conversation actually received the notes. ``code_sha``
    defaults to this process's own build identity — the delta channel
    settles on it (an unusable sha keeps the stored one rather than
    forgetting the previous build). Write-on-change, keyed on the session's
    own record: an absent/corrupt record (the stage write could not be
    established) or an already-current proof is a silent no-op, never an
    error.
    """
    if not _valid_session_id(session_id) or not isinstance(note, str) or not note:
        return False
    db = _resolve_db(store, session_id)
    if db is None:
        return False
    try:
        raw = db.get_meta(_meta_key(session_id))
        record = json.loads(raw) if isinstance(raw, str) else None
        if not isinstance(record, dict):
            return False
        if code_sha is None:
            from gateway.serving_identity import _static_identity_fields

            code_sha = _static_identity_fields().get("code_sha")
        from gateway.serving_codedelta import usable_sha

        next_sha = code_sha if usable_sha(code_sha) else record.get("code_sha")
        if record.get("note") == note and record.get("code_sha") == next_sha:
            return False
        record["note"] = note
        record["code_sha"] = next_sha
        db.set_meta(_meta_key(session_id), json.dumps(record))
        return True
    except Exception:
        logger.debug("serving transition: note confirm failed", exc_info=True)
        return False


def session_turn_notes(
    store: Any, session_id: Optional[str], runner: Any
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """``(identity note | None, transition note | None, code-delta note | None,
    pending note | None)`` with the durable comparison inside.

    Reads the session's last-served record, renders the identity/transition
    pair via ``gateway.serving_identity.serving_turn_notes`` (a note the
    record proves seen is suppressed) plus the code-delta note
    (:mod:`gateway.serving_codedelta` — a build change between the
    predecessor and this process), then records THIS process as the last
    server (on change only). Read-before-write ordering matters: the notes
    must describe the state BEFORE this turn's record lands; persistence
    failure never breaks the turn — unproven notes still render.

    The stage write carries the process identity but NOT the new note proof,
    and keeps the PREVIOUS record's code_sha when a parseable predecessor
    exists (a build change is settled by the stamp ack, not by staging): the
    proofs only become durable once the note's sidecar stamp lands
    (:func:`confirm_served_note`, called from
    ``agent.turn_context._persist_turn_user_api_content``). With no
    parseable predecessor the stage write establishes this build's sha — the
    first turn rides the delta-free path and records what ran. The returned
    pending note is the identity the stamp ack must confirm — the note just
    emitted, or (on a suppressed repeat) the stored proof it matched, which
    is already durable and needs no write.
    """
    from gateway.serving_codedelta import code_delta_note
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
    snapshot = serving_identity_snapshot(runner)
    identity_note, transition_note = serving_turn_notes(runner, previous)
    # The build delta rides the process handoff: the record's sha proof
    # decides (not transition_state — staging rewrites pid/start while the
    # sha proof stays at its previous value, so a crashed turn re-emits).
    delta_note = code_delta_note(previous, snapshot.get("code_sha"))
    # The note the stamp ack must confirm: the one just emitted, or — on a
    # suppressed repeat — the stored one it matched (a no-op confirm).
    pending_note = identity_note or (previous or {}).get("note")
    # Record THIS process as the last server, on change only — but keep the
    # PREVIOUS note proof and the PREVIOUS record's code_sha: neither is
    # durable until the sidecar stamp lands, and a turn that never reaches
    # the stamp must re-emit them.
    try:
        record_served_identity(
            store, session_id, snapshot,
            (previous or {}).get("note"),
            code_sha=previous.get("code_sha") if previous else snapshot.get("code_sha"))
    except Exception:
        logger.debug("serving transition: record step failed", exc_info=True)
    return identity_note, transition_note, delta_note, pending_note
