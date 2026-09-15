# Operations

## Invariants

- Do not restart `hermes-gateway.service` while agents are active.
- Do not create a cron job unless separately approved.
- There is one ingestion implementation: the `hermes discord-history` CLI. No crawler daemon or duplicate timer is permitted.
- Database transport is socket-activated infrastructure only. PID 1 listens on loopback and starts a hardened `ssh -W` worker for the lifetime of each PostgreSQL connection; no persistent SSH process remains. It must not inventory Discord, invoke DCE, import data, schedule work, or expose PostgreSQL publicly.
- Preserve root ownership and private modes: state directories `0700`; secrets, logs, exports, review artifacts, and installed plugin files `0600`; executable DCE launchers `0700`.

## Routine checks

```bash
hermes discord-history doctor --json
hermes discord-history status --json
hermes plugins tools | grep discord_history
```

Status includes coverage start/end/state, newest live message, last successful run, lag seconds, stale state, and the most recent error code.

## Inventory and synchronization

Inventory is fail-closed. A reconciliation is tombstone-safe only when configured roots, active threads, all archived-thread endpoints, page counts, cursor chains, fingerprints, parent unions, and run scope are complete and linked.

```bash
hermes discord-history inventory --guild <GUILD_ID> --json
hermes discord-history sync --guild <GUILD_ID> --mode incremental --json
hermes discord-history sync --guild <GUILD_ID> --mode reconcile --json
```

Reconciliation uses a fixed `source_before` cutoff. A message absent from DCE is tombstoned only when its `created_at` is strictly before that cutoff. Repeating the same import must create neither duplicates nor false revisions.

## Verification

Channel verification performs a fresh DCE export at the latest persisted reconciliation cutoff and compares:

- DCE/live PostgreSQL message-ID sets
- tombstone disjointness
- minimum and maximum timestamps
- deterministic sampled content hashes
- linked inventory, endpoint, page, cursor, fingerprint, parent-union, and run-scope evidence

```bash
hermes discord-history verify --channel <CHANNEL_ID> --json
```

For production acceptance, first perform an authorized exact-phrase search through the installed model tool and capture its `search_audit.audit_id`, then run:

```bash
hermes discord-history verify-e2e \
  --guild <GUILD_ID> \
  --owner-audit-id <AUDIT_ID> \
  --expected-message-id <MESSAGE_ID> \
  --json
```

`--expected-phrase '<EXACT_PHRASE>'` is optional and strengthens the canonical
message check when exact text is part of the acceptance fixture.

Also prove an invalid audit ID returns `FAIL` and nonzero. The plain positive command must print exactly `PASS`.

## DCE installation and recovery

`scripts/install-dce` downloads the pinned release, verifies SHA-256, extracts all 75 files into a private staging tree, verifies the launcher, atomically replaces the version tree and `current` symlink, and restores the prior tree on any late failure. Failed runs must leave no `.<version>.new.*`, `.<version>.old.*`, or `.current.new` debris.

Before changing the live plugin, archive `$HERMES_HOME/plugins/discord-history` under `$HERMES_HOME/discord-history/backups`. Deploy atomically, compare SHA-256 manifests, and retain the backup until installed verification passes.

## PostgreSQL and audit checks

The production DSN reaches a loopback-only systemd socket. Each connection
starts an `ssh -W` worker under `DynamicUser=yes`; systemd supplies the private
identity and known-hosts files through `LoadCredential`. Verify the listener,
absence of idle workers, and sandbox score with:

```bash
systemctl is-active hermes-discord-history-pg-transport.socket
systemctl list-units 'hermes-discord-history-pg-transport@*.service' --state=running
systemd-analyze security hermes-discord-history-pg-transport@.service --no-pager
```

After database clients close, the second command must list no running units.
The independently measured deployment score is `1.4 OK`.

Migrations are ordered and idempotent. The application role must have only the required schema privileges. `discord_archive.search_audit` must:

- have no raw principal-user column
- be owned by a dedicated `NOLOGIN` role
- grant the app only `SELECT` and `INSERT`
- reject update, delete, and truncate
- retain its append-only trigger

Search audit records contain HMAC-derived principal and query identities, complete requested/effective non-query filters, and exactly the message IDs delivered after output trimming.

## Failure handling

- Authorization failure: do not retry with identity arguments. Inspect the private denial log for stable reason codes.
- Partial/inaccessible inventory: do not reconcile or infer that missing messages were deleted.
- DCE failure: preserve the manifest reason code; do not import partial output.
- Verification failure: stop deployment acceptance, inspect `failed_checks`, repair, retest, redeploy, and use a fresh reviewer.
- Excessive lag: run the existing CLI sync manually and verify database state independently. Do not invent a second scheduler.
