# Usage Proxy Ledger Web UI

A small, read-only web dashboard for the Hermes usage-proxy SQLite ledger. It shows request/token summaries (last 24 hours and all-time) plus the most recent usage events. Built with Python stdlib only (`http.server` + `sqlite3`) — no external dependencies, no CDN assets.

**LAN-only, no authentication.** Bind to a private interface and do not port-forward this service to the public internet.

## Run

```bash
cd /root/.hermes/scratch/usage-proxy-webui
python3 server.py
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `192.168.30.20` | Bind address |
| `--port` | `9136` | HTTP port |
| `--db` | `/root/.hermes/usage-proxy/usage.sqlite` | Path to the ledger SQLite file (opened read-only) |

Example (local testing):

```bash
python3 server.py --host 127.0.0.1 --port 9136 --db /tmp/my-usage-copy.sqlite
```

### Routes

- `GET /` — HTML dashboard (auto-refreshes every 30 seconds)
- `GET /api/summary` — JSON summary (`last_24h`, `all_time`)
- `GET /api/events?limit=N` — JSON events (default 200, max 1000)

## systemd (optional, not installed by default)

See `usage-proxy-webui.service` in this directory. Example unit:

```ini
[Unit]
Description=Hermes usage-proxy ledger web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/.hermes/scratch/usage-proxy-webui
ExecStart=/usr/bin/python3 server.py --host 192.168.30.20 --port 9136
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Copy to `/etc/systemd/system/`, then `systemctl daemon-reload && systemctl enable --now usage-proxy-webui` if you want it managed — **only on a trusted LAN**.

## Security note

This UI has **no auth** and exposes usage metrics. Keep it on a private network (`192.168.x.x` or similar). **Do not** expose via reverse proxy or SSH port-forward to the public internet.