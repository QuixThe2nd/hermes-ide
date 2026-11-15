# Browser sidebar platform

The Brave extension sidebar talks to the installation-wide Discord hub over a WebSocket.
The hub routes its frames to the currently attached gateway for this Hermes profile.
The extension offers the configured token as its sole subprotocol, and the
server authenticates and selects it during the handshake. There is no
allow-all switch.

Put `BROWSER_SIDEBAR_TOKEN` in `/root/.hermes/secrets/browser-sidebar.env`.
Do not commit the token.

The hub listens on port **8796**, path `/ws`. Point the extension at
`ws://server.tailea4ecb.ts.net:8796/ws`.

## Hub lifecycle and deployment

`hermes-discord-hub` owns the authenticated extension socket; gateway processes attach
over the hub's same-user Unix socket. Gateway replacement changes the current routing
generation without closing the extension connection. Detached or superseded gateways
cannot send frames or reclaim ownership by reconnecting.

The attaching profile resolves `BROWSER_SIDEBAR_TOKEN` in its own secret scope and passes
the resolved value only in the local attach request. The hub does not read another
profile's environment.

On upgrade, stop the old gateway-owned sidebar listener, deploy and start the updated hub,
then restart the gateways. Starting updated gateways against an old hub is not supported:
they intentionally do not fall back to a process-local listener. The listener and token
lock remain hub-owned during gateway restart gaps. Changing the configured host or port
requires a hub restart.

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
