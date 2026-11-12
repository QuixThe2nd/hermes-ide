"""Auto-goal (``goals.auto.mode=on``): apply a Jev decision at the turn boundary.

The decision is classified off-path at message arrival
(``gateway/run_goal_intent.py``) and applied here, at turn end, BEFORE the goal
judge runs — a freshly created goal is judged against the finished turn and
continues only through the existing continuation path in ``run_goals.py``.
The 🎯 line is an outbound chat message (the same deferred status-notice lane
the judge verdict uses), never model context.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

#: Bounded wait for an in-flight classification at the turn boundary. Two Jev
#: attempts are 2x8s; the classification started at message arrival, so this
#: only covers the tail of a slow decision.
_AUTO_GOAL_DECISION_WAIT_S = 20.0

_DM_CHAT_TYPES = {"dm", "private"}


def _confidence_of(decision: Dict[str, Any]) -> float:
    try:
        value = float(decision.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value


class GatewayGoalAutoMixin:
    """Turn-boundary application of Jev auto-goal decisions for GatewayRunner."""

    # ── pending decision slot ────────────────────────────────────────────
    # The store itself (``_auto_goal_pending``) is owned and lazily created by
    # GatewayGoalIntentMixin, which parks decisions at message arrival.

    async def _take_pending_auto_goal_decision(self: "GatewayRunner", source) -> Optional[Dict[str, Any]]:
        """Pop this session's pending classification and bounded-await it.

        Returns None when nothing is pending (shadow mode, internal turns,
        suppressed messages) or the decision did not land in time — never
        raises, never defers a stale decision to a later turn.
        """
        key = None
        if source is not None:
            try:
                key = self._session_key_for_source(source)
            except Exception:
                key = None
        task = (getattr(self, "_auto_goal_pending", None) or {}).pop(key, None) if key else None
        if task is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=_AUTO_GOAL_DECISION_WAIT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("auto-goal decision unavailable at turn end: %s", exc)
            return None

    # ── decision application ─────────────────────────────────────────────

    async def _apply_auto_goal_decision(
        self: "GatewayRunner",
        decision: Dict[str, Any],
        session_entry: Any,
        source: Any,
    ) -> Optional[str]:
        """Apply one decision at turn end. Returns the 🎯 notice line, or None.

        Never raises. Mutations run off the event loop with the turn's
        ContextVars (profile secret scope) preserved.
        """
        from hermes_cli.goal_intent import AUTO_GOAL_CONFIDENCE_MIN, CLASSIFICATION_LABELS

        try:
            label = str(decision.get("label") or "unknown")
            confidence = _confidence_of(decision)
            # chat / unknown / low confidence: nothing to do — the raw label and
            # confidence are already in the shadow JSONL.
            if label not in CLASSIFICATION_LABELS or confidence < AUTO_GOAL_CONFIDENCE_MIN:
                return None
            message = str(decision.get("message") or "").strip()
            sid = str(decision.get("session_id") or getattr(session_entry, "session_id", "") or "")
            if not message or not sid:
                return None
            if not self._auto_goal_sender_allowed(source, session_entry):
                logger.info(
                    "auto-goal: sender %s is not the session owner or an operator; goal not started/reopened",
                    getattr(source, "user_id", None),
                )
                return None

            if label == "task":
                return await self._run_in_executor_with_context(
                    self._auto_goal_start_or_append, sid, message, decision
                )
            if label == "approval":
                return await self._run_in_executor_with_context(
                    self._auto_goal_approval, sid, message, decision
                )
            if label == "chase":
                return await self._run_in_executor_with_context(
                    self._auto_goal_chase, sid, message, decision
                )
            return None  # chat (and anything else): never acted on
        except Exception as exc:
            logger.debug("auto-goal decision application failed: %s", exc)
            return None

    def _auto_goal_sender_allowed(self: "GatewayRunner", source: Any, session_entry: Any) -> bool:
        """Group threads: only the session owner (the first human, i.e. the session's
        recorded origin) or an explicitly-recognised operator may start or reopen
        a goal. DMs are unaffected — admission already authorized the sender."""
        chat_type = str(getattr(source, "chat_type", "") or "").lower()
        if chat_type in _DM_CHAT_TYPES:
            return True
        try:
            if self._resume_caller_is_admin(source):
                return True
        except Exception:
            pass
        origin_user = getattr(getattr(session_entry, "origin", None), "user_id", None)
        sender = getattr(source, "user_id", None)
        return bool(origin_user and sender and str(sender) == str(origin_user))

    # ── label arms (sync; run in the executor) ───────────────────────────

    def _goal_max_turns(self: "GatewayRunner") -> int:
        try:
            return int(self._goal_max_turns_from_config())
        except Exception:
            from hermes_cli.goals import DEFAULT_MAX_TURNS

            return DEFAULT_MAX_TURNS

    def _auto_goal_contract(self, message: str, decision: Dict[str, Any]):
        """Merged completion contract: inline user fields win over the classifier's
        draft; an explicit ``done when:`` line overrides the outcome verbatim."""
        from hermes_cli import goals

        headline, inline = goals.parse_contract(message)
        draft = goals.GoalContract.from_dict(decision.get("contract_draft"))
        merged = goals.GoalContract(
            **{field: (getattr(inline, field) or getattr(draft, field)) for field in goals._CONTRACT_FIELDS}
        )
        override = goals.extract_done_when(message)
        if override:
            merged.outcome = override
        return (headline or message).strip(), merged

    def _auto_goal_set_from_text(self: "GatewayRunner", mgr, goal_text: str, contract) -> str:
        """Set a fresh goal; returns its done-condition (contract outcome, else the goal text)."""
        from hermes_cli import goals

        state = mgr.set(goal_text, contract=contract)
        return (state.contract.outcome.strip() if state.has_contract() else "") or goal_text

    def _auto_goal_start_or_append(
        self: "GatewayRunner", sid: str, message: str, decision: Dict[str, Any]
    ) -> Optional[str]:
        """task + confidence: append to an active goal, else create one (draft contract,
        ``done when:`` override, one 🎯 line)."""
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=sid, default_max_turns=self._goal_max_turns())
        if mgr.is_active():
            mgr.add_subgoal(message)  # never a second goal
            return None
        goal_text, contract = self._auto_goal_contract(message, decision)
        done_condition = self._auto_goal_set_from_text(mgr, goal_text, contract)
        logger.info("auto-goal: created goal for session %s", sid)
        return f"🎯 Done when: {done_condition}"

    def _auto_goal_approval(
        self: "GatewayRunner", sid: str, message: str, decision: Dict[str, Any]
    ) -> Optional[str]:
        """approval + confidence: resume a paused goal (no papercut); otherwise treat as task."""
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=sid, default_max_turns=self._goal_max_turns())
        state = mgr.state
        if state is not None and state.status == "paused":
            mgr.resume()
            logger.info("auto-goal: approval resumed paused goal for session %s", sid)
            return None
        return self._auto_goal_start_or_append(sid, message, decision)

    def _auto_goal_chase(
        self: "GatewayRunner", sid: str, message: str, decision: Dict[str, Any]
    ) -> Optional[str]:
        """chase + confidence: reopen a done/paused goal inside the dedupe window and
        append the not-done note; append to an active goal; else start one from the
        chase text plus the last assistant message. The phase-1 papercut was already
        logged at classification time — none here."""
        from hermes_cli import goals
        from hermes_cli.goal_intent import CHASE_DEDUPE_WINDOW

        mgr = goals.GoalManager(session_id=sid, default_max_turns=self._goal_max_turns())
        state = mgr.state
        note = f"user says not done: {message}"
        if state is not None and state.status in {"done", "paused"}:
            recent = state.last_turn_at > 0 and (
                time.time() - state.last_turn_at
            ) <= CHASE_DEDUPE_WINDOW.total_seconds()
            if recent:
                mgr.resume()
                mgr.add_subgoal(note)
                logger.info("auto-goal: chase reopened goal for session %s", sid)
                return None
        if mgr.is_active():
            mgr.add_subgoal(note)
            return None
        last_assistant = str(decision.get("last_assistant") or "").strip()
        goal_text = (
            f"{message}\n\nAssistant's last reply, for context: {last_assistant}"
            if last_assistant
            else message
        )
        draft = goals.GoalContract.from_dict(decision.get("contract_draft"))
        self._auto_goal_set_from_text(mgr, goal_text, draft)
        logger.info("auto-goal: chase started a goal for session %s", sid)
        return None

    # ── 'stop' pause (message arrival, mode on) ──────────────────────────

    async def _pause_goal_for_stop(self: "GatewayRunner", source) -> None:
        """Pause any active goal for a ``stop`` message. Best-effort, off the reply path."""
        try:
            session_entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=False
            )
        except Exception as exc:
            logger.debug("auto-goal stop: session resolution failed: %s", exc)
            return
        sid = getattr(session_entry, "session_id", None)
        if not sid:
            return

        def _pause():
            from hermes_cli.goals import GoalManager

            return GoalManager(session_id=sid).pause(reason="user said stop")

        try:
            state = await self._run_in_executor_with_context(_pause)
            if state is not None:
                logger.info("auto-goal: paused goal for session %s after 'stop'", sid)
        except Exception as exc:
            logger.debug("auto-goal stop pause failed: %s", exc)
