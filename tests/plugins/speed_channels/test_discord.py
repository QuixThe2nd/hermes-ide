"""Discord rename contracts: change-detection and 429 posture.

Discord allows 2 renames per 10 minutes per channel, so a rename whose target
equals the current name must issue no PATCH at all, and the category — touched
every tick — treats a 429 as a skip rather than a failure.
"""

from __future__ import annotations

import pytest

from plugins.speed_channels.core import (
    SpeedChannelsError,
    discord_headers,
    rename_channel,
)


def test_matching_name_issues_no_patch(hermes, transport):
    headers = discord_headers()
    transport.channels["cq"]["name"] = "qBittorrent: 0 B/s ↓ • 0 in queue"

    assert rename_channel("cq", "qBittorrent: 0 B/s ↓ • 0 in queue", headers, http_fn=transport) == "unchanged"
    assert transport.patch_calls == 0


def test_changed_name_is_patched_once(hermes, transport):
    headers = discord_headers()

    assert rename_channel("cq", "qBittorrent: 2.5 MB/s ↓ • 5 in queue", headers, http_fn=transport) == "renamed"
    assert transport.patched == [("cq", "qBittorrent: 2.5 MB/s ↓ • 5 in queue")]
    # Second call with the now-current name converges without another PATCH.
    assert rename_channel("cq", "qBittorrent: 2.5 MB/s ↓ • 5 in queue", headers, http_fn=transport) == "unchanged"
    assert transport.patch_calls == 1


def test_category_429_is_a_skip_not_a_failure(hermes, transport):
    transport.patch_status = 429

    result = rename_channel(
        "cat", "Speeds • 24/8 9:07am • Next: 9:12am", discord_headers(),
        skip_on_429=True, http_fn=transport,
    )
    assert result == "skipped"


def test_channel_429_still_raises(hermes, transport):
    """A downloader channel must not silently freeze on rate limit."""
    transport.patch_status = 429

    with pytest.raises(SpeedChannelsError, match="429"):
        rename_channel("cq", "qBittorrent: 2.5 MB/s ↓ • 5 in queue", discord_headers(), http_fn=transport)


def test_authorization_header_shape(hermes):
    headers = discord_headers()
    assert headers["Authorization"].startswith("Bot ")
    assert "t0ken" in headers["Authorization"]
