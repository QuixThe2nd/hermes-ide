"""The `hermes-agent` skill is what a running Hermes knows about itself.

The docsite is gone from the tree (7b383376efd), so an installed Hermes has no
local copy of the user guide; skills ARE synced into `$HERMES_HOME/skills/`.
The skill therefore does not try to restate the product — it routes to the
hosted `llms.txt` index. These tests keep that routing honest: the exact
external index URL must be what the shipped skill carries, and every
reference has to be reachable, otherwise a shipped feature is invisible and
the agent answers "Hermes can't do that."
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / "skills" / "autonomous-ai-agents" / "hermes-agent"
SKILL_MD = SKILL_DIR / "SKILL.md"

# The published index, verbatim. The generator that derived this from the
# docs tree left with the site — the URL itself is now the contract.
PUBLISHED_INDEX = "https://hermes-agent.nousresearch.com/docs/llms.txt"


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def test_every_referenced_file_exists(skill_text):
    """Routing a question to a file that isn't there is a dead end."""
    targets = set(re.findall(r"`((?:references|templates)/[^`]+)`", skill_text))

    assert targets, "the skill's routing table no longer references any files"
    for target in sorted(targets):
        assert (SKILL_DIR / target).exists(), f"SKILL.md routes to missing {target}"


def test_every_reference_is_reachable_from_the_skill(skill_text):
    """An unrouted reference is one the agent will never think to open.

    This is the failure that produced the original complaint: content can exist
    and still be invisible because nothing points at it.
    """
    on_disk = {f"references/{path.name}" for path in (SKILL_DIR / "references").glob("*.md")}
    routed = set(re.findall(r"`(references/[^`]+)`", skill_text))

    assert not (on_disk - routed), (
        f"reference files no reader will ever reach: {sorted(on_disk - routed)} — "
        "add a routing-table row in SKILL.md"
    )


def test_unknown_features_route_to_the_published_index(skill_text):
    """The catch-all is what makes coverage of the whole product possible."""
    assert "/docs/llms.txt" in skill_text
    # web_extract can be disabled; terminal never is.
    assert "curl" in skill_text, "no way to reach the index without web tools"


def test_the_index_is_published_at_the_exact_external_url(skill_text):
    """The shipped skill must carry the exact published index URL — not a
    relative `/docs/llms.txt` the model can't resolve — and the
    no-web-tools fallback must curl that same address."""
    assert PUBLISHED_INDEX in skill_text, (
        f"the shipped skill must route unknown-feature questions to {PUBLISHED_INDEX}"
    )
    assert f"curl -s {PUBLISHED_INDEX}" in skill_text, (
        "the curl fallback must target the exact index URL"
    )
