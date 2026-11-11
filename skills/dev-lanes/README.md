# Delegation lanes

Delegation lanes are configured specializations of Hermes' existing delegation backends. They are not standalone skills: the Markdown files under `souls/` are short system-prompt fragments consumed by the lane runner.

## Tools

- `delegate_ui_dev` handles visual implementation, responsive behavior, accessibility, and polish.
- `delegate_client_dev` handles frontend state, hooks, interactions, and data flow.
- `delegate_backend_dev` handles APIs, data layers, migrations, and contracts.
- `delegate_reverse_engineer` handles static-only binary, APK, and protocol analysis.

Each lane tool takes an optional `description` argument — a short phrase shown to the user as progress ("Delegated to backend dev lane: migrating the schema") instead of the bare tool name. It never enters the task prompt.

The bundled defaults use OMP. Every lane can instead select `claude`, `cursor`, or `subagent` in config. Models form an ordered fallback chain; only quota and rate-limit failures advance the chain, with OMP `errorStatus` 403 and 429 classified as quota. Other errors stop immediately so an invalid task or missing credential does not fan out across providers.

## Opt in

Lane schemas are hidden until `delegation.lanes` is non-empty. Any non-empty mapping opts into all four tools; entries override the bundled defaults field by field. Config keys accept either `ui_dev` or `delegate_ui_dev` naming.

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

Each lane supports `harness`, `models`, `soul`, `skills`, `mcp`, `tools_allowlist`, `reviewer`, and `receipt_fields`. A soul may be literal text or a fragment path. Skills are directories containing `SKILL.md`; their complete contents are injected into the task prompt. Relative paths resolve to an existing in-tree path first, then to the active profile home.

For OMP, `tools_allowlist` becomes `--tools` and a declared `mcp` source is copied to `<workdir>/.omp/mcp.json`. No MCP file is staged for lanes without `mcp`. MCP staging is OMP-specific. Claude receives its allowlist through its existing backend option; Cursor and native subagents retain their own tool policy.

Every result's `final_report` ends with:

```text
lane: <name> | harness: <harness> | model: <selected> | reviewer: <reviewer> | verdict: <one-line>
```

The JSON result also includes structured receipt data, per-model attempts, and the original backend payload.
