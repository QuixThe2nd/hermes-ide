<p align="center">
  <img src="assets/banner.png" alt="Hermes IDE" width="100%">
</p>

# Hermes IDE ☤

A developer edition of [Hermes Agent](https://github.com/NousResearch/hermes-agent): a Hermes that can maintain codebases, with extra tooling and gateway behaviour, kept current with upstream (auto-synced hourly). Everything lands via PR with full CI.

Stock Hermes needs a lot of config. It does not arrive wheels-included, so you spend a long time wiring providers, channels, memory, notifications, a home server, and skills before it feels like a working assistant. A fresh Hermes also starts off stupid, and it only slowly learns: skills from experience, memory of who you are, your conventions, the shape of your homelab. Learning is the point of Hermes, but day one is bare.

This repo is part of an effort to build a preconfigured, **wheels-included** Hermes. The tooling lives in the tree, the dev pipelines come prebuilt, and capability that makes the out-of-box experience better belongs here instead of in a pile of private scripts. Upstream keeps its repo slim and ships capability externally, Debian to this fork's Ubuntu; this fork is deliberately the opposite. So on top of the agent you get the machinery around it: coding lanes, a Discord home server, model quota routing, safer restarts, and the small things you normally discover after losing an afternoon. Most of it exists to cut down on wiring and checking.

The biggest piece of that machinery is Discord. This fork is **Discord First**: Discord isn't just another gateway adapter here, it is the primary operator surface, where the work gets driven from. Telegram, Slack, WhatsApp, Signal, and the CLI still work. Discord is where the house is.

## Discord First

Start with a home server: a new Discord server for just you and your bot, then run `/sethomeserver`. Consider it mission control. Hermes builds the whole house in one pass: Notifications, Lounges, Honcho Memory, Models, and Speeds, with Notifications at the top. Moving an existing home server needs confirmation, because even Discord layout changes have somehow acquired paperwork.

The home_server plugin keeps that layout in sync. It checks at most hourly, but the template fingerprint bypasses the wait when the layout changes, so new or reordered channels appear straight away. It is idempotent, keeps categories and channels in template order, never deletes anything, renames legacy Quotas and Chat categories to Models and Lounges in place, and does not clobber existing home, notification, or rename targets.

Notifications get their own category instead of everything landing in the home channel: `#model-fallback`, `#gateway-restarts`, and `#other`. The restarts channel doubles as a session counter: `#gateway-restarts` becomes `agents-N` while Hermes is running and `restarting-N-agents` while it drains, so you can see how many sessions are alive without opening another dashboard.

Conversation uses an email-inspired structure. Chat lives in `#inbox` and `#outbox`. The outbox is where you start conversations and threads; the inbox contains conversations initiated by your agent. Agents can start conversations at any time using a tool, a post-run hook, or a cronjob, and they get another cronjob registered that automatically modifies how conversations are started, so your agent can give itself creative freedom. The goal of the inbox is to give your agent an outlet for unsolicited advice.

Hermes Starts is what lets the agent speak first. It creates and pins its own Discord inbox, then uses each opening message as the anchor for a new thread. The plugin also lets the agent ask for changes to how it runs — its instructions, access, tools, limits, or working habits — when it has actually hit friction or spotted a concrete opportunity. Inbox Sparks adds a once-per-4-hour window where the agent must weigh starting a conversation before a turn ends. The inbox is allowed to be quiet; it is not allowed to exist only as decoration.

Discord History provides read-only search over an owner-authorized PostgreSQL archive of Discord messages. It is opt-in and off by default. Papercuts keeps a structured journal of workflow friction, and its optional daily autofix cron, installed with `hermes papercuts autofix install`, turns small mechanical fixes into PRs.

Memory observability is crucial for a good agent, and stock Hermes provides almost none. Hermes IDE shows both reads and writes: the memory channels in your home server log edits, and memory injection is displayed in live chats.

The Models category is a quota wall. Hermes IDE automatically monitors configured token providers for remaining usage, resets available, time till expiry, and uptime, then orders the list of preferred models to match. `quota_channels` creates Discord voice channels under a Models category — one row each for Codex, Kimi, Cursor, and Grok, and one row per Z.AI credential-pool wallet (`z.ai 1`, `z.ai 2`, …) — ordered with the same score used by fallback routing:

`quota_frac × (168h / hours_to_reset) + one full wallet per pending usage-limit reset (Codex/Grok/z.ai), all × uptime_24h × uptime_1h`

Codex, z.ai, and Cursor get automatic 7-day token enrichment. The channel names show what is left, when it resets, and which provider Hermes currently prefers.

`fallback_quota_reorder` uses the same score to rotate the primary and fallback list, moving the top scorer into the primary slot. Wallets that reset sooner rank higher. `fallback_watch` tails `agent.log` and alerts Discord when the primary model falls back; it is opt-in, off by default, and cooldown-deduped.

The Speeds category is the same trick pointed at downloads. `speed_channels` turns Discord voice channels into a download wall for qBittorrent, SABnzbd, and slskd. The names show live throughput and queue depth, and the category label shows current 1.1.1.1 ICMP latency plus the countdown to the next poll.

Adding a guest through `discord_guests` creates a private `#<guest>-<host>-lounge` under Lounges. Legacy Chat still resolves. `@everyone` stays view-denied, so only that member and the house bots can see it.

Power users will have noticed that when Hermes is used too much, Discord rate limits it from creating new threads. Hermes IDE handles this automatically by queuing threads to be auto-created once the rate limit passes.

Stock Hermes is also inconsistent about when it pings you, so you end up jumping between threads checking which one needs input. Hermes IDE avoids pinging you on iterations or mid-run messages, but will reply to you or ping you on the final message. I found this simple change improved my productivity a lot, because I stopped burning mental bandwidth rotating between 20 chats for hours straight waiting for one to complete.

The smaller edges are handled too. Sessions use your stable username instead of a server nickname. `DISCORD_ALLOWED_GUILDS` lets any member of a listed server talk to the bot without changing DM access. Threads rename once after the first reply lands, not halfway through a turn. Progress updates respect each platform's real message limit.

Multiplexed profiles use their own `display.reasoning_style`. Compact mode renders `💭 thought for Xs` and can include an `(N tokens)` count for the turn. Streaming previews and interim updates stay standalone, and only the completed final answer reply-pings you. If an auto-threaded root turn cannot attach a reply reference, the final message uses an inline mention instead.

Clarify prompts mention the requesting user by default; set `discord.clarify_mentions: false` to stop that. The questions are numbered plain text rather than buttons, because Discord component views time out. A `resolve_ticket` proposal is terminal: the confirmation embed is the reply, and Hermes does not add another message underneath it.

The typing indicator stays on while a background delegated task is still running. MoA consult and debate progress uses one self-editing embed per call, with live N/T advisor counts on `moa_ask`. Cursor runs post a Cursor-branded progress embed with a "Watch live session" link instead of dumping a URL, and Claude runs post a Claude-branded embed that links into the local viewer after the delegation tool-progress message, in the same order as Cursor.

## Coding delegated from chat

Coding jobs leave the chat and run against the target checkout. `delegate_cursor_agent` sends small and medium jobs to a Cursor My Machines Cloud Agent. Cursor builds its checkout from pushed refs only, so the tool refuses to start when your local HEAD has unpushed commits. It does not quietly run against yesterday's code.

`delegate_claude_agent` handles medium and large jobs through the Claude Code CLI, with headless `/goal` as the default lane. Give it a `/goal <condition>` and it keeps going until a model judge, verified through the claude-glm wrapper, says the condition has been met. If the goal is too long for Claude Code's 4000-character limit, Hermes puts it in a brief inside the workdir instead of aborting the run. Claude writes stream-json output to the `claude-runs` logs while it works, so you can tail the run instead of staring at a silent chat.

Neither delegate tool has a stall watchdog or a default wall-clock limit, though a positive `timeout_seconds` still applies when you set one. `delegate_agent action='list'` shows the current tool, iteration, time since activity, and whether a child looks stalled. Slow and dead are different problems. Both delegate tools and the dev-pipeline lanes use the same agent-CLI runner underneath.

All four delegate tools (`delegate_agent`, `delegate_claude_agent`, `delegate_cursor_agent`, `delegate_assistant`) share one lifecycle: omitted/false `background` blocks the calling turn until the work is terminal and returns the final result inline, while `background=true` returns a handle immediately and delivers exactly one completion later. `delegate_task`/`dispatch_assistant` still dispatch as hidden aliases of the renamed tools.

Sometimes you want the whole coding process handed off, including the paperwork. The dev-pipeline plugin takes a repo and task through MoA planning, Cursor execution, mechanical verification, dual-model review, and a draft PR. It reports each stage back to chat in plain English and skips useless same-stage heartbeats. A separate claude-endurance lane uses Claude Code through claude-glm for broad or long builds.

Planning normally uses a consult; `delegate_development plan_mode=debate` runs the multi-round adversarial council instead. Reviews use a Russian-language kimi and grok pair, and `open_pr=false` skips the draft PR when you do not want one. `delegate_development` itself is currently parked and not registered, while `dev_pipeline_status` remains live.

Research has a lane of its own. `delegate_research` hands a substantial research brief to durable `researcher`-profile lanes (transient systemd user or system service) and returns one citation-checked report; every cited URL must have been fetched during the job. Set `deep_research.worker_file_tools: false` in the calling profile's config to run no-file jobs instead: lanes are pinned to `web,browser` and the synthesis writer to the empty `research_writer` toolset.

Agents are very good at ignoring the nice coding lane you built and editing the source anyway. The code-lane-gate plugin blocks in-context source edits everywhere by default, inside a git repo or not. Set `CODE_LANE_GATE_E2E=0` to opt out. Terminal writes are still a known v1 bypass.

One model is usually enough. When it is not, `moa_ask` and `moa_debate` give you multi-model consults and debates without leaving the chat. Both tools were restored from the archive.

`pr_intent_watch` watches this fork for new PRs and comments on what the change is trying to do, whether it is worth considering, and whether the claimed bug sounds real. It is an intent review, not a code review; the watcher never sees the diff.

`claude_viewer` ships the Claude run viewer with Hermes and installs and starts it automatically on Linux/systemd when the gateway comes up. The "Watch live session" link in a `delegate_claude_agent` embed uses this machine's LAN or Tailscale address, detected at runtime, instead of somebody else's hardcoded IP, so it works on any install. Use `hermes claude_viewer status|enable|disable|reconcile` to control it. Opening a run lands on its original prompt, with `G` jumping to the live tail. The viewer has no authentication, so keep it on your LAN or tailnet. If the port is already being served by a viewer you started yourself, reconcile stands down instead of starting a small civil war over one socket.

`mission_control` points the same local-viewer idea at every session, not just Claude runs: `hermes mission_control serve` puts a messenger-style web inbox for the last 24 hours of Hermes on loopback, across the main database and every profile. Sections stay honest (Active, Open · unfinished, Open · completed, Closed) and open sessions always render ahead of closed ones, however much newer the closed session's last activity is; closed means the projected tip carries `ended_at` or the archived flag, so an ended-but-unarchived conversation never sits in an open section. Transcripts render with maximal runs of consecutive tool calls collapsed into expandable groups, pages poll live, and a composer starts new chats or replies by admitting an authenticated, profile-scoped run on the core API server (`/v1/runs`) so no child process is ever spawned and the prompt never leaves the one request's JSON body — a send the API refuses (busy, unreachable, session mismatch) fails explicitly with the text restored for retry; when a run asks a clarifying question the card pauses it, blocks the composer, and the answer is proxied back through the same profile-scoped API with that profile's own key to resume it, with stale or cross-session answers refused. Codex's between-tools commentary renders as ordinary messages — recovered only from the narration column, never tool arguments — and dispatched research jobs land under their parent's sub-agents section instead of posing as separate top-level chats. Participants render as generic letter badges, optionally layered with local avatar PNGs you drop beside a profile's database (a missing or broken image just falls back to the letter). It reads through bounded SQL so a giant tool blob never leaves the database whole, is stdlib-only with no external assets, and adds no model tools. Like the Claude viewer it has no authentication: loopback is the supported bind, a non-loopback `--host` is explicit and warned about at startup, and remote access is your authenticating proxy's problem. The optional `mission_control` config section holds `host`, `port`, and `discord_sync`; the Discord archive mirror runs only when a `DISCORD_BOT_TOKEN` sits in the `.env` beside a profile's database.

## Memory and profiles

Memory should fail loudly. Honcho memory tools return dead API keys, 401s, and timeouts as real errors instead of pretending there was simply nothing to store or recall.

Hindsight can use `{chat}` inside `bank_id_template`, which gives one profile a separate bank for each messaging chat. It also skips memory lines already injected earlier in the same session. On the first bank init, Hermes seeds a `retain_mission` that ignores transient task state and two recall rules: prefer newer facts and ignore session dumps.

The default SOUL gives fresh profiles a credential preflight before asking you for another key. They check environment variables, `.env`, local secret files, and any configured secret manager first. Asking the human again is the last step, where it belongs.

Research and sandbox profiles often need to read files without being allowed to rewrite the machine. `hermes profile create NAME --read-only` creates a profile with the `file_readonly` toolset: `read_file` and `search_files`, without `write_file` or `patch`. On an existing profile, `hermes tools enable file_readonly` swaps out the normal `file` toolset the same way on every platform.

## WhatsApp missions

WhatsApp chats can be given a job instead of being left as permanent open conversations. Assistant chats expose the mission-aware `end_session` tool or the one-way `escalate_task` tool.

Goal-bound missions use the missions plugin through `delegate_assistant` and `end_session`. Mission-only DMs are controlled by `platforms.<whatsapp|whatsapp_cloud>.extra.mission_only_dms` and are off by default. When enabled, Hermes answers those DMs only while an assistant mission is attached to the chat.

A mission on a `@g.us` group admits exactly that group. It needs no mention and keeps all members in one shared session. Profile-scoped pairing no longer leaks into the global allowlist.

Allowlisted groups can also observe unmentioned chatter with `observe_unmentioned_group_messages` and `require_mention`. This is off by default. Hermes stores the chatter as context but waits until the next ping or mention before replying.

## Operations

A stalled gateway should not look dead. `display.retry_progress` adds a live provider retry and fallback bubble during stalls; it is off by default. Replies can also end with a timing split for total, API, tools, and other time, which is off by default upstream.

Steered follow-ups get a second "✅ Steer delivered" acknowledgement when the text actually reaches the model's context. Accepted and delivered are different states, although software often enjoys pretending otherwise.

`/restart` waits for active sessions to finish naturally — with no cap, so a user-requested restart never interrupts in-flight work, however long the last session takes. New work is refused while it waits, and the restart proceeds only once every active session, cron run, and API run has finished. `agent.restart_after_turn_timeout` is legacy and ignored for this wait. On Discord the requester gets one ⏸️ embed in the same chat, and only their reaction asks the other live chats to park safely now; that reaction-time snapshot is the only set resumed after restart. The shutdown warning shows each accepted LLM park steer. Chats that received the warning get a matching ♻️ back-online notice after the gateway returns, including after a raw SIGTERM.

A session the bounce kills mid-turn — drain timeout, crash, anything it did not agree to — resumes transparently instead of being told the gateway restarted. Hermes re-runs that turn's still-unanswered calls literally through the normal dispatcher — same name, call id, and arguments, side-effecting calls included — keeps every completed result, and continues the turn through the same continue-after-tool-results path an uninterrupted loop uses. Nothing synthetic is added anywhere: no restart note, no continuation note, no recovery user row in the model request or the transcript. If real user text arrives while recovery closes the batch, it runs verbatim as the next message after the batch completes. Only sessions that accepted the park steer see restart guidance. The one call never replayed is the lifecycle request that caused the bounce (the `restart` tool or a shell command targeting the gateway itself): it keeps the existing UNKNOWN effect-disposition treatment so a bounce cannot re-trigger itself, and calls that cannot pair unambiguously (duplicate or missing call ids) fail closed the same way instead of guessing.

Agents can call the `restart` tool as well. When other sessions or background jobs are in flight, it pings the requester and waits indefinitely for the exact word `restart`; anything else cancels. When this chat is the only active session, it skips the prompt and queues the restart outright. While it waits, the calling thread is temporarily titled `Restart Pending`. Once confirmed, it uses the same drain path as `/restart`. On Discord the confirmation is one embed that pings the requester, without a second progress bubble sitting beside it.

Lifecycle messages can go to a dedicated channel per platform with `/setnotify` and `/clearnotify`, which keeps startup and shutdown noise out of the home chat. The system prompt also tells the agent its own platform display name; on Discord that means the server nickname or global name appears as `**Your name:**` in session context.

`auto_update` runs unattended Hermes updates through an independent Linux/systemd timer, controlled with `hermes auto_update status|enable|disable|reconcile`. The default schedule is every 30 minutes all day, with no randomized delay. Every tick prepares the available update even while Hermes is busy, and the fleet restarts onto a fully prepared update only once Hermes is idle, ignoring stale streaming or unanswered rows outside the idle window.

`drift_watch` keeps a default-on eye on the live checkout. Twice an hour it inventories uncommitted drift, and when the drift set changes it captures a patch and copies untracked files, using auditd attribution where available. `hermes drift_watch reconcile` repairs its timer and setup. It watches and records; it is read-only toward git state.

Cron jobs do not get to casually restart the gateway or rewrite the live checkout. A quote-aware lifecycle guard blocks cron-spawned commands that try either one.

Config gains API retry backoff timing plus fallback chains for web search and web extract. `agent.tool_call_narration_guidance` is on by default and asks the model to briefly explain a tool call before it makes one. Parent agents run on a default 256-turn budget (`agent.max_turns`, the same default `run_agent.py`'s `main()` uses); set it higher or to `none`/`0` for no limit. Z.AI silently defaults to GLM-5.3, with GLM-5.3-Flash as its fallback. `security.allow_agent_config_writes` lets an operator opt out of the `write_file` and `patch` guard on the Hermes config file; it is off by default. `display.notify_on_complete` can send a native OS notification when a turn finishes, including over an SSH target, and is also off by default.

`compression.tail_mode` defaults to `lean`, which clamps the verbatim tail to 10–25K. Set it to `legacy` to restore the old `0.20 × threshold` tail.

For the upstream project, see [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). Everything below this paragraph is upstream's README.

---

<p align="center">
  <a href="https://hermes-agent.nousresearch.com/">Hermes Agent</a> | <a href="https://hermes-agent.nousresearch.com/">Hermes Desktop</a>
</p>
<p align="center">
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Docs-hermes--agent.nousresearch.com-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://nousresearch.com"><img src="https://img.shields.io/badge/Built%20by-Nous%20Research-blueviolet?style=for-the-badge" alt="Built by Nous Research"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
  <a href="README.ur-pk.md"><img src="https://img.shields.io/badge/Lang-اردو-green?style=for-the-badge" alt="اردو"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

**The self-improving AI agent built by [Nous Research](https://nousresearch.com).** It's the only agent with a built-in learning loop — it creates skills from experience, improves them during use, nudges itself to persist knowledge, searches its own past conversations, and builds a deepening model of who you are across sessions. Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), OpenRouter, OpenAI, your own endpoint, and [many others](https://hermes-agent.nousresearch.com/docs/integrations/providers). Switch with `hermes model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Seven terminal backends — local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## Quick Install

### Linux, macOS, WSL2, Termux

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

### Windows (native, PowerShell)

> **Heads up:** Native Windows runs Hermes without WSL — CLI, gateway, TUI, and tools all work natively. If you'd rather use WSL2, the Linux/macOS one-liner above works there too. Found a bug? Please [file issues](https://github.com/NousResearch/hermes-agent/issues).

Run this in PowerShell:

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

The installer handles everything: uv, Python 3.11, Node.js, ripgrep, ffmpeg, **and a portable Git Bash** (MinGit, unpacked to `%LOCALAPPDATA%\hermes\git` — no admin required, completely isolated from any system Git install). Hermes uses this bundled Git Bash to run shell commands.

If you already have Git installed, the installer detects it and uses that instead. Otherwise a ~45MB MinGit download is all you need — it won't touch or interfere with any system Git.

> **Android / Termux:** The tested manual path is documented in the [Termux guide](https://hermes-agent.nousresearch.com/docs/getting-started/termux). On Termux, Hermes installs a curated `.[termux]` extra because the full `.[all]` extra currently pulls Android-incompatible voice dependencies.
>
> **Windows:** Native Windows is fully supported — the PowerShell one-liner above installs everything. If you'd rather use WSL2, the Linux command works there too. Native Windows install lives under `%LOCALAPPDATA%\hermes`; WSL2 installs under `~/.hermes` as on Linux.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes              # start chatting!
```

### Troubleshooting

#### Windows Defender or antivirus flags `uv.exe` as malware

If your antivirus (Bitdefender, Windows Defender, etc.) quarantines `uv.exe` from the Hermes `bin` folder (`%LOCALAPPDATA%\hermes\bin\uv.exe`), this is a **false positive**. The file is Astral's `uv` — the Rust Python package manager Hermes bundles to manage its Python environment. ML-based antivirus engines commonly flag unsigned Rust binaries that download and install packages.

**To verify your copy is authentic:**

```powershell
# Install GitHub CLI if needed
winget install --id GitHub.cli

# Login to GitHub
gh auth login

# Run verification
$uv = "$env:LOCALAPPDATA\hermes\bin\uv.exe"
$ver = (& $uv --version).Split(' ')[1]
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = "$env:TEMP\uv.zip"
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
gh attestation verify $zip --repo astral-sh/uv
Expand-Archive $zip "$env:TEMP\uv_x" -Force
(Get-FileHash "$env:TEMP\uv_x\uv.exe").Hash -eq (Get-FileHash $uv).Hash
```

If attestation says "Verification succeeded" and the last line prints `True`, you're good.

**To whitelist Hermes:**
- **Windows Defender:** Run PowerShell as Admin → `Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\hermes\bin"`
- **Bitdefender:** Add an exception in the Bitdefender console (Protection > Antivirus > Settings > Manage Exceptions)
- Whitelist the **folder**, not the file hash — Hermes updates `uv` and the hash changes every version

For more context, see the upstream Astral reports: [astral-sh/uv#13553](https://github.com/astral-sh/uv/issues/13553), [astral-sh/uv#15011](https://github.com/astral-sh/uv/issues/15011), [astral-sh/uv#10079](https://github.com/astral-sh/uv/issues/10079).

---

## Getting Started

```bash
hermes              # Interactive CLI — start a conversation
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes config get   # Print individual config values
hermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)
hermes setup        # Run the full setup wizard (configures everything at once)
hermes claw migrate # Migrate from OpenClaw (if coming from OpenClaw)
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

📖 **[Full documentation →](https://hermes-agent.nousresearch.com/docs/)**

---

## Skip the API-key collection — Nous Portal

Hermes works with whatever provider you want — that's not changing. But if you'd rather not collect five separate API keys for the model, web search, image generation, TTS, and a cloud browser, **[Nous Portal](https://portal.nousresearch.com)** covers all of them under one subscription:

- **300+ models** — pick any of them with `/model <name>`
- **Tool Gateway** — web search (Firecrawl), image generation (FAL), text-to-speech (OpenAI), cloud browser (Browser Use), all routed through your sub. No extra accounts.

One command from a fresh install:

```bash
hermes setup --portal
```

That logs you in via OAuth, sets Nous as your provider, and turns on the Tool Gateway. Check what's wired up any time with `hermes portal info`. Full details on the [Tool Gateway docs page](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-gateway).

You can still bring your own keys per-tool whenever you want — the gateway is per-backend, not all-or-nothing.

---

## CLI vs Messaging Quick Reference

Hermes has two entry points: start the terminal UI with `hermes`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both interfaces.

| Action                         | CLI                                           | Messaging platforms                                                              |
| ------------------------------ | --------------------------------------------- | -------------------------------------------------------------------------------- |
| Start chatting                 | `hermes`                                      | Run `hermes gateway setup` + `hermes gateway start`, then send the bot a message |
| Start fresh conversation       | `/new` or `/reset`                            | `/new` or `/reset`                                                               |
| Change model                   | `/model [provider:model]`                     | `/model [provider:model]`                                                        |
| Set a personality              | `/personality [name]`                         | `/personality [name]`                                                            |
| Retry or undo the last turn    | `/retry`, `/undo`                             | `/retry`, `/undo`                                                                |
| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]`                                        |
| Browse skills                  | `/skills` or `/<skill-name>`                  | `/<skill-name>`                                                                  |
| Interrupt current work         | `Ctrl+C` or send a new message                | `/stop` or send a new message                                                    |
| Platform-specific status       | `/platforms`                                  | `/status`, `/sethome`                                                            |

For the full command lists, see the [CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli) and the [Messaging Gateway guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

---

## Documentation

All documentation lives at **[hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs/)**:

| Section                                                                                             | What's Covered                                             |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| [Quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart)                 | Install → setup → first conversation in 2 minutes          |
| [CLI Usage](https://hermes-agent.nousresearch.com/docs/user-guide/cli)                              | Commands, keybindings, personalities, sessions             |
| [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)                | Config file, providers, models, all options                |
| [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)                | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)                          | Command approval, DM pairing, container isolation          |
| [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)            | 40+ tools, toolset system, terminal backends               |
| [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)              | Procedural memory, Skills Hub, creating skills             |
| [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)                     | Persistent memory, user profiles, best practices           |
| [MCP Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)               | Connect any MCP server for extended capabilities           |
| [Cron Scheduling](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)              | Scheduled tasks with platform delivery                     |
| [Context Files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)       | Project context that shapes every conversation             |
| [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)             | Project structure, agent loop, key classes                 |
| [Contributing](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing)             | Development setup, PR process, code style                  |
| [CLI Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)                  | All commands and flags                                     |
| [Environment Variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) | Complete env var reference                                 |

---

## Migrating from OpenClaw

If you're coming from OpenClaw, Hermes can automatically import your settings, memories, skills, and API keys.

**During first-time setup:** The setup wizard (`hermes setup`) automatically detects `~/.openclaw` and offers to migrate before configuration begins.

**Anytime after install:**

```bash
hermes claw migrate              # Interactive migration (full preset)
hermes claw migrate --dry-run    # Preview what would be migrated
hermes claw migrate --preset user-data   # Migrate without secrets
hermes claw migrate --overwrite  # Overwrite existing conflicts
```

What gets imported:

- **SOUL.md** — persona file
- **Memories** — MEMORY.md and USER.md entries
- **Skills** — user-created skills → `~/.hermes/skills/openclaw-imports/`
- **Command allowlist** — approval patterns
- **Messaging settings** — platform configs, allowed users, working directory
- **API keys** — allowlisted secrets (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs)
- **TTS assets** — workspace audio files
- **Workspace instructions** — AGENTS.md (with `--workspace-target`)

See `hermes claw migrate --help` for all options, or use the `openclaw-migration` skill for an interactive agent-guided migration with dry-run previews.

---

## Contributing

We welcome contributions! See the [Contributing Guide](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing) for development setup, code style, and PR process.

Quick start for contributors — use the standard installer, then work from the
full git checkout it creates at `$HERMES_HOME/hermes-agent` (usually
`~/.hermes/hermes-agent`). This matches the layout used by `hermes update`, the
managed venv, lazy dependencies, gateway, and docs tooling.

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
cd "${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

Manual clone fallback (for throwaway clones/CI where you intentionally do not
want the managed install layout):

Create the venv outside the cloned source tree — a venv inside the directory
the agent operates from can be wiped by a relative-path command the agent runs
against its own checkout, destroying the running runtime mid-session.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv ~/.hermes/venvs/hermes-dev --python 3.11
source ~/.hermes/venvs/hermes-dev/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

---

## Community

- 💬 [Discord](https://discord.gg/NousResearch)
- 📚 [Skills Hub](https://agentskills.io)
- 🐛 [Issues](https://github.com/NousResearch/hermes-agent/issues)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux desktop-control MCP server for Hermes and other MCP hosts, with AT-SPI accessibility trees, Wayland/X11 input, screenshots, and compositor window targeting.
- 🔌 [HermesClaw](https://github.com/AaronWong1999/hermesclaw) — Community WeChat bridge: Run Hermes Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Built by [Nous Research](https://nousresearch.com).
