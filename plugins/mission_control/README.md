# Mission Control

A repo-native Hermes plugin: a local web UI over the sessions stored in
your Hermes home. One command runs it:

```
hermes mission_control serve
```

then open <http://127.0.0.1:9136/>. The server reads
`HERMES_HOME` (via `get_hermes_home()`) — the main `state.db` plus every
`profiles/*/state.db` — and never writes a conversation itself: its own
writes are the archive flag for Close/Reopen and the optional Discord
archive mirror, while composer turns are admitted as runs on the core
API server, which owns every session write.

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
- A composer: replies and new chats are admitted as runs on the core
  API server (`POST /v1/runs`, profile-scoped and authenticated) — the
  prompt travels only inside that one request's JSON body, no child
  process is ever spawned, and the 202 names the run's canonical
  session id so the page navigates and polls immediately. One
  in-flight turn per session; progress ticks and a live activity strip
  reflect the actual persisted state. A send the API does not accept —
  busy, unreachable, or a session id it will not confirm — is an
  explicit failed send that puts the exact submitted text back in the
  composer for retry, never a half-run or a duplicate.
- Clarify cards: when a run the composer started asks a blocking
  question, the run parks on it and the session page renders the card
  with the suggested answers (multi-select when the agent allows it,
  plus a free-text Other). While a card is pending the composer is
  disabled; the answer is proxied to the core API server that owns the
  session — keyed to that exact session and profile, so a stale or
  cross-session id is refused — and the moment the card resolves the
  run resumes and the composer re-enables on the next poll.
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
or configured for a clean install. Composer turns and clarify cards
ride the core API server each profile already configures
(`HERMES_API_SERVER_URL`, default `http://127.0.0.1:8642`, with the
`API_SERVER_KEY` from the `.env` beside that profile's `state.db`):
when it is not reachable a send fails explicitly and the text is
restored for retry — nothing runs, nothing is half-written.

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
  server** — an `http` origin with a trusted host and the port
  actually bound, never merely an echo of the request's own `Host`,
  plus the one deliberate proxy exception: when the request's `Host`
  is a `--trusted-host` name, a browser `Origin` naming that same
  host over `https` on the default public port is accepted, so a
  TLS-terminating reverse proxy in front of the plain-HTTP backend
  works without weakening anything else (an `https` origin on any
  other host or port is still refused, and `Forwarded` /
  `X-Forwarded-*` are never consulted for this decision) — then
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
- **Every composer turn runs in the profile you addressed.** Replies
  and new chats are admitted as runs on the core API server at that
  profile's own scoped URL (`/p/<profile>/v1/runs`), authorized with
  the `API_SERVER_KEY` from the `.env` beside that profile's
  `state.db` — the main home's key for the default profile, the
  profile's own key for a named one, never the other way round, so a
  named-profile reply runs against that profile's DB and never the
  default home. The profile name resolves through the discovered DB
  mapping or not at all (no filesystem path is derived from URL
  input), the 202 must confirm the exact canonical session id or the
  send fails closed, and the prompt travels only inside the one
  authenticated request's JSON body — never argv (there is no child),
  never a URL, never a log line.
- **The clarify bridge is fail-closed and profile-keyed.** The pending
  card is read from, and the answer posted to, the core API server
  (`HERMES_API_SERVER_URL`, default `http://127.0.0.1:8642`) using
  the `API_SERVER_KEY` from the `.env` beside that profile's
  `state.db` — a secret, like the Discord token, so it never goes in
  config.yaml, a flag, or a page, and a named profile never inherits
  the main home's key. GET surfaces only the oldest unresolved
  question for that exact session; POST must name that exact
  `clarify_id` and it must belong to that session and profile, so
  stale, cross-session, or cross-profile answers are refused. An
  unreachable or erroring upstream fails closed: the card stays put,
  the composer stays disabled, and only a safe subset of upstream
  statuses ever surfaces.
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
- Run status is polled over the same authenticated, profile-scoped
  core API, and only the statuses and ids that API returns ever reach
  a response — never prompt bytes, never an upstream error body.
