# Threat Model

## Assets

- Discord message content, revisions, stable IDs, observed display names, timestamps, and channel topology
- Discord bot token, PostgreSQL DSN, and 32-byte audit HMAC key
- Authorization scope for the configured owner, guilds, root channels, and current Discord thread
- Audit evidence proving who searched, with which non-query filters, and which message IDs were delivered

## Trust boundaries

1. **Discord gateway to plugin:** caller identity and chat/thread scope come only from directly bound task-local `ContextVar` values. Environment variables, session caches, and model arguments are not authorization sources.
2. **Plugin to PostgreSQL:** only fixed parameterized SQL is used. No table, column, operator, ordering, or SQL fragment crosses the model-tool boundary.
3. **Plugin to Discord/DCE:** Discord inventory and DCE JSON are untrusted structured input. IDs are strict ASCII decimal strings; payload shape, parent provenance, cursor progress, counts, timestamps, and export metadata are validated fail-closed.
4. **Filesystem:** secrets, logs, temporary exports, binaries, and plugin state are root-owned private regular files/directories. Sensitive opens use no-follow checks where applicable.
5. **Database transport:** a loopback-only systemd socket starts a hardened `ssh -W` worker per PostgreSQL connection. The worker uses `DynamicUser`, systemd credentials, an empty capability set, and syscall, namespace, device, process, filesystem, and destination-IP restrictions. It exits with that connection, so there is no persistent transport daemon. It has no Discord token, DCE invocation, import loop, schedule, or model surface and does not make PostgreSQL publicly reachable.

## Authorization

Authorization binds provider, configured owner, requested guild, configured root/current chat, current thread, and action. Missing, internal `_UNSET`, empty, malformed, Unicode-lookalike, non-Discord, non-owner, wrong-scope, unsupported-action, and tool-name-collision cases fail before constructing a database connection. Denials return one generic model-facing error and append a private HMAC-only event.

## Confidentiality

- Secret values and connection strings are never returned or logged.
- Search audit stores HMAC-derived principal identity and keyed query fingerprint, not raw principal IDs or query text.
- Result audit IDs are recorded only after final row/byte trimming and therefore equal the messages actually delivered.
- Retrieval selects only required bounded fields. It does not materialize message `raw_json` or expose tombstones.
- A coverage miss is conclusive only for complete scope and period; partial or stale coverage is reported honestly.

## Integrity

- DCE version and release archive checksum are pinned.
- The full DCE distribution is staged privately and switched atomically. Late failure restores the prior tree and removes all temporary symlinks/directories.
- Inventory completeness is tied to endpoint manifests, contiguous cursor pages, fingerprints, parent unions, and the exact reconciliation run.
- Reconciliation tombstones only messages older than the fixed export cutoff.
- Reimports preserve one canonical row and create immutable revisions only when content changes.
- Search audit is owned by a separate `NOLOGIN` role, append-only by trigger, and not mutable by the application role.

## Availability and resource bounds

- Resolver/status expansion fails closed above 1,000 channels.
- Search/get/context/status records, context windows, snippets, revisions, and final UTF-8 JSON are bounded.
- Context is removed farthest from its associated hit first; primary hits are retained until no context remains. Omission counters and truncation flags remain truthful.
- Discord retries are limited to transient failures. Non-progressing/repeated cursors terminate with incomplete inventory rather than loop indefinitely.

## Explicitly out of scope

- Recovering content deleted before any complete archive observation
- Authorizing non-owner users or delegated roles
- Arbitrary SQL, raw archive browsing, write actions, or restoration into Discord
- A separate MCP service, plugin ingestion daemon, unapproved cron job, or Hermes core fork
- Semantic/vector retrieval until its separate labelled evaluation threshold is met

## Residual risks

The owner can retrieve content from configured archive scope, as intended. PostgreSQL administrators and root can access stored data. Mutable Discord display names are snapshots and are not identity proof; stable IDs are returned when identity matters. Discord/DCE/API behavior can change, so doctor, fixed-cutoff verification, and fresh adversarial acceptance remain release gates.
