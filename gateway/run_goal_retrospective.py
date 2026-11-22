"""Post-turn retrospective intent classification and conservative recovery."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from gateway.run import GatewayRunner

logger = logging.getLogger("gateway.run")


class GatewayGoalRetrospectiveMixin:
    """Reclassify completed exchanges without replacing handled decisions."""

    def _retrospective_goals_enabled(self: "GatewayRunner", source) -> bool:
        """Read both gates from the profile owning this source."""
        if not self._auto_goals_enabled(source):
            return False
        from gateway.run import _load_gateway_config
        from hermes_cli.config import cfg_get
        from utils import is_truthy_value

        home = Path(self._resolve_profile_home_for_source(source))
        enabled = cfg_get(
            _load_gateway_config(home / "config.yaml"),
            "goals",
            "auto",
            "retrospective",
            "enabled",
        )
        return is_truthy_value(enabled, default=True)

    async def _retrospective_auto_goal_decision(
        self: "GatewayRunner",
        *,
        event: Any,
        source: Any,
        session_entry: Any,
        final_response: str,
        initial_decision: Optional[Dict[str, Any]],
        is_internal: bool,
    ) -> Optional[Dict[str, Any]]:
        """Return a recovered task decision, or the original decision unchanged.

        Every eligible completed exchange gets a verdict, audit row, and visible
        status. Recovery is limited to a high-confidence task after chat/unknown
        with no pre-existing goal; the normal continuation path still owns goal
        creation, judging, notices, and enqueueing.
        """
        if event is None or source is None or not final_response.strip():
            return initial_decision
        message = str(getattr(event, "text", "") or "")
        if (
            is_internal
            or bool(getattr(event, "internal", False))
            or bool(getattr(source, "is_bot", False))
            or not message.strip()
            or message.lstrip().startswith("/")
            or not self._retrospective_goals_enabled(source)
        ):
            return initial_decision
        from hermes_cli.goal_intent import auto_goal_suppression

        if auto_goal_suppression(message) is not None:
            return initial_decision

        initial = initial_decision if isinstance(initial_decision, dict) else {}
        initial_label = str(initial.get("label") or "unknown").strip().lower()
        if initial_label not in {"chat", "task", "approval", "chase", "unknown"}:
            initial_label = "unknown"
        from gateway.run_goal_auto import _confidence_of

        initial_confidence = _confidence_of(initial)
        sid = str(
            getattr(session_entry, "session_id", "") or initial.get("session_id") or ""
        )
        if not sid:
            return initial_decision

        try:
            from gateway.jev_retrospective import classify_retrospective

            result = await self._run_in_executor_with_context(
                lambda: classify_retrospective(
                    message,
                    final_response,
                    initial_label=initial_label,
                    initial_confidence=initial_confidence,
                )
            )
            if not isinstance(result, dict):
                return initial_decision
            await self._record_retrospective_verdict(
                result=result,
                message=message,
                session_id=sid,
                source=source,
                initial_label=initial_label,
                initial_confidence=initial_confidence,
            )
            notice = result.get("notice")
            if isinstance(notice, str) and notice:
                await self._defer_goal_status_notice_after_delivery(
                    source, notice, non_conversational=True
                )
        except Exception as exc:
            logger.debug("retrospective goal intent failed: %s", type(exc).__name__)
            return initial_decision

        # Do not trust a bridge result to replace an arrival-time decision that
        # was already handled, even if the process boundary is corrupted.
        if (
            result.get("recover") is not True
            or initial_label not in {"chat", "unknown"}
            or result.get("label") != "task"
        ):
            return initial_decision

        try:
            from hermes_cli.goals import GoalManager

            existing = await self._run_in_executor_with_context(
                lambda: GoalManager(session_id=sid).state
            )
            if existing is not None:
                logger.info(
                    "retrospective goal recovery suppressed: session %s already has a goal",
                    sid,
                )
                return initial_decision
        except Exception as exc:
            logger.debug(
                "retrospective goal state check failed: %s", type(exc).__name__
            )
            return initial_decision

        logger.info(
            "retrospective goal intent recovered a missed task for session %s", sid
        )
        return {
            "label": "task",
            "confidence": float(result.get("confidence") or 0.0),
            "reason": "jev retrospective recovery",
            "contract_draft": None,
            "message": message,
            "session_id": sid,
            "last_assistant": final_response,
            "user_previous_message": str(initial.get("user_previous_message") or ""),
        }

    async def _record_retrospective_verdict(
        self: "GatewayRunner",
        *,
        result: Dict[str, Any],
        message: str,
        session_id: str,
        source: Any,
        initial_label: str,
        initial_confidence: float,
    ) -> None:
        from hermes_cli.goal_intent import EXCERPT_LIMIT, _append_shadow_record

        platform = getattr(getattr(source, "platform", None), "value", "")
        error = str(result.get("error") or "")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "retrospective",
            "session_id": session_id,
            "platform": str(platform or ""),
            "message_excerpt": message[:EXCERPT_LIMIT],
            "initial_label": initial_label,
            "initial_confidence": initial_confidence,
            "label": str(result.get("label") or "unknown"),
            "confidence": float(result.get("confidence") or 0.0),
            "comparison": str(result.get("comparison") or "unavailable"),
            "recovery_requested": result.get("recover") is True,
            "reason": error or "jev retrospective decision",
        }
        try:
            await self._run_in_executor_with_context(
                lambda: _append_shadow_record(record)
            )
        except Exception as exc:
            logger.debug(
                "retrospective goal audit write failed: %s", type(exc).__name__
            )
