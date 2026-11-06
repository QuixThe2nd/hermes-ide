"""Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from contextlib import nullcontext, suppress
from typing import TYPE_CHECKING, Any, Optional

from gateway.platforms.event import MessageEvent, MessageType

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


class GatewayGoalsMixin:
    """Goal/heartbeat continuation, post-turn hooks and loop-wakeup watcher methods for GatewayRunner."""

    # ── /goal — persistent cross-turn goals (Ralph-style loop) ──────────
    def _goal_max_turns_from_config(self) -> int:
        """Configured /goal turn budget. GatewayRunner.config is a GatewayConfig dataclass, so the
        top-level ``goals`` block is only reachable via hermes_cli.config.load_config()."""
        try:
            goals_cfg = (
                (self.config or {}).get("goals", {})
                if isinstance(self.config, dict)
                else getattr(self.config, "goals", {}) or {}
            )
            if not goals_cfg:
                from hermes_cli.config import load_config

                goals_cfg = (load_config() or {}).get("goals") or {}
            return int(goals_cfg.get("max_turns", 20) or 20)
        except Exception:
            return 20

    async def _warm_goals_session_db(self, label: str) -> None:
        """Warm the goals SessionDB cache off-loop (best-effort): a cold cache runs the state.db
        init on the loop thread and freezes the loop. The executor hop keeps the profile home
        override alive under multiplex; a failed warm-up is a bounded stall, never a crash."""
        try:
            from hermes_cli.goals import _get_session_db as _warm_goals_db

            await self._run_in_executor_with_context(_warm_goals_db)
        except Exception as exc:
            logger.warning("%s: session DB warm-up failed: %s", label, exc)

    async def _session_entry_for_manager(self, event: "MessageEvent", label: str):
        """Session entry for a /goal or /heartbeat manager, or None when lookup fails. Warms the
        SessionDB cache first (a cold cache drops the first write while the reply claims it was
        set). Internal events never touch activity (idle/daily reset clock)."""
        await self._warm_goals_session_db(label)
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                event.source, touch_activity=not bool(getattr(event, "internal", False)),
            )
        except Exception as exc:
            logger.debug("%s: session lookup failed: %s", label, exc)
            return None
        return session_entry if getattr(session_entry, "session_id", None) else None

    async def _manager_for_event(self, event: "MessageEvent", kind: str, load):
        """``(manager, session_entry)`` for *kind* ("goal"/"heartbeat"), or ``(None, None)``.
        ``load()`` imports the manager class and returns a ``session_id -> manager`` factory."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s manager unavailable: %s", kind, exc)
            return None, None
        session_entry = await self._session_entry_for_manager(event, f"{kind} manager")
        if session_entry is None:
            return None, None
        return factory(session_entry.session_id), session_entry

    async def _get_goal_manager_for_event(self, event: "MessageEvent"):
        """Return a GoalManager bound to the session for this gateway event.

        Returns ``(manager, session_entry)`` or ``(None, None)`` if the
        goals module can't be loaded.
        """
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            logger.debug("goal manager unavailable: %s", exc)
            return None, None
        # Warm the SessionDB cache off-loop. A cold cache freezes the
        # loop for the init duration and drops the first write: the
        # /goal reply claims the goal was set.
        await self._warm_goals_session_db("goal manager")
        try:
            # Session lookups on behalf of an internal event must not advance
            # the user-activity clock that drives idle/daily reset policy
            # (same class as the wake fix in _handle_message_with_agent).
            session_entry = await self.async_session_store.get_or_create_session(
                event.source,
                touch_activity=not bool(getattr(event, "internal", False)),
            )
        except Exception as exc:
            logger.debug("goal manager: session lookup failed: %s", exc)
            return None, None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None, None
        max_turns = self._goal_max_turns_from_config()
        return GoalManager(session_id=sid, default_max_turns=max_turns), session_entry

    async def _get_heartbeat_manager_for_event(self, event: "MessageEvent"):
        """Return a HeartbeatManager bound to the session for this event.

        Returns ``(manager, session_entry)`` or ``(None, None)``.
        """
        try:
            from hermes_cli.heartbeat import HeartbeatManager
        except Exception as exc:
            logger.debug("heartbeat manager unavailable: %s", exc)
            return None, None
        # Warm the SessionDB cache off-loop. A cold cache can drop the
        # first /heartbeat write while the reply claims it was set.
        await self._warm_goals_session_db("heartbeat manager")
        try:
            # Same reset-policy contract as _get_goal_manager_for_event:
            # internal events look up the session without touching activity.
            session_entry = await self.async_session_store.get_or_create_session(
                event.source,
                touch_activity=not bool(getattr(event, "internal", False)),
            )
        except Exception as exc:
            logger.debug("heartbeat manager: session lookup failed: %s", exc)
            return None, None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None, None
        return HeartbeatManager(session_id=sid), session_entry

    @staticmethod
    def _synthetic_prompt_event(source: Any, text: str, *, internal: bool = False) -> MessageEvent:
        """Build the TEXT event used to inject a goal/heartbeat/loop prompt into a session.

        The stored source's ``message_id`` is the message that registered the watch; a synthetic
        prompt is not a reply to it, so it is dropped or every progress bubble and final reply
        would quote that stale message (Telegram DM topics route anchorless via the topic id).
        """
        source = dataclasses.replace(source, message_id=None) if getattr(source, "message_id", None) else source
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, internal=internal)

    def _register_heartbeat_watch(self, quick_key: str, source: Any, session_id: str) -> None:
        """Track a session with an active heartbeat and start the poller.

        The registry maps ``quick_key`` → ``(source, session_id)`` so the
        poller can rebuild a MessageEvent and enqueue via the adapter FIFO.
        In-memory by design: heartbeat STATE survives restarts in SessionDB,
        but firing resumes when the user touches /heartbeat again in the new
        gateway process (documented; durable schedules belong to cron).
        """
        watch = getattr(self, "_heartbeat_watch", None)
        if watch is None:
            watch = {}
            self._heartbeat_watch = watch
        watch[quick_key] = (source, session_id)
        self._start_heartbeat_poller()

    def _unregister_heartbeat_watch(self, quick_key: str) -> None:
        watch = getattr(self, "_heartbeat_watch", None)
        if watch:
            watch.pop(quick_key, None)

    async def _heartbeat_poll_once(self, watch: dict) -> None:
        """Wake each idle watched session once; leave busy sessions' ticks unclaimed."""
        for quick_key, (source, session_id) in list(watch.items()):
            try:
                with self._profile_scope_for_source(source):
                    await self._heartbeat_poll_watch(watch, quick_key, source, session_id)
            except Exception as exc:
                logger.debug("heartbeat poll for %s failed: %s", quick_key, exc)

    async def _heartbeat_poll_watch(self, watch, quick_key, source, session_id):
        await self._warm_goals_session_db("heartbeat poll")
        store = getattr(self, "session_store", None)
        if store is not None:
            current = store.peek_session_id(quick_key)
            if not current:
                watch.pop(quick_key, None)
                return
            session_id = current
            watch[quick_key] = (source, session_id)
        adapter = self._delivery_adapter_for(source)
        if adapter is None or not adapter._message_handler:
            return
        if (
            self._is_session_running(quick_key)
            or quick_key in adapter._active_sessions
            or self._queue_depth(quick_key, adapter=adapter) > 0
        ):
            return  # keep missed intervals due until user work has drained
        from hermes_cli.heartbeat import HeartbeatManager

        mgr = HeartbeatManager(session_id=session_id)
        if not mgr.has_heartbeat():
            watch.pop(quick_key, None)
            return
        prompt = mgr.due_prompt()
        if not prompt:
            return
        event = self._synthetic_prompt_event(source, prompt)
        event.metadata["gateway_session_key"] = quick_key
        event._heartbeat_execution_started = False
        # Provenance read by display_kind_for_event / the turn's quiet surfaces; the event stays
        # non-internal so authorization and the emergency stop still apply.
        event._heartbeat_session_id = session_id
        # A pinned route skips topic recovery: no await between the idle
        # check and adapter claim. FIFO alone never wakes an idle session.
        try:
            await adapter.handle_message(event)
        finally:
            task = getattr(adapter, "_session_tasks", {}).get(quick_key)
            if task is not None:
                from gateway.run_heartbeat_acceptance import settle_heartbeat_attempt
                task.add_done_callback(lambda done: settle_heartbeat_attempt(event, mgr))
            elif quick_key not in adapter._active_sessions:
                mgr.abandon_fire()

    def _start_heartbeat_poller(self) -> None:
        """Start the single gateway-wide heartbeat poll task (idempotent)."""
        existing = getattr(self, "_heartbeat_poll_task", None)
        if existing is not None and not existing.done():
            return

        from hermes_cli.heartbeat import POLL_SECONDS

        async def _poll_loop():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                watch = getattr(self, "_heartbeat_watch", None)
                if not watch:
                    continue
                # Warm the cache off-loop once per poll. A watch can only
                # be registered through the warmed /heartbeat command, so
                # this covers only the degraded path where that warm-up
                # failed.
                await self._warm_goals_session_db("heartbeat poll")
                for quick_key, (source, session_id) in list(watch.items()):
                    try:
                        # Busy sessions coalesce their tick to the next idle poll.
                        if quick_key in self._running_agents:
                            continue
                        from hermes_cli.heartbeat import HeartbeatManager

                        mgr = HeartbeatManager(session_id=session_id)
                        if not mgr.has_heartbeat():
                            watch.pop(quick_key, None)
                            continue
                        prompt = mgr.due_prompt()
                        if not prompt:
                            continue
                        adapter = self._adapter_for_source(source)
                        if adapter is None:
                            continue
                        hb_event = MessageEvent(
                            text=prompt,
                            message_type=MessageType.TEXT,
                            source=source,
                            message_id=None,
                            channel_prompt=None,
                        )
                        self._enqueue_fifo(quick_key, hb_event, adapter)
                    except Exception as exc:
                        logger.debug("heartbeat poll for %s failed: %s", quick_key, exc)

        try:
            task = asyncio.create_task(_poll_loop())
            self._heartbeat_poll_task = task
            # PERMANENT once started (an infinite while-True loop, no exit
            # condition) — same as a _spawn_supervised watcher. Tag it so
            # _scale_to_zero_has_live_background_work() doesn't treat a
            # gateway with an active heartbeat watch as busy forever.
            task._hermes_supervised_watcher = True  # type: ignore[attr-defined]
            _bg = getattr(self, "_background_tasks", None)
            if _bg is not None:
                _bg.add(task)
                task.add_done_callback(_bg.discard)
        except Exception:
            logger.debug("Failed to start heartbeat poller", exc_info=True)

    def _goal_notice_adapter(self, source: Any):
        adapter = self._delivery_adapter_for(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
        return adapter

    async def _send_goal_status_notice(self, source: Any, message: str) -> None:
        """Send a /goal judge status line back to the originating chat/thread."""
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
            return

        try:
            metadata = self._thread_metadata_for_source(source)
        except Exception:
            metadata = None

        result = await adapter.send(source.chat_id, message, metadata=metadata)
        if result is not None and not getattr(result, "success", True):
            logger.warning(
                "goal continuation: status send failed: %s",
                getattr(result, "error", "unknown error"),
            )

    async def _defer_goal_status_notice_after_delivery(self, source: Any, message: str) -> None:
        """Send a /goal status line after the main response is delivered.

        The gateway message handler returns the agent response to the platform
        adapter, which sends it after this method's caller has returned.  For a
        natural Discord/Telegram reading order, goal status belongs after that
        send.  Platform adapters provide a one-shot post-delivery callback for
        exactly this boundary; when unavailable, fall back to direct awaited
        delivery rather than silently dropping the notice.
        """
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
            return

        async def _deliver() -> None:
            try:
                await self._send_goal_status_notice(source, message)
            except Exception as exc:
                logger.warning("goal continuation: status send failed: %s", exc, exc_info=True)

        try:
            session_key = self._session_key_for_source(source)
        except Exception:
            session_key = None

        if session_key and hasattr(adapter, "register_post_delivery_callback"):
            try:
                generation = None
                active = getattr(adapter, "_active_sessions", {}).get(session_key)
                if active is not None:
                    generation = getattr(active, "_hermes_run_generation", None)
                adapter.register_post_delivery_callback(
                    session_key,
                    _deliver,
                    generation=generation,
                )
                return
            except Exception as exc:
                logger.debug("goal continuation: post-delivery callback registration failed: %s", exc)

        await _deliver()

    async def _post_turn_manager(self, session_entry: Any, label: str, module: str, load):
        """Shared head of the post-turn hooks: ``load()`` imports the manager module and returns a
        ``session_id -> manager`` factory; None when unavailable / no session id. Warms the
        SessionDB cache first — a cold cache at the turn boundary drops the read/write."""
        try:
            factory = load()
        except Exception as exc:
            logger.debug("%s: %s module unavailable: %s", label, module, exc)
            return None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None
        await self._warm_goals_session_db(label)
        return factory(sid)

    async def _post_turn_goal_continuation(
        self,
        *,
        session_entry: Any,
        source: Any,
        final_response: str,
    ) -> None:
        """Run the goal judge after a gateway turn and, if still active,
        enqueue a continuation prompt for the same session.

        Called from ``_handle_message_with_agent`` at turn boundary, AFTER
        the response has been delivered. Safe when no goal is set.

        We use the adapter's pending-message / FIFO machinery so any real
        user message that arrives simultaneously is handled by the same
        queue and takes priority naturally.
        """
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            logger.debug("goal continuation: goals module unavailable: %s", exc)
            return

        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return

        max_turns = self._goal_max_turns_from_config()

        # Warm the SessionDB cache off-loop. A cold cache runs the
        # state.db init on the loop thread at the turn boundary (the
        # 2026-08-14 crash-loop seam). A slow init can drop the goal
        # read and silently end the goal loop.
        await self._warm_goals_session_db("goal continuation")

        mgr = GoalManager(session_id=sid, default_max_turns=max_turns)
        if not mgr.is_active():
            return

        try:
            from hermes_cli.goals import gather_background_processes as _gather_bg
            _bg_procs = _gather_bg()
        except Exception:
            _bg_procs = None

        # evaluate_after_turn calls judge_goal() which makes a synchronous
        # HTTP request to the auxiliary LLM.  Running it on the event-loop
        # thread would block Discord heartbeats for 10-40 s and cause
        # connection flaps, so we offload it to a thread-pool executor.
        # _run_in_executor_with_context (not bare run_in_executor): the
        # profile secret scope and auxiliary runtime context are contextvars,
        # and a default-executor hop would drop them — aux-client provider
        # resolution would then read credentials unscoped and fail under
        # multiplexing (same pattern as compression in slash_commands.py).
        decision = await self._run_in_executor_with_context(
            lambda: mgr.evaluate_after_turn(
                final_response or "",
                user_initiated=True,
                background_processes=_bg_procs,
            ),
        )
        msg = decision.get("message") or ""

        # Defer the status line until after the adapter has delivered the
        # agent's visible final response. The judge runs after the response is
        # produced but before BasePlatformAdapter sends it, so sending here
        # would show "✓ Goal achieved" before the answer itself. Registering
        # an awaited post-delivery callback preserves delivery reliability
        # without reversing the user-visible ordering.
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)

        if not decision.get("should_continue"):
            return

        prompt = decision.get("continuation_prompt") or ""
        if not prompt or source is None:
            return

        # Enqueue via the adapter's FIFO so a user message already in
        # flight preempts the continuation naturally.
        try:
            adapter = self._adapter_for_source(source)
            _quick_key = self._session_key_for_source(source)
            if adapter and _quick_key:
                cont_event = MessageEvent(
                    text=prompt,
                    message_type=MessageType.TEXT,
                    source=source,
                    message_id=None,
                    channel_prompt=None,
                )
                self._enqueue_fifo(_quick_key, cont_event, adapter)
        except Exception as exc:
            logger.debug("goal continuation: enqueue failed: %s", exc)

    async def _run_post_turn_hooks(
        self,
        *,
        agent_result: Any,
        source: Any,
        is_internal: bool,
        event: Any = None,
    ) -> None:
        """Run goal and loop bookkeeping after an agent turn returns."""
        final_text = self._final_text_for_post_turn_hooks(agent_result, event)

        try:
            session_entry = await self.async_session_store.get_or_create_session(
                source,
                touch_activity=not is_internal,
            )
        except Exception as exc:
            logger.debug("post-turn session resolution failed: %s", exc)
            return

        # Empty interrupted/errored responses must not drive /goal, but an
        # in-flight /loop tick still needs to be released and rescheduled.
        if final_text.strip():
            try:
                await self._post_turn_goal_continuation(
                    session_entry=session_entry,
                    source=source,
                    final_response=final_text,
                )
            except Exception as exc:
                logger.debug("goal continuation hook failed: %s", exc)
        try:
            await self._post_turn_loop_completion(
                session_entry=session_entry,
                source=source,
                final_response=final_text,
            )
        except Exception as exc:
            logger.debug("loop completion hook failed: %s", exc)

    @staticmethod
    def _final_text_for_post_turn_hooks(agent_result, event=None) -> str:
        """Text for /goal and /loop after a gateway turn.

        Streamed turns return None from _handle_message_with_agent
        (already_sent). The delivered reply is stashed on the event so
        those hooks still see it.
        """
        text = ""
        if isinstance(agent_result, dict):
            text = str(agent_result.get("final_response") or "")
        elif isinstance(agent_result, str):
            text = agent_result
        if text.strip():
            return text
        streamed = getattr(event, "_streamed_final_response", None)
        if isinstance(streamed, str) and streamed.strip():
            return streamed
        return text

    async def _post_turn_loop_completion(
        self,
        *,
        session_entry: Any,
        source: Any,
        final_response: str,
    ) -> None:
        """Complete a /loop wakeup tick after a gateway turn.

        No-op unless the session has a loop whose tick is in flight
        (``awaiting_response`` — set when the wakeup was injected). Applies
        the LOOP_COMPLETE marker / --until judge / caps and schedules the
        next tick; the idle wakeup watcher fires it when due.
        """
        try:
            from hermes_cli.loops import LoopManager
        except Exception as exc:
            logger.debug("loop completion: loops module unavailable: %s", exc)
            return

        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return

        # Warm the SessionDB cache off-loop. A cold cache at the turn
        # boundary stalls the loop for the init duration and can drop
        # the tick-completion write (the /goal continuation seam, one
        # sibling over).
        await self._warm_goals_session_db("loop completion")

        mgr = LoopManager(session_id=sid)
        state = mgr.state
        if state is None or not state.awaiting_response:
            return

        # The --until judge is a sync aux-LLM call — keep it off the event loop.
        decision = await asyncio.get_running_loop().run_in_executor(
            None, mgr.complete_tick, final_response or ""
        )
        msg = decision.get("message") or ""
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)

    async def _loop_wakeup_fire_one(
        self, sid: str, state: Any, now: float, warned_no_route: set, profile: Optional[str] = None,
    ) -> None:
        """Inject one due /loop wakeup into its session, applying every deferral rule. ``profile`` is
        the store being scanned (None = default); a ``profile`` persisted in the route wins."""
        from hermes_cli.loops import LoopManager, goal_blocks_loop_tick

        if state.awaiting_response or now < state.next_due_at:
            return
        route = state.route or {}
        platform_name = route.get("platform", "")
        chat_id = route.get("chat_id", "")
        if not platform_name or not chat_id:
            return  # CLI / TUI-owned loop — their own schedulers drive it.
        profile = route.get("profile") or profile
        # The loop's OWN profile's adapter map, fail closed: ``self.adapters`` is the default profile's,
        # so a secondary session's wakeup would inject via the default bot on a bare chat_id (a
        # Telegram DM lands in the user's chat with the other bot).
        adapters = self._adapters_for_profile(profile)
        adapter = next((a for p, a in adapters.items() if p.value == platform_name), None)
        if adapter is None:
            if sid not in warned_no_route:
                warned_no_route.add(sid)
                logger.debug(
                    "loop wakeup: no adapter for platform %r (session %s, profile %s)", platform_name, sid, profile,
                )
            return

        source = self._build_process_event_source({
            "session_key": "",
            "platform": platform_name,
            "chat_id": chat_id,
            **{k: route.get(k, "") for k in ("chat_type", "thread_id", "user_id", "user_name")},
        })
        if source is None:
            return
        if profile and not getattr(source, "profile", None):
            source.profile = profile  # session key + runtime scope of the injected turn
        session_key = None
        with suppress(Exception):
            session_key = self._session_key_for_source(source)
        if session_key and session_key in self._running_agents:
            return  # busy — stays due, next scan retries
        if goal_blocks_loop_tick(sid):
            return

        mgr = LoopManager(session_id=sid)
        if not mgr.is_due(now):
            return
        # fire_tick()/complete_tick() are writes (BEGIN IMMEDIATE) taking the SessionDB writer lock; a slow
        # writer elsewhere holding it while the loop thread blocked froze the gateway until the watchdog
        # fired. The context-preserving executor keeps the profile HERMES_HOME override under multiplex.
        wakeup = await self._run_in_executor_with_context(mgr.fire_tick)
        if not wakeup:
            return
        # #85957: after the parent turn's event.complete the CLIENT owns the next turn on this stateless
        # surface. Persist the completion as a durable delivery row — never self-post it as a new role=user
        # prompt.
        # #85957: same client-owns-the-turn rule as the raw-key branch above — persist the completion as a
        # delivery row, never self-post it as a new role=user prompt.
        try:
            logger.info(
                "loop wakeup #%s — injecting for %s chat=%s thread=%s",
                mgr.state.ticks_fired if mgr.state else "?",
                platform_name, source.chat_id, source.thread_id,
            )
            await adapter.handle_message(self._synthetic_prompt_event(source, wakeup, internal=True))
            # Slash-command loops dispatch through the command path and never hit the post-turn
            # completion hook — complete the tick immediately (caps + scheduling).
            if wakeup.lstrip().startswith("/"):
                await self._run_in_executor_with_context(mgr.complete_tick, "")
        except Exception as exc:
            logger.warning("loop wakeup injection failed for %s: %s", sid, exc)
            with suppress(Exception):
                mgr.abandon_tick()

    async def _loop_wakeup_watcher(self, interval: float = 15.0) -> None:
        """Fire due /loop wakeups for idle gateway sessions.

        The gateway has no per-session scheduler thread, so a coarse ticker
        scans persisted loops (SessionDB ``loop:*`` rows) and injects the
        wakeup prompt into each due session's chat via the same synthetic-
        message path used by watch notifications. Deferrals:

        - session currently running an agent turn → skip (stays due; the
          adapter FIFO would race the live turn otherwise)
        - active non-parked /goal on the session → skip (goal owns the
          idle boundary)
        - no routing metadata on the loop → skip with a one-time warning
          (CLI/TUI loops carry no route and are driven by their own surfaces)
        """
        await asyncio.sleep(5)  # let platforms finish connecting
        warned_no_route: set = set()
        while self._running:
            try:
                from hermes_cli.loops import (
                    LoopManager,
                    goal_blocks_loop_tick,
                    list_active_loops,
                )

                # Warm the cache off-loop once per scan. The scan reads
                # every persisted loop, so a cold cache runs the state.db
                # init on the loop thread before the first read.
                await self._warm_goals_session_db("loop wakeup")

                # Every SessionDB call in this scan runs off the loop thread.
                # fire_tick()/complete_tick() are writes (BEGIN IMMEDIATE) that
                # take the writer lock; a slow writer elsewhere (FTS merge, WAL
                # checkpoint, a long flush) holding it while the watcher blocked
                # the loop on the same lock froze the gateway for 90+ s until
                # the liveness watchdog force-exited. list_active_loops() reads
                # via _read_ctx (lock-free under WAL) but still convoys on the
                # writer lock when WAL is unavailable, so it goes off-loop too.
                # _run_in_executor_with_context keeps the profile HERMES_HOME
                # override alive under multiplex, like the warm-up above.
                active_loops = await self._run_in_executor_with_context(list_active_loops)

                now = time.time()
                for sid, state in active_loops:
                    if state.awaiting_response or now < state.next_due_at:
                        continue
                    route = state.route or {}
                    platform_name = route.get("platform", "")
                    chat_id = route.get("chat_id", "")
                    if not platform_name or not chat_id:
                        # CLI / TUI-owned loop — their own schedulers drive it.
                        continue
                    adapter = None
                    for p, a in self.adapters.items():
                        if p.value == platform_name:
                            adapter = a
                            break
                    if adapter is None:
                        if sid not in warned_no_route:
                            warned_no_route.add(sid)
                            logger.debug(
                                "loop wakeup: no adapter for platform %r (session %s)",
                                platform_name, sid,
                            )
                        continue

                    # Build the source + session key to check business.
                    evt_stub = {
                        "session_key": "",
                        "platform": platform_name,
                        "chat_id": chat_id,
                        "chat_type": route.get("chat_type", ""),
                        "thread_id": route.get("thread_id", ""),
                        "user_id": route.get("user_id", ""),
                        "user_name": route.get("user_name", ""),
                    }
                    source = self._build_process_event_source(evt_stub)
                    if source is None:
                        continue
                    try:
                        session_key = self._session_key_for_source(source)
                    except Exception:
                        session_key = None
                    if session_key and session_key in self._running_agents:
                        continue  # busy — stays due, next scan retries
                    if goal_blocks_loop_tick(sid):
                        continue

                    mgr = LoopManager(session_id=sid)
                    if not mgr.is_due(now):
                        continue
                    wakeup = await self._run_in_executor_with_context(mgr.fire_tick)
                    if not wakeup:
                        continue
                    try:
                        # Fall back to the session's remembered last real user
                        # message id so the wakeup's reply still carries a
                        # reply anchor (and pings) on Discord/Telegram.
                        _anchor = None
                        if session_key:
                            try:
                                _stored = await self.async_session_store.get_session_metadata(
                                    session_key, "_last_user_message_id", None
                                )
                                if _stored:
                                    _anchor = str(_stored)
                            except Exception:
                                logger.debug(
                                    "loop wakeup anchor lookup failed for %s",
                                    session_key, exc_info=True,
                                )
                        synth_event = MessageEvent(
                            text=wakeup,
                            message_type=MessageType.TEXT,
                            source=source,
                            internal=True,
                            message_id=_anchor,
                        )
                        logger.info(
                            "loop wakeup #%s — injecting for %s chat=%s thread=%s",
                            mgr.state.ticks_fired if mgr.state else "?",
                            platform_name, source.chat_id, source.thread_id,
                        )
                        await adapter.handle_message(synth_event)
                        # Slash-command loops dispatch through the command
                        # path and never hit the post-turn completion hook —
                        # complete the tick immediately (caps + scheduling).
                        if wakeup.lstrip().startswith("/"):
                            await self._run_in_executor_with_context(mgr.complete_tick, "")
                    except Exception as exc:
                        logger.warning("loop wakeup injection failed for %s: %s", sid, exc)
                        try:
                            mgr.abandon_tick()
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("loop wakeup watcher error: %s", exc)
            await asyncio.sleep(interval)
