# deep_research

One model tool, `delegate_research`, hands a substantial research brief to durable worker sessions and returns a single cited report. The parent agent clarifies scope with the user, shows them the brief, starts the job, and then **waits** — the tool description forbids running its own searches in parallel, and the job re-enters the originating session when it finishes.

Workers run as the `researcher` profile (configurable via `deep_research.worker_profile`). The plugin never mutates that profile; it only invokes it one-shot.

## The tool

`delegate_research` lives in the `web` toolset. One schema, five actions:

| Action | Required | What it does |
|---|---|---|
| `start` | `brief` | Creates the job, spawns the durable runner, returns immediately with `job_id` + job dir. Optional `research_questions` (1–8 lanes), `timeout_minutes` (5–60), `max_parallel` (1–4). |
| `status` | `job_id` | Phase, lane counts, updated time, blocker/error. |
| `cancel` | `job_id` | Stops exactly this job's runner; marks it `cancelled`. |
| `result` | `job_id` | Completed → the final report plus report/evidence paths. Otherwise → current status, never a plausible partial report. |
| `list` | — | Recent jobs, one bounded line each. |

The tool is hidden (`check_fn`) when the plugin is disabled, the configured worker profile does not exist, `HERMES_BIN` points at a missing file, or the process is itself a research worker (`HERMES_RESEARCH_JOB` is set — research cannot nest).

## Architecture

```
delegate_research start
  └─ jobs.create_job()                  $HERMES_HOME/research_jobs/rj_<hex12>/
  └─ launcher.launch()                  systemd-run --user (or detached fallback)
       └─ python -m plugins.deep_research.runner --job rj_… --hermes-home …
            ├─ lane 0..N  hermes -p researcher --cli chat -Q --query-file prompts/lane_N.md
            ├─ synthesis  hermes -p researcher … -t file_readonly   (no retrieval tools)
            └─ citations.validate_citations() → publish report.md, or fail closed
```

Nothing user-controlled ever reaches a command line. The brief and lane objectives are untrusted data: they are written to 0o600 prompt files under the job dir and handed to workers by path (`--query-file`), fenced as `DATA` blocks with any embedded fence markers neutralized. argv lists only, never a shell string.

### Job directory

All artifacts live under `$HERMES_HOME/research_jobs/<job_id>/` (0o700 dir, 0o600 files, atomic temp+fsync+replace writes):

| File | Contents |
|---|---|
| `request.json` | Frozen brief, lanes, budgets, worker profile, origin session ids. Written once. |
| `status.json` | `state` (`queued`/`running`/`synthesizing`/`completed`/`failed`/`cancelled`), phase, per-lane state, timestamps, error, runner identity, synthesis bookkeeping. |
| `prompts/*.md` | The argv-free prompt transport for each lane and the writer passes. |
| `lanes/<n>.md` | Each lane worker's report, verbatim. |
| `evidence.jsonl` | Append-only ledger of fetched sources (below). Pre-created empty and private at job creation. |
| `report.md` | The final report. Published only after citation validation passes. |
| `report.draft.md` | A failed synthesis draft, preserved for inspection, never published. |
| `runner.log` | Bounded (256 KiB rotating), secret-free — ids, states, exit codes, durations only. No brief text. |

Job ids are canonical (`rj_` + 12 hex) and validated on every access and every path resolution, so there is no traversal and `cancel`/`result` only ever address jobs under the current `HERMES_HOME`.

### Worker execution

On Linux with a usable systemd **user** manager, the runner is a transient user service — `systemd-run --user --unit=hermes-research-<job_id> --collect` with bounded `RuntimeMaxSec` (job budget + 300 s slack) and `MemoryMax`, plus `OOMScoreAdjust=500`. Deliberately *not* `--scope`: a scope would keep the runner inside the gateway's cgroup and die with a gateway restart. A transient service owns its own cgroup, so the job survives the gateway. `cancel` stops that one unit — systemd reaps its whole cgroup — and never touches other Hermes processes.

Each lane is one independent `researcher` session with that profile's tools (`web`, `browser`, `file_readonly`). Lanes run concurrently up to `max_parallel`. Without `research_questions`, exactly one lane runs the frozen brief. The lane prompt requires reading full pages (not snippets), preferring primary sources, two independent or one authoritative source per material claim, reporting conflicts and coverage gaps, and citing only fetched URLs.

Synthesis runs once, after every requested lane has succeeded. The writer is the same profile but with `--toolsets file_readonly`, so it has no retrieval tool at all and cannot do new research; its only inputs are the frozen brief and the lane reports.

**Fail closed.** A lane failure or timeout, a writer failure, or a citation failure after the single correction pass marks the job `failed` with the reason; artifacts are preserved and no `report.md` is published. A partial report is never presented as complete.

### Evidence and citations

A `post_tool_call` hook records sources into the job's `evidence.jsonl`. It is a strict no-op unless the runner set `HERMES_RESEARCH_EVIDENCE` to a canonical ledger path — ordinary conversations, including the parent that started the job, record nothing. Only successful `web_extract` results (per-URL, ignoring per-URL failures) and successful `browser_navigate` calls are recorded. `web_search` snippets are discovery and are never evidence. Each record is `{url, normalized_url, tool, lane, fetched_at, title, status}` — appended under `O_APPEND` + `flock`, never page bodies, never secrets.

Before publication, every http(s) URL in the report must normalize-match a ledger entry, and a non-empty report must cite at least one source. On failure the writer gets one bounded correction pass restricted to the allowed URL list (still no retrieval); if that also fails, the job is `failed` and the draft is preserved as `report.draft.md`.

> **Limitation.** This proves *URL provenance* — every cited URL was actually fetched during the job. It does **not** prove that a cited page semantically supports the claim it is attached to. `result` states this in its `citation_check` metadata.

## Non-systemd fallback

Without a usable user systemd (macOS, Windows, containers, or a system-service context with no user D-Bus session), the runner spawns detached instead (`start_new_session=True` / `windows_detach_popen_kwargs()`), output captured to `runner.out` in the job dir. The mode is recorded in `status.json` and surfaced by `status`/`start` as reduced durability: it survives a gateway *process* restart, but not a cgroup-wide host supervisor stop. `runner_mode: fallback` in config forces it; `systemd` forces the service path. Artifacts and recovery behave identically either way.

## Completion and recovery

A daemon watcher (started from `on_gateway_start`, off under `HERMES_TEST_ISOLATION`) notices terminal-but-unnotified jobs and pushes an `async_delegation`-shaped event onto the gateway's completion queue, so the outcome re-enters the originating session without polling. Notification is marked at-least-once and coalesced per job. Because everything durable is on disk, a lost in-memory event costs nothing: `status` and `result` recover the job at any time, across gateway restarts.

On gateway start, non-terminal jobs whose runner is verifiably gone (unit inactive, or PID dead with a PID-reuse guard) become `failed` with reason `interrupted: runner not running after restart`. An inconclusive liveness probe never fails a job, and completed artifacts stay readable.

## Configuration

Top-level `HERMES_HOME/config.yaml` section (defaults shown; malformed values degrade to defaults rather than disabling the tool):

```yaml
deep_research:
  enabled: true                # false hides the tool entirely
  worker_profile: researcher   # the profile that runs lanes and synthesis
  default_timeout_minutes: 30  # per job, 5–60
  max_parallel: 2              # concurrent lanes, 1–4
  memory_max: 2G               # systemd MemoryMax per runner unit
  runner_mode: auto            # auto | systemd | fallback
  notify_interval_seconds: 5.0 # completion watcher sweep
  max_recent_jobs: 20          # bound on list output
```

The plugin can also be disabled through the standard deny-list (`plugins.disabled: [deep_research]`), which always wins.

## Security notes

- Briefs, lane questions, and retrieved page content are data. They cannot alter the brief, lane boundaries, budgets, the runner command, paths, or status — they reach workers only as private prompt files, and prompt-injection strings are inert by construction (verified by test).
- The schema exposes no command, path, or executable parameters; the only model-supplied identifier is a canonical job id.
- No secrets in argv, status, logs, evidence, report metadata, or tool results.
- Job dirs and files are private (0o700/0o600); ledger and prompt paths are canonicalized and validated before use.
