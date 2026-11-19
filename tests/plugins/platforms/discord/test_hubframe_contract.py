"""Shared-contract parity for the Discord hub frame-size limit.

``internal/hubframe`` (Go twin) and ``plugins.platforms.discord.hub_client``
must agree on the maximum hub frame size: the Python client opens its unix
socket with a StreamReader limit of ``_HUB_STREAM_LIMIT`` so frames larger
than asyncio's 64 KiB default arrive whole instead of raising
LimitOverrunError and being dropped.

The shared testdata table (``internal/hubframe/testdata/hubframe_cases.json``)
pins the value the Go constant must equal (asserted by the Go module's own
tests); this test asserts the Python constant equals the same table entry,
so Python == table == Go. Production Python never runs the Go binary.
"""

from __future__ import annotations

import json
from pathlib import Path

from plugins.platforms.discord.hub_client import _HUB_STREAM_LIMIT

_TABLE = (
    Path(__file__).resolve().parents[4]
    / "internal"
    / "hubframe"
    / "testdata"
    / "hubframe_cases.json"
)


def test_python_hub_stream_limit_matches_the_shared_table():
    table = json.loads(_TABLE.read_text(encoding="utf-8"))
    assert _HUB_STREAM_LIMIT == table["stream_limit_bytes"], (
        "hub stream limit drifted from the Go twin's shared table: "
        f"python={_HUB_STREAM_LIMIT} table={table['stream_limit_bytes']}"
    )


def test_shared_table_covers_the_limit_contract():
    """The table keeps pinning the boundaries that make the limit meaningful."""
    table = json.loads(_TABLE.read_text(encoding="utf-8"))
    by_name = {case["name"]: case for case in table["frames"]}
    required = (
        "just_over_the_64kib_asyncio_default",
        "content_exactly_at_stream_limit",
        "content_one_byte_over_stream_limit",
    )
    for name in required:
        assert name in by_name, f"shared table lost the {name} case"
    assert by_name["just_over_the_64kib_asyncio_default"]["accepted"] is True
    assert by_name["just_over_the_64kib_asyncio_default"]["content_bytes"] > 65536
    at_limit = by_name["content_exactly_at_stream_limit"]
    assert at_limit["accepted"] is True and at_limit["content_bytes"] == _HUB_STREAM_LIMIT
    over_limit = by_name["content_one_byte_over_stream_limit"]
    assert over_limit["accepted"] is False and over_limit["content_bytes"] == _HUB_STREAM_LIMIT + 1
