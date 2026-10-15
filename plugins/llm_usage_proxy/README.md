# llm_usage_proxy

A loopback reverse proxy that records **real** provider-reported token usage from
HTTP traffic on the wire — not estimates. It runs as its own systemd unit (one per
profile), forwards `http://127.0.0.1:<port>/p/<route>/<rest>` to the route's real
provider endpoint, and tees the usage object out of every response into a local
SQLite ledger (`<HERMES_HOME>/usage-proxy/usage.sqlite`). Measurement is on by
default per profile; opt out with `hermes llm_usage_proxy disable`, an explicit
`llm_usage_proxy.enabled: false`, or a `plugins.disabled` entry.

```bash
hermes llm_usage_proxy enable      # (re-)install + start this profile's unit
hermes llm_usage_proxy status      # bind address, SQLite path, unit state, routes
hermes llm_usage_proxy disable     # stop + disable (explicit opt-out)
hermes llm_usage_proxy reconcile   # rewrite the unit idempotently after config changes
hermes llm_usage_proxy serve       # foreground, no systemd (handy for testing)

curl -s localhost:8790/usage/summary   # rolling 24h totals, per upstream, per caller
curl -s 'localhost:8790/usage?limit=20'  # newest ledger rows
```

It only sees traffic that goes through Hermes's HTTP client seams; anything that
bypasses them (Cursor Cloud, an external CLI, a custom SDK client) is not captured,
and providers that omit usage fields are stored as nulls — unknown is never written
as a measured zero.

## Key-manager mode

Off by default, the proxy is a credential passthrough: it forwards whatever
`Authorization`/`x-api-key` the client sent, untouched, and accepts any caller. With
key-manager mode on (`hermes llm_usage_proxy manage-keys on`, which adds
`--manage-keys` to the unit argv), the proxy becomes the **only** holder of provider
keys. Clients authenticate with a *caller token* — a local token with no provider
meaning — and the proxy swaps it for the route's real credential before forwarding.
Every ledger row records which caller made the request in its `caller` column, so
`/usage/summary` can answer "who burned this window" rather than only "how much".

```bash
hermes llm_usage_proxy keys set zai --key sk-...          # repeat --key for rotation
hermes llm_usage_proxy keys list                          # route names + fingerprints only
hermes llm_usage_proxy keys remove zai --index 1          # drop one key (1-based, as listed)
hermes llm_usage_proxy callers create claudia             # prints the token ONCE
hermes llm_usage_proxy manage-keys on                     # writes config, reconciles the unit
```

Keys and tokens live in one root-only file, `<HERMES_HOME>/usage-proxy/keys.json`
(`0600` in a `0700` directory). A route may hold several keys — the proxy rotates
them round-robin per request, and on a `401`/`429` retries the request once with the
route's next key, so a rotated-out credential never surfaces to the client. Edits made
by the CLI are picked up by the running proxy without a restart. `keys list` shows
fingerprints only, never a key or a token; nothing ever logs a full key.

Three client recipes, using a caller token in place of a provider key:

```bash
# 1. Any OpenAI-compatible client: point base_url at the route, use the caller
#    token as the API key. GET /p/<route>/models and /p/<route>/v1/models are
#    forwarded like any request, so model pickers keep working.
export OPENAI_BASE_URL=http://127.0.0.1:8790/p/zai
export OPENAI_API_KEY=<caller token>

# 2. Anthropic-protocol clients (e.g. z.ai's Anthropic endpoint).
export ANTHROPIC_BASE_URL=http://127.0.0.1:8790/p/zai-anthropic
export ANTHROPIC_AUTH_TOKEN=<caller token>        # sent as Authorization: Bearer …
#    ANTHROPIC_API_KEY also works — it is sent as x-api-key, which the proxy
#    reads and replaces the same way.

# 3. Hindsight's local-mode LLM endpoint.
export HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:8790/p/zai
export HINDSIGHT_LLM_API_KEY=<caller token>
```

A caller token may also be presented in `X-Usage-Caller-Token`, which keeps a real
credential in `Authorization` free to pass through untouched — useful when a route
has no stored key. That header is a credential for the proxy alone and is always
stripped before anything is forwarded upstream.

Any client may also name itself with `X-Usage-Caller` — a label, not a secret, so it
is recorded in every mode, not just key-manager mode: the first 64 characters of
`[A-Za-z0-9._:-]`, ignored otherwise, and always stripped before forwarding. A
matched caller token wins when both are sent. The label never authenticates, so
sending one never causes a `401`; once caller tokens exist, the token is still
required. Hermes sets this header to `hermes` on the traffic it routes itself, and
keeps whatever label a client supplied.

Hermes's own routed transports additionally name the *chat* a request serves with
`X-Usage-Chat-Type` / `X-Usage-Chat-Id` / `X-Usage-Chat-Name` (the requesting
task's session identity: platform or local surface, durable chat id — with the
thread id embedded so threads stay distinct — and display name; cron jobs send
their durable job id/name, CLI/TUI sessions their session id). Same rules as the
caller label: validated (ids/names travel percent-encoded; unusable values are
ignored, never rewritten), recorded on the row in the `chat_type`/`chat_id`/
`chat_name` columns — additive migrations, so rows written before the columns
existed stay NULL and read as an explicit `Unknown` — and always stripped before
forwarding. The read-only dashboard in `apps/usage-proxy-webui` filters and
breaks down by these fields.

With no caller tokens configured the proxy accepts anything and attributes nothing,
which is what keeps Hermes's own in-process routing working unchanged. Once one
token exists, requests must present a known one: an unknown or missing token is a
`401`, and it is still a ledger row (`outcome: rejected`) rather than a silent gap.

**Not manageable:** OAuth-based providers — `openai-codex` and `xai-oauth` — have no
static API key to store, so key-manager mode cannot hold or inject credentials for
them. Their routes stay passthrough, and clients using them must keep their own
OAuth credentials.

Route names come from the proxy's route table (`hermes llm_usage_proxy status`).
Explicit `llm_usage_proxy.upstreams` config entries, the well-known provider
defaults, and each credential-pool entry's base URL all become routes.

## Per-harness routing policies

Off by default. With policies, the proxy — not the harness — chooses which
compatible upstream/model serves a request, per authenticated harness ("policy
caller"), per API family:

```bash
hermes llm_usage_proxy policy-callers create hermes     # prints the token ONCE
hermes llm_usage_proxy routing set hermes --family chat_completions \
    --entry zai:glm-5.3-flash --entry kimi:kimi-k3      # ordered: first usable wins
hermes llm_usage_proxy routing show                     # policies + live cooldowns
hermes llm_usage_proxy routing disable hermes           # entries kept, routing inert
```

* **API families, not provider names.** Policies are per family —
  `chat_completions`, `anthropic_messages`, `responses` — resolved from the exact
  request path. There is **no format translation**: an entry only serves a
  request of its own family, and only when the target route's endpoint actually
  serves that family. Endpoint capabilities come from a built-in table of the
  providers Hermes knows; declare any other endpoint's families explicitly with
  `llm_usage_proxy.route_capabilities.<route>` (a list or comma-separated
  string). A route with no declared capabilities cannot be targeted — the CLI
  rejects the entry at write time and the proxy skips it at request time,
  rather than guessing compatibility from a route's name. The Codex
  subscription Responses API is its own capability and is NOT interchangeable
  with an arbitrary OpenAI-compatible deployment.
* **Per-family route aliases.** One provider often needs multiple upstream
  bases: declare separate routes (for example `kimi` with
  `anthropic_messages` only versus `kimi-chat` with base
  `https://api.kimi.com/coding/v1` and `chat_completions` only). Policies
  reference the alias that matches the family; the arriving route's slug does
  not imply capability.
* **Chat Completions path on cross-route policy reroutes.** Clients normally
  call `/p/<route>/v1/chat/completions`. When a policy moves a Chat
  Completions request to a *different* route, the proxy rewrites that tail to
  `chat/completions` before prepending the candidate's base URL (so Z.AI gets
  `…/chat/completions`, Kimi chat gets `…/coding/v1/chat/completions`). Direct
  traffic and same-route policy hits keep `v1/chat/completions` verbatim;
  Anthropic `v1/messages`, Responses, and opaque paths are unchanged.
* **Operator priority, never reshuffled.** Entries are tried in saved order;
  quota scores never reorder them. The client-selected model is replaced by the
  chosen entry's model (everything else in the request body is preserved
  byte-for-byte), and cross-route moves inject the *target* route's stored key
  — the client's own credential is never forwarded to a different provider,
  and a local caller/policy token never travels upstream at all.
* **429 cooldowns.** A `429` received before anything was sent downstream marks
  a bounded persistent cooldown on exactly that `(route, model)` (honoring
  `Retry-After`, delta-seconds or HTTP-date, clamped to 1s–1h) and tries the
  next permitted candidate. If none remain, the client gets a `503` with a
  `Retry-After` scoped to its own candidates. Cooldowns survive proxy restarts
  and are shared process-safely with the CLI. Auth failures, 400s, 5xx and
  network errors never mark cooldowns and never retry.
* **Endpoint-owned state stays home — verifiably.** Requests carrying
  continuation state (`previous_response_id`, `conversation`, Responses
  replayed `encrypted_content`, Anthropic signed
  `thinking`/`redacted_thinking` blocks) are never moved across routes. The
  Responses family is **endpoint-local** outright: a Responses policy entry
  only ever serves requests that *arrived at its own route* (skip reason
  `endpoint_local`), because provider continuation state cannot be verified
  across Responses deployments and the Codex subscription Responses API is
  NOT interchangeable with a generic one — even a fresh Codex-native body is
  never routed to a generic Responses endpoint. For the other families, an
  authenticated policy caller's opaque continuation is served on the arriving
  route only when the caller's policy could *never* route that family
  elsewhere; when it could, the continuation's owner is not verifiable
  (concurrent conversations from one harness have different owners, so
  request history is not evidence) and the request is refused with an
  actionable `409` (`ambiguous_state`) instead of being silently labelled
  safe. When an enabled policy *does* control the family's opaque
  continuation, the request additionally needs independently verifiable
  upstream auth: stored keys are bound to a route, never to an account (and
  the round-robin cursor moves with unrelated traffic), so substituting a
  managed key could present the state under a different account than the one
  that created it. A policy-controlled pinned request that arrives with only
  a caller token on a stored-key route is therefore refused with an
  actionable `409` (`unverifiable_state_auth`) before anything is sent;
  presenting the provider credential that owns the conversation (alongside
  the caller token) serves it verbatim under *that* credential — the managed
  key is not substituted over it. One stored key / one route is not treated
  as proof of account identity. Legacy/no-policy/disabled-policy flows pass
  through untouched (managed-key substitution included), and pinned requests
  are forwarded byte-verbatim — never scrubbed — and are never re-presented
  under a second credential (no key-rotation retry).
* **Local tokens never leave.** Any `Authorization` bearer or `x-api-key`
  value matching a *local* credential (caller or policy token) is withheld
  from upstreams whichever header actually authenticated the request — a
  token duplicated into a second header is still recognized by value. When
  nothing independent remains and the route holds no stored key, the request
  is refused (`401`) or skipped (`no_upstream_auth`) rather than leaked.
* **Attribution.** A matched policy token decides both routing and the ledger's
  caller name — a forged `X-Usage-Caller` label can never steer a request or
  borrow another caller's policy. `/routing/policies`, `/routing/cooldowns`
  and `/routing/decisions` expose the live state (loopback, read-only, no
  secrets).

**Wiring Hermes itself as a policy caller:** mint the token, put it in the
profile's `.env` (secrets live there, never in `config.yaml`), and reference
the variable name from config:

```bash
hermes llm_usage_proxy policy-callers create hermes   # prints the token ONCE
echo 'HERMES_USAGE_PROXY_CALLER_TOKEN=<token>' >> ~/.hermes/.env
hermes config set llm_usage_proxy.caller_token_env HERMES_USAGE_PROXY_CALLER_TOKEN
```

Hermes's routed transports resolve the reference against their own profile's
secret store at client construction — through the *bound* profile's own
secret scope, never the ambient multiplex scope, so another profile's active
turn can never answer for it. A configured-but-unresolvable reference (or a
raw `caller_token` value in `config.yaml`, which is refused) raises a
sanitized `CallerTokenConfigError` at client construction: an opted-in
profile's traffic must fail loudly, never silently go out tokenless and
bypass its routing policy. The same fail-closed rule covers the config file
itself: a missing/empty `config.yaml` or a missing section is simply
unconfigured (legacy behavior), but a `config.yaml` that exists and cannot
be read or parsed — malformed YAML, a non-mapping top level or section, a
non-string reference — raises the same sanitized error rather than silently
becoming "unconfigured". Error messages name the file, key and defect type
only — never parser excerpts or config content, which may sit next to
secrets.

## Jev body capture (opt-in)

Off by default, nothing is captured. With `--capture-jev-bodies` on the proxy's
argv (or `llm_usage_proxy.capture_jev_bodies: true` in config — see the caveat
below), the proxy stores the **full request JSON and the complete
non-streaming response body** for jev traffic only, so a human can troubleshoot
a bad response from the actual bytes that crossed the wire.

Scope and honesty rules:

* Captured traffic is exactly: route `openrouter-alpha` **and** request model
  starting with `typesafe/jev-` (case-insensitive). Everything else — other
  routes, other model families — is never captured, even with the flag on.
* Bodies go to a **separate `jev_bodies` table** (request and response text,
  status, latency, request id, timestamps, and a `capture_state`); the usage
  ledger's shape and retention are untouched. The table is only created when
  the flag is on, so a default-off deployment's DB is byte-identical to one
  that never had the feature.
* Streaming (SSE) responses are never captured — the row stores the request
  plus an explicit `[response was streamed …]` marker, so a row can never read
  as a half-response. Bodies past the buffered cap are stored truncated with
  an explicit truncation marker; an abandoned or failed relay stores what it
  had with an `incomplete` marker.
* Retention is bounded: rows older than 24h are pruned opportunistically on
  insert, and the table is capped at 5000 rows (newest kept). The DB file
  keeps the same `0600` posture as the ledger.

The dashboard in `apps/usage-proxy-webui` lists the captures at `/captures`
(time, model, path, status, latency, state) with a detail page per row
rendering the pretty-printed request JSON and the raw response text —
escaped, monospace, read-only, same LAN-only posture as the rest of the
dashboard, linked from the main page and no auto-refresh.

**Config:** `capture_jev_bodies` in the plugin config (default off) is wired
through the systemd unit renderer: an enabled config adds
`--capture-jev-bodies` to the unit's `ExecStart` argv on the next
`hermes llm_usage_proxy enable`/update, or pass the flag directly to a manual
`hermes llm_usage_proxy serve --capture-jev-bodies` run.

## Security posture

* Binds `127.0.0.1` only. Never a LAN or Tailscale address.
* Never follows redirects; a 3xx `Location` passes through to the client, so a
  response cannot pivot the proxy onto another host.
* Forwards only to routes named in its table; `/p/<unknown>` is a 404, not a fetch.
* No raw access logging — request lines embed query strings, and query strings can
  carry keys. Failures are logged only after `redact_text` scrubs them.
* Provider keys are read from the key store at request time and are never written
  into the unit file, the argv, the ledger, or a log line. `keys set --key VALUE`
  is the one place a secret may sit in argv (shell history and `ps` can see it);
  pass `--key -` to read it from stdin instead.
* The ledger, WAL/SHM siblings, and key store are all `0600` from creation.
* Body capture is opt-in and narrow by construction: one route, one model
  family, its own table, bounded retention. With the flag off, no body is ever
  stored and the `jev_bodies` table is never created.
