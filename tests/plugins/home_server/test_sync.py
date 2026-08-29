"""Behavior contracts for the debounced sync entry point.

sync_if_due is what gateway-connect and cron call: it must be inert when the
feature is unconfigured, inert before the first explicit /sethomeserver, and
run at most once per hour after that — except when the in-code template
changed, which re-syncs immediately via the stored fingerprint.
"""

from __future__ import annotations

import json

from plugins.home_server.core import (
    SYNC_DEBOUNCE_SECONDS,
    reconcile,
    should_sync,
    sync_if_due,
    template_fingerprint,
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
    seeded = {"guild_id": "g1", "template_fingerprint": template_fingerprint()}
    assert should_sync({}) is True
    assert should_sync({"guild_id": ""}) is True
    assert should_sync({**seeded, "last_sync": "not-a-number"}) is True
    assert should_sync({**seeded, "last_sync": 100.0}, now_fn=lambda: 100.0 + 100) is False
    assert should_sync({**seeded, "last_sync": 100.0}, now_fn=lambda: 100.0 + 3601) is True


def test_should_sync_bypasses_the_debounce_on_template_change():
    """A missing or stale fingerprint means the guild predates the current
    in-code template — reconcile now, inside the debounce window or not."""
    synced = {"guild_id": "g1", "last_sync": 100.0}
    assert should_sync(synced, now_fn=lambda: 100.0 + 100) is True  # no fingerprint yet
    stale = {**synced, "template_fingerprint": "not-the-current-template"}
    assert should_sync(stale, now_fn=lambda: 100.0 + 100) is True
    current = {**synced, "template_fingerprint": template_fingerprint()}
    assert should_sync(current, now_fn=lambda: 100.0 + 100) is False


def test_sync_if_due_skips_inside_debounce_when_fingerprint_matches(
    hermes, make_discord
):
    discord = make_discord()
    reconcile(http_fn=discord, now_fn=lambda: 1000.0)

    result = sync_if_due(http_fn=discord, now_fn=lambda: 1100.0)
    assert result["synced"] is False
    assert result["reason"] == "debounced"


def _rewrite_state(hermes, mutate):
    path = hermes / "home_server" / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_sync_if_due_runs_inside_debounce_when_fingerprint_is_missing(
    hermes, make_discord
):
    """A state file written before fingerprints existed gets exactly one
    immediate re-sync, then debounces normally again."""
    discord = make_discord()
    reconcile(http_fn=discord, now_fn=lambda: 1000.0)
    _rewrite_state(hermes, lambda data: data.pop("template_fingerprint", None))

    result = sync_if_due(http_fn=discord, now_fn=lambda: 1100.0)
    assert result["synced"] is True

    # The re-sync persisted the current fingerprint.
    data = json.loads((hermes / "home_server" / "state.json").read_text("utf-8"))
    assert data["template_fingerprint"] == template_fingerprint()
    again = sync_if_due(http_fn=discord, now_fn=lambda: 1200.0)
    assert again["synced"] is False
    assert again["reason"] == "debounced"


def test_sync_if_due_runs_inside_debounce_when_fingerprint_is_stale(
    hermes, make_discord
):
    """An in-code template change (stored fingerprint no longer matches)
    re-syncs without waiting out the hour and without /sethomeserver."""
    discord = make_discord()
    reconcile(http_fn=discord, now_fn=lambda: 1000.0)
    _rewrite_state(hermes, lambda data: data.update(template_fingerprint="0" * 64))

    result = sync_if_due(http_fn=discord, now_fn=lambda: 1100.0)
    assert result["synced"] is True
