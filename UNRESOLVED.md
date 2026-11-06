# Wave-2 lane 1 — unresolved pairs

None. All 96 manifest pairs (run_adapters.py 44, run_startup.py 26, run_notifications.py 26)
were resolved with decisive evidence; see the lane-1 report for the per-pair decision table.

## Follow-ups surfaced during triage (NOT acted on — would change live behavior)

These are divergences inside resolved pairs where the losing body carried an unreleased fix.
Consolidation preserved live behavior verbatim; each deserves its own deliberate PR:

1. `GatewayRunner._send_restart_notification` (run_notifications.py): the pre-restore sibling
   body routed the restart notice through `_adapters_for_profile(self._marker_profile(data))`
   (requester's profile adapter map) instead of `self.adapters`; the facade body (now placed,
   live) uses the launch-profile map. Multiplex profile-scoped delivery leak class.
2. `GatewayRunner._start_secondary_profile_adapters` (run_adapters.py): the pre-restore sibling
   body ended with `self._restore_secondary_completion_ledgers(profile_homes)`; the facade body
   (now placed, live) does not restore secondary completion ledgers.
3. `GatewayRunner._redeliver_claimed_obligations` (run_startup.py): the pre-restore sibling body
   held adopted flood-refused rows for their deadline and called
   `_arm_flood_timers_for_waiting_rows()` at the end; the facade body (now placed, live) does
   neither.
4. `GatewayRunner.start` (facade, lifecycle exception): the deleted pre-restore sibling copy
   ended with `self._update_runtime_status(self._serving_state())` (parked-platform-aware) and
   ran free-tier bootstrap under `_run_boot_probe_in_launch_scope`; the live facade start
   writes plain "running" and runs the bootstrap unscoped in an executor.
5. `GatewayRunner._start_one_profile_adapters` (run_adapters.py): triage initially leaned
   sibling, but the facade body is the #405 post-restore winner (per-profile
   `_collect_profile_gate_warnings` — pinned by TestGateConfigWarningsAccumulation) and was
   placed verbatim. The pre-restore sibling body additionally had a live/queued rescan guard
   and `_note_unserved_secondary_platform` reporting whose absence the lane suites do not
   pin; revisit if unserved-secondary reporting regresses in the field.

# Wave-2 lane 4 — unresolved pairs

None. All 36 manifest pairs (`run_inbound.py` 19, `run_turn.py` 12, `run_watchers.py` 5) were
resolved with decisive evidence (see lane-4 report): default sibling-win for AST-identical
pairs, live facade-win for diverged pairs with post-restore merge history to `gateway/run.py`
(Oct-02 `6b5950dc71a5` and earlier `#563`/`#564` consolidation context).

## Follow-ups surfaced during triage (NOT acted on — would change live behavior)

1. `GatewaySessionWatchersMixin._check_session_stalls` / `_session_stall_watcher` (now carrying
   the live facade bodies): the **pre-restore** `run_watchers.py` had factored stall delivery
   into `_notify_session_stall` plus `_stall_candidates()` (delivery adapter for overflow
   queues, profile-scoped send via `present_notification`). The **live** facade implementation
   inlined notify in `_check_session_stalls` and iterated adapters via `_iter_gateway_adapters`
   (still on `GatewayRunner` in `run.py`, **not** duplicated). Consolidation **dropped** the dead
   `_stall_candidates` / `_notify_session_stall` helpers from the mixin — they never executed at
   runtime while the facade shadowed the mixin.

2. `GatewaySessionWatchersMixin._session_stall_watcher`: the pre-restore sibling loop ended with
   `await _interruptible_sleep(self, …)`; the live facade used an explicit 1-second stepped sleep.
   The live facade body was placed verbatim.

---

# Wave-2 lane 3 — unresolved / follow-ups

Scope: `run_shutdown.py` (41 pairs), `run_agent_cache.py` (22), `run_voice.py` (17),
`run_goals.py` (14). All 94 manifest pairs resolved; none left untouched as genuinely
ambiguous. Archaeology: NONE of the 94 facade defs existed in `gateway/run.py` at
`6b5950dc71a5^` — the Oct-02 upstream merge (6b5950dc) authored them into the facade
post-restore and edited the sibling copies in the same merge; no later commit (other than
the wave-1/2 dedupe commits) touched either side. Per the methodology's post-restore
facade-edit exception, the FACADE body won and was moved verbatim into the sibling (only
late-import lines for run.py module globals plus stdlib/typing imports added). The 9
AST-identical pairs kept the sibling def (facade def deleted); `stop` is the lifecycle
exception (facade body stays in `run.py`, sibling copy deleted).

## Ambiguous pairs left untouched

None — history was decisive for every pair (single authoring commit 6b5950dc on both
sides → the exception applies uniformly).

## Follow-ups (features only the losing SIBLING body had — dropped; live behavior preserved)

Each lived in the pre-edit sibling bodies (dead code at runtime since Oct-02; recoverable
from commit e06ee64784ae):

1. `_scale_to_zero_self_suspend` — NAS-brokered suspend path: `request_brokered_suspend`
   via stamped `GATEWAY_RELAY_SLEEP_URL`, `_scale_to_zero_await_freeze_gap`,
   `_scale_to_zero_abandon_suspend`, `_scale_to_zero_hold_redial`. Facade (now placed)
   keeps the flaps-socket-only path.
2. `_scale_to_zero_watcher` — direct-platform logging (`_scale_to_zero_direct_platform_logged`)
   and the abandon/hold-redial suspend choreography from (1).
3. `_scale_to_zero_active_messaging_platforms` — multiplex awareness: live secondary-profile
   adapters (`_profile_adapters`) and pending reconnects (`_profile_failed_platforms`)
   counted as active so they block suspension; facade counts only enabled primary config.
4. `_exit_external_drain` — revert to `_serving_state()` instead of hardcoded `'running'`.
5. `_scale_to_zero_note_real_inbound` — `_scale_to_zero_status` helper instead of direct
   `_update_runtime_status('running')`.
6. `_invalidate_session_run_generation` — settles a pending one-turn model override
   (`_restore_pending_one_turn_model_override`) before claiming a new generation.
7. `_run_post_turn_hooks` — skips the goal-continuation hook when the turn's final text is
   empty (placed facade runs both hooks unconditionally).
8. `_send_goal_status_notice` — `_goal_notice_adapter` helper + debug log when no adapter.
9. Behavior-equivalent helper-based rewrites left unadopted (helpers remain in the sibling
   modules, currently unused by the placed bodies): `_relay_wake_url_or_none`,
   `_gateway_cfg_section` + `_int_or` (`_scale_to_zero_idle_timeout_seconds`,
   `_restart_loop_guard_config`), `_write_runtime_status_quiet`
   (`_update_platform_runtime_status`), `_stuck_loop_counts_path` + `_read_json_counts`
   (`_clear_restart_failure_count`, `_increment_restart_failure_counts`),
   `_api_server_hook` (`_active_api_run_count`, `_interrupt_api_server_runs`,
   `_scale_to_zero_is_idle`), `_running_cron_job_count` (`_active_cron_job_count`,
   `_scale_to_zero_is_idle`).
10. Voice family helper-based rewrites left unadopted: `_apply_voice_mode`,
    `_toggle_adapter_auto_tts_set`, `_delivery_adapter_for`, `_VOICE_MODES`,
    `_voice_input_source`, `_is_user_authorized_for_source`, `_deliver_voice_reply`
    multi-file TTS handling (`_send_voice_reply` placed facade sends only the first file).
