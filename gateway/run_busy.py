"""Busy-session queueing, slot claims, slash dispatch tables and confirmation prompts
for GatewayRunner (mixin bound via the MRO).

``gateway.run`` internals are imported lazily inside method bodies (import cycle), so
``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import asyncio
import contextlib
import json
import os
import time
from agent.i18n import t
from agent.session_activity import format_iteration_progress
from gateway.config import Platform
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.whatsapp_identity import canonical_whatsapp_identifier
from typing import Any, Dict, List, Optional, Tuple, Union

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def _strip_slot(text: str, slot: str) -> Optional[str]:
    """Remainder after ``slot`` when ``text`` starts with it as a WHOLE slot, else None.

    The ``:``-delimited slot layout is ``build_session_key``'s: an id that merely starts with another
    must never match, so a whole-slot comparison is what every caller here uses. A text that IS the
    slot yields ``""``."""
    if text == slot:
        return ""
    if text.startswith(slot + ":"):
        return text[len(slot) + 1:]
    return None


def _tail_has_slot(tail: str, slot: str) -> bool:
    """True when ``tail``'s FIRST slot is ``slot`` (``tail`` is ``""`` when the key ends at the
    chat id)."""
    return _strip_slot(tail, slot) is not None


def _same_chat_key_slots(
    key: str, *, prefix: str, chat_id: str, scope_id: Optional[str],
) -> Optional[Tuple[str, str]]:
    """``(chat_type, tail)`` when ``key`` names the SAME chat as ``prefix`` + ``chat_id``, else None.

    ``prefix`` is the key's fixed-shape head, ``agent:<profile>:<platform>:``. Everything after it is
    matched as TEXT, because ids may themselves contain ``:`` (Matrix ``!room:example.org``).
    ``scope_id`` is Slack's workspace slot — ``build_session_key`` emits it there alone — and a key
    without it still names the same chat; a key carrying a DIFFERENT known scope is another
    workspace's chat. ``tail`` is ``""`` when the key ends at the chat id.
    """
    if not key.startswith(prefix):
        return None
    chat_type, _, rem = key[len(prefix):].partition(":")
    candidates = (f"{scope_id}:{chat_id}", chat_id) if scope_id else (chat_id,)
    for candidate in candidates:
        tail = _strip_slot(rem, candidate)
        if tail is not None:
            return chat_type, tail
    return None


class GatewayBusySessionMixin:
    """Busy-session queueing, slot claims, slash dispatch tables, and confirmation prompts."""

    def _queue_during_drain_enabled(self, busy_input_mode: Optional[str] = None) -> bool:
        # "queue"/"steer" mean messages survive a restart (queued for the new process); "interrupt" drops.
        mode = busy_input_mode or self._busy_input_mode
        return self._restart_requested and mode in {"queue", "steer"}

    def _overflow_queue(self, session_key: str):
        """The session's FIFO overflow list, or None when no session state exists yet."""
        state = self._peek_session_state(session_key)
        return state.conversation.queued_events if state else None

    @staticmethod
    def _fifo_message_id(event: Any) -> str:
        """Dedupe key for queue re-delivery: the inbound platform message id.

        Synthetic events (goal continuations, heartbeats) carry no id and are never deduped.
        Internal synthetic events are also exempt: watch-notification/completion events
        inherit the spawning turn's reply-anchor id, so distinct internal events can
        legitimately share one message id.
        """
        if getattr(event, "internal", False):
            return ""
        message_id = getattr(event, "message_id", None)
        return str(message_id).strip() if message_id else ""

    def _find_queued_copy_by_message_id(
        self, session_key: str, message_id: str, adapter: Any
    ) -> Optional["MessageEvent"]:
        """Return the already-queued event with this platform message id, if any."""
        if not message_id:
            return None
        pending_slot = getattr(adapter, "_pending_messages", None)
        if isinstance(pending_slot, dict):
            slot_event = pending_slot.get(session_key)
            if slot_event is not None and self._fifo_message_id(slot_event) == message_id:
                return slot_event
        overflow = self._overflow_queue(session_key)
        if overflow:
            for queued_copy in overflow:
                if self._fifo_message_id(queued_copy) == message_id:
                    return queued_copy
        return None

    def _drop_redelivered_fifo_copy(
        self, session_key: str, message_id: str, queued_copy: "MessageEvent"
    ) -> None:
        """Collapse a re-delivered platform message id into its ONE queued turn.

        The incoming copy is dropped (it would run as its own full agent turn — the observed
        amplification loop) and the surviving queued copy is marked ``redelivered`` so the model
        can tell it from a fresh message. One log line per drop.
        """
        try:
            queued_copy.redelivered = True
        except Exception:
            pass
        logger.info(
            "Dropped re-delivered copy of message id %s for session %s — the "
            "message is already queued; its queued copy is marked as a "
            "re-delivery (one platform message, one turn)",
            message_id, session_key,
        )

    def _purge_redelivered_overflow_copies(
        self, session_key: str, kept: "MessageEvent", overflow: Any
    ) -> int:
        """Drop overflow copies sharing ``kept``'s message id; mark ``kept``. Returns the drop count.

        Called when an overflow event is taken out to run (promotion or rescue): every same-id
        sibling left behind would later run as its own full turn for the SAME platform message.
        """
        message_id = self._fifo_message_id(kept)
        if not message_id or not overflow:
            return 0
        dropped = 0
        index = 0
        while index < len(overflow):
            if self._fifo_message_id(overflow[index]) == message_id:
                overflow.pop(index)
                dropped += 1
                continue
            index += 1
        if dropped:
            try:
                kept.redelivered = True
            except Exception:
                pass
            logger.info(
                "Dropped %d re-delivered cop(y/es) of message id %s for session "
                "%s at FIFO promotion — one platform message keeps one turn",
                dropped, message_id, session_key,
            )
        return dropped

    def _enqueue_fifo(
        self, session_key: str, queued_event: "MessageEvent", adapter: Any
    ) -> Optional["MessageEvent"]:
        """Append a /queue event and return the event that will consume it."""
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if pending_slot is None:
            return None
        # One platform message id, one queued turn: a re-delivered copy parked here would run as
        # its own turn at promotion time.
        message_id = self._fifo_message_id(queued_event)
        if message_id:
            queued_copy = self._find_queued_copy_by_message_id(session_key, message_id, adapter)
            if queued_copy is not None:
                self._drop_redelivered_fifo_copy(session_key, message_id, queued_copy)
                from gateway.platforms.event import transfer_replay_completion

                return transfer_replay_completion(queued_copy, queued_event)
        if session_key in pending_slot or getattr(queued_event, "internal", False):
            # Internal events (resume wakes, background completions) never take the
            # human head slot: the slot's text-merge semantics would collapse a wake
            # into the next user message (mirrors the adapter-side rule that a wake
            # must not merge into an occupied human slot).
            self._session_state(session_key).conversation.queued_events.append(queued_event)
        else:
            pending_slot[session_key] = queued_event
        queued_event._gateway_processing_deferred = True
        queued_event._gateway_accepted = True
        return queued_event

    def _promote_queued_event(
        self, session_key: str, adapter: Any, pending_event: Optional["MessageEvent"]
    ) -> Optional["MessageEvent"]:
        """Promote the next overflow item after the slot drained.

        ``pending_event`` None → the overflow head becomes the pending event; otherwise the head is
        staged into the slot for the NEXT recursion. Returns the (possibly updated) pending_event.
        """
        overflow = self._overflow_queue(session_key)
        if not overflow:
            return pending_event
        next_queued = overflow.pop(0)
        # Collapse same-id siblings now: each would promote into its own full turn for the SAME
        # platform message (the observed overflow amplification loop).
        self._purge_redelivered_overflow_copies(session_key, next_queued, overflow)
        if pending_event is None:
            return next_queued
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = next_queued
        # else: no adapter — leave the head in place so we don't silently drop it.
        else:
            overflow.insert(0, next_queued)
        return pending_event

    def _queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        """Total pending /queue items for a session — slot + overflow."""
        depth = len(self._overflow_queue(session_key) or ())
        if adapter is not None and session_key in getattr(adapter, "_pending_messages", {}):
            depth += 1
        return depth

    def _rescue_orphaned_overflow(self, session_key: str, adapter: Any) -> Optional["MessageEvent"]:
        """Pop the oldest orphaned FIFO overflow event for an idle session (None if nothing to rescue).

        ``queued_events`` drains only at the post-turn promotion site; a busy window ending without
        it (early exit, exception/interrupt/generation-bump) orphans the overflow. On a NEW event for
        a NON-busy session the oldest orphan runs as THIS turn, the next is staged into the slot so
        arrival order holds, and the caller enqueues the incoming event behind it. The returned
        event is REMOVED from both stores, else the post-turn dequeue would run it twice.

        See #28503.
        """
        try:
            overflow = self._overflow_queue(session_key)
            if not overflow:
                return None
            pending_slot = getattr(adapter, "_pending_messages", None)
            if not isinstance(pending_slot, dict) or pending_slot.get(session_key):
                return None  # slot occupied (busy) or no slot storage — promotion owns this
            head = overflow.pop(0)
            # Collapse same-id siblings before staging: a re-delivered copy staged (or left)
            # behind would run as its own turn for the SAME platform message.
            self._purge_redelivered_overflow_copies(session_key, head, overflow)
            # Keep the slot occupied so the drain promotes in order and a mid-chain arrival routes
            # to overflow instead of jumping the queue (same invariant as _promote_queued_event).
            if overflow:
                pending_slot[session_key] = overflow.pop(0)
            logger.warning(
                "Rescued orphaned FIFO overflow event for idle session "
                "%s — it was queued during a busy window but the post-turn "
                "drain never promoted it (#99882)", session_key,
            )
            if overflow:
                logger.warning(
                    "%d overflow event(s) still queued for session %s after "
                    "rescue staging (will drain via normal promotion)", len(overflow), session_key,
                )
            return head
        except Exception:
            logger.debug("FIFO overflow rescue failed for %s", session_key, exc_info=True)
            return None

    def _rescue_park_incoming_event(
        self,
        session_key: str,
        incoming: "MessageEvent",
        adapter: Any,
        rescued: Optional["MessageEvent"],
    ) -> None:
        """Park the idle-path arrival behind a rescue chain, deduped by id.

        The rescued orphan runs as THIS turn. A platform re-delivery of that same message id must
        not be parked (it would queue a SECOND turn for one message) — drop it and mark the
        running copy. Anything else parks in FIFO order (``_enqueue_fifo`` drops already-queued ids).
        """
        message_id = self._fifo_message_id(incoming)
        rescued_id = self._fifo_message_id(rescued) if rescued is not None else ""
        if message_id and rescued_id and message_id == rescued_id:
            try:
                rescued.redelivered = True
            except Exception:
                pass
            logger.info(
                "Dropped re-delivered copy of message id %s for session %s — "
                "it is already running as this turn (idle FIFO rescue)",
                message_id, session_key,
            )
            return
        self._enqueue_fifo(session_key, incoming, adapter)

    @staticmethod
    def _is_goal_continuation_event(event_or_text: Any) -> bool:
        """True for synthetic /goal continuation turns (so pause/clear can spare real /queue items)."""
        text = getattr(event_or_text, "text", event_or_text) or ""
        return str(text).startswith("[Continuing toward your standing goal]\nGoal:")

    def _clear_goal_pending_continuations(self, session_key: str, adapter: Any) -> int:
        """Remove queued synthetic /goal continuations for one session; real /queue items are kept."""
        removed = 0
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if self._is_goal_continuation_event(pending_event):
                pending_slot.pop(session_key, None)
                removed += 1

        overflow = self._overflow_queue(session_key)
        if overflow:
            kept = [e for e in overflow if not self._is_goal_continuation_event(e)]
            removed += len(overflow) - len(kept)
            self._peek_session_state(session_key).conversation.queued_events = kept
        return removed

    def _goal_still_active_for_session(self, session_id: str) -> bool:
        """Best-effort fresh DB check before running a queued continuation."""
        if not session_id:
            return False
        try:
            from hermes_cli.goals import GoalManager
            return GoalManager(session_id=session_id).is_active()
        except Exception as exc:
            logger.debug("goal continuation: active-state recheck failed: %s", exc)
            return False

    def _get_max_concurrent_sessions(self) -> Optional[int]:
        """Return the configured active chat session cap, if enabled."""
        try:
            from hermes_cli.active_sessions import resolve_max_concurrent_sessions
            return resolve_max_concurrent_sessions(getattr(self, "config", None))
        except Exception:
            return None

    def _active_session_limit_message(self, session_key: str) -> Optional[str]:
        """Return a user-facing rejection when starting a new session exceeds the cap."""
        max_sessions = self._get_max_concurrent_sessions()
        if max_sessions is None or self._is_session_running(session_key):
            return None
        active_count = self._running_agent_count()
        if active_count < max_sessions:
            return None
        from hermes_cli.active_sessions import active_session_limit_message
        return active_session_limit_message(active_count, max_sessions)

    def _claim_active_session_slot(
        self, session_key: str, source: SessionSource
    ) -> tuple[Any, Optional[str]]:
        """Claim a cross-process active-session slot for a new gateway turn."""
        if self._is_session_running(session_key):
            return None, None
        limit_message = self._active_session_limit_message(session_key)
        if limit_message is not None:
            return None, limit_message
        try:
            from hermes_cli.active_sessions import try_acquire_active_session
            platform = source.platform.value if source and source.platform else "gateway"
            return try_acquire_active_session(
                session_id=session_key,
                surface=f"gateway:{platform}",
                config=getattr(self, "config", None),
                metadata={
                    "platform": platform,
                    "chat_id": getattr(source, "chat_id", "") or "",
                    "user_id": getattr(source, "user_id", "") or "",
                    # Writer identity: a leaked lease from this process is re-acquired by the next
                    # turn rather than fencing it out forever (pruning only reclaims dead PROCESSES).
                    # Writer identity for re-entrancy (#94595): if this process leaks a lease for this
                    # session (exception path skipped release), the next turn re-acquires its own entry
                    # instead of being fenced out of it forever — pruning only reclaims entries whose
                    # PROCESS died.
                    "live_session_id": str(session_key),
                },
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return None, None

    @staticmethod
    def _agent_has_active_subagents(running_agent: Any) -> bool:
        """True when *running_agent* is driving subagents (callers demote interrupt → queue;
        ``interrupt()`` would cascade through ``_active_children``). Fail-safe False on any error."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        if running_agent is None or running_agent is _AGENT_PENDING_SENTINEL:
            return False
        children = getattr(running_agent, "_active_children", None)
        # Real collections only — a ``MagicMock()._active_children`` auto-attr must not demote.
        if not isinstance(children, (list, tuple, set)) or not children:
            return False
        lock = getattr(running_agent, "_active_children_lock", None)
        try:
            with lock if lock is not None else contextlib.nullcontext():
                return bool(children)
        except Exception:
            return False

    @staticmethod
    def _steer_active_subagents(running_agent: Any, text: str) -> int:
        """Queue *text* into every live child of *running_agent*; returns how many accepted it.

        A parent blocked inside ``delegate_task`` only drains its own steer queue after the tool
        returns, i.e. after the child finishes — so a steer aimed at a looping child would sit
        unread for the whole delegation (#112095, Telegram). Children are the parent's own
        ``_active_children`` (identity-scoped, same snapshot ``interrupt()`` fans out to)."""
        children = getattr(running_agent, "_active_children", None)
        if not isinstance(children, (list, tuple, set)) or not children:
            return 0
        lock = getattr(running_agent, "_active_children_lock", None)
        try:
            with lock if lock is not None else contextlib.nullcontext():
                snapshot = list(children)
        except Exception:
            return 0
        accepted = 0
        for child in snapshot:
            steer = getattr(child, "steer", None)
            if not callable(steer):
                continue
            try:
                accepted += bool(steer(text))
            except Exception as exc:
                logger.warning("Steer into subagent %r failed: %s", getattr(child, "_delegate_id", child), exc)
        return accepted

    def _steer_running_agent(self, running_agent: Any, text: str) -> bool:
        """``running_agent.steer(text)`` plus fan-out to its active subagents (see
        :meth:`_steer_active_subagents`); True when the parent or any child queued it."""
        accepted = bool(running_agent.steer(text))
        return bool(self._steer_active_subagents(running_agent, text)) or accepted

    async def _session_has_compression_in_flight(self, session_key: str) -> bool:
        """True when a compression lock is held for this session's id (callers demote interrupt →
        queue, else a follow-up against the pre-rotation parent orphans compression siblings).
        Both blocking reads run in a worker thread so a large state.db never freezes the loop.

        Context compression is interrupt-protected (#23975) but gateway ``interrupt`` busy-input mode can
        still start a follow-up turn against the pre-rotation parent while compression is mid-flight,
        producing orphaned compression siblings (#56391).
        """
        session_store = getattr(self, "session_store", None)
        if not session_key or session_store is None:
            return False
        def _assume_active(what: str, ident) -> bool:
            logger.warning(
                "Compression in-flight check failed while reading %s %s; treating compression as "
                "active to avoid interrupting a possible parent-session rotation", what, ident, exc_info=True,
            )
            return True

        try:
            session_id = await asyncio.to_thread(
                self._lookup_session_id_under_store_lock, session_store, session_key
            )
        except (AttributeError, TypeError):
            return False
        except Exception:
            return _assume_active("session", session_key)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(raw_db.get_compression_lock_holder, str(session_id))
            # Production returns Optional[str]. Reject non-strings so a MagicMock auto-attr (or any
            # unexpected truthy) cannot look like a held lock and skip hygiene.
            # See #96953.
            return isinstance(holder, str) and bool(holder)
        except (AttributeError, TypeError):
            return False
        except Exception:
            return _assume_active("lock holder for session", session_id)

    @staticmethod
    def _lookup_session_id_under_store_lock(session_store, session_key: str):
        """Sync helper run in the thread pool: read session_id under the store lock."""
        # noqa: SLF001 — intentional private access; runs off the event loop.
        with session_store._lock:  # noqa: SLF001
            session_store._ensure_loaded_locked()  # noqa: SLF001
            entry = session_store._entries.get(session_key)  # noqa: SLF001
        return getattr(entry, "session_id", None) if entry is not None else None

    # Metadata that must match for two pending events to merge into one slot.
    _SECURITY_METADATA_KEYS = (
        "hermes_plugin_id", "hermes_plugin_injection", "gateway_session_key",
        "gateway_session_id", "gateway_session_strict",
        "notification_category",
    )

    def _queue_or_replace_pending_event(
        self, session_key: str, event: MessageEvent
    ) -> Optional[MessageEvent]:
        """Queue one event and return its consumer, or None when not inserted."""
        from gateway.platforms.base import merge_pending_message_event

        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return None
        # #28503 — Previously this called ``merge_pending_message_event``
        # with the default ``merge_text=False``, which silently OVERWROTE
        # the single pending slot when consecutive text messages arrived
        # in ``busy_input_mode: queue``. Route through the FIFO
        # infrastructure shared with ``/queue`` so each follow-up gets
        # its own turn in arrival order. Photo bursts still merge into
        # the head slot via ``merge_pending_message_event`` (album
        # semantics); everything else appends to the overflow tail.
        #
        # One platform message id, one queued turn — checked BEFORE the
        # media merge so a re-delivered photo/caption cannot double-merge
        # into the head slot either; the queued copy keeps the turn and is
        # marked as a re-delivery.
        _dedupe_id = self._fifo_message_id(event)
        if _dedupe_id:
            _queued_copy = self._find_queued_copy_by_message_id(
                session_key, _dedupe_id, adapter
            )
            if _queued_copy is not None:
                self._drop_redelivered_fifo_copy(session_key, _dedupe_id, _queued_copy)
                from gateway.platforms.event import transfer_replay_completion

                return transfer_replay_completion(_queued_copy, event)
        pending_slot = getattr(adapter, "_pending_messages", None)
        existing = pending_slot.get(session_key) if isinstance(pending_slot, dict) else None
        security_metadata_keys = (
            "hermes_plugin_id",
            "hermes_plugin_injection",
            "gateway_session_key",
            "gateway_session_id",
            "gateway_session_strict",
        )
        same_security_context = existing is not None and (
            getattr(existing, "internal", False) == getattr(event, "internal", False)
            and getattr(existing, "allow_gateway_control", True)
            == getattr(event, "allow_gateway_control", True)
            and all(
                (getattr(existing, "metadata", None) or {}).get(key)
                == (getattr(event, "metadata", None) or {}).get(key)
                for key in security_metadata_keys
            )
        )
        if same_security_context and (
            getattr(existing, "message_type", None) == MessageType.PHOTO
            or event.message_type == MessageType.PHOTO
            or bool(getattr(existing, "media_urls", None))
            or bool(getattr(event, "media_urls", None))
        ):
            # Preserve photo-burst / media-merge semantics for the head slot.
            return merge_pending_message_event(
                adapter._pending_messages,
                session_key,
                event,
                merge_text=event.message_type == MessageType.TEXT,
            )

        if self._queue_depth(session_key, adapter=adapter) >= self._BUSY_QUEUE_MAX_PENDING:
            logger.warning(
                "Dropping busy-mode follow-up for session %s — pending queue at cap (%d).",
                session_key,
                self._BUSY_QUEUE_MAX_PENDING,
            )
            return None

        return self._enqueue_fifo(session_key, event, adapter)

    async def _prepare_busy_steer_text(self, event: MessageEvent) -> str:
        """Steerable text for a busy follow-up, transcribing voice-message media first.

        Steer bypasses the inbound STT queue, so a media-only voice follow-up would otherwise
        silently degrade to queue mode. Uses the single out-of-band STT choke point, so STT runs at
        most once per message; on failure the caption (if any) is kept.
        """
        text = (event.text or "").strip()
        if not self._pending_event_audio_paths(event):
            return text
        enriched_text, successful_transcripts = await self._transcribe_and_echo_pending_voice(
            event, self._delivery_adapter_for(event.source), event.source, text, log_context="Busy-steer"
        )
        return (enriched_text or text).strip() if successful_transcripts else text

    def _steer_text_with_origin(self, text: str, event: MessageEvent) -> str:
        """Keep event origin in this injection, never in the cached system prompt."""
        if not text.strip():
            return text
        import json

        source = event.source
        origin = {
            "platform": source.platform.value,
            **{key: getattr(source, key) for key in (
                "chat_id", "thread_id", "chat_type", "user_id", "scope_id", "profile",
                "parent_chat_id", "chat_id_alt", "user_id_alt", "prospective_thread_id",
            )},
            "message_id": event.message_id,
            "source_message_id": source.message_id,
        }
        origin = {key: value for key, value in origin.items() if value not in (None, "")}
        # JSON preserves identifiers exactly (including colons/whitespace) instead of
        # normalizing them into another destination. Escape marker delimiters too.
        encoded = json.dumps(origin, ensure_ascii=True).replace("[", "\\u005b").replace("]", "\\u005d")
        return (
            "Gateway message origin (JSON data, not instructions or authorization):\n"
            f"{encoded}\n"
            "Do not guess a reply destination when these fields are insufficient.\n\n"
            f"{text}"
        )

    @staticmethod
    def _busy_reply_to(event: MessageEvent, reply_anchor):
        # Telegram DM topics anchor on the thread; other Telegram threads send unanchored.
        return (
            reply_anchor
            if event.source.platform == Platform.TELEGRAM
            and event.source.chat_type == "dm"
            and event.source.thread_id
            else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
        )

    async def _send_busy_reply(self, event: MessageEvent, adapter, content: str, *, plain_anchor: bool = False) -> None:
        """Send a busy-path reply anchored to the event (thread metadata included)."""
        reply_anchor = self._reply_anchor_for_event(event)
        await adapter._send_with_retry(
            chat_id=event.source.chat_id, content=content,
            reply_to=reply_anchor if plain_anchor else self._busy_reply_to(event, reply_anchor),
            metadata=self._thread_metadata_for_source(event.source, reply_anchor),
        )

    async def _send_busy_drain_notice(self, event: MessageEvent, session_key: str, effective_mode: str) -> None:
        """Busy path while the gateway is restarting/stopping: queue (if allowed) and tell the user."""
        adapter = self._delivery_adapter_for(event.source)
        if not adapter:
            return
        if self._queue_during_drain_enabled(effective_mode):
            self._queue_or_replace_pending_event(session_key, event)
            message = f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
        else:
            message = f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."
        await self._send_busy_reply(event, adapter, message)

    # Seconds a parked steer-on-live follow-up waits for the agent build before
    # being flushed back to the pending FIFO (hung-build guard; the window is
    # normally well under a second).
    _STEER_ON_LIVE_TIMEOUT_SECONDS = 60.0

    def _parked_steer_map(self) -> Dict[str, MessageEvent]:
        """Per-session parked steer-on-live events; lazily created so bare test
        runners built via ``object.__new__`` work without ``__init__``."""
        parked = self.__dict__.get("_parked_steer_events")
        if parked is None:
            parked = {}
            self.__dict__["_parked_steer_events"] = parked
        return parked

    def _flush_parked_steer(self, session_key: str) -> None:
        """Recover this session's parked steer-on-live event into the pending
        FIFO (turn teardown, /stop, drain). Idempotent; never drops silently."""
        event = self._parked_steer_map().pop(session_key, None)
        if event is None:
            return
        if self._queue_or_replace_pending_event(session_key, event) is None:
            logger.warning(
                "Parked steer follow-up for session %s could not be re-queued (no adapter) — dropped",
                session_key,
            )
        else:
            logger.info(
                "Recovered parked steer follow-up for session %s into the pending queue",
                session_key,
            )

    def _arm_parked_steer_timeout(self, session_key: str, event: MessageEvent) -> None:
        """Flush the parked event back to the FIFO if the agent never goes live
        within the timeout (hung build). Identity-checked so a superseding park
        is never flushed by an older timer."""
        async def _flush_if_still_parked() -> None:
            await asyncio.sleep(self._STEER_ON_LIVE_TIMEOUT_SECONDS)
            if self._parked_steer_map().get(session_key) is event:
                logger.warning(
                    "Parked steer follow-up for session %s timed out waiting for the agent "
                    "to start; re-queued for the next turn",
                    session_key,
                )
                self._flush_parked_steer(session_key)

        try:
            asyncio.get_running_loop().create_task(_flush_if_still_parked())
        except RuntimeError:  # no loop — nothing parked can ever be consumed anyway
            pass

    async def _maybe_park_steer_on_agent_live(
        self, event: MessageEvent, session_key: str, effective_mode: str, running_agent: Any
    ) -> bool:
        """Park a steer-eligible follow-up in the steer-on-live slot when the
        turn's agent is still being built (``_AGENT_PENDING_SENTINEL``).

        The parked event is steered into the run the moment
        ``_run_agent_track_agent`` promotes the agent, instead of waiting out
        the whole turn as a queued second user turn. Only steer mode parks —
        queue/interrupt modes keep their current behavior. Returns True when
        the event was parked (the caller must NOT also queue it)."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        if effective_mode != "steer" or self._draining:
            return False
        # The startup auto-resume event consumes its own pre-claim; parking it
        # "until the agent goes live" orphans the resume — there is no agent
        # build to wait for behind the sentinel it placed itself.
        if self._resume_claim_event_consumes_sentinel(event, session_key):
            return False
        # Commands never park — they keep their dedicated dispatch/discard lanes
        # (the drain's slash-command safety net must stay reachable for them).
        if event.get_command():
            return False
        agent_live = running_agent is not None and running_agent is not _AGENT_PENDING_SENTINEL
        if agent_live:
            return False
        steer_text = await self._prepare_busy_steer_text(event)
        _steer_media_urls = getattr(event, "media_urls", None) or []
        _steer_all_voice = bool(_steer_media_urls) and (
            len(self._pending_event_audio_paths(event)) == len(_steer_media_urls)
        )
        plain_text = (
            event.message_type == MessageType.TEXT and not event.media_urls and not event.media_types
        )
        if not (steer_text and (plain_text or _steer_all_voice)):
            return False
        parked = self._parked_steer_map()
        # At most one parked event per session: an earlier one still parked means
        # the agent never promoted — recover it to the FIFO rather than dropping.
        if parked.get(session_key) is not None:
            self._flush_parked_steer(session_key)
        parked[session_key] = event
        self._arm_parked_steer_timeout(session_key, event)
        logger.info(
            "Parked steer follow-up for session %s until the agent goes live", session_key
        )
        return True

    async def _consume_parked_steer_on_agent_live(self, session_key: str, agent: Any) -> None:
        """Steer the session's parked follow-up into the just-promoted agent.

        Called from ``_run_agent_track_agent`` right after the agent lands in
        ``turn.agent``. On any steer failure the event is recovered into the
        adapter's pending FIFO so the existing post-turn drain handles it —
        never dropped."""
        event = self._parked_steer_map().pop(session_key, None)
        if event is None:
            return
        steer = getattr(agent, "steer", None)
        if agent is None or not callable(steer):
            self._queue_or_replace_pending_event(session_key, event)
            return
        steered = False
        try:
            text = await self._prepare_busy_steer_text(event)
            steered = bool(text) and self._try_agent_verb(
                agent, "steer", text, session_key, event=event
            )
        except Exception:
            logger.warning(
                "Steering parked follow-up into the live agent failed for session %s",
                session_key, exc_info=True,
            )
            steered = False
        if steered:
            logger.info("Steered parked follow-up into live agent for session %s", session_key)
            if self._steer_delivered_ack_enabled(event):
                self._register_steer_delivered_ack(event, session_key, agent)
        else:
            self._queue_or_replace_pending_event(session_key, event)
            logger.info(
                "Recovered parked steer follow-up for session %s into the pending queue (steer failed)",
                session_key,
            )

    async def _resolve_busy_steer_or_redirect(
        self, event: MessageEvent, session_key: str, effective_mode: str, running_agent: Any
    ) -> "GatewayRunner._BusySteerOutcome":
        """Apply interrupt->queue demotions, then attempt steer (steer mode) or redirect (interrupt mode)."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        # Steer injects mid-run via running_agent.steer(), falling back to queue (nothing lost) when
        # the agent isn't running yet, lacks steer(), or the payload is empty. Interrupt is demoted
        # to queue while subagents run (interrupt() would abort them); /stop and /new still cancel all.
        demoted_for_subagents = (
            effective_mode == "interrupt" and self._agent_has_active_subagents(running_agent)
        )
        if demoted_for_subagents:
            effective_mode = self._demote_interrupt(session_key, "the running agent has active subagents (#30170)")
        demoted_for_compression = (
            effective_mode == "interrupt" and await self._session_has_compression_in_flight(session_key)
        )
        if demoted_for_compression:
            effective_mode = self._demote_interrupt(session_key, "context compression is in flight (#56391)")
        steered = redirected = False
        steer_fallback_reason = None
        parked_for_steer = False
        agent_live = running_agent is not None and running_agent is not _AGENT_PENDING_SENTINEL
        plain_text = (
            event.message_type == MessageType.TEXT and not event.media_urls and not event.media_types
        )
        if effective_mode == "steer":
            steer_text = await self._prepare_busy_steer_text(event)
            # Steerable: plain text, OR every attachment is voice media folded into steer_text.
            # A follow-up qualifies for steering when it is plain text, OR when every attachment is
            # STT-eligible voice media whose transcript was just folded into steer_text — otherwise a voice
            # note in steer mode silently degrades to queue mode (#58780).
            _steer_media_urls = getattr(event, "media_urls", None) or []
            _steer_all_voice = bool(_steer_media_urls) and (
                len(self._pending_event_audio_paths(event)) == len(_steer_media_urls)
            )
            if steer_text and (plain_text or _steer_all_voice) and agent_live and hasattr(running_agent, "steer"):
                steered = self._try_agent_verb(
                    running_agent, "steer", steer_text, session_key, event=event
                )
            if not steered:
                # Agent-not-live + steer-eligible: park in the steer-on-live slot
                # instead of queueing — it is steered in the moment the agent
                # build finishes (_run_agent_track_agent promotion).
                if (
                    not agent_live
                    and await self._maybe_park_steer_on_agent_live(
                        event, session_key, "steer", running_agent
                    )
                ):
                    parked_for_steer = True
                else:
                    effective_mode = "queue"
                    # Record WHY steer fell back to queue so the ack can say so
                    # instead of the generic queue text (silent-feel bug fix).
                    if not agent_live:
                        steer_fallback_reason = "agent-not-live"
                    elif not (steer_text and (plain_text or _steer_all_voice)):
                        steer_fallback_reason = "media-not-steerable"
                    else:
                        steer_fallback_reason = "steer-failed"
        elif (
            effective_mode == "interrupt" and plain_text and agent_live
            and getattr(running_agent, "_supports_active_turn_redirect", False) is True
            and hasattr(running_agent, "redirect")
        ):
            redirected = self._try_agent_verb(
                running_agent, "redirect", (event.text or "").strip(), session_key, event=event
            )
        return self._BusySteerOutcome(
            effective_mode=effective_mode, demoted_for_subagents=demoted_for_subagents,
            demoted_for_compression=demoted_for_compression, steered=steered, redirected=redirected,
            steer_fallback_reason=steer_fallback_reason, parked_for_steer=parked_for_steer,
        )

    @staticmethod
    def _demote_interrupt(session_key: str, why: str) -> str:
        logger.info("Demoting busy_input_mode 'interrupt' to 'queue' for session %s because %s", session_key, why)
        return "queue"

    def _try_agent_verb(
        self, running_agent, verb: str, text: str, session_key: str, *, event: Optional[MessageEvent] = None
    ) -> bool:
        """Call ``running_agent.<verb>(text)`` (steer/redirect); False + warning on failure."""
        try:
            call_text = self._steer_text_with_origin(text, event) if event else text
            if verb == "steer":
                return self._steer_running_agent(running_agent, call_text)
            return bool(getattr(running_agent, verb)(call_text))
        except Exception as exc:
            logger.warning("Gateway %s failed for session %s: %s", verb, session_key, exc)
            return False

    async def _interrupt_running_agent_for_busy_event(self, event: MessageEvent, adapter, running_agent) -> None:
        """Interrupt mode: abort in-flight tool calls; the agent loop exits at its next check point."""
        from gateway.run import _build_media_placeholder
        try:
            _interrupt_text = event.text
            _media_urls = getattr(event, "media_urls", None) or []
            if self._pending_event_audio_paths(event):
                _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                    event, adapter, event.source, event.text or "", log_context="Voice-busy-interrupt",
                )
            elif not _interrupt_text and _media_urls:
                _interrupt_text = _build_media_placeholder(event)
            running_agent.interrupt(_interrupt_text)
        except Exception:
            pass  # don't let interrupt failure block the ack

    def _busy_steer_ack_enabled(self, event: MessageEvent, session_key: str) -> bool:
        # Some mobile chat setups want silent steering — keep the behavior, drop the bubble.
        from gateway.run import _load_gateway_config, _platform_config_key
        from gateway.display_config import resolve_display_setting
        steer_ack_env = os.environ.get("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED")
        if steer_ack_env is not None:
            steer_ack_enabled = steer_ack_env.strip().lower() in {"1", "true", "yes", "on"}
        else:
            steer_ack_enabled = bool(
                resolve_display_setting(
                    _load_gateway_config(), _platform_config_key(event.source.platform),
                    "busy_steer_ack_enabled", True,
                )
            )
        if not steer_ack_enabled:
            logger.debug("Busy steer ack suppressed for session %s", session_key)
        return steer_ack_enabled

    async def _maybe_react_discord_busy_steer(
        self, event: MessageEvent, adapter: Any, session_key: str, *,
        steered: bool, is_steer_mode: bool,
    ) -> bool:
        """Single Discord busy-steer ⏩ receipt; shared by facade and mixin busy handlers."""
        # Mobile Discord clients treat reactions as lighter-weight steer receipts
        # than a bot reply bubble; steer delivery is unchanged.
        if not (steered and is_steer_mode and event.source.platform == Platform.DISCORD):
            return False
        if not self._busy_steer_ack_enabled(event, session_key):
            return True
        # ⏩ steer receipt is a reactions feature — honor the adapter's
        # reactions opt-out; skip is silent (no bubble) per busy-steer contract.
        reactions_gate = getattr(adapter, "_reactions_enabled", None)
        if not callable(reactions_gate):
            logger.debug(
                "Discord busy-steer reaction skipped (missing _reactions_enabled) "
                "for session %s",
                session_key,
            )
            return True
        try:
            if not reactions_gate():
                logger.debug(
                    "Discord busy-steer reaction suppressed (reactions disabled) "
                    "for session %s",
                    session_key,
                )
                return True
        except Exception:
            logger.debug(
                "Discord busy-steer reaction gate failed for session %s",
                session_key,
                exc_info=True,
            )
            return True
        add_reaction = getattr(adapter, "_add_reaction", None)
        raw = getattr(event, "raw_message", None)
        if not callable(add_reaction) or raw is None:
            logger.debug(
                "Discord busy-steer reaction skipped (missing _add_reaction or raw_message) "
                "for session %s",
                session_key,
            )
            return True
        try:
            await add_reaction(raw, "⏩")
        except Exception as exc:
            logger.debug(
                "Discord busy-steer reaction failed for session %s: %s",
                session_key,
                exc,
            )
        return True

    _BUSY_DEMOTED_TAIL = (
        " — your message is queued for when it finishes (use /stop to cancel everything)."
    )

    # Human phrasing for why a steer-mode message fell back to queue, keyed by
    # _BusySteerOutcome.steer_fallback_reason.
    _STEER_FALLBACK_REASONS = {
        "agent-not-live": "the run isn't accepting injections right now",
        "media-not-steerable": "attachments can't be steered mid-run",
        "steer-failed": "the active run didn't accept the injection",
    }

    def _compose_busy_ack_message(
        self, event: MessageEvent, now: float, _busy_state, running_agent: Any, *,
        is_steer_mode: bool, is_queue_mode: bool, is_redirect_mode: bool,
        demoted_for_subagents: bool, demoted_for_compression: bool,
        steer_fallback_reason: Optional[str] = None,
        parked_for_steer: bool = False,
    ) -> str:
        from gateway.run import (
            _AGENT_PENDING_SENTINEL, _hermes_home, _load_gateway_config, _platform_config_key
        )
        from gateway.display_config import resolve_display_setting

        # Terse by default; iteration/tool detail opts in via display.platforms.<p>.busy_ack_detail.
        status_parts = []
        busy_ack_detail_enabled = bool(
            resolve_display_setting(
                _load_gateway_config(), _platform_config_key(event.source.platform),
                "busy_ack_detail", True,
            )
        )
        if busy_ack_detail_enabled and running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            try:
                summary = running_agent.get_activity_summary()
                elapsed_min = 0
                if _busy_state and _busy_state.turn.started_ts:
                    elapsed_min = int((now - _busy_state.turn.started_ts) / 60)
                if elapsed_min > 0:
                    status_parts.append(f"{elapsed_min} min elapsed")
                if summary.get("max_iterations", 0):
                    status_parts.append(
                        format_iteration_progress(
                            summary.get("api_call_count", 0), summary.get("max_iterations", 0)
                        )
                    )
                if summary.get("current_tool"):
                    status_parts.append(f"running: {summary.get('current_tool')}")
            except Exception:
                pass
        status_detail = f" ({', '.join(status_parts)})" if status_parts else ""
        if parked_for_steer:
            head, tail = "⏩ Steered into current run (pending start)", \
                ". It will be injected as soon as the run accepts."
        elif is_steer_mode and self._agent_has_active_subagents(running_agent):
            head = "⏩ Steered into current run and its active subagent(s)"
            tail = ". Your message arrives after their next tool call."
        elif is_steer_mode:
            head, tail = "⏩ Steered into current run", ". Your message arrives after the next tool call."
        elif is_redirect_mode:
            head, tail = "↪ Redirected current run", ". I'll adjust using your correction."
        elif is_queue_mode and demoted_for_subagents:
            # Explain the demotion: the follow-up didn't kill the subagent; /stop is the escape hatch.
            head, tail = "⏳ Subagent working", self._BUSY_DEMOTED_TAIL
        elif is_queue_mode and demoted_for_compression:
            head, tail = "⏳ Compressing context", self._BUSY_DEMOTED_TAIL
        elif is_queue_mode and steer_fallback_reason:
            # Steer mode was active but fell back to queue — say why instead
            # of the generic queue text, which reads as steering failing silently.
            _why = self._STEER_FALLBACK_REASONS.get(steer_fallback_reason, "steering wasn't possible")
            head, tail = f"⏳ Couldn't steer into the current run ({_why})", \
                " — queued for the next turn. I'll respond once the current task finishes."
        elif is_queue_mode:
            head, tail = "⏳ Queued for the next turn", ". I'll respond once the current task finishes."
        else:
            head, tail = "⚡ Interrupting current task", ". I'll respond to your message shortly."
        message = f"{head}{status_detail}{tail}"

        # One-time onboarding hint about the queue/interrupt knob (flag persisted to config.yaml).
        try:
            from agent.onboarding import (BUSY_INPUT_FLAG, busy_input_hint_gateway, is_seen, mark_seen)
            if not is_seen(_load_gateway_config(), BUSY_INPUT_FLAG):
                _hint_mode = (
                    "steer" if is_steer_mode
                    else "queue" if is_queue_mode
                    else "redirect" if is_redirect_mode
                    else "interrupt"
                )
                message = f"{message}\n\n{busy_input_hint_gateway(_hint_mode)}"
                mark_seen(_hermes_home / "config.yaml", BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug("Failed to apply busy-input onboarding hint: %s", _onb_err)
        return message

    async def _send_busy_ack_reply(self, event: MessageEvent, adapter, message: str) -> None:
        try:
            await self._send_busy_reply(event, adapter, message)
        except Exception as e:
            logger.debug("Failed to send busy-ack: %s", e)

    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        from gateway.run import (
            _AGENT_PENDING_SENTINEL, _build_media_placeholder, _hermes_home,
            _load_gateway_config, _platform_config_key,
        )

        # --- Authorization gate (#17775) ---
        # The cold path (_handle_message) checks _is_user_authorized before
        # creating a session.  The busy path must enforce the same check;
        # otherwise unauthorized users in shared threads (Slack/Telegram/Discord)
        # can inject messages into an active session they don't own.
        if not self._is_user_authorized(event.source):
            logger.warning(
                "Dropping message from unauthorized user in active session: "
                "user=%s (%s), platform=%s, session=%s",
                event.source.user_id,
                event.source.user_name,
                event.source.platform.value if event.source.platform else "unknown",
                session_key,
            )
            return True  # handled (silently dropped); do not fall through
        # Bot loop guard: a steered or queued follow-up never reaches the cold-path
        # admission, so the budget is charged here.
        if not self._admit_bot_message_for_source(event.source):
            return True
        event._bot_loop_admitted = True

        effective_mode = self._effective_busy_input_mode(event.source)

        # --- Draining case (gateway restarting/stopping) ---
        from gateway.overlap_restart import admits_local_drain_turn, admits_owned_work
        if self._draining and not admits_owned_work(self, event.source) \
                and not admits_local_drain_turn(self, event.source):
            adapter = self._adapter_for_source(event.source)
            if not adapter:
                return True

            reply_anchor = self._reply_anchor_for_event(event)
            thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
            message = self._busy_drain_admission(event, session_key, effective_mode)

            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=(
                    reply_anchor
                    if event.source.platform == Platform.TELEGRAM
                    and event.source.chat_type == "dm"
                    and event.source.thread_id
                    else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
                ),
                metadata=thread_meta,
            )
            return True

        # Normal busy case (agent actively running a task)
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return False  # let default path handle it

        # --- Internal synthetic events must never interrupt/steer ---
        # Async-delegation completions (delegate_agent(background=true)) and
        # background-process completions (terminal notify_on_complete) re-enter
        # the originating session as internal MessageEvents. When the session
        # is busy, treating them like a user TEXT message means interrupt-mode
        # (the default busy_text_mode) aborts the active turn AND sends a "⚡
        # Interrupting current task" ack — exactly the opposite of the design
        # invariant that a completion surfaces as a NEW turn only when idle and
        # never splices into a running turn. Plugin events carry untrusted
        # payload text, so queue those through the gateway FIFO to keep their
        # security metadata separate from pending user input.
        if getattr(event, "internal", False) and not event.allow_gateway_control:
            self._queue_or_replace_pending_event(session_key, event)
            return True
        if getattr(event, "internal", False):
            return False

        _busy_state = self._peek_session_state(session_key)
        running_agent = _busy_state.turn.agent if _busy_state else None

        busy_text_mode = self._effective_busy_text_mode(event.source)
        if (
            event.message_type == MessageType.TEXT
            and busy_text_mode == "queue"
            and effective_mode != "steer"
        ):
            return False

        # Steer mode: inject mid-run via running_agent.steer() instead of
        # queueing + interrupting.  If the agent isn't running yet
        # (sentinel) or lacks steer(), or the payload is empty, fall back
        # to queue semantics so nothing is lost.
        # #30170 — Subagent protection. ``AIAgent.interrupt()`` cascades
        # to every entry in the parent's ``_active_children`` list and
        # aborts in-flight ``delegate_agent`` work. Demote ``interrupt``
        # to ``queue`` when the parent is currently driving subagents so
        # a conversational follow-up doesn't destroy minutes of subagent
        # work. Explicit ``/stop`` and ``/new`` slash commands go through
        # ``_interrupt_and_clear_session`` and are unaffected — the
        # operator still has a way to force-cancel everything.
        # #56391 — Compression protection: context compression is
        # interrupt-protected (#23975), but an interrupt here starts a new
        # turn against the pre-rotation parent session while the
        # still-running compression later rotates the id out from under
        # it, forking orphaned compression siblings.
        # Demotions, the steer attempt (with voice transcripts folded in,
        # #58780) and the text-only redirect are shared with the
        # _handle_message priority fast-path: one resolver, not two
        # hand-maintained copies.
        outcome = await self._resolve_busy_steer_or_redirect(
            event, session_key, effective_mode, running_agent
        )
        effective_mode = outcome.effective_mode
        demoted_for_subagents = outcome.demoted_for_subagents
        demoted_for_compression = outcome.demoted_for_compression
        steered = outcome.steered
        redirected = outcome.redirected
        steer_fallback_reason = outcome.steer_fallback_reason
        parked_for_steer = outcome.parked_for_steer
        if steered and self._steer_delivered_ack_enabled(event):
            # The busy-steer bubble below promises FUTURE delivery;
            # schedule the one-shot follow-up ack for the moment the
            # text actually lands in the model's context.
            self._register_steer_delivered_ack(event, session_key, running_agent)

        # Store the message so it's processed as the next turn after the
        # current run finishes (or is interrupted).  Skip this for a
        # successful steer — the text already landed inside the run and
        # must NOT also be replayed as a next-turn user message.
        #
        # Route through _queue_or_replace_pending_event (the same FIFO
        # infrastructure used by busy queue-mode and /queue) rather than a
        # raw merge_pending_message_event(merge_text=True). The raw merge
        # newline-joins consecutive TEXT follow-ups into a SINGLE pending
        # turn, destroying message boundaries — so two separate user
        # messages sent while the agent was busy (interrupt mode, or a
        # steer that fell back to queue) arrived as one mashed-together
        # turn (#43066 sub-bug 2). The FIFO path gives each text its own
        # turn in arrival order while still preserving photo-burst / album
        # merge semantics for media.
        if not steered and not redirected and not parked_for_steer:
            self._queue_or_replace_pending_event(session_key, event)

        is_queue_mode = effective_mode == "queue"
        is_steer_mode = effective_mode == "steer"
        is_redirect_mode = effective_mode == "interrupt" and redirected

        # If not in queue/steer mode, interrupt the running agent immediately.
        # This aborts in-flight tool calls and causes the agent loop to exit
        # at the next check point.
        if (
            effective_mode == "interrupt"
            and not redirected
            and running_agent
            and running_agent is not _AGENT_PENDING_SENTINEL
        ):
            try:
                _interrupt_text = event.text
                _media_urls = getattr(event, "media_urls", None) or []
                if self._pending_event_audio_paths(event):
                    _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                        event,
                        adapter,
                        event.source,
                        event.text or "",
                        log_context="Voice-busy-interrupt",
                    )
                elif not _interrupt_text and _media_urls:
                    _interrupt_text = _build_media_placeholder(event)
                running_agent.interrupt(_interrupt_text)
            except Exception:
                pass  # don't let interrupt failure block the ack

        # Check if busy ack is disabled — skip sending but still process the input.
        # Placed before debounce so we don't stamp a "last ack" timestamp that was
        # never actually delivered.
        busy_ack_enabled = os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true").lower() == "true"
        if not busy_ack_enabled:
            logger.debug("Busy ack suppressed for session %s", session_key)
            return True  # input still processed, just no ack sent

        # Mobile Discord clients treat reactions as lighter-weight steer receipts
        # than a bot reply bubble; steer delivery is unchanged.
        if await self._maybe_react_discord_busy_steer(
            event, adapter, session_key, steered=steered, is_steer_mode=is_steer_mode
        ):
            return True

        # Debounce before consulting config-heavy display settings. Rapid
        # follow-ups should be processed but should not trigger another config
        # read just to discover that no ack will be sent.
        _BUSY_ACK_COOLDOWN = 30
        now = time.time()
        last_ack = _busy_state.turn.busy_ack_ts if _busy_state else 0
        if now - last_ack < _BUSY_ACK_COOLDOWN:
            return True  # interrupt sent (if not queue), ack already delivered recently

        from gateway.display_config import resolve_display_setting
        platform_key = _platform_config_key(event.source.platform)

        # In steer mode the user's text has already been injected into the
        # active run. Some mobile chat setups want that steering to be silent,
        # like STT transcript echo suppression: keep the behavior, drop only
        # the confirmation bubble.
        if is_steer_mode:
            steer_ack_env = os.environ.get("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED")
            if steer_ack_env is not None:
                steer_ack_enabled = steer_ack_env.strip().lower() in {"1", "true", "yes", "on"}
            else:
                steer_ack_enabled = bool(
                    resolve_display_setting(
                        _load_gateway_config(),
                        platform_key,
                        "busy_steer_ack_enabled",
                        True,
                    )
                )
            if not steer_ack_enabled:
                logger.debug("Busy steer ack suppressed for session %s", session_key)
                return True

        self._session_state(session_key).turn.busy_ack_ts = now

        # Build a status-rich acknowledgment. Mobile chat defaults keep this
        # terse; detailed iteration/tool state is still available in logs and
        # can be opted in per platform via display.platforms.<platform>.busy_ack_detail.
        status_parts = []
        busy_ack_detail_enabled = bool(
            resolve_display_setting(
                _load_gateway_config(),
                _platform_config_key(event.source.platform),
                "busy_ack_detail",
                True,
            )
        )

        if busy_ack_detail_enabled and running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            try:
                summary = running_agent.get_activity_summary()
                iteration = summary.get("api_call_count", 0)
                max_iter = summary.get("max_iterations", 0)
                current_tool = summary.get("current_tool")
                start_ts = _busy_state.turn.started_ts if _busy_state else 0
                if start_ts:
                    elapsed_min = int((now - start_ts) / 60)
                    if elapsed_min > 0:
                        status_parts.append(f"{elapsed_min} min elapsed")
                if max_iter:
                    # sys.maxsize means unbounded — render "iteration N" without
                    # the sentinel denominator (#102806).
                    from agent.session_activity import format_iteration_progress

                    status_parts.append(format_iteration_progress(iteration, max_iter))
                if current_tool:
                    status_parts.append(f"running: {current_tool}")
            except Exception:
                pass

        status_detail = f" ({', '.join(status_parts)})" if status_parts else ""
        if parked_for_steer:
            message = (
                f"⏩ Steered into current run (pending start){status_detail}. "
                f"It will be injected as soon as the run accepts."
            )
        elif is_steer_mode:
            message = (
                f"⏩ Steered into current run{status_detail}. "
                f"Your message arrives after the next tool call."
            )
        elif is_redirect_mode:
            message = (
                f"↪ Redirected current run{status_detail}. "
                f"I'll adjust using your correction."
            )
        elif is_queue_mode and demoted_for_subagents:
            # #30170 — explain the demotion so the user knows their
            # follow-up didn't accidentally kill the subagent and
            # discovers `/stop` as the explicit escape hatch.
            message = (
                f"⏳ Subagent working{status_detail} — your message is queued for "
                f"when it finishes (use /stop to cancel everything)."
            )
        elif is_queue_mode and demoted_for_compression:
            message = (
                f"⏳ Compressing context{status_detail} — your message is queued for "
                f"when it finishes (use /stop to cancel everything)."
            )
        elif is_queue_mode and steer_fallback_reason:
            # Steer mode was active but fell back to queue — say why instead
            # of the generic queue text, which reads as steering failing silently.
            _why = self._STEER_FALLBACK_REASONS.get(steer_fallback_reason, "steering wasn't possible")
            message = (
                f"⏳ Couldn't steer into the current run ({_why}){status_detail} — "
                f"queued for the next turn. I'll respond once the current task finishes."
            )
        elif is_queue_mode:
            message = (
                f"⏳ Queued for the next turn{status_detail}. "
                f"I'll respond once the current task finishes."
            )
        else:
            message = (
                f"⚡ Interrupting current task{status_detail}. "
                f"I'll respond to your message shortly."
            )

        # First-touch onboarding: the very first time a user sends a message
        # while the agent is busy, append a one-time hint explaining the
        # queue/interrupt knob.  Flag is persisted to config.yaml so it never
        # fires again on this install.
        try:
            from agent.onboarding import (
                BUSY_INPUT_FLAG,
                busy_input_hint_gateway,
                is_seen,
                mark_seen,
            )
            _user_cfg = _load_gateway_config()
            if not is_seen(_user_cfg, BUSY_INPUT_FLAG):
                if is_steer_mode:
                    _hint_mode = "steer"
                elif is_queue_mode:
                    _hint_mode = "queue"
                elif is_redirect_mode:
                    _hint_mode = "redirect"
                else:
                    _hint_mode = "interrupt"
                message = (
                    f"{message}\n\n"
                    f"{busy_input_hint_gateway(_hint_mode)}"
                )
                mark_seen(_hermes_home / "config.yaml", BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug("Failed to apply busy-input onboarding hint: %s", _onb_err)

        reply_anchor = self._reply_anchor_for_event(event)
        thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
        try:
            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=(
                    reply_anchor
                    if event.source.platform == Platform.TELEGRAM
                    and event.source.chat_type == "dm"
                    and event.source.thread_id
                    else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
                ),
                metadata=thread_meta,
            )
        except Exception as e:
            logger.debug("Failed to send busy-ack: %s", e)

        return True

    # Slash name → handler method is ``_handle_<name>_command`` (``-`` → ``_``) except these.
    _COMMAND_HANDLER_ALIASES = {
        "bg": "_handle_background_command",
        "setnotify": "_handle_set_notify_command",
        "clearnotify": "_handle_clear_notify_command",
    }
    # Ordinary slash handlers shared by idle and busy dispatch.
    _PLAIN_COMMANDS = (
        "status", "context", "restart", "pause", "agents", "bg", "btw",
        "subgoal", "busy", "verbose", "footer", "help",
        "commands", "profile", "login", "update", "version",
    )
    # Dispatched only on the idle path (busy dispatch has its own allowlist).
    _IDLE_COMMANDS = (
        "topic", "whoami", "platform", "stop", "memory", "skills",
        "codex-runtime", "suggestions", "save", "retry",
        "setnotify", "clearnotify", "compress", "usage", "topup", "insights", "reload-mcp", "reload-skills",
        "debug", "title", "resume", "sessions", "branch", "rollback", "diff",
        "loop", "refine", "review", "voice",
    )

    def _command_handler_table(self, names) -> Dict[str, Any]:
        return {
            name: getattr(
                self, self._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}_command"),
            )
            for name in names
        }

    def _gateway_plain_command_handlers(self):
        """Return ordinary slash handlers shared by idle and busy dispatch."""
        return self._command_handler_table(self._PLAIN_COMMANDS)

    async def _send_command_ack(self, source, text: str, label: str) -> None:
        """Best-effort acknowledgment for a slash command that falls through to agent processing."""
        try:
            adapter = self._delivery_adapter_for(source)
            if adapter:
                await adapter.send(
                    str(source.chat_id), text, metadata=self._thread_metadata_for_source(source)
                )
        except Exception:
            logger.debug("%s ack send failed", label, exc_info=True)

    def _gateway_idle_command_handlers(self):
        """Slash handlers dispatched only on the idle path (busy dispatch has its own allowlist)."""
        return self._command_handler_table(self._IDLE_COMMANDS)

    # busy_handler key (hermes_cli/commands.py CommandDef) → mid-run variant ``_busy_<key>_command``.
    _BUSY_SPECIAL_HANDLERS: Dict[str, str] = {
        k: f"_busy_{k}_command" for k in ("start", "stop", "new", "incognito", "queue", "egress", "loop")
    }

    async def _dispatch_busy_slash_command(self, event: MessageEvent, cmd_def, quick_key: str, source):
        """Dispatch a recognized slash command while an agent is running.

        Order: ``busy_handler`` (mid-run variant) → ``busy_policy == "dispatch"`` (normal handler)
        → catch-all reject text. Rejecting is required rather than falling through to
        interrupt + discard: commands like /voice, /insights, /title,
        /resume, /retry, /undo, /compress, /usage, /reload-mcp, /setnotify (all
        registered as Discord slash commands) would interrupt the agent AND get silently
        discarded by the slash-command safety net, producing a zero-char response.
        See #5057, #6252, #10370.

        1. ``busy_handler`` — special mid-run variant (e.g. /goal's control-verb whitelist, /queue's FIFO
        enqueue, /model's custom reject text). 2. 3. See #5057, #6252, #10370.
        """
        name = cmd_def.name
        policy = getattr(cmd_def, "busy_policy", "reject")
        handler_key = getattr(cmd_def, "busy_handler", None)
        if handler_key:
            special = self._BUSY_SPECIAL_HANDLERS.get(handler_key)
            if special is not None:
                return await getattr(self, special)(event, quick_key, source)
            reject_text = self._BUSY_REJECT_TEXT.get(handler_key)
            if reject_text is not None:
                return reject_text
        if policy in ("dispatch", "interrupt_then_dispatch"):
            plain = self._gateway_plain_command_handlers().get(name)
            if plain is not None:
                return await plain(event)
            logger.warning(
                "busy_policy=%s for /%s has no mid-run handler — "
                "falling back to busy-reject", policy, name,
            )

        return (
            f"⏳ Agent is running — `/{name}` can't run "
            f"mid-turn. Wait for the current response or `/stop` first."
        )

    async def _handle_pause_command(self, event: MessageEvent):
        """`/pause [reason]` engages the global emergency stop; `/pause off` lifts it (the estop gate
        lets slash commands through while paused so messaging-only operators are never locked out)."""
        from agent import estop
        args = (event.get_command_args() or "").strip()
        if args.lower() in {"off", "resume", "stop", "disengage"}:
            if estop.disengage():
                return "▶️ Resumed — new work is accepted again."
            return "Hermes wasn't paused."
        state = estop.get_state()
        if state is not None and not args:
            suffix = f" (reason: {state.get('reason')})" if state.get("reason") else ""
            return f"⏸️ Hermes is already paused{suffix}. Use `/pause off` to resume."
        estop.engage(reason=args or None)
        suffix = f" (reason: {args})" if args else ""
        return (
            f"⏸️ Paused{suffix}. New cron/gateway work is on hold; "
            "in-flight work finishes normally. Use `/pause off` to resume."
        )

    async def _busy_start_command(self, event: MessageEvent, quick_key: str, source):
        # Telegram's /start is a platform ping (bot launch/deep-link), not a user command.
        logger.info("Ignoring /start platform ping for active session %s", quick_key)
        return ""

    async def _busy_egress_command(self, event: MessageEvent, quick_key: str, source):
        from hermes_cli.proxy_cli import format_status_text
        return format_status_text()

    async def _busy_stop_command(self, event: MessageEvent, quick_key: str, source):
        # Hard-kill: a soft interrupt can't reach a truly hung executor thread.
        from gateway.run import _INTERRUPT_REASON_STOP
        await self._interrupt_and_clear_session(
            quick_key, source, interrupt_reason=_INTERRUPT_REASON_STOP, invalidation_reason="stop_command",
        )
        logger.info("STOP for session %s — agent interrupted, session lock released", quick_key)
        return EphemeralReply(t("gateway.stop.stopped"))

    async def _busy_new_command(self, event: MessageEvent, quick_key: str, source):
        # /reset and /new bypass the running-agent guard (else they'd queue as user text and replay
        # into the same broken history); clear pending messages so the old text doesn't replay.
        from gateway.run import _INTERRUPT_REASON_RESET
        # Interrupt the agent first, then clear the adapter's pending queue so the stale "/reset" text
        # doesn't get re-processed as a user message after the interrupt completes. See #2170.
        await self._interrupt_and_clear_session(
            quick_key, source, interrupt_reason=_INTERRUPT_REASON_RESET, invalidation_reason="new_command",
        )
        return await self._handle_reset_command(event)

    async def _busy_incognito_command(self, event: MessageEvent, quick_key: str, source):
        # Same shape as _busy_new_command: the reset must bypass the running-agent guard, and the
        # incognito flag is stamped on the NEW entry only after the reset lands.
        from gateway.run import _INTERRUPT_REASON_RESET
        await self._interrupt_and_clear_session(
            quick_key, source, interrupt_reason=_INTERRUPT_REASON_RESET, invalidation_reason="incognito_command",
        )
        return await self._handle_incognito_command(event)

    async def _busy_queue_command(self, event: MessageEvent, quick_key: str, source):
        # Each /queue is its own full agent turn, run FIFO after the current run; never merged.
        queued_text = event.get_command_args().strip()
        # A /queue carrying media or reply context is valid with no prompt text (image caption).
        has_media = bool(getattr(event, "media_urls", None))
        if not queued_text and not has_media:
            return "Usage: /queue <prompt>"
        adapter = self._delivery_adapter_for(source)
        if adapter:
            self._enqueue_fifo(quick_key, MessageEvent(
                text=queued_text, message_type=event.message_type if has_media else MessageType.TEXT,
                source=event.source, raw_message=event.raw_message, message_id=event.message_id,
                media_urls=list(getattr(event, "media_urls", []) or []),
                media_types=list(getattr(event, "media_types", []) or []),
                media_text_inlined=list(getattr(event, "media_text_inlined", []) or []),
                reply_to_message_id=event.reply_to_message_id, reply_to_text=event.reply_to_text,
                reply_to_author_id=event.reply_to_author_id,
                reply_to_author_name=event.reply_to_author_name,
                reply_to_is_own_message=event.reply_to_is_own_message, auto_skill=event.auto_skill,
                channel_prompt=event.channel_prompt, channel_context=event.channel_context,
                internal=event.internal, timestamp=event.timestamp,
            ), adapter)
        depth = self._queue_depth(quick_key, adapter=adapter)
        return "Queued for the next turn." + (f" ({depth} queued)" if depth > 1 else "")

    async def _busy_goal_command(self, event: MessageEvent, quick_key: str, source):
        # Control verbs are safe mid-run (state only); setting new goal text is rejected so we don't
        # race a second continuation against the current turn. wait/gate take an argument.
        from hermes_cli.goal_command import is_goal_control

        if is_goal_control(event.get_command_args() or ""):
            return await self._handle_goal_command(event)
        return "Agent is running — use /goal status / pause / clear / wait mid-run, or /stop before setting a new goal."

    async def _busy_loop_command(self, event: MessageEvent, quick_key: str, source):
        # Mirrors /goal: control verbs are safe mid-run; a new loop is rejected.
        _loop_arg = (event.get_command_args() or "").strip().lower()
        if not _loop_arg or _loop_arg in {"status", "pause", "resume", "stop", "clear", "cancel", "help", "--help", "-h"}:
            return await self._handle_loop_command(event)
        return "Agent is running — use /loop status / pause / stop mid-run, or /stop before setting a new loop."

    def _check_slash_access(self, source: SessionSource, canonical_cmd: str) -> Optional[str]:
        """Denial message if ``source`` cannot run ``canonical_cmd``, else None (both dispatch paths
        use it so an in-flight agent can't bypass admin gating; no ``allow_admin_from`` → None)."""
        from gateway.slash_access import policy_for_source as _policy_for_source
        if not canonical_cmd:
            return None
        policy = _policy_for_source(self.config, source)
        if not policy.enabled or policy.can_run(source.user_id, canonical_cmd):
            return None
        logger.info(
            "Slash command /%s denied for %s:%s (not admin, not in user_allowed_commands)",
            canonical_cmd, source.platform.value if source.platform else "?", source.user_id,
        )
        allowed_preview = sorted(policy.user_allowed_commands)
        if allowed_preview:
            suffix = (
                "You can run: " + ", ".join(f"/{c}" for c in allowed_preview[:12])
                + ("…" if len(allowed_preview) > 12 else "") + ". Use /whoami for the full list."
            )
        else:
            suffix = (
                "No slash commands are enabled for non-admins on this platform. Ask an admin to "
                "add you to allow_admin_from or to set user_allowed_commands."
            )
        return f"⛔ /{canonical_cmd} is admin-only here. {suffix}"

    def _same_chat_runs(self, source: SessionSource, own_key: str) -> List[Tuple[str, str, str]]:
        """``(key, chat_type, tail)`` for every OTHER running turn in the caller's chat (``tail`` is
        the key text after the chat id, ``""`` when the key ends there).

        The namespace comes from ``own_key`` — the session store's own answer, so a named-profile
        stop matches that profile's runs and never a literal. ``_snapshot_running_agents`` already
        drops the pending sentinel (a session still being set up has no agent). Callers gate on
        authorization; ``own_key`` is excluded. Both tiers share one call.
        """
        chat_id = str(getattr(source, "chat_id", None) or "")
        if not chat_id:
            return []
        if source.chat_type == "dm" and source.platform == Platform.WHATSAPP:
            # Match the same text build_session_key keyed: WhatsApp DM chat ids are canonicalised
            # there, so a raw JID/LID alias would never line up with the stored key.
            chat_id = canonical_whatsapp_identifier(chat_id) or chat_id
        namespace = ":".join(own_key.split(":", 2)[:2])
        prefix = f"{namespace}:{source.platform.value}:"
        scope_id = str(getattr(source, "scope_id", None) or "") or None
        runs = []
        for key in self._snapshot_running_agents():
            if key == own_key:
                continue
            parsed = _same_chat_key_slots(key, prefix=prefix, chat_id=chat_id, scope_id=scope_id)
            if parsed is not None:
                runs.append((key, parsed[0], parsed[1]))
        return runs

    def _sibling_thread_run_keys(
        self, source: SessionSource, runs: List[Tuple[str, str, str]],
    ) -> List[str]:
        """Keys from ``runs`` belonging to OTHER participants in the caller's own thread (per-user
        thread mode keys are ``...:{thread_id}:{user_id}``, so another user's run is invisible to the
        caller's own ``/stop``). Callers still gate on authz."""
        thread_id = str(getattr(source, "thread_id", None) or "")
        chat_type = getattr(source, "chat_type", None) or ""
        if not thread_id or not chat_type:
            return []
        return [
            key
            for key, key_chat_type, tail in runs
            if key_chat_type == chat_type and _tail_has_slot(tail, thread_id)
        ]

    def _chat_scoped_run_keys(
        self, source: SessionSource, runs: List[Tuple[str, str, str]],
    ) -> List[str]:
        """Keys from ``runs`` for ANY session of the same chat, whatever the chat_type/thread/
        participant slots. Two supported shapes make a /stop key miss a run in the same chat (found
        via Slack's native stop button, gateway-gateway#286): a top-level channel turn keys
        ``channel`` while an in-thread /stop normalizes to ``thread``, and rolling-DM configs key
        without the thread slot the stop carries. "/stop" means "stop what's running in THIS chat",
        which is also what lets a human stop a peer's per-sender group run (see ``_same_chat_runs``).

        A stop sent from INSIDE a thread only reaches runs whose own thread slot is that thread (or
        that carry no thread slot at all — the rolling-DM shape). Anything else in the channel is a
        different conversation: another reply thread, or a peer's top-level run. Callers gate on
        authz.
        """
        thread_id = str(getattr(source, "thread_id", None) or "")
        return [
            key
            for key, _key_chat_type, tail in runs
            if not thread_id or not tail or _tail_has_slot(tail, thread_id)
        ]

    def _is_stale_restart_redelivery(self, event: MessageEvent) -> bool:
        """True if this /restart is a Telegram re-delivery we already handled.

        The previous gateway wrote ``.restart_last_processed.json`` (platform + update_id). A
        /restart with update_id <= that value is a redelivery when this process booted from that
        restart; otherwise the marker must be < 5 minutes old. Telegram only (numeric ordering).
        """
        from gateway.run import _hermes_home
        if event is None or event.source is None or event.platform_update_id is None:
            return False
        try:
            if event.source.platform.value != "telegram":
                return False
        except Exception:
            return False

        try:
            marker_path = _hermes_home / ".restart_last_processed.json"
            if not marker_path.exists():
                # Missing marker: a redelivered /restart would otherwise re-restart forever. Suppress
                # ONLY when this process booted from a chat /restart AND is within a short post-boot
                # window; consume the flag one-shot so a later legitimate /restart is honored.
                if (
                    # Belt-and-suspenders for when the dedup marker goes missing (manually cleaned up, or
                    # the previous cycle's write failed). Without a marker the update_id comparison below
                    # can't run, so a redelivered /restart would sail through and re-restart the gateway —
                    # an infinite loop (issue #18528).
                    getattr(self, "_booted_from_restart", False)
                    and time.time() - getattr(self, "_startup_time", 0.0) < 60
                ):
                    self._booted_from_restart = False
                    return True
                return False
            data = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return False

        recorded_uid = data.get("update_id")
        if (
            data.get("platform") != "telegram"
            or not isinstance(recorded_uid, int)
            or event.platform_update_id > recorded_uid
        ):
            return False

        # A service-managed restart can outlast the 5-minute trust window; consume the boot
        # signal one-shot.
        if getattr(self, "_booted_from_restart", False):
            self._booted_from_restart = False
            return True

        # Staleness guard: an old marker (crash recovery) must not swallow a fresh /restart.
        requested_at = data.get("requested_at")
        return not (isinstance(requested_at, (int, float)) and time.time() - requested_at > 300)

    async def _handle_suggestions_command(self, event: MessageEvent) -> str:
        """/suggestions via the shared handler (origin = event source so jobs deliver back here)."""
        from gateway.run import _command_origin_for_source
        try:
            from hermes_cli.suggestions_cmd import handle_suggestions_command
            return handle_suggestions_command(
                (event.get_command_args() or "").strip(),
                origin=_command_origin_for_source(event.source), surface="gateway",
            )
        except Exception as e:
            logger.debug("suggestions command failed: %s", e)
            return f"Suggestions command failed: {e}"

    async def _handle_blueprint_command(self, event: MessageEvent):
        """/blueprint via the shared handler (origin = event source so jobs deliver back here)."""
        from gateway.run import _command_origin_for_source
        try:
            from hermes_cli.blueprint_cmd import handle_blueprint_command
            return handle_blueprint_command(
                (event.get_command_args() or "").strip(),
                origin=_command_origin_for_source(event.source), surface="gateway",
            )
        except Exception as e:
            logger.debug("blueprint command failed: %s", e)
            from hermes_cli.blueprint_cmd import BlueprintCommandResult
            return BlueprintCommandResult(f"Cron blueprint command failed: {e}")


    async def _request_slash_confirm(
        self, *, event: MessageEvent, command: str, title: str, message: str, handler,
        include_always: bool = True,
    ) -> Optional[str]:
        """Ask the user to confirm a slash command and invoke ``handler(choice)``."""
        from tools import slash_confirm as _slash_confirm_mod
        source = event.source
        session_key = self._session_key_for_source(source)
        # object.__new__ test runners lack the counter; fall back to a local one.
        counter = getattr(self, "_slash_confirm_counter", None)
        if counter is None:
            import itertools as _itertools
            counter = self._slash_confirm_counter = _itertools.count(1)
        confirm_id = f"{next(counter)}"

        # Register FIRST so a fast button click cannot race the send_slash_confirm return.
        choices = ("once", "always", "cancel") if include_always else ("once", "cancel")
        _slash_confirm_mod.register(
            session_key, confirm_id, command, handler, allowed_choices=choices)

        adapter = self._delivery_adapter_for(source)
        metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

        if adapter is not None:
            try:
                button_result = await adapter.send_slash_confirm(
                    chat_id=source.chat_id, title=title, message=message, session_key=session_key,
                    confirm_id=confirm_id, metadata=metadata, include_always=include_always,
                )
                if button_result and getattr(button_result, "success", False):
                    return None  # buttons rendered — no redundant text ack
                # P5(b): distinguish a connector egress DECLINE from a lane
                # failure. On a decline the connector refused this destination,
                # so returning `message` as the direct reply would deliver the
                # very content it refused, as text, to the same chat. Suppress
                # the fallback and tear down the registration — no card
                # rendered, so a later reply must not be captured as an answer
                # to an invisible prompt.
                #
                # Classify the STRUCTURED response: a code-only decline has no marker colon in its
                # rendered text, and an ambiguous result is not a definite refusal.
                # and an ambiguous result must not be treated as a definite
                # refusal.
                from gateway.relay.egress import declined_send

                _confirm_err = getattr(button_result, "error", None)
                if declined_send(button_result):
                    logger.warning(
                        "slash-confirm DECLINED by the connector's egress "
                        "guard for %s on %s — suppressing the text fallback: %s",
                        command, source.platform, _confirm_err,
                    )
                    _slash_confirm_mod.clear(session_key)
                    return None
            except Exception as exc:
                logger.debug("send_slash_confirm failed for %s on %s: %s", command, source.platform, exc)
        # Text fallback — the prompt message itself is the direct reply.
        return message

    def _read_user_config(self) -> Dict[str, Any]:
        """Raw config.yaml for gate lookups that must see on-disk changes without a restart."""
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            return {}
        return cfg if isinstance(cfg, dict) else {}
