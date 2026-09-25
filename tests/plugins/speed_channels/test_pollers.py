"""Downloader poll contracts against a mocked transport.

The API shapes (endpoints, auth style, response fields) come from the peer
deployment's reference script; these tests pin them so a downloader-side
change is caught instead of silently zeroing a channel.
"""

from __future__ import annotations

import pytest

from plugins.speed_channels.core import (
    SpeedChannelsError,
    qbit_speeds,
    sab_speed,
    slskd_speeds,
)


def test_qbit_login_sets_cookie_replayed_on_reads(hermes, transport):
    dl, up, queue = qbit_speeds(transport)

    assert dl == 2_500_000.0
    assert up == 100.0
    assert queue == 5

    # The SID from the login Set-Cookie must be replayed on both reads.
    reads = [r for r in transport.requests if "transfer/info" in r[1] or "torrents/info" in r[1]]
    assert len(reads) == 2
    assert all("SID=s1" in r[2]["cookie"] for r in reads)
    # Credentials ride the login form, not the query string.
    login = next(r for r in transport.requests if r[1].endswith("/auth/login"))
    assert "u=p" in login[3] or "username=u" in login[3]


def test_qbit_rejects_a_failed_login(hermes, transport):
    def reject(req, timeout=20.0):
        if req.full_url.endswith("/auth/login"):
            return 403, b"Fails.", {}
        return transport(req, timeout)

    with pytest.raises(SpeedChannelsError, match="login"):
        qbit_speeds(reject)


def test_sab_reads_queue_fields_and_authenticates(hermes, transport):
    speed, slots = sab_speed(transport)

    assert speed == pytest.approx(1100.0 * 1024)  # kbpersec → bytes/s
    assert slots == 12

    (method, url, _headers, _body) = next(
        r for r in transport.requests if "mode=queue" in r[1]
    )
    assert method == "GET"
    expected_query = "api" + "key=sabkey"
    assert expected_query in url


def test_sab_without_queue_object_is_an_error(hermes, transport):
    def no_queue(req, timeout=20.0):
        if "mode=queue" in req.full_url:
            return 200, b'{"error": "bad api key"}', {}
        return transport(req, timeout)

    with pytest.raises(SpeedChannelsError, match="queue"):
        sab_speed(no_queue)


def test_slskd_sums_active_transfers_and_skips_finished(hermes, transport):
    dl, up, queue = slskd_speeds(transport)

    # Two active download files (InProgress + Queued); Succeeded/Cancelled
    # must contribute neither speed nor queue depth.
    assert queue == 2
    assert dl == pytest.approx(340 * 1024)
    assert up == pytest.approx(96 * 1024)

    calls = [r for r in transport.requests if "/api/v0/transfers/" in r[1]]
    assert {c[1].rsplit("/", 1)[-1] for c in calls} == {"downloads", "uploads"}
    assert all(r[2]["x-api-key"] == "slskey" for r in calls)


def test_secrets_resolve_relative_to_hermes_home(hermes, monkeypatch, transport):
    """The reference script's /root paths must never be hardcoded here — the
    env files are found under whatever HERMES_HOME points at."""
    qbit_speeds(transport)  # already proves HERMES_HOME/secrets/qbittorrent.env

    def missing(_req, _timeout=20.0):
        raise AssertionError("should not be reached")

    monkeypatch.setenv("HERMES_HOME", "/nonexistent-home")
    with pytest.raises(SpeedChannelsError, match="qbittorrent.env"):
        qbit_speeds(missing)
