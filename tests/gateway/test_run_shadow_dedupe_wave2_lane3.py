"""Anti-regression guard: wave-2 lane-3 restore-shadowed duplicates stay consolidated.

History: the Sep-25 repo-restore resurrected the ``gateway/run.py`` monolith on top of the
Sep-2026 extracted sibling modules. Every method defined in BOTH the facade class body and a
sibling mixin was shadowed by the facade copy (MRO binds the facade first). Wave-2 lane 3
resolved the run_shutdown.py / run_agent_cache.py / run_voice.py / run_goals.py pairs — the
facade copies were removed and the winning body lives on the mixin (for ``stop``, the
lifecycle exception, the facade body stays in ``run.py`` and the mixin copy was deleted).
This test fails if any manifest pair is re-introduced as a double definition.
"""

import gateway.run as run_mod
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.run_goals import GatewayGoalsMixin
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.run_voice import GatewayVoiceMixin

# (method name, sibling module, mixin class) manifest resolved by lane 3.
_NAMES_BY_MODULE = {
    GatewayShutdownMixin: [
        "_active_work_count", "_active_cron_job_count", "_active_api_run_count",
        "_interrupt_api_server_runs", "_active_deferred_agent_worker_count",
        "_track_deferred_agent_worker", "_interrupt_deferred_agent_workers",
        "_scale_to_zero_has_live_background_work", "_scale_to_zero_idle_timeout_seconds",
        "_restart_loop_guard_config", "_scale_to_zero_active_messaging_platforms",
        "_scale_to_zero_should_arm", "_log_scale_to_zero_not_armed_reason",
        "_scale_to_zero_is_idle", "_scale_to_zero_note_real_inbound",
        "_relay_adapter_for_dormancy", "_scale_to_zero_watcher",
        "_scale_to_zero_self_suspend", "_enter_external_drain", "_exit_external_drain",
        "_drain_control_watcher", "_update_platform_runtime_status",
        "_pause_failed_platform", "_resume_paused_platform", "_drain_active_agents",
        "_interrupt_running_agents", "_notify_interrupted_cron_jobs",
        "_notify_active_sessions_of_shutdown", "_finalize_shutdown_agents",
        "_defer_agent_cleanup_until_future_done", "_finalize_session_off_loop",
        "_cleanup_agent_resources_off_loop", "_cleanup_agent_resources",
        "_increment_restart_failure_counts", "_suspend_stuck_loop_sessions",
        "_clear_restart_failure_count", "_wedged_agent_count",
        "_await_active_work_before_restart", "request_restart",
        "_start_systemd_watchdog",
    ],
    GatewayAgentCacheMixin: [
        "_extract_cache_busting_config", "_agent_config_signature",
        "_rehydrate_session_model_override", "_apply_session_model_override",
        "_snapshot_session_model_override", "_restore_session_model_override",
        "_is_intentional_model_switch", "_release_running_agent_state",
        "_release_turn_lease", "_rebind_turn_lease", "_clear_conversation_scope",
        "_clear_session_boundary_security_state", "_begin_session_run_generation",
        "_invalidate_session_run_generation", "_bind_adapter_run_generation",
        "_interrupt_and_clear_session", "_refresh_agent_cache_message_count",
        "_consume_pending_turn_sidecar_notes", "_voice_channel_sidecar_note",
        "_pinned_session_context_prompt", "_ephemeral_change_key",
        "_evict_cached_agent",
    ],
    GatewayVoiceMixin: [
        "_voice_key", "_voice_key_for_source", "_bind_voice_input_callback",
        "_load_voice_modes", "_save_voice_modes", "_set_adapter_auto_tts_disabled",
        "_set_adapter_auto_tts_enabled", "_sync_voice_mode_state_to_adapter",
        "_get_guild_id", "_handle_voice_channel_join", "_handle_voice_channel_leave",
        "_handle_voice_timeout_cleanup", "_is_duplicate_voice_transcript",
        "_handle_voice_channel_input", "_should_send_voice_reply",
        "_should_echo_stt_transcripts", "_send_voice_reply",
    ],
    GatewayGoalsMixin: [
        "_goal_max_turns_from_config", "_warm_goals_session_db",
        "_get_goal_manager_for_event", "_get_heartbeat_manager_for_event",
        "_register_heartbeat_watch", "_unregister_heartbeat_watch",
        "_start_heartbeat_poller", "_send_goal_status_notice",
        "_defer_goal_status_notice_after_delivery", "_post_turn_goal_continuation",
        "_run_post_turn_hooks", "_final_text_for_post_turn_hooks",
        "_post_turn_loop_completion", "_loop_wakeup_watcher",
    ],
}

ALL_NAMES = [(mixin, name) for mixin, names in _NAMES_BY_MODULE.items() for name in names]


def test_every_lane3_name_defined_exactly_once():
    """Each manifest name has exactly one def across {facade class, sibling mixin}.

    ``stop`` is the lifecycle exception: its single def stays on the facade
    (``GatewayRunner.__dict__``), not the mixin.
    """
    for mixin, name in ALL_NAMES:
        facade_def = name in run_mod.GatewayRunner.__dict__
        mixin_def = name in mixin.__dict__
        if name == "stop":
            assert facade_def and not mixin_def, (
                "stop is the lifecycle exception: the facade body must stay in run.py"
            )
            continue
        assert mixin_def and not facade_def, (
            f"{name}: expected exactly one def on {mixin.__name__} (facade copy must stay "
            f"deleted); facade_def={facade_def} mixin_def={mixin_def}"
        )
        assert callable(getattr(run_mod.GatewayRunner, name, None)), name


def test_no_lane3_name_lost_from_the_composed_runner():
    """Every manifest name still resolves to a callable on the composed GatewayRunner."""
    for _mixin, name in ALL_NAMES:
        assert callable(getattr(run_mod.GatewayRunner, name, None)), name
