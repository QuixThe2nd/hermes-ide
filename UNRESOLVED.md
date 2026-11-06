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

