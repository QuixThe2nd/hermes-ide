# pr_intent_watch

Watches `QuixThe2nd/hermes-ide` for newly opened pull requests and posts one **intent review** comment on each: what the PR is trying to do, whether it is worth a maintainer's time, and — when it claims to fix a bug — whether the write-up describes a real, coherent symptom.

It reviews the **objective, not the code**. The model never sees the diff: the GitHub adapter strips the `patch` field from file payloads before anything leaves it, so the review works from title, body, author, labels, base/head, commit subject lines, and file names + churn only. It is not a code review bot and never requests code changes.

Default-on (`default_enabled: true`), hook-only backend plugin — no model tools, no skill, no system-prompt surface.

## How a tick runs

1. List open PRs (newest-updated first, at most 2 pages of 50).
2. **First tick is baseline-silent**: every currently open PR is recorded as seen and **zero comments are posted** — enabling the watch never replays history.
3. Each new PR is checked for skip rules, then reviewed and commented. PRs are processed oldest-first, so a rate limit mid-tick still comments the earlier ones.
4. A PR is never re-reviewed on new pushes — intent is about opening, not every commit.
5. Existing comments carrying the marker `<!-- hermes-pr-intent-watch -->` are detected on GitHub, so the bot stays idempotent even if the state file is lost.

The posted comment is exactly:

```
<!-- hermes-pr-intent-watch -->
## Intent review

**Objective:** …
**Worth considering:** yes | no | unclear
**Is this a real bug?** yes | no | n/a

…rationale…
```

## Configuration

Section in `HERMES_HOME/config.yaml` (all keys optional):

```yaml
pr_intent_watch:
  enabled: true            # false disables the plugin AND retires the timer
  repo: QuixThe2nd/hermes-ide
  poll_seconds: 300        # timer cadence, floored at 60
  skip_drafts: false
  skip_authors: []         # logins, case-insensitive
  comment: true            # false = review but do not POST
  max_file_names: 40       # cap paths sent to the model
  max_commits: 20
```

Disable either way: `pr_intent_watch.enabled: false` or `plugins.disabled: [pr_intent_watch]` — both stop and disable a previously installed timer.

## Token

Resolved in this order, never printed: `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth token` (subprocess, 5s timeout). No token → a warning is logged and the tick exits 0, so default-on never fails CI or tokenless hosts.

## State and scheduler

- State: `HERMES_HOME/state/pr_intent_watch.json` (`repo`, `seen` map of PR number → `{head_sha, commented, skipped}`, `baseline_complete`), written atomically. The `seen` map is capped at the 500 newest PR numbers.
- systemd user units (Linux with a user manager only; elsewhere reconciled once and skipped with a log line): `~/.config/systemd/user/hermes-pr-intent-watch.service` (oneshot, no `[Install]`) + `hermes-pr-intent-watch.timer` (`Persistent=true`, `AccuracySec=1s`, `OnCalendar` derived from `poll_seconds` — 300 → every 5 minutes). Reconciled on gateway start; the timer never runs from `run.py`.
- A rate limit (HTTP 403/429) ends the tick cleanly: already-posted PRs stay marked, unprocessed ones retry next tick.

## Running

```bash
python -m plugins.pr_intent_watch.run            # one tick
python -m plugins.pr_intent_watch.run --dry-run  # review, post nothing, write no state
python -m plugins.pr_intent_watch.run --config /path/to/config.yaml
```

Exit 0 on disabled, no token, rate limit, and network errors; 1 only on unexpected exceptions.

## Tests

`tests/plugins/pr_intent_watch/` — config normalization, state round-trip, GitHub adapter (patch stripping, marker detection), review parsing, tick behavior (baseline, idempotency, skips, rate limits), lifecycle reconcile, and the CLI.
