# quota_channels

Discord voice-channel model quota display for **Codex**, **Kimi**, **z.ai**, **Cursor**, **Grok**, and **OpenRouter** under a **Models** category. Renames one configured voice channel per provider with remaining quota percentages, a granular time-until-reset countdown (days at 2+ days out, then hours, then minutes), rolling 7-day consumed tokens for Codex, z.ai, and Cursor, and pending usage-limit resets for Codex and Grok — all in the same channel name. Channels are ordered by the same spendability score the fallback router uses (see below), and the category label stays fresh between cron ticks.

## What it does

Each tick (typically every minute via cron):

1. **Quota gate** — provider API fetches run at most every `quota_interval_seconds` (default 30 minutes) unless forced. State lives in `HERMES_HOME/quota_channels_state.json`.
2. **On a quota run** — fetch all enabled providers, rename their voice channels (quota + token segment where supported), sort by score (descending, see below), and save state.
3. **Every tick** — update the Models category name once with the absolute local timestamp of the last successful quota run and either the next scheduled run time or `Due` when the interval has elapsed. Format: `Models • <day/month hour:minam/pm> • Next: <hour:minam/pm|Due>`.

Silent success for the headless CLI; failures print `quota-channels: <message>` and exit 1.

## Channel ordering

Voice channels are ordered by the **same policy as `fallback_quota_reorder`** (the implementation is shared, not duplicated): healthy entries sort by descending

`score = (quota_frac × 168/hours_remaining + resets × 168/hours_reset_expires) × rate_24h × rate_1h`

where the uptime factors come from the fallback reliability ledger (`HERMES_HOME/fallback_quota_reorder_reliability.jsonl`) with its usual sample thresholds — a provider with too few samples stays neutral at 1.0. Discord order therefore uses exactly the score failover uses, **pending usage-limit resets included for Codex and Grok**: each pending reset adds one full wallet on its own expiry clock (Grok's soonest token expiry, or the usage-reset countdown when the provider has none), so a `0% • 1 reset in 1h` row outranks a `100% • 7d left` one. The reset counts and their expiry are persisted in `quota_channels_state.json` alongside each reading (`reset_count`, `reset_expiry_seconds`) so the fallback reorder scores the same wallet from precise state as from the channel name; rows without a resets API keep the remaining term alone. Entries below 5% with no pending resets sink behind all healthy entries (still by score among themselves) — a 0% wallet with a pending reset is equivalent to a full one and stays healthy — and ties keep the spec order (Codex, Kimi, z.ai, Cursor, Grok, OpenRouter). Quota keys map to routing providers `codex→openai-codex`, `kimi→kimi-coding`, `zai→zai`, `cursor→cursor`, `grok→xai-oauth`, `openrouter→openrouter`. The current primary model stays in the display and simply sorts by its own score.

### The virtual OpenRouter row

`OpenRouter` is a virtual row for the unlimited Ox Alpha model (`openrouter/stealth/ox-alpha`). There is no quota API to call: the channel carries the managed name `OpenRouter: 100% • Unlimited`, and its state reading is a synthetic full wallet — 100% against exactly 168 hours — so with neutral uptime it scores exactly 1.0 and observed uptime derates it through the same factors as everyone else.

## Configuration

Add a `quota_channels:` section to `config.yaml`:

```yaml
quota_channels:
  guild_id: "YOUR_GUILD_ID"
  category_id: "YOUR_CATEGORY_CHANNEL_ID"
  quota_interval_seconds: 1800   # optional, default 1800 (30 min)
  post_quota_delay_seconds: 31   # deprecated; ignored (kept for backward compatibility)
  channel_ids:
    codex: "VOICE_CHANNEL_ID"
    kimi: "VOICE_CHANNEL_ID"
    zai: "VOICE_CHANNEL_ID"
    cursor: "VOICE_CHANNEL_ID"
    grok: "VOICE_CHANNEL_ID"
    openrouter: "VOICE_CHANNEL_ID"
  enabled_providers:             # optional; default = the wired rows
    codex: true
    kimi: true
    zai: true
    cursor: true
    grok: true
    openrouter: true
```

`enabled_providers` may also be a list, e.g. `["codex", "kimi"]`.

**Upgrade note:** a config written before OpenRouter existed (the original five channel IDs, no `enabled_providers`) keeps validating and running unchanged; the OpenRouter row auto-enables as soon as its channel ID is wired. An explicit `enabled_providers` list or map still controls every row, OpenRouter included.

**Upgrade note:** Updating the plugin automatically enriches existing quota channels for Codex, z.ai, and Cursor with a `<compact> tok/7d` segment — no config changes required.

## Rolling 7-day token enrichment

Each provider has **one** voice channel. For Codex, z.ai, and Cursor the channel name includes quota fields plus a rolling 7-day token total between the percentage segment and the reset countdown, e.g. `Codex: 99% • 2.2B tok/7d • 7d left`. Kimi and Grok have no account-wide consumed-token API; their channels stay quota-only and make **no** token-related HTTP request. OpenRouter is virtual — no quota or token HTTP request at all.

| Provider | Source | Notes |
|----------|--------|-------|
| Codex | `GET …/wham/profiles/me` → sum latest 7 calendar-day `stats.daily_usage_buckets` | Stats may lag ~1 day (`stats_as_of`); OAuth refresh on 401 |
| z.ai | `GET …/model-usage` with UTC `startTime`/`endTime` as `yyyy-MM-dd HH:mm:ss` | HTTP 200 with empty body is an error, not zero |
| Cursor | `POST …/GetAggregatedUsageEvents` (epoch-ms strings, now−7d..now) | Total = input + output only; cache tokens excluded |
| Kimi, Grok | — | Quota-only channel names; no token HTTP call |
| OpenRouter | — | Virtual unlimited row; managed name, no HTTP call |

If a token fetch fails, the channel is still renamed with fresh quota data. When the current name already contains a parseable `tok/7d` segment, that segment is preserved; otherwise the name is quota-only. Token failures never block other providers, sorting, or the category update. Quota fetch failures leave that channel completely unchanged.

Enable the toolset for sessions that should call the tool:

```bash
hermes tools   # enable "Quota Channels" / quota_channels
```

## Pending usage-limit resets

Codex and Grok channel names end with a count of still-unused manual usage-limit resets; Grok also shows when the soonest one expires:

- Codex — `Codex: 100% • 7d left • 2 resets` (from `rate_limit_reset_credits` in the same `wham/usage` payload)
- Grok — `Grok: 46% • 3d left • 1 reset in 2d` (second gRPC-web call to `ConsumerUiSvc/GetRemainingResets`)

A failed or unparseable resets lookup never fails the provider's tick: Codex drops the segment, Grok falls back to `0 resets`, and the channel is still renamed with fresh quota data.

## Credentials (never commit real values)

| Provider | Location | Notes |
|----------|----------|-------|
| Discord bot | `HERMES_HOME/secrets/discord.env` | `DISCORD_BOT_TOKEN=` |
| Kimi | `HERMES_HOME/.env` | `KIMI_API_KEY=` |
| z.ai | `HERMES_HOME/secrets/zai.env` | `ZAI_API_KEY=` (raw Authorization header) |
| Codex | `HERMES_HOME/auth.json` | `providers.openai-codex.tokens` (OAuth refresh on 401) |
| Grok | `HERMES_HOME/auth.json` | `providers.xai-oauth.tokens` (OAuth refresh once on 401) |
| Cursor | `~/.config/cursor/auth.json` | `accessToken` JWT (re-run `agent login` on 401) |

## Cron setup

Run every minute without invoking the agent. Use an absolute path to `run.py` so the script bootstraps repo imports from any working directory; `python3 -m plugins.quota_channels.run` only works from the repo root (or with `PYTHONPATH` set).

```bash
hermes cron add \
  --schedule "every 1m" \
  --script "python3 /path/to/hermes-agent/plugins/quota_channels/run.py" \
  --no-agent \
  --name "quota-channels"
```

Force a quota fetch on one run:

```bash
python3 /path/to/hermes-agent/plugins/quota_channels/run.py --force-quota
```

Debug JSON on success:

```bash
python3 /path/to/hermes-agent/plugins/quota_channels/run.py --debug
```

## Tool

When the `quota_channels` toolset is enabled, the model may call:

- **`quota_channels_tick`** — one tick; optional `force: true` bypasses the quota gate.

Returns compact JSON, e.g. `{"success":true,"did_quota":true,"providers":{...},"category":"renamed","sorted":false}`.

When Grok's billing config omits the usage ratio (proto3 default 0) and carries a valid current usage-period marker (config field 8 type 1 or 2) with a valid reset timestamp, the Grok voice channel is renamed to `Grok: 100% • Nd left • 0 resets`. If the ratio is absent without that evidence, the provider fails honestly with an error instead of fabricating a percentage.

Per-provider failures are isolated: a failing provider appears as `{"error": "..."}` under `providers` in debug JSON and does not block other providers. If every provider fails on a quota run, state is not advanced and channel sorting is skipped.
