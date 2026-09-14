# Dev Pipeline — Slice 1: durable Cursor lane (implementation spec)

Status: approved architecture, ready for implementation.

**2026-08-19 user directive:** the endurance lane is Claude Code via the claude-glm wrapper (lane `claude-endurance`), superseding the slice-2 glm-endurance design and the Claude-Code-is-out note below. Routing returns `claude` instead of blocking `lane_unavailable`.

Architecture decision record: `/root/.hermes/second-brain/2026-08-10-automated-development-pipeline-moa-debate.md` (MoA debate, 4 configured models, 3 rounds, independently verified).

### Addendum: `plan_mode` (2026-08-19)

`delegate_development` accepts an optional `plan_mode` parameter: `consult` (default) uses `tools.moa_tool.moa_ask`; `debate` uses `tools.moa_debate.moa_debate` directly for multi-round adversarial planning on big or ambiguous tasks. The executor adapts debate output into the same synthesis path as consult — no reimplementation of either tool.

## Goal

One user entrypoint: `delegate_development(repo, task)`. Hermes plans via the configured MoA council, executes bounded work through Cursor, verifies mechanically, reviews with Kimi K3 + Grok 4.5, and opens a draft PR on pass. The job survives gateway restart, executor restart, and host reboot. The user never chooses a lane.

## Non-goals (hard exclusions for this slice)

- No GLM endurance lane in this slice (deferred to slice 2). **Lane harness decision (2026-08-10, user-directed and probed): the GLM lane is the SAME Cursor Agent CLI in its hidden "agent-cli-local" mode — `CURSOR_LOCAL_AGENT_BASE_URL` + `CURSOR_LOCAL_AGENT_API_KEY` pointing at the Alibaba coding-plan OpenAI-compatible endpoint, `--model glm-5.2`. Probed live: chat, tool use (edit tool wrote a file), and full stream-json events all work. Claude Code is OUT of the design entirely.** Slice 2 = an endurance profile of the same attempt runner (different model/env, longer budget, checkpoint cadence), not a second harness. It still does not ship until the observability bar below is met for that profile.
- No ambiguous-task probe lane, no Cursor→GLM escalation, no runtime lane switching.
- No learned routing, semantic progress scoring, or LLM-judged test greenness.
- No concurrent writers, multi-host scheduling, auto-merge, or deployment.
- No new external job database: Kanban (`hermes_cli/kanban_db.py`) is the single durable ledger.
- No modifications to the gateway service, the Kanban dispatcher, or `tools/cursor_agent_tool.py` behavior (read-only reuse of its helpers is fine).

## Lane-agnostic observability requirement (applies to every worker lane, Cursor now, GLM later)

A lane may not ship unless the user can see what it is doing while it runs, without asking a model to summarize. Minimum bar:

1. **Structured event stream per attempt.** Cursor bounded lane: its `--output-format stream-json` JSONL. GLM endurance lane (slice 2): same harness in `agent-cli-local` mode, same stream shape — verified by live probe on 2026-08-10.
2. **Condensed progress events on the Kanban card.** The executor tails the attempt stream and writes coarse `task_events` rows (file edited, command run + exit code, checkpoint created, no-output warnings). Queryable via `hermes kanban show <id>` and by the agent mid-run.
3. **Checkpoint commits as timeline.** Every meaningful step becomes a commit whose message names the milestone; the job branch's `git log` is the progress feed.
4. **Phase-change thread updates.** The existing kanban notifier posts phase transitions (planned → running → verifying → reviewing → done/blocked) to the origin thread as ordinary messages; only the terminal message replies/mentions. Quiet between phases, but state is always queryable.
5. **Stall detection from the same stream.** No stream growth for N minutes = stalled → terminate, checkpoint, classify. Observability doubles as the dead-man switch.

## Existing pieces to reuse (verified in tree)

- `tools/cursor_agent_tool.py`: availability check, stream-json log handling, process-group cleanup lessons. The executor calls the Cursor CLI directly through its own attempt runner; do not import the tool handler itself.
- `hermes_cli/kanban_db.py`: WAL + CAS claims (`claim_task`), `tasks`/`task_runs`/`task_events`, `claim_expires`, `heartbeat_claim`, `worker_pid`, `max_runtime_seconds`, `idempotency_key`, `consecutive_failures` + `max_retries`, `workspace_kind`/`workspace_path`, `block_kind` typed blockers, `task_runs.metadata` JSON. Boards: separate DB per board under `<hermes_root>/kanban/boards/<slug>/`.
- `tools/moa_tool.py`: `moa_ask` — restored verbatim in Component 0 (it is absent from the base checkout; the restore is part of this change, and planning must go through it).
- `kanban_notify_subs` + gateway kanban notifier: completion/blocked notifications to the originating chat — reuse, do not rebuild.
- `gh` CLI: authenticated on this host for PR creation. Executor uses it; attempts never see its credentials.

## Components

### 0. Restore `moa_ask` (prerequisite, exact port)

`tools/moa_tool.py` is absent from this checkout, but `agent/moa_loop.py` is fully present and the recovered wrapper is proven compatible with this exact tree. Restore it verbatim from the Git object store:

```bash
git show 947bd957a:tools/moa_tool.py > tools/moa_tool.py
git show 947bd957a:tests/tools/test_moa_tool.py > tests/tools/test_moa_tool.py
```

Then register `moa_ask` in `toolsets.py` following the documented pattern (`_HERMES_CORE_TOOLS` + `TOOLSETS["moa"]["tools"]`, matching how `cursor_agent_tool` is gated). If the blob paths differ, locate with `git log --all --oneline -- tools/moa_tool.py`. Port exactly; do not "improve" it. Its tests must pass before any executor work begins. The planner MUST use this real `moa_ask` against the active configured preset — no roster overrides, no substitutes. (Note: `tools/moa_debate.py` is deliberately NOT part of this slice; planning uses consult, debate stays for contested decisions.)

### 1. `tools/dev_pipeline_tool.py` — `delegate_development`

Parameters: `repo` (absolute local path or https URL), `task` (string), `branch` (optional; default = repo default branch).

Behavior:
1. Validate repo input (local path must be a git repo; https URL must parse). Reject anything else.
2. Ensure Kanban board `dev` exists (create via existing board helpers if missing).
3. Dedup: if a task with the same `idempotency_key` (sha256 of `repo|branch|task`) exists in status `triage|todo|ready|running|blocked`, return that task id instead of creating a duplicate.
4. Create the Kanban task: `workspace_kind='git'`, body = JSON document `{repo, branch, task, submitted_at}`, `max_retries=2`.
5. If session/chat context is available, register a `kanban_notify_subs` row so the existing notifier reports completion/blocked to the origin thread. Best-effort; never fail job creation on this.
6. Return JSON: `{success, task_id, board, deduplicated, message}`.

Registration: follow the `cursor_agent_tool.py` precedent — service-gated `check_fn` (Cursor `agent` binary resolvable AND kanban importable), registered in the `moa`-adjacent new toolset `dev-pipeline` plus `_HERMES_CORE_TOOLS` gating exactly like the cursor tool does it. Keep the schema description explicit that this is the ONLY way users submit durable dev jobs.

### 2. `hermes_cli/dev_executor.py` — the executor service

Entry: `python -m hermes_cli.dev_executor run` (system service, Restart=always, its own cgroup — installed later by the operator, not by this change). Also expose `python -m hermes_cli.dev_executor attempt <task_id> <run_id>` used as the per-attempt unit's ExecStart, and `reconcile` for one-shot startup reconciliation (invoked automatically by `run` on boot).

Tick loop (default 15s, config `dev_executor.tick_seconds`):
1. Reconcile (see below) on startup, then claim ready tasks from board `dev` via `kanban_db` claim functions with a claim TTL of 15 min + heartbeats every 60s while active.
2. Drive each claimed task through the phase machine below. Persist every phase transition as a `task_events` row (`kind='dev_phase'`, payload JSON: phase, attempt/run id, evidence paths, exit codes). Kanban task `status` stays `running` while working; `task_runs.metadata` carries the pipeline state. The DB is the only source of truth — no in-memory-only state that a restart would need.

Phases:

**PLANNING**
- Build the planning prompt: task, repo summary (file tree top levels, languages, test setup hints from common files), and a demand for a STRICT JSON plan contract (schema below). Call the configured MoA council (`moa_ask`, active preset exactly as configured — never override the roster).
- Parse and validate the contract. On invalid JSON/schema: one retry with the validation errors appended. Still invalid → block the task with `block_kind='plan_invalid'` and the validator output in the task result.
- Planning council failures: if moa_ask reports partial/degraded, proceed only if ≥2 advisors returned usable plans and the acting synthesis can produce one valid contract; otherwise block `planning_unavailable`. Record advisor statuses in task_events.

**ROUTING**
- If contract `blocked_reasons` is non-empty → block with `block_kind` mapped from the reason (`missing_credentials`, `missing_product_input`, `infra_broken`, `acceptance_unverifiable`). Never execute.
- If `lane_hint != 'cursor'` or any broad-change flag is set (`migration`, `repo_wide_change`, `toolchain_change`, `multi_subsystem`, `estimated_minutes > 30`, `long_verification`) → block with `block_kind='lane_unavailable'`, result text explaining the GLM endurance lane is not built yet. This is deliberate, not an error.
- Else proceed Cursor.

**PREPARING**
- Workspace: `<board workspaces root>/<task_id>/`. Clone local path or https URL into `repo/`. Checkout base branch, create `hermes-dev/<task_id>`.
- Ensure `.cursor/agents/` exists in the workspace with the pinned implementer/reviewer agents (ship canonical copies under `hermes_cli/dev_pipeline_assets/cursor-agents/` and copy if absent; if the repo has its own, keep the repo's and record that).
- Record base commit SHA in run metadata.

**RUNNING** (per attempt; attempts are separate `task_runs` rows)
- Spawn attempt as a named transient systemd unit, outside the executor's cgroup:
  `systemd-run --unit=hermes-dev-<task_id>-<run_id> --property=RuntimeMaxSec=2400 --property=MemoryMax=6G --property=OOMScoreAdjust=500 --working-directory=<workspace repo> --setenv=HOME=/root --setenv=PATH=<sanitized> ... /root/hermes-agent/.venv/bin/python -m hermes_cli.dev_executor attempt <task_id> <run_id>`
  (Resolve paths at runtime; never hardcode blindly — use the running interpreter and installed package location.)
- The `attempt` subcommand takes a lane profile argument (`--lane cursor-bounded`; slice 1 implements only this one, but the parameter and config seam exist from day one so slice 2 adds `glm-endurance` as config, not surgery): builds a sanitized environment (allowlist: HOME, PATH, LANG/LC_*, git author/committer from config, Cursor config dirs; explicitly strip `GH_TOKEN`, `GITHUB_TOKEN`, any `*_API_KEY`, `*_OAUTH*` — slice 2's glm-endurance profile is the ONLY exception, injecting exactly `CURSOR_LOCAL_AGENT_BASE_URL` + `CURSOR_LOCAL_AGENT_API_KEY` from the Hermes secrets store, never from the repo or task text), then execs:
  `agent -p --trust --force --model kimi-k3-high --output-format stream-json "<attempt prompt>"`
  streaming JSONL to `<board logs root>/<task_id>/attempt-<run_id>.jsonl`.
- Attempt prompt: task text, plan contract JSON, rules: delegate implementation to `implementer`, then review to `reviewer`, fix blocking findings via implementer, commit with conventional message, do not push, do not create PRs, report structured final summary. (Mirror the proven prompt shape from the cursor-bridge skill.)
- Monitor: every tick, `systemctl is-active <unit>` + heartbeat the claim + record output mtime. Also tail the JSONL stream and write coarse progress `task_events` (per the observability requirement: files edited, commands run + exit codes, checkpoints, no-output warnings). Stall definition: unit active but no JSONL output growth for 10 min → terminate unit (`systemctl stop`), classify `stalled`.
- On unit exit: collect exit code, final JSONL events, `git log` in workspace, candidate commit SHA. Classify: `completed` | `timeout` | `stalled` | `crashed` | `no_changes`.

**VERIFYING (mechanical — no model judgment of greenness)**
- Create a SEPARATE clean worktree/clone of the candidate commit under the workspace `verify/` (never the writer worktree).
- Run each `acceptance_commands[]` entry with per-command timeout (default 600s, cap 1800s), capture exit code + bounded output to evidence files.
- If all pass → mechanical PASS.
- If any fail → clone base commit into `verify-base/`, run the same commands:
  - base also fails same commands → `baseline_failure`: not blocking; record prominently.
  - base passes → `regression`: if attempts remain (max 2 total), start ONE repair attempt (new run row) whose prompt includes the failing commands, outputs, and diff summary; then re-verify once. Still failing → task `failed` with evidence, no PR.
- Commands run with the same sanitized env approach plus whatever the repo needs to build (document that dependency install commands may be part of acceptance commands; they run as the executor user — record this as a known slice-1 boundary).

**REVIEWING (model stage — required)**
Two independent read-only reviews of the candidate diff + evidence, both prompted in Russian (adversarial framing, untrusted-context warning, reasoning in Russian, strict JSON verdict with English `blocking_findings`/`notes` on the final line). Both must produce parseable structured verdicts; unparseable or unavailable = fail-closed (task blocked `review_unavailable`), never silently skipped.

1. **Kimi K3 acceptance review** — via subprocess `hermes chat -Q --provider kimi-coding --model kimi-k3 --toolsets safe -q "<prompt>"` (uses Hermes auth/config, no new client code). Prompt: task, plan contract, unified diff (truncated to ~50KB with note), mechanical verification results. Ask: does the diff actually implement the task; any acceptance gaming (weakened/deleted tests, stubbed implementations, scope drift, contract tampering)? Require STRICT JSON: `{"verdict":"pass|fail","blocking_findings":[...],"notes":[...]}`.
2. **Grok 4.5 adversarial review** — via Cursor read-only review attempt: `agent -p --trust --output-format stream-json` (NO `--force`) in the workspace, instructing orchestration to delegate ONLY to the `reviewer` subagent for a correctness/security review of the committed diff, returning the same strict JSON verdict shape. No writes permitted; absence of `--force` plus readonly subagent is the enforcement.
- Gate: mechanical PASS AND kimi verdict pass AND grok verdict pass → proceed. Any `fail` with blocking findings → same bounded-repair path as regression (one attempt if attempts remain, then re-verify + re-review once). Otherwise task `failed` with full evidence.

**Note (2026 addendum):** For one release the pair was replaced by a single Russian claude-glm pass over a bounded diff file (#32). The pair is restored; the Russian adversarial prompt shape introduced there (untrusted-context warning, strict JSON on the final line, English findings fields) is kept for both reviewers. `reviews.json` stores `kimi` + `grok`. The bounded diff is still written to `review-diff.txt` as a review artifact.

**PUBLISHING**
- Secret scan the full diff before anything leaves the host: built-in conservative regex set (private key blocks, common token shapes like `ghp_`/`sk-`/`xoxb-`/AWS key ids, `.env`-style `KEY=value` with sensitive names). Any hit → block `secret_in_diff`, quarantine evidence, never push.
- `open_pr` (task body, default true): when false, skip push/PR; complete the task with branch/repo/commit in the result (`pr_skipped` recorded in phase metadata).
- Push branch `hermes-dev/<task_id>` to `origin` via `gh`/`git push` using the EXECUTOR's credentials (attempts never had them).
- Draft PR: `gh pr create --draft` with body containing: task, plan contract summary, lane, attempt history, verification commands + exit codes, both review verdicts, evidence paths. Idempotency: search for an existing open PR head `hermes-dev/<task_id>` first; if found, `gh pr comment` with the new evidence instead of creating a duplicate. Marker HTML comment `<!-- hermes-dev-job:<task_id> -->` in the body.
- Task complete (`status='done'`), result = PR URL. The existing kanban notifier then messages the origin thread.

**RECONCILE (startup + periodic)**
For every task in `running` on board `dev`:
1. Read current run metadata: unit name, pid, host_start_time, phase, workspace, candidate SHA, attempt count.
2. `systemctl is-active <unit>`:
   - active → verify pid start-time matches metadata (PID-reuse guard pattern from `tools/process_registry.py`) → adopt: resume heartbeat + monitoring at the recorded phase.
   - not active → inspect workspace:
     - candidate commit exists AND phase was past RUNNING → resume at VERIFYING.
     - phase was RUNNING/PREPARING and no candidate commit → classify attempt `crashed`; if attempts remain → start new attempt from base (or last green checkpoint commit if one exists); else block `executor_restarted`.
     - phase VERIFYING/REVIEWING/PUBLISHING → these phases are executor-local and idempotent → re-enter at the start of the recorded phase.
3. Never start a second writer while a unit for the same task is active. Fencing = unit identity check before any spawn.

**Cancellation**
- If a running task is blocked externally (e.g. `hermes kanban block <id>`), the executor stops the active unit, records `cancelled_by_user`, and leaves the workspace intact for evidence.

### 3. Plan contract schema (strict JSON from the MoA council)

```json
{
  "task_summary": "string",
  "lane_hint": "cursor|broad",
  "estimated_minutes": 0,
  "allowed_paths": ["relative/globs"],
  "acceptance_commands": ["shell commands, run from repo root"],
  "broad_flags": {
    "migration": false, "repo_wide_change": false, "toolchain_change": false,
    "multi_subsystem": false, "long_verification": false
  },
  "blocked_reasons": [],
  "step_plan": [{"id": "s1", "description": "...", "verifiable": true}],
  "assumptions": ["..."]
}
```

Validation: required keys present; `lane_hint` in enum; `acceptance_commands` non-empty list of non-empty strings, each ≤ 500 chars, no shell metacharacter chains that would make per-command timeout meaningless (`&&`/`;` allowed but the whole string is one timed command — document this); `allowed_paths` relative only, no `..`, no absolute; booleans actually bool; `estimated_minutes` int 1..480; `blocked_reasons` subset of `{missing_credentials, missing_product_input, infra_broken, acceptance_unverifiable}`. Reject unknown top-level keys (strict schema).

### 4. Config (`config.yaml` via `hermes config set`, defaults in code)

- `dev_pipeline.enabled` (default false — service refuses to claim when false)
- `dev_pipeline.board` (default `dev`)
- `dev_pipeline.cursor_timeout_seconds` (default 1800, cap 2400)
- `dev_executor.tick_seconds` (default 15)
- `dev_pipeline.max_attempts` (default 2)
- `dev_pipeline.verify_command_timeout` (default 600)

### 5. Systemd packaging (shipped, not installed by this change)

- `packaging/dev-executor/hermes-dev-executor.service`: `Type=simple`, `Restart=always`, `ExecStart=<venv python> -m hermes_cli.dev_executor run`, `Environment=HOME=/root`, `TimeoutStopSec=30`, `KillMode=mixed` acceptable HERE only because attempts live in their own units; document that.
- `packaging/dev-executor/README.md`: install/enable/verify commands, how to check `systemctl status`, journal locations, how cancellation works.

### 6. Tests (`tests/dev_pipeline/`, pytest, no real Cursor/systemd/gh in unit tests)

- Contract validator: valid document; each missing/invalid field; unknown keys; `..`/absolute paths; non-bool flags; empty acceptance commands; oversized commands.
- Router: broad flags → `lane_unavailable`; blocked_reasons mapping; happy path → cursor.
- Tool dedup: existing open task returned; completed task does not block a new submission.
- Phase machine: transitions recorded as events; fail-closed paths emit correct block kinds.
- Reconcile matrix (mock systemctl): unit alive+start-time match → adopt; alive+mismatch → treat as gone; gone+candidate commit → resume at VERIFYING; gone+no commit+attempts left → retry; none left → blocked.
- Verification classification: candidate pass; candidate fail + base fail → baseline; candidate fail + base pass → regression → repair prompt contains failure evidence.
- Review stage: verdict parsing (valid, garbage → fail-closed); gate logic (any fail → repair-or-fail); kimi/grok invocations mocked at subprocess boundary.
- Secret scan: true positives (private key, ghp_, sk-, .env), true negatives, quarantine path.
- PR idempotency: existing PR found → comment path; not found → create; body contains job marker.
- Kanban integration: tmp `HERMES_HOME`, real `kanban_db` (follow existing kanban test fixtures).
- Attempt env sanitization: GH_TOKEN/API keys stripped; HOME/PATH present.

### 7. Observability

- Every phase transition + classification → `task_events` row with evidence paths.
- Artifacts under the board's logs root per task: attempt JSONL, verify logs per command, base-verify logs, review raw outputs + parsed verdicts, secret scan report, PR URL.
- `delegate_development` returns the task id immediately; progress reaches the user through the existing kanban notifier on completion/blocked.

## Acceptance criteria for this implementation (what "done" means)

1. All new tests pass: `python -m pytest tests/dev_pipeline/ -q`.
2. Ruff clean on changed files; `git diff --check` clean.
3. Existing kanban + cursor tool tests still pass: `python -m pytest tests/hermes_cli/test_kanban_goal_mode.py tests/tools/test_cursor_agent_tool.py -q` (and any kanban_db test module present).
4. Build proves env sanitization and reconcile logic via tests, not prose.
5. Manual acceptance (performed by the operator after review, not Cursor): install service on the live host, submit a real small task against a throwaway GitHub repo, `kill -9` the gateway AND executor mid-attempt, restart executor, observe adoption/retry, job completes with a single draft PR and full evidence.

## Working agreements for the implementer

- Base: this worktree, branch `feat/dev-pipeline`. Commit early, conventional messages.
- No new third-party dependencies. Stdlib + existing Hermes modules only.
- Follow existing code style; type hints; docstrings on public functions.
- Do not modify: `hermes_cli/kanban_db.py` claim logic (read-only usage), the gateway, `tools/cursor_agent_tool.py`, systemd unit of the gateway.
- If `moa_ask` cannot be restored from the blob or its tests fail, STOP and report instead of inventing a substitute planner.
- Secrets: never log tokens; evidence files must be safe to attach to PRs (redact).
