"""Per-platform streaming defaults + dashboard exposure.

Edit-only platforms like Discord and Slack flicker under repeated
editMessage streaming. The shipped default encodes that:
display.platforms.discord.streaming=false. It is a gap-filler (user
values win via deep-merge) and, because the dashboard schema is
generated from DEFAULT_CONFIG, it automatically appears as an editable
toggle in the web UI.
"""

from __future__ import annotations


def test_default_per_platform_streaming_flags():
    from hermes_cli.config import DEFAULT_CONFIG
    plats = DEFAULT_CONFIG["display"]["platforms"]
    assert plats["discord"]["streaming"] is False
