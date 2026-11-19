# Hermes Starts

**Your AI has always had a reply box. This gives it an opening line.**

Hermes Starts is a Discord channel where your agent can *start* conversations — not just answer them. Like a trusted co-founder or close friend texting first: a joke, a compliment, a noticed pattern, a business idea, a disagreement, a personal check-in, advice, feedback, or yes, sometimes a complaint. Hermes uses it at-will and sparingly, in its own voice.

## Setup

1. The plugin is default-enabled in this fork.
2. Ensure `$HERMES_HOME/.env` contains a non-empty `DISCORD_BOT_TOKEN`.
3. Enable the toolset for Discord sessions:

```bash
hermes tools enable hermes_starts --platform discord
```

The bot needs the **Manage Channels** permission for first-time channel provisioning.

## First start

On the first `start` action, if no channel exists yet, the plugin auto-runs setup: it creates
`#inbox` (or your chosen name), posts a pinned welcome embed, and saves state under
`$HERMES_HOME/hermes_starts/state.json`.

You can also provision explicitly with `action='setup'`.

## How a conversation lands

Every public inbox opener uses the same delivery path. The opening is posted in
the channel and a public thread is anchored on it; starts are named
`Start #N — <kind>`. When `mention_user_id` is configured, its mention always
shares the anchor with real opening text, and the owner is added to the thread
on a best-effort basis. Openings longer than the message limit split on
paragraph or word boundaries — the first part is the channel message, the rest
go in the thread. If start thread creation fails, the whole opening stays in
the channel and the start still reports success.

### Settings

Under `plugins.entries.hermes_starts.settings` in `config.yaml`:

| Key | Default | What it does |
|---|---|---|
| `sparks_cron` | `true` | Master switch for the default `inbox-sparks` cron job (see below). `false` never seeds it, on any profile. |

## The default `inbox-sparks` cron job

Every install gets periodic agent-initiated-conversation wakes without
hand-creating a job. On gateway start (the plugin's `on_gateway_start` hook,
run off the startup path), the plugin checks each served profile's cron store
and — when no job named `inbox-sparks` exists — creates one:

- **schedule** `every 120m`, **deliver** `local` (a no-opening run posts
  nothing anywhere; `start_conversation` is the only thing that talks),
- **toolsets** `hermes_starts` plus the generic `file`, `terminal`,
  `session_search`, `skills`, `web` — no host-specific toolsets;
- **no skills** — the repo ships no skill for the job to attach; the wake
  prompt is self-contained and works on every install.

Seeding is idempotent and deferential:

- an existing `inbox-sparks` job is **never modified or duplicated** — a
  hand-tuned job keeps its prompt, schedule, and toolsets byte-identical,
  paused or not;
- after the seeder creates the job once, a marker file
  (`$HERMES_HOME/hermes_starts/sparks_cron_seeded`) records that it seeded —
  delete the job and it stays deleted;
- `plugins.entries.hermes_starts.settings.sparks_cron: false` opts a profile
  out entirely (per-profile: under `gateway.multiplex_profiles` each served
  profile's own flag and own store apply);
- `plugins.disabled` naming `hermes_starts` or `inbox_sparks` opts the home
  out the same way (spelling normalized, so `inbox-sparks` counts): no job
  is created and no marker written — the deny-list is the loader's own
  opt-out for these bundled plugins, so seeding honors it too;
- seeding also requires a configured `DISCORD_BOT_TOKEN` for that profile —
  without the token the wake's only tool cannot resolve, so no job is
  created until the token exists.

The job spec and the create/skip decision live in the Go module
`internal/sparkscron` (built on demand into `$HERMES_HOME/cache/sparkscron`;
needs a Go toolchain — without one, seeding is skipped with a log line and
retried on the next gateway start).

## Duplicate-start protection

Enable the optional guard under `plugins.entries.hermes_starts.settings`:

```yaml
dedup_enabled: true
dedup_window_days: 10
dedup_max_history: 100
```

The guard checks the full opening and proposed next step against recent starts before
posting. Cosmetic copies are rejected locally; reworded proposals use one bounded
comparison through Hermes's existing auxiliary-model routing. Different proposals
about the same project remain eligible. No additional credentials or dependencies
are required. The guard is off by default for backward compatibility.

A duplicate returns `success: false`, `duplicate_start_number`, `duplicate_thread_id`,
and `duplicate_thread_url` when the destination is known. Read that thread and add
only material new information; otherwise leave it alone. If the model, history, or
state is unavailable or invalid, the guard returns a deferred error without posting.
Do not bypass a refusal by rephrasing or by using a different posting tool.

State persists across sessions. Existing installations rebuild their initial comparison
window from recent inbox anchors. The window is bounded by both age and entry count;
older material is not guaranteed to be compared. Semantic matching can still make
mistakes, so this guard complements checking existing conversations.

Enabled starts serialize their state reads, cooldown checks, comparison and delivery.
A reservation is saved before sending and retains any known anchor or thread after a
partial failure. Retrying the same opening therefore cannot assume that a timeout
means nothing was posted. Inspect the recorded destination before any manual recovery.
The guard preserves mentions, thread membership and conversation seeding.

## Recovery

A start whose Discord send was interrupted — process death, or a network call
that hit its 30s bound — leaves a reservation in `state.json` recording how
far it got (`pending`, `anchor_posted`, `thread_created`) and, once known,
the posted anchor's message id. Send failures and lock-busy deferrals return
that `pending_start` list plus a `recover_hint`.

Reconcile from the anchor without reposting:

```
start_conversation action='recover' anchor_id='<message id>'
```

Recovery is idempotent (an anchor already in history is a no-op), reuses a
thread that already exists on the anchor, verifies content before adopting an
anchor whose id was never recorded (pass `start_number` for that), posts only
thread tail parts not already present, and then applies the same listener
mark, session seed and history commit as a live start. An anchor Discord no
longer knows is a clean refusal that changes nothing.

## Multi-guild bots

If the bot is in more than one Discord server, setup returns the guild list and asks you to
re-run with `guild_id`. Single-guild bots auto-select the only server.

## vs Papercuts

| | Hermes Starts | Papercuts |
|---|---|---|
| Purpose | Agent initiates a human conversation | Agent workflow friction |
| Examples | "I noticed we skip retros — worth one?" | "Config lookup required 3 extra steps" |
| Tone | Personal, strategic, funny, warm, blunt | Mechanical, fixable |
| Destination | Discord channel | Local JSONL journal |
| Auto-fix | No | Optional daily autofix cron |

Papercuts records fixable system and workflow friction. Hermes Starts is the agent reaching out to *you* — praise, jokes, personal matters, strategic debate, feedback, or complaints. Complaints are only one possible conversation, not the point of the channel.
