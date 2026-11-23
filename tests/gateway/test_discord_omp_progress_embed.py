"""Behavior tests for Discord OMP lane progress embeds.

The Discord adapter renders an ``"OMP Lane: <url>"`` mid-tool status line
as a branded embed only when the URL passes the same safety gate the
Claude Code Agent link passes (``plugins.omp_viewer.url.is_allowed_watch_url``).
"""

from __future__ import annotations

import pytest

WATCH_URL = "http://10.0.0.7:8790/#/srv/work/proj/.hermes-lane/litellm/backend_dev"


# ---------------------------------------------------------------------------
# Status-line recognition (the adapter's own strict gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content,expected", [
    (f"OMP Lane: {WATCH_URL}", WATCH_URL),
    (f"  OMP Lane: {WATCH_URL}  ", WATCH_URL),          # whitespace tolerated
    ("OMP Lane: http://8.8.8.8:8790/#/srv/work/x", None),       # public host
    ("OMP Lane: http://viewer.example.com/#/srv/work/x", None),  # bare DNS
    ("OMP Lane: http://10.0.0.7/other#/srv/work/x", None),      # path
    ("OMP Lane: http://10.0.0.7/#../escape", None),             # traversal
    ("OMP Lane:", None),                                        # no URL
    ("OMP Lane: not a url", None),
    ("Claude Code Agent: http://10.0.0.7:8787/#20261123-101010-1", None),
    ("ordinary progress line", None),
])
def test_omp_lane_status_url_gate(content, expected):
    from plugins.platforms.discord.adapter import _omp_lane_status_url

    assert _omp_lane_status_url(content) == expected


# ---------------------------------------------------------------------------
# Embed rendering
# ---------------------------------------------------------------------------


def test_omp_lane_embed_spec_fields():
    from plugins.platforms.discord.adapter import _omp_lane_embed_spec

    spec = _omp_lane_embed_spec(WATCH_URL)

    assert spec["title"] == "OMP Lane"
    assert spec["url"] == WATCH_URL
    assert WATCH_URL in spec["description"]
    assert spec["footer"]


def test_build_omp_lane_embed_carries_url_and_title():
    from plugins.platforms.discord.adapter import _build_omp_lane_embed

    embed = _build_omp_lane_embed(WATCH_URL)

    assert embed.url == WATCH_URL
    assert embed.title == "OMP Lane"


def test_omp_lane_markdown_progress_has_no_raw_url():
    from plugins.platforms.discord.adapter import (
        _OMP_LANE_STATUS_PREFIX,
        _format_omp_lane_markdown_progress,
    )

    line = _format_omp_lane_markdown_progress(WATCH_URL)

    assert line.startswith(_OMP_LANE_STATUS_PREFIX)
    assert WATCH_URL in line
    assert line != f"{_OMP_LANE_STATUS_PREFIX}{WATCH_URL}"  # linked, not dumped


# ---------------------------------------------------------------------------
# Dispatch: prefixes stay disjoint
# ---------------------------------------------------------------------------


def test_agent_progress_payload_routes_omp_line_to_omp_embed():
    from plugins.platforms.discord.adapter import (
        _agent_progress_embed_payload,
        _build_omp_lane_embed,
    )

    payload = _agent_progress_embed_payload(f"OMP Lane: {WATCH_URL}")

    assert payload is not None
    url, markdown, factory = payload
    assert url == WATCH_URL
    assert factory is _build_omp_lane_embed
    assert "OMP Lane" in markdown


def test_agent_progress_payload_still_routes_claude_line():
    from plugins.platforms.discord.adapter import (
        _agent_progress_embed_payload,
        _build_claude_agent_embed,
    )

    payload = _agent_progress_embed_payload(
        "Claude Code Agent: http://10.0.0.7:8787/#20261123-101010-4242"
    )

    assert payload is not None
    assert payload[2] is _build_claude_agent_embed


def test_agent_progress_payload_ignores_ordinary_content():
    from plugins.platforms.discord.adapter import _agent_progress_embed_payload

    assert _agent_progress_embed_payload("working on it") is None
