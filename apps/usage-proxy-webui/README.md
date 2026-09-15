# Usage Proxy Ledger Web UI

A small, read-only web dashboard for the Hermes usage-proxy SQLite ledger. It shows request/token summaries (last 24 hours and all-time) broken down by model and by harness (`usage_events.caller`), plus the most recent usage events. Events with no recorded caller render as `unattributed`. Built with Python stdlib only (`http.server` + `sqlite3`) — no external dependencies, no CDN assets.

**LAN-only, no authentication.** Bind to a private interface and do not port-forward this service to the public internet.

## Run

```bash
python3 server.py
```

Defaults: binds `127.0.0.1:9136` and reads `~/.hermes/usage-proxy/usage.sqlite`.

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `9136` | HTTP port |
| `--db` | `~/.hermes/usage-proxy/usage.sqlite` | Path to the ledger SQLite file (opened read-only) |

Example:

```bash
python3 server.py --host 127.0.0.1 --port 9136 --db /tmp/my-usage-copy.sqlite
```

### Routes

- `GET /` — HTML dashboard (auto-refreshes every 30 seconds)
- `GET /api/summary` — JSON summary (`last_24h`, `all_time`)
- `GET /api/events?limit=N` — JSON events (default 200, max 1000)
- `GET /api/timeseries` — JSON hourly buckets for the chart (last 24 h, per-harness/model splits)

## Hourly chart

The "Tokens per hour" stacked column chart has a segmented toggle with four breakdown modes:

- **harness** / **model** — tokens stacked per harness or per model (stable per-name colors)
- **in/out** — prompt tokens (input) vs completion tokens (output)
- **cache** — cached vs uncached prompt tokens (uncached = prompt − cached; columns are shorter because they sum prompt tokens only)

Tooltip, the screen-reader table, and aria-labels follow the active mode.

## systemd (optional, not installed by default)

Example unit:

```ini
[Unit]
Description=Hermes usage-proxy ledger web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/usage-proxy-webui
ExecStart=/usr/bin/python3 server.py --port 9136
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Copy to `/etc/systemd/system/`, adjust `--host` to a private interface if you want LAN access, then `systemctl daemon-reload && systemctl enable --now usage-proxy-webui` — **only on a trusted LAN**.

## Security note

This UI has **no auth** and exposes usage metrics. Keep it on a private network (`192.168.x.x` or similar). **Do not** expose via reverse proxy or SSH port-forward to the public internet.
