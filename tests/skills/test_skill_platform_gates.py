"""Platform gates of optional skills whose bodies are bound to one OS family.

Front-matter ``platforms`` feeds skill selection, so a declared platform the
body cannot run on gets the skill offered and failing at step one. Driven
through the real loader (``agent.skill_utils``), not a frontmatter re-parse.
"""
from pathlib import Path

import pytest

from agent.skill_utils import parse_frontmatter, skill_matches_platform

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("skill", "platform", "why"),
    [
        ("optional-skills/creative/heartmula", "win32", ". .venv/bin/activate"),
        ("optional-skills/mlops/tensorrt-llm", "darwin", "Requires CUDA"),
    ],
)
def test_os_bound_skill_is_not_offered_on_the_wrong_platform(monkeypatch, skill, platform, why):
    fm, body = parse_frontmatter((REPO / skill / "SKILL.md").read_text(encoding="utf-8"))
    assert why in body, f"{skill}: body no longer carries the OS-binding step; re-evaluate its platforms"
    monkeypatch.setattr("agent.skill_utils.sys.platform", platform)
    monkeypatch.setattr("agent.skill_utils.is_termux", lambda: False)
    assert not skill_matches_platform(fm), f"{skill} declares a platform its body cannot run on: {platform}"
