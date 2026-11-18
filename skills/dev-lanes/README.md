# Delegation lanes

Delegation lanes are configured specializations of Hermes' existing delegation backends. They are not standalone skills: the Markdown files under `souls/` are short system-prompt fragments consumed by the lane runner.

`SOUL.md` is the canonical fork coding conventions source. Direct Claude and native delegation plus every configured lane harness receive it automatically exactly once, including when no skills are configured. `fork-coding-conventions/SKILL.md` is only a compatibility pointer; loading it is not required.

## Parent-only compatibility migration

The legacy external skill at `~/.hermes/lanes/skills/shared/fork-coding-conventions/SKILL.md` belongs to the parent lane and is not modified by this checkout. After this runtime and canonical `SOUL.md` are present in every target lane, preserve that external file's path and frontmatter and replace only its duplicated body with a pointer to the lane checkout's `skills/dev-lanes/SOUL.md`. Use `fork-coding-conventions/SKILL.md` as the pointer template; its repository-relative source reference does not depend on an adjacent copied SOUL. Do not copy the convention body into the external skill.

Deploy in that order so the pointer never precedes automatic injection. Verify one real startup for each of OMP, Claude, and lane-native delegation with `skills: []` and with the compatibility skill loaded, plus direct-native delegation without a skill (its API has no `skills` argument). Each initial payload must contain the complete SOUL exactly once and retain its existing task, context, and lane soul. The parameterized startup regression suite covers these checks with subprocess, network, and model boundaries mocked.

Rollback is limited to restoring the original external `SKILL.md` body from the parent's snapshot. Keep the canonical SOUL and automatic injection installed during rollback so no coding agent loses the conventions. No persistent data or database migration is involved.

## Tools

- `delegate_ui_dev` handles visual implementation, responsive behavior, accessibility, and polish.
- `delegate_client_dev` handles frontend state, hooks, interactions, and data flow.
- `delegate_backend_dev` handles APIs, data layers, migrations, and contracts.
- `delegate_reverse_engineer` handles static-only binary, APK, and protocol analysis.

Each lane tool takes an optional `description` argument — a short phrase shown to the user as progress ("Delegated to backend dev lane: migrating the schema") instead of the bare tool name. It never enters the task prompt.

The bundled defaults use OMP. Every lane can instead select `claude` or `subagent` in config. Models form an ordered fallback chain; only quota and rate-limit failures advance the chain, with OMP `errorStatus` 403 and 429 classified as quota. Other errors stop immediately so an invalid task or missing credential does not fan out across providers.

## Configuration

The lane tools are always registered; there is no opt-in switch. Each lane is visible whenever its harness can actually run: when the `omp` binary is missing from the host, the bundled OMP default falls back to the Claude wrapper, then to the in-process subagent, so the tools work out of the box. A lane explicitly pinned to `claude` or `cursor` in config hides only while that binary is unavailable.

`delegation.lanes` is a per-lane override overlay: entries override the bundled defaults field by field without discarding the rest of a lane's configuration. Config keys accept either `ui_dev` or `delegate_ui_dev` naming.

```yaml
delegation:
  omp:
    binary: omp
    secrets_file: ~/secrets/omp.env
  lanes:
    ui_dev:
      models:
        - openai/gpt-5.2
        - anthropic/claude-sonnet-5-5
    reverse_engineer:
      mcp: ~/secrets/static-analysis-mcp.json
```

The secrets file is a dotenv file containing `LITELLM_API_KEY`. Hermes reads only that value and injects it into the OMP child environment; it never puts the key in argv. There is no hardcoded secrets-file location or environment fallback. `delegation.omp.binary` is optional and defaults to `omp` on `PATH`.

Each lane supports `harness`, `models`, `soul`, `skills`, `mcp`, `tools_allowlist`, `reviewer`, `receipt_fields`, and `goal`. A soul may be literal text or a fragment path. Skills are directories containing `SKILL.md`; their complete contents are injected into the task prompt. Relative paths resolve to an existing in-tree path first, then to the active profile home.

`reverse_engineer` defaults to `goal: true`, which drives OMP RPC goal continuation — the lane keeps working (multi-turn, OMP decides when to continue) until the goal completes, is budget-limited, or is paused. Print-mode one-shot (`goal: false`) sends a single prompt and does not continue; set it explicitly to keep that behavior.

For OMP, `tools_allowlist` becomes `--tools` and a declared `mcp` source is copied to `<workdir>/.omp/mcp.json`. No MCP file is staged for lanes without `mcp`. MCP staging is OMP-specific. Claude receives its allowlist through its existing backend option; native subagents retain their own tool policy.

Every result's `final_report` ends with:

```text
lane: <name> | harness: <harness> | model: <selected> | reviewer: <reviewer> | verdict: <one-line>
```

The JSON result also includes structured receipt data, per-model attempts, and the original backend payload.
