"""Contract for the wave-3 TurnRunner facade deduplication."""

import gateway.run as gateway_run
from gateway.run_turn_runner import TurnRunner


_FACADE_MEMBER_NAMES = frozenset({
    "_STAGE_DELIVERY_RECOVER_TIMEOUT",
    "_STAGE_OUTCOME_DELIVERED",
    "_STAGE_OUTCOME_FAILED",
    "_STAGE_OUTCOME_UNKNOWN",
    "_STAGE_STATE_DELIVERED",
    "_STAGE_STATE_EMPTY",
    "_STAGE_STATE_FAILED",
    "_STAGE_STATE_FAILED_FINAL",
    "_STAGE_STATE_IN_FLIGHT",
    "_STAGE_STATE_UNKNOWN",
    "_STAGE_STOP_DRAIN_TIMEOUT",
    "_attach_session_title_callback",
    "_claim_tool_stage_delivery",
    "_collect_final_stage_events",
    "_deliver_agent_status_marker",
    "_deliver_tool_stage_event",
    "_drain_parked_tool_stage_events",
    "_event_callback_sync",
    "_fresh_stage_delivery_record",
    "_handle_stage_delivery_cancel",
    "_harvest_tool_stage_delivery",
    "_invocation_has_live_unknown_delivery",
    "_notice_callback_sync",
    "_pin_stage_delivery_task",
    "_process_tool_stage_event",
    "_send_native_task_card_progress",
    "_status_callback_sync",
    "_step_callback_sync",
    "_stop_drain_remaining_stage_embeds",
    "_sync_consume_stage_delivery_task",
    "combined_tool_start_callback",
    "native_tool_complete_callback",
    "native_tool_start_callback",
    "progress_callback",
    "run_sync",
    "send_progress_messages",
    "send_tool_stage_embeds",
    "tool_stage_event_callback",
})

_STAGE_CONSTANTS = {
    "_STAGE_DELIVERY_RECOVER_TIMEOUT": 10.0,
    "_STAGE_OUTCOME_DELIVERED": "delivered",
    "_STAGE_OUTCOME_FAILED": "failed",
    "_STAGE_OUTCOME_UNKNOWN": "unknown",
    "_STAGE_STATE_DELIVERED": "delivered",
    "_STAGE_STATE_EMPTY": "empty",
    "_STAGE_STATE_FAILED": "failed",
    "_STAGE_STATE_FAILED_FINAL": "failed_final",
    "_STAGE_STATE_IN_FLIGHT": "in_flight",
    "_STAGE_STATE_UNKNOWN": "unknown",
    "_STAGE_STOP_DRAIN_TIMEOUT": 1.0,
}


def test_gateway_run_no_longer_defines_turn_runner():
    assert "TurnRunner" not in vars(gateway_run)


def test_live_turn_runner_retains_the_facade_member_contract():
    missing = sorted(name for name in _FACADE_MEMBER_NAMES if not hasattr(TurnRunner, name))
    assert not missing, f"live TurnRunner is missing facade members: {missing}"

    changed_constants = {
        name: getattr(TurnRunner, name, None)
        for name, expected in _STAGE_CONSTANTS.items()
        if getattr(TurnRunner, name, None) != expected
    }
    assert not changed_constants, f"live TurnRunner stage constants changed: {changed_constants}"
