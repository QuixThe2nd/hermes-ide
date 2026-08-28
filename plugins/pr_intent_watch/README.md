# pr_intent_watch

Watches `QuixThe2nd/hermes-ide` for newly opened pull requests and posts one **intent review** comment on each: what the PR is trying to do, whether it is worth a maintainer's time, and — when it claims to fix a bug — whether the write-up describes a real, coherent symptom.

It reviews the **objective, not the code**. The model never sees the diff: the GitHub adapter strips the `patch` field from file payloads before anything leaves it, so the review works from title, body, author, labels, base/head, commit subject lines, and file names + churn only. It is not a code review bot and never requests code changes.

Default-on (`default_enabled: true`), hook-only backend plugin — no model tools, no skill, no system-prompt surface.

## How a review happens

**Primary path — live webhook.** GitHub POSTs `pull_request` events to the plugin's own HTTP listener (`run.py --serve`). Each delivery is HMAC-verified against the stored webhook secret, filtered, and the one PR is reviewed immediately. The handler answers `202` before the LLM call runs, so GitHub never retries a slow endpoint.

**Backup path — in-process poll.** The same `--serve` process runs `run_tick` every `poll_seconds` (default 300). Same seen-map, same marker, same skip rules — the two paths are idempotent with each other.

The poll's tick:

1. List open PRs (newest-updated first, at most 2 pages of 50).
2. **First tick is baseline-silent**: every currently open PR is recorded as seen and **zero comments are posted** — enabling the watch never replays history. (Webhook deliveries are never baseline-silenced: a PR GitHub just announced is by definition new.)
3. Each new PR is checked for skip rules, then reviewed and commented via `review_one_pr` — the same function the webhook path calls. PRs are processed oldest-first, so a rate limit mid-tick still comments the earlier ones.
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
  enabled: true            # false disables the plugin AND stops the service
  repo: QuixThe2nd/hermes-ide
  poll_seconds: 300        # backup poll cadence inside --serve, floored at 60
  listen_host: 0.0.0.0     # webhook bind (reverse-proxied by NPM)
  listen_port: 8645
  webhook_path: /webhooks/pr-intent-watch
  skip_drafts: false
  skip_authors: []         # logins, case-insensitive
  comment: true            # false = review but do not POST
  max_file_names: 40       # cap paths sent to the model
  max_commits: 20
```

Invalid types fall back to defaults; `listen_port` is clamped to 1–65535.

Disable either way: `pr_intent_watch.enabled: false` or `plugins.disabled: [pr_intent_watch]` — both stop and disable the installed service.

## Token

Resolved in this order, never printed: `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth token` (subprocess, 5s timeout). No token → a warning is logged and the tick exits 0, so default-on never fails CI or tokenless hosts.

## State and scheduler

- State: `HERMES_HOME/state/pr_intent_watch.json` (`repo`, `seen` map of PR number → `{head_sha, commented, skipped}`, `baseline_complete`, `webhook_secret`), written atomically under a short exclusive lock so the webhook worker and the poll thread cannot clobber each other's JSON. The `seen` map is capped at the 500 newest PR numbers.
- systemd user unit (Linux with a user manager only; elsewhere reconciled once and skipped with a log line): `~/.config/systemd/user/hermes-pr-intent-watch.service` — `Type=simple`, `ExecStart=… run.py --serve`, `Restart=on-failure`, enabled `--now` on gateway start. The old oneshot+timer pair is retired: a leftover timer from the previous model is stopped and disabled on reconcile, since the poll now lives inside `--serve` (a firing timer would double-poll).
- A rate limit (HTTP 403/429) ends the tick cleanly: already-posted PRs stay marked, unprocessed ones retry next tick.

## The webhook listener

Stdlib-only HTTP server (`http.server`), default `0.0.0.0:8645` — reachable from the LAN reverse proxy, not just loopback.

| Request | Response |
|---|---|
| `GET /health` | `200` `{"status": "ok"}` |
| `GET` webhook path | `200` one-line `pr_intent_watch` body (proxy probes) |
| `POST` webhook path, bad/missing `X-Hub-Signature-256` | `401` |
| `POST` webhook path, `X-GitHub-Event: ping` | `200` `{"ok": true}` |
| `POST` webhook path, any other non-`pull_request` event | `204` |
| `POST` webhook path, `pull_request` but not `opened`/`reopened` (e.g. `synchronize`, `closed`) | `204` |
| `POST` webhook path, wrong `repository.full_name` | `204` |
| `POST` webhook path, `opened`/`reopened` on the configured repo | `202`, review runs on a background worker |

The signature is `hmac.compare_digest` over the raw body with the shared secret. Anything else gets `404`. Reviews serialize on a single worker thread; the secret, tokens, and raw HMAC never appear in logs.

The public endpoint is `https://pr-intent.yazdani.au/webhooks/pr-intent-watch` (an Nginx Proxy Manager host forwards it to `:8645`).

### Registering the GitHub hook

The secret is generated on first serve and stored in the state file (mode 0600) — never in config.yaml. Print it for the one-time registration:

```bash
python -m plugins.pr_intent_watch.run --print-webhook-secret
```

Then register the hook (substitute the printed secret):

```bash
gh api repos/QuixThe2nd/hermes-ide/hooks \
  -f name=web \
  -f config[url]=https://pr-intent.yazdani.au/webhooks/pr-intent-watch \
  -f config[content_type]=json \
  -f config[secret]=SECRET \
  -F config[insecure_ssl]=0 \
  -f events[]=pull_request
```

GitHub answers with a `ping` event on creation; the listener's `200 {"ok": true}` confirms the wiring.

## Running

```bash
python -m plugins.pr_intent_watch.run                 # one poll tick
python -m plugins.pr_intent_watch.run --serve         # webhook listener + poll backup (what systemd runs)
python -m plugins.pr_intent_watch.run --dry-run       # review, post nothing, write no state
python -m plugins.pr_intent_watch.run --print-webhook-secret
python -m plugins.pr_intent_watch.run --config /path/to/config.yaml
```

`--serve` runs the HTTP listener in a daemon thread and the poll loop on the main thread; SIGTERM/SIGINT stop both. It never reconciles the scheduler — arming units is the gateway hook's job. Exit 0 on disabled, no token, rate limit, and network errors; 1 on unexpected exceptions or a taken port (`--serve`).

## Tests

`tests/plugins/pr_intent_watch/` — config normalization, state round-trip, GitHub adapter (patch stripping, marker detection), review parsing, tick behavior (baseline, idempotency, skips, rate limits), lifecycle reconcile, and the CLI.
