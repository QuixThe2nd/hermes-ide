# Papercuts plugin

The papercuts tool records workflow friction in `$HERMES_HOME/papercuts/events.jsonl`.
The optional **autofix** workflow triages open papercuts daily, fixes small mechanical
issues in a scratch git clone, opens PRs, watches CI, and resolves items with evidence.

## Prerequisites

- `gh` CLI installed and authenticated (`gh auth login`)
- Push access to the PR remote you configure at install time
- Papercuts toolset enabled for cron sessions (installed job enables it automatically)
- Gateway running (built-in cron ticker fires inside the gateway)

## Install

```bash
hermes papercuts autofix install
```

Common overrides:

```bash
hermes papercuts autofix install \
  --repo /path/to/your/checkout \
  --remote origin \
  --base-repo owner/repo \
  --schedule "30 8 * * *" \
  --deliver local \
  --max-fixes 3
```

This creates or updates a cron job named `daily-papercuts-autofix`.

## Safety properties

- All git mutations happen in a dated scratch clone under `$HERMES_HOME/scratch`; the live checkout must stay clean.
- At most `--max-fixes` items per run (default 3); judgement items are never attempted.
- PRs must pass CI (or have failures classified as baseline/flaky) before papercuts are resolved.
- Papercuts are never resolved without an open PR URL and verification evidence in the closing note.

## Uninstall

```bash
hermes papercuts autofix uninstall
```

## Status

```bash
hermes papercuts autofix status
```

Shows schedule, last run, last status, and next run for the installed job.
