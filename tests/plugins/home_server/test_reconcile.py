"""Behavior contracts for the home_server reconcile engine.

Idempotency is asserted by *mutating-call counts*, not snapshots: a second
reconcile must issue zero creates and zero message posts.
"""

from __future__ import annotations

from plugins.home_server.core import (
    CHANNEL_TYPE_CATEGORY,
    MODULE_KEYS,
    TEMPLATE,
    reconcile,
    template_fingerprint,
)

# 5 categories + 2 chat + 3 notifications + 4 memory + 5 model voices + 3 speeds.
FIRST_RUN_CREATES = 5 + 2 + 3 + 4 + 5 + 3


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
    # Lounges is inbox/outbox only — the old Chat/#home channel is gone from
    # the template and must not be minted anymore.
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


def test_template_order_puts_notifications_first():
    """The canonical order is a product decision, not an accident: Notifications
    is the first managed category, so it sits at the top of the guild."""
    assert MODULE_KEYS[0] == "notifications"
    assert list(TEMPLATE)[0] == "notifications"
    assert list(TEMPLATE) == list(MODULE_KEYS)
    assert TEMPLATE["notifications"].category == "Notifications"
    assert [c.name for c in TEMPLATE["notifications"].channels] == [
        "model-fallback",
        "gateway-restarts",
        "other",
    ]
    # Lounges is inbox/outbox only — no home channel in the template. The
    # module key stays `chat` (internal slug); "Chat" is its legacy category.
    assert TEMPLATE["chat"].category == "Lounges"
    assert TEMPLATE["chat"].legacy_categories == ("Chat",)
    assert [c.name for c in TEMPLATE["chat"].channels] == ["inbox", "outbox"]


def test_fresh_provision_seats_notifications_at_the_top(hermes, make_discord, state):
    discord = make_discord()
    reconcile(http_fn=discord)

    positions = {
        key: discord.channels[cid]["position"]
        for key, cid in state()["categories"].items()
    }
    assert positions["notifications"] == 0
    assert positions["chat"] == 1
    assert positions["memory"] == 2
    assert positions["quotas"] == 3
    assert positions["speeds"] == 4

    # Channels inside a category follow ChannelSpec order.
    notifications = state()["channels"]["notifications"]
    for name, wanted in (
        ("model-fallback", 0),
        ("gateway-restarts", 1),
        ("other", 2),
    ):
        assert discord.channels[notifications[name]]["position"] == wanted, name


def test_existing_guild_is_reordered_notifications_first(
    hermes, make_discord, state
):
    """A guild provisioned before Notifications existed carries it at the
    bottom: reconcile PATCHes it to position 0 and seats the other managed
    categories in template order, without touching unmanaged ones."""
    from plugins.home_server.core import CHANNEL_TYPE_TEXT, CHANNEL_TYPE_VOICE

    discord = make_discord(
        existing=[
            # The four old categories, deliberately scrambled.
            {"id": "10", "name": "Chat", "type": CHANNEL_TYPE_CATEGORY, "position": 3},
            {"id": "11", "name": "inbox", "type": CHANNEL_TYPE_TEXT, "parent_id": "10", "position": 0},
            {"id": "12", "name": "outbox", "type": CHANNEL_TYPE_TEXT, "parent_id": "10", "position": 1},
            {"id": "20", "name": "Honcho Memory", "type": CHANNEL_TYPE_CATEGORY, "position": 0},
            {"id": "21", "name": "explicit-facts", "type": CHANNEL_TYPE_TEXT, "parent_id": "20", "position": 0},
            {"id": "22", "name": "deductions", "type": CHANNEL_TYPE_TEXT, "parent_id": "20", "position": 1},
            {"id": "23", "name": "patterns", "type": CHANNEL_TYPE_TEXT, "parent_id": "20", "position": 2},
            {"id": "24", "name": "contradictions", "type": CHANNEL_TYPE_TEXT, "parent_id": "20", "position": 3},
            {"id": "30", "name": "Models", "type": CHANNEL_TYPE_CATEGORY, "position": 2},
            {"id": "31", "name": "Codex", "type": CHANNEL_TYPE_VOICE, "parent_id": "30", "position": 0},
            {"id": "40", "name": "Speeds", "type": CHANNEL_TYPE_CATEGORY, "position": 1},
            {"id": "41", "name": "qBittorrent", "type": CHANNEL_TYPE_VOICE, "parent_id": "40", "position": 0},
            # Notifications landed at the bottom when it was added.
            {"id": "90", "name": "Notifications", "type": CHANNEL_TYPE_CATEGORY, "position": 9},
            {"id": "91", "name": "other", "type": CHANNEL_TYPE_TEXT, "parent_id": "90", "position": 2},
            {"id": "92", "name": "gateway-restarts", "type": CHANNEL_TYPE_TEXT, "parent_id": "90", "position": 0},
            {"id": "93", "name": "model-fallback", "type": CHANNEL_TYPE_TEXT, "parent_id": "90", "position": 1},
            # Unmanaged: must survive untouched, never PATCHed by id.
            {"id": "99", "name": "Gaming", "type": CHANNEL_TYPE_CATEGORY, "position": 8},
        ]
    )

    report = reconcile(http_fn=discord)

    # The Notifications category itself was PATCHed to the top.
    assert discord.channels["90"]["position"] == 0
    assert discord.count("PATCH", "/channels/90") >= 1
    assert "category:Notifications" in report["positioned"]

    # Managed categories now follow template order, scrambled start or not.
    for key, cid in state()["categories"].items():
        wanted = list(MODULE_KEYS).index(key)
        assert discord.channels[cid]["position"] == wanted, key

    # Channel order inside Notifications becomes the template order.
    notifications = state()["channels"]["notifications"]
    for name, wanted in (
        ("model-fallback", 0),
        ("gateway-restarts", 1),
        ("other", 2),
    ):
        assert discord.channels[notifications[name]]["position"] == wanted, name

    # The unmanaged category was neither deleted nor written to.
    assert discord.channels["99"]["name"] == "Gaming"
    assert discord.channels["99"]["position"] == 8
    assert discord.count("PATCH", "/channels/99") == 0

    # Already in order: a second reconcile issues no mutating call at all.
    mutations = len(discord.mutations)
    second = reconcile(http_fn=discord)
    assert second["positioned"] == []
    assert len(discord.mutations) == mutations


def test_state_persists_the_template_fingerprint(hermes, make_discord, state):
    reconcile(http_fn=make_discord())
    stored = state()["template_fingerprint"]
    assert stored == template_fingerprint()
    assert len(stored) == 64  # sha256 hex digest


def test_existing_channels_are_discovered_not_recreated(
    hermes, guild, make_discord, state
):
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    discord = make_discord(
        existing=[
            {"id": "11", "name": "Lounges", "type": CHANNEL_TYPE_CATEGORY},
            {"id": "12", "name": "inbox", "type": CHANNEL_TYPE_TEXT, "parent_id": "11"},
        ]
    )
    report = reconcile(http_fn=discord)

    assert "category:Lounges" not in report["created"]
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
    assert "Lounges" in names and "Models" in names

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


def test_restart_channel_rename_is_set_when_none_exists(
    hermes, make_discord, read_config, state
):
    report = reconcile(http_fn=make_discord())

    assert report["restart_channel_rename"] == "set"
    rcr = read_config()["gateway"]["restart_channel_rename"]
    channel_id = state()["channels"]["notifications"]["gateway-restarts"]
    assert rcr["channel_id"] == channel_id
    assert rcr["platform"] == "discord"
    assert rcr["base_name"] == "gateway-restarts"
    assert rcr["renamed_template"] == "restarting-{agents}-agents"
    assert rcr["idle_template"] == "agents-{agents}"


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


def test_restart_channel_rename_is_set_even_when_notification_channel_already_exists(
    hermes, guild, make_discord, write_config, read_config, state
):
    """Existing /setnotify must not leave drain-progress renaming dark."""
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
    assert report["restart_channel_rename"] == "set"
    rcr = read_config()["gateway"]["restart_channel_rename"]
    assert rcr["channel_id"] == state()["channels"]["notifications"]["gateway-restarts"]


def test_existing_restart_channel_rename_is_never_clobbered(
    hermes, guild, make_discord, write_config, read_config
):
    import yaml

    write_config({"guild_id": guild})
    path = hermes / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["gateway"] = {
        "restart_channel_rename": {
            "platform": "discord",
            "channel_id": "333",
            "base_name": "custom-restarts",
        }
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    report = reconcile(http_fn=make_discord())

    assert report["restart_channel_rename"] == "kept"
    rcr = read_config()["gateway"]["restart_channel_rename"]
    assert rcr["channel_id"] == "333"
    assert rcr["base_name"] == "custom-restarts"


def test_malformed_restart_channel_rename_is_replaced(
    hermes, guild, make_discord, write_config, read_config, state
):
    import yaml

    write_config({"guild_id": guild})
    path = hermes / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["gateway"] = {"restart_channel_rename": {"channel_id": "abc"}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    report = reconcile(http_fn=make_discord())

    assert report["restart_channel_rename"] == "set"
    assert (
        read_config()["gateway"]["restart_channel_rename"]["channel_id"]
        == state()["channels"]["notifications"]["gateway-restarts"]
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
    assert report["restart_channel_rename"] == "skipped"
    discord_section = read_config().get("platforms", {}).get("discord", {})
    assert "home_channel" not in discord_section
    assert "notification_channel" not in discord_section
    assert "restart_channel_rename" not in (read_config().get("gateway") or {})


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
    for i, key in enumerate(("Codex", "Kimi", "z.ai", "Cursor", "Grok")):
        discord.add_channel(id=7100 + i, name=key, type=2, parent_id=7001)
    # Leftover from before OpenRouter left the template: never deleted.
    discord.add_channel(id=7199, name="OpenRouter", type=2, parent_id=7001)

    report = reconcile(http_fn=discord)

    assert "category:Models" not in report["created"]
    assert report["renamed"] == []
    assert report["adopted"] == ["category:Models"]
    assert state()["categories"]["quotas"] == "7001"
    # No duplicate voice channels were minted: the five template rows plus the
    # OpenRouter leftover, which reconcile leaves where it is.
    voices = [
        c
        for c in discord.channels.values()
        if c["type"] == 2 and c["parent_id"] == 7001
    ]
    assert len(voices) == 6


def test_orphan_channels_are_adopted_into_the_module_category(
    hermes, guild, make_discord, state
):
    """An existing #inbox at guild level moves under Lounges instead of a dupe."""
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


def test_agents_count_name_is_adopted_as_gateway_restarts(
    hermes, make_discord, state
):
    """A live agents-N rename is the same channel, not a new gateway-restarts."""
    discord = make_discord()
    discord.add_channel(id=9101, name="model-fallback", type=0)
    discord.add_channel(id=9102, name="agents-5", type=0)
    discord.add_channel(id=9103, name="other", type=0)

    report = reconcile(http_fn=discord)

    assert "channel:gateway-restarts" not in report["created"]
    assert state()["channels"]["notifications"]["gateway-restarts"] == "9102"
    names = {c["name"] for c in discord.channels.values()}
    assert "gateway-restarts" not in names
    assert "agents-5" in names


def test_stored_gateway_restarts_id_survives_idle_rename(
    hermes, make_discord, state
):
    """Second reconcile must not mint a new channel after agents-N rename."""
    discord = make_discord()
    reconcile(http_fn=discord)
    cid = state()["channels"]["notifications"]["gateway-restarts"]
    discord.channels[cid]["name"] = "agents-2"
    mutations = len(discord.mutations)

    report = reconcile(http_fn=discord)

    assert "channel:gateway-restarts" not in report["created"]
    assert state()["channels"]["notifications"]["gateway-restarts"] == cid
    assert discord.channels[cid]["name"] == "agents-2"
    assert len(discord.mutations) == mutations


def test_leftover_chat_home_channel_is_never_deleted(hermes, make_discord):
    """A #home left by a previous Chat provision stays exactly where it was —
    even as its parent category migrates in place to Lounges: the template no
    longer references the channel, and reconcile never deletes."""
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
    # The parent category itself migrated in place — same ID, new name.
    assert discord.channels["21"]["name"] == "Lounges"


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


def test_models_template_has_five_voice_channels(hermes, make_discord, state):
    """The canonical fresh provision creates a Models category with five rows
    (OpenRouter left the template — a fresh provision must not mint it)."""
    discord = make_discord()
    reconcile(http_fn=discord)

    models_cat = state()["categories"]["quotas"]
    voices = sorted(
        c["name"]
        for c in discord.channels.values()
        if c["type"] == 2 and c["parent_id"] == models_cat
    )
    assert voices == ["Codex", "Cursor", "Grok", "Kimi", "z.ai"]
    assert discord.channels[models_cat]["name"] == "Models"


def test_fresh_provision_creates_the_lounges_category(hermes, make_discord, state):
    """The conversation category is Lounges now: a fresh provision mints it
    with inbox/outbox unchanged, and no Chat category is created."""
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    discord = make_discord()
    reconcile(http_fn=discord)

    lounges_cat = state()["categories"]["chat"]
    assert discord.channels[lounges_cat]["name"] == "Lounges"
    children = sorted(
        c["name"]
        for c in discord.channels.values()
        if c["type"] == CHANNEL_TYPE_TEXT and str(c["parent_id"]) == lounges_cat
    )
    assert children == ["inbox", "outbox"]
    names = {c["name"] for c in discord.channels.values()}
    assert "Chat" not in names


def test_legacy_dynamic_quotas_category_is_renamed_in_place(
    hermes, make_discord, state
):
    """A pre-Models provision: the dynamic Quotas category is PATCHed to
    Models, the five child IDs survive, and no extra voice is created."""
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
    # OpenRouter left the template: nothing new is created for this module.
    assert report["created"].count("channel:OpenRouter") == 0
    voices = [
        c
        for c in discord.channels.values()
        if c["type"] == 2 and str(c["parent_id"]) == "7001"
    ]
    assert len(voices) == 5

    # Idempotent: a second reconcile renames nothing and creates nothing.
    codex_id = state()["channels"]["quotas"]["Codex"]
    mutations = len(discord.mutations)
    second = reconcile(http_fn=discord)
    assert second["created"] == []
    assert second["renamed"] == []
    assert second["adopted"] == []
    assert len(discord.mutations) == mutations
    assert state()["channels"]["quotas"]["Codex"] == codex_id


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
    assert len(voices) == 5


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


def test_legacy_chat_category_is_renamed_in_place(hermes, make_discord, state):
    """A pre-Lounges provision: the Chat category is PATCHed to Lounges, the
    inbox/outbox IDs survive, and no duplicate category is created."""
    discord = make_discord()
    discord.add_channel(id=6001, name="Chat", type=4)
    legacy = {}
    for i, name in enumerate(("inbox", "outbox")):
        cid = str(6100 + i)
        legacy[name] = cid
        discord.add_channel(id=cid, name=name, type=0, parent_id=6001)

    report = reconcile(http_fn=discord)

    assert report["renamed"] == ["category:Chat->Lounges"]
    assert "category:Lounges" not in report["created"]
    # Same Discord ID, new name — one in-place PATCH, no second category.
    assert discord.channels["6001"]["name"] == "Lounges"
    assert discord.count("PATCH", "/channels/6001") == 1
    assert state()["categories"]["chat"] == "6001"
    chat_categories = [
        c["name"]
        for c in discord.channels.values()
        if c["type"] == 4 and c["name"] in ("Chat", "Lounges")
    ]
    assert chat_categories == ["Lounges"]
    # The legacy children keep their IDs and stay under the category.
    for name, cid in legacy.items():
        assert state()["channels"]["chat"][name] == cid
        assert str(discord.channels[cid]["parent_id"]) == "6001"
        assert f"channel:{name}" not in report["created"]

    # Idempotent: a second reconcile renames nothing and creates nothing.
    inbox_id = state()["channels"]["chat"]["inbox"]
    mutations = len(discord.mutations)
    second = reconcile(http_fn=discord)
    assert second["created"] == []
    assert second["renamed"] == []
    assert second["adopted"] == []
    assert len(discord.mutations) == mutations
    assert state()["categories"]["chat"] == "6001"
    assert state()["channels"]["chat"]["inbox"] == inbox_id


def test_unrelated_chat_prefixed_category_is_never_claimed(
    hermes, make_discord, state
):
    """A user-made "Chat Archive" is not the legacy Chat category.

    Chat never carried a dynamic poller suffix, so its migration matches the
    legacy name exactly — a prefix rule here would claim (and rename, and
    repopulate) any category that merely starts with "Chat". The archive keeps
    its name, its children, its position, and is never written to."""
    from plugins.home_server.core import CHANNEL_TYPE_TEXT

    discord = make_discord(
        existing=[
            {
                "id": "6401",
                "name": "Chat Archive",
                "type": CHANNEL_TYPE_CATEGORY,
                "position": 7,
            },
            {
                "id": "6402",
                "name": "2024",
                "type": CHANNEL_TYPE_TEXT,
                "parent_id": "6401",
                "position": 0,
            },
        ]
    )

    report = reconcile(http_fn=discord)

    assert report["renamed"] == []
    assert discord.channels["6401"]["name"] == "Chat Archive"
    assert discord.channels["6401"]["position"] == 7
    assert str(discord.channels["6402"]["parent_id"]) == "6401"
    assert discord.channels["6402"]["name"] == "2024"
    assert discord.count("PATCH", "/channels/6401") == 0
    assert discord.count("PATCH", "/channels/6402") == 0

    # A separate Lounges category was minted instead of the archive claimed.
    lounges_cat = state()["categories"]["chat"]
    assert lounges_cat != "6401"
    assert discord.channels[lounges_cat]["name"] == "Lounges"
    children = sorted(
        c["name"]
        for c in discord.channels.values()
        if c["type"] == CHANNEL_TYPE_TEXT and str(c["parent_id"]) == lounges_cat
    )
    assert children == ["inbox", "outbox"]
    assert {c["name"] for c in discord.channels.values()} >= {
        "Chat Archive",
        "Lounges",
    }


def test_legacy_chat_is_left_alone_when_lounges_already_exists(
    hermes, make_discord, state
):
    """Canonical wins: with a Lounges category present the legacy Chat
    category is neither renamed nor deleted."""
    discord = make_discord()
    discord.add_channel(id=6301, name="Lounges", type=4)
    discord.add_channel(id=6302, name="inbox", type=0, parent_id=6301)
    discord.add_channel(id=6399, name="Chat", type=4)

    report = reconcile(http_fn=discord)

    assert report["renamed"] == []
    assert state()["categories"]["chat"] == "6301"
    assert discord.channels["6399"]["name"] == "Chat"
