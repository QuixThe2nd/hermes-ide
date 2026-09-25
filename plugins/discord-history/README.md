# Hermes Discord History

Owner-authorized Discord history recall for Hermes Agent, backed by PostgreSQL and the pinned DiscordChatExporter CLI.

## Surface

The native user plugin registers one read-only model tool, `discord_history`, with exactly four actions:

- `search`: ranked full-text/trigram search with channel, author, and time filters
- `get`: retrieve one live message by ID, including bounded immutable revisions
- `context`: retrieve bounded surrounding live messages
- `status`: report archive coverage, freshness, lag, and last error

Synchronization, migrations, diagnostics, and verification remain CLI-only under `hermes discord-history`. The plugin adds no core Hermes tool, MCP server, crawler daemon, timer, or cron job.

## Architecture

- PostgreSQL is the canonical archive and search index.
- DiscordChatExporter `2.47.3` supplies historical exports. Its release checksum is pinned to `8f86bd3a2c2f4412ffbbb2dcb9348642f8f929ad94a4f290ff0f78068c44fc86`.
- Discord REST inventory establishes complete channel/thread scope before reconciliation can create tombstones.
- Authorization uses directly bound Discord gateway `ContextVar` values before any database connector is called.
- Retrieval SQL is fixed and parameterized. Every message path excludes tombstones and enforces resolver, row, context, snippet, revision, and UTF-8 response-byte limits.
- Results include stable IDs, observed names, UTC timestamps, edit metadata, coverage, and Discord permalinks. Thread results include both thread and parent-channel identity.

## Files

- Bundled plugin: `plugins/discord-history/` in the Hermes repository
- Installed plugin: `$HERMES_HOME/plugins/discord-history`
- Private state: `$HERMES_HOME/discord-history`
- Secret file: `$HERMES_HOME/secrets/discord-history.env`, root-owned mode `0600`
- Plugin configuration: `plugins.entries.discord-history.config` in Hermes `config.yaml`

Never print the secret file. The plugin loads it with no-follow, ownership, mode, size, key-name, DSN, and HMAC-key validation.

## Verification

```bash
hermes discord-history doctor --json
hermes discord-history status --guild <GUILD_ID> --json
hermes discord-history verify --channel <CHANNEL_ID> --json
hermes discord-history verify-e2e \
  --guild <GUILD_ID> \
  --owner-audit-id <AUDIT_ID> \
  --expected-message-id <MESSAGE_ID> \
  --json
```

Add `--expected-phrase '<EXACT_PHRASE>'` when the acceptance check should also
prove that exact text remains in the canonical message.

`verify-e2e` must return exit `0`, `ok=true`, and `verdict=PASS`. Without `--json`, successful output is exactly `PASS`.

See [operations](docs/operations.md) for sync and recovery procedures and [threat model](docs/threat-model.md) for trust boundaries.
