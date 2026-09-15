---
sidebar_position: 6
sidebar_label: "Auto Update"
title: "Unattended Auto Update"
description: "Safe scheduled Hermes updates on Linux/systemd — prepare every tick, restart only when idle"
---

# Unattended Auto Update

The bundled **`auto_update`** backend plugin installs an independent systemd timer. Every tick is two phases, both through the **stock** Hermes updater:

1. **Prepare** — always: `hermes update --check`, then `hermes update --yes --defer-restart` when an update is available. Pulling code, syncing dependencies, and running migrations never interrupts a conversation, so preparation is not idle-gated.
2. **Activate** — always attempted in a fresh process: `hermes auto_update activate`. It restarts the fleet onto the prepared update only when Hermes is idle; otherwise it exits 0 and leaves the obligation pending for a later tick. Activation also requires the update to be *proven* prepared — marker, receipt, and checkout SHA all agreeing (see [Safety boundaries](#safety-boundaries)) — and it re-proves everything under the stock updater lock before restarting anything.

The plugin is a thin scheduler and policy layer. It does **not** reimplement backups, git operations, dependency installs, config migration, gateway restarts, or rollback logic — those stay inside `hermes update`.

:::note Platform support
Auto-update installs only on **Linux hosts with a functioning systemd**. Other platforms import the plugin cleanly and install nothing.
:::

## Quick start

```bash
hermes auto_update status
hermes auto_update enable      # write units + enable timer (never runs update immediately)
hermes auto_update disable     # stop timer; explicit disable survives upgrades
hermes auto_update reconcile   # idempotent unit refresh
hermes auto_update activate    # manual: restart onto a prepared update, only when idle
```

Plugin discovery and registration never write systemd units or run subprocesses.
On Linux/systemd hosts, gateway startup fires the generic `on_gateway_start`
lifecycle hook, which idempotently reconciles the timer (install/enable when
enabled; stop/disable when `auto_update.enabled: false`). The CLI verbs remain
the explicit management path for non-gateway installs and manual control.

## How scheduling works

| Piece | Name | Notes |
|---|---|---|
| Timer | `hermes-auto-updater.timer` | Every **30 minutes** around the clock (`*:00,30:00`), `RandomizedDelaySec=0`, `AccuracySec=1s`, `Persistent=true` |
| Service | `hermes-auto-updater.service` | `Type=oneshot`, **no** `PartOf=` / `BindsTo=` coupling to the gateway |

First setup enables the **timer only** — it never starts the oneshot service immediately. On first enable the timer stamp is pre-set so `Persistent=true` does not catch up missed slots from before install.

Install scope (system vs user systemd) is derived from your real Hermes paths and existing gateway unit metadata — not hardcoded usernames or checkout paths.

### User scope and linger

User-scoped timers stop when you log out unless **linger** is enabled for your account:

```bash
loginctl enable-linger "$USER"
```

`hermes auto_update status` and `enable` warn when user scope is selected but `/var/lib/systemd/linger/<user>` is absent (read-only check — no loginctl mutation).

## Idle gate (activation only)

The activation command re-checks idleness immediately before restarting anything, against a **read-only** `state.db` adapter:

Signals:

- active assistant streaming (incomplete final assistant row)
- unanswered user work (interrupted last turn)
- recent message activity inside the configured idle window
- active compression locks
- live session turn leases (`session_turn_leases.expires_at > now`)
- live delegated agents (`async_delegations.state IN ('dispatched','running','finalizing')`)

Busy → exit 0 and keep the prepared update pending. Missing or unreadable `state.db` fails **closed** as a quiet deferral. Lock contention or an in-flight manual update (`read_live_update`) also defer the whole tick quietly.

`gateway/scale_to_zero.is_idle` is **not** used — it requires live gateway process state unavailable to a standalone oneshot.

### Manual restarts do not double-bounce

A prepared update can also be picked up by hand: `/restart` or `hermes gateway restart` boots the new code but leaves the pending marker. When activation later runs, it first compares every live gateway's stamped version against the checkout — if the whole fleet already serves the current code it clears the marker **without restarting**. Stale, down, unknown, or missing runtimes — or a missing, malformed, or unreadable update receipt, which cannot prove that no required runtime went missing — keep the restart path.

## Configuration (`config.yaml`)

```yaml
auto_update:
  enabled: true
  idle_minutes: 8
  schedule: "*-*-* *:00,30:00"
  randomized_delay_sec: 0
  accuracy_sec: "1s"
  notify_on_success: ""
  notify_on_failure: ""
```

- **`enabled: false`** stops and disables the timer on the next reconcile (gateway startup hook or `hermes auto_update reconcile` / `disable`) and survives upgrades. Prefer this when you need the CLI to stay available.
- Listing `auto_update` under **`plugins.disabled`** keeps the full plugin (tools, the `run` oneshot entrypoint, and scheduler startup) off, but a restricted bundled cleanup path still loads: the management CLI (`status`, `enable`, `disable`, `reconcile`) and a gateway-start hook that stops/disables an already-installed timer. Explicit disable therefore remains effective across gateway starts and upgrades without re-enabling the updater capability.
- Optional `schedule_start_hour` / `schedule_end_hour` still override the default when set explicitly.
- Empty notification strings keep success/failure quiet except for systemd journal logs.
- Non-empty strings append one line to `$HERMES_HOME/auto-update/notifications.log` (notification failures are non-fatal).

## Legacy units

Older installs may have shipped `hermes-auto-update.service` / `hermes-auto-update.timer` plus a wrapper script. The plugin:

- never deletes administrator-owned unit files or wrapper scripts
- backs up positively identified legacy units under `$HERMES_HOME/auto-update/legacy-units/`
- disables **only** units matching the exact shipped legacy fingerprint (wrapper `ExecStart` line or full reference hash)
- refuses to enable duplicate schedulers when an unknown legacy timer remains enabled

New units use the distinct prefix **`hermes-auto-updater.*`**.

## Safety boundaries

- One tick = one deferred prepare (`--check`, then `--yes --defer-restart` only if an update is available) + one fresh-process activation attempt. A failed, timed-out, or hung preparation is never activated — any nonzero `--check` exit (regardless of output text: the stock check reports availability with exit 0, so no nonzero rc ever means "update available") plus check and prepare timeouts are a **nonzero** tick outcome, and a timed-out re-prepare never disturbs an older valid prepared generation (its pull-time write touches only the generic `fleet_restart_pending` marker; the strict record lives separately in `fleet_restart_prepared`).
- **Preparation only counts when it is proven.** `hermes update --yes --defer-restart` exits 0 solely after every required preparation step completed, the checkout still matches the target SHA, and a *prepared generation* was durably published: the update receipt is written and bound to the generation first (atomic temp-file write, fsync, directory fsync, exact read-back), then the `fleet_restart_prepared` record is published with the same durable transaction, carrying the schema version, a generation id, the exact 40-or-64 hex target SHA, and the receipt it belongs to. A required step that failed or only partially completed, a checkout that moved, or a record that could not be written and read back leaves the run at a **nonzero** exit with nothing stamped. Optional components that were never selected or installed do not become hard requirements.
- Activation needs durable proof, not just a pending marker. A hand-written or truncated `prepared=yes` proves nothing: the `fleet_restart_prepared` record must parse strictly (known schema, well-formed generation and full SHA, `restart=pending`, no duplicate or unknown fields), the bound receipt must still exist and agree with it, and the checkout `HEAD` must equal the recorded `expected_sha` at that moment. Anything else — missing, malformed, unreadable, mismatched, or a moved HEAD — is **not** success: activation exits nonzero, keeps the obligation, and points at `hermes update`. Being up to date per `--check` never counts as readiness.
- Activation runs **under the stock updater lock**, the same one a manual `hermes update` takes: idle is checked before acquiring it (never waiting for idle while holding a lock), then everything — record, receipt, HEAD, pending obligation, runtime plan, live fleet — is re-validated under the lock with no unlocked gap. Lock contention or a concurrent HEAD/generation change is a retryable non-success that preserves the obligation.
- After a restart, activation independently re-inspects the fleet before clearing anything. The obligation (record and marker) is cleared only when every updater-managed runtime the plan expects is present, healthy, and serving the expected generation — matched **one-to-one**, so two planned backends of the same kind require two distinct live replacements; a missing planned runtime, an unverifiable plan, a failed inspection, or a stale/down/ambiguous PID keeps the obligation and reports nonzero. Repeated activation is idempotent, and a valid plan proving the fleet already serves the exact SHA clears the obligation without a second restart.
- `hermes update --defer-restart` is a stock updater flag: full preparation, no gateway/serve/dashboard restart, `fleet_restart_pending` left behind. It never stops, kills, or pauses a live runtime either — on Windows it refuses (nonzero) while any gateway/serve/dashboard process holds the venv, rather than reap it or fall back to a ZIP update the way a full update does. The next plain `hermes update` — or an idle activation tick — finishes the restart. Default `hermes update` behavior is unchanged.
- Profile-safe nonblocking flock lock under `$HERMES_HOME/auto-update/.run.lock` for the tick itself (lock file retained after release).
- Atomic unit writes (`*.tmp` + `os.replace`) and byte-identical idempotent reconcile.
- Disable/reconcile stop the **timer only** — never `systemctl stop hermes-auto-updater.service`.

Disable any time:

```bash
hermes auto_update disable
# or
hermes config set auto_update.enabled false
```

Logs: `journalctl --user -u hermes-auto-updater.service` (user scope) or `journalctl -u hermes-auto-updater.service` (system scope).
