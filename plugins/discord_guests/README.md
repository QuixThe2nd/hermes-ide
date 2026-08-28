# Discord Guests

**Invited bots and friends get a Guest badge, not the keys.**

`discord_guests` manages a private server where `@everyone` cannot `VIEW_CHANNEL`.
It provisions a zero-permission `Guest` role, locks the server down, and then hands
named members view/send access to the categories and channels you choose — never
moderation, never administration.

## Setup

1. The plugin is default-enabled in this fork.
2. Ensure `$HERMES_HOME/.env` contains a non-empty `DISCORD_BOT_TOKEN`.
3. Enable the toolset for Discord sessions:

```bash
hermes tools enable discord_guests --platform discord
```

The bot needs **Manage Roles** and **Manage Channels** (to write permission
overwrites). State lands in `$HERMES_HOME/discord_guests/state.json`.

## Actions

| Action | What it does |
|---|---|
| `setup` | Create the `Guest` role (hoisted, not mentionable, guild permissions `0`) and persist `{guild_id, role_id}`. On first setup it also denies `VIEW_CHANNEL` to `@everyone` on every category and top-level channel; afterwards lockdown is opt-in via `lockdown=true`. |
| `add` | Give a member the role — by user ID or a name prefix resolved through guild member search. Optionally `channels: [...]` grants `VIEW_CHANNEL` on those at the same time. |
| `remove` | Take the role off one member. The role and its channel overwrites stay. |
| `grant` | Allow the `Guest` role on the given channels/categories. |
| `revoke` | Delete the `Guest` overwrite on the given channels/categories. `@everyone` denies are untouched. |
| `list` | Role id, members holding it, and channels/categories carrying a Guest allow. |

Channels are given by ID or by name (case-insensitive). `guild_id` comes from the
argument, else saved state, else the bot's only guild; a multi-guild bot errors
until you pass one.

## What a Guest gets

Exactly these, and nothing else:

`VIEW_CHANNEL`, `SEND_MESSAGES`, `READ_MESSAGE_HISTORY`, `ADD_REACTIONS`,
`EMBED_LINKS`, `ATTACH_FILES`, `CONNECT`, `SPEAK`, `SEND_MESSAGES_IN_THREADS`.

The mask is fixed in code and guarded: `ADMINISTRATOR`, `MANAGE_GUILD`,
`MANAGE_ROLES`, `MANAGE_CHANNELS`, `BAN MEMBERS`, `KICK MEMBERS`, and
`MENTION_EVERYONE` are refused if they ever appear in an allow. A member who
already holds `ADMINISTRATOR` through any role is refused as a guest target —
an operator cannot be guested.

Granting a **category** writes one overwrite on the category; synced children
inherit it. A child carrying its own `@everyone` overwrite is unsynced and would
stay dark, so it gets the allow written directly too.

## Transport

REST only, over `urllib` — no discord.py, no Gateway. Writes are paced at least
0.3s apart, `429 retry_after` is honoured, and the bot token is never echoed in
an error or a result.
