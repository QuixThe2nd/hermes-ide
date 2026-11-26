"""Runtime-metadata footer switch: display setting and per-channel override."""

from unittest.mock import patch

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.display_config import resolve_display_setting
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner(channel_overrides=None):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(
        enabled=True, channel_overrides=channel_overrides or {})})
    return runner


def _source(chat_id="chan_1", thread_id=None, parent_chat_id=None):
    return SessionSource(platform=Platform.DISCORD, chat_id=chat_id,
                         thread_id=thread_id, parent_chat_id=parent_chat_id)


def _enabled(runner, source, user_config=None):
    with patch("gateway.run._load_gateway_config", return_value=user_config or {}):
        return runner._runtime_footer_enabled(source)


class TestDisplaySettingDefault:
    def test_unconfigured_resolves_on(self):
        assert resolve_display_setting({}, "discord", "runtime_footer") is True

    def test_global_off(self):
        assert resolve_display_setting({"display": {"runtime_footer": False}},
                                       "discord", "runtime_footer") is False

    def test_platform_override_is_scoped(self):
        config = {"display": {"platforms": {"discord": {"runtime_footer": False}}}}
        assert resolve_display_setting(config, "discord", "runtime_footer") is False
        assert resolve_display_setting(config, "whatsapp", "runtime_footer") is True


class TestFooterEnabledPerTurn:
    def test_unconfigured_channel_keeps_the_footer(self):
        assert _enabled(_runner(), _source()) is True

    def test_display_off_suppresses(self):
        assert _enabled(_runner(), _source(), {"display": {"runtime_footer": False}}) is False

    def test_channel_override_off_suppresses(self):
        assert _enabled(_runner({"chan_1": ChannelOverride(runtime_footer=False)}),
                        _source()) is False

    def test_channel_override_beats_display_on(self):
        runner = _runner({"chan_1": ChannelOverride(runtime_footer=False)})
        assert _enabled(runner, _source(), {"display": {"runtime_footer": True}}) is False

    def test_channel_override_true_beats_display_off(self):
        runner = _runner({"chan_1": ChannelOverride(runtime_footer=True)})
        assert _enabled(runner, _source(), {"display": {"runtime_footer": False}}) is True

    def test_override_on_another_channel_does_not_apply(self):
        assert _enabled(_runner({"chan_2": ChannelOverride(runtime_footer=False)}),
                        _source()) is True

    def test_thread_override_applies_inside_the_thread(self):
        runner = _runner({"thread_99": ChannelOverride(runtime_footer=False)})
        assert _enabled(runner, _source(chat_id="parent", thread_id="thread_99")) is False

    def test_unparsable_config_fails_open(self):
        runner = _runner()
        with patch("gateway.run._load_gateway_config", side_effect=RuntimeError("boom")):
            assert runner._runtime_footer_enabled(_source()) is True


class TestChannelOverrideParsing:
    def test_from_dict_keeps_false(self):
        assert ChannelOverride.from_dict({"runtime_footer": False}).runtime_footer is False

    def test_from_dict_defaults_to_unset(self):
        assert ChannelOverride.from_dict({"model": "m"}).runtime_footer is None
