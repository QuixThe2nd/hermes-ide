# prompts/

Complete (stored-whole) system prompts, one file each, loaded verbatim by
`prompts/load.py` (`load(name)` → the bytes of `<name>.txt`, no trimming).

The chat system prompt is NOT stored here — it stays assembled at runtime in
`agent/system_prompt.py` + `agent/prompt_builder.py`.

| File | Consumer | Constant |
|---|---|---|
| `profile_describer_system.txt` | `hermes_cli/profile_describer.py` | `_SYSTEM_PROMPT` |
| `goal_judge_system.txt` | `hermes_cli/goals.py` | `JUDGE_SYSTEM_PROMPT` |
| `goal_draft_contract_system.txt` | `hermes_cli/goals.py` | `DRAFT_CONTRACT_SYSTEM_PROMPT` |
| `moa_reference_system.txt` | `agent/moa_loop.py` | `_REFERENCE_SYSTEM_PROMPT` |
| `title_generator_system.txt` | `agent/title_generator.py` | `_TITLE_PROMPT_TEMPLATE` (unformatted; `__LANGUAGE_RULE__` substituted at call site) |
| `trajectory_system.txt` | `agent/agent_runtime_helpers.py` | `_TRAJECTORY_SYSTEM_PROMPT` (unformatted; `{tools}` substituted at call site) |
| `memory_query_rewrite_system.txt` | `plugins/memory/query_rewrite.py` | `_SYSTEM_PROMPT` |
| `papercuts_opening_system.txt` | `plugins/papercuts/threads.py` | `_OPENING_SYSTEM_PROMPT` |
| `hermes_starts_dedup_system.txt` | `plugins/hermes_starts/__init__.py` | `_DEDUP_SYSTEM_PROMPT` |
| `mini_swe_bash_agent_system.txt` | `mini_swe_runner.py` | `SYSTEM_PROMPT` |
| `micro_compaction_summarization_system.txt` | `agent/micro_compaction.py` | `_SUMMARY_SYSTEM_PROMPT` |
| `auxiliary_fallback_instructions.txt` | `agent/auxiliary_client.py` | `_RESPONSES_FALLBACK_INSTRUCTIONS` |

Files must stay byte-identical to their consumer constants — enforced by
`tests/prompts/test_prompt_files.py`.
