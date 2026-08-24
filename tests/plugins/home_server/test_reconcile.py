"""Behavior contracts for the home_server reconcile engine.

Idempotency is asserted by *mutating-call counts*, not snapshots: a second
reconcile must issue zero creates and zero message posts.
"""

from __future__ import annotations

from plugins.home_server.core import (
    CHANNEL_TYPE_CATEGORY,
    TEMPLATE,
    reconcile,
)

# 4 categories + 3 chat + 4 memory + 5 quotas + 3 speeds channels.
FIRST_RUN_CREATES = 4 + 3 + 4 + 5 + 3


def test_first_run_creates_the_whole_template(hermes, guild, make_discord):
    discord = make_discord()
    report = reconcile(http_fn=discord)

    assert report["success"] is True
    assert report["guild_id"] == guild
    assert len(report["created"]) == FIRST_RUN_CREATES
    assert discord.count("POST", f"/guilds/{guild}/channels") == FIRST_RUN_CREATES

    categories = [
        c for c in discord.channels.values() if c["type"] == CHANNEL_TYPE_CATEGORY
    ]
    assert sorted(c["name"] for c in categories) == sorted(
        spec.category for spec in TEMPLATE.values()
    )

    for key, spec in TEMPLATE.items():
        under = [
            c
            for c in discord.channels.values()
            if c["parent_id"] and c["name"] in [s.name for s in spec.channels]
        ]
        assert len(under) == len(spec.channels), key


def test_second_run_is_a_noop(hermes, guild, make_discord):
    discord = make_discord()
    reconcile(http_fn=discord)

    before_channels = {k: dict(v) for k, v in discord.channels.items()}
    before_messages = dict(discord.messages)
    mutations_after_first = len(discord.mutations)

    report = reconcile(http_fn=discord)

    assert report["created"] == []
    assert report["embeds_posted"] == []
    assert discord.channels == before_channels
    assert discord.messages == before_messages
    # The second run issued no mutating call at all.
    assert len(discord.mutations) == mutations_after_first


def test_existing_channels_are_discovered_not_recreated(
    hermes, guild, make_discord, state
):
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    discord = make_discord(
        existing=[
            {"id": "11", "name": "Chat", "type": CHANNEL_TYPE_CATEGORY},
            {"id": "12", "name": "inbox", "type": CHANNEL_TYPE_TEXT, "parent_id": "11"},
        ]
    )
    report = reconcile(http_fn=discord)

    assert "category:Chat" not in report["created"]
    assert "channel:inbox" not in report["created"]
    assert discord.channels["12"]["id"] == "12"
    assert state()["channels"]["chat"]["inbox"] == "12"


def test_unknown_extra_channels_are_left_alone_and_nothing_deleted(hermes, make_discord):
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    extra = {"id": "99", "name": "my-own-channel", "type": CHANNEL_TYPE_TEXT}
    discord = make_discord(existing=[extra])
    reconcile(http_fn=discord)
    reconcile(http_fn=discord)

    assert "99" in discord.channels
    assert discord.channels["99"]["name"] == "my-own-channel"


def test_disabled_modules_are_skipped_entirely(
    hermes, guild, make_discord, write_config, read_config
):
    write_config({"guild_id": guild, "modules": {"speeds": False, "memory": False}})
    discord = make_discord()
    report = reconcile(http_fn=discord)

    names = {c["name"] for c in discord.channels.values()}
    assert "Speeds" not in names
    assert "Honcho Memory" not in names
    assert "qBittorrent" not in names
    assert "Chat" in names and "Quotas" in names

    # Disabled modules must not be wired either.
    assert "speed_channels" not in read_config()
    assert report["modules"]["speeds"] is False
    assert report["modules"]["chat"] is True


def test_home_channel_is_set_when_none_exists(hermes, make_discord, read_config, state):
    reconcile(http_fn=make_discord())

    home = read_config()["platforms"]["discord"]["home_channel"]
    assert home["chat_id"] == state()["channels"]["chat"]["home"]
    assert home["name"] == "home"


def test_existing_home_channel_is_never_clobbered(
    hermes, guild, make_discord, write_config, read_config
):
    write_config(
        {"guild_id": guild},
        platforms={
            "discord": {
                "home_channel": {
                    "platform": "discord",
                    "chat_id": "111",
                    "name": "my home",
                }
            }
        },
    )
    report = reconcile(http_fn=make_discord())

    assert report["home_channel"] == "kept"
    assert read_config()["platforms"]["discord"]["home_channel"]["chat_id"] == "111"


def test_welcome_embeds_posted_once_per_category_into_first_channel(
    hermes, make_discord, state
):
    discord = make_discord()
    reconcile(http_fn=discord)

    assert len(discord.messages) == len(TEMPLATE)
    assert set(state()["welcome_embeds"]) == set(TEMPLATE)

    for key, spec in TEMPLATE.items():
        first_id = state()["channels"][key][spec.channels[0].name]
        posted = [m for m in discord.messages.values() if m["channel_id"] == first_id]
        assert len(posted) == 1, key

    reconcile(http_fn=discord)
    assert len(discord.messages) == len(TEMPLATE)


def test_hermes_starts_is_prewired_to_the_shared_inbox(hermes, make_discord, state):
    discord = make_discord()
    report = reconcile(http_fn=discord)

    assert report["wired"]["hermes_starts"] == "wired"
    import json

    starts = json.loads(
        (hermes / "hermes_starts" / "state.json").read_text(encoding="utf-8")
    )
    assert starts["channel_id"] == state()["channels"]["chat"]["inbox"]
    assert starts["channel_name"] == "inbox"

    assert reconcile(http_fn=discord)["wired"]["hermes_starts"] == "skipped"


def test_quota_and_speeds_config_are_written_and_unrelated_keys_kept(
    hermes, guild, make_discord, read_config, state
):
    reconcile(http_fn=make_discord())
    config = read_config()

    assert config["model"]["default"] == "keep-me"
    assert config["quota_channels"]["guild_id"] == guild
    assert config["quota_channels"]["category_id"] == state()["categories"]["quotas"]
    assert set(config["quota_channels"]["channel_ids"]) == {
        "codex",
        "kimi",
        "zai",
        "cursor",
        "grok",
    }
    assert config["speed_channels"]["category_id"] == state()["categories"]["speeds"]
    assert set(config["speed_channels"]["channel_ids"]) == {
        "qbittorrent",
        "sabnzbd",
        "slskd",
    }
    assert (
        config["quota_channels"]["channel_ids"]["codex"]
        == state()["channels"]["quotas"]["Codex"]
    )


def test_memory_webhooks_are_created_and_exported(hermes, make_discord):
    discord = make_discord()
    reconcile(http_fn=discord)

    env = (hermes / ".env").read_text(encoding="utf-8")
    assert "HONCHO_DISCORD_WEBHOOK_EXPLICIT=" in env
    assert "HONCHO_DISCORD_WEBHOOK_INDUCTIVE=" in env
    assert len(discord.webhooks) == 4


def test_memory_webhook_env_is_never_overwritten(hermes, make_discord):
    (hermes / ".env").write_text(
        "HONCHO_DISCORD_WEBHOOK_EXPLICIT=mine-not-yours\n", encoding="utf-8"
    )
    discord = make_discord()
    reconcile(http_fn=discord)

    env = (hermes / ".env").read_text(encoding="utf-8")
    assert "HONCHO_DISCORD_WEBHOOK_EXPLICIT=mine-not-yours" in env
    assert len(discord.webhooks) == 3

    before = len(discord.webhooks)
    reconcile(http_fn=discord)
    assert len(discord.webhooks) == before


def test_disabled_feature_is_a_noop(hermes, make_discord, write_config):
    write_config({"guild_id": ""})
    discord = make_discord()
    report = reconcile(http_fn=discord)
    assert report["enabled"] is False
    assert report["created"] == []
    assert discord.mutations == []
