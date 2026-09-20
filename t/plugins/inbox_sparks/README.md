# inbox_sparks

End-of-turn gate plugin. Subscribes to the `pre_turn_end` hook (new in this
fork) and, at most once per cooldown window, returns one continuation
directive asking the agent to decide — before the turn finishes — whether
anything in the conversation or its wider context is worth starting a new
conversation with the user about via the `start_conversation` tool
(`hermes_starts` plugin). Calling it zero times is an explicitly fine answer.

- Directive budget: one per turn (`agent.max_pre_turn_end_nudges`, default 1,
  hard cap 2), shared with any other `pre_turn_end` subscriber.
- Rate limit: at most one directive per cooldown window (default 240 minutes),
  persisted in `<HERMES_HOME>/inbox_sparks/state.json` (mode 0600) so every
  process on the host shares the budget.
- Cooldown override: `plugins.entries.inbox_sparks.settings.cooldown_minutes`
  (read at register time). `0` disables the rate limit (the per-turn budget
  still applies).
- The handler never raises and its hot path is one small file read.

Disable with `hermes plugins` (`plugins.disabled` wins over
`default_enabled`).
