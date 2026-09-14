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

Replay is at-most-once: the snapshot is claimed by an atomic rename BEFORE
any event is injected and deleted after, so a crash mid-replay can never
double-deliver (a leftover claim file is discarded on the next boot).
Events whose adapter is not live are retained, not dropped: their records
return to the live snapshot and retry at the platform's reconnect or the
next boot.
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
# Claim marker: the live snapshot is renamed here before injection, so a
# crash mid-replay leaves this file (discarded on the next boot) instead of
# a live snapshot a later boot would replay a second time.
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
    "internal",
    "allow_gateway_control",
)


def drain_queue_path() -> Path:
    """Live snapshot path, beside ``cooperative_restart_resume.json``."""
    return get_hermes_home() / "gateway" / DRAIN_QUEUE_FILENAME


def claimed_drain_queue_path() -> Path:
    return drain_queue_path().parent / DRAIN_QUEUE_CLAIMED_FILENAME


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
    ``_BUSY_QUEUE_MAX_PENDING`` and counts the in-memory FIFO depth and the
    durable snapshot TOGETHER against that one budget — the two pools are
    two copies of the same backlog, so counting each separately would allow
    up to 2× the cap per session across a drain window.
    """
    path = drain_queue_path()
    events = _load_snapshot_events(path)
    cap = int(getattr(runner, "_BUSY_QUEUE_MAX_PENDING", 32))
    session = str(session_key or "")
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
    if durable_depth + in_memory_depth >= cap:
        logger.warning(
            "Dropping drain-time message for session %s — pending backlog at "
            "cap (%d across in-memory FIFO + durable snapshot; %d + %d).",
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


def claim_drain_queue() -> Optional[List[Tuple[str, MessageEvent, Dict[str, Any]]]]:
    """Claim the snapshot atomically; None when there was nothing to replay.

    Returns ``(session_key, event, raw_record)`` triples — the raw record
    rides along so a pass that cannot inject an event can write the ORIGINAL
    bytes back without a re-serialize round trip.

    Claiming renames the live file to the claim marker, so an injection
    crash can never be re-read as a fresh queue. A leftover marker from a
    previous crashed replay is discarded (never re-injected — at-most-once).
    An unreadable SNAPSHOT is logged and dropped: startup must proceed. A
    single unreadable RECORD only costs that record — the good ones in the
    same file still replay, so one torn row cannot discard a whole drain
    window's messages.
    """
    claimed = claimed_drain_queue_path()
    if claimed.exists():
        logger.warning(
            "Discarding drain queue claim left by an interrupted replay "
            "(events may already have been delivered): %s",
            claimed.name,
        )
        try:
            claimed.unlink()
        except OSError:
            logger.debug("Could not remove stale drain queue claim", exc_info=True)
    path = drain_queue_path()
    if not path.exists():
        return None
    try:
        os.replace(path, claimed)
    except OSError:
        logger.warning("Drain queue claim failed; skipping replay this boot", exc_info=True)
        return None
    import json

    raw_events: Any = None
    try:
        data = json.loads(claimed.read_text(encoding="utf-8"))
        raw_events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(raw_events, list):
            raise ValueError("snapshot 'events' is not a list")
    except Exception as exc:
        logger.warning(
            "Drain queue snapshot corrupt after claim; dropping %s queued "
            "message(s) and continuing startup: %s",
            "unknown" if raw_events is None else len(raw_events),
            exc,
        )
        clear_claimed_drain_queue()
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


def clear_claimed_drain_queue() -> None:
    """Delete the claim marker after injection (end of the at-most-once window)."""
    try:
        claimed_drain_queue_path().unlink(missing_ok=True)
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


def _write_back_retained_records(records: List[Dict[str, Any]]) -> None:
    """Return never-injected records to the live snapshot (merge, never clobber).

    The write happens BEFORE the claim marker is cleared, so a crash between
    the two leaves the records in the live snapshot — the next replay pass
    (platform reconnect or next boot) retries them; nothing is lost and,
    because only never-injected records are written back, nothing is
    delivered twice.
    """
    path = drain_queue_path()
    events = _load_snapshot_events(path)
    events.extend(records)
    try:
        atomic_json_write(
            path,
            {"version": _SNAPSHOT_VERSION, "events": events},
            indent=None,
        )
    except Exception:
        # The claim marker still holds the records; the caller clears it as
        # usual (at-most-once outranks retention). A write failure on the
        # same directory means the next boot could not have replayed them
        # either — fail loud, never silently pretend they were kept.
        logger.error(
            "Drain queue write-back failed; %d retained message(s) cannot be "
            "retried automatically",
            len(records),
            exc_info=True,
        )


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


def replay_drain_queue(runner: Any, platform: Optional[Platform] = None) -> int:
    """Replay the drain snapshot into the live queues; returns turns started.

    Called from gateway ``start()`` after adapters are ready and before the
    resume scheduler runs. Every event re-enters the owning adapter's FIFO
    through ``_queue_or_replace_pending_event`` (same merge/cap semantics as
    live queueing). Sessions in the cooperative-restart resume set only
    enqueue — their resumed turn picks pending events up at its post-turn
    drain. Every other affected session gets a turn started for its head
    event through the resume scheduler's dispatch path, so the queued text
    is answered without waiting for the user to speak again.

    Events whose adapter is not live (platform down at boot) are NOT
    dropped: their records are written back to the live snapshot and retried
    at the next natural point — the platform's reconnect (the reconnect
    watcher calls this again scoped to ``platform``) or the next boot.
    ``platform`` scopes a retry pass to one platform; other platforms'
    records stay queued untouched.
    """
    claimed = claim_drain_queue()
    if not claimed:
        return 0

    grouped: Dict[str, List[Tuple[MessageEvent, Dict[str, Any]]]] = {}
    for session_key, event, record in claimed:
        grouped.setdefault(session_key, []).append((event, record))

    injected: Dict[str, Any] = {}
    retained_records: List[Dict[str, Any]] = []
    for session_key, items in grouped.items():
        source = items[0][0].source
        if platform is not None and getattr(source, "platform", None) != platform:
            # Scoped pass (platform reconnect): other platforms keep waiting.
            retained_records.extend(record for _event, record in items)
            continue
        adapter = runner._adapter_for_source(source)
        if adapter is None:
            logger.warning(
                "Retaining %d drain-queued message(s) for %s: no live adapter "
                "yet — retried on reconnect or at next boot",
                len(items),
                session_key,
            )
            retained_records.extend(record for _event, record in items)
            continue
        for event, _record in items:
            runner._queue_or_replace_pending_event(session_key, event)
        injected[session_key] = adapter

    # Close the at-most-once window: retained records go back to the live
    # snapshot first, then the claim is cleared — a crash in between leaves
    # them retryable, never lost, and injected records never return.
    if retained_records:
        _write_back_retained_records(retained_records)
    clear_claimed_drain_queue()
    if not injected:
        return 0
    logger.info(
        "Replaying %d drain-queued message(s) across %d session(s)",
        len(claimed) - len(retained_records),
        len(injected),
    )

    allowlist = getattr(runner, "_resume_allowlist_for_this_boot", None)
    raw_allowlist = allowlist() if callable(allowlist) else None
    cooperative = set(raw_allowlist or set())

    started = 0
    for session_key, adapter in injected.items():
        if session_key in cooperative and _scheduler_will_resume(
            runner, session_key, raw_allowlist
        ):
            # The scheduler resumes this session THIS boot; that turn drains
            # the FIFO after it finishes — no second turn from replay.
            continue
        if runner._is_session_running(session_key):
            # Already busy (e.g. boot auto-resume claimed it): the FIFO is
            # consumed by that turn's post-turn drain — no second turn.
            continue
        head = _pop_replay_head(runner, adapter, session_key)
        if head is None:
            continue
        started += _dispatch_replay_turn(runner, adapter, head, session_key)
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
