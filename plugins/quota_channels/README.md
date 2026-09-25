# quota_channels

Discord voice-channel model quota display for **Codex**, **Kimi**, **z.ai**, **Cursor**, and **Grok** under a **Models** category. Renames configured voice channels with remaining quota percentages, a granular time-until-reset countdown (days at 2+ days out, then hours, then minutes), rolling 7-day consumed tokens for Codex, z.ai, and Cursor, and pending usage-limit resets for Codex, Grok, and z.ai — all in the channel name. Codex, Kimi, Cursor, and Grok each have one voice row; **z.ai has one row per credential-pool wallet** (`z.ai 1`, `z.ai 2`, …) keyed by the immutable pool entry id. Channels are ordered by the same spendability score the fallback router uses (see below), and the category label stays fresh between cron ticks.

## What it does

Each tick (typically every minute via cron):

1. **Quota gate** — provider API fetches run at most every `quota_interval_seconds` (default 30 minutes) unless forced. State lives in `HERMES_HOME/quota_channels_state.json`.
2. **On a quota run** — fetch all enabled providers, rename their voice channels (quota + token segment where supported), sort by score (descending, see below), and save state.
3. **Every tick** — update the Models category name once with the absolute local timestamp of the last successful quota run and either the next scheduled run time or `Due` when the interval has elapsed. Format: `Models • <day/month hour:minam/pm> • Next: <hour:minam/pm|Due>`.

Silent success for the headless CLI; failures print `quota-channels: <message>` and exit 1.

## Channel ordering

Voice channels are ordered by the **same policy as `fallback_quota_reorder`** (the implementation is shared, not duplicated): healthy entries sort by descending

`score = (quota_frac × 168/hours_remaining + Σ_credits 168/hours_credit_expires) × rate_24h × rate_1h`

where the uptime factors come from the fallback reliability ledger (`HERMES_HOME/fallback_quota_reorder_reliability.jsonl`) with its usual sample thresholds — a provider with too few samples stays neutral at 1.0. Discord order therefore uses exactly the score failover uses, **pending usage-limit resets included for Codex, Grok, and z.ai**: each pending reset adds one full wallet on **its own expiry clock**, so a `0% • 1 reset in 1h` row outranks a `100% • 7d left` one. For Codex and z.ai the per-credit clocks come from the precise state row (`reset_expiry_horizons`) — two credits expiring in 3d and 9d score `168/72 + 168/216`, never `2 × 168/72`; the single `in <t>` countdown the channel shows is the **earliest** expiry and is display-only. Grok reports one soonest token expiry, which each of its resets spends on (the legacy single-clock shape — also what a Codex or z.ai row without the richer list keeps meaning). A reset whose expiry is unknown adds nothing to the score — the usage-reset countdown is never borrowed as a stand-in, because urgency would not be measurable — but it still renders as a count and still keeps a 0% wallet out of the low-quota sink. The reset counts, the display expiry, and the per-credit horizons are persisted in `quota_channels_state.json` alongside each reading (`reset_count`, `reset_expiry_seconds`, `reset_expiry_horizons`) so the fallback reorder scores the same wallet from precise state as from the channel name; rows without a resets API keep the remaining term alone. Entries below 5% with no pending resets sink behind all healthy entries (still by score among themselves) — a 0% wallet with a pending reset is treated as spendable capacity and stays healthy — and ties keep the spec order (Codex, Kimi, z.ai, Cursor, Grok). Quota keys map to routing providers `codex→openai-codex`, `kimi→kimi-coding`, `zai→zai`, `cursor→cursor`, `grok→xai-oauth`. The current primary model stays in the display and simply sorts by its own score.

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
  enabled_providers:             # optional; default = every wired row
    codex: true
    kimi: true
    zai: true
    cursor: true
    grok: true
```

`enabled_providers` may also be a list, e.g. `["codex", "kimi"]`.

**Upgrade note:** a config that still carries a `channel_ids.openrouter` entry (the retired virtual row) keeps validating and running unchanged; the leftover key activates nothing and can be deleted whenever convenient.

**Upgrade note:** Updating the plugin automatically enriches existing quota channels for Codex, z.ai, and Cursor with a `<compact> tok/7d` segment — no config changes required.

**Upgrade note (1.4.0):** Multiple Z.AI credentials in `auth.json` `credential_pool.zai` each get their own numbered voice row. On first run with extra wallets, the legacy `channel_ids.zai` channel binds to the first wallet (stable pool order after in-memory key dedupe); additional wallets get new voice channels under `category_id`. Ordinal numbers bind once to entry ids in `quota_channels_state.json` and are never reclaimed. `readings.zai` remains a single alias for the best currently spendable wallet so `fallback_quota_reorder` keeps working unchanged.

## Rolling 7-day token enrichment

Codex, Kimi, Cursor, and Grok each have **one** voice row. For Codex, z.ai, and Cursor the channel name includes quota fields plus a rolling 7-day token total between the percentage segment and the reset countdown, e.g. `Codex: 99% • 2.2B tok/7d • 7d left` and `z.ai 2: 74% • 250.0M tok/7d • 4d left`. Kimi and Grok have no account-wide consumed-token API; their channels stay quota-only and make **no** token-related HTTP request.

| Provider | Source | Notes |
|----------|--------|-------|
| Codex | `GET …/wham/profiles/me` → sum latest 7 calendar-day `stats.daily_usage_buckets` | Stats may lag ~1 day (`stats_as_of`); OAuth refresh on 401 |
| z.ai | `GET …/model-usage` with UTC `startTime`/`endTime` as `yyyy-MM-dd HH:mm:ss` | HTTP 200 with empty body is an error, not zero |
| Cursor | `POST …/GetAggregatedUsageEvents` (epoch-ms strings, now−7d..now) | Total = input + output only; cache tokens excluded |
| Kimi, Grok | — | Quota-only channel names; no token HTTP call |

If a token fetch fails, the channel is still renamed with fresh quota data. When the current name already contains a parseable `tok/7d` segment, that segment is preserved; otherwise the name is quota-only. Token failures never block other providers, sorting, or the category update. Quota fetch failures leave that channel completely unchanged.

Enable the toolset for sessions that should call the tool:

```bash
hermes tools   # enable "Quota Channels" / quota_channels
```

## Pending usage-limit resets

Codex, Grok, and z.ai channel names end with a count of still-unused manual usage-limit resets plus, when known, when the soonest one expires:

- Codex — `Codex: 100% • 7d left • 1 reset in 22d`
- Grok — `Grok: 46% • 3d left • 1 reset in 2d` (second gRPC-web call to `ConsumerUiSvc/GetRemainingResets`)
- z.ai — `z.ai: 0% • 4d left • 1 reset in 30d` (read-only `GET …/biz/customer-package-reset/list?targetType=PERSONAL` with the same raw `Authorization` key as the usage read; the mutating `…/use` endpoint is never called)

Codex reads two payloads. `rate_limit_reset_credits` in the `wham/usage` response gives the available total; when that total is nonzero, `GET …/wham/rate-limit-reset-credits` — the same authenticated Codex path, with the usual OAuth refresh on 401 — gives the per-credit detail behind it. Only credits with `reset_type: codex_rate_limits`, `status: available`, **and** an `expires_at` still in the future count; a credit whose readable `expires_at` has already passed cannot be spent, so it is neither counted nor scored. Each counted credit's own future expiry is preserved (`reset_expiry_horizons` in the state reading, earliest first) so the score spends every credit on its own clock; the countdown shown in the channel name is the earliest of them — a compact display, never a clock the whole stack shares. The details payload wins over the usage total, so a credit that is not genuinely spendable for Codex is not counted.

z.ai matches its reset cards to the quota window the row represents: the usual weekly row (usage-limit `unit: 6`) counts `weekResets` — a weekly reset refills both the weekly and the 5h window — while a payload whose longest window is the 5h one (`unit: 3`) counts `fiveHourResets`, so a 5h-only reset is never scored as a weekly full wallet. Only cards with `available: true` and an `expireTime` still in the future count; the naive `YYYY-MM-DD HH:MM:SS` expiry is Z.AI platform time, read consistently as UTC+8 (Singapore/China). Each usable card keeps its own expiry horizon for the score, exactly like Codex credits.

A failed or unparseable resets lookup never fails the provider's tick: Codex keeps the count from the usage payload and drops the countdown, z.ai drops the segment and persists no reset fields (both recorded as `reset_error` in debug JSON), Grok falls back to `0 resets`, and the channel is still renamed with fresh quota data. A credit whose expiry is missing or malformed is still counted, but renders without a countdown and scores no reset term — the quota window is never used as a stand-in clock.

## Credentials (never commit real values)

| Provider | Location | Notes |
|----------|----------|-------|
| Discord bot | `HERMES_HOME/secrets/discord.env` | `DISCORD_BOT_TOKEN=` |
| Kimi | `HERMES_HOME/.env` | `KIMI_API_KEY=` |
| z.ai | `HERMES_HOME/secrets/zai.env` **or** `HERMES_HOME/auth.json` `credential_pool.zai[]` | `ZAI_API_KEY=` (raw Authorization header) when the pool is empty; one wallet row per pool entry (immutable `id`) when the pool is populated. Duplicate exact runtime keys are deduped in memory only (first id wins). |
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
