"""Per-turn context shared between ``GatewayRunner._run_agent_inner`` and ``TurnRunner``.

Each ex-closure local is a field, written once by ``_run_agent_inner`` while wiring the turn.
``message`` (ex-``nonlocal``) is the only rebindable field; other mutable state uses
single-element lists so mutation stays visible to the outer body.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class TurnContext:
    # read-only turn identity / wiring
    source: Any = None
    # Scheduled heartbeats are proactive work, not replies to the source message that
    # registered the watch.  Their routine delivery surfaces stay quiet.
    scheduled_heartbeat: bool = False
    _run_still_current: Callable[[], bool] = None  # type: ignore[assignment]
    _live_status_adapter: Any = None
    _live_status_mode: str = "off"
    _thinking_enabled: bool = False
    progress_mode: str = "off"
    progress_grouping: str = "grouped"
    # This turn's resolved tool_preview_length (0 = unlimited), snapshotted at turn
    # start. Rendering reads this turn-owned value, never the process-global
    # agent.display budget: interleaved turns (Discord unlimited vs a capped
    # platform) must not change each other's previews mid-turn.
    tool_preview_max_len: int = 0
    tool_progress_enabled: bool = False
    progress_queue: Any = None
    log_queue: Any = None
    # Structured tool-stage events (moa_ask / moa_debate round
    # transitions) for surfaces that render one self-editing card per tool
    # invocation. None on every platform except Discord-with-embeds, so the
    # agent.moa_loop stage bus simply has no subscriber elsewhere.
    stage_event_queue: Any = None
    _stage_embed_adapter: Any = None

    # --- mutable single-element containers (shared with the outer body) --
    # Monotonic timestamp of this turn's last USER-VISIBLE progress (a delivered
    # tool update, commentary line, successful progress/status/stream edit or
    # send — never typing indicators, internal model/tool activity, or failed and
    # suppressed sends). The long-running heartbeat waits for gateway_notify_interval
    # of silence since this timestamp, so continuous visible progress suppresses
    # "still working" bubbles. Turn-scoped by construction: another chat's traffic
    # writes its own TurnContext. None until the first visible delivery.
    _last_visible_progress: List[float] = field(default_factory=lambda: [None])
    # Lazily-created per-turn asyncio.Lock serializing user-visible transport
    # operations (tool/commentary/stream send+edit) against the heartbeat
    # edit/send, so a heartbeat can never interleave with - or land stale after -
    # same-turn visible progress (and vice versa). Created on the turn's loop.
    _delivery_lock: Any = None
    last_progress_msg: list = field(default_factory=lambda: [None])
    last_tool: list = field(default_factory=lambda: [None])
    last_was_terminal_block: list = field(default_factory=lambda: [False])
    repeat_count: list = field(default_factory=lambda: [0])
    long_tool_hint_fired: list = field(default_factory=lambda: [False])
    agent_holder: list = field(default_factory=lambda: [None])
    _LONG_TOOL_THRESHOLD_S: float = 30.0
    _cleanup_progress: bool = False
    _cleanup_msg_ids: List[str] = field(default_factory=list)
    _progress_metadata: Optional[dict] = None
    _progress_reply_to: Optional[Any] = None
    message: Optional[str] = None  # the only rebindable field
    # turn parameters / config snapshots (read-only in run_sync)
    history: Any = None
    context_prompt: Optional[str] = None
    channel_prompt: Optional[str] = None
    session_id: Optional[str] = None
    session_key: Optional[str] = None
    run_generation: Optional[int] = None
    process_task_id: str = ""
    process_baseline: frozenset[str] = field(default_factory=frozenset)
    _interrupt_depth: int = 0
    event_message_id: Optional[str] = None
    # Raw inbound platform id (not the event_message_id reply anchor); stamped on the user turn.
    inbound_message_id: Optional[str] = None
    moa_config: Optional[dict] = None
    persist_user_message: Optional[Any] = None
    persist_user_timestamp: Optional[float] = None
    # display_kind of the persisted user row for a self-injected turn; DB-only, never sent.
    # "internal_notification" for async-delegation/background notifications (#82888).
    persist_user_display_kind: Optional[str] = None
    persist_user_display_metadata: Optional[dict] = None
    user_config: Any = None
    mute_notification_reply: bool = False
    enabled_toolsets: Any = None
    disabled_toolsets: Any = None
    log_mode_enabled: bool = False
    interim_assistant_messages_enabled: bool = False
    needs_progress_queue: bool = False
    AIAgent: Any = None
    resolve_display_setting: Any = None
    result_holder: list = field(default_factory=lambda: [None])
    tools_holder: list = field(default_factory=lambda: [None])
    stream_consumer_holder: list = field(default_factory=lambda: [None])
    streaming_tts_consumer_holder: list = field(default_factory=lambda: [None])
    # voice-ack wiring
    _voice_ack_fired: list = field(default_factory=lambda: [False])
    _voice_ack_guild: list = field(default_factory=lambda: [None])
    _voice_ack_loop: Any = None
    # hook / status bridge wiring
    _loop_for_step: Any = None
    _hooks_ref: Any = None
    _status_adapter: Any = None
    _status_chat_id: Any = None
    _status_thread_metadata: Optional[dict] = None
    # bound TurnRunner callbacks read via ctx
    progress_callback: Optional[Callable] = None
    voice_ack_callback: Optional[Callable] = None
    _step_callback_sync: Optional[Callable] = None
    _event_callback_sync: Optional[Callable] = None
    _status_callback_sync: Optional[Callable] = None
    # Slack-native task cards (opt-in); ID-bearing callbacks correlate start/complete by call ID
    # --- Slack-native task-card progress (opt-in; #29483) ------------------ True when the Slack adapter's
    # ``native_task_cards_enabled()`` opt-in is set for this turn's platform. The ID-bearing lifecycle
    # callbacks are published by TurnRunner (like voice_ack_callback above) so tool starts and completions
    # correlate by real tool-call ID instead of tool name.
    _native_slack_task_cards: bool = False
    native_tool_start_callback: Optional[Callable] = None
    native_tool_complete_callback: Optional[Callable] = None
    # Context-card ordering gate. ``_context_cards_queued`` flips True in
    # progress_callback (agent thread, plain box write) when the first
    # context.injected card lands on progress_queue — always BEFORE the first
    # model call. ``_context_gate_state`` is the shared one-shot latch:
    #   None  → gate not armed yet (nothing has waited so far)
    #   Event → armed: the FIRST waiter enqueued the ("__flush__", event)
    #           marker behind the cards; EVERY concurrent/ subsequent caller
    #           awaits the SAME event, so they all observe actual drain
    #           completion, never "someone started waiting".
    #   True  → cleared (delivered, or drain-lifecycle release) — cheap no-op.
    # ``_progress_task`` is the turn's OWN progress drain task
    # (TurnRunner.send_progress_messages), bound by the turn setup that
    # creates it. Its lifecycle — not any timer — is the gate's shutdown
    # signal: a task that never started, or that already ended, cancelled or
    # failed, can never deliver the marker, so the gate releases immediately.
    _context_cards_queued: list = field(default_factory=lambda: [False])
    _context_gate_state: list = field(default_factory=lambda: [None])
    _progress_task: list = field(default_factory=lambda: [None])

    def bind_progress_task(self, task) -> None:
        """Record the turn's owning progress-drain task (the
        ``asyncio.create_task(send_progress_messages())`` started by the turn
        setup). Its lifecycle is the ordering gate's shutdown signal — see
        ``wait_context_cards_delivered``."""
        self._progress_task[0] = task

    async def wait_context_cards_delivered(self) -> None:
        """Ordering gate for the turn's visible assistant output.

        The progress drain (``send_progress_messages``) polls on a 0.3s cadence
        and throttles edits to 1.5s, while stream/interim/final output reaches
        the user on separate paths that only share the ACTIVE-I/O delivery lock —
        queued context cards could still be sitting in ``progress_queue`` when the
        reply that used them becomes visible. The missing signal is queue-
        admission completion, so the first caller enqueues a ``("__flush__",
        event)`` marker BEHIND everything already queued (queue.Queue is FIFO, so
        all context cards injected before the first model call precede it) and
        waits for the drain to deliver what it absorbed and set the event.

        Completion is SHARED: concurrent callers await the same event, so no
        second output lane can slip past while the first is still gated. There
        is no invented timeout: the wait ends when the marker is set, or when
        the turn's OWN progress task (``_progress_task``, bound at turn setup)
        ends — a drain that never started, or already returned, was cancelled
        or failed can never deliver the marker, so the gate releases with
        ordering degraded to the pre-fix behavior instead of stalling the
        reply. A transport-hung send stays the transport's problem: its own
        timeouts/cancellation end the task, which ends this wait. A waiter
        cancelled from outside re-raises without touching the shared event or
        the progress worker. No cards queued / no progress queue / stale turn
        → immediate return."""
        import asyncio as _asyncio

        state = self._context_gate_state[0]
        if state is True:
            return
        if state is None:
            # Arming is synchronous on the turn's event loop (all gate callers
            # are loop tasks), so check-and-set here is atomic — exactly one
            # caller can ever enqueue the marker.
            q = self.progress_queue
            if (
                q is None
                or not self._context_cards_queued[0]
                or not self._run_still_current()
            ):
                self._context_gate_state[0] = True
                return
            ev = _asyncio.Event()
            try:
                q.put(("__flush__", ev))
            except Exception:
                # Unwritable/closed queue: nothing can ever drain it.
                self._context_gate_state[0] = True
                return
            self._context_gate_state[0] = ev
            state = ev
        if state.is_set():
            self._context_gate_state[0] = True
            return
        task = self._progress_task[0]
        if task is None or task.done():
            # Drain never started or already ended (returned, cancelled,
            # failed): nothing will ever set the marker — release now.
            self._context_gate_state[0] = True
            return
        waiter = _asyncio.ensure_future(state.wait())
        try:
            # First of: marker delivered (event set) OR the drain task ended.
            # If the task ends after delivering everything the event is
            # already set, so this races only against a drain that stopped
            # without (or before) delivering — exactly the release case.
            await _asyncio.wait({waiter, task}, return_when=_asyncio.FIRST_COMPLETED)
        except _asyncio.CancelledError:
            # This waiter was cancelled: leave the shared event armed for the
            # other lanes, never cancel the progress worker, and re-raise.
            raise
        finally:
            waiter.cancel()
        self._context_gate_state[0] = True

    def mark_visible_progress(self) -> None:
        """Record a successful user-visible delivery for this turn (see
        ``_last_visible_progress``). Called only after a send/edit result confirmed
        success, so failed, declined and suppressed output never resets the
        heartbeat's silence clock."""
        self._last_visible_progress[0] = time.monotonic()

    def last_visible_progress_monotonic(self) -> Optional[float]:
        return self._last_visible_progress[0]

    def delivery_lock(self):
        """The per-turn visible-delivery lock (see ``_delivery_lock``); lazily
        created on the running event loop."""
        if self._delivery_lock is None:
            import asyncio as _asyncio

            self._delivery_lock = _asyncio.Lock()
        return self._delivery_lock

    async def deliver_visible(self, deliver):
        """Await ``deliver()`` under the turn's delivery lock and mark visible
        progress only when it reports success. Serializes actual tool/commentary/
        stream transport operations against the heartbeat edit/send: whichever
        acquires first completes (and marks) before the other's transport call
        starts, so a heartbeat can no longer land an edit/send that same-turn
        visible progress has already superseded. ``deliver`` (and anything it
        calls back into, e.g. mark callbacks) must not acquire the lock -
        reentrancy would deadlock."""
        async with self.delivery_lock():
            result = await deliver()
            if getattr(result, "success", False):
                self.mark_visible_progress()
            return result
