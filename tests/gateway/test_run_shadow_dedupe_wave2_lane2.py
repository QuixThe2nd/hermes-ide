"""Anti-regression guard: wave-2 lane-2 diverged shadow pairs stay consolidated.

History: the Sep-2026 mixin split (run.py -> run_busy.py / run_config_loaders.py /
run_topics.py) was clobbered by the Sept-25 repo-restore, leaving diverged duplicate
definitions — the ``GatewayRunner`` class-body copies in ``gateway/run.py`` shadowed
their mixin counterparts via the MRO. Wave 2 resolved every diverged pair (wave-1
removed the byte-identical ones): exactly ONE definition of each name remains across
the facade class bodies and the owning sibling module. This test fails if any pair is
re-introduced.
"""

import ast
from pathlib import Path

import gateway.run as run_mod

REPO = Path(run_mod.__file__).resolve().parent.parent
# module -> method names that were diverged facade/sibling duplicates (wave-2 lane 2).
MANIFEST = {
    "run_busy.py": ['_active_session_limit_message', '_busy_egress_command', '_busy_goal_command', '_busy_loop_command', '_busy_new_command', '_busy_queue_command', '_busy_start_command', '_busy_stop_command', '_check_slash_access', '_claim_active_session_slot', '_clear_goal_pending_continuations', '_dispatch_busy_slash_command', '_drop_redelivered_fifo_copy', '_enqueue_fifo', '_fifo_message_id', '_find_queued_copy_by_message_id', '_gateway_plain_command_handlers', '_get_max_concurrent_sessions', '_handle_active_session_busy_message', '_handle_blueprint_command', '_handle_pause_command', '_handle_suggestions_command', '_is_goal_continuation_event', '_is_stale_restart_redelivery', '_maybe_confirm_destructive_slash', '_prepare_busy_steer_text', '_promote_queued_event', '_purge_redelivered_overflow_copies', '_queue_depth', '_queue_during_drain_enabled', '_queue_or_replace_pending_event', '_read_user_config', '_request_slash_confirm', '_rescue_orphaned_overflow', '_rescue_park_incoming_event', '_sibling_thread_run_keys', '_steer_text_with_origin'],
    "run_config_loaders.py": ['_apply_fallback_chain_to_agent', '_busy_modes_from_config', '_effective_busy_input_mode', '_effective_busy_text_mode', '_get_system_prompt_for_channel', '_load_background_notifications_mode', '_load_busy_input_mode', '_load_busy_text_mode', '_load_cron_drain_timeout', '_load_ephemeral_system_prompt', '_load_fallback_model', '_load_prefill_messages', '_load_provider_routing', '_load_reasoning_config', '_load_restart_after_turn_timeout', '_load_restart_drain_timeout', '_load_service_tier', '_load_show_reasoning', '_load_signal_interrupt_grace_timeout', '_parse_reasoning_command_args', '_post_interrupt_grace_timeout', '_refresh_fallback_model', '_resolve_model_for_channel', '_resolve_session_reasoning_config', '_resolve_session_service_tier', '_set_session_reasoning_override', '_set_session_service_tier_override', '_snapshot_profile_busy_modes'],
    "run_topics.py": ['_await_relay_auto_thread_info', '_disable_telegram_topic_mode_for_chat', '_ensure_telegram_system_topic', '_get_telegram_topic_capabilities', '_is_discord_auto_thread_lane', '_is_relay_discord_channel_lane', '_is_telegram_topic_lane', '_is_telegram_topic_root_lobby', '_record_telegram_topic_binding', '_recover_telegram_topic_thread_id', '_relay_auto_thread_info', '_rename_discord_auto_thread_for_session_title', '_rename_telegram_topic_for_session_title', '_restore_telegram_topic_session', '_sanitize_discord_thread_title', '_sanitize_telegram_topic_title', '_schedule_discord_semantic_thread_rename', '_schedule_telegram_topic_title_rename', '_send_telegram_topic_setup_image', '_should_send_telegram_capability_hint', '_should_send_telegram_lobby_reminder', '_sync_telegram_topic_binding', '_telegram_topic_auto_rename_disabled', '_telegram_topic_cooldown_key', '_telegram_topic_mode_enabled', '_telegram_topic_new_header', '_telegram_topic_profile_name', '_telegram_topic_root_status_message'],
}
FACADE_CLASSES = ("GatewayRunner", "TurnRunner")


def _facade_class_def_names() -> set:
    tree = ast.parse((REPO / "gateway" / "run.py").read_text())
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in FACADE_CLASSES:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(item.name)
    return names


def _sibling_def_names(module: str) -> set:
    tree = ast.parse((REPO / "gateway" / module).read_text())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_wave2_lane2_single_def_invariant():
    """Every manifest name has exactly one def across facade class bodies + sibling module."""
    facade = _facade_class_def_names()
    for module, pairs in MANIFEST.items():
        sibling = _sibling_def_names(module)
        for name in pairs:
            count = int(name in facade) + int(name in sibling)
            assert count == 1, (
                f"{module}:{name} has {count} defs (facade={name in facade}, "
                f"sibling={name in sibling}); the shadow duplicate is back"
            )


def test_wave2_lane2_method_resolves_to_mixin():
    """Each manifest method resolves on GatewayRunner (mixin copy is live and importable)."""
    runner_cls = run_mod.GatewayRunner
    for module, pairs in MANIFEST.items():
        for name in pairs:
            assert callable(getattr(runner_cls, name, None)), f"{name} missing on GatewayRunner"
