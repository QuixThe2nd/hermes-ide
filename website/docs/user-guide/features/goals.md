---
sidebar_position: 16
title: "Persistent Goals"
description: "Automatic standing goals: task-like messages start the loop, and Hermes keeps working across turns until it's done. Our take on the Ralph loop."
---

# Persistent Goals (auto-goal)

A standing goal gives Hermes an objective that survives across turns. Every
eligible message is classified by a Jev Decisions model
(`typesafe/jev-1.13` via Openrouter): task-like messages **start a goal
automatically**, follow-up task messages extend it, approvals resume a paused
loop, and chases reopen work that finished or stalled. After every turn a
lightweight judge model checks whether the goal is satisfied by the
assistant's last response; if not, Hermes feeds a continuation prompt back
into the same session and keeps working — until the goal is achieved, you say
"stop", or the turn budget runs out.

It's our take on the **Ralph loop**, directly inspired by
[Codex CLI 0.128.0's `/goal`](https://github.com/openai/codex) by Eric Traut
(OpenAI). The core idea — keep a goal alive across turns and don't stop until
it's achieved — is theirs. The implementation here is independent and adapted
to Hermes' architecture: goals start from what you already typed, not from a
command.

## When it kicks in

Auto-goal is for tasks where you want Hermes to iterate on its own without you
re-prompting every turn:

- "Fix every lint error in `src/` and verify `ruff check` passes"
- "Port feature X from repo Y, including tests, and get CI green"
- "Investigate why session IDs sometimes drift on mid-run compression and write up a report"
- "Build a small CLI to rename files by their EXIF dates, then test it against the photos/ folder"

The classifier labels messages that want a thing changed, produced, diagnosed,
or found as **task** — including complaints ("why is tdarr down", "the error
handling is spamming") — and messages that only want words ("what is X",
"list the commands") as **chat**. Chat never starts a goal. Tasks where the
agent does one turn and stops simply finish; tasks where *you'd otherwise have
to say "keep going" three times* are where the loop shines.

## Quick start

Nothing to enable — just send the task:

```
You: why is tdarr down and can you get it back up

Hermes: <starts diagnosing>

  🎯 Done when: tdarr is back up and verified serving

  ↻ Continuing toward goal (1/20): the cause is found but the fix is unverified.
```

What happened:

1. **Your message was classified** (`task`, confidence ≥ 0.6) off-path, at
   message arrival — it never delays the reply.
2. **At the end of the turn**, the goal is created with a drafted completion
   contract and you get exactly one `🎯 Done when: …` line carrying the
   contract's outcome — from an explicit `done when:` line in your message if
   you wrote one, otherwise the drafted outcome (no line is posted when no
   outcome could be drafted; the raw message is never echoed back as a
   done-condition). A `stop_when` in the contract rides the same line as a
   "stop and ask when" clause.
3. **The judge runs** against the turn that just finished; if the goal isn't
   met, a continuation prompt re-enters the session automatically.
4. **The loop terminates** with `✓ Goal achieved`, `⏸ Goal paused — N/20
   turns used`, or your own "stop".


Low-confidence decisions, `chat`, and `unknown` (the fail-safe result when
the classifier is unreachable) never start a goal — but the raw label and
confidence are always logged (see the intent report below).

## Controlling the loop

| You say / do | What happens |
|---|---|
| a task-like message (no active goal) | Starts a goal + one `🎯 Done when:` line. |
| a task-like message (goal active) | Appended to the active goal as an extra criterion. No second goal. |
| "yeah", "merge", "go ahead" (goal paused) | Resumes the paused goal (budget reset). No papercut. With nothing paused, it starts a goal from the approval — contract drafted like any other start. |
| "status?", "so u done?", "it didnt work" | **Chase.** Reopens a goal that finished or paused within the last 30 minutes and appends `user says not done: <message>`; with no recent goal, starts one from the chase text plus the last assistant message. |
| "stop" | Pauses the active goal immediately and keeps the turn goal-free. |
| "just answer" (anywhere in the message) | Skips goal machinery for that turn entirely. |
| `done when: <condition>` (a line in your message) | Overrides the completion condition verbatim. |
| `/subgoal <text>` | Append an extra criterion to the active goal. |

Two safety rails:

- **Group threads.** Only the session owner (the first human who started the
  conversation) or an explicitly configured gateway operator may start or
  reopen a goal. Anyone can talk; DMs are unaffected.
- **Judge errors pause.** Two consecutive judge failures (malformed verdicts
  or transport errors) pause the goal with a message pointing at the
  `auxiliary.goal_judge` config instead of burning the budget on a broken judge.

The manual `/goal` slash command was removed when automatic creation landed;
the goal machinery itself (judge, contracts, gates, wait barriers) is unchanged
and still drives every loop. `/subgoal` remains a registered command.

## Completion contracts

A *vague* goal makes for vague judging — the judge can only check what you
told it to want. A durable objective names **what done means, how to prove it,
what not to break, what's in scope, and when to stop**. Hermes captures this
as an optional **completion contract** attached to the goal:

| Field | Meaning |
|---|---|
| `outcome` | The single end state that must be true when done. |
| `verification` | The specific test / command / artifact that *proves* the outcome. |
| `constraints` | What must not change or regress. |
| `boundaries` | Which files, dirs, tools, or systems are in scope. |
| `stop_when` | The condition under which Hermes should stop and ask for input. |

When a contract is set, both prompts change: the **continuation prompt** tells
the agent to target the verification surface and respect the constraints, and
the **judge prompt** decides `done` *only when the verification criterion is
met with concrete evidence* (a command result, file excerpt, test output) —
not a loose "looks done" claim.

### Two ways a contract gets set

**1. Automatically.** Whenever a message starts a goal — from any label
(task, an approval with nothing paused, a chase with no recent goal) — the
`goal_judge` auxiliary model drafts a contract from the message (plus the
assistant's last reply as context when there is one). If drafting is
unavailable, the goal is set free-form and no `🎯` line is posted — drafting
never blocks goal creation.

**2. Inline, in your message.** Write `field: value` lines directly in the
task message:

```
Migrate auth to JWT
verify: pytest tests/auth passes
constraints: keep the /login response shape unchanged
boundaries: only touch services/auth and its tests
stop when: a DB schema migration is required
```

Recognized field prefixes (`verify:`, `verified by:`, `constraints:`,
`preserve:`, `boundaries:`, `scope:`, `stop when:`, `blocked:`, …) populate the
contract; inline fields always win over the drafted ones. A plain message with
an incidental colon (`Fix bug: the parser drops commas`) is **not** mangled —
only known field prefixes are pulled out. An explicit `done when:` line
overrides the outcome verbatim and is what the 🎯 line announces.

Contracts persist in `SessionDB.state_meta` alongside the goal, so they
survive `/resume`. Contracts and `/subgoal` criteria compose: subgoals fold
into the contract as extra criteria the judge must also satisfy.

## Adding criteria mid-goal: `/subgoal`

While a goal is active you can append extra acceptance criteria with
`/subgoal <text>` without resetting the loop. Each call adds one numbered item
to the goal's subgoal list; the **continuation prompt** the agent sees on the
next turn includes the original goal plus an "Additional criteria the user
added mid-loop" block, and the **judge prompt** is rewritten so the verdict
must consider every subgoal — the goal isn't marked done until the original
objective **and** every subgoal are met. Auto-goal appends task messages the
same way.

| Command | What it does |
|---|---|
| `/subgoal <text>` | Append a new criterion to the active goal. Requires an active goal. |
| `/subgoal` (no args) | Show the current numbered subgoal list. |
| `/subgoal remove <N>` | Remove the Nth subgoal (1-based). |
| `/subgoal clear` | Drop every subgoal but keep the original goal intact. |

## Parking on a background process

Some goals are gated on something that takes minutes and runs on its own — CI
on a pushed PR, a long build, a test matrix, a deploy, a rate-limit cooldown.
Without help, the goal loop would re-poke the agent every turn into "is it
done yet?" busy-work while it waits.

**This is handled automatically.** Every turn, the judge is shown the agent's
own live background processes (the `terminal(background=true)` registry
entries this session spawned — pid, session id, command, uptime, recent
output, and any `watch_patterns` / `notify_on_complete` trigger; processes
started by delegated subagents are not shown, so a fan-out parent is never
parked on a worker's poller) alongside the goal and the agent's response.
When the agent's progress is genuinely gated on one of them, the judge returns
a **`wait`** verdict instead of `continue`, and the loop **parks**: the next
turns are skipped (no judge call, no continuation, no turn consumed) until the
wait is satisfied — then it resumes normally with the result in hand. A
pid/session wait is capped at 30 minutes; a process that never exits (a
watcher, a forgotten poller) cannot park the goal indefinitely.

The judge picks the right kind of wait from the process's own signal:

- **`wait_on_session <id>`** — releases when the process's *own trigger*
  fires: it exits, **or** (if it was started with `watch_patterns`) its
  pattern matches. For a long-lived watcher / server / poller that signals
  **mid-run** and may never exit on its own.
- **`wait_on_pid <pid>`** — releases on process exit only.
- **`wait_for_seconds <n>`** — releases after a fixed delay.

You don't type anything for this — it's the judge's decision, made from the
process context the loop hands it. The barrier (pid- or time-based) is
persisted with the goal in `SessionDB.state_meta`, so it survives `/resume`.
If the PID is already dead when the barrier is set (or dies while parked), or
the time deadline passes, the barrier clears on the next check — a stale
barrier can never wedge the loop. Saying "stop" also drops it.

## Behavior details

### The judge

After every turn, Hermes calls an auxiliary model with:

- The standing goal text
- The agent's most recent final response (last ~4 KB of text)
- A system prompt telling the judge to reply with strict one-line JSON:
  `{"verdict": "done" | "blocked" | "continue" | "wait", "reason":
  "<one-sentence rationale>"}` (wait verdicts add `wait_on_session` /
  `wait_on_pid` / `wait_for_seconds`; the legacy `{"done": <bool>, "reason":
  "..."}` shape is still accepted)

The judge is deliberately conservative: it marks a goal `done` only when the
response **explicitly** confirms the goal is complete, when the final
deliverable is clearly produced, and when there is **no obvious remaining next
step the user would clearly want**. Unfinished edges and explicitly in-scope
untried options require `continue` with the missing piece named in the reason.
A goal the agent explains is **unachievable** (impossible, out of scope, needs
user input) gets a `blocked` verdict instead — never `done`: the goal
**pauses** with the judge's reason (`🚫 Goal judged unachievable — paused`),
so you can re-state the goal or say "go ahead" to override rather than burning
budget or having an impossible task waved through as complete.

### Fail-open semantics, bounded

If the judge errors once (network blip, malformed response, unavailable aux
client), Hermes treats the verdict as `continue` — a broken judge never wedges
progress. Two failures **in a row** (parse or transport) auto-pause the goal
with a message pointing at the `auxiliary.goal_judge` config, and the turn
budget remains the real backstop.

### Turn budget

Default is 20 continuation turns (`goals.max_turns` in `config.yaml`). When
the budget is hit, Hermes auto-pauses:

```
⏸ Goal paused — 20/20 turns used. Send "go ahead" to keep going, or "stop" to end the goal.
```

Saying "go ahead" resets the counter to zero, so you can keep going in
measured chunks.

Exhausting the budget is also a completion barrier: even a `done` verdict on
the final allowed turn pauses instead of reporting the goal achieved.

### User messages always preempt

Any real message you send while a goal is active takes priority over the
continuation loop. On the CLI your message lands in `_pending_input` ahead of
the queued continuation; on the gateway it goes through the adapter FIFO the
same way. The judge runs again after your turn — so if your message happens
to complete the goal, the judge will catch it and stop.

### Continuation replies do not quote the goal message (gateway)

A continuation prompt is not a reply to the message that set the goal, so on
platforms that quote a reply target (Telegram) its progress bubbles and final
reply are posted to the chat/topic without quoting that original message. The
🎯 line is a normal outbound chat message too — never model context, never a
system-prompt change.

### Persistence

Goal state lives in `SessionDB.state_meta` keyed by `goal:<session_id>`. That
means `/resume` picks up right where you left off — a goal, close your laptop,
come back tomorrow, `/resume`, and the goal is still standing exactly as you
left it (active, paused, or done).

### Prompt cache

The continuation prompt is a plain user-role message appended to history. It
does **not** mutate the system prompt, swap toolsets, or touch the
conversation in any way that invalidates Hermes' prompt cache. Running a
20-turn goal costs the same cache-wise as 20 turns of normal conversation.
Auto-goal classification happens off-path (never inline with the reply) and
adds nothing to model context.

## Configuration

```yaml
goals:
  # Max continuation turns before Hermes auto-pauses. Default 20.
  max_turns: 20
```

Automatic goals are always on — there is no mode setting. A `goals.auto` key
left over in a config.yaml from an older install is ignored, not an error;
you can delete it. Every eligible human message is classified by one Jev
Decisions POST to Openrouter (`typesafe/jev-1.13`, 8s timeout, one retry,
then fail closed as `unknown`). The classifier needs `OPENROUTER_API_KEY`
(in `.env` or the platform's secret scope); without a key, goals simply never
start automatically — nothing else changes. Internal/system events,
bot-authored messages, blank messages, and slash commands are skipped.

Records are appended to `<HERMES_HOME>/goals/intent_shadow.jsonl`
(profile-aware) — the audit trail of what the loop acted on. The directory
and file use private
permissions (`0700`/`0600` where supported), and each stored message excerpt
is capped at 300 characters. Inspect counts and the newest examples per label
without making an LLM call:

```bash
hermes goals intent-report --since 7d
# Choose how many recent examples to print per label:
hermes goals intent-report --since 24h --examples 5
```

`--since` accepts positive durations in seconds, minutes, hours, days, or
weeks (`s`, `m`, `h`, `d`, `w`). Malformed or torn JSONL rows are skipped.

`chase` results also use the existing papercuts plugin to record
operator-visible friction, with fields including session id and goal-active
state. Duplicate chase papercuts are suppressed per session for 30 minutes.
If papercuts is unavailable, logging continues without any user-visible
effect.

### Choosing the judge model

The judge uses the `goal_judge` auxiliary task. By default it resolves to
your main model (see [Auxiliary Models](../configuration.md#auxiliary-models)).
If you want to route the judge to a cheap fast model to keep costs down, add
an override:

```yaml
auxiliary:
  goal_judge:
    provider: openrouter
    model: google/gemini-3-flash-preview
```

The judge call is small (~200 output tokens) and runs once per turn, so a
cheap fast model is usually the right call.

## Example walkthrough

```
You: create four files ~/.hermes/cache/scratch/note_{1..4}.txt, one per turn, each containing its number

Hermes: Creating ~/.hermes/cache/scratch/note_1.txt now.
  💻 echo "1" > ~/.hermes/cache/scratch/note_1.txt   (0.1s)
  I've created ~/.hermes/cache/scratch/note_1.txt with the content "1". I'll continue with the remaining files on the next turn as you specified.

  🎯 Done when: create four files ~/.hermes/cache/scratch/note_{1..4}.txt, one per turn, each containing its number
  ↻ Continuing toward goal (1/20): Only 1 of 4 files has been created; 3 files remain.

Hermes: [Continuing toward your standing goal]
  💻 echo "2" > ~/.hermes/cache/scratch/note_2.txt   (0.1s)
  Created ~/.hermes/cache/scratch/note_2.txt. Two more to go.

  ↻ Continuing toward goal (2/20): 2 of 4 files created; 2 remain.

Hermes: [Continuing toward your standing goal]
  💻 echo "3" > ~/.hermes/cache/scratch/note_3.txt   (0.1s)
  Created ~/.hermes/cache/scratch/note_3.txt.

  ↻ Continuing toward goal (3/20): 3 of 4 files created; 1 remains.

Hermes: [Continuing toward your standing goal]
  💻 echo "4" > ~/.hermes/cache/scratch/note_4.txt   (0.1s)
  All four files have been created: ~/.hermes/cache/scratch/note_1.txt through ~/.hermes/cache/scratch/note_4.txt, each containing its number.

  ✓ Goal achieved: All four files were created with the specified content, completing the goal.

You: _
```

Four turns, zero "keep going" prompts from you, no command to remember.

## When the judge gets it wrong

No judge is perfect. Two failure modes to watch for:

**False negative — judge says continue when the goal is actually done.** The
turn budget catches this. You'll see `⏸ Goal paused` and can say "stop", or
just send a new message.

**False positive — judge says done when work remains.** You'll see `✓ Goal
achieved` but you know better. Say so — "again, it didnt work" is a chase:
within 30 minutes it reopens the goal and appends `user says not done: …`, so
the loop picks the work back up instead of starting over. The judge's system
prompt is deliberately conservative to make false positives rarer than false
negatives.

If you find a judge verdict unconvincing, the reason text in the `↻
Continuing toward goal` or `✓ Goal achieved` line tells you exactly what the
judge saw. That's usually enough to diagnose whether the goal text was
ambiguous or the model's response was.

## Attribution

The standing-goal loop is Hermes' take on the **Ralph loop** pattern. The
user-facing design — keep a goal alive across turns, don't stop until it's
achieved — was popularised and shipped in
[Codex CLI 0.128.0](https://github.com/openai/codex) by Eric Traut on OpenAI's
Codex team. Our implementation is independent (central `CommandDef` registry,
`SessionDB.state_meta` persistence, auxiliary-client judge, Jev intent
classification, adapter-FIFO continuation on the gateway side) but the idea is
theirs. Credit where credit's due.
