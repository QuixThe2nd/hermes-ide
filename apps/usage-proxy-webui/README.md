# Usage Proxy Ledger Web UI

A small, read-only web dashboard (the "AI Usage" page) for the Hermes usage-proxy SQLite ledger. One shared filter state — time range (24 h / 7 d / 30 d / all), harness, provider (the ledger's actual upstream route), model, chat type, chat, route and outcome — narrows every card, chart, breakdown and event list at once; the state lives in the page URL so a refresh keeps it and a copied link reproduces it. Per-chat rows come from the ledger's `chat_type`/`chat_id`/`chat_name` columns (actual recorded identity only — threads stay their own chats, cron jobs and CLI sessions fall back to their durable job/session ids, and anything written before those columns existed is an explicit `Unknown`; nothing is inferred from timestamps or models). Harness names are display-mapped: the gateway's `hermes` caller renders as **Hermes IDE** and per-profile `hermes:<profile>` callers as **Hermes IDE · \<profile\>**; raw caller strings remain the JSON keys, filter values and colour-hash inputs everywhere. In the per-harness table, profile callers render as indented subrows under a Hermes IDE parent row whose totals also fold in any pre-split plain `hermes` traffic — the subtotal is display-only (never added into any total) and not clickable, because the harness filter matches one exact caller; the profile subrows drill to their exact caller. Events with no recorded caller render as `unattributed`. Built with Python stdlib only (`http.server` + `sqlite3`) — no external dependencies, no CDN assets.

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

All routes accept the shared filter query (`?range=24h|7d|30d|all&harness=…&provider=…&model=…&type=…&chat=…&route=…&outcome=…`; chat keys are `<type>:<id>`, a bare `<type>`, or `unknown`).

- `GET /` — HTML dashboard (polls every 5 s; first paint server-rendered under the same filters)
- `GET /api/summary` — JSON totals, facets and breakdowns over the filtered window
- `GET /api/events?limit=N` — JSON events, filtered before the limit (default 200, max 1000)
- `GET /api/timeseries` — JSON buckets for the chart (granularity follows the range; per-harness/model and per-chat splits)

Filters combine with AND; each facet's option list is computed over all *other* filters so it stays usable when narrowed, while totals, charts, the per-chat table and events apply all of them. Aggregates are computed over the full filtered ledger in SQL — never over the latest N events — and request values travel only as bound parameters.

## Time chart

The "Tokens over time" stacked column chart follows the range filter (hourly buckets for 24 h, daily beyond, auto-width for all time) and has a segmented toggle with six breakdown modes.  Buckets cover exactly the same filtered window as the stat cards and breakdowns: the first column is the partial hour/day the rolling cutoff falls inside, the last is the current still-in-progress bucket (both drawn at half strength), and all of a snapshot's queries share one read transaction and one `now`, so chart totals always reconcile with the cards even while the proxy writes.  The six modes:

- **harness** / **model** — tokens stacked per harness or per model (stable per-name colors)
- **chat** / **type** — tokens stacked per recorded chat identity or per chat type
- **in/out** — prompt tokens (input) vs completion tokens (output)
- **cache** — cached vs uncached prompt tokens (uncached = prompt − cached; columns are shorter because they sum prompt tokens only)

Tooltip, the screen-reader table, and aria-labels follow the active mode.

## Drill-down

The compact toolbar keeps the time range, searchable chat picker, and **Filters** button above the metrics. Typing searches chat choices; selecting a result applies the chat filter. The selected chat remains visible and has its own clear button. **Filters** opens a drawer for harness, provider, model, chat type, route, and outcome. Changes apply immediately; **Done** closes the drawer. Active drawer filters appear as removable chips, and clear-all removes constraints while preserving the time range.

Per-chat rows (sortable by requests/input/output/cached/total), per-harness rows, donut slices and legend entries all set their filter on click, or with Enter/Space once focused. The per-chat table reads `Unknown` for rows with no recorded identity, the honest state of ledgers written before chat attribution, which the dashboard reads as-is (it never migrates anything).

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
