"""run_tick contracts: poll gating, all-or-nothing polls, state, and the
category label refresh that happens even when the poll is skipped."""

from __future__ import annotations

import json

import pytest

from plugins.speed_channels.core import (
    SpeedChannelsError,
    load_state,
    run_tick,
)


def test_first_tick_polls_and_renames_everything(hermes, config, transport, now):
    report = run_tick(config, now_fn=now, http_fn=transport)

    assert report["success"] is True
    assert report["did_poll"] is True
    assert report["category"] == "renamed"
    assert set(report["names"]) == {"qbittorrent", "sabnzbd", "slskd"}

    patched = dict(transport.patched)
    assert patched["cq"] == "qBittorrent: 2.4 MB/s ↓ • 5 in queue"
    assert patched["cs"] == "SABnzbd: 1.1 MB/s ↓ • 12 in queue"
    assert patched["cl"] == "slskd: 340 KB/s ↓ • 96 KB/s ↑ • 2 in queue"

    assert load_state()["last_poll_success"] == int(now())


def test_second_tick_within_interval_patches_nothing(hermes, config, transport, now):
    run_tick(config, now_fn=now, http_fn=transport)
    patches_after_first = transport.patch_calls

    report = run_tick(config, now_fn=now, http_fn=transport)

    assert report["did_poll"] is False
    # Same wall clock ⇒ same category label ⇒ zero PATCHes this tick.
    assert transport.patch_calls == patches_after_first


def test_tick_after_interval_polls_again(hermes, config, transport):
    clock = {"t": 1_700_000_000.0}
    now = lambda: clock["t"]  # noqa: E731

    run_tick(config, now_fn=now, http_fn=transport)
    clock["t"] += config["poll_interval_seconds"]

    assert run_tick(config, now_fn=now, http_fn=transport)["did_poll"] is True


def test_force_polls_even_when_not_due(hermes, config, transport, now):
    run_tick(config, now_fn=now, http_fn=transport)

    report = run_tick(config, force=True, now_fn=now, http_fn=transport)
    assert report["did_poll"] is True


def test_a_failing_downloader_leaves_state_and_channels_untouched(
    hermes, config, transport, now
):
    run_tick(config, now_fn=now, http_fn=transport)
    before_state = load_state()
    before_patches = transport.patch_calls
    transport.sab_status = 500

    with pytest.raises(SpeedChannelsError):
        run_tick(config, force=True, now_fn=now, http_fn=transport)

    # No state advance and no partial rename: a frozen wall must not masquerade
    # as a fresh poll, and one dead downloader must not zero its channel.
    assert load_state() == before_state
    assert transport.patch_calls == before_patches


def test_category_label_tracks_poll_success_even_when_poll_skipped(
    hermes, config, transport, now
):
    from plugins.speed_channels.core import fmt_time

    run_tick(config, now_fn=now, http_fn=transport)
    label = dict(transport.patched)["cat"]
    assert label.startswith("Speeds • ")
    assert label.endswith(f"Next: {fmt_time(int(now()) + config['poll_interval_seconds'])}")

    # Much later: still no new poll forced, but the label flips to Due.
    late = lambda: now() + 10 * config["poll_interval_seconds"]  # noqa: E731
    report = run_tick(config, now_fn=late, http_fn=transport)
    assert report["did_poll"] is True  # overdue ⇒ poll runs
    assert dict(transport.patched)["cat"].endswith(
        f"Next: {fmt_time(int(late()) + config['poll_interval_seconds'])}"
    )


def test_state_file_is_written_under_hermes_home(hermes, config, transport, now):
    run_tick(config, now_fn=now, http_fn=transport)
    data = json.loads((hermes / "speed_channels_state.json").read_text(encoding="utf-8"))
    assert data == {"last_poll_success": int(now())}
