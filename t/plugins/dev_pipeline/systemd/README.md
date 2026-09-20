# Hermes dev-pipeline executor (systemd)

The executor service **self-installs**. On plugin load (gateway startup),
`plugins/dev_pipeline/executor_setup.py` reconciles
`~/.config/systemd/user/hermes-dev-executor.service` on Linux hosts with a
reachable systemd **user** manager: it renders the canonical unit with paths
resolved from the running interpreter, runs `systemctl --user daemon-reload`
when the content changed, and enables the unit (`systemctl --user enable
--now`, which also starts it best-effort — a failure there is a warning, never
a plugin-load failure).

On every other platform (macOS, Windows, containers without systemd,
system-scope installs) reconcile logs once and leaves the host alone —
nothing is written.

## systemd scope (user vs system manager)

Every executor↔systemd interaction — spawning attempt units
(`systemd-run`), `is-active`, `stop`, `show` — goes through one scope
resolution:

1. `dev_pipeline.systemd_scope` in `~/.hermes/config.yaml` (`"user"` or
   `"system"`, case-insensitive; anything else reads as unset),
2. the internal `DEV_PIPELINE_SYSTEMD_SCOPE` bridge env var (for unit
   files/wrappers that cannot express a config override — config.yaml is
   the documented knob),
3. auto-detection: non-root executor → `user`, root → `system`.

Auto-detection is what makes the self-installed **user** executor work:
bare `systemd-run` defaults to `--system` and a non-root executor gets
`Failed to start transient service unit: Access denied` from polkit
(exactly the 2026-08-24 incident). With scope resolved to `user`, the
executor passes `--user` to both `systemd-run` and every `systemctl`
call, so attempts spawn into the executor's own user manager. Root
(system-scope) hosts keep the historical bare argv byte-for-byte.

No `/run/user/<uid>` paths are hardcoded anywhere: the user manager
itself sets `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` for every
unit it spawns — the executor service and each transient attempt unit
alike — so `systemctl --user` / `systemd-run --user` locate the user bus
purely through the inherited environment (verified live: a transient
unit spawned with no relevant `--setenv` still receives both vars). This
is also why the rendered executor unit needs no extra `Environment=`
entries for user-scope spawning.

If the user manager is unreachable (no user session, bus socket gone),
the spawn does not crash the executor: `systemd-run` fails, the failure
is logged (`systemd-run failed for <unit>: …`), and the task is blocked
with `infra_broken` / "failed to spawn attempt unit" — the same clean
warning-and-block path as any other spawn failure.

`hermes-dev-executor.service` in this directory is the canonical-unit
**reference copy** plus the manual-override/debug guide. The unit the plugin
renders and installs is authoritative; keep this file in sync when the
renderer changes.

## What the rendered unit pins

- `ExecStart` — `sys.executable -m plugins.dev_pipeline.executor run`, i.e.
  the interpreter running the plugin (its venv), never an ambient `python3`
  (which produced broken units with no hermes tree importable).
- `Environment` — `HOME`, `HERMES_HOME` (profile-aware), `PYTHONPATH=<repo
  root>`, and a `PATH` assembled from the venv bin, Hermes-managed Node,
  `~/.local/bin`, and the standard system bins (both agent lanes shell out to
  node-based CLIs).
- `WorkingDirectory` — the repo root derived from the plugin's own file,
  never the cwd of whatever process loaded the plugin.

Reconcile is idempotent: an identical installed unit means no rewrite and no
`daemon-reload`. A hand-installed legacy unit with different content is
**adopted** — rewritten to the canonical shape — not refused.

## Prerequisites

- `dev_pipeline.enabled: true` in `~/.hermes/config.yaml` gates work-claiming
  (default **false**; the executor exits when disabled). Flip the flag, then
  `systemctl --user restart hermes-dev-executor`.
- Cursor Agent CLI (`agent`) on `PATH`.
- `gh` authenticated for draft PR creation (executor credentials only —
  attempts never see tokens).

## Attempt resource limits & timeouts (`config.yaml`)

Every attempt runs as its own transient unit, so the resource properties are
what stand between a runaway job and the host. All of them live under
`dev_pipeline:` in `~/.hermes/config.yaml`:

```yaml
dev_pipeline:
  # cgroup memory ceiling per attempt unit (systemd MemoryMax=).
  # Default "6G" — the value this used to be hardcoded to.
  attempt_memory_max: "6G"

  # Hard wall-clock ceiling for a Claude-lane attempt (RuntimeMaxSec).
  claude_timeout_seconds: 7200

  # Same, for a Cursor-lane attempt.
  cursor_timeout_seconds: 1800
```

`attempt_memory_max` takes any size systemd understands (`512M`, `6G`,
`1.5G`, `512MiB`, `infinity` for no limit). An unset, empty, or invalid value
falls back to `6G`, so the spawned unit's property list is byte-identical to
what this pipeline shipped before the knob existed.

These three are the same failure class: on 2026-08-25 job `t_135a3014`
OOM-killed at the then-unconfigurable `MemoryMax=6G` on run 6, then took a
`SIGTERM` at `RuntimeMaxSec` (the `claude_timeout_seconds` default of 7200s)
on run 8, and the block loop routed it to triage. Neither ceiling was
adjustable without editing the source.

## Agent wake on block

When a dev job blocks with an actionable cause (`infra_broken`,
`attempts_exhausted`, `planning_unavailable`, …) — including when the block
loop routes it to triage — the kanban notifier wakes the **submitting agent's
session** with an actionable turn, not just the human-facing chat message.
The turn is self-contained: board, task id and title, block kind + reason,
the last few runs with durations and failure lines, the workspace and logs
paths, and the standing instruction to investigate first, recover
autonomously when the cause is mechanical (resource limits, stale executor,
known transient), and escalate to the human only for genuinely human
decisions (auth, business trade-offs, anything destructive).

```yaml
dev_pipeline:
  agent_wake_on_block: true   # default; false disables the agent turn
```

Behaviour worth knowing:

- **No wake on deliberate human stops.** `cancelled_by_user` and
  `secret_in_diff` never wake the agent — the human parked the job, or a
  secret already reached the diff and the next step is a human decision.
- **Loop safety.** At most one agent wake per (task, block signature,
  destination). If the agent's own recovery attempt re-blocks for the *same*
  cause, that round gets the human-facing message only — no self-sustaining
  agent loop. A genuinely different cause produces a different signature and
  wakes again; a task watched from two chats wakes each chat once (that is
  delivery, not a loop). The record lives in
  `<kanban root>/kanban/agent_wake_ledger.json` and survives gateway
  restarts.
- **Routing.** The wake goes to the session that submitted the job, recorded
  at submit time as the task's kanban notify subscription
  (`delegate_development` registers it from session context).

## Verify / debug

```bash
systemctl --user status hermes-dev-executor
systemctl --user cat hermes-dev-executor   # what is actually installed
journalctl --user -u hermes-dev-executor -f
```

Logs also land under the Kanban board logs root
(`<hermes_home>/kanban/boards/dev/logs/<task_id>/`).

## Manual override

To pin your own unit or opt out of self-management, mask the managed name —
reconcile treats a masked unit as an explicit opt-out and only logs a warning
on load:

```bash
systemctl --user mask hermes-dev-executor
```

System-scope deployments (root hosts, `/etc/systemd/system`) are outside
self-install by design: copy the reference unit, edit paths for your host,
and manage it by hand as before.

## Cancellation

Blocking a running task (`hermes kanban block <id>`) causes the executor to
stop the active attempt unit, record `cancelled_by_user`, and leave the
workspace intact for evidence.

## Attempt units

Each implementation attempt runs as a separate transient unit
`hermes-dev-<task_id>-<run_id>`, spawned via `systemd-run` (with `--user`
when the resolved scope is `user` — see above) outside the executor
cgroup. That is why `KillMode=mixed` on the executor service is
acceptable. Resource properties (`RuntimeMaxSec`, `MemoryMax`,
`OOMScoreAdjust`) apply in user scope too — the user manager holds the
delegated controllers on cgroup v2 hosts. `MemoryMax` comes from
`dev_pipeline.attempt_memory_max`; the other two are fixed.
