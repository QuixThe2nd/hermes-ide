"""Setup surfaces ask only for what a platform can't start without.

The knobs in SETUP_HIDDEN_ENV_SUFFIXES are self-configuring or already
correct by default, so they don't belong on a setup form. They must still
be reachable everywhere else — this is a presentation change, not a
feature removal.
"""

import pytest

from hermes_cli.setup_hidden_env import is_setup_hidden_env


class TestIsSetupHiddenEnv:
    @pytest.mark.parametrize(
        "key",
        [
            "DISCORD_ALLOW_ALL_USERS",
            "DISCORD_REPLY_TO_MODE",
            "TELEGRAM_PROXY",
        ],
    )
    def test_self_configuring_knobs_are_hidden(self, key):
        assert is_setup_hidden_env(key)


    def test_applies_to_plugin_platforms_nobody_enumerated(self):
        """Suffix matching is the point — unenumerated plugin platforms get this for free."""
        for key in (
            "PHOTON_ALLOW_ALL_USERS",
            "BUZZ_REPLY_TO_MODE",
            "RAFT_AUTO_THREAD",
            "GOOGLE_CHAT_ALLOW_ALL_USERS",
        ):
            assert is_setup_hidden_env(key), key


class TestChannelCards:
    def test_discord_card_asks_for_token_and_allowlist_only(self):
        """The reported bug: the Discord card asked five questions for a
        one-credential platform."""
        from hermes_cli.web_server_messaging import _build_catalog_entry

        assert set(_build_catalog_entry("discord")["env_vars"]) == {
            "DISCORD_BOT_TOKEN",
            "DISCORD_ALLOWED_USERS",
        }

    def test_no_card_shows_a_hidden_knob(self):
        from hermes_cli.web_server_messaging import _messaging_platform_catalog

        for entry in _messaging_platform_catalog():
            for key in entry["env_vars"]:
                assert not is_setup_hidden_env(key), f"{entry['id']}: {key}"


    def test_hidden_knobs_move_to_the_keys_page_not_into_a_void(self):
        """Keys hides what a Channels card owns. Dropping these from the card
        must hand them back to Keys, not orphan them from every surface."""
        from hermes_cli.web_server_messaging import _channel_managed_env_keys

        managed = _channel_managed_env_keys()
        for key in (
            "DISCORD_ALLOW_ALL_USERS",
            "DISCORD_REPLY_TO_MODE",
        ):
            assert key not in managed, f"{key} is hidden from both surfaces"


class TestStillConfigurable:
    def test_gateway_still_honors_the_env_vars(self, tmp_path, monkeypatch):
        """Nothing was removed from the product — only from the setup form."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_REPLY_TO_MODE", "off")

        from gateway.config import Platform, load_gateway_config

        discord = load_gateway_config().platforms[Platform.DISCORD]
        assert discord.reply_to_mode == "off"
