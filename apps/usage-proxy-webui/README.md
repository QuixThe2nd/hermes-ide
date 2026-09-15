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

## Provider branding

Everywhere a model name appears — the model-usage donut and its legend, the hourly chart's **model** mode (columns, tooltip lines, screen-reader table) and the recent-events table — it carries its provider's brand: a small inline SVG logo beside the name and the provider's brand colour on the donut slices and per-model chart series. Matching is longest-prefix-first and case-insensitive on the model string:

| Prefixes | Provider | Colour |
|----------|----------|--------|
| `gpt-*`, `o3`, `o4-*`, `codex*` | ChatGPT/OpenAI (knot logo) | `#10A37F` |
| `glm-*`, `zai*` | Z.ai (initial badge) | `#8A8AF0` |
| `kimi-*` | Kimi / Moonshot AI (initial badge) | `#5A5AF5` |
| `claude-*` | Claude / Anthropic ("A" logo) | `#D97757` |
| `grok-*` | Grok / xAI (white X glyph) | `#BFC7D3` |
| `openrouter/*` | OpenRouter | `#6467F2` |

Z.ai's identity is monochrome, so the glyph renders near-white and the violet is the one slice/dot accent; Grok's X glyph renders white on the dark page. Logo path data comes from Simple Icons (CC0) where a dependable path exists; Z.ai and Kimi use a clean initial badge instead. Everything is inline in `server.py` — no image files, no runtime fetches. Models from unknown providers (and the folded "other" slice) keep the existing neutral palette; caller/harness chips represent client apps, not model providers, so they stay out of this table — they carry their own identity colours (below).

Because the `<canvas>` charts cannot read CSS values, each brand colour exists in both the Python table (`PROVIDER_BRANDS`) and the JS mirror (`PROVIDER_HEXES`) — the same pairing the neutral model palette uses (`MODEL_COLORS` / `MODEL_HEXES`). When one provider has several models in the same ring or column, repeats shade toward the card surface so same-brand neighbours stay distinguishable.

Harness/caller chips carry identity colours too — hermes blue (`#3987e5`), Claude orange for `claude-code`, OpenAI green for `codex`, teal for the `hindsight` family, plus OpenRouter and grok/xai — matched longest-prefix-first and case-insensitively on the caller string, so `hindsight-smoke`/`hindsight-migrate` fold onto the same teal and a known caller keeps its colour even when its rank shifts. Unknown callers keep the existing rank palette and `unattributed` stays neutral. The twin-table rule applies here as well: every hex lives in the Python `HARNESS_BRANDS` table, the JS `HARNESS_HEX` mirror and the `.hb-*` CSS rules.

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
