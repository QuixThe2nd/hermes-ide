"""Label-format contracts — the strings operators read off the channel wall."""

from __future__ import annotations

from datetime import datetime

import pytest

from plugins.speed_channels.core import (
    category_name,
    channel_names,
    fmt_latency,
    fmt_speed,
    fmt_time,
    fmt_ts,
)


def test_fmt_speed_units():
    assert fmt_speed(0) == "0 B/s"
    assert fmt_speed(512) == "512 B/s"
    assert fmt_speed(1023.9) == "1023 B/s"
    assert fmt_speed(2048) == "2 KB/s"
    assert fmt_speed(2.5 * 1024 ** 2) == "2.5 MB/s"
    assert fmt_speed(3 * 1024 ** 3) == "3.00 GB/s"


def test_fmt_speed_clamps_negatives():
    """A missing field parses as 0, a negative must never print as such."""
    assert fmt_speed(-4096) == "0 B/s"


def test_channel_names_exact_shape():
    names = channel_names(
        qbit_dl=2.5 * 1024 ** 2,
        qbit_queue=5,
        sab_dl=1.1 * 1024 ** 2,
        sab_queue=12,
        slsk_dl=340 * 1024,
        slsk_up=96 * 1024,
        slsk_queue=3,
    )
    assert names == {
        "qbittorrent": "qBittorrent: 2.5 MB/s ↓ • 5 in queue",
        "sabnzbd": "SABnzbd: 1.1 MB/s ↓ • 12 in queue",
        "slskd": "slskd: 340 KB/s ↓ • 96 KB/s ↑ • 3 in queue",
    }


def test_clock_format_is_12_hour_no_leading_zero():
    # 06:05 and 18:05 local-time epochs, built from a fixed local date so the
    # test holds in any timezone.
    morning = datetime(2026, 8, 24, 6, 5).timestamp()
    evening = datetime(2026, 8, 24, 18, 5).timestamp()
    noon = datetime(2026, 8, 24, 12, 0).timestamp()
    midnight = datetime(2026, 8, 24, 0, 0).timestamp()
    assert fmt_time(morning) == "6:05am"
    assert fmt_time(evening) == "6:05pm"
    assert fmt_time(noon) == "12:00pm"
    assert fmt_time(midnight) == "12:00am"


def test_fmt_ts_is_day_month_clock():
    epoch = datetime(2026, 8, 24, 9, 7).timestamp()
    assert fmt_ts(epoch) == "24/8 9:07am"


@pytest.mark.parametrize(
    "latency_ms, expected",
    [
        (None, "timeout"),
        (0.4, "<1ms"),
        (0.0, "<1ms"),
        (33.3, "33ms"),
        (1500, "1500ms"),
    ],
)
def test_fmt_latency(latency_ms, expected):
    assert fmt_latency(latency_ms) == expected


def test_category_label_states():
    assert (
        category_name(0, 300, 33.3, now_fn=lambda: 1000.0)
        == "Speeds • 33ms • never • Next: Due"
    )
    assert (
        category_name(0, 300, None, now_fn=lambda: 1000.0)
        == "Speeds • timeout • never • Next: Due"
    )

    last = 1000.0
    scheduled = category_name(last, 300, 33.3, now_fn=lambda: last + 100)
    assert scheduled == f"Speeds • 33ms • {fmt_ts(last)} • Next: {fmt_time(last + 300)}"

    overdue = category_name(last, 300, 0.4, now_fn=lambda: last + 300)
    assert overdue == f"Speeds • <1ms • {fmt_ts(last)} • Next: Due"
