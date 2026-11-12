---
title: Browser Automation
description: Control a Camofox browser for web interaction, form filling, snapshots, and vision analysis.
sidebar_label: Browser
sidebar_position: 5
---

# Browser Automation

Hermes Agent uses [Camofox](https://github.com/jo-inc/camofox-browser) for browser automation. Camofox is a self-hosted Node.js server wrapping Camoufox, a Firefox fork with fingerprint-spoofing support.

Pages are represented as accessibility-tree snapshots. Interactive elements receive refs such as `@e1` and `@e2`, which the agent uses for clicking and typing.

## Capabilities

- Navigate, read snapshots, click, type, scroll, go back, and press keys.
- Extract image references from the accessibility tree.
- Capture screenshots and analyze them with a vision model.
- Evaluate JavaScript through `browser_console(expression=...)` when needed.
- Reuse persistent or externally managed Camofox profiles.
- Fill credential-vault values through authenticated Camofox evaluation without exposing secret values to the model.

Camofox is the only browser backend. There is no browser engine or cloud-provider selection. Browser tools remain available in the tool schema; if `CAMOFOX_URL` is not configured or the server is unreachable, calls return a configuration or connection error.

## Setup

Run `hermes tools` and open **Browser Automation → Camofox**. The setup flow installs the `@askjo/camofox-browser` npm package when Node.js is available and prompts for the server URL.

Start the installed server:

```bash
npx @askjo/camofox-browser
```

The first run downloads the Camoufox engine. Configure the control API in `~/.hermes/.env`:

```bash
CAMOFOX_URL=http://localhost:9377
```

For an authenticated remote server, also set:

```bash
CAMOFOX_API_KEY=your-token
```

Hermes sends that token as `Authorization: Bearer ...` on Camofox API requests. Keep it in `.env`, not in `config.yaml` or prompts.

### Docker

You can run Camofox directly instead of installing the npm package:

```bash
docker run -d \
  --name camofox-browser \
  --restart unless-stopped \
  -p 9377:9377 \
  -e CAMOFOX_PORT=9377 \
  jo-inc/camofox-browser
```

The [Camofox repository](https://github.com/jo-inc/camofox-browser) also provides `make up`, `make down`, `make reset`, `make fetch`, and architecture/version overrides for building its image.

### VNC live view

To watch a headed browser, publish Camofox's VNC and noVNC ports and enable VNC:

```bash
mkdir -p ~/.camofox-docker
docker run -d \
  --name camofox-browser \
  --restart unless-stopped \
  -p 9377:9377 \
  -p 6080:6080 \
  -p 5901:5900 \
  -e CAMOFOX_PORT=9377 \
  -e ENABLE_VNC=1 \
  -e VNC_BIND=0.0.0.0 \
  -e VNC_RESOLUTION=1920x1080 \
  -v ~/.camofox-docker:/root/.camofox \
  jo-inc/camofox-browser
```

Open `http://localhost:6080` for noVNC, or connect a VNC client to `localhost:5901`. When the Camofox health response advertises a VNC port, Hermes includes the live-view URL in navigation results.

### Opening host loopback pages from Docker

`CAMOFOX_URL` points to the host-published control API, but a page such as `http://127.0.0.1:3000` must be resolved from inside the Camofox container. Enable loopback rewriting in `~/.hermes/config.yaml`:

```yaml
browser:
  camofox:
    rewrite_loopback_urls: true
    loopback_host_alias: host.docker.internal
```

Equivalent environment settings are `CAMOFOX_REWRITE_LOOPBACK_URLS=true` and `CAMOFOX_LOOPBACK_HOST_ALIAS=host.docker.internal`.

Only navigation URLs whose hosts are `localhost`, `127.0.0.1`, or `::1` are rewritten. `CAMOFOX_URL` itself is unchanged. Leave rewriting disabled when Camofox runs directly on the host. Use a reachable LAN address instead of `host.docker.internal` when the container platform requires it.

## Persistent sessions

By default, each task receives a random Camofox identity; cookies and logins do not survive agent restarts. Enable managed persistence in `~/.hermes/config.yaml`:

```yaml
browser:
  camofox:
    managed_persistence: true
```

Restart Hermes after changing this setting.

With managed persistence, Hermes:

- derives a deterministic `userId` scoped to the active Hermes profile;
- reuses that identity across tasks and process starts;
- skips destructive server-side session deletion during cleanup.

Different Hermes profiles receive different Camofox identities. Hermes stores its profile-scoped identity state under `$HERMES_HOME/browser_auth/camofox/`; browser profile data remains on the Camofox server.

The Camofox server must implement persistent per-`userId` profiles. Hermes cannot make a server persistent if it creates a fresh browser context for every identity. To reset a profile completely, remove its server-side data and the corresponding Hermes identity state.

## Externally managed sessions

When another application owns a visible Camofox browser, Hermes can share that identity instead of creating an isolated profile.

| Setting | Environment variable | Effect |
|---------|----------------------|--------|
| `browser.camofox.user_id` | `CAMOFOX_USER_ID` | Uses an externally managed Camofox `userId`. |
| `browser.camofox.session_key` | `CAMOFOX_SESSION_KEY` | Sets the `listItemId` used when creating or adopting a tab. Defaults to a per-task value. |
| `browser.camofox.adopt_existing_tab` | `CAMOFOX_ADOPT_EXISTING_TAB` | Looks for an existing tab before creating one. |

```yaml
browser:
  camofox:
    user_id: shared-camofox
    session_key: visible-tab
    adopt_existing_tab: true
```

Setting `user_id` enables non-destructive cleanup: Hermes does not call `DELETE /sessions/<user_id>`, so the other application's tabs, cookies, and profile survive.

With tab adoption enabled, the first browser operation calls `GET /tabs?userId=<user_id>`. Hermes prefers the newest tab whose `listItemId` matches `session_key`, then the newest tab for that user. If no tab is available or the request fails, the next operation creates one. Adoption stops after `tab_id` is populated; if the external application closes that tab, later calls return the Camofox error rather than silently adopting another tab.

Camofox does not coordinate tab focus between clients. Coordinate ownership externally—for example, pause the other application while Hermes is operating its tab.

## Available tools

| Tool | Purpose |
|------|---------|
| `browser_navigate` | Open a URL in the task's Camofox tab and return a compact snapshot when available. |
| `browser_snapshot` | Read the accessibility tree and element refs. |
| `browser_click` | Click an element ref from a snapshot. |
| `browser_type` | Replace an input's value using its element ref. |
| `browser_scroll` | Scroll up or down. |
| `browser_back` | Navigate back in the current tab. |
| `browser_press` | Press a key such as `Enter` or `Escape`. |
| `browser_get_images` | Extract image URLs and alt text from snapshot entries. |
| `browser_vision` | Capture a screenshot and analyze it with the configured vision model. |
| `browser_console` | Evaluate a supplied JavaScript expression. Console-log capture is unavailable. |

Use `web_search` or `web_extract` for simple retrieval. Use browser tools when the task needs interaction, dynamic page state, or screenshots.

### Snapshot limits

Snapshots longer than `browser.snapshot_threshold` (15,000 characters by default) are truncated at line boundaries. Hermes stores the complete snapshot under `$HERMES_HOME/cache/web/` and returns a path for `read_file` paging. No auxiliary LLM summarization is performed.

### JavaScript and private-network policy

Navigation and JavaScript evaluation apply Hermes's URL-safety policy. Cloud metadata endpoints remain blocked even when private URLs are explicitly allowed.

`browser.allow_private_urls` controls access to private/internal page URLs where the policy applies. JavaScript evaluation checks URL-shaped literals before execution and checks the current page after execution before returning content.

For stronger restrictions on hostile pages, enable:

```yaml
browser:
  restrict_evaluate: true
```

This denies common sensitive primitives such as cookies, web storage, clipboard access, and network requests in `browser_console(expression=...)`. It is opt-in because it also blocks legitimate DOM extraction. `browser.allow_unsafe_evaluate` explicitly overrides this denylist; it does not override URL safety.

Credential-vault fills use authenticated Camofox evaluation and bind each fill to an origin saved with the vault item. Secrets are inserted into the page without being returned in the tool result. See [Credential Vault](credential-vault.md).

## Session lifecycle

- Each task has its own Hermes-to-Camofox session mapping.
- Ephemeral sessions are closed during cleanup.
- Managed and externally managed sessions use non-destructive cleanup.
- Inactive sessions are reaped after `browser.inactivity_timeout` (120 seconds by default).
- Cleanup also runs during shutdown.

The model does not need to close a tab explicitly. Persistent profiles are retained intentionally; deleting their server-side data is a separate administrative action.

## Limitations

- Interaction is accessibility-tree based, not pixel-coordinate based.
- Camofox's REST API does not expose console-log capture, raw CDP, native-dialog, HAR-capture, or session-recording interfaces.
- Large snapshots are truncated as described above.
- File downloads are not exposed through the browser toolset.
- A running Camofox server and a configured `CAMOFOX_URL` are required for successful calls.
