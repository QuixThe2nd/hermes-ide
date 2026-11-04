# cron/ — scheduled jobs

Applies on top of the root `AGENTS.md`. Long-form: `website/docs/developer-guide/cron-internals.md`;
user docs `website/docs/user-guide/features/cron.md`.

## Cron

`cron/jobs.py` (job store) + `cron/scheduler.py` (tick loop; `scheduler_*.py` siblings). Agents
schedule via the `cronjob` tool; users via `hermes cron list|add|edit|pause|resume|run|remove` or
`/cron`. Schedules: duration (`"30m"`, `"2h"`, `"1d"`), "every" phrase (`"every 2h"`, `"every monday
9am"`), 5-field cron (`"0 9 * * *"`), ISO one-shot (`"2026-06-01T09:00:00Z"`). Per-job fields:
`skills`, `model`/`provider` overrides, `script` (pre-run data-collection script whose stdout is
injected into the prompt; `no_agent=True` makes the script the whole job), `context_from` (chain job
A's last output into job B's prompt), `workdir` (run with that directory's `AGENTS.md`/`CLAUDE.md`
loaded), multi-platform delivery.

Hardening invariants — each guards a real failure; don't weaken without answering for it:
- **Inactivity watchdog** on cron agent sessions (`_cron_inactivity_seconds()`): default 600s idle,
  `HERMES_CRON_TIMEOUT` overrides, `0` = unlimited. It is idle time, not wall-clock — a stalled
  session is hard-interrupted so it cannot monopolise the scheduler, while a long-but-active job
  is never cut off. Attached scripts (pre-run or `no_agent`) are bounded separately by the script
  timeout (`_DEFAULT_SCRIPT_TIMEOUT`, 3600s).
- Catch-up window = half the period, clamped to 120s–2h; 120s grace for missed one-shots.
- Every recurring occurrence is accounted for: `tick()` advances `next_run_at` BEFORE dispatch
  (at-most-once across a mid-run crash) and stamps `pending_slot` in the same save; a scan that
  finds the stamp with a dead owner restores the instant ONCE (`cron/occurrences.py`), the
  executions ledger's `scheduled_instant` blocks a second fire, `cron.catch_up_missed: false`
  skips past-grace misses with a logged reason. Never drop a slot silently (#107485).
- Per-home tick lock `<home>/cron/.tick.lock` prevents duplicate ticks across processes for
  that profile's store; never a `~/.hermes/...` literal.
- **The ticker binds each served profile's scope for the whole tick, including pre-loop code.**
  `scheduler_provider.py::_start_multiplex` is ONE ticker iterating `profiles_to_serve()`
  sequentially under `_profile_cron_scope(home)` (home + secret scope + terminal scope) — never N
  threads (module globals race). Everything a tick touches lives inside that guard: store open,
  lock path, backoff/failure counters (`_note_tick_failure`), job env construction, and the
  `on_session_end` flush of a finished job. Supervision (`scheduler_thread.py::
  SupervisedTickerThread`; start on gateway boot, stand down for homes another gateway already
  serves, re-enumerate when a profile dir appears or is tombstoned) is per served home, not per
  process. Why: a store opened before the scope was entered wrote a secondary profile's run
  records into the launch profile's `jobs.json`.
- Cron sessions pass `skip_memory=True`; memory providers intentionally do not run during cron.
- Cron execution has its own session. Eligible continuable deliveries may mirror or seed the
  reply-facing conversation: origin, origin-less home fallback, user-written bare-platform home,
  or opted-in explicit targets. `all` expansions do not gain home mirror eligibility. Mirrored
  briefs are labelled user turns appended at a turn boundary, preserving role alternation.
- The cron ticker runs in the desktop-spawned backend when `HERMES_DESKTOP=1` — that env var means
  "spawned by the app", not "a GUI is watching" (root: capability is a property of the session).
- Background `delegate_task` is process-local; work that must survive restarts is a cron job or a
  `terminal(background=True, notify_on_complete=True)` process.

## Tests

`tests/cron/`. Schedule parsing and catch-up windows are pure functions — test them as data. Never
assert on the verb list or toolset size (root: no change-detectors). Time-based tests use loose
bounds (≥ 2s) and event sync.
