# Browser sidebar platform

The Brave extension sidebar talks to this Hermes profile over a WebSocket.
A socket that presents the configured token as its subprotocol is the
authorized user. There is no allow-all switch.

Put `BROWSER_SIDEBAR_TOKEN` in `/root/.hermes/secrets/browser-sidebar.env`.
Do not commit the token.

The adapter listens on port **8796**, path `/ws`. Point the extension at
`ws://server.tailea4ecb.ts.net:8796/ws`.

## Protocol (JSON text frames, single socket)

Inbound from the extension:

- `{"type":"chat","id":"m1","text":"...","tab":{"id":..,"url":..,"title":..}}` — becomes a
  gateway message prefixed with `[browser tab: <title> — <url>]`; missing tab fields render `?`.
- `{"type":"tool_result","request_id":"..","ok":true,"result":".."}` — resolves a pending
  `run_tab_js` call (`ok:false` carries `"error"`); results truncate at 100 000 chars.
- `{"type":"ready"}` — hello, ignored.

Outbound to the extension:

- `{"type":"delta","id":"<draft>","text":"<suffix>"}` — streaming deltas, append-only.
- `{"type":"final","id":"<last chat id>","text":"..."}` — sealed turn end; draft state resets.
- `{"type":"tool","name":"run_tab_js","args":{"code":".."},"request_id":".."}` — tool request.
- `{"type":"error","text":".."}` — malformed/unknown frames; the socket stays usable.

A wrong, missing, or non-token-character configured token closes the socket with `1008`.

The `browser_sidebar` toolset (just `run_tab_js`) is default-off and restricted to this
platform; enable it via `hermes tools` or `platform_toolsets`.
