"""Off-reply-path gateway scheduling for auto-goal intent.

Only this sibling owns message eligibility, profile-scoped task lifetime, and
the pending-decision slot consumed at turn end by
``run_goals._run_post_turn_hooks``. ``run_inbound.py`` has exactly one
hook; the classifier, JSONL audit trail and papercuts live in
``hermes_cli/goal_intent.py``; decision application lives in
``gateway/run_goal_auto.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from gateway.run import GatewayRunner


logger = logging.getLogger("gateway.run")


class GatewayGoalIntentMixin:
    """Classify eligible human messages off-path; park decisions for turn end."""

    def _auto_goal_pending_slot(self: "GatewayRunner") -> Dict[str, asyncio.Task]:
        pending = getattr(self, "_auto_goal_pending", None)
        if pending is None:
            pending = self._auto_goal_pending = {}
        return pending

    def _spawn_goal_intent_task(
        self: "GatewayRunner", source, coro, session_key: str, *, park: bool
    ) -> None:
        """Create the off-path task inside this source's profile scope.

        ``create_task`` snapshots the contextvars here, so the worker (and its
        executor hops) run under the right profile's secret scope even when
        scheduled from a multiplexed inbound path. ``park`` additionally slots
        the task for the turn-boundary apply.
        """
        try:
            with self._profile_scope_for_source(source):
                task = asyncio.create_task(coro)
            self._retain_background_task(task)
            from agent.async_utils import consume_detached_task_result

            task.add_done_callback(consume_detached_task_result)
            if park:
                self._auto_goal_pending_slot()[session_key] = task
        except Exception:
            coro.close()
            raise

    def _schedule_auto_goal_intent(
        self: "GatewayRunner", event, session_key: str
    ) -> None:
        """Classify an eligible inbound message off-path; park the decision."""
        coro = None
        try:
            source = event.source
            text = str(event.text or "")
            if (
                bool(getattr(event, "internal", False))
                or getattr(source, "is_bot", False)
                or not text.strip()
                or text.lstrip().startswith("/")
            ):
                return
            platform = str(getattr(getattr(source, "platform", None), "value", "") or "")

            from hermes_cli.goal_intent import auto_goal_suppression

            suppression = auto_goal_suppression(text)
            if suppression == "stop":
                # Pause before the turn runs; the judge must not continue
                # past a stop turn. No classification, no pending decision.
                coro = self._pause_goal_for_stop(source)
                self._spawn_goal_intent_task(source, coro, session_key, park=False)
                return
            if suppression is not None:
                return  # "just answer": no Jev call, no pending decision
            coro = self._run_auto_goal_evaluation(text, source, platform)
            self._spawn_goal_intent_task(source, coro, session_key, park=True)
        except Exception as exc:
            if coro is not None:
                coro.close()
            logger.debug("auto-goal intent scheduling failed: %s", exc)

    async def _run_auto_goal_evaluation(
        self: "GatewayRunner", text: str, source, platform: str
    ) -> Optional[Dict[str, Any]]:
        """Worker: resolve the real session id, classify off-loop, return the decision.

        The decision task is parked by the caller; awaiting it at the turn
        boundary yields this dict. Goal state is keyed by the session id (the
        scheduling hook only sees the quick key), so the real id is resolved
        here and carried inside the decision.
        """
        from hermes_cli.goal_intent import evaluate_goal_intent

        sid = ""
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=False
            )
            sid = str(getattr(session_entry, "session_id", "") or "")
        except Exception as exc:
            logger.debug("auto-goal session resolution failed: %s", exc)
        loop = asyncio.get_running_loop()
        on_attempt = self._jev_attempt_notifier(source, loop)
        try:
            decision = await self._run_in_executor_with_context(
                evaluate_goal_intent, text, sid, platform, on_attempt
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("auto-goal evaluation failed: %s", exc)
            return None
        if isinstance(decision, dict):
            decision.setdefault("session_id", sid)
            decision.setdefault("message", text)
            return decision
        return None

    def _jev_attempt_notifier(self: "GatewayRunner", source, loop: asyncio.AbstractEventLoop):
        """Per-attempt Jev notice callback for one inbound message's classification.

        Runs on the executor thread (inside the source's profile scope, carried
        by ``_run_in_executor_with_context``); each real HTTP attempt posts one
        concise line back to the originating chat through the same deferred
        status-notice lane the 🎯 line uses — outbound-only, never model
        context, and a delivery failure is logged without touching the
        classification or the turn.
        """
        author = str(getattr(source, "user_name", "") or "")

        def on_attempt(attempt: int, attempts: int, label, confidence, error) -> None:
            from gateway.jev_notice import format_notice, goal_intent_attempt_record

            line = format_notice(
                goal_intent_attempt_record(
                    attempt=attempt, attempts=attempts, label=label,
                    confidence=confidence, error=error, author=author,
                )
            )
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._defer_goal_status_notice_after_delivery(source, line), loop
                )

                def _drain(fut) -> None:
                    exc = fut.exception()
                    if exc is not None:
                        logger.debug("jev notice delivery failed: %s", exc)

                future.add_done_callback(_drain)
            except Exception as exc:
                logger.debug("jev notice delivery scheduling failed: %s", exc)

        return on_attempt
