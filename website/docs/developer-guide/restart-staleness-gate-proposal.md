# Restart-after-ship gate — proposal

**Status:** proposed, not implemented. Owner: whoever next touches `plugins/gateway_restart/`.

## Problem

The fork merges a PR whose code the running gateway has already imported; the live process keeps
executing the old module from its already-loaded imports. Behavior claims then reference code that
is on disk but not in the process. The failure mode is silent: the merge is green, the artifact on
disk is new, the behavior is old.

## Invariant

After a merge that changes files already imported by the running gateway process, either (a) the
gateway restarts before any behavior claim about that code, or (b) the claim explicitly names the
running process as stale.

## Existing mechanism that owns restart

`plugins/gateway_restart/tool.py` — the `restart` tool. It already pings the user when other
sessions are in flight; that ping is the confirmation gate. This proposal EXTENDS that plugin.
Do not build a parallel watcher.

## Proposed addition

A `stale_import_check` function in the gateway_restart plugin:

1. Input: a merge event (PR number or SHA). Source can be a cron job polling `gh pr list --state
   merged`, or a webhook — decide when a concrete consumer exists.
2. Compute the merged file list: `gh pr diff <N> --name-only`.
3. Map to loaded modules: the gateway process's imported module set is approximated by walking
   `sys.modules` of the live process. The plugin runs in-process, so it can snapshot
   `set(sys.modules)` at startup and map repo paths to module names (`agent/turn_api.py` ->
   `agent.turn_api`).
4. Intersection non-empty -> the running process is stale for those modules.
5. Action: post a normal channel message (no ping) to the configured notify channel: "merged code
   overlaps loaded modules: <modules>; restart when convenient." If no sessions are in flight, the
   restart tool's existing ping path handles it — reuse, don't duplicate.

## Config

Under the plugin's config section (`plugins.gateway_restart`):

```yaml
plugins:
  gateway_restart:
    stale_import_check: true   # default false until a consumer exists
```

No new `HERMES_*` env vars (fork rule).

## Tests

- BEFORE: tests fail — the function does not exist.
- Unit: synthetic `sys.modules` fixture + a file list; assert the path->module mapping and the
  intersection result. Use `monkeypatch` on `sys.modules`, never a real gateway.
- Contract: a merge touching only docs/tests produces an empty intersection (no nag).
- Adjacent: run the existing gateway_restart plugin tests after the change.

## Explicitly out of scope

- Auto-restart on merge. The user decides; the gate informs. Auto-restarting on merge events
  without a human in the loop is a separate decision and needs its own authorization.
- Cron delivery wiring. The poll source is a deliberate deferral until a real consumer exists
  (speculative infrastructure is rejected per the fork rubric).

## What this is NOT

Not a runtime code gate. Nothing here blocks a merge or a claim; it makes staleness visible. The
blocking version (refuse behavior claims while stale) would require intercepting assistant output,
which no current hook supports cleanly.
