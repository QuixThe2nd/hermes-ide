"""Contracts for the fork coding-conventions pointer and its canonical source."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOUL_TEXT = (REPO_ROOT / "skills/dev-lanes/SOUL.md").read_text(encoding="utf-8")
SKILL_TEXT = (
    REPO_ROOT / "skills/dev-lanes/fork-coding-conventions/SKILL.md"
).read_text(encoding="utf-8")
CANONICAL_TEST_COMMAND = "scripts/run_tests.sh tests/<area>/ -q"
REQUIRED_SECTIONS = (
    "When to Use",
    "Prerequisites",
    "How to Run",
    "Quick Reference",
    "Procedure",
    "Pitfalls",
    "Verification",
)


def _frontmatter_value(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}: (.+)$", SKILL_TEXT, re.MULTILINE)
    assert match is not None, f"missing {name} frontmatter"
    return match.group(1).strip()


def test_soul_tests_use_the_canonical_runner():
    tests_section = SOUL_TEXT.split("## Tests", 1)[1].split("## Diff discipline", 1)[0]

    assert f"`{CANONICAL_TEST_COMMAND}`" in tests_section
    assert "-m pytest" not in tests_section


def test_skill_remains_a_pointer_to_the_single_convention_source():
    assert "`skills/dev-lanes/SOUL.md`" in SKILL_TEXT
    assert "loading this compatibility skill is not required" in SKILL_TEXT
    for convention_heading in (
        "## Ownership",
        "## Commits",
        "## Tests",
        "## Diff discipline",
        "## Reporting",
    ):
        assert convention_heading not in SKILL_TEXT


def test_skill_has_human_first_attribution_and_a_concise_description():
    author = _frontmatter_value("author")
    description = _frontmatter_value("description")

    assert author.startswith("Parsa Yazdani (@QuixThe2nd)")
    assert author.endswith(", Hermes Agent")
    assert len(description) <= 60
    assert description.endswith(".")


def test_skill_uses_the_modern_section_order():
    body = SKILL_TEXT.split("---", 2)[2].lstrip()
    assert body.startswith("# Fork Coding Conventions Skill\n")
    headings = re.findall(r"^## (.+)$", SKILL_TEXT, re.MULTILINE)
    assert headings == list(REQUIRED_SECTIONS)


def test_verification_requires_effective_context_assertions():
    verification = SKILL_TEXT.split("## Verification", 1)[1]

    assert "effective initial system context" in verification
    assert "exactly once" in verification
    assert "file existence alone is insufficient" in verification
    assert "scripts/run_tests.sh tests/tools/test_delegate_conventions_startup.py -q" in verification
