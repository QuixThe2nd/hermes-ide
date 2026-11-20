"""Tests for runtime-footer timing breakdown fields."""

from __future__ import annotations

import pytest

from gateway.runtime_footer import (
    _format_duration,
    build_footer_line,
    format_runtime_footer,
    phase_times_from_result,
    turn_metrics,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, ""),
        (-1.0, ""),
        (0.4, "0.4s"),
        (9.99, "10.0s"),
        (10, "10s"),
        (42.7, "43s"),
        (60, "1m00s"),
        (125, "2m05s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3725, "1h02m"),
    ],
)
def test_format_duration(seconds, expected):
    assert _format_duration(seconds) == expected


def test_format_footer_full_timing_breakdown():
    out = format_runtime_footer(
        model="MiniMax-M3",
        context_tokens=20_000,
        context_length=200_000,
        turn_time=70.0,
        api_time=42.0,
        tool_time=15.0,
        api_calls=3,
        fields=(
            "model",
            "context_pct",
            "turn_time",
            "api_time",
            "tool_time",
            "overhead_time",
            "api_calls",
        ),
    )
    assert out == (
        "MiniMax-M3 · 10% · 1m10s · api 42s · tools 15s · "
        "other 13s · 3 calls"
    )


def test_format_footer_timing_fields_are_independently_optional():
    out = format_runtime_footer(
        model="x",
        context_tokens=0,
        context_length=100,
        api_time=38.2,
        tool_time=3.1,
        api_calls=1,
        fields=("api_time", "tool_time", "api_calls"),
    )
    assert out == "api 38s · tools 3.1s · 1 call"


def test_format_footer_overhead_is_clamped_to_zero():
    out = format_runtime_footer(
        model="x",
        context_tokens=0,
        context_length=100,
        turn_time=10.0,
        api_time=15.0,
        tool_time=5.0,
        fields=("overhead_time",),
    )
    assert out == "other 0.0s"


@pytest.mark.parametrize(
    "tool_time,tool_calls,expected",
    [
        (0.0, 0, "tools 0"),
        (0.0, 5, "tools 0"),
        (3.1, 0, "tools 0"),
        (3.1, 5, "tools 3.1s"),
        (15.0, 3, "tools 15s"),
        (None, 0, ""),
        (None, None, ""),
    ],
)
def test_tool_time_zero_activity_renders_without_unit(tool_time, tool_calls, expected):
    """A turn with no tool activity (zero calls or an exactly-zero
    measurement) renders `tools 0`, not `tools 0.0s`; an unmeasured
    tool_time stays omitted."""
    out = format_runtime_footer(
        model="x", context_tokens=0, context_length=100,
        tool_time=tool_time, tool_calls=tool_calls,
        fields=("tool_time",),
    )
    assert out == expected


def test_format_footer_omits_breakdown_when_component_timings_are_unavailable():
    out = format_runtime_footer(
        model="x",
        context_tokens=0,
        context_length=100,
        turn_time=70.0,
        api_time=None,
        tool_time=None,
        fields=("turn_time", "api_time", "tool_time", "overhead_time"),
    )
    assert out == "1m10s"


def test_build_footer_wires_timing_values_end_to_end():
    out = build_footer_line(
        model="x",
        context_tokens=0,
        context_length=100,
        turn_time=70.0,
        api_time=42.0,
        tool_time=15.0,
        api_calls=3,
    )
    assert out == "x · 0% · 1m10s · api 42s · tools 15s · other 13s · 3 calls"

@pytest.mark.parametrize(
    "turn,api,tool,n_calls,n_tools,phases,expected",
    [
        # Fixed order after tools; residual renders above threshold.
        (20.0, 11.0, 0.0, 1, 0, {"save": 0.2, "compress": 5.0, "setup": 0.4, "mem": 3.2},
         "x · 0% · 20s · api 11s · tools 0 · mem 3.2s · setup 0.4s · "
         "compress 5.0s · save 0.2s · other 0.2s · 1 call"),
        # A 0.05s phase renders nothing yet shrinks the residual.
        (30.0, 15.0, 0.0, 1, 0, {"mem": 0.05, "setup": 4.9},
         "x · 0% · 30s · api 15s · tools 0 · setup 4.9s · other 10s · 1 call"),
        # Sub-threshold residual `other` renders nothing.
        (30.0, 15.0, 14.0, 1, 0, {"mem": 0.05, "setup": 0.02},
         "x · 0% · 30s · api 15s · tools 0 · other 0.9s · 1 call"),
        # No measured phases (None or {}): legacy single `other`.
        (70.0, 42.0, 15.0, 3, None, None,
         "x · 0% · 1m10s · api 42s · tools 15s · other 13s · 3 calls"),
        (70.0, 42.0, 15.0, 3, None, {},
         "x · 0% · 1m10s · api 42s · tools 15s · other 13s · 3 calls"),
        # Unknown phase keys are ignored, not rendered or attributed.
        (30.0, 14.0, 0.0, 1, 0, {"bogus": 5.0, "mem": 1.0},
         "x · 0% · 30s · api 14s · tools 0 · mem 1.0s · other 15s · 1 call"),
    ],
)
def test_footer_phase_tail_rules(turn, api, tool, n_calls, n_tools, phases, expected):
    out = format_runtime_footer(
        model="x", context_tokens=0, context_length=100,
        turn_time=turn, api_time=api, tool_time=tool,
        api_calls=n_calls, tool_calls=n_tools, phase_times=phases,
    )
    assert out == expected


def test_phase_times_from_result_maps_measured_keys_and_none_without_them():
    assert phase_times_from_result(
        {"mem_time": 3.2, "save_time": 0.2, "api_time": 1.0}
    ) == {"mem": 3.2, "save": 0.2}
    for result in (None, {}, {"api_time": 1.0}, {"mem_time": None, "setup_time": None}):
        assert phase_times_from_result(result) is None


def test_turn_metrics_carries_phase_times_for_every_return_path():
    metrics = turn_metrics(
        {"api_time": 1.0, "tool_time": 0.0, "tool_calls": 0, "mem_time": 0.5}
    )
    assert metrics["mem_time"] == 0.5
    assert metrics["compress_time"] is None
