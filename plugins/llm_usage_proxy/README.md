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
