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

## How a start lands

Each start is **one message in the channel**: the full opening in Hermes's own voice,
with a `*Where I'd take this:*` paragraph when there is a next move. A public thread
named `Start #N — <kind>` is anchored on that message, so replies stay out of the
channel. Openings longer than the message limit split on paragraph boundaries — the
first part is the channel message, the rest go in the thread. If thread creation fails,
the whole opening stays in the channel and the start still reports success.

### Settings

Under `plugins.entries.hermes_starts.settings` in `config.yaml`:

| Key | Default | What it does |
|---|---|---|
| `mention_user_id` | *(unset)* | Discord user ID pinged on each start. The mention prefixes the opening, and the same user is added to the thread so replies and subscription keep working. |
| `quiet_hours` | `23:00-08:00` | Window in `quiet_tz` where starts still post but neither ping nor thread-add happens. Empty string disables the gate. |
| `quiet_tz` | `Australia/Sydney` | IANA zone used to evaluate `quiet_hours`. |

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
The guard preserves quiet hours, mentions, thread membership and conversation seeding.

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
