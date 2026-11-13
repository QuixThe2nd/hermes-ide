"""Byte contract for the stored-whole system prompts under ``prompts/``.

Each complete (stored-whole) system prompt lives as one file in ``prompts/``
and its consumer module assigns the runtime constant from ``prompts.load``.
The constant must equal the file's exact bytes — no appended or stripped
trailing newline, no reflowed text. The chat system prompt is NOT stored
here; it stays assembled in agent/system_prompt.py + agent/prompt_builder.py.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# (file, module, attribute) — one row per relocated stored-whole prompt.
# The two templates (title, trajectory) are compared UNFORMATTED: the file
# holds the template itself (``__LANGUAGE_RULE__`` / ``{tools}`` intact),
# substitution stays at the call site.
PROMPT_CASES = [
    ("profile_describer_system.txt", "hermes_cli.profile_describer", "_SYSTEM_PROMPT"),
    ("goal_judge_system.txt", "hermes_cli.goals", "JUDGE_SYSTEM_PROMPT"),
    ("goal_draft_contract_system.txt", "hermes_cli.goals", "DRAFT_CONTRACT_SYSTEM_PROMPT"),
    ("moa_reference_system.txt", "agent.moa_loop", "_REFERENCE_SYSTEM_PROMPT"),
    ("title_generator_system.txt", "agent.title_generator", "_TITLE_PROMPT_TEMPLATE"),
    ("trajectory_system.txt", "agent.agent_runtime_helpers", "_TRAJECTORY_SYSTEM_PROMPT"),
    ("memory_query_rewrite_system.txt", "plugins.memory.query_rewrite", "_SYSTEM_PROMPT"),
    ("papercuts_opening_system.txt", "plugins.papercuts.threads", "_OPENING_SYSTEM_PROMPT"),
    ("hermes_starts_dedup_system.txt", "plugins.hermes_starts", "_DEDUP_SYSTEM_PROMPT"),
    ("mini_swe_bash_agent_system.txt", "mini_swe_runner", "SYSTEM_PROMPT"),
    ("micro_compaction_summarization_system.txt", "agent.micro_compaction", "_SUMMARY_SYSTEM_PROMPT"),
    ("auxiliary_fallback_instructions.txt", "agent.auxiliary_client", "_RESPONSES_FALLBACK_INSTRUCTIONS"),
]


@pytest.mark.parametrize(("file_name", "module_name", "attr"), PROMPT_CASES)
def test_prompt_file_is_byte_identical_to_constant(file_name, module_name, attr):
    if module_name == "mini_swe_runner":
        # Its import-time load_dotenv() would open the developer's real ~/.hermes/.env
        # (python-dotenv walks up from the module's directory) and die under the
        # hermetic-home guard. The .env read is irrelevant to the byte contract.
        with patch("dotenv.load_dotenv", return_value=False):
            module = importlib.import_module(module_name)
    else:
        module = importlib.import_module(module_name)
    constant = getattr(module, attr)
    data = (PROMPTS_DIR / file_name).read_bytes()
    assert constant.encode("utf-8") == data, (
        f"{module_name}.{attr} drifted from prompts/{file_name}"
    )


def test_loader_returns_file_bytes_verbatim():
    from prompts.load import load

    read_direct = (PROMPTS_DIR / "moa_reference_system.txt").read_text(encoding="utf-8")
    assert load("moa_reference_system") == read_direct
