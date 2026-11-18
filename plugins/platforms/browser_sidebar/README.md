# Browser sidebar platform

The Brave extension sidebar talks to the installation-wide Discord hub over a WebSocket.
Each browser tab is its own Hermes session (``chat_id="tab-<id>"``); the hub routes each
tab's session to one attached gateway generation for this Hermes profile (see "Hub
lifecycle and deployment" for the restart-handoff contract). Frames whose tab id is
missing or unusable fall back to the single legacy ``brave`` conversation, so older
extension builds keep working unchanged. The extension offers the configured token as
its sole subprotocol, and the server authenticates and selects it during the handshake.
There is no allow-all switch.

Put `BROWSER_SIDEBAR_TOKEN` in `/root/.hermes/secrets/browser-sidebar.env`.
Do not commit the token.

The hub listens on port **8796**, path `/ws`. Point the extension at
`ws://server.tailea4ecb.ts.net:8796/ws`.

## Hub lifecycle and deployment

`hermes-discord-hub` owns the authenticated extension socket; gateway processes attach
over the hub's same-user Unix socket. The sidebar rides the same instant-overlap restart
machinery as Discord: a successor gateway attaches while the predecessor drains, and the
hub's session router picks the delivery generation per session — per TAB session, so a
tab with a turn still running on the predecessor stays there (follow-ups queue on the
running owner) while another tab's new turn routes to the successor immediately; the
browser socket never drops. A pending `run_tab_js` result returns to the generation that
issued the request. Events that arrive while no gateway generation can take them are
retained at the hub and replayed on the next attach. A still-attached predecessor may
finish its in-flight outbound (drafts/finals) after the successor attaches; a detached or
superseded gateway cannot send frames or reclaim ownership by reconnecting.

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
  A numeric `tab.id` selects that tab's own session (`tab-<id>`); anything else falls back
  to the legacy `brave` conversation.
- `{"type":"tool_result","request_id":"..","ok":true,"result":".."}` — resolves a pending
  tool call (`ok:false` carries `"error"`); results truncate at 100 000 chars.
- `{"type":"hello","client":"big-steve-extension","version":".."}` — client hello;
  the server replies `{"type":"ready"}`. `{"type":"ready"}` is also accepted as a no-op.

Outbound to the extension:

- `{"type":"ready"}` — successful hello acknowledgement.
- `{"type":"delta","id":"<draft>","text":"<suffix>","tab_id":<int>}` — streaming deltas,
  append-only. `tab_id` names which tab's conversation the frame belongs to (absent on
  legacy frames; the panel then renders it on the active tab).
- `{"type":"final","id":"<last chat id>","text":"...","tab_id":<int>}` — sealed turn end;
  draft state resets.
- `{"type":"typing","active":true|false,"tab_id":<int>}` — typing indicator per tab.
- `{"type":"tool","name":"run_tab_js","args":{"code":".."},"request_id":"..","tab_id":<int>}`
  — tool request; the worker scripts THAT tab (a closed tab fails the tool instead of
  scripting whichever tab is focused). Without `tab_id` the focused tab is used. An
  optional `args.tab_id` takes precedence over the frame's `tab_id`.
- `{"type":"tool","name":"navigate_tab","args":{"url":"https://..","tab_id":<int>?},"request_id":"..","tab_id":<int>?}`
  — navigates the resolved tab with `chrome.tabs.update` and replies only once that tab
  reports `tabs.onUpdated` `status:"complete"` (bounded by the tool timeout; the
  `onUpdated` listener is removed afterwards). No page JS runs. Only http(s) URLs (and
  `about:blank`) are allowed; anything else, and a closed tab, return `ok:false`. The
  result is JSON `{"tab_id":..,"url":"..","title":".."}`.
- `{"type":"tool","name":"activate_tab","args":{"tab_id":<int>},"request_id":".."}`
  — activates the tab (`chrome.tabs.update` `{active:true}`) and focuses its window
  (`chrome.windows.update` `{focused:true}`); the result is the same `{tab_id,url,title}`
  JSON. An unknown tab returns `ok:false`.
- Unknown tool names return `ok:false` with `unknown tool: <name>`. Every tool resolves
  its target the same way: `args.tab_id`, then the frame's `tab_id`, then the focused
  tab; a named-but-closed tab fails the tool.
- `{"type":"error","text":".."}` — malformed/unknown frames; the socket stays usable.

The side panel keeps one transcript per tab (persisted to `chrome.storage.session`,
keyed `tabTranscript:<id>`; the legacy conversation keeps the old
`chrome.storage.local` `transcript` key) and tracks its window's active tab
(`tabs.onActivated`/`onUpdated`, window focus). The header's **new chat** button sends
`/new` tagged with the current tab, so the gateway's existing `/new` handling resets
only that tab's session, and clears that tab's local transcript.

A wrong, missing, or additional offered subprotocol — or a configured token
with non-token characters — rejects the WebSocket handshake. Authentication
errors do not include either token.

The `browser_sidebar` toolset (`run_tab_js`, `navigate_tab`, `activate_tab`) is
default-off and restricted to this platform; enable it via `hermes tools` or
`platform_toolsets`.

