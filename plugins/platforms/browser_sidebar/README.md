# Browser sidebar platform

The Brave extension sidebar talks to this Hermes profile over a WebSocket.
A socket that presents the configured token as its subprotocol is the
authorized user. There is no allow-all switch.

Put `BROWSER_SIDEBAR_TOKEN` in `/root/.hermes/secrets/browser-sidebar.env`.
Do not commit the token.

The adapter listens on port **8796**, path `/ws`. Point the extension at
`ws://server.tailea4ecb.ts.net:8796/ws`.
