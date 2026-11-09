"""Drain-window record format + legacy-snapshot disposal.

Scope after the Nov 2026 successor-delivery redesign (user contract, Parsa
2026-11-09): a restart NEVER durably snapshots other chats' in-flight or
drain-window work. The predecessor keeps running its current turns; the hub
transports (Discord/Matrix) hand NEW user messages to the successor via
``handoff_overlap_event``, and once a turn ends the next user message is
delivered to the new instance by the hub. The former
``drain_message_queue.json`` snapshot/replay handover — whose
``drain_message_queue.replaying.json`` claim was replayed into model turns
on every later boot, injecting ``[Gateway restart completed]`` and a new
turn into chats that never asked — must not exist.

What remains in this module:

- ``serialize_drain_event`` / ``deserialize_drain_event`` — the JSON record
  format the hub clients use to carry a ``MessageEvent`` across the
  predecessor/successor RPC boundary (registration, routing, handoff,
  processing-completion). That is live process-to-process delivery, not a
  file of acting sessions.
- The restart-drain lifecycle stamp + receipt proof/renderers, shared with
  ``shutdown_flush`` (the pending-message flush recovery, which bakes the
  receipt into stored user content exactly once).
- ``discard_drain_queue_snapshots_for_profiles`` — the boot-time disposal
  of any snapshot left by an older build: one warning, both files gone,
  never a replay.

Ownership-marker attribute kept for hub-record compatibility: events the
old durable snapshot used to own carried ``_drain_snapshot_owned`` and the
hub completion record still round-trips the field. Nothing sets it anymore;
consumers treat it as always-False.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DRAIN_QUEUE_FILENAME = "drain_message_queue.json"
DRAIN_QUEUE_CLAIMED_FILENAME = "drain_message_queue.replaying.json"

# See the module docstring: a compatibility field on hub completion records,
# always False now that no durable drain snapshot exists.
DRAIN_SNAPSHOT_OWNED_ATTR = "_drain_snapshot_owned"

# Lifecycle receipt for post-restart recovery. ``serialize_drain_event`` and
# the shutdown pending-flush stamp the RECORD (gateway-owned durable state
# beside auth.json — the one place a platform message cannot forge it: event
# metadata is user-influenceable, so the ownership fact must not ride the
# event) with the identity of the process that queued the event during its
# restart-requested drain. Recovery in a DIFFERENT process proves that
# restart completed — the queueing process is gone and its successor is
# delivering the message — which is the weakest cross-process provenance
# that states the fact. Legacy records (no stamp) and same-process recovery
# (the restart never finished, or was cancelled) prove nothing and get no
# receipt; delivery is unchanged either way.
DRAIN_RESTART_STAMP_FIELD = "restart_drain"
# A durable JSON stamp can carry any integer; no host allocates pids past the
# signed 32-bit range, so anything larger is malformed data, never a pid whose
# death could be proven (probing it must not throw either).
_MAX_PROBEABLE_PID = 2**31 - 1


def _restart_drain_stamp(restart_requested: bool) -> Optional[Dict[str, Any]]:
    """Record-level lifecycle stamp for a drain-time queueing (see above)."""
    if not restart_requested:
        return None
    return {"requested": True, "pid": os.getpid()}


def drain_restart_receipt_for_record(record: Any) -> Optional[Dict[str, Any]]:
    """Prove, from one queue record, that the restart it was queued for completed.

    Returns receipt facts (``{"queued_pid": ...}``) only when the record was
    stamped by a restart-requested drain of ANOTHER process that is no longer
    running — i.e. the queueing process bounced and this process is its
    successor delivering the message. Returns None for absent/malformed
    stamps (legacy records, corruption), same-process stamps (a live process
    that queued during its own drain: the restart has not completed, or was
    cancelled), a queueing pid that is somehow still alive, an out-of-range
    pid (the stamp is durable JSON: a corrupted or oversized integer is not a
    pid anyone can prove dead), or an unavailable liveness backend; in every
    None case recovery proceeds unchanged, just without the receipt.
    """
    if not isinstance(record, dict):
        return None
    stamp = record.get(DRAIN_RESTART_STAMP_FIELD)
    if not isinstance(stamp, dict) or stamp.get("requested") is not True:
        return None
    pid = stamp.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if pid > _MAX_PROBEABLE_PID:
        return None  # beyond any host pid space: malformed, not provably dead
    if pid == os.getpid():
        return None
    # Liveness via the shared non-signalling tri-state probe. Never
    # ``os.kill(pid, 0)``: it raises OverflowError for oversized JSON ints
    # and on Windows signals the target's whole console group. The helper
    # returns False ONLY for a kernel-confirmed NoSuchProcess (proven dead)
    # and None for an unavailable/erroring backend — unknown is never dead.
    try:
        from hermes_cli.process_identity import _pid_alive_matches
    except Exception:
        return None
    try:
        alive = _pid_alive_matches(pid, None)
    except Exception:
        return None
    if alive is False:
        return {"queued_pid": pid}  # queueing process is gone: restart completed
    return None


def render_drain_restart_receipt(receipt: Dict[str, Any]) -> str:
    """Render the bracketed lifecycle-receipt note for a proven receipt.

    Used by the shutdown pending-flush recovery path
    (``shutdown_flush._recover_one_payload``), which bakes it into the stored
    user content once. The drain-snapshot replay path that also used this
    renderer is gone (see the module docstring).
    """
    note = "[Gateway restart completed"
    queued_pid = receipt.get("queued_pid")
    if isinstance(queued_pid, int) and queued_pid > 0:
        note += (
            f" — this message arrived while the previous gateway "
            f"process (pid {queued_pid}) was draining for the "
            f"restart"
        )
    note += (
        ". The restart finished and this successor process is "
        "delivering the queued message now; no action is needed "
        "about the restart itself.]"
    )
    return note


def drain_queue_path(home: Optional[Path] = None) -> Path:
    """Legacy live-snapshot path in the gateway state dir (now only checked
    for disposal at boot). *home* pins the profile home."""
    base = Path(home) if home is not None else get_hermes_home()
    return base / "gateway" / DRAIN_QUEUE_FILENAME


def claimed_drain_queue_path(home: Optional[Path] = None) -> Path:
    return drain_queue_path(home).parent / DRAIN_QUEUE_CLAIMED_FILENAME


def discard_drain_queue_snapshots(home: Optional[Path] = None) -> bool:
    """Delete any legacy drain snapshot/claim under *home*; warn once.

    A restart no longer writes these files (see the module docstring); one
    left by an older build is discarded at boot — never replayed into a
    model turn. Returns True when at least one file was removed.
    """
    removed = False
    for path in (drain_queue_path(home), claimed_drain_queue_path(home)):
        try:
            existed = path.exists()
        except OSError:
            logger.debug("Drain snapshot existence check failed for %s", path,
                         exc_info=True)
            continue
        if not existed:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning(
                "Could not remove legacy drain snapshot %s; it is never "
                "replayed — delete it manually",
                path,
            )
            continue
        removed = True
    if removed:
        logger.warning(
            "Discarded legacy drain-queue snapshot(s) under %s: restarts no "
            "longer snapshot or replay other chats' in-flight work (the "
            "successor only continues the restart requester; the hub "
            "delivers every new message)",
            drain_queue_path(home).parent,
        )
    return removed


def discard_drain_queue_snapshots_for_profiles(runner: Any) -> None:
    """Boot entry: dispose of every queue this gateway could own.

    Under ``gateway.multiplex_profiles`` a secondary profile's snapshot lived
    under that profile's home, so dispose of each served profile's files as
    well as the launch home's — mirroring the enumeration chokepoint gateway
    startup uses for adapter discovery (``profiles_to_serve``).
    """
    discard_drain_queue_snapshots()
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return
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
            "Legacy drain-snapshot disposal could not enumerate the "
            "multiplex profiles; disposing the launch home only",
            exc_info=True,
        )
        return
    for _profile_name, profile_home in served:
        try:
            discard_drain_queue_snapshots(profile_home)
        except Exception:
            logger.warning(
                "Legacy drain-snapshot disposal failed for profile home %s",
                profile_home,
                exc_info=True,
            )


# ── hub record (de)serialization — the single pair, on the event model ──────

# Routing-relevant SessionSource fields. Session-key derivation
# (``build_session_key``) and reply anchoring must see the same values the
# live event carried, or a routed message would land on a different session
# than the one the user was talking to.
#
# Deliberately EXCLUDED (the source dataclass's own wire-invisible trust
# signals — its field docs say they must never be restorable from
# persistence, and a serialized record is a persistence medium):
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

# MessageEvent fields carried in the hub record. Media paths survive the
# process boundary (they live under HERMES_HOME), so the rebuilt event
# re-enters the same vision/STT preprocessing a fresh message would.
# ``text`` is deliberately NOT in this tuple: it is a required positional on
# ``MessageEvent`` and the generic skip-empty filter below would drop an
# empty caption, so serialize always emits it explicitly (empty string
# included).
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
    # Channel-scoped instructions ride along so the rebuilt event re-enters
    # the turn with the same per-channel system prompt and backfilled
    # context the original inbound message carried. They are applied at API
    # call time and never persisted to the transcript.
    "channel_prompt",
    "channel_context",
    "internal",
    "allow_gateway_control",
    "redelivered",
)


def _json_safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the JSON-encodable metadata entries (ids, flags, short strings).

    Metadata is free-form; a connector could stash an exotic object there.
    Dropping the exotic entry keeps the WHOLE event carryable — the honest
    alternative (fail the send) would refuse the message over a decoration.
    Observed keys across the platform connectors are plain routing ids/flags,
    so this drops nothing in practice.
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


def serialize_drain_event(
    session_key: str, event: MessageEvent, *, restart_requested: bool = False
) -> Dict[str, Any]:
    """Flatten one event + its source into a JSON-safe hub record.

    The FULL metadata dict is persisted, not just a subset: routing metadata
    such as ``whatsapp_from_owner`` decides how the routed message is
    treated, and the record stays inside the gateway's own RPC channel. A
    survey of the connectors found no display secrets in metadata — only ids
    and flags — so no denylist is needed; ``_SOURCE_FIELDS`` documents the
    two source-level trust flags that are never serialized.
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
    record = {
        "session_key": str(session_key or ""),
        "queued_at": time.time(),
        "source": source_data,
        "event": event_data,
        "event_timestamp": timestamp.isoformat() if timestamp is not None else None,
    }
    # The restart-drain stamp closes one gap: nothing else in the record (or
    # anywhere durable) binds a queued message to the restart-draining
    # PROCESS, so recovery could not tell "queued by the pre-restart process"
    # from "queued by this live process during a cancelled restart".
    stamp = _restart_drain_stamp(restart_requested)
    if stamp is not None:
        record[DRAIN_RESTART_STAMP_FIELD] = stamp
    return record


def deserialize_drain_event(record: Any) -> Tuple[str, MessageEvent]:
    """Rebuild ``(session_key, MessageEvent)`` from a hub record.

    Raises ``ValueError`` on any structurally invalid record so the caller
    can quarantine a corrupt payload without crashing.
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
