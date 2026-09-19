"""Task-snapshot heading identity across prompt, template, and grounding.

The iterative summarizer prompt, the emitted template, and deterministic
grounding must share one heading. A leftover ``## Active Task`` section is
not disclaimed by SUMMARY_PREFIX and reads as live work (#114479 / #44454).
"""

from types import SimpleNamespace

from agent.context_compressor import (
    ContextCompressor,
    HISTORICAL_TASK_HEADING,
)

_LEGACY_ACTIVE_TASK_HEADING = "## Active Task"


def _iterative_update_prompt() -> str:
    stub = SimpleNamespace(
        tail_mode="lean",
        _previous_summary="PREVIOUS SUMMARY BODY",
        _bound_summary_input=lambda text: text,
    )
    stub._summary_template_sections = ContextCompressor._summary_template_sections
    stub._build_summary_prompt = ContextCompressor._build_summary_prompt.__get__(stub)
    return stub._build_summary_prompt("NEW TURNS", 2000, None, "", True)


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_iterative_update_instruction_names_canonical_heading():
    prompt = _iterative_update_prompt()
    assert f'Update "{HISTORICAL_TASK_HEADING}"' in prompt
    assert f'Update "{_LEGACY_ACTIVE_TASK_HEADING}"' not in prompt
    assert HISTORICAL_TASK_HEADING in prompt


def test_grounding_replaces_legacy_active_task_heading():
    body = (
        f"{_LEGACY_ACTIVE_TASK_HEADING}\n"
        "User asked: 'do X'\n\n"
        "## Goal\n"
        "thing\n"
    )
    grounded = ContextCompressor._ground_historical_task_snapshot.__func__(
        ContextCompressor,
        body,
        [{"role": "user", "content": "do X"}],
    )
    headings = _headings(grounded)
    assert headings.count(HISTORICAL_TASK_HEADING) == 1
    assert _LEGACY_ACTIVE_TASK_HEADING not in headings
    assert "## Goal" in headings


def test_grounding_keeps_single_canonical_heading():
    body = (
        f"{HISTORICAL_TASK_HEADING}\n"
        "User asked: 'stale'\n\n"
        "## Goal\n"
        "thing\n"
    )
    grounded = ContextCompressor._ground_historical_task_snapshot.__func__(
        ContextCompressor,
        body,
        [{"role": "user", "content": "fresh ask"}],
    )
    headings = _headings(grounded)
    assert headings.count(HISTORICAL_TASK_HEADING) == 1
    assert _LEGACY_ACTIVE_TASK_HEADING not in headings
    assert "fresh ask" in grounded
