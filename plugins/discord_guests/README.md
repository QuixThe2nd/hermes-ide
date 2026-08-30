# Discord Guests

**Invite a bot or a friend, and they get a private lounge of their own.**

Adding a guest auto-creates a private text channel `#<guest>-<host>-lounge`
(e.g. bot "Big Steve" + guest "Winnie" → `#winnie-big-steve-lounge`) under the
server's **Lounges** category. Only that member — plus the people who already
see Lounges, i.e. the owner and bots with admin — can view it. `@everyone`
stays view-denied everywhere. Access is per-channel overwrites only; nothing
beyond the channel itself is ever created or assigned.

The host part of the name comes from the running bot itself — first match wins:

1. the per-call `host` argument,
2. `plugins.entries.discord_guests.settings.host_slug` in `config.yaml`,
3. the bot's own display name in the guild (nick, else global name, else
   username), slugified,
4. the literal fallback `agent`.

When the guest and host slugs match, the name collapses to `#<guest>-lounge`
(no stutter).

## Setup

1. The plugin is default-enabled in this fork.
2. Ensure `$HERMES_HOME/.env` contains a non-empty `DISCORD_BOT_TOKEN`.
3. Optional first-time `action='setup'`: pins the guild and Lounges category,
   and (first setup only) denies `@everyone` view on every category and
   top-level channel. The category is resolved by name, case-insensitively —
   `Lounges` first, with the legacy `Chat` as a fallback — unless
   `chat_category_id` is passed.

The bot needs **Manage Channels** and **Manage Roles**-equivalent channel
overwrite permissions.

## Actions

| Action | What it does |
|---|---|
| `add` | Resolves the member (`user_id`, or a name prefix via member search), refuses anyone holding ADMINISTRATOR (the host bot itself excepted), then creates — or reuses — the lounge under Lounges, allows the member on it, and denies `@everyone` view on the lounge itself so it stays private even if category perms drift. |
| `remove` | Takes the member overwrite off the lounge. The channel and its history are kept — delete it manually if you really want it gone. |
| `list` | Current guests from state, with live channel existence. |
| `setup` | Optional: persist guild + Lounges category; first-run `@everyone` view lockdown. |

Members with ADMINISTRATOR are never added — they already see everything. The
one exception is the host bot itself: it holds admin, yet adding it provisions
its own lounge (guest and host slugs match, so the name collapses to
`#<host>-lounge`).

Guests are persisted in `$HERMES_HOME/discord_guests/state.json`. REST only,
≥0.3s between writes, 429 `retry_after` honoured, token never printed.
