"""Dry-run integration check: provision twice, second run must be inert.

The operator's live-fire dogfood gate in miniature — against a fresh
HERMES_HOME and a mocked Discord transport, run the provisioning twice and
prove by *call counts* (never snapshots) that the second run creates no
channels, mints no webhooks, and reposts no embeds.
"""

from __future__ import annotations

from plugins.home_server.core import reconcile

# 5 categories + 2 chat + 3 notifications + 4 memory + 5 model voices + 3 speeds.
FIRST_RUN_CREATES = 5 + 2 + 3 + 4 + 5 + 3


def test_double_provision_second_run_creates_nothing(hermes, guild, make_discord):
    discord = make_discord()

    first = reconcile(http_fn=discord)
    mutations_after_first = len(discord.mutations)

    second = reconcile(http_fn=discord)

    assert len(first["created"]) == FIRST_RUN_CREATES
    assert second["created"] == []
    assert second["embeds_posted"] == []
    # Zero new channel creates, webhook creates, or message posts on run two.
    assert discord.count("POST", f"/guilds/{guild}/channels") == FIRST_RUN_CREATES
    assert discord.count("POST", "/channels/") == len(discord.messages) + len(
        discord.webhooks
    )
    assert len(discord.mutations) == mutations_after_first
