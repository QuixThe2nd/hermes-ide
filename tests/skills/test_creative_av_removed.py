"""Guard: music, video, and Unreal optional skills stay removed."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REMOVED = (
    "audiocraft-audio-generation",
    "heartmula",
    "ai-presenter-video",
    "hyperframes",
    "unreal-mcp",
)


@pytest.mark.parametrize("name", REMOVED)
def test_removed_creative_skill_is_absent(name: str) -> None:
    assert not (REPO / "optional-skills" / "creative" / name).exists(), (
        f"optional-skills/creative/{name} was removed from this fork"
    )
