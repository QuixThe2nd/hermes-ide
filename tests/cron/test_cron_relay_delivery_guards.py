"""Fire-time guards: origin thread routing + relay-fronted preflight.

Two related behaviors on relay-fronted Slack deployments:

1. ``deliver=origin`` replays the captured origin thread verbatim (Slack
   thread-per-message keys, Discord forum topics alike), and an explicit
   ``slack:<chat_id>`` target addressed to the origin chat re-attaches the
   origin's thread so the reply lands in the same thread.

2. ``_preflight_check_delivery`` must validate the ``slack:`` prefix against
   natively-configured platforms AND the relay's fronted set; in relay-only
   topology the native set is ``{relay}`` and the job was refused with "no
   gateway credentials configured" although fire-time routing
   (resolve_delivery_transport + fronts_platform) would have delivered it.
"""

from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler_preflight import _preflight_check_delivery
from cron.scheduler_delivery import _resolve_single_delivery_target


SYNTH = "1755043010.123456"


@pytest.fixture()
def passthrough_send_targets(monkeypatch):
    """Resolve every explicit target as written (no directory rewriting)."""
    monkeypatch.setattr(
        "tools.send_message_tool.prepare_send_message_platforms", lambda: None)
    monkeypatch.setattr(
        "tools.send_message_tool.resolve_send_target",
        lambda platform, rest, **kw: (rest, None, None))


class TestOriginThreadRouting:
    def test_origin_thread_replayed_verbatim(self):
        """deliver=origin replays the captured thread id as-is."""
        job = {"origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C",
                          "thread_id": SYNTH}}
        target = _resolve_single_delivery_target(job, "origin")
        assert target == {"platform": "slack", "chat_id": "D0BJTDCSR7C",
                          "thread_id": SYNTH, "_resolved_from": "origin"}

    def test_origin_without_thread_stays_top_level(self):
        job = {"origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C"}}
        target = _resolve_single_delivery_target(job, "origin")
        assert target["thread_id"] is None

    def test_non_slack_origin_thread_untouched(self):
        """Discord forum-topic origins replay their thread verbatim."""
        job = {"origin": {"platform": "discord", "chat_id": "-1003941067111",
                          "thread_id": "2203"}}
        target = _resolve_single_delivery_target(job, "origin")
        assert target["thread_id"] == "2203"

    def test_explicit_target_reattaches_origin_thread_on_same_chat(
        self, passthrough_send_targets
    ):
        """An explicit slack:<origin chat> inherits the origin's thread so the
        delivery lands in the same thread a reply would."""
        job = {"origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C",
                          "thread_id": SYNTH}}
        target = _resolve_single_delivery_target(job, "slack:D0BJTDCSR7C")
        assert target["thread_id"] == SYNTH

    def test_explicit_other_chat_gets_no_reattach(self, passthrough_send_targets):
        """Origin-affinity re-attach only applies to the origin's own chat."""
        job = {"origin": {"platform": "slack", "chat_id": "C0AGENERAL",
                          "thread_id": "1755040000.000100"}}
        target = _resolve_single_delivery_target(job, "slack:D0OTHER")
        assert target["thread_id"] is None

    def test_explicit_thread_beats_origin_affinity(self, monkeypatch):
        """An explicit slack:<chat>:<thread> target keeps its own thread — the origin's
        thread never overrides an addressing the job author wrote."""
        monkeypatch.setattr(
            "tools.send_message_tool.prepare_send_message_platforms", lambda: None)
        monkeypatch.setattr(
            "tools.send_message_tool.resolve_send_target",
            lambda platform, rest, **kw: ("D0BJTDCSR7C", "1755049999.000777", None))
        job = {"origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C",
                          "thread_id": SYNTH}}
        target = _resolve_single_delivery_target(
            job, "slack:D0BJTDCSR7C:1755049999.000777")
        assert target["thread_id"] == "1755049999.000777"

    def test_affinity_never_crosses_platform(self, passthrough_send_targets):
        """A slack origin never re-attaches its thread onto another platform's target,
        even at the same chat id string."""
        job = {"origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C",
                          "thread_id": SYNTH}}
        target = _resolve_single_delivery_target(job, "discord:D0BJTDCSR7C")
        assert target["thread_id"] is None

    def test_discord_same_chat_target_gets_no_reattach(self, passthrough_send_targets):
        """Affinity is Slack-only: an ordinary explicit Discord target addressed to the
        origin chat stays as resolved — Discord forum/topic ids are addressing, not
        session keying."""
        job = {"origin": {"platform": "discord", "chat_id": "-1003941067111",
                          "thread_id": "2203"}}
        target = _resolve_single_delivery_target(job, "discord:-1003941067111")
        assert target["thread_id"] is None

    def test_slack_affinity_needs_origin_thread(self, passthrough_send_targets):
        """Origin captured without a thread (top-level creation): an explicit same-chat
        target stays top-level — there is nothing to re-attach."""
        job = {"origin": {"platform": "slack", "chat_id": "D0BJTDCSR7C"}}
        target = _resolve_single_delivery_target(job, "slack:D0BJTDCSR7C")
        assert target["thread_id"] is None


def _gateway_config(connected_values):
    config = MagicMock()
    config.get_connected_platforms.return_value = [
        MagicMock(value=v) for v in connected_values
    ]
    return config


class TestPreflightRelayFronted:
    def test_relay_fronted_slack_accepted(self, monkeypatch):
        """Relay-only topology fronting slack: slack:CHAT passes preflight."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            assert _preflight_check_delivery(
                {"deliver": "slack:D0BJTDCSR7C"}) is None

    def test_relay_fronted_slack_explicit_thread_accepted(self, monkeypatch):
        """An explicit slack:<chat>:<thread> target validates on its platform part —
        the thread segment does not re-plate the name."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            assert _preflight_check_delivery(
                {"deliver": "slack:D0BJTDCSR7C:1755043010.123456"}) is None

    def test_disabled_relay_slack_rejected(self, monkeypatch):
        """A relay explicitly disabled fronts nothing: fronted slack stays unconnected."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        monkeypatch.setattr("gateway.relay.relay_explicitly_disabled", lambda: True)
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            reason = _preflight_check_delivery({"deliver": "slack:D0BJTDCSR7C"})
            assert reason is not None
            assert "slack" in reason

    def test_unfronted_slack_rejected(self, monkeypatch):
        """A connected relay fronting another platform does not whitelist slack."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "discord")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            reason = _preflight_check_delivery({"deliver": "slack:D0BJTDCSR7C"})
            assert reason is not None
            assert "slack" in reason

    def test_unknown_platform_rejected_even_when_relay_fronts(self, monkeypatch):
        """Fronting slack never whitelists an unregistered name — not even when
        the relay stamp itself includes it."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack,banana")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            reason = _preflight_check_delivery({"deliver": "banana:12345"})
            assert reason is not None
            assert "banana" in reason

    @pytest.mark.parametrize("deliver", [
        "slack:D0BJTDCSR7C",
        "discord:123,slack:D0BJTDCSR7C",
    ])
    def test_fronted_slack_fails_closed_when_config_unreadable(self, monkeypatch, deliver):
        """An unregistered (fronted-slack) name must not ride the known-platform
        fail-open — even beside a registered platform: with the gateway config
        unreadable there is no proof of fronting."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        with patch("gateway.config.load_gateway_config",
                   side_effect=OSError("unreadable")):
            reason = _preflight_check_delivery({"deliver": deliver})
            assert reason is not None
            assert "slack" in reason

    def test_unfronted_platform_still_rejected(self, monkeypatch):
        """The relay fronting slack does not whitelist other platforms."""
        monkeypatch.setenv("GATEWAY_RELAY_PLATFORMS", "slack")
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"relay"})):
            reason = _preflight_check_delivery({"deliver": "discord:12345"})
            assert reason is not None
            assert "discord" in reason

    def test_native_strictness_without_relay(self, monkeypatch):
        """No relay configured: the native credential check is unchanged."""
        monkeypatch.delenv("GATEWAY_RELAY_PLATFORMS", raising=False)
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"discord"})):
            reason = _preflight_check_delivery(
                {"deliver": "slack:D0BJTDCSR7C"})
            assert reason is not None
            assert "slack" in reason

    def test_native_connected_slack_without_relay_rejected(self, monkeypatch):
        """A stale native-slack connection stamp is not relay fronting: slack
        stays rejected when no relay fronts it."""
        monkeypatch.delenv("GATEWAY_RELAY_PLATFORMS", raising=False)
        with patch("gateway.config.load_gateway_config",
                   return_value=_gateway_config({"slack"})):
            reason = _preflight_check_delivery(
                {"deliver": "slack:D0BJTDCSR7C"})
            assert reason is not None
            assert "slack" in reason
