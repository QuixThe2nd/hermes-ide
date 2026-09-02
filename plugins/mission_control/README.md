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
  hours, in honest sections — Active / Open · completed /
  Open · unfinished / Closed — with search, profile filters, and a
  no-JS auto-refresh fallback. Open always outranks closed: every open
  row renders before any closed row, however much newer the closed
  session's last activity is, while each section keeps its own
  newest-first order. A conversation is closed when its projected tip
  carries `ended_at` or the archived flag — an ended-but-unarchived
  session never sits in an open section; its row says which it is
  (Ended or Archived).
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
- Clarify cards: when the agent asks a blocking question, the session
  page renders it as a card with the suggested answers (multi-select
  when the agent allows it, plus a free-text Other). While a card is
  pending the composer is disabled; the answer is proxied to the core
  API server that owns the session, and the moment the card resolves
  the composer re-enables on the next poll.
- Optional local avatar images: drop a `mission-control/avatar.png`
  beside a profile's `state.db` (or `mission-control/user.png` in the
  main home for yourself) and the generic letter badges gain a picture
  layer — rail, conversation rows, transcript bubbles, the sidebar
  footer, and your own messages. Nothing is fetched or configured:
  with no file — or an image that fails to load — the letter badge
  simply shows.

## Setup

No setup beyond a working Hermes install: the server is Python stdlib
only (plus the repo's own `hermes_constants` for home discovery), with
no web framework and no external assets — participants render as
generic letter badges out of the box, and the optional avatar images
are local PNGs you drop in yourself, so nothing needs to be downloaded
or configured for a clean install. Clarify cards need no setup either:
they surface only when the agent asks, and are answered through the
core API server already configured for that profile.

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
  (`127.0.0.1`); that is the supported configuration. An empty or
  wildcard `--host` counts as non-loopback exposure, not loopback.
- A non-loopback bind is always explicit (`--host` or config) and
  prints a loud warning at startup. It does not imply any
  authentication, and none is built in — if you need remote access,
  front it with an authenticating reverse proxy on a network you
  trust.
- **The Host header itself is checked first.** The server answers
  only for the Host names the address it bound implies: a loopback
  or wildcard bind trusts the loopback names plus this machine's own
  interface addresses, and an explicit `--host` trusts exactly that
  address. A request whose `Host` names anything else is refused
  with `421 Misdirected Request` before any page, token or route
  runs — the DNS-rebinding shape, where a public name resolves to
  your loopback and both `Host` and `Origin` name the attacker, never
  gets far enough to receive or use a CSRF token. Host spellings are
  normalized (IPv4, bracketed IPv6, optional port, one case-insensitive
  trailing dot); `Forwarded`/`X-Forwarded-*` are client-controlled and
  never consulted. To answer for one more name — say, behind a proxy
  you control — pass the repeatable, CLI-only flag:

  ```
  hermes mission_control serve --trusted-host mc.lan.example
  ```

  It is a hostname, not a secret, and stays out of `config.yaml` so
  the config file remains non-secret settings only.
- **Cross-site request forgery is refused before anything runs.**
  Every state-changing route (`/s/new`, reply, close, reopen, the
  clarify answer) gates on four header checks before the body is
  parsed: the trusted-`Host` check above, then an `Origin` (or, when
  no `Origin` rides along, a `Referer`) that names **exactly this
  server** — `http` scheme, a trusted host, and the port actually
  bound, never merely an echo of the request's own `Host` — then
  exactly `application/json` (an HTML form cannot produce that), and
  finally this server process's cryptographically random CSRF token
  in the non-simple `X-CSRF-Token` header, compared in constant time.
  The token is emitted only to pages this process serves (a `<meta>`
  tag) and never appears in a response body or the log. A
  browser-simple form or text post cannot launch Hermes, update
  SQLite, or call Discord.
- **Secrets never reach activity or transcript HTML.** One bounded
  redaction boundary covers every UI-exposed tool-argument summary and
  tool-result detail: complete `Authorization` values (scheme word and
  credential together), bare bearer tokens, `api-key`/`password`/
  `token`/`secret` assignments in underscore or hyphen spelling with
  their whole quoted-or-bare value, and credential-bearing URL/DB-URI
  userinfo. Useful non-secret text passes through.
- **Every composer subprocess runs in the profile you addressed.**
  Replies and new chats resolve the child's `HERMES_HOME` only through
  the discovered DB mapping — the main home for the default profile,
  the profile's own directory for a named one — so a named-profile
  reply writes that profile's DB, never the default home. No
  filesystem path is derived from URL input; argv is a plain list with
  no shell, and the child's stdout/stderr never reach a response.
- **The clarify bridge is fail-closed and profile-keyed.** A clarify
  answer is proxied to the core API server (`HERMES_API_SERVER_URL`,
  default `http://127.0.0.1:8642`) using the `API_SERVER_KEY` from the
  `.env` beside that profile's `state.db` — a secret, like the Discord
  token, so it never goes in config.yaml, a flag, or a page, and a
  named profile never inherits the main home's key. An unreachable or
  erroring upstream fails closed: the card stays put, the composer
  stays disabled, and only a safe subset of upstream statuses ever
  surfaces.
- **Avatar serving reads exactly two trusted filenames.**
  `GET /avatar/<profile>` and `GET /avatar-user` answer only a fixed
  `avatar.png` / `user.png` inside a home this server already
  discovered — the path is rebuilt from the discovered home, never
  taken from the URL, and re-checked for symlink escape and a 2 MB
  size cap before any byte is read. Anything else is the themed 404.
- **The Discord archive mirror cannot overwrite you.** A background
  snapshot is discarded whole if you closed or reopened anything while
  it was fetching (a per-profile archive epoch, checked under the same
  lock both paths write under); your confirmed state wins.
- **Lineage is evidence-based.** A session becomes a Sub-agents child
  only on concrete evidence: a durable research-job artifact whose
  prompt bytes and time window match a session whose own source marks
  a non-human worker, or a terminal launch recording the child's
  session id. A human-facing session (CLI, API, Discord, Telegram, …)
  keeps its inbox row even when its prompt and start time happen to
  match a job.
- Deployment guidance is intentionally generic: any "run this on a
  box, point a proxy at `127.0.0.1:9136`" scheme works; the plugin
  ships no service units and installs nothing.
- Reads are bounded: SQL projections cap every text/blob column they
  select, tool arguments are never rendered raw (only redacted,
  length-capped argument summaries in the live activity strip), and
  composer payloads are size-capped before they are read.
- Hermes subprocess output is parsed for the session id and exit code
  only and is never echoed into any response or log.
