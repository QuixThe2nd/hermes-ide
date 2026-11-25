"""Behavioral contract for xurl / x_search routing guidance.

These tests assert structural invariants (required topics + placement of the
routing guidance), not frozen prose snapshots.

Placement contract (July 2026; the docsite feature page was removed with the
site in 7b383376efd):
- The xurl SKILL must NOT name `x_search` (or any other credential-gated
  surface): the skill loads even when that tool isn't registered, so it must
  describe its own search distinctively in its own terms (raw, engageable
  post objects as the authenticated account).
- Cross-surface routing guidance lives where both surfaces are known to
  exist together: the x_search toolset description and the shipped
  X_SEARCH_SCHEMA (its action-word scope is also pinned in
  tests/tools/test_x_search_tool.py).
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
XURL_SKILL = REPO_ROOT / "skills" / "social-media" / "xurl" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _contains_any(text: str, *needles: str) -> bool:
    lowered = text.lower()
    return any(n.lower() in lowered for n in needles)


def test_xurl_skill_never_names_credential_gated_surfaces():
    """The skill must be self-contained: no cross-references to tools that
    may not be registered (x_search is check_fn-gated on xAI credentials)."""
    lowered = _read(XURL_SKILL).lower()
    assert "x_search" not in lowered
    assert "web_search" not in lowered


def test_xurl_skill_search_is_distinct_standalone():
    """Search must be described so an agent can route correctly even when
    another X search surface exists — raw engageable posts, authenticated."""
    text = _read(XURL_SKILL)
    assert _contains_any(text, "raw post")
    assert _contains_any(text, "authenticated")
    assert _contains_any(text, "engage", "engageable")
    # Distinguish from synthesized-answer surfaces in xurl's own terms.
    assert _contains_any(text, "summarized answer", "summary of a topic")


def test_x_search_toolset_routes_account_actions_to_xurl():
    """The surviving cross-surface router is the toolset description:
    x_search is read-only public X discovery, and authenticated X reads +
    account actions are routed to the xurl skill by name."""
    from toolsets import TOOLSETS

    description = TOOLSETS["x_search"]["description"].lower()

    assert _contains_any(description, "read-only public", "public x discovery")
    assert "xurl" in description
    assert _contains_any(description, "authenticated")
    assert _contains_any(description, "account actions")


def test_x_search_boundary_agrees_between_schema_and_toolset():
    """The schema (what the model sees) and the toolset description (what the
    config surface says) draw the same boundary: read-only discovery inside
    the tool; the authenticated account-action surface stays outside it."""
    from toolsets import TOOLSETS
    from tools.x_search_tool import X_SEARCH_SCHEMA

    schema = X_SEARCH_SCHEMA["description"].lower()
    toolset = TOOLSETS["x_search"]["description"].lower()

    assert "read-only" in schema and "read-only" in toolset
    assert _contains_any(
        schema, "outside this tool", "separate authenticated x api surface"
    )


def test_xurl_guidance_says_only_x_output_proves_a_write():
    """Write confirmation must come from xurl / the X API, never from a
    discovery surface like x_search: the xurl skill's own workflow carries
    that rule."""
    text = _read(XURL_SKILL).lower()
    assert _contains_any(
        text,
        "only the `xurl` command output",
        "xurl` output",
        "x api response",
    )
    assert _contains_any(
        text, "never report a write", "never evidence"
    )
