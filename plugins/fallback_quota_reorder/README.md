# fallback_quota_reorder

Reorders the top-level `fallback_providers` list in `config.yaml` based on quota readings parsed from the five Discord voice-channel names maintained by **quota_channels**.

## What it does

Each run:

1. Reads the five configured Discord voice channels (`codex`, `kimi`, `zai`, `grok`, `cursor`).
2. Parses remaining percentage and reset countdown from each channel name (strict regex; unreadable names are ignored for scoring). Precise `quota_channels` state beats the rounded name when fresh.
3. Scores each readable entry and reorders `fallback_providers`.
4. Writes `config.yaml` only when the desired order differs from the current order (unless frozen by staleness).

Silent success for the headless CLI; failures print `fallback-quota-reorder: <message>` and exit 1.

## Ordering policy

Chain entries are never renamed; the only membership change is the primary-rotation swap below. All other config keys are untouched.

1. **Healthy scored entries** — entries with a readable quota and `pct >= 5` (or any percentage with at least one pending reset, see below) sort by **highest score first**:

   `score = (quota_frac × 168/hours_remaining + resets × 168/hours_reset_expires) × rate_24h × rate_1h`

   - `hours_remaining` is `reset_seconds / 3600`, floored at one minute so a nearly-reset wallet is not divided by zero.
   - `quota_frac` is remaining percent / 100, clamped to `[0, 1]`.
   - Time enters **inversely**: a wallet resetting sooner is more urgent to spend now and scores higher; a wallet resetting in exactly 168 hours (one week, the reference horizon) scores its quota fraction 1:1, and anything later is diluted below it.
   - `rate_24h` / `rate_1h` are API success fractions from the last 24 hours and last hour. Fewer than 3 (24h) or 2 (1h) samples stays **1.0** so a quiet provider is not punished.

   **Pending usage-limit resets are additive.** Codex and Grok channels end with `• N reset(s)[ in <t>]`; each pending manual reset stacks one more full wallet (`quota_frac` of 1.0) onto the score on **its own expiry clock** — Codex's earliest still-available credit expiry, Grok's soonest token validity-end, both shown as `in <t>`. The invariant: **one pending reset at 0% remaining scores exactly like zero resets at 100% remaining when the two clocks are equal**, and each extra reset adds another full wallet with no cap. A reset whose expiry is unknown adds nothing: urgency is not measurable, so the usage-reset countdown is never borrowed as a stand-in — the credit stays visible as a count (and still keeps an emptied wallet out of the sink), it just earns no score term. The uptime factors multiply both terms, and providers without a resets API (Kimi, z.ai, Cursor, the OpenRouter virtual row) keep the remaining term alone — no bonus, no penalty.
2. **Low quota sink** — entries with parsed `pct` below 5 **and no pending resets** sink behind all healthy scored entries; among themselves they still sort by the same score, highest first. A 0% wallet with ≥1 pending reset still holds spendable capacity, so it stays in the healthy bucket.
3. **Unreadable tail** — entries with no readable quota (unmapped provider slug, unreadable channel name, or an `openrouter` model other than Ox Alpha) keep their relative order and go after all scored entries.

### Unlimited Ox Alpha route

The free/unlimited route `openrouter/stealth/ox-alpha` has no quota channel of its own, so it is scored from a **synthetic full-wallet reading**: exactly 100% quota against exactly 168 hours. With no reliability samples that scores exactly **1.0** — the neutral point of the formula — and observed uptime derates it through the same `rate_24h`/`rate_1h` factors as every other provider. Only the exact `openrouter` + `stealth/ox-alpha` pair (case-insensitive) gets the treatment; any other `openrouter` model without a real reading is an ordinary unscored tail entry.

The live gateway records each provider API success (`post_api_request`) and failure (`api_request_error`) into `HERMES_HOME/fallback_quota_reorder_reliability.jsonl`. The reorder tick reads that ledger. Until a provider has enough samples, ranking is just quota over time-to-reset.

## Primary rotation

The tick also rotates the **primary** model slot (`model.default` + `model.provider`), not just the chain order:

1. Candidates are the tracked providers with a reading plus the unlimited `openrouter/stealth/ox-alpha` route (through its synthetic reading), all scored with the same `score_provider` math as the chain. The current primary counts too — an untracked primary (e.g. a plain `openrouter` route) scores 0.
2. The highest score wins. Ties keep the current primary; ties between tracked providers go to the lowest channel index (`codex` → `kimi` → `zai` → `grok` → `cursor`), with the unlimited route competing after them so it only wins by beating the best tracked score outright. With no readings and no unlimited route the primary is left alone, and a winner with no `fallback_providers` entry to source its model string from is never promoted.
3. On a swap the winner's fallback entry graduates to `model.default`/`model.provider`, and the displaced previous primary is inserted back into the chain by the same bucket rules (healthy → low-quota → unscored, score descending within group). A displaced Ox Alpha primary re-enters by its synthetic score; any other untracked displaced primary (score 0) lands at the **end** of the chain.
4. The primary swap and the chain reorder are written in ONE `save_config` call, with the same backup/restore rollback and post-write verification as the chain-only path (verification re-checks both the chain signature and the primary keys).
5. The staleness freeze blocks primary writes too; `--force-quota` bypasses it.

`--dry-run` prints the pending swap:

```text
PRIMARY: openrouter/or -> openai-codex/codex
```

or `PRIMARY: unchanged <provider>/<model>` when readings exist but the current primary already wins. Nothing is printed about `PRIMARY` when there are no readings.

### Channel key → provider slug

| Channel key | Provider slug |
|-------------|---------------|
| `codex` | `openai-codex` |
| `kimi` | `kimi-coding` |
| `zai` | `zai` |
| `grok` | `xai-oauth` |
| `cursor` | `cursor` |

## Configuration

Requires an existing `quota_channels` section with all five channel IDs:

```yaml
quota_channels:
  quota_interval_seconds: 1800   # optional, default 1800
  channel_ids:
    codex: "VOICE_CHANNEL_ID"
    kimi: "VOICE_CHANNEL_ID"
    zai: "VOICE_CHANNEL_ID"
    grok: "VOICE_CHANNEL_ID"
    cursor: "VOICE_CHANNEL_ID"
```

Discord bot token (never commit real values):

| Secret | Location |
|--------|----------|
| Discord bot | `HERMES_HOME/.env` | `DISCORD_BOT_TOKEN=` |

## Staleness freeze

State is stored in `HERMES_HOME/fallback_quota_reorder_state.json` (previous raw channel names, timestamp, consecutive-stale counter).

When **all** channel names are byte-identical to the previous tick **and** any parsed reading has `reset_seconds <= 2 * quota_interval_seconds`, a consecutive-stale counter increments. After **two** consecutive stale ticks, config writes are frozen until channel names change again (which resets the counter).

Readings older than six hours never trigger staleness alone: if the previous tick timestamp is more than six hours old, that tick does not count as stale.

Use `--force-quota` to bypass the freeze for one run.

## Scheduling (self-installed systemd timer)

No manual cron setup. On Linux hosts with a systemd **user** manager, enabling this plugin is all it takes: on every gateway start it idempotently installs and reconciles a user-scope oneshot+timer pair (same pattern as **auto_update** and **dev_pipeline**):

- `~/.config/systemd/user/hermes-fallback-quota-reorder.service` — oneshot running `run.py` from the repo venv (fallback: the running interpreter), with `HERMES_HOME=%h/.hermes`.
- `~/.config/systemd/user/hermes-fallback-quota-reorder.timer` — `Persistent=true`, `WantedBy=timers.target`.

The schedule derives from `quota_channels.quota_interval_seconds` (default 1800) via `recommended_cron_spec`, converted to an `OnCalendar` expression. The default 1800s interval fires at minutes 2 and 32 of every hour (~120s after each 30-minute quota refresh):

```ini
OnCalendar=*-*-* *:02,32:00
```

```python
from plugins.fallback_quota_reorder.core import recommended_cron_spec
recommended_cron_spec(1800)  # -> '2,32 * * * *'
```

Units are rewritten only when their content actually changes; an unchanged, already-enabled timer is left alone. Disabling the plugin (`plugins.disabled` or `plugins.fallback_quota_reorder.enabled: false`) stops and disables the timer on the next gateway start. Non-Linux hosts or hosts without a reachable user manager skip self-install with a log line.

### Removing legacy cron entries

Older setups scheduled this plugin by hand. Both of these are obsolete and should be removed so the reorder does not run twice per tick:

- Hermes cron jobs named **"Quota-based fallback reorder"** (or similar): `hermes cron list` → `hermes cron remove <id>`.
- The copied script `~/.hermes/scripts/quota_reorder.py` — the timer runs `plugins/fallback_quota_reorder/run.py` from the repo directly; the copy is dead weight.

### Manual runs

Dry run (no writes, no state update):

```bash
python3 /path/to/hermes-agent/plugins/fallback_quota_reorder/run.py --dry-run
```

Force past staleness freeze:

```bash
python3 /path/to/hermes-agent/plugins/fallback_quota_reorder/run.py --force-quota
```

## Name formats (strict)

Standard: `Grok: 75% • 8h left`

With optional token enrichment from quota_channels: `Codex: 99% • 2.2B tok/7d • 7d left`

With optional trailing pending-reset segment (Codex/Grok): `Grok: 46% • 3d left • 1 reset in 2d` or `Codex: 100% • 7d left • 2 resets`

Cursor variant (pct = min of the two values): `Cursor: 76%/58% • 25d left`

Countdown units: `Nd`, `Nh`, `Nm` followed by ` left` (`d` = 86400s, `h` = 3600s, `m` = 60s); the same units follow `in` inside the resets segment. Any name that does not match the full anchored pattern — including truncation at Discord's 100-character limit — is treated as unreadable.
