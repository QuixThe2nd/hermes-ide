"""Text-response stop gates for the conversation turn loop.

When the model stops with a text answer, two gates may instead append the answer as an
interim row plus a synthetic user-role nudge and continue the turn: verify-on-stop (#65919)
and the ``pre_verify`` plugin hook after code edits.
Each keeps the candidate answer as a budget-exhaustion fallback
(``pending_verification_response``) and clears ``final_response`` so the finalizer can tell
this gate from error exits (#61631). Nothing here imports ``agent.conversation_loop`` at
module level (cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class StopGateVerdict:
    """``continue_turn`` True → a nudge was appended; re-enter the turn loop with
    ``final_response=None`` and the pending-verification fields updated."""

    continue_turn: bool
    final_response: Any
    pending_verification_response: Any
    pending_verification_response_previewed: Any


# First lines of every restart-notice last-line wording ever shipped: the
# original builder text ("This chat is no longer running in ..." / "You are
# now running in ...") and the do-not-send instruction line. A text stop
# made of the identity lines plus at most ONE of these is an echo of the
# tool result, not an answer.
_RESTART_NOTICE_TRAILING_PREFIXES = (
    "This chat is no longer running",
    "You are now running in",
    "This is a tool result for you",
)


def _is_restart_notice_echo(text: Any) -> bool:
    """True when *text* is (a quote of) the restart tool result, not an answer.

    Shape, not bytes: the identity block (``Restarted successfully`` +
    ``Old process:``/``New process:`` lines) with at most one trailing
    instruction line in a known wording. Anything beyond that is task
    content, so a genuine final that mentions the pids passes through.
    """
    if not isinstance(text, str):
        return False
    lines = [line.strip() for line in text.strip().split("\n")]
    if not lines or not lines[0].startswith("Restarted successfully"):
        return False
    index = 1
    if index < len(lines) and lines[index].startswith("Old process: "):
        index += 1
    if index >= len(lines) or not lines[index].startswith("New process: "):
        return False
    trailing = [line for line in lines[index + 1:] if line]
    if not trailing:
        return True
    return len(trailing) == 1 and trailing[0].startswith(
        _RESTART_NOTICE_TRAILING_PREFIXES
    )


def _history_has_restart_notice(messages: List[Dict[str, Any]]) -> bool:
    """The transcript must actually carry the restart tool result this echo quotes."""
    for row in messages:
        if isinstance(row, dict) and row.get("role") == "tool":
            content = row.get("content")
            if (
                isinstance(content, str)
                and "Restarted successfully" in content
                and "New process: " in content
            ):
                return True
    return False


def _restart_echo_nudge() -> str:
    return (
        "[System: that text was the restart tool result, not a reply to "
        "send to the user. Do not repeat or quote it. Continue the "
        "interrupted task with your tools, and answer the user only about "
        "that work.]"
    )


def _gate_restart_echo(agent, final_msg, final_response, messages) -> Optional[StopGateVerdict]:
    """Drop a verbatim echo of the restart notice and continue the turn once.

    The echo is context, not an answer: it is not emitted, not persisted as
    the reply, and not kept as the budget-exhaustion fallback. One nudge
    only — a second echo is delivered as the answer so the turn cannot loop.
    """
    if getattr(agent, "_restart_echo_nudges", 0) > 0:
        return None
    if any(
        isinstance(row, dict) and row.get("_restart_echo_synthetic")
        for row in messages
    ):
        return None
    if not _is_restart_notice_echo(final_response):
        return None
    if not _history_has_restart_notice(messages):
        return None

    agent._restart_echo_nudges = 1
    if not (messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "tool"):
        # user-tail: keep role alternation by persisting the echo as an
        # assistant row first — WITHOUT emitting it to the user.
        append_message(messages, final_msg)
    append_message(
        messages,
        {"role": "user", "content": _restart_echo_nudge(), "_restart_echo_synthetic": True},
    )
    agent._session_messages = messages
    logger.debug("restart-notice echo dropped; continuation nudge issued")
    return StopGateVerdict(
        continue_turn=True, final_response=None,
        pending_verification_response=None,
        pending_verification_response_previewed=False,
    )


def _verify_on_stop_nudge(agent) -> Optional[str]:
    try:
        from agent.verification_stop import (
            build_verify_on_stop_nudge, verify_on_stop_enabled
        )

        if verify_on_stop_enabled():
            return build_verify_on_stop_nudge(
                session_id=getattr(agent, "session_id", None),
                changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
                attempts=getattr(agent, "_verification_stop_nudges", 0),
            )
    except Exception:
        logger.debug("verification stop-loop check failed", exc_info=True)
    return None


def _pre_verify_nudge(agent, final_response, attempt: int) -> Optional[str]:
    """After code edits a registered ``pre_verify`` hook may keep the agent going one
    more turn; no default continuation cost."""
    _edited = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
    try:
        from agent.verify_hooks import max_verify_nudges
        from hermes_cli.lifecycle import has_hook
        from hermes_cli.plugins import get_pre_verify_continue_message

        if _edited and has_hook("pre_verify") and attempt < max_verify_nudges():
            # Posture is fixed for the session — resolve once + cache.
            coding = getattr(agent, "_resolved_is_coding", None)
            if coding is None:
                from agent.coding_context import is_coding_context
                coding = bool(is_coding_context(platform=getattr(agent, "platform", "") or ""))
                agent._resolved_is_coding = coding
            return get_pre_verify_continue_message(
                session_id=getattr(agent, "session_id", None) or "",
                platform=getattr(agent, "platform", "") or "",
                model=getattr(agent, "model", "") or "", coding=coding, attempt=attempt,
                final_response=final_response, changed_paths=_edited,
            )
    except Exception:
        logger.debug("pre_verify hook check failed", exc_info=True)
    return None


def _append_interim_answer(agent, final_msg, messages, conversation_history, flush_fail_msg: str) -> None:
    """Real content: persist and emit as interim so the user sees the attempted answer;
    only the nudge is flagged synthetic (#65919)."""
    agent._emit_interim_assistant_message(final_msg)
    append_message(messages, final_msg)
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.debug(flush_fail_msg, exc_info=True)


def apply_stop_gates(
    agent: Any, final_msg: Dict[str, Any], *, final_response: Any, messages: List[Dict[str, Any]],
    conversation_history: Any, pending_verification_response: Any,
    pending_verification_response_previewed: Any,
) -> StopGateVerdict:
    """Run verify-on-stop, then the pre_verify hook. Nudges are user-role rows
    appended only after the assistant answer row, so role alternation holds.
    Hook lookups are imported lazily from their origin modules (tests patch them
    there)."""

    def _continue(nudge: str, flag: str) -> StopGateVerdict:
        """Append the synthetic nudge row and hand the turn back to the loop."""
        append_message(messages, {"role": "user", "content": nudge, flag: True})
        agent._session_messages = messages
        # Keep the answer only as a budget-exhaustion fallback; clear ``final_response`` so
        # the finalizer can tell this gate from error exits. Mark previewed only if the
        # candidate is reused (#61631).
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    _echo_verdict = _gate_restart_echo(agent, final_msg, final_response, messages)
    if _echo_verdict is not None:
        return _echo_verdict

    _verify_nudge = _verify_on_stop_nudge(agent)
    if _verify_nudge:
        agent._verification_stop_nudges = getattr(agent, "_verification_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "verify-on-stop interim flush failed"
        )
        verdict = _continue(_verify_nudge, "_verification_stop_synthetic")
        # Internal nudge: stay silent on the terminal, debug-log only.
        logger.debug("verification stop-loop nudge issued (attempt %d)", agent._verification_stop_nudges)
        return verdict

    _attempt = getattr(agent, "_pre_verify_nudges", 0)
    _verify_nudge2 = _pre_verify_nudge(agent, final_response, _attempt)
    if _verify_nudge2:
        agent._pre_verify_nudges = _attempt + 1
        final_msg["finish_reason"] = "verify_hook_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "pre_verify interim flush failed"
        )
        verdict = _continue(_verify_nudge2, "_pre_verify_synthetic")
        logger.debug("pre_verify nudge issued (attempt %d)", agent._pre_verify_nudges)
        return verdict

    return StopGateVerdict(
        continue_turn=False, final_response=final_response,
        pending_verification_response=pending_verification_response,
        pending_verification_response_previewed=pending_verification_response_previewed,
    )
