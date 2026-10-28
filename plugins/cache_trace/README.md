# cache_trace — opt-in, metadata-only cache trace

A bundled observability plugin that records, per API attempt, enough metadata
to correlate **request shape**, **component stability** and **observed cache
usage/timing**. The hooks do receive the full lifecycle payload (raw
request/response kwargs), but the collector stores ONLY allowlisted metadata —
never prompt text, tool arguments, response content, credentials or exception
messages — and never changes request behavior.

## Enable / disable

Off by default. Enable per profile (this writes `plugins.enabled` into that
profile's `config.yaml`):

```bash
hermes plugins enable cache_trace     # or add "cache_trace" to plugins.enabled
hermes plugins disable cache_trace    # off again
```

## Output

`<profile home>/logs/cache-trace/trace.jsonl` — append-only JSON lines,
rotated at 2 MiB, 3 rotations kept (`trace.jsonl.1..3`), file mode 0600 /
directory 0700. Existing trace files and backups are tightened to these modes before writing.

String values over 4,096 UTF-8 bytes are replaced by an explicit `__oversized__` marker, SHA-256 and byte count. A record that still exceeds the file cap is replaced by a bounded `collector_oversize_drop` diagnostic. JSON lines are never cut mid-record.

Normal records use one line per lifecycle event:

- `pre_api_request` — ids (`task/turn/api_request/session`), model/provider/
  api_mode, retry & call counts, timing start, and `request_metadata`:
  SHA256 + UTF-8 byte/item counts for the ordered `tools`, `system` and
  `messages` components (with `messages_embedded` subtotals for
  system/developer messages carried inside `messages`, labeled by source so
  totals are never double counted), plus bounded per-message fingerprints and
  a `prefix_comparison` against the previous request in the same session AND
  profile-home scope (shared prefix length, first changed message index;
  `coverage` is `partial` when either side was truncated or skipped an
  unserializable message — indices past a gap are not comparable).
- `post_api_request` — duration, `first_chunk_at` (only when the transport
  actually reported a stream chunk; `first_chunk_available` marks it — API
  duration is NOT TTFT), finish reason, response model/id, assistant
  content LENGTH and tool-call COUNT (never content), and
  `usage_cache_presence`: raw client-usage cache fields (top-level and the
  nested `prompt_tokens_details` / `input_tokens_details` — Chat Completions
  and Responses shapes) classified as `null` / `zero` / `positive` /
  `invalid` (negative counts, bools, non-numbers, NaN/Infinity) before Hermes
  normalization. Invalid values use fixed reason labels rather than arbitrary text;
  absence (client never sent the field, including pydantic
  unset defaults) is reported as absent, never as zero.
- `api_request_error` — error TYPE, status code, retry counters. Never the
  message or exception.

## Limitations

- Fingerprints describe the **canonical client-side, pre-transport
  representation** (the kwargs Hermes hands the provider SDK) — not the exact
  wire bytes and not tokens. SDK serialization, proxies and upstream
  normalization are unobserved; client-reported cache fields cannot prove
  pre-normalization upstream cache presence.
- **Missing cache telemetry is unknown, not zero.** An absent field never
  appears as a zero.
- Per-message fingerprints are capped (512 messages); `coverage`/`
  per_message_truncated` say when a comparison or component is partial.
- The collector fails open: if the log path is unwritable, records are
  dropped (one WARNING) and API calls proceed unchanged. Short writes are
  completed or the failed append is rolled back to its previous file length.
  This is not a crash-durable transaction or protection against an unrecoverable
  filesystem failure.
- Local file only — no network, no background service, no dashboards.
