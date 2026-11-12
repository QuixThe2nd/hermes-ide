"""Text-response stop gates for the conversation turn loop.

When the model stops with a text answer, three gates may instead continue the
turn. The restart-echo gate runs first and DROPS the answer: a model quoting
the restart tool result (the rich "Restarted successfully" notice) is echoing
context, not replying, so the candidate must not be emitted, persisted, or
kept as a fallback — one synthetic user nudge re-enters the loop instead.
The other two append the answer as an interim row plus a synthetic user-role
nudge: verify-on-stop (#65919) and the ``pre_verify`` plugin hook after code
edits. Each clears ``final_response`` so the finalizer can tell a gate from
error exits (#61631); the interim-answer gates keep the candidate as a
budget-exhaustion fallback (``pending_verification_response``). Nothing here
imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message
from agent.turn_control import (
    RESTART_NOTICE_FIRST_LINE,
    RESTART_NOTICE_NEW_PROCESS_PREFIX,
    RESTART_NOTICE_OLD_PROCESS_PREFIX,
    parse_restart_notice_lines,
)

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class StopGateVerdict:
    """``continue_turn`` True → a nudge was appended; re-enter the turn loop with
    ``final_response=None`` and the pending-verification fields updated."""

    continue_turn: bool
    final_response: Any
    pending_verification_response: Any
    pending_verification_response_previewed: Any


# A model that ends the turn by quoting the restart tool result is echoing
# context, not answering (PR #667 follow-up: the notice reached a Discord
# user verbatim as the turn's final reply). The echo is dropped — never
# emitted, never persisted, never a budget fallback — and one synthetic user
# nudge re-enters the loop after the tool result (tool → user, the legal
# boundary /steer uses). The nudge is itself a user row after the notice, so
# it structurally closes the gate: a second echo is delivered as the answer
# (no loop), and a later restart's fresh notice re-arms it.
_RESTART_ECHO_NUDGE = (
    "[System: the \"Restarted successfully\" report with the process ids is "
    "the restart tool result, not a reply to the user — do not send, repeat, "
    "or quote it. The old process is gone; you are running in the new one. "
    "Continue the interrupted task with your tools — confirm the restart "
    "took effect and finish the remaining work — then answer the user about "
    "that work.]"
)
_RESTART_ECHO_FLAG = "_restart_echo_synthetic"


def _latest_restart_notice(
    messages: List[Dict[str, Any]],
) -> Optional[tuple]:
    """``(index, lines)`` of the newest rich restart-notice tool result."""
    for index in range(len(messages) - 1, -1, -1):
        row = messages[index]
        if not (isinstance(row, dict) and row.get("role") == "tool"):
            continue
        lines = parse_restart_notice_lines(row.get("content"))
        if lines is not None:
            return index, lines
    return None


def _restart_notice_window_open(messages: List[Dict[str, Any]], notice_index: int) -> bool:
    """True while the restart turn is still open: no user row follows the
    notice (the echo nudge itself, a raced-in user message, or a later turn
    closes the window)."""
    return not any(
        isinstance(row, dict) and row.get("role") == "user"
        for row in messages[notice_index + 1:]
    )


def _is_restart_notice_echo(text: Any, notice_lines: List[str]) -> bool:
    """True when *text* quotes the notice itself: exact copy, or a subset —
    it opens with the notice's first line, every line is one of the notice's
    lines, and the identity (Old/New process) lines are present. A reply
    about different pids, or notice lines plus real work, is content."""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    reply_lines = [line.strip() for line in stripped.split("\n") if line.strip()]
    if reply_lines == notice_lines:
        return True
    if not stripped.startswith(RESTART_NOTICE_FIRST_LINE):
        return False
    if not all(line in notice_lines for line in reply_lines):
        return False
    identity = [
        line for line in notice_lines
        if line.startswith((
            RESTART_NOTICE_OLD_PROCESS_PREFIX,
            RESTART_NOTICE_NEW_PROCESS_PREFIX,
        ))
    ]
    return all(line in reply_lines for line in identity)


def _gate_restart_echo(agent, final_response, messages) -> Optional[StopGateVerdict]:
    """Drop an echo of the restart notice and continue the interrupted turn."""
    notice = _latest_restart_notice(messages)
    if notice is None:
        return None
    notice_index, notice_lines = notice
    if not _restart_notice_window_open(messages, notice_index):
        return None
    if not _is_restart_notice_echo(final_response, notice_lines):
        return None
    append_message(messages, {
        "role": "user", "content": _RESTART_ECHO_NUDGE, _RESTART_ECHO_FLAG: True,
    })
    agent._session_messages = messages
    logger.debug("restart-result echo gated; continuation nudge issued")
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
    """Run the restart-echo gate first (it drops the candidate entirely),
    then verify-on-stop, then the pre_verify hook. Nudges are user-role rows
    appended only after a legal boundary (tool result for the echo gate,
    assistant answer row for the others), so role alternation holds. Hook
    lookups are imported lazily from their origin modules (tests patch them
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

    _echo_verdict = _gate_restart_echo(agent, final_response, messages)
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
