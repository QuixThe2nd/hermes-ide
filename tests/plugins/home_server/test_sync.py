"""Behavior contracts for the debounced sync entry point.

sync_if_due is what gateway-connect and cron call: it must be inert when the
feature is unconfigured, inert before the first explicit /sethomeserver, and
run at most once per hour after that.
"""

from __future__ import annotations

from plugins.home_server.core import (
    SYNC_DEBOUNCE_SECONDS,
    reconcile,
    should_sync,
    sync_if_due,
)


def test_unconfigured_is_inert(hermes, make_discord, write_config):
    write_config({"guild_id": ""})
    result = sync_if_due(http_fn=make_discord())
    assert result["enabled"] is False
    assert result.get("synced") is False


def test_never_provisioned_does_not_sync(hermes, make_discord):
    """The first provision must be an explicit /sethomeserver, never a side
    effect of the gateway connecting."""
    result = sync_if_due(http_fn=make_discord())
    assert result["synced"] is False
    assert result["reason"] == "never provisioned"
    assert not (hermes / "home_server" / "state.json").exists()


def test_syncs_once_then_debounces(hermes, make_discord):
    discord = make_discord()
    # Provision once (what /sethomeserver does) so state.json exists.
    reconcile(http_fn=discord, now_fn=lambda: 1000.0)

    result = sync_if_due(http_fn=discord, now_fn=lambda: 1100.0)
    assert result["synced"] is False
    assert result["reason"] == "debounced"

    result = sync_if_due(
        http_fn=discord, now_fn=lambda: 1000.0 + SYNC_DEBOUNCE_SECONDS + 1
    )
    assert result["synced"] is True


def test_debounce_window_is_one_hour():
    """The contract is 'at most once per hour', not an arbitrary constant."""
    assert SYNC_DEBOUNCE_SECONDS == 3600


def test_should_sync_handles_missing_and_corrupt_state():
    assert should_sync({}) is True
    assert should_sync({"guild_id": ""}) is True
    seeded = {"guild_id": "g1"}
    assert should_sync({**seeded, "last_sync": "not-a-number"}) is True
    assert should_sync({**seeded, "last_sync": 100.0}, now_fn=lambda: 100.0 + 100) is False
    assert should_sync({**seeded, "last_sync": 100.0}, now_fn=lambda: 100.0 + 3601) is True
