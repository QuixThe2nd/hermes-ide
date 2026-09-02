# Mission Control

A repo-native Hermes plugin: a local web UI over the sessions stored in
your Hermes home. One command runs it:

```
hermes mission_control serve
```

then open <http://127.0.0.1:9136/>. The server reads
`HERMES_HOME` (via `get_hermes_home()`) — the main `state.db` plus every
`profiles/*/state.db` — and never writes anywhere except the archive
flag for its own Close/Reopen control and the optional Discord archive
mirror.

## What it shows

- An inbox of every non-hidden session with activity in the last 24
  hours, in honest sections (Active / Closed / Open · completed /
  Open · unfinished), with search, profile filters, and a no-JS
  auto-refresh fallback.
- Full transcripts: user/assistant text (HTML-escaped, never rendered
  as markdown), maximal runs of consecutive tool calls collapsed into
  one expandable group per run, sub-agent children, and cross-profile
  lineage for dispatched research jobs.
- Assistant commentary recovery: an assistant row whose `content` is
  empty narrates only in `codex_message_items`; exactly those rows
  recover their displayable assistant text (bounded, defensively
  parsed — never tool arguments) so it renders as a normal message and
  splits tool groups chronologically. A row with real `content` is
  always the sole authority.
- A composer: replies run `hermes --resume <id> chat --oneshot -q …`;
  a new chat runs `hermes chat --oneshot --source mission-control -q
  …`. One in-flight turn per session; progress ticks and a live
  activity strip reflect the actual persisted state.

## Setup

No setup beyond a working Hermes install: the server is Python stdlib
only (plus the repo's own `hermes_constants` for home discovery), with
no web framework and no external assets — participants render as
generic letter badges, so nothing needs to be downloaded or configured
for a clean install.

### Optional config (non-secret settings only)

Defaults live in code; a `mission_control` section in `config.yaml`
overrides them, and CLI flags override the config:

```yaml
mission_control:
  host: 127.0.0.1      # loopback default; anything else is explicit
  port: 9136
  discord_sync: true    # archive mirror, only when a token is present
```

The Discord archive mirror (closed-state parity for Discord-thread
sessions) is config-gated per profile: it activates only when a
`DISCORD_BOT_TOKEN` line exists in the `.env` file beside that
profile's `state.db` — a secret, so it never goes in config.yaml or a
flag. `--no-discord-sync` (or `discord_sync: false`) turns the mirror
off entirely, e.g. for proof servers against synthetic data.

## Security boundaries

- **Unauthenticated by design.** Anyone who can reach the bound
  address can read every session in the window and send messages
  through the composer. The default bind is loopback
  (`127.0.0.1`); that is the supported configuration.
- A non-loopback bind is always explicit (`--host` or config) and
  prints a loud warning at startup. It does not imply any
  authentication, and none is built in — if you need remote access,
  front it with an authenticating reverse proxy on a network you
  trust.
- Deployment guidance is intentionally generic: any "run this on a
  box, point a proxy at `127.0.0.1:9136`" scheme works; the plugin
  ships no service units and installs nothing.
- Reads are bounded: SQL projections cap every text/blob column they
  select, tool arguments are never rendered (only redacted,
  length-capped argument summaries in the live activity strip), and
  composer payloads are size-capped before they are read.
- Hermes subprocess output is parsed for the session id and exit code
  only and is never echoed into any response or log.
