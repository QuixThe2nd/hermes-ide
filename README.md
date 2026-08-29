<p align="center">
  <img src="assets/banner.png" alt="Hermes IDE" width="100%">
</p>

# Hermes IDE ☤

A developer edition of [Hermes Agent](https://github.com/NousResearch/hermes-agent): a Hermes that can maintain codebases, with extra tooling and gateway behavior, kept current with upstream (auto-synced hourly). Everything lands via PR with full CI.

### Why this fork exists

Stock Hermes needs a lot of config. It does not arrive wheels-included. You spend a long time wiring providers, channels, memory, notifications, a home server, and skills before it feels like a working assistant.

### It starts off stupid

A fresh Hermes starts off stupid, and it only slowly learns: skills from experience, memory of who you are, your conventions, the shape of your homelab. Learning is the point of Hermes — but day-one is bare.

### Wheels-included

This repo is part of an effort to build a preconfigured, **wheels-included** Hermes. The tooling lives in the tree. The dev pipelines come prebuilt. Capability that makes the out-of-box experience better belongs here, not in a pile of private scripts. Upstream keeps its repo slim and ships capability externally — Debian to this fork's Ubuntu — and this fork is deliberately the opposite.

### Discord-native

This fork is **Discord-native**. Discord isn't just another gateway adapter here — it's the primary operator surface, where the work gets driven from. Telegram, Slack, WhatsApp, Signal, and the CLI still work; Discord is where the house is.

The design centers on a **home server**: one Discord guild is the house. `/sethomeserver` makes that guild the home. Everything else hangs off it.

So this fork ships its own home-server layout instead of making every operator invent one. Chat lives in `#inbox` and `#outbox`; notifications land in `#model-fallback`, `#gateway-restarts`, and `#other`; the Honcho Memory channels keep memory visible; Models is the quota voice-channel wall; Speeds carries the download walls. One command provisions and wires all of it — the `hermes_starts` inbox, the home and notification channels, `#gateway-restarts` showing `agents-N` while agents are live and `restarting-N-agents` while draining, `quota_channels`, `speed_channels`. It's idempotent, it never deletes, and moving an existing home server requires a confirm.

### The house is the IDE

That home server is the **second brain** and the **IDE**. Tickets, `#inbox` and `#outbox`, the Honcho Memory channels, the Models quota wall, the Speeds download walls, the `#gateway-restarts` drain, and the coding work itself all live as channels in that one server — not behind a separate dashboard or an Electron window. The Discord server IS the workspace.

None of that stops the learning. Day one just arrives with the house already built — then it still learns.

## What's different here

This fork ships a lot of opinionated capability on top of upstream Hermes. The sections below are the delta — what actually changed in how you drive coding work, run the Discord house, and operate the gateway day to day.

### Coding delegated from chat

You can hand real coding work to machines from chat instead of babysitting a terminal. `delegate_cursor_agent` sends tasks to a Cursor My Machines Cloud Agent in the target checkout; `delegate_claude_agent` does the same for the Claude Code CLI. The Cursor path states its real contract up front — cloud checkout from pushed refs only — and refuses to start when local HEAD has unpushed commits, so cloud runs cannot silently target the wrong repository shape.

Claude Code gets a headless `/goal` mode: pass a `/goal <condition>` task and the run loops until a model judge confirms the condition is met (verified with the claude-glm wrapper). Overlong `/goal` conditions are auto-spilled to a workdir brief so Claude Code's 4000-character cap does not abort the run. The lane rule is deliberate: `delegate_cursor_agent` for small/medium work, `delegate_claude_agent` with default `/goal` for medium/large. `delegate_claude_agent` writes stream-json run logs, so claude-runs logs are live-tailable for progress reporting.

Delegate coding tools run with no stall watchdog and no default wall-clock limit; an optional positive `timeout_seconds` still applies. When you need visibility, `delegate_task action='list'` surfaces per-child liveness — current tool, iteration, seconds since activity, stalled flag — so a wedged subagent is distinguishable from a slow one. One shared agent-CLI runner powers both delegate tools and the dev-pipeline build lanes.

### Dev pipeline

The dev-pipeline plugin hands a repo and task to an automated pipeline: MoA planning, Cursor execution, mechanical verification, dual-model review, and a draft PR, with plain-English stage-progress messages to your chat (same-phase heartbeats skipped) and a status tool. A claude-endurance lane uses Claude Code (claude-glm) for broad or long builds.

`delegate_development plan_mode=debate` runs the multi-round adversarial council for planning; consult stays the default. Dev-pipeline review runs as a Russian-language kimi + grok dual review, and `delegate_development` gains `open_pr=false` to skip the draft PR. `delegate_development` is parked (not registered); `dev_pipeline_status` stays live.

### Code-lane gate

The code-lane-gate plugin blocks in-context source edits anywhere, git repo or not (on by default; opt out via `CODE_LANE_GATE_E2E=0`) to steer coding to the delegate lanes. Terminal writes are a known v1 bypass.

### Mixture of Agents

`moa_ask` and `moa_debate` are restored from the archive — multi-model consult and debate without leaving chat.

### Memory

Honcho memory tools surface backend failures — dead API keys, auth 401s, timeouts — as explicit errors instead of silently looking like "nothing stored". Hindsight `bank_id_template` supports `{chat}` so one profile can keep a separate bank per messaging chat. Per-turn injection skips lines already delivered earlier in the session. Fresh profiles start with a credential preflight in the default SOUL: check environment variables, `.env`, local secret files, and configured secret managers before asking the owner again.

- Memory — Hindsight seeds a retain_mission (skip transient task state) and two recall directives (prefer-newer, ignore session dumps) on first bank init

### Profiles

`hermes profile create NAME --read-only` ships a `file_readonly` toolset (`read_file` + `search_files`, no `write_file`/`patch`) and wires the new profile to it, so a sandbox or researcher profile can be granted file reads without file writes; `hermes tools enable file_readonly` swaps `file` out the same way on any platform.

### The Discord house and its plugins

Set a Discord home server once and Hermes provisions and keeps in sync the whole structure — Notifications, Chat, Honcho Memory, Models, Speeds — fully wired, with Notifications first at the top of the server. `/sethomeserver` does it from one command (confirm required to move an existing one), wires `#gateway-restarts` as `agents-N` while up and `restarting-N-agents` while draining, then re-syncs at most hourly — and immediately whenever the in-code template changes (a template fingerprint bypasses the hourly debounce, so an update that adds or reorders channels lands without a forced re-provision). The home_server plugin keeps the same layout idempotent and seats categories and channels in template order: never deletes, a legacy Quotas category is renamed in place to Models, and existing home, notification, and rename targets are never clobbered. `#gateway-restarts` shows `agents-N` live and `restarting-N-agents` while draining.

The Models category is a quota wall: `quota_channels` creates Discord voice channels for six AI providers (one channel each), ordered by the same score as fallback routing — `quota_frac × (168h / hours_to_reset) + one full wallet per pending usage-limit reset (Codex/Grok), all × uptime_24h × uptime_1h`; OpenRouter is a virtual unlimited Ox Alpha row — with automatic 7-day token enrichment on Codex, z.ai, and Cursor. `fallback_quota_reorder` uses that score for primary/fallback quota rotation: it ranks soonest-reset wallets first (unlimited Ox Alpha scored as a synthetic 100%/168h wallet, derated by uptime) and rotates the primary slot to the top scorer. `fallback_watch` tails agent.log and alerts a Discord channel whenever the primary model falls back, cooldown-deduped (opt-in, off by default). `pr_intent_watch` watches this fork for newly opened PRs and comments an intent review — what the change is trying to do, whether it is worth considering, and whether a claimed bug describes a real symptom — not a code review. It never sees the diff.

`speed_channels` is the download wall for qBittorrent, SABnzbd, and slskd: voice-channel names carry live throughput and queue depth; the category label shows live 1.1.1.1 ICMP latency and the next-poll countdown.

Hermes Starts lets your AI open conversations instead of only replying — it creates and pins its own Discord inbox, and each opening is a single message that anchors its own thread. Inbox Sparks pairs with that: once per 4-hour window the agent must weigh starting a conversation before a turn ends.

Discord History is a read-only search over an owner-authorized PostgreSQL archive of Discord messages (opt-in, off by default). Papercuts keeps a structured journal of workflow friction, plus an opt-in daily autofix cron (`hermes papercuts autofix install`) that turns small mechanical fixes into PRs. `auto_update` runs safe unattended Hermes updates on Linux/systemd via an independent timer (`hermes auto_update status|enable|disable|reconcile`; default every 30 minutes all day, idle-gated with stale streaming/unanswered rows ignored outside the idle window, no randomized delay). `discord_guests` auto-creates a private `#<guest>-<host>-lounge` text channel under Chat when a guest is added — `@everyone` stays view-denied, so the lounge is visible only to that member plus the house bots.

`drift_watch` keeps a default-on eye on the live checkout: a timer inventories uncommitted drift twice hourly, auto-captures a patch plus untracked copies whenever the drift set changes, and attributes writes via auditd where available (`hermes drift_watch reconcile`; read-only toward git state).

### Gateway lifecycle and operator UX

During stalls you can turn on a live provider retry/fallback progress bubble (`display.retry_progress`, off by default). Replies can end with a timing breakdown — total, API, tools, other (off by default upstream).

Steered follow-ups get a second "✅ Steer delivered" ack the moment the text actually lands in the model's context. `/restart` parks only live chats, resumes only that snapshot, and shows each accepted LLM park steer in its shutdown warning. Chats that got the shutdown warning get a matching ♻️ back-online notice after restart, including raw SIGTERM. An agent-callable `restart` tool pings the requester, waits indefinitely for the exact word `restart` (anything else cancels), then uses the same drain path as `/restart` — on Discord the confirmation is one embed that pings the requester, with no progress bubble beside it, and the calling thread is temporarily titled `Restart Pending` until the reply lands.

Lifecycle broadcasts (shutdown/startup) can route to a dedicated per-platform notification channel, keeping home chats free (`/setnotify`, `/clearnotify`). The system prompt tells the agent its own name: the bot's platform display name (Discord server nickname/global name) renders as `**Your name:**` in the session context.

### Discord session and UX details

Sessions are keyed to your stable username, not your per-server nickname. `DISCORD_ALLOWED_GUILDS` lets any member of a listed server talk to the bot (DMs unaffected). Threads rename once, after the first reply lands, never mid-turn. Progress updates respect each platform's real message limits.

Multiplexed profiles honor their own `display.reasoning_style`; compact renders "💭 thought for Xs" with an optional "(N tokens)" per-turn count. Only the completed turn-final answer reply-pings the user; streaming previews and interim messages stay standalone. Auto-threaded root-turn finals ping via inline mention when the reply reference cannot attach.

Clarify prompts @mention the requesting user by default (`discord.clarify_mentions: false` to opt out) and are numbered plain text — no buttons; Discord component views time out. `resolve_ticket` propose is terminal: the confirmation embed is the reply, no follow-up message.

The typing indicator stays lit while a background delegated task is still running. MoA consult/debate progress renders as one self-editing embed per call. `delegate_cursor_agent` live progress posts a Cursor-branded embed with a Watch live session hyperlink instead of dumping the URL. `delegate_claude_agent` live progress posts a Claude-branded embed deep-linking the local run viewer after its delegation tool-progress message, matching Cursor ordering.

### WhatsApp missions

Assistant WhatsApp chats expose mission-aware `end_session` or one-way `escalate_task`. Goal-bound WhatsApp missions (missions plugin: `dispatch_assistant`/`end_session`) support mission-only DMs (`platforms.<whatsapp|whatsapp_cloud>.extra.mission_only_dms`, off by default) that are answered only while an assistant-mission is bound to the chat. A mission on a `@g.us` group admits exactly that group — no mention required, all members in one shared session; profile-scoped pairing no longer mirrors into the global allowlist.

Allowlisted groups can observe unmentioned chatter (`observe_unmentioned_group_messages` with `require_mention`, off by default): stored as shared-session context, replied to only on the next ping/mention.

### Cron, config, models, display, and compression

A lifecycle guard blocks cron-spawned commands that would restart the gateway or rewrite the live checkout (quote-aware). Config covers API retry backoff timing and web search and extract fallback chains. `agent.tool_call_narration_guidance` (default on) has the model briefly explain each tool call before making it.

Z.AI silent default is GLM-5.3, with GLM-5.3-Flash as fallback. `security.allow_agent_config_writes` opts out of the write_file/patch guard on the Hermes config file (operator request; default off). Optional native OS notification when a turn finishes (`display.notify_on_complete`, off by default; SSH target supported). `compression.tail_mode` defaults to `lean` (clamped 10-25K tail; `legacy` restores the 0.20×threshold verbatim tail).


<<<<<<< HEAD
**What's different here:**
- **Tools**
  - Delegation — send coding tasks to a Cursor My Machines Cloud Agent in the target checkout, straight from chat
  - Delegation — `delegate_cursor_agent` states its real contract (cloud checkout from pushed refs only) and refuses to start when local HEAD has unpushed commits, so cloud runs can't silently target the wrong repository shape
  - Delegation — send coding tasks to the Claude Code CLI, straight from chat
  - Delegation — `delegate_claude_agent` supports Claude Code `/goal` headless: pass a `/goal <condition>` task and the run loops until a model judge confirms the condition met (verified with the claude-glm wrapper)
  - Delegation — lane rule: `delegate_cursor_agent` for small/medium, `delegate_claude_agent` with default `/goal` for medium/large
  - Delegation — `delegate_claude_agent` writes stream-json run logs, so claude-runs logs are live-tailable for progress reporting
  - Delegation — delegate coding tools run with no stall watchdog and no default wall-clock limit (an optional positive `timeout_seconds` still applies)
  - Delegation — `delegate_task action='list'` surfaces per-child liveness (current tool, iteration, seconds since activity, stalled flag) so a wedged subagent is distinguishable from a slow one
  - Delegation — one shared agent-CLI runner powers both delegate tools and the dev-pipeline build lanes
  - Delegation — `delegate_development plan_mode=debate` runs the multi-round adversarial council for planning; consult stays the default
  - Delegation — dev-pipeline review runs as a Russian-language kimi + grok dual review; `delegate_development` gains `open_pr=false` to skip the draft PR
  - MoA — `moa_ask` and `moa_debate`, restored from the archive
  - Memory (Honcho) — memory tools surface backend failures (dead API keys, auth 401s, timeouts) as explicit errors instead of silently looking like "nothing stored"
- **Plugins**
  - dev-pipeline — hand a repo + task to an automated pipeline: MoA planning, Cursor execution, mechanical verification, dual-model review, draft PR, with plain-English stage-progress messages to your chat (same-phase heartbeats skipped) and a status tool
  - dev-pipeline claude-endurance lane — Claude Code (claude-glm) builds for broad/long tasks
  - dev-pipeline — delegate_development is parked (not registered); dev_pipeline_status stays live
  - code-lane-gate — blocks in-context source edits inside git repos (opt-in via CODE_LANE_GATE_E2E=1) to steer coding to the delegate lanes; terminal writes are a known v1 bypass
  - Discord History — read-only search over an owner-authorized PostgreSQL archive of Discord messages (opt-in, off by default)
  - Papercuts — structured journal of workflow friction, plus an opt-in daily autofix cron (`hermes papercuts autofix install`) that turns small mechanical fixes into PRs
  - Hermes Starts — your AI can open conversations instead of only replying; it creates and pins its own Discord inbox, and each opening is a single message that anchors its own thread
  - Inbox Sparks — once per 4-hour window the agent must weigh starting a conversation before a turn ends (pairs with Hermes Starts)
  - auto_update — safe unattended Hermes updates on Linux/systemd via an independent timer (`hermes auto_update status|enable|disable|reconcile`; default every 30 minutes all day, idle-gated, no randomized delay)
  - quota_channels — Discord model voice channels for six AI providers (one channel each) under a Models category, ordered by the same score as fallback routing (`quota_frac × (168h / hours_to_reset) × uptime_24h × uptime_1h`; OpenRouter is a virtual unlimited Ox Alpha row), with automatic 7-day token enrichment on Codex, z.ai, and Cursor
  - fallback_quota_reorder — score-based primary/fallback quota rotation: `quota_frac × (168h / hours_to_reset) × uptime_24h × uptime_1h` ranks soonest-reset wallets first (unlimited Ox Alpha scored as a synthetic 100%/168h wallet, derated by uptime) and rotates the primary slot to the top scorer
  - fallback_watch — tails agent.log and alerts a Discord channel whenever the primary model falls back, cooldown-deduped (opt-in, off by default)
  - home_server — set a Discord home server once and Hermes provisions and keeps in sync the whole structure (Chat / Notifications / Honcho Memory / Models / Speeds), fully wired; `#gateway-restarts` shows `agents-N` live and `restarting-N-agents` while draining; idempotent, never deletes, a legacy Quotas category is renamed in place to Models, existing home, notification, and rename targets are never clobbered
  - speed_channels — Discord download walls for qBittorrent, SABnzbd, and slskd: voice-channel names carry live throughput and queue depth, category label stays fresh between ticks
  - discord_guests — adding a guest auto-creates a private #{name}-{bot}-lounge under Chat; @everyone stays view-denied (host slug overridable via settings)
- **Other**
  - Gateway — optional live provider retry/fallback progress bubble during stalls (`display.retry_progress`, off by default)
  - Gateway — replies can end with a timing breakdown: total, API, tools, other (off by default upstream)
  - Gateway — steered follow-ups get a second "✅ Steer delivered" ack the moment the text actually lands in the model's context
  - Gateway — `/restart` parks only live chats, resumes only that snapshot, and shows each accepted LLM park steer in its shutdown warning
  - Gateway — chats that got the shutdown warning get a matching ♻️ back-online notice after restart, including raw SIGTERM
  - Gateway — lifecycle broadcasts (shutdown/startup) can route to a dedicated per-platform notification channel, keeping home chats free (`/setnotify`, `/clearnotify`)
  - Gateway — `/sethomeserver` provisions the whole Discord home server from one command (confirm required to move an existing one), wires `#gateway-restarts` as `agents-N` while up and `restarting-N-agents` while draining, then re-syncs at most hourly
  - Discord — sessions keyed to your stable username, not your per-server nickname
  - Discord — `DISCORD_ALLOWED_GUILDS`: any member of a listed server can talk to the bot (DMs unaffected)
  - Discord — threads renamed once, after the first reply lands, never mid-turn
  - Discord — progress updates respect each platform's real message limits
- Discord — multiplexed profiles honor their own display.reasoning_style; compact renders "💭 thought for Xs" with an optional "(N tokens)" per-turn count
  - Discord — only the completed turn-final answer reply-pings the user; streaming previews and interim messages stay standalone
  - Discord — auto-threaded root-turn finals ping via inline mention when the reply reference can't attach
  - Discord — clarify prompts @mention the requesting user by default (`discord.clarify_mentions: false` to opt out)
  - Discord — clarify prompts are numbered plain text (no buttons; Discord component views time out)
  - Discord — resolve_ticket propose is terminal: the confirmation embed is the reply, no follow-up message
  - Gateway — system prompt tells the agent its own name: the bot's platform display name (Discord server nickname/global name) renders as `**Your name:**` in the session context
  - Missions — assistant WhatsApp chats expose mission-aware `end_session` or one-way `escalate_task`
  - Gateway — goal-bound WhatsApp missions (missions plugin: `dispatch_assistant`/`end_session`): mission-only DMs (`platforms.<whatsapp|whatsapp_cloud>.extra.mission_only_dms`, off by default) are answered only while an assistant-mission is bound to the chat; a mission on a `@g.us` group admits exactly that group — no mention required, all members in one shared session; profile-scoped pairing no longer mirrors into the global allowlist
  - WhatsApp — allowlisted groups can observe unmentioned chatter (`observe_unmentioned_group_messages` with `require_mention`, off by default): stored as shared-session context, replied to only on the next ping/mention
  - Memory — Hindsight `bank_id_template` supports `{chat}` so one profile can keep a separate bank per messaging chat
  - Discord — typing indicator stays lit while a background delegated task is still running
  - Discord — MoA consult/debate progress renders as one self-editing embed per call
  - Cron — lifecycle guard blocks cron-spawned commands that would restart the gateway or rewrite the live checkout (quote-aware)
  - Memory — per-turn injection skips lines already delivered earlier in the session
  - Config — API retry backoff timing
  - Agent — tool-call narration guidance: model briefly explains each tool call before making it (`agent.tool_call_narration_guidance`, default on)
  - Config — web search and extract fallback chains
  - Models — Z.AI silent default is GLM-5.3, with GLM-5.3-Flash as fallback
  - Config — `security.allow_agent_config_writes` opts out of the write_file/patch guard on the Hermes config file (operator request; default off)
  - Display — optional native OS notification when a turn finishes (`display.notify_on_complete`, off by default; SSH target supported)
=======
>>>>>>> github/main

For the upstream project, see [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). Everything below this header is upstream's README.

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
