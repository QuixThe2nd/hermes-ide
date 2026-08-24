# fallback_quota_reorder

Reorders the top-level `fallback_providers` list in `config.yaml` based on quota readings parsed from the five Discord voice-channel names maintained by **quota_channels**.

## What it does

Each run:

1. Reads the five configured Discord voice channels (`codex`, `kimi`, `zai`, `grok`, `cursor`).
2. Parses remaining percentage and reset countdown from each channel name (strict regex; unreadable names are ignored for scoring).
3. Reorders `fallback_providers` using the fixed policy below.
4. Writes `config.yaml` only when the desired order differs from the current order (unless frozen by staleness).

Silent success for the headless CLI; failures print `fallback-quota-reorder: <message>` and exit 1.

## Ordering policy

Entries are never added, removed, or renamed. The primary model and all other config keys are untouched.

1. **OpenRouter first** — any entry with provider `openrouter` stays at the front (stable order among OpenRouter entries).
2. **Healthy scored entries** — remaining entries with a readable quota sort by parsed `reset_seconds` ascending (soonest reset first).
3. **Low quota sink** — entries with parsed `pct` below 5 sink behind all healthy scored entries; among themselves they still sort by `reset_seconds` ascending.
4. **Unreadable tail** — entries with no readable quota (unmapped provider slug or unreadable channel name) keep their relative order and go after all scored entries.

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

Cursor variant (pct = min of the two values): `Cursor: 76%/58% • 25d left`

Countdown units: `Nd`, `Nh`, `Nm` followed by ` left` (`d` = 86400s, `h` = 3600s, `m` = 60s). Any name that does not match the full anchored pattern — including truncation at Discord's 100-character limit — is treated as unreadable.
