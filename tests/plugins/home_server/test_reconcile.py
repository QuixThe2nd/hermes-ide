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

# 5 categories + 2 chat + 3 notifications + 4 memory + 6 model voices + 3 speeds.
FIRST_RUN_CREATES = 5 + 2 + 3 + 4 + 6 + 3


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

    names = {c["name"] for c in discord.channels.values()}
    # Chat is inbox/outbox only — the old Chat/home channel is gone from the
    # template and must not be minted anymore.
    assert "home" not in names
    for name in ("model-fallback", "gateway-restarts", "other"):
        assert name in names

    for key, spec in TEMPLATE.items():
        under = [
            c
            for c in discord.channels.values()
            if c["parent_id"] and c["name"] in [s.name for s in spec.channels]
        ]
        assert len(under) == len(spec.channels), key


def test_second_run_is_idempotent(hermes, guild, make_discord):
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
    assert "Chat" in names and "Models" in names

    # Disabled modules must not be wired either.
    assert "speed_channels" not in read_config()
    assert report["modules"]["speeds"] is False
    assert report["modules"]["chat"] is True


def test_home_channel_is_set_when_none_exists(hermes, make_discord, read_config, state):
    report = reconcile(http_fn=make_discord())

    assert report["home_channel"] == "set"
    home = read_config()["platforms"]["discord"]["home_channel"]
    assert home["chat_id"] == state()["channels"]["notifications"]["other"]
    assert home["name"] == "other"


def test_notification_channel_is_set_when_none_exists(
    hermes, make_discord, read_config, state
):
    report = reconcile(http_fn=make_discord())

    assert report["notification_channel"] == "set"
    notify = read_config()["platforms"]["discord"]["notification_channel"]
    assert (
        notify["chat_id"] == state()["channels"]["notifications"]["gateway-restarts"]
    )
    assert notify["name"] == "gateway-restarts"


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


def test_existing_notification_channel_is_never_clobbered(
    hermes, guild, make_discord, write_config, read_config
):
    """A notification channel set by /setnotify survives provisioning."""
    write_config(
        {"guild_id": guild},
        platforms={
            "discord": {
                "notification_channel": {
                    "platform": "discord",
                    "chat_id": "222",
                    "name": "my restarts",
                }
            }
        },
    )
    report = reconcile(http_fn=make_discord())

    assert report["notification_channel"] == "kept"
    assert (
        read_config()["platforms"]["discord"]["notification_channel"]["chat_id"]
        == "222"
    )


def test_disabled_notifications_module_is_skipped_entirely(
    hermes, guild, make_discord, write_config, read_config
):
    write_config({"guild_id": guild, "modules": {"notifications": False}})
    discord = make_discord()
    report = reconcile(http_fn=discord)

    names = {c["name"] for c in discord.channels.values()}
    assert "Notifications" not in names
    assert "model-fallback" not in names
    assert "gateway-restarts" not in names
    assert "other" not in names
    assert report["modules"]["notifications"] is False

    # No wiring either: both channel pointers stay untouched.
    assert report["home_channel"] == "skipped"
    assert report["notification_channel"] == "skipped"
    discord_section = read_config().get("platforms", {}).get("discord", {})
    assert "home_channel" not in discord_section
    assert "notification_channel" not in discord_section


def test_welcome_embeds_posted_once_per_text_category_and_skipped_for_voice(
    hermes, make_discord, state
):
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    discord = make_discord()
    reconcile(http_fn=discord)

    text_keys = [
        key
        for key, spec in TEMPLATE.items()
        if any(c.kind == CHANNEL_TYPE_TEXT for c in spec.channels)
    ]
    voice_keys = [key for key in TEMPLATE if key not in text_keys]

    assert len(discord.messages) == len(text_keys)
    assert set(state()["welcome_embeds"]) == set(TEMPLATE)

    for key in text_keys:
        spec = TEMPLATE[key]
        first_text = next(c for c in spec.channels if c.kind == CHANNEL_TYPE_TEXT)
        first_id = state()["channels"][key][first_text.name]
        posted = [m for m in discord.messages.values() if m["channel_id"] == first_id]
        assert len(posted) == 1, key

    for key in voice_keys:
        assert state()["welcome_embeds"][key] == "skipped-no-text-channel"

    reconcile(http_fn=discord)
    assert len(discord.messages) == len(text_keys)


def test_notifications_welcome_embed_is_posted_once(hermes, make_discord, state):
    """Notifications has text channels, so it gets the one-time category embed."""
    discord = make_discord()
    report = reconcile(http_fn=discord)

    assert "Notifications/model-fallback" in report["embeds_posted"]
    channel_id = state()["channels"]["notifications"]["model-fallback"]
    posted = [m for m in discord.messages.values() if m["channel_id"] == channel_id]
    assert len(posted) == 1

    second = reconcile(http_fn=discord)
    assert second["embeds_posted"] == []
    assert len(posted) == 1


def test_dynamic_category_label_is_adopted_not_duplicated(hermes, make_discord, state):
    """A "Models • <clock>" category from the live poller matches Models."""
    discord = make_discord()
    discord.add_channel(id=7001, name="Models \u2022 25/8 3:30pm", type=4)
    for i, key in enumerate(("Codex", "Kimi", "z.ai", "Cursor", "Grok", "OpenRouter")):
        discord.add_channel(id=7100 + i, name=key, type=2, parent_id=7001)

    report = reconcile(http_fn=discord)

    assert "category:Models" not in report["created"]
    assert report["renamed"] == []
    assert report["adopted"] == ["category:Models"]
    assert state()["categories"]["quotas"] == "7001"
    # No duplicate voice channels were minted.
    voices = [
        c
        for c in discord.channels.values()
        if c["type"] == 2 and c["parent_id"] == 7001
    ]
    assert len(voices) == 6


def test_orphan_channels_are_adopted_into_the_module_category(
    hermes, guild, make_discord, state
):
    """An existing #inbox at guild level moves under Chat instead of a dupe."""
    discord = make_discord()
    discord.add_channel(id=8001, name="inbox", type=0)

    report = reconcile(http_fn=discord)
    chat_cat = state()["categories"]["chat"]

    assert "channel:inbox" not in report["created"]
    assert f"channel:inbox" in [a.split(":")[0] + ":" + a.split(":")[1] for a in report["adopted"]]
    assert state()["channels"]["chat"]["inbox"] == "8001"
    moved = discord.channels["8001"]
    assert moved["parent_id"] == chat_cat


def test_orphan_notification_channels_are_adopted_not_recreated(
    hermes, make_discord, state, read_config
):
    """Guild-level #model-fallback/#gateway-restarts/#other move under
    Notifications instead of being duplicated, and the channel pointers adopt
    the discovered IDs."""
    discord = make_discord()
    discord.add_channel(id=8101, name="model-fallback", type=0)
    discord.add_channel(id=8102, name="gateway-restarts", type=0)
    discord.add_channel(id=8103, name="other", type=0)

    report = reconcile(http_fn=discord)
    notifications_cat = state()["categories"]["notifications"]

    adopted = set(report["adopted"])
    for name, cid in (
        ("model-fallback", "8101"),
        ("gateway-restarts", "8102"),
        ("other", "8103"),
    ):
        assert f"channel:{name}" not in report["created"]
        assert f"channel:{name}" in adopted
        assert state()["channels"]["notifications"][name] == cid
        assert discord.channels[cid]["parent_id"] == notifications_cat

    platforms = read_config()["platforms"]["discord"]
    assert platforms["home_channel"]["chat_id"] == "8103"
    assert platforms["notification_channel"]["chat_id"] == "8102"


def test_leftover_chat_home_channel_is_never_deleted(hermes, make_discord):
    """A Chat/#home left by a previous provision stays exactly where it was:
    the template no longer references it, and reconcile never deletes."""
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    discord = make_discord(
        existing=[
            {"id": "21", "name": "Chat", "type": CHANNEL_TYPE_CATEGORY},
            {"id": "22", "name": "home", "type": CHANNEL_TYPE_TEXT, "parent_id": "21"},
        ]
    )
    report = reconcile(http_fn=discord)

    assert "channel:home" not in report["created"]
    assert discord.channels["22"]["name"] == "home"
    assert discord.channels["22"]["parent_id"] == "21"


def test_second_run_after_adoption_is_a_full_no_op(hermes, make_discord, state):
    discord = make_discord()
    discord.add_channel(id=8001, name="inbox", type=0)
    discord.add_channel(id=8002, name="other", type=0)
    reconcile(http_fn=discord)
    before = list(discord.mutations)

    report = reconcile(http_fn=discord)

    assert report["created"] == []
    assert report["adopted"] == []
    assert discord.mutations[len(before):] == []


def test_prefix_collision_never_steals_another_modules_channel(
    hermes, make_discord, state
):
    """A channel already claimed by an earlier module is not re-adopted."""
    discord = make_discord()
    # "Speeds" category created by hand BEFORE reconcile; its children must stay.
    discord.add_channel(id=9001, name="Speeds", type=4)
    discord.add_channel(id=9002, name="qBittorrent", type=2, parent_id=9001)

    reconcile(http_fn=discord)

    speeds_cat = state()["categories"]["speeds"]
    assert speeds_cat == "9001"
    assert discord.channels["9002"]["parent_id"] == 9001


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
        "openrouter",
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


def test_models_template_has_six_voice_channels(hermes, make_discord, state):
    """The canonical fresh provision creates a Models category with six rows."""
    discord = make_discord()
    reconcile(http_fn=discord)

    models_cat = state()["categories"]["quotas"]
    voices = sorted(
        c["name"]
        for c in discord.channels.values()
        if c["type"] == 2 and c["parent_id"] == models_cat
    )
    assert voices == ["Codex", "Cursor", "Grok", "Kimi", "OpenRouter", "z.ai"]
    assert discord.channels[models_cat]["name"] == "Models"


def test_legacy_dynamic_quotas_category_is_renamed_in_place(
    hermes, make_discord, state
):
    """A pre-Models provision: the dynamic Quotas category is PATCHed to
    Models, the five child IDs survive, and only OpenRouter is created."""
    discord = make_discord()
    discord.add_channel(id=7001, name="Quotas • 25/8 3:30pm", type=4)
    legacy = {}
    for i, name in enumerate(("Codex", "Kimi", "z.ai", "Cursor", "Grok")):
        cid = str(7100 + i)
        legacy[name] = cid
        discord.add_channel(id=cid, name=name, type=2, parent_id=7001)

    report = reconcile(http_fn=discord)

    assert report["renamed"] == ["category:Quotas->Models"]
    assert "category:Models" not in report["created"]
    # Same Discord ID, new name — one in-place PATCH, no second category.
    assert discord.channels["7001"]["name"] == "Models"
    assert discord.count("PATCH", "/channels/7001") == 1
    assert state()["categories"]["quotas"] == "7001"
    category_names = [
        c["name"]
        for c in discord.channels.values()
        if c["type"] == 4 and "Quotas" in c["name"]
    ]
    assert category_names == []
    # The five legacy children keep their IDs and stay under the category.
    for name, cid in legacy.items():
        assert state()["channels"]["quotas"][name] == cid
        assert str(discord.channels[cid]["parent_id"]) == "7001"
        assert f"channel:{name}" not in report["created"]
    # Exactly one channel is created for this module: OpenRouter.
    assert report["created"].count("channel:OpenRouter") == 1
    voices = [
        c
        for c in discord.channels.values()
        if c["type"] == 2 and str(c["parent_id"]) == "7001"
    ]
    assert len(voices) == 6

    # Idempotent: a second reconcile renames nothing and creates nothing.
    openrouter_id = state()["channels"]["quotas"]["OpenRouter"]
    mutations = len(discord.mutations)
    second = reconcile(http_fn=discord)
    assert second["created"] == []
    assert second["renamed"] == []
    assert second["adopted"] == []
    assert len(discord.mutations) == mutations
    assert state()["channels"]["quotas"]["OpenRouter"] == openrouter_id


def test_legacy_exact_quotas_category_is_renamed_in_place(hermes, make_discord, state):
    """The exact (non-dynamic) legacy spelling migrates the same way."""
    discord = make_discord()
    discord.add_channel(id=7002, name="Quotas", type=4)
    for i, name in enumerate(("Codex", "Kimi", "z.ai", "Cursor", "Grok")):
        discord.add_channel(id=7200 + i, name=name, type=2, parent_id=7002)

    report = reconcile(http_fn=discord)

    assert report["renamed"] == ["category:Quotas->Models"]
    assert discord.channels["7002"]["name"] == "Models"
    assert state()["categories"]["quotas"] == "7002"
    voices = [
        c
        for c in discord.channels.values()
        if c["type"] == 2 and str(c["parent_id"]) == "7002"
    ]
    assert len(voices) == 6


def test_legacy_quotas_is_left_alone_when_models_already_exists(
    hermes, make_discord, state
):
    """Canonical wins: with a Models category present the legacy Quotas
    category is neither renamed nor deleted."""
    discord = make_discord()
    discord.add_channel(id=7301, name="Models", type=4)
    discord.add_channel(id=7302, name="OpenRouter", type=2, parent_id=7301)
    discord.add_channel(id=7399, name="Quotas", type=4)

    report = reconcile(http_fn=discord)

    assert report["renamed"] == []
    assert state()["categories"]["quotas"] == "7301"
    assert discord.channels["7399"]["name"] == "Quotas"
