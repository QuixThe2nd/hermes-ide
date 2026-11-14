"""The IB modeling optional skills are not shipped."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REMOVED = (
    "3-statement-model",
    "dcf-model",
    "comps-analysis",
    "lbo-model",
    "merger-model",
)


@pytest.mark.parametrize("name", REMOVED)
def test_removed_finance_skill_is_absent(name: str) -> None:
    assert not (REPO / "optional-skills" / "finance" / name).exists(), (
        f"optional-skills/finance/{name} was removed from this fork"
    )
