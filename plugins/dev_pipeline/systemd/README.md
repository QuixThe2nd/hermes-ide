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
delegated controllers on cgroup v2 hosts.
