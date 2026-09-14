"""category_name absolute timestamp and next-due tests."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from plugins.quota_channels.core import category_name

CATEGORY_SHAPE = re.compile(
    r"^Models \u2022 (?:never|\d+/\d+ \d+:\d{2}(?:am|pm)) \u2022 Next: (?:Due|\d+:\d{2}(?:am|pm))$"
)


def _clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def _fmt_ts(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch)
    return f"{dt.day}/{dt.month} {_clock(dt)}"


def _fmt_time(epoch: float) -> str:
    return _clock(datetime.fromtimestamp(epoch))


def _expected_name(last: float, interval: int, now: float) -> str:
    if last <= 0:
        return "Models \u2022 never \u2022 Next: Due"
    next_due = last + interval
    ts_part = _fmt_ts(last)
    if now >= next_due:
        return f"Models \u2022 {ts_part} \u2022 Next: Due"
    return f"Models \u2022 {ts_part} \u2022 Next: {_fmt_time(next_due)}"


def test_never_updated():
    assert (
        category_name(0, 1800, now_fn=lambda: 1_000_000.0)
        == "Models \u2022 never \u2022 Next: Due"
    )
    assert (
        category_name(-5, 1800, now_fn=lambda: 1_000_000.0)
        == "Models \u2022 never \u2022 Next: Due"
    )


def test_normal_case_before_due():
    last = datetime(2026, 8, 21, 18, 53, 0).timestamp()
    interval = 1800
    now = last + 900
    expected = _expected_name(last, interval, now)
    assert category_name(last, interval, now_fn=lambda: now) == expected
    assert CATEGORY_SHAPE.match(expected)


def test_exact_due_boundary():
    last = datetime(2026, 8, 21, 18, 53, 0).timestamp()
    interval = 1800
    now = last + interval
    expected = _expected_name(last, interval, now)
    assert expected.endswith("Next: Due")
    assert category_name(last, interval, now_fn=lambda: now) == expected


def test_one_second_before_due():
    last = datetime(2026, 8, 21, 18, 53, 0).timestamp()
    interval = 1800
    now = last + interval - 1
    expected = _expected_name(last, interval, now)
    assert expected.endswith(f"Next: {_fmt_time(last + interval)}")
    assert category_name(last, interval, now_fn=lambda: now) == expected


def test_midnight_wrap_next_time():
    last = datetime(2026, 8, 21, 23, 45, 0).timestamp()
    interval = 1800
    now = last + 600
    next_dt = datetime.fromtimestamp(last + interval)
    assert _clock(next_dt) == "12:15am"
    expected = _expected_name(last, interval, now)
    assert expected.endswith("Next: 12:15am")
    assert category_name(last, interval, now_fn=lambda: now) == expected


@pytest.mark.parametrize(
    ("last", "interval", "now"),
    [
        (0, 1800, 1_000_000.0),
        (datetime(2026, 8, 21, 6, 53).timestamp(), 1800, datetime(2026, 8, 21, 7, 0).timestamp()),
        (datetime(2026, 8, 21, 18, 53).timestamp(), 1800, datetime(2026, 8, 21, 19, 23).timestamp()),
    ],
)
def test_format_contract_shape(last, interval, now):
    name = category_name(last, interval, now_fn=lambda: now)
    assert CATEGORY_SHAPE.match(name), name
