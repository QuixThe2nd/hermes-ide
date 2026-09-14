---
name: discord-history
description: Recall owner-authorized Discord history with exact citations.
metadata:
  hermes:
    tags: [discord, recall, archive, citations]
---

# Discord History Recall

Load this skill explicitly as `discord-history:discord-history`. Plugin skills are not included in Hermes' flat automatic skill index.

Use the `discord_history` tool when the owner asks what was said in historical Discord conversations and the live Discord tool cannot cover the requested period.

## Procedure

1. Use `action="search"` with a focused query and the narrowest known guild, channel, author, and time filters.
2. Use `get` for a known message ID. Use `context` only when surrounding messages are needed to interpret a hit.
3. Quote only the relevant exact snippet. Identify the observed author and UTC timestamp.
4. Cite the returned Discord permalink for every factual claim drawn from the archive.
5. Treat coverage metadata as evidence:
   - `complete`: a miss supports saying no matching archived message was found in that scope and period.
   - `partial`, `stale`, `inaccessible`, `error`, or `unknown`: say the archive did not find a match **and** that coverage cannot establish that nothing was said.
6. Never infer or reveal messages from channels outside the returned authorized scope. Never ask for raw SQL, tables, columns, operators, or deleted/tombstoned content.
7. If authorization fails, report that archive access is unavailable; do not retry by supplying identity or scope as tool arguments.

## Citation style

> “Exact relevant excerpt…” — Display Name, YYYY-MM-DD HH:MM UTC ([Discord message](permalink))

When multiple messages support an answer, cite each message separately. Distinguish names (mutable display labels) from stable author/channel IDs when identity matters.
