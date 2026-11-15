# Browser sidebar platform

The Brave extension sidebar talks to this Hermes profile over a WebSocket.
The extension offers the configured token as its sole subprotocol, and the
server authenticates and selects it during the handshake. There is no
allow-all switch.

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
- `{"type":"hello","client":"big-steve-extension","version":".."}` — client hello;
  the server replies `{"type":"ready"}`. `{"type":"ready"}` is also accepted as a no-op.

Outbound to the extension:

- `{"type":"ready"}` — successful hello acknowledgement.
- `{"type":"delta","id":"<draft>","text":"<suffix>"}` — streaming deltas, append-only.
- `{"type":"final","id":"<last chat id>","text":"..."}` — sealed turn end; draft state resets.
- `{"type":"tool","name":"run_tab_js","args":{"code":".."},"request_id":".."}` — tool request.
- `{"type":"error","text":".."}` — malformed/unknown frames; the socket stays usable.

A wrong, missing, or additional offered subprotocol — or a configured token
with non-token characters — rejects the WebSocket handshake. Authentication
errors do not include either token.

The `browser_sidebar` toolset (just `run_tab_js`) is default-off and restricted to this
platform; enable it via `hermes tools` or `platform_toolsets`.
