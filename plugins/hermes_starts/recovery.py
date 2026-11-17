"""Anchor recovery for starts whose send was abandoned mid-flight.

A ``start_conversation`` call can time out at the tool level while its
worker thread is still inside the Discord send (pc_c965dac61862): the
opening exists in #inbox, but the thread, listener mark, session seed and
history entry were never written, and the state lock stays pinned until
the abandoned worker dies. ``action='recover'`` reconciles from the one
fact that survived — the anchor message id — without reposting the
opening.

The decision contract this module executes has a Go twin,
``internal/startrecovery``, which mirrors it as pure functions.

Like ``threads.py``, this sibling late-imports the package internals so
the dependency points one way; ``hermes_starts`` never imports a consumer.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any, Dict, List, Optional, Tuple


def _handle_recover(args: Dict[str, Any], token: str) -> str:
    from plugins.hermes_starts import (
        _RECOVER_LOCK_TIMEOUT_SECONDS,
        _StateLockUnavailable,
        _StateUnreadable,
        _state_lock,
    )

    anchor_id = str(args.get("anchor_id") or "").strip()
    if not anchor_id:
        return json.dumps({
            "success": False,
            "error": "recover requires the anchor message id of the posted opening",
        })
    start_number: Optional[int] = None
    raw_number = args.get("start_number")
    if raw_number is not None and str(raw_number).strip() != "":
        try:
            start_number = int(raw_number)
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "start_number must be an integer"})

    try:
        with _state_lock(timeout=_RECOVER_LOCK_TIMEOUT_SECONDS):
            return _recover_locked(anchor_id, start_number, token)
    except _StateLockUnavailable as exc:
        return json.dumps({"success": False, "error": str(exc)})
    except _StateUnreadable as exc:
        return json.dumps({
            "success": False,
            "error": (
                f"recover not attempted: hermes_starts state is unreadable "
                f"({exc}); repair or remove hermes_starts/state.json explicitly"
            ),
        })
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _match_reservation(
    state: Dict[str, Any], anchor_id: str, start_number: Optional[int]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(reservation, error)``: the pending start an anchor belongs to.

    A recorded anchor id is authoritative. Without one — the hang happened
    during the anchor POST itself — only a caller who names the reserved
    number may adopt, and the fetched message's content is verified before
    any write.
    """
    from plugins.hermes_starts import _RESERVATION_KEY

    reservations = state.get(_RESERVATION_KEY)
    rows = [r for r in reservations if isinstance(r, dict)] if isinstance(reservations, list) else []
    for row in rows:
        if str(row.get("anchor_id") or "") == anchor_id and anchor_id:
            return row, None
    if start_number is not None:
        for row in rows:
            if not str(row.get("anchor_id") or "") and int(row.get("number") or 0) == start_number:
                return row, None
        return None, (
            f"anchor {anchor_id} matches no pending start and no history entry"
        )
    return None, (
        f"anchor {anchor_id} matches no reservation with a recorded anchor "
        "id; retry with the start_number of the reservation whose opening "
        "it is (content is verified before adopting)"
    )


def _recover_locked(anchor_id: str, start_number: Optional[int], token: str) -> str:
    """Reconcile one anchor. Caller holds the state lock."""
    from plugins.hermes_starts import (
        _DISCORD_API_BASE,
        _RESERVATION_KEY,
        _add_thread_member,
        _create_thread_for_message,
        _discord_request,
        _finalize_committed_start,
        _load_state,
        _mention_user_id,
        _post_channel_message,
        _read_history,
        _split_delivery,
        _update_reservation,
    )

    state = _load_state(strict=True)
    channel_id = str(state.get("channel_id") or "")
    if not channel_id:
        return json.dumps({
            "success": False,
            "error": "no provisioned inbox channel; run action='setup' first",
        })

    # History first, before any network write: a recovered start is
    # idempotent, and the anchor's history entry is the proof it finished.
    entries, _corrupt = _read_history(state)
    for entry in entries:
        if entry["id"] == anchor_id:
            return json.dumps({
                "success": True,
                "already_recovered": True,
                "start_number": entry["number"],
                "anchor_id": anchor_id,
                "thread_id": entry["thread_id"],
            })

    reservation, match_error = _match_reservation(state, anchor_id, start_number)
    if reservation is None:
        return json.dumps({"success": False, "error": match_error})
    number = int(reservation.get("number") or 0)
    kind = str(reservation.get("kind") or "")
    composed = str(reservation.get("text") or "")
    thread_name = f"Start #{number} — {kind}"

    # The anchor is fetched before anything is written: a message id
    # Discord no longer knows is a clean refusal, not a recovery.
    message = _discord_request(
        token, "GET", f"{_DISCORD_API_BASE}/channels/{channel_id}/messages/{anchor_id}"
    )
    content = str(message.get("content") or "")
    mention_uid = _mention_user_id()
    parts = _split_delivery(composed, mention_uid)

    if not str(reservation.get("anchor_id") or ""):
        if content != parts[0]:
            return json.dumps({
                "success": False,
                "error": (
                    f"anchor {anchor_id} content does not match the first "
                    f"delivery part of start #{number}; refusing to adopt"
                ),
            })
        _update_reservation(number, stage="anchor_posted", anchor_id=anchor_id)

    thread = message.get("thread")
    thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
    if not thread_id:
        thread_id = _create_thread_for_message(token, channel_id, anchor_id, thread_name)
        _update_reservation(number, stage="thread_created", thread_id=thread_id)

    warnings: List[str] = []
    if mention_uid:
        try:
            _add_thread_member(token, thread_id, mention_uid)
        except Exception as exc:
            warnings.append(
                f"thread member add failed for {mention_uid}: {type(exc).__name__}: {exc}"
            )

    # Only tail parts the thread does not already hold are posted — the
    # abandoned worker may have gotten further than its last milestone.
    existing = _discord_request(
        token, "GET", f"{_DISCORD_API_BASE}/channels/{thread_id}/messages"
    )
    rows = existing.get("data") if isinstance(existing.get("data"), list) else []
    present = {str(row.get("content") or "") for row in rows if isinstance(row, dict)}
    thread_message_ids = [
        _post_channel_message(token, thread_id, part)
        for part in parts[1:]
        if part not in present
    ]

    result = _finalize_committed_start(
        number,
        kind,
        composed,
        anchor_id,
        thread_id,
        thread_name,
        channel_id,
        [anchor_id],
        thread_message_ids,
        warnings,
    )
    result["action"] = "recover"
    result["recovered"] = True
    return json.dumps(result)
