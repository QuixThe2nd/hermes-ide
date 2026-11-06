"""Anti-regression guard (wave 2, lane 1): adapter/startup/notification shadow dupes stay resolved.

The Sep-25 repo-restore (c4e46ff8) resurrected the gateway/run.py monolith on top of the
extracted sibling mixins. Wave 1 removed byte-identical shadows; wave 2 lane 1 resolved the
diverged pairs across gateway/run_adapters.py, gateway/run_startup.py and
gateway/run_notifications.py (except ``GatewayRunner.start``, which stays on the facade as a
lifecycle method). This test fails if any manifest name is ever defined in BOTH the
GatewayRunner class body in gateway/run.py AND its sibling module again.
"""

import ast
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[2] / "gateway"

# sibling module -> mixin class holding the (single) surviving definition.
LANE1_MODULES = {
    "run_adapters.py": "GatewayAdapterLifecycleMixin",
    "run_startup.py": "GatewayStartupMixin",
    "run_notifications.py": "GatewayNotificationsMixin",
}


def _class_method_names(path: Path, cls: str) -> set:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"class {cls} not found in {path.name}")


# Names from the wave-2 lane-1 manifest (shadow-duplicated GatewayRunner methods).
LANE1_NAMES = {
    "run_adapters.py": [
        "_adapter_credential_claim", "_adapter_credential_fingerprint",
        "_adapter_disconnect_timeout_secs", "_adapter_listener_claim",
        "_await_adapter_cleanup_with_timeout", "_bounded_adapter_teardown",
        "_cancel_secondary_profile_reconnect_tasks", "_configure_profile_adapter",
        "_connect_adapter_with_timeout", "_connect_initial_adapter_with_timeout",
        "_create_adapter", "_ensure_reconnect_watcher_running",
        "_handle_adapter_fatal_error", "_handle_adapter_fatal_error_detached",
        "_handle_adapter_fatal_error_impl", "_handle_gateway_platform_event",
        "_handle_profile_adapter_fatal_error", "_handle_reaction_event",
        "_handoff_watcher", "_instantiate_adapter", "_make_adapter_auth_check",
        "_make_default_profile_message_handler",
        "_make_default_profile_platform_event_handler",
        "_make_profile_busy_session_handler", "_make_profile_fatal_error_handler",
        "_make_profile_message_handler", "_make_profile_platform_event_handler",
        "_on_reconnect_watcher_gave_up", "_platform_connect_timeout_secs",
        "_platform_reconnect_watcher", "_primary_message_handler",
        "_primary_platform_event_handler", "_queue_retryable_fatal_platform",
        "_run_secondary_profile_reconnect", "_safe_adapter_disconnect",
        "_schedule_secondary_profile_reconnect",
        "_schedule_secondary_profile_startup_reconnect",
        "_schedule_slow_reconnect_watcher_respawn", "_spawn_reconnect_watcher",
        "_spawn_supervised", "_start_one_profile_adapters",
        "_start_secondary_profile_adapters", "_supervised_backoff",
        "_wire_adapter_handlers",
    ],
    "run_notifications.py": [
        "_async_delegation_watcher", "_build_process_event_source",
        "_cancel_process_completion_batch_tasks", "_classify_completion_target",
        "_completion_delivery_identity", "_deliver_async_delegation_group",
        "_deliver_completion_notification", "_deliver_media_from_response",
        "_deliver_platform_notice", "_deliver_queued_first_response",
        "_drain_watch_notifications", "_enqueue_process_completion_notification",
        "_enrich_async_delegation_routing", "_flush_process_completion_batch",
        "_format_coalesced_process_completions", "_free_tier_startup_line",
        "_inject_watch_notification", "_record_coalesced_completion_siblings",
        "_resolve_async_delegation_session", "_run_process_watcher",
        "_schedule_update_notification_watch",
        "_send_notification_channel_startup_notifications",
        "_send_restart_notification", "_send_session_db_warning_notifications",
        "_send_update_notification", "_watch_update_progress",
    ],
    "run_startup.py": [
        "_abort_startup_if_shutdown_requested", "_await_startup_boot_sends",
        "_await_startup_warmup", "_claim_pending_obligations",
        "_clear_resume_pending_for_claimed_obligations",
        "_consume_clean_shutdown_marker", "_drain_startup_restore_queue",
        "_finish_startup_restore", "_hosted_room_worker_watcher",
        "_process_handoff", "_queue_startup_restore_event",
        "_recover_unclean_sessions", "_redeliver_claimed_obligations",
        "_redeliver_failed_obligations_for_platform",
        "_redeliver_pending_obligations", "_run_startup_resume_event",
        "_schedule_resume_pending_sessions", "_start_hosted_room_worker_sync",
        "_start_loop_heartbeat_task", "_start_loop_liveness_guards",
        "_start_startup_warmup", "_startup_should_abort",
        "_stop_hosted_room_worker", "_stop_loop_liveness_guards",
        "_warm_turn_prerequisites", "start",
    ],
}


def test_every_manifest_name_defined_exactly_once():
    facade_names = _class_method_names(GATEWAY_DIR / "run.py", "GatewayRunner")
    duplicates = []
    for mod, names in LANE1_NAMES.items():
        sibling_names = _class_method_names(GATEWAY_DIR / mod, LANE1_MODULES[mod])
        for name in names:
            count = int(name in facade_names) + int(name in sibling_names)
            if count != 1:
                duplicates.append(f"{mod}::{name} defined {count}x")
    assert not duplicates, "shadow duplicates re-introduced: " + ", ".join(duplicates)


def test_lifecycle_start_remains_on_facade():
    """``start`` is a lifecycle method: the facade owns construction; the sibling copy is gone."""
    facade_names = _class_method_names(GATEWAY_DIR / "run.py", "GatewayRunner")
    sibling_names = _class_method_names(
        GATEWAY_DIR / "run_startup.py", "GatewayStartupMixin"
    )
    assert "start" in facade_names
    assert "start" not in sibling_names
