"""run_tick contracts: poll gating, all-or-nothing polls, state, the
category label refresh that happens even when the poll is skipped, and the
best-effort 1.1.1.1 latency that rides on that label."""

from __future__ import annotations

import json
import subprocess

import pytest

from plugins.speed_channels.core import (
    SpeedChannelsError,
    fmt_time,
    fmt_ts,
    load_state,
    run_tick,
)


def test_first_tick_polls_and_renames_everything(hermes, config, transport, now, ping):
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    assert report["did_poll"] is True
    assert report["category"] == "renamed"
    assert report["latency_ms"] == 33.3
    assert set(report["names"]) == {"qbittorrent", "sabnzbd", "slskd"}

    patched = dict(transport.patched)
    assert patched["cq"] == "qBittorrent: 2.4 MB/s ↓ • 5 in queue"
    assert patched["cs"] == "SABnzbd: 1.1 MB/s ↓ • 12 in queue"
    assert patched["cl"] == "slskd: 340 KB/s ↓ • 96 KB/s ↑ • 2 in queue"
    assert patched["cat"] == (
        f"Speeds • 33ms • {fmt_ts(now())}"
        f" • Next: {fmt_time(int(now()) + config['poll_interval_seconds'])}"
    )

    assert load_state()["last_poll_success"] == int(now())


def test_second_tick_within_interval_patches_nothing(hermes, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    patches_after_first = transport.patch_calls

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["did_poll"] is False
    # The ping still runs on a skipped-poll tick...
    assert ping.calls == 2
    # ...but the same 33.3ms sample is inside the 5ms hysteresis band, so the
    # label is byte-identical and zero PATCHes are issued this tick.
    assert transport.patch_calls == patches_after_first
    assert report["category"] == "unchanged"


def test_tick_after_interval_polls_again(hermes, config, transport, ping):
    clock = {"t": 1_700_000_000.0}
    now = lambda: clock["t"]  # noqa: E731

    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    clock["t"] += config["poll_interval_seconds"]

    assert run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)["did_poll"] is True


def test_force_polls_even_when_not_due(hermes, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    report = run_tick(config, force=True, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is True


def test_a_failing_downloader_leaves_state_and_channels_untouched(
    hermes, config, transport, now, ping
):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    before_state = load_state()
    before_patches = transport.patch_calls
    transport.sab_status = 500

    with pytest.raises(SpeedChannelsError):
        run_tick(config, force=True, now_fn=now, http_fn=transport, ping_fn=ping)

    # No state advance and no partial rename: a frozen wall must not masquerade
    # as a fresh poll, and one dead downloader must not zero its channel.
    assert load_state() == before_state
    assert transport.patch_calls == before_patches


def test_category_label_tracks_poll_success_even_when_poll_skipped(
    hermes, config, transport, now, ping
):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    label = dict(transport.patched)["cat"]
    assert label.startswith("Speeds • 33ms • ")
    assert label.endswith(f"Next: {fmt_time(int(now()) + config['poll_interval_seconds'])}")

    # Much later: still no new poll forced, but the label flips to Due.
    late = lambda: now() + 10 * config["poll_interval_seconds"]  # noqa: E731
    report = run_tick(config, now_fn=late, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is True  # overdue ⇒ poll runs
    assert dict(transport.patched)["cat"].endswith(
        f"Next: {fmt_time(int(late()) + config['poll_interval_seconds'])}"
    )


def test_latency_hysteresis_holds_small_jitter_and_redisplays_big_moves(
    hermes, config, transport, now, ping
):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)  # displays 33ms
    patches_after_first = transport.patch_calls

    # 36ms is within 5ms of the displayed 33.3 → keep 33, no category PATCH.
    ping.value = 36.0
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is False
    assert report["latency_ms"] == 33.3
    assert "33ms" in dict(transport.patched)["cat"]
    assert transport.patch_calls == patches_after_first
    assert load_state()["last_latency_ms"] == 33.3

    # 40ms moves ≥5ms from the displayed value → redisplay as 40ms.
    ping.value = 40.0
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["latency_ms"] == 40.0
    assert "40ms" in dict(transport.patched)["cat"]
    assert transport.patch_calls == patches_after_first + 1
    assert load_state()["last_latency_ms"] == 40.0


def test_ping_timeout_renders_but_never_fails_the_tick(
    hermes, config, transport, now, ping
):
    ping.value = None

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    assert report["did_poll"] is True
    assert report["latency_ms"] is None
    # The downloaders still rename; only the latency slot degrades.
    patched = dict(transport.patched)
    assert set(patched) == {"cq", "cs", "cl", "cat"}
    assert patched["cat"].startswith("Speeds • timeout • ")
    state = load_state()
    assert state["last_poll_success"] == int(now())
    assert state["last_latency_ms"] is None


def test_state_file_is_written_under_hermes_home(hermes, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    data = json.loads((hermes / "speed_channels_state.json").read_text(encoding="utf-8"))
    assert data == {"last_poll_success": int(now()), "last_latency_ms": 33.3}


def test_skipped_poll_still_pings_and_persists_latency(hermes, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    ping.value = 60.0  # ≥5ms away → new displayed value

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["did_poll"] is False
    assert ping.calls == 2
    assert report["latency_ms"] == 60.0
    # last_poll_success must not advance on a skipped poll.
    state = load_state()
    assert state["last_poll_success"] == int(now())
    assert state["last_latency_ms"] == 60.0


# -- default_ping (fake subprocess.run; never a real binary or network) ------


def _fake_ping_run(monkeypatch, stdout="", returncode=0, exc=None):
    """Patch core's subprocess.run; return the argv/kwargs it was called with."""
    from plugins.speed_channels import core

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    return seen


IPUTILS_LINE = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=33.338 ms\n"


def test_default_ping_parses_iputils_output(monkeypatch):
    from plugins.speed_channels import core

    seen = _fake_ping_run(monkeypatch, stdout=IPUTILS_LINE)

    assert core.default_ping() == pytest.approx(33.338)
    # One packet, ~2s wait, hardcoded host; argument list, no shell.
    assert seen["argv"] == ["ping", "-c", "1", "-W", "2", core.PING_HOST]
    assert seen["kwargs"]["timeout"] == 3


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("time=33.3ms\n", 33.3),          # iputils, no space
        ("time<1ms\n", 0.5),              # ceiling → pinned inside [0, 1)
        ("time<1 ms\n", 0.5),             # Windows-style spacing
        ("Reply from 1.1.1.1: bytes=32 time=33ms TTL=57\n", 33.0),  # Windows
    ],
)
def test_default_ping_accepts_the_known_output_shapes(monkeypatch, stdout, expected):
    from plugins.speed_channels import core

    _fake_ping_run(monkeypatch, stdout=stdout)
    assert core.default_ping() == pytest.approx(expected)


@pytest.mark.parametrize(
    "returncode, exc",
    [(1, None), (2, None), (0, FileNotFoundError("ping")), (0, OSError("boom"))],
)
def test_default_ping_never_raises(monkeypatch, returncode, exc):
    from plugins.speed_channels import core

    _fake_ping_run(monkeypatch, stdout=IPUTILS_LINE, returncode=returncode, exc=exc)
    assert core.default_ping() is None


def test_default_ping_with_unparseable_output_is_none(monkeypatch):
    from plugins.speed_channels import core

    _fake_ping_run(monkeypatch, stdout="PING 1.1.1.1 56(84) bytes of data.\n")
    assert core.default_ping() is None
