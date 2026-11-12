"""Off-reply-path gateway scheduling for the Phase 1 auto-goal shadow.

Only this sibling owns eligibility, profile-scoped task lifetime, and the mode
notice. ``run_inbound.py`` has one hook; the classifier and JSONL/report logic
stay in ``hermes_cli/goal_intent.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from gateway.run import GatewayRunner


logger = logging.getLogger("gateway.run")


class GatewayGoalIntentMixin:
    """Classify eligible human messages in shadow mode, never inline with the reply."""

    def _goal_intent_mode(self: "GatewayRunner") -> str:
        """Return ``off`` or ``shadow``. ``on`` is explicitly shadow in Phase 1."""
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
                logger.info(
                    "goals.auto.mode=on is not implemented yet; running in shadow mode "
                    "(Phase 1 logs classifications only; no automatic goal creation)."
                )
        return mode

    def _schedule_goal_intent_shadow(
        self: "GatewayRunner", event, session_id: str
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
            # create_task snapshots this profile scope; the executor snapshots it
            # again so config, secrets, goal state and JSONL stay in the source home.
            with self._profile_scope_for_source(source):
                if self._goal_intent_mode() == "off":
                    return
                from hermes_cli.goal_intent import record_goal_intent_shadow

                coro = self._run_goal_intent_shadow(
                    record_goal_intent_shadow,
                    text,
                    session_id,
                    str(getattr(getattr(source, "platform", None), "value", "") or ""),
                )
                try:
                    task = asyncio.create_task(coro)
                except Exception:
                    coro.close()
                    coro = None
                    raise
                coro = None  # ownership transferred to the scheduled task
                self._retain_background_task(task)
                from agent.async_utils import consume_detached_task_result

                task.add_done_callback(consume_detached_task_result)
        except Exception as exc:
            if coro is not None:
                coro.close()
            logger.debug("auto-goal shadow scheduling failed: %s", exc)

    async def _run_goal_intent_shadow(
        self: "GatewayRunner", record, text: str, session_id: str, platform: str
    ) -> None:
        """Worker boundary: all imports/I/O are fail-safe and never reach the reply path."""
        try:
            await self._run_in_executor_with_context(record, text, session_id, platform)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("auto-goal shadow worker failed: %s", exc)
