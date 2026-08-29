# fallback_watch

Tails `HERMES_HOME/logs/agent.log` and posts an alert to a Discord channel whenever Hermes activates a fallback model — so a silent primary-model outage (rate limits, dead key, provider hiccup) becomes visible instead of just degrading every reply.

Service-only plugin: no model tools, no gateway hooks. It runs as its own long-lived process (`run.py`), which is what a systemd unit starts.

## What it watches

Log lines shaped like:

```
2026-08-25 15:32:15,579 INFO [20260825_153208_64d08c2b] agent.chat_completion_helpers: Fallback activated: stealth/ox-alpha → grok-4.6 (xai-oauth)
```

The session id is taken from the same line's `[YYYYMMDD_HHMMSS_hex]` bracket. The tail starts at **EOF** — enabling the watch never replays historical outages — and survives log rotation (inode change reopens the fresh file; in-place truncation rewinds to byte 0).

## Configuration

Section in `HERMES_HOME/config.yaml` (read directly by the plugin; it is not part of the agent's auto-loaded config schema, and absent means disabled):

```yaml
fallback_watch:
  enabled: false            # opt-in; off by default
  platform: discord         # optional, discord is the only wired target
  chat_id: ""               # Discord channel id that receives alerts
  cooldown_seconds: 120     # optional, one alert per window
  poll_seconds: 1.0         # optional, tail poll interval
```

`enabled: true` without `chat_id` fails with a clear error instead of silently not alerting. Same for any `platform` other than `discord`.

### Secrets

| Secret | Location | Key |
|--------|----------|-----|
| Discord bot token | `HERMES_HOME/.env` **or** `HERMES_HOME/secrets/discord.env` | `DISCORD_BOT_TOKEN` |

`.env` wins when both exist; resolution is the same file-reading path the other Discord plugins use (no process env involved). The token is never printed, logged, or included in any error text.

## Cooldown and state

One alert per `cooldown_seconds` window. Suppressed events are counted and the next alert that does go out carries:

```
Note: `N` additional fallback event(s) were suppressed during cooldown.
```

State lives in `HERMES_HOME/state/fallback_watch.json` (`last_alert_at`, `last_line`, `suppressed_since_last`) and is written atomically after every fallback event, so the cooldown window and dedup position survive restarts. A send failure drops that one event (no retry loop), keeps the suppressed tally, and backs off 10 s.

Alert format:

```
⚠️ Hermes primary model fallback activated
Primary: `stealth/ox-alpha`
Fallback: `grok-4.6` via `xai-oauth`
Session: `20260825_153208_64d08c2b`
Time: `2026-08-25 15:32:15,579`
```

Messages are plain REST POSTs to `/channels/{chat_id}/messages` with `allowed_mentions: {parse: []}` — a crafted log line can never turn an alert into a ping storm.

## Running

```bash
# validate config + token, then exit
python -m plugins.fallback_watch.run --check

# watch until SIGTERM/SIGINT (exit 0 when disabled)
python -m plugins.fallback_watch.run
```

Both take `--config /path/to/config.yaml` (default `HERMES_HOME/config.yaml`). A missing token fails fast at startup rather than retrying every event.

### systemd unit sketch

User-scope unit, saved as `~/.config/systemd/user/hermes-fallback-watch.service`:

```ini
[Unit]
Description=Hermes fallback watch — alert Discord on primary-model fallback

[Service]
Type=simple
WorkingDirectory=/path/to/hermes-agent
ExecStart=/path/to/hermes-agent/.venv/bin/python -m plugins.fallback_watch.run
Environment=HERMES_HOME=%h/.hermes
Restart=on-failure

[Install]
WantedBy=default.target
```

Enable with the usual `loginctl enable-linger` + `systemctl --user` dance for user units. SIGTERM shuts the watcher down cleanly (exit 0), so unit restarts are safe: the state file picks the cooldown back up where it left off.

## Tests

`tests/plugins/fallback_watch/` — config parsing/validation, line parsing + session extraction, alert formatting, cooldown suppression counting, state round-trip, rotation handling, disabled/unconfigured no-send paths, and an end-to-end run over a real log file.
