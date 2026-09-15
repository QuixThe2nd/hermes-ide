"""Durable queue for messages that arrive while the gateway is draining.

``/restart`` closes admission (``_draining``) while in-flight work finishes.
Until this module, every message that landed in that window was either
refused (idle sessions: the drain-gate notice — the message was simply gone)
or parked in the adapter's in-memory ``_pending_messages`` FIFO (busy
sessions) — a queue that dies with the process bounce, so the "queued for
the next turn after it comes back" ack was a promise nothing could keep.

This module copies the cooperative-restart receipt pattern
(``gateway/restart_wind_down.py``): every drain-time queueing appends the
event to one JSON snapshot in the gateway state dir, and gateway startup
replays that snapshot — re-injecting each event into the owning adapter's
pending FIFO and starting the turn — before the resume scheduler runs.

Ownership of the underlying semantics stays where it was:

- the drain gate (``_slash_drain_gate_notice`` / ``_DRAIN_ALLOWED_COMMANDS``)
  stays the authoritative idle-path verdict; queueing only replaces what the
  gate does with a *refused* plain-text/media event, never a command;
- the busy path's ``_queue_or_replace_pending_event`` (photo merge, FIFO
  promotion, ``_BUSY_QUEUE_MAX_PENDING`` cap) stays the queue mechanics —
  both the drain-time append and the startup replay route through it;
- startup replay reuses the resume-scheduler's task dispatch
  (``_run_startup_resume_event``), so replayed turns are waited on by the
  same startup-restore gate as boot auto-resume turns.

Replay never loses an acknowledged message to a crash: the snapshot is
claimed by an atomic rename BEFORE any event is injected, and a session
group's records leave the claim ledger the moment their consumption is
secured (turn dispatched, or a live/resumed turn owns the FIFO) — that
rewrite IS the per-record dispatch state. A claim left by a crashed replay
therefore holds exactly the un-delivered records, and the next boot RESUMES
it instead of discarding it; the only duplicate window is a turn that fully
completes between its dispatch and its ledger rewrite, and a duplicate
answer outranks a lost one. Events whose adapter is not live are retained,
not dropped: their records return to the live snapshot and retry at the
platform's reconnect or the next boot; when that write-back fails, the
claim file — the sole copy — is kept and retried after storage recovers.

Under ``gateway.multiplex_profiles`` each profile's handler queues under
its own home, so startup replays every served profile's queue, not just
the launch home's.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

DRAIN_QUEUE_FILENAME = "drain_message_queue.json"
# Claim marker: the live snapshot is renamed here before injection. While a
# replay runs the claim file doubles as the DISPATCH LEDGER — records are
# rewritten out of it as their consumption is secured — so a crash mid-replay
# leaves exactly the un-delivered set, which the next boot resumes (never
# discards, never re-reads as a fresh queue).
DRAIN_QUEUE_CLAIMED_FILENAME = "drain_message_queue.replaying.json"

_SNAPSHOT_VERSION = 1

# Routing-relevant SessionSource fields. Session-key derivation
# (``build_session_key``) and reply anchoring must see the same values the
# live event carried, or a replayed message would land on a different
# session than the one the user was talking to.
#
# Deliberately EXCLUDED (the source dataclass's own wire-invisible trust
# signals — its field docs say they must never be restorable from
# persistence, and the snapshot is a persistence medium):
# ``delivered_via_upstream_relay`` and ``profile_route_rejected``. A record
# carrying either would hand whatever process reads it an authorization it
# was never re-granted.
_SOURCE_FIELDS = (
    "platform",
    "chat_id",
    "chat_name",
    "chat_type",
    "user_id",
    "user_name",
    "thread_id",
    "chat_topic",
    "user_id_alt",
    "chat_id_alt",
    "scope_id",
    "guild_id",
    "parent_chat_id",
    "message_id",
    "role_authorized",
    "profile",
    "is_bot",
    "auto_thread_created",
    "auto_thread_initial_name",
    "prospective_thread_id",
    "bot_display_name",
)

# MessageEvent fields persisted for replay. Media paths survive the bounce
# (they live under HERMES_HOME), so the replayed event re-enters the same
# vision/STT preprocessing a fresh message would. ``text`` is deliberately
# NOT in this tuple: it is a required positional on ``MessageEvent`` and the
# generic skip-empty filter below would drop an empty caption, so serialize
# always emits it explicitly (empty string included).
_EVENT_FIELDS = (
    "message_id",
    "ledger_message_id",
    "user_id",
    "user_name",
    "reply_to_message_id",
    "reply_to_text",
    "reply_to_author_id",
    "reply_to_author_name",
    "reply_to_is_own_message",
    "auto_skill",
    # Channel-scoped instructions ride along so the replayed event re-enters
    # the turn with the same per-channel system prompt and backfilled
    # context the original inbound message carried — without them a drain
    # would silently strip the channel's instructions from the queued
    # message. They are applied at API call time and never persisted to the
    # transcript, so the queue snapshot is not a second transcript copy.
    "channel_prompt",
    "channel_context",
    "internal",
    "allow_gateway_control",
    "redelivered",
)


def drain_queue_path(home: Optional[Path] = None) -> Path:
    """Live snapshot path, beside ``cooperative_restart_resume.json``.

    *home* pins the profile home the queue belongs to — startup replay under
    ``gateway.multiplex_profiles`` reads every served profile's file. The
    default resolves the ambient home, which the drain-time append path
    relies on: it always runs inside the owning profile's home scope.
    """
    base = Path(home) if home is not None else get_hermes_home()
    return base / "gateway" / DRAIN_QUEUE_FILENAME


def claimed_drain_queue_path(home: Optional[Path] = None) -> Path:
    return drain_queue_path(home).parent / DRAIN_QUEUE_CLAIMED_FILENAME


# ── (de)serialization — the single pair, built on the existing event model ──


def _json_safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the JSON-encodable metadata entries (ids, flags, short strings).

    Metadata is free-form; a connector could stash an exotic object there.
    Dropping the exotic entry keeps the WHOLE event queueable — the honest
    alternative (fail the append) would refuse the message over a
    decoration. Observed keys across the platform connectors are plain
    routing ids/flags, so this drops nothing in practice.
    """
    kept: Dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)):
            kept[str(key)] = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            kept[str(key)] = list(value)
    return kept


def serialize_drain_event(session_key: str, event: MessageEvent) -> Dict[str, Any]:
    """Flatten one queued event + its source into a JSON-safe record.

    The FULL metadata dict is persisted, not just the security keys
    ``_queue_or_replace_pending_event`` compares: routing metadata such as
    ``whatsapp_from_owner`` decides how the replayed message is treated, and
    the snapshot already lives in the gateway state dir beside auth.json at
    the same trust level. A survey of the connectors found no display
    secrets in metadata — only ids and flags — so no denylist is needed;
    ``_SOURCE_FIELDS`` documents the two source-level trust flags that stay
    off disk.
    """
    source = getattr(event, "source", None)
    if source is None:
        raise ValueError("drain queue: event has no source")
    platform = getattr(source, "platform", None)
    if platform is None:
        raise ValueError("drain queue: event source has no platform")

    source_data: Dict[str, Any] = {"platform": platform.value}
    for field in _SOURCE_FIELDS:
        if field == "platform":
            continue
        value = getattr(source, field, None)
        if value is not None:
            source_data[field] = value

    event_data: Dict[str, Any] = {
        # Always present, empty string included: ``text`` is a required
        # positional on ``MessageEvent``, so a record without the key could
        # never be rebuilt (a caption-less photo used to die right here).
        "text": str(getattr(event, "text", "") or ""),
        "message_type": (getattr(event, "message_type", None) or MessageType.TEXT).value,
        "media_urls": list(getattr(event, "media_urls", None) or []),
        "media_types": list(getattr(event, "media_types", None) or []),
        "media_text_inlined": list(getattr(event, "media_text_inlined", None) or []),
    }
    for field in _EVENT_FIELDS:
        value = getattr(event, field, None)
        if value is not None and not (isinstance(value, (list, str)) and not value):
            event_data[field] = value
    metadata = getattr(event, "metadata", None) or {}
    if metadata:
        event_data["metadata"] = _json_safe_metadata(metadata)
    timestamp = getattr(event, "timestamp", None)
    return {
        "session_key": str(session_key or ""),
        "queued_at": time.time(),
        "source": source_data,
        "event": event_data,
        "event_timestamp": timestamp.isoformat() if timestamp is not None else None,
    }


def deserialize_drain_event(record: Any) -> Tuple[str, MessageEvent]:
    """Rebuild ``(session_key, MessageEvent)`` from a snapshot record.

    Raises ``ValueError`` on any structurally invalid record so the caller
    can quarantine a corrupt snapshot without crashing startup.
    """
    from gateway.config import Platform
    from gateway.session import SessionSource

    if not isinstance(record, dict):
        raise ValueError("drain queue: record is not an object")
    session_key = str(record.get("session_key") or "")
    source_data = record.get("source")
    event_data = record.get("event")
    if not session_key or not isinstance(source_data, dict) or not isinstance(event_data, dict):
        raise ValueError("drain queue: record missing session_key/source/event")

    platform_name = source_data.get("platform")
    try:
        platform = Platform(str(platform_name))
    except ValueError as exc:
        raise ValueError(f"drain queue: unknown platform {platform_name!r}") from exc

    source_kwargs = {}
    for field in _SOURCE_FIELDS:
        if field == "platform":
            continue
        value = source_data.get(field)
        if value is not None:
            source_kwargs[field] = value
    source = SessionSource(platform=platform, **source_kwargs)

    event_kwargs: Dict[str, Any] = {"source": source}
    # Mirror of serialize: the key is always emitted, but tolerate a record
    # from an older writer that omitted it for an empty caption.
    event_kwargs["text"] = str(event_data.get("text") or "")
    message_type_name = event_data.get("message_type") or MessageType.TEXT.value
    try:
        event_kwargs["message_type"] = MessageType(str(message_type_name))
    except ValueError:
        event_kwargs["message_type"] = MessageType.TEXT
    for field in _EVENT_FIELDS:
        value = event_data.get(field)
        if value is not None:
            event_kwargs[field] = value
    for field in ("media_urls", "media_types", "media_text_inlined"):
        value = event_data.get(field)
        if isinstance(value, list):
            event_kwargs[field] = list(value)
    metadata = event_data.get("metadata")
    if isinstance(metadata, dict):
        event_kwargs["metadata"] = {
            str(key): value for key, value in metadata.items()
        }
    timestamp = record.get("event_timestamp")
    if isinstance(timestamp, str) and timestamp:
        try:
            event_kwargs["timestamp"] = datetime.fromisoformat(timestamp)
        except ValueError:
            pass
    return session_key, MessageEvent(**event_kwargs)


# ── drain-time append ────────────────────────────────────────────────────────


def _quarantine_snapshot(path: Path, reason: str) -> None:
    """Rename an unreadable snapshot aside instead of overwriting it.

    A corrupt file still holds the only copy of whatever the previous
    process queued; letting the next append overwrite it would discard the
    bytes silently. The timestamped ``.bad`` sibling keeps them for
    forensics while the caller proceeds with a fresh snapshot. Best effort:
    a quarantine failure is logged and never raises.
    """
    sibling = path.with_name(f"{path.name}.bad-{time.time_ns()}")
    try:
        os.replace(path, sibling)
    except OSError:
        logger.warning(
            "Drain queue snapshot unreadable (%s) and could not be "
            "quarantined; starting a fresh one",
            reason,
            exc_info=True,
        )
        return
    logger.warning(
        "Drain queue snapshot unreadable (%s); quarantined as %s and "
        "starting a fresh one",
        reason,
        sibling.name,
    )


def _load_snapshot_events(path: Path) -> List[Dict[str, Any]]:
    """Read the events list from *path*; missing → empty; corrupt → the file
    is quarantined as a ``.bad`` sibling (unreadable data is never
    overwritten in place) and a fresh snapshot starts."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        _quarantine_snapshot(path, str(exc))
        return []
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        _quarantine_snapshot(path, "'events' missing or not a list")
        return []
    return [event for event in events if isinstance(event, dict)]


def record_drain_event(runner: Any, session_key: str, event: MessageEvent) -> bool:
    """Append one event to the durable drain snapshot.

    Returns True only when the event is durably on disk; the caller must
    answer with the OLD refusal on False so the ack never promises a queue
    that does not exist. The per-session cap reuses the runner's
    ``_BUSY_QUEUE_MAX_PENDING`` and counts the LOGICAL backlog — the union
    of the in-memory FIFO and the durable snapshot. Once drain queueing
    starts, every accepted event exists in BOTH pools (durable append, then
    the FIFO enqueue), so the union is the larger of the two depths;
    summing them counted each drain-window event twice and refused the
    message that hit cap/2 (the 17th at the default cap of 32, with an
    empty pre-drain backlog). A pre-drain backlog lives only in the FIFO;
    records retained for a platform that was down at boot live only in the
    snapshot — ``max`` counts each logical message once.

    A re-delivered copy of a message id already recorded for this session
    is dropped idempotently (one log line): the first copy IS durably on
    disk, so True stays an honest ack — the queue holds the message, just
    not twice. Without this, a platform re-delivery inside the drain window
    wrote N records and the boot replay injected N turns for one message.
    """
    path = drain_queue_path()
    events = _load_snapshot_events(path)
    cap = int(getattr(runner, "_BUSY_QUEUE_MAX_PENDING", 32))
    session = str(session_key or "")
    message_id = str(getattr(event, "message_id", "") or "").strip()
    # Internal (synthetic) events are never deduped by id: background-process watchers
    # inherit the spawning turn's reply anchor (``HERMES_SESSION_MESSAGE_ID``), so two
    # distinct completions can share one id — dropping the second loses its model turn.
    if message_id and not getattr(event, "internal", False):
        for item in events:
            if item.get("session_key") != session:
                continue
            existing_id = str(
                (item.get("event") or {}).get("message_id", "") or ""
            ).strip()
            if existing_id == message_id:
                logger.info(
                    "Drain queue already holds message id %s for session %s — "
                    "dropping the re-delivered copy (one platform message, "
                    "one queued turn)",
                    message_id,
                    session,
                )
                return True
    durable_depth = sum(1 for item in events if item.get("session_key") == session)
    in_memory_depth = 0
    depth_of = getattr(runner, "_queue_depth", None)
    adapter = None
    if callable(depth_of):
        try:
            adapter = runner._adapter_for_source(event.source)
            in_memory_depth = int(depth_of(session, adapter=adapter) or 0)
        except Exception:
            logger.debug(
                "Drain queue in-memory depth check failed for %s", session, exc_info=True
            )
            in_memory_depth = 0
    if max(durable_depth, in_memory_depth) >= cap:
        logger.warning(
            "Dropping drain-time message for session %s — pending backlog at "
            "cap (%d logical event(s); in-memory FIFO %d, durable snapshot %d).",
            session,
            cap,
            in_memory_depth,
            durable_depth,
        )
        return False
    try:
        record = serialize_drain_event(session, event)
    except Exception as exc:
        logger.warning("Drain queue serialization failed for %s: %s", session, exc)
        return False
    events.append(record)
    try:
        atomic_json_write(
            path,
            {"version": _SNAPSHOT_VERSION, "events": events},
            indent=None,
        )
    except Exception:
        logger.warning(
            "Drain queue snapshot write failed; refusing to promise a queued turn",
            exc_info=True,
        )
        return False
    return True


def queue_drain_refused_message(
    runner: Any, event: MessageEvent, session_key: str
) -> Optional[str]:
    """Idle-path drain admission for a plain-text/media event the gate refused.

    Returns the queued-for-next-turn ack, or None when queueing is not
    allowed or failed — the caller then answers with the drain-gate notice
    it already built (honest refusal instead of a false promise).

    Posture (aligned with the busy path's ``_queue_during_drain_enabled``):
    the queued ack is promised only when a RESTART was requested and the
    effective busy input mode says messages survive it (queue/steer). An
    external quiesce (``_enter_external_drain`` sets ``_draining`` without
    ``_restart_requested``) gets None — the caller's plain drain notice, the
    same honest refusal the busy path gives under that state — because an
    externally stopped gateway has no restart of its own that would replay
    this snapshot; queueing would park the message for a comeback nobody
    promised (it would replay only at some LATER restart).
    """
    if not getattr(runner, "_draining", False):
        return None
    enabled = getattr(runner, "_queue_during_drain_enabled", None)
    if not callable(enabled):
        return None
    mode_of = getattr(runner, "_effective_busy_input_mode", None)
    effective_mode = mode_of(event.source) if callable(mode_of) else None
    if not enabled(effective_mode):
        return None
    if not record_drain_event(runner, session_key, event):
        return None
    runner._queue_or_replace_pending_event(session_key, event)
    return (
        f"⏳ Gateway {runner._status_action_gerund()} — queued for the next "
        f"turn after it comes back."
    )


def queue_drain_busy_message(runner: Any, event: MessageEvent, session_key: str) -> bool:
    """Busy-path drain queueing: durable snapshot first, in-memory FIFO second.

    The busy path's own policy (``_queue_during_drain_enabled``) decides
    whether this is called at all. Returns False when the durable append
    failed (cap / serialize / IO) — the caller keeps the old refusal text.
    """
    if not record_drain_event(runner, session_key, event):
        return False
    runner._queue_or_replace_pending_event(session_key, event)
    return True


# ── startup replay ───────────────────────────────────────────────────────────


def _parse_claim_records(
    path: Path,
) -> Optional[List[Tuple[str, MessageEvent, Dict[str, Any]]]]:
    """Parse a claimed snapshot into ``(session_key, event, raw_record)`` triples.

    The raw record rides along so a pass that cannot inject an event can
    write the ORIGINAL bytes back without a re-serialize round trip. A
    single unreadable RECORD only costs that record (dropped + logged) — the
    good ones in the same file still replay, so one torn row cannot discard
    a whole drain window's messages. An unreadable FILE is quarantined as a
    ``.bad`` sibling (its bytes may be all that is left of a drain window)
    and returns None: startup must proceed either way.
    """
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(raw_events, list):
            raise ValueError("snapshot 'events' is not a list")
    except Exception as exc:
        logger.warning(
            "Drain queue snapshot corrupt after claim (%s); quarantined and "
            "continuing startup: %s",
            path.name,
            exc,
        )
        _quarantine_snapshot(path, str(exc))
        return None
    parsed: List[Tuple[str, MessageEvent, Dict[str, Any]]] = []
    dropped = 0
    for item in raw_events:
        try:
            session_key, event = deserialize_drain_event(item)
            parsed.append((session_key, event, item))
        except Exception as exc:
            dropped += 1
            logger.warning(
                "Drain queue record unreadable; dropping that record and "
                "replaying the rest: %s",
                exc,
            )
    if dropped:
        logger.warning(
            "Dropped %d unreadable drain queue record(s) out of %d",
            dropped,
            len(raw_events),
        )
    return parsed


def claim_drain_queue(
    home: Optional[Path] = None,
) -> Tuple[List[Tuple[str, MessageEvent, Dict[str, Any]]], bool]:
    """Claim the snapshot atomically; it becomes the replay's dispatch ledger.

    Returns ``(records, folded_live)``; an empty list means nothing to
    replay. Claiming renames the live file to the claim marker, so an
    injection crash can never be re-read as a fresh queue.

    A claim marker left by a CRASHED replay is resumed, never discarded: it
    holds exactly the records whose dispatch was never durably marked
    (settled groups are rewritten out of it as they are consumed), so
    dropping it would delete every acknowledged message the crash had not
    yet delivered. A live snapshot written by a LATER drain window (after
    that crash) was never claimed — its records are folded into this pass
    and the file removed up-front, so a crash mid-pass cannot re-fold
    already-settled records on the next boot.
    """
    claimed = claimed_drain_queue_path(home)
    resuming = claimed.exists()
    if resuming:
        logger.warning(
            "Resuming drain queue claim left by an interrupted replay "
            "(records whose delivery was never confirmed): %s",
            claimed.name,
        )
    else:
        path = drain_queue_path(home)
        if not path.exists():
            return [], False
        try:
            os.replace(path, claimed)
        except OSError:
            logger.warning(
                "Drain queue claim failed; skipping replay this boot", exc_info=True
            )
            return [], False
    parsed = _parse_claim_records(claimed)
    if parsed is None or not resuming:
        return parsed or [], False
    live = drain_queue_path(home)
    if not live.exists():
        return parsed, False
    folded = _parse_claim_records(live)
    if not folded:
        return parsed, False
    try:
        live.unlink()
    except OSError:
        logger.debug("Could not remove folded drain queue snapshot", exc_info=True)
    return parsed + folded, True


def clear_claimed_drain_queue(home: Optional[Path] = None) -> None:
    """Delete the claim marker after every record is settled (or retained)."""
    try:
        claimed_drain_queue_path(home).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove drain queue claim marker", exc_info=True)


def _pop_replay_head(runner: Any, adapter: Any, session_key: str) -> Optional[MessageEvent]:
    """Take the head event out of the FIFO so it runs as THIS turn.

    Mirrors the /queue flush shape: the slot's event starts the turn, the
    overflow tail stays parked for the post-turn drain to promote in arrival
    order.
    """
    pending_slot = getattr(adapter, "_pending_messages", None)
    if isinstance(pending_slot, dict):
        head = pending_slot.pop(session_key, None)
        if head is not None:
            return head
    peek_state = getattr(runner, "_peek_session_state", None)
    _q_state = peek_state(session_key) if callable(peek_state) else None
    conversation = getattr(_q_state, "conversation", None) if _q_state is not None else None
    overflow = getattr(conversation, "queued_events", None) if conversation is not None else None
    if overflow:
        return overflow.pop(0)
    return None


def _rewrite_claim_ledger(
    pending: Dict[str, List[Dict[str, Any]]], home: Optional[Path] = None
) -> bool:
    """Atomically persist the still-owed records as the claim ledger."""
    records = [record for group in pending.values() for record in group]
    try:
        atomic_json_write(
            claimed_drain_queue_path(home),
            {"version": _SNAPSHOT_VERSION, "events": records},
            indent=None,
        )
        return True
    except Exception:
        logger.error(
            "Drain queue dispatch-ledger rewrite failed; %d message(s) stay "
            "claimed and will be re-replayed on the next boot (a duplicate "
            "turn is possible; a lost one is not)",
            len(records),
            exc_info=True,
        )
        return False


def _mark_group_dispatched(
    pending: Dict[str, List[Dict[str, Any]]],
    session_key: str,
    home: Optional[Path] = None,
) -> None:
    """Durably mark one session group's records as dispatched.

    This rewrite is the per-record dispatch state: a record's presence in
    the claim ledger means "delivery not yet confirmed", so a crash right
    after a dispatch leaves the group in the ledger and the next boot
    re-replays it — at-least-once with a window of one rewrite, instead of
    the old unconditional claim discard that lost everything the crashed
    replay had acknowledged but not yet delivered.
    """
    records = pending.pop(session_key, None)
    if not records:
        return
    if _rewrite_claim_ledger(pending, home):
        return
    pending[session_key] = records  # keep the in-memory mirror honest


def _unlink_folded_live(home: Optional[Path] = None) -> None:
    """Remove a folded snapshot left on disk (its records joined the pass)."""
    try:
        drain_queue_path(home).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove folded drain queue snapshot", exc_info=True)


def _write_back_retained_records(
    records: List[Dict[str, Any]],
    home: Optional[Path] = None,
    folded_live: bool = False,
) -> bool:
    """Return never-injected records to the live snapshot (merge, never clobber).

    The claim ledger is rewritten to hold exactly *records* and then renamed
    onto the live path, so the retained set is durably retryable at the
    platform's reconnect or the next boot, and never exists in both files at
    once. ``folded_live`` marks a pass that folded (and removed) a later
    live snapshot: that content is already accounted for in this pass, so
    the merge must not read the path again and resurrect dispatched records.

    Returns False when either step fails — the claim file then remains the
    sole durable copy of the retained records, and the caller must KEEP it:
    clearing it would delete the last copy. The next boot resumes the claim
    once storage recovers.
    """
    claim = claimed_drain_queue_path(home)
    path = drain_queue_path(home)
    events: List[Dict[str, Any]] = [] if folded_live else _load_snapshot_events(path)
    events.extend(records)
    try:
        atomic_json_write(
            claim,
            {"version": _SNAPSHOT_VERSION, "events": events},
            indent=None,
        )
    except Exception:
        logger.error(
            "Drain queue write-back failed; keeping the claim file — it still "
            "holds all %d retained message(s) and is retried once storage "
            "recovers",
            len(records),
            exc_info=True,
        )
        return False
    try:
        os.replace(claim, path)
    except OSError:
        logger.error(
            "Drain queue claim could not be released onto the live snapshot; "
            "keeping it — it holds exactly the %d retained message(s)",
            len(records),
            exc_info=True,
        )
        return False
    return True


def _scheduler_will_resume(runner: Any, session_key: str, allowlist: Any) -> bool:
    """True when the resume scheduler would resume ``session_key`` THIS boot.

    Mirrors ``GatewayRunner._schedule_resume_pending_sessions`` exactly —
    the candidate filter (``resume_pending``, not suspended, origin present,
    reason in ``_AUTO_RESUME_REASONS``, allowlist membership) plus the
    freshness gate on ``last_resume_marked_at or updated_at``. Replay may
    only leave a queued message for the scheduler when the scheduler will
    ACTUALLY take it: an allowlist entry alone is not enough, because a
    suspended entry or a stale marker makes the scheduler skip the session —
    and then nobody would ever consume the queued message.
    """
    from gateway.restart_wind_down import should_auto_resume_session
    from gateway.session_lifecycle import auto_continue_freshness_window

    store = getattr(runner, "session_store", None)
    if store is None:
        return False
    try:
        with store._lock:  # noqa: SLF001 — same locked snapshot the scheduler reads
            store._ensure_loaded_locked()  # noqa: SLF001
            entries = store._entries  # noqa: SLF001
            entry = entries.get(session_key) if isinstance(entries, dict) else None
            resume_pending = bool(getattr(entry, "resume_pending", False))
            suspended = bool(getattr(entry, "suspended", False))
            origin = getattr(entry, "origin", None)
            reason = getattr(entry, "resume_reason", None)
            marker = getattr(entry, "last_resume_marked_at", None) or getattr(
                entry, "updated_at", None
            )
    except Exception:
        logger.debug(
            "Drain replay: resume-pending lookup failed for %s; replay owns "
            "the turn",
            session_key,
            exc_info=True,
        )
        return False
    reasons = getattr(runner, "_AUTO_RESUME_REASONS", frozenset())
    if not resume_pending or suspended or origin is None or reason not in reasons:
        return False
    if not should_auto_resume_session(session_key, allowlist):
        return False
    if marker is not None:
        window = auto_continue_freshness_window()
        if (datetime.now() - marker).total_seconds() > window:
            return False
    return True


def replay_drain_queue(
    runner: Any,
    platform: Optional[Platform] = None,
    home: Optional[Path] = None,
) -> int:
    """Replay the drain snapshot into the live queues; returns turns started.

    Called from gateway ``start()`` after adapters are ready and before the
    resume scheduler runs (and, scoped to one platform, from the reconnect
    watcher). *home* pins the profile home whose queue is replayed — under
    ``multiplex_profiles`` startup goes through
    ``replay_drain_queues_for_profiles`` so every served profile's file is
    claimed; the drain-time append always writes the ambient (profile-scoped)
    home. Every event re-enters the owning adapter's FIFO through
    ``_queue_or_replace_pending_event`` (same merge/cap semantics as live
    queueing). Sessions in the cooperative-restart resume set only enqueue —
    their resumed turn picks pending events up at its post-turn drain. Every
    other affected session gets a turn started for its head event through
    the resume scheduler's dispatch path, so the queued text is answered
    without waiting for the user to speak again.

    Durability contract: a group's records leave the claim ledger only once
    their consumption is secured (turn dispatched, or a live/scheduler-owned
    turn will drain the FIFO), so a crash mid-replay leaves exactly the
    un-delivered records claimed and the next boot resumes them. Events
    whose adapter is not live (platform down at boot) are NOT dropped:
    their records return to the live snapshot and retry at the next natural
    point — the platform's reconnect or the next boot. ``platform`` scopes a
    retry pass to one platform; other platforms' records stay queued
    untouched.
    """
    claimed, folded_live = claim_drain_queue(home)
    if not claimed:
        if claimed_drain_queue_path(home).exists():
            # A claim whose records were all unreadable: nothing to deliver,
            # but the marker must not outlive the pass (the next boot would
            # treat the corpse as a crashed replay's ledger).
            clear_claimed_drain_queue(home)
        return 0

    grouped: Dict[str, List[Tuple[MessageEvent, Dict[str, Any]]]] = {}
    dropped_redeliveries = 0
    for session_key, event, record in claimed:
        group = grouped.setdefault(session_key, [])
        message_id = str(getattr(event, "message_id", "") or "").strip()
        if message_id and not getattr(event, "internal", False) and any(
            str(getattr(existing, "message_id", "") or "").strip() == message_id
            for existing, _existing_record in group
        ):
            dropped_redeliveries += 1
            continue
        group.append((event, record))
    if dropped_redeliveries:
        logger.info(
            "Dropped %d re-delivered drain record(s) by message id — one "
            "platform message, one replayed turn",
            dropped_redeliveries,
        )

    # In-memory mirror of the claim ledger: the records still owed a
    # delivery. Groups leave it (and the on-disk ledger via
    # ``_mark_group_dispatched``) as their consumption is secured.
    pending: Dict[str, List[Dict[str, Any]]] = {
        session_key: [record for _event, record in items]
        for session_key, items in grouped.items()
    }
    injected: Dict[str, Any] = {}
    for session_key, items in grouped.items():
        source = items[0][0].source
        if platform is not None and getattr(source, "platform", None) != platform:
            # Scoped pass (platform reconnect): other platforms keep waiting.
            continue
        adapter = runner._adapter_for_source(source)
        if adapter is None:
            logger.warning(
                "Retaining %d drain-queued message(s) for %s: no live adapter "
                "yet — retried on reconnect or at next boot",
                len(items),
                session_key,
            )
            continue
        for event, _record in items:
            # A replay injection is a re-delivery of an already-received
            # platform message: mark the event so the model can tell it from
            # a fresh user message and answer it once, not per copy.
            event.redelivered = True
            runner._queue_or_replace_pending_event(session_key, event)
        injected[session_key] = adapter

    allowlist = getattr(runner, "_resume_allowlist_for_this_boot", None)
    raw_allowlist = allowlist() if callable(allowlist) else None
    cooperative = set(raw_allowlist or set())

    started = 0
    for session_key, adapter in injected.items():
        if session_key in cooperative and _scheduler_will_resume(
            runner, session_key, raw_allowlist
        ):
            # The scheduler resumes this session THIS boot; that turn drains
            # the FIFO after it finishes — no second turn from replay, and
            # consumption is as secured as a dispatch would make it.
            _mark_group_dispatched(pending, session_key, home)
            continue
        if runner._is_session_running(session_key):
            # Already busy (e.g. boot auto-resume claimed it): the FIFO is
            # consumed by that turn's post-turn drain — no second turn.
            _mark_group_dispatched(pending, session_key, home)
            continue
        head = _pop_replay_head(runner, adapter, session_key)
        if head is None:
            # Injected and the FIFO already consumed them: settled.
            _mark_group_dispatched(pending, session_key, home)
            continue
        if _dispatch_replay_turn(runner, adapter, head, session_key):
            started += 1
            _mark_group_dispatched(pending, session_key, home)
        # A failed dispatch leaves the group in the ledger: its events sit
        # in a FIFO with no turn to drain it, so the next boot/reconnect
        # retries them instead of stranding the messages.

    retained = [record for group in pending.values() for record in group]
    if retained and not _write_back_retained_records(retained, home, folded_live):
        # Write-back failed: the claim file still holds every retained
        # record — the sole surviving copy. KEEP it (clearing it here would
        # delete the messages); the next boot resumes the claim once
        # storage recovers.
        return started
    clear_claimed_drain_queue(home)
    if folded_live and not retained:
        # A folded snapshot with nothing retained was fully consumed by this
        # pass; the early fold usually removed it already.
        _unlink_folded_live(home)
    if not injected:
        return 0
    logger.info(
        "Replaying %d drain-queued message(s) across %d session(s)",
        len(claimed) - len(retained),
        len(injected),
    )
    return started


def replay_drain_queues_for_profiles(
    runner: Any, platform: Optional[Platform] = None
) -> int:
    """Startup/reconnect entry: replay every queue this gateway can own.

    Under ``gateway.multiplex_profiles`` a secondary profile's inbound
    handler runs inside that profile's home scope, so its drain snapshot is
    written under the PROFILE home — one ambient replay would claim only the
    launch home's file and the secondary queues would sit unclaimed until
    some later gateway serving them alone happens to boot. Enumerate the
    served profiles through the same chokepoint gateway startup uses for
    adapter and MCP discovery (``profiles_to_serve``) and replay each home's
    queue explicitly. Single-profile gateways keep exactly the one ambient
    pass they always had.
    """
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return replay_drain_queue(runner, platform=platform)
    from hermes_cli.profiles import profiles_to_serve

    try:
        served = list(
            profiles_to_serve(
                multiplex=True,
                profile_allowlist=getattr(config, "multiplex_profile_allowlist", None),
            )
        )
    except Exception:
        logger.warning(
            "Drain replay could not enumerate the multiplex profiles; "
            "replaying the launch home only",
            exc_info=True,
        )
        return replay_drain_queue(runner, platform=platform)
    started = 0
    for _profile_name, profile_home in served:
        try:
            started += replay_drain_queue(runner, platform=platform, home=profile_home)
        except Exception:
            logger.warning(
                "Drain replay failed for profile home %s", profile_home, exc_info=True
            )
    return started


def _dispatch_replay_turn(runner: Any, adapter: Any, event: MessageEvent, session_key: str) -> bool:
    """Start the replayed turn through the resume scheduler's dispatch path."""
    import asyncio

    from gateway.run import _AGENT_PENDING_SENTINEL

    # Same marker the startup-restore drain sets, so the (still closed)
    # inbound restore gate lets the replayed turn through instead of
    # queueing it behind itself.
    try:
        setattr(event, "_hermes_startup_restore_replay", True)
    except Exception:
        pass
    # Pre-claim the runner slot exactly like _schedule_resume_pending_sessions:
    # between task creation and the task's first await, an inbound message (or
    # the resume scheduler about to run) must see the session as occupied.
    try:
        _resume_state = runner._session_state(session_key)
        _resume_state.turn.agent = _AGENT_PENDING_SENTINEL
        _resume_state.turn.started_ts = time.time()
        runner._persist_active_agents()
    except Exception:
        logger.debug(
            "Drain replay pre-claim failed for %s; dispatching without it",
            session_key,
            exc_info=True,
        )
    try:
        task = asyncio.create_task(
            runner._run_startup_resume_event(adapter, event, session_key)
        )
    except Exception:
        logger.warning("Drain replay turn dispatch failed for %s", session_key, exc_info=True)
        # The pre-claim above holds the running-agent slot and the task that
        # would normally clear it was never created — release it here or the
        # session stays "running" until the process dies.
        release = getattr(runner, "_release_running_agent_state", None)
        if callable(release):
            try:
                release(session_key)
            except Exception:
                logger.debug(
                    "Drain replay slot release failed for %s", session_key, exc_info=True
                )
        return False
    background = getattr(runner, "_background_tasks", None)
    if background is not None:
        background.add(task)
        task.add_done_callback(background.discard)
    if getattr(runner, "_startup_restore_in_progress", False):
        tasks = getattr(runner, "_startup_restore_tasks", None)
        if tasks is None:
            tasks = []
            runner._startup_restore_tasks = tasks
        tasks.append(task)
    return True
