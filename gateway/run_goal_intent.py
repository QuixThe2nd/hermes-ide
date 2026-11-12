"""Off-reply-path gateway scheduling for auto-goal intent (``goals.auto.mode``).

Only this sibling owns message eligibility, profile-scoped task lifetime, the
mode notice, and — for ``mode=on`` — the pending-decision slot consumed at turn
end by ``run_goals._run_post_turn_hooks``. ``run_inbound.py`` has exactly one
hook; the classifier, JSONL audit trail and papercuts live in
``hermes_cli/goal_intent.py``; decision application lives in
``gateway/run_goal_auto.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from gateway.run import GatewayRunner


logger = logging.getLogger("gateway.run")


_ON_MODE_NOTICE = (
    "goals.auto.mode=on: automatic goal creation is active — task-like messages can "
    "start or extend a goal at the end of the turn (say \"stop\" or \"just answer\" "
    "to keep a turn goal-free)."
)


class GatewayGoalIntentMixin:
    """Classify eligible human messages off-path; park on-mode decisions for turn end."""

    def _goal_intent_mode(self: "GatewayRunner") -> str:
        """Return ``off``, ``shadow`` (default), or ``on``."""
        from gateway.run import _load_gateway_config

        try:
            cfg = _load_gateway_config()
        except Exception:
            cfg = {}
        goals = cfg.get("goals") if isinstance(cfg, dict) else {}
        auto = goals.get("auto") if isinstance(goals, dict) else {}
        raw_mode = auto.get("mode", "shadow") if isinstance(auto, dict) else "shadow"
        # YAML 1.1 parses bare ``on``/``off`` as booleans; retain their explicit meaning.
        if isinstance(raw_mode, bool):
            mode = "on" if raw_mode else "off"
        else:
            mode = str(raw_mode).strip().lower()
        if mode not in {"off", "shadow", "on"}:
            mode = "shadow"
        if mode == "on":
            # The notice is one-time per runner/profile, not one per message.
            notices = getattr(self, "_goal_intent_on_notice_homes", None)
            if notices is None:
                notices = self._goal_intent_on_notice_homes = set()
            home = str(get_hermes_home())
            if home not in notices:
                notices.add(home)
                logger.info("%s", _ON_MODE_NOTICE)
        return mode

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
        the task for the turn-boundary apply (on-mode classification only).
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

    def _schedule_goal_intent_shadow(
        self: "GatewayRunner", event, session_key: str
    ) -> None:
        """Retain a fail-safe background classification for an eligible inbound event."""
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
            mode = self._goal_intent_mode()
            if mode == "off":
                return
            platform = str(getattr(getattr(source, "platform", None), "value", "") or "")

            if mode == "on":
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
                return

            from hermes_cli.goal_intent import record_goal_intent_shadow

            coro = self._run_goal_intent_shadow(
                record_goal_intent_shadow, text, session_key, platform
            )
            self._spawn_goal_intent_task(source, coro, session_key, park=False)
        except Exception as exc:
            if coro is not None:
                coro.close()
            logger.debug("auto-goal intent scheduling failed: %s", exc)

    async def _run_goal_intent_shadow(
        self: "GatewayRunner", record, text: str, session_key: str, platform: str
    ) -> None:
        """Shadow worker boundary: all imports/I/O are fail-safe and never reach the reply path."""
        try:
            await self._run_in_executor_with_context(record, text, session_key, platform)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("auto-goal shadow worker failed: %s", exc)

    async def _run_auto_goal_evaluation(
        self: "GatewayRunner", text: str, source, platform: str
    ) -> Optional[Dict[str, Any]]:
        """On-mode worker: resolve the real session id, classify off-loop, return the decision.

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
        try:
            decision = await self._run_in_executor_with_context(
                evaluate_goal_intent, text, sid, platform
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
