"""Turn-message preparation and transparent forced-interruption recovery.

The live :class:`gateway.run_turn_runner.TurnRunner` delegates here so the
recovery policy does not inflate its callback/turn facade. Gateway helpers
are imported at call time to retain the ``gateway.run`` monkeypatch seams.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("gateway.run")


@dataclass
class TurnMessagePreparation:
    """Model-facing history and persistence semantics for one prepared turn."""

    agent_history: list
    observed_group_context: Any
    persist_user_message: Any
    persist_user_timestamp: Optional[float]
    history_media_paths: set = field(default_factory=set)
    continue_interrupted_turn: bool = False
    blocked_error: Optional[str] = None


def _rebuild_durable_history(turn, preparation: TurnMessagePreparation) -> None:
    """Rebuild through the normal replay cleanup after changing raw history."""
    from gateway.run import (
        _build_gateway_agent_history,
        _collect_history_media_paths,
        _message_timestamps_enabled,
    )

    ctx = turn._ctx
    (
        preparation.agent_history,
        preparation.observed_group_context,
        turn._cursor_recovery_note,
    ) = _build_gateway_agent_history(
        ctx.history,
        channel_prompt=ctx.channel_prompt,
        inject_timestamps=_message_timestamps_enabled(ctx.user_config),
        hermes_session_id=ctx.session_id,
    )
    preparation.history_media_paths = _collect_history_media_paths(
        preparation.agent_history
    )


def _queue_resume_user_text(turn, text: str) -> None:
    """Preserve raced-in user bytes until the interrupted batch is closed."""
    ctx = turn._ctx
    if not ctx.session_key:
        return
    pending = getattr(turn._runner, "_pending_resume_user_text", None)
    if not isinstance(pending, dict):
        pending = {}
        turn._runner._pending_resume_user_text = pending
    queued = pending.get(ctx.session_key)
    pending[ctx.session_key] = (
        f"{queued}\n\n{text}" if isinstance(queued, str) and queued else text
    )


def _consume_queued_resume_text(turn) -> None:
    ctx = turn._ctx
    pending = getattr(turn._runner, "_pending_resume_user_text", None)
    if not isinstance(pending, dict) or not ctx.session_key:
        return
    queued = pending.pop(ctx.session_key, None)
    if not isinstance(queued, str) or not queued:
        return
    ctx.message = (
        f"{queued}\n\n{ctx.message}"
        if isinstance(ctx.message, str) and ctx.message.strip()
        else queued
    )


def _stand_down_for_claim_loss(
    turn,
    preparation: TurnMessagePreparation,
    claim: str,
) -> None:
    """Block duplicate recovery; queue real text until a legal boundary."""
    from gateway.run import build_victim_replay_plan

    ctx = turn._ctx
    if not (isinstance(ctx.message, str) and ctx.message.strip()):
        logger.warning(
            "Transparent resume for %s: recovery claim=%s; standing down",
            ctx.session_key,
            claim,
        )
        preparation.blocked_error = f"forced_recovery_{claim}"
        return

    reloaded = None
    load_transcript = getattr(turn._runner.session_store, "load_transcript", None)
    if callable(load_transcript):
        try:
            reloaded = load_transcript(ctx.session_id)
        except Exception:
            logger.warning(
                "Transparent resume: transcript reload after losing the "
                "recovery claim raised",
                exc_info=True,
            )
    if isinstance(reloaded, list):
        ctx.history = reloaded
        _rebuild_durable_history(turn, preparation)
        plan = build_victim_replay_plan(ctx.history)
        batch_open = bool(
            plan.identity_malformed
            or plan.has_replay_work
            or (plan.batch_present and plan.fail_closed_calls)
        )
        if not batch_open:
            # The winning worker closed the exact old batch. This real user
            # message may now run verbatim as an ordinary new turn.
            return
        suffix = "batch_open"
    else:
        suffix = "reload_failed"

    _queue_resume_user_text(turn, ctx.message)
    logger.warning(
        "Transparent resume for %s: recovery claim=%s; queuing user text "
        "instead of starting on an unclosed batch",
        ctx.session_key,
        claim,
    )
    ctx.message = None
    preparation.blocked_error = f"forced_recovery_{claim}_{suffix}"


def _repair_restart_tool_tail(history: list) -> Optional[tuple[list, bool]]:
    """Return the exact successful restart result the successor must resume from."""
    from agent.message_sanitization import tool_call_id_variants, tool_result_id_variants
    from agent.turn_control import GATEWAY_RESTART_CONTROL, TURN_CONTROL_FIELD

    if not history:
        return None
    tail_index = len(history) - 1
    close_row = history[tail_index]
    drop_close = bool(
        isinstance(close_row, dict)
        and close_row.get("role") == "assistant"
        and not close_row.get("tool_calls")
        and close_row.get("content")
        == "[Turn ended here: gateway restart was queued by the restart tool]"
    )
    if drop_close:
        tail_index -= 1
    if tail_index < 1 or not isinstance(history[tail_index], dict):
        return None
    if history[tail_index].get("role") != "tool":
        return None

    result_end = tail_index + 1
    assistant_index = tail_index
    while (
        assistant_index >= 0
        and isinstance(history[assistant_index], dict)
        and history[assistant_index].get("role") == "tool"
    ):
        assistant_index -= 1
    if assistant_index < 0 or not isinstance(history[assistant_index], dict):
        return None
    assistant = history[assistant_index]
    if assistant.get("role") != "assistant":
        return None
    tool_calls = assistant.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None

    restart_call = None
    for call in tool_calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            continue
        function = call["function"]
        target = function.get("name")
        if target != "restart":
            continue
        if restart_call is not None:
            return None
        call_id = call.get("id") or call.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        restart_call = call
    if restart_call is None:
        return None

    id_variants = tool_call_id_variants(restart_call)
    matching_results = []
    for index in range(assistant_index + 1, result_end):
        row = history[index]
        if id_variants.isdisjoint(tool_result_id_variants(row.get("tool_call_id"))):
            continue
        content = row.get("content")
        trusted_result = content == "Restarted successfully"
        if not trusted_result:
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                payload = None
            trusted_result = bool(
                isinstance(payload, dict)
                and payload.get("success") is True
                and payload.get(TURN_CONTROL_FIELD) == GATEWAY_RESTART_CONTROL
            )
        if not trusted_result:
            return None
        matching_results.append(index)
    if len(matching_results) != 1:
        return None

    result_index = matching_results[0]
    rewritten = [
        dict(row) if isinstance(row, dict) else row
        for index, row in enumerate(history)
        if not (drop_close and index == len(history) - 1)
    ]
    result = rewritten[result_index]
    changed = bool(
        drop_close
        or result.get("content") != "Restarted successfully"
        or "api_content" in result
    )
    result["content"] = "Restarted successfully"
    result.pop("api_content", None)
    return rewritten, changed


def _continue_restart_tool_turn(turn, preparation: TurnMessagePreparation) -> bool:
    """Repair and continue a restart call without replaying any tool."""
    ctx = turn._ctx
    repair = _repair_restart_tool_tail(ctx.history)
    if repair is None:
        return False

    from gateway.run import plan_forced_resume_turn

    resume_turn = plan_forced_resume_turn(ctx.message)
    ctx.message = resume_turn.message
    preparation.persist_user_message = resume_turn.persist_user_message
    preparation.continue_interrupted_turn = resume_turn.continue_interrupted_turn
    rewritten, changed = repair
    if changed:
        persisted = turn._runner.session_store.rewrite_transcript(
            ctx.session_id, rewritten, active_only=True
        )
        if not persisted:
            logger.error(
                "Restart continuation for %s could not persist its repaired tool result",
                ctx.session_id,
            )
            ctx.message = None
            preparation.persist_user_message = None
            preparation.blocked_error = "restart_tool_continuation_rewrite_failed"
            return True
    ctx.history = rewritten
    _rebuild_durable_history(turn, preparation)
    return True


def _replay_interrupted_batch(turn, preparation: TurnMessagePreparation) -> None:
    """Claim, replay, and durably pair the final interrupted tool batch."""
    from gateway.run import (
        build_victim_replay_plan,
        claim_forced_recovery_ownership,
        execute_victim_replay,
        plan_forced_resume_turn,
        trim_incomplete_assistant_text_tail,
    )
    from tools.approval_context import reset_current_session_key, set_current_session_key

    ctx = turn._ctx
    claim = claim_forced_recovery_ownership(
        turn._runner.session_store, ctx.session_id, ctx.history
    )
    if claim != "claimed":
        _stand_down_for_claim_loss(turn, preparation, claim)
        return

    resume_turn = plan_forced_resume_turn(ctx.message)
    ctx.message = resume_turn.message
    preparation.persist_user_message = resume_turn.persist_user_message
    preparation.continue_interrupted_turn = resume_turn.continue_interrupted_turn

    # Cached live history can itself hold the dangling final batch. Select
    # the same authoritative rows the model would otherwise receive.
    replay_source = (
        turn._live_replay_source if turn._live_history_selected else ctx.history
    )
    plan = build_victim_replay_plan(replay_source)
    session_key = ctx.session_key or ""
    approval_token = set_current_session_key(session_key)
    try:
        outcome = execute_victim_replay(
            turn._ctx.agent_holder[0],
            plan,
            raw_history=replay_source,
            session_store=turn._runner.session_store,
            session_id=ctx.session_id,
            effective_task_id=ctx.session_id or "",
        )
    finally:
        reset_current_session_key(approval_token)

    if outcome.repaired_history is not None:
        if turn._live_history_selected:
            preparation.agent_history = outcome.repaired_history
            from gateway.run import _collect_history_media_paths

            preparation.history_media_paths = _collect_history_media_paths(
                preparation.agent_history
            )
        else:
            ctx.history = outcome.repaired_history
            _rebuild_durable_history(turn, preparation)
    elif outcome.failure:
        logger.warning(
            "Transparent resume: victim replay failed (%s); blocking the turn",
            outcome.failure,
        )
        preparation.blocked_error = "forced_resume_replay_failed"
        return

    if plan.batch_present or not preparation.continue_interrupted_turn:
        return

    # A pure text continuation cannot append a second assistant row after
    # the incomplete one. Resume from the original legal boundary instead.
    trimmed, dropped_live = trim_incomplete_assistant_text_tail(
        preparation.agent_history
    )
    if dropped_live:
        preparation.agent_history = trimmed
    durable_tail, dropped_durable = trim_incomplete_assistant_text_tail(ctx.history)
    if not dropped_durable:
        return
    rewrite = getattr(turn._runner.session_store, "rewrite_transcript", None)
    if not callable(rewrite):
        ctx.history = durable_tail
        return
    try:
        rewritten = rewrite(ctx.session_id, durable_tail, active_only=True)
    except Exception:
        logger.warning("Text-only resume: durable tail trim raised", exc_info=True)
        rewritten = False
    if rewritten:
        ctx.history = durable_tail
        if not turn._live_history_selected:
            _rebuild_durable_history(turn, preparation)
        return

    logger.error(
        "Text-only resume for %s: durable tail trim failed; failing closed",
        ctx.session_key,
    )
    preparation.blocked_error = "forced_resume_text_tail_trim_failed"


def _append_cursor_recovery_note(turn, preparation: TurnMessagePreparation) -> None:
    ctx = turn._ctx
    note = getattr(turn, "_cursor_recovery_note", None)
    if not note:
        return
    ctx.message = f"{note}\n\n{ctx.message}" if ctx.message else note
    preparation.continue_interrupted_turn = False


def prepare_turn_message(
    turn,
    agent_history: list,
    observed_group_context: Any,
) -> TurnMessagePreparation:
    """Prepare ordinary guidance or transparent forced-victim recovery."""
    from gateway.run import (
        _auto_continue_freshness_window,
        _collect_history_media_paths,
        _is_fresh_gateway_interruption,
        _last_transcript_timestamp,
        _prepare_resume_pending_message,
        build_resume_recovery_note,
        is_forced_interruption_reason,
    )

    ctx = turn._ctx
    preparation = TurnMessagePreparation(
        agent_history=agent_history,
        observed_group_context=observed_group_context,
        persist_user_message=ctx.persist_user_message,
        persist_user_timestamp=ctx.persist_user_timestamp,
        history_media_paths=_collect_history_media_paths(agent_history),
    )
    turn._prepend_pending_note("_pending_model_notes")
    window = _auto_continue_freshness_window()
    interruption_is_fresh = _is_fresh_gateway_interruption(
        _last_transcript_timestamp(ctx.history), window_secs=window
    )
    entry = None
    if ctx.session_key:
        with suppress(Exception):
            entry = turn._runner.session_store._entries.get(ctx.session_key)
    resume_pending = entry is not None and getattr(entry, "resume_pending", False)
    resume_reason = (
        (getattr(entry, "resume_reason", None) or "restart_timeout")
        if resume_pending
        else None
    )
    mark_is_fresh = resume_pending and _is_fresh_gateway_interruption(
        getattr(entry, "last_resume_marked_at", None), window_secs=window
    )
    is_resume_pending = bool(
        resume_pending and (interruption_is_fresh or mark_is_fresh)
    )
    fresh_tool_tail = bool(
        agent_history
        and agent_history[-1].get("role") == "tool"
        and interruption_is_fresh
    )

    _consume_queued_resume_text(turn)
    restart_tool_continuation = False
    if is_resume_pending:
        if is_forced_interruption_reason(resume_reason):
            _replay_interrupted_batch(turn, preparation)
        else:
            restart_tool_continuation = _continue_restart_tool_turn(turn, preparation)
            if not restart_tool_continuation:
                ctx.message, preparation.persist_user_message = (
                    _prepare_resume_pending_message(
                        resume_reason,
                        ctx.message,
                        interactive=turn._resume_note_interactive(),
                    )
                )
        if not restart_tool_continuation:
            _append_cursor_recovery_note(turn, preparation)
    elif fresh_tool_tail:
        preparation.persist_user_message = ctx.message
        ctx.message = (
            "[System note: A new message has arrived. The conversation "
            "history contains pending tool outputs from an interrupted turn. "
            "IGNORE those pending results. Address the user's NEW message "
            "below FIRST. Do NOT re-execute old tool calls from the history.]\n\n"
            + ctx.message
        )
    elif not resume_pending:
        # Preserve ordinary observed-group cursor recovery outside resume.
        _append_cursor_recovery_note(turn, preparation)

    turn._prepend_pending_note("_pending_skills_reload_notes")
    if isinstance(ctx.message, str) and not ctx.message.strip() and resume_pending:
        if is_forced_interruption_reason(resume_reason):
            # If freshness prevented the recovery branch, never substitute
            # restart prose or a synthetic user row for the continuation.
            ctx.message = None
            preparation.persist_user_message = None
            preparation.continue_interrupted_turn = True
        else:
            ctx.message = build_resume_recovery_note(
                resume_reason,
                "",
                interactive=turn._resume_note_interactive(),
            )
    return preparation
