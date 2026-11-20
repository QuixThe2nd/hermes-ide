"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os

import pytest

from gateway.runtime_footer import (
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    format_runtime_footer,
)


# ---------------------------------------------------------------------------
# _model_short + _home_relative_cwd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.4", "gpt-5.4"),
        ("anthropic/claude-sonnet-4.6", "sonnet-4.6"),
        ("claude-opus-5-5", "opus-5-5"),
        ("gpt-5.4", "gpt-5.4"),
        ("gemini-3.1-pro", "gemini-3.1-pro"),
        ("claude", "claude"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_short_drops_vendor_and_maker_prefix(model, expected):
    assert _model_short(model) == expected


def test_home_relative_cwd_collapses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # On Windows os.path.expanduser("~") reads USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    sub = tmp_path / "projects" / "hermes"
    sub.mkdir(parents=True)
    result = _home_relative_cwd(str(sub))
    assert result == os.path.join("~", "projects", "hermes")


# ---------------------------------------------------------------------------
# format_runtime_footer
# ---------------------------------------------------------------------------

def test_format_footer_all_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # On Windows os.path.expanduser("~") reads USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "projects" / "hermes"))
    (tmp_path / "projects" / "hermes").mkdir(parents=True)
    out = format_runtime_footer(
        model="openrouter/openai/gpt-5.4",
        context_tokens=68000,
        context_length=100000,
        cwd=None,  # falls back to TERMINAL_CWD env var
        fields=("model", "context_pct", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · " + os.path.join("~", "projects", "hermes")


def test_format_footer_skips_missing_context_length():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=500,
        context_length=None,
        cwd="/tmp/wd",
        fields=("model", "context_pct", "cwd"),
    )
    # context_pct dropped silently; no "?%" artifact
    assert "%" not in out
    assert "gpt-5.4" in out
    assert os.path.abspath("/tmp/wd") in out


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# latency — opt-in wall-clock turn duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.999, "<1s"),
        (1.0, "1s"),
        (22.0, "22s"),
        (22.4, "22s"),
        (59.4, "59s"),
        (59.6, "1m00s"),
        (60.0, "1m00s"),
        (65.0, "1m05s"),
        (125.0, "2m05s"),
        (3600.0, "60m00s"),
    ],
)
def test_format_latency(seconds, expected):
    from gateway.runtime_footer import _format_latency

    assert _format_latency(seconds) == expected


def test_format_footer_latency_renders():
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
        fields=("latency",),
    )
    assert out == "22s"


def test_format_footer_latency_skipped_when_unmeasured():
    """A call site that doesn't measure timing leaves the field out entirely."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=None,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_skipped_when_negative():
    """A nonsensical (negative) duration is dropped rather than rendered."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=-1.0,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_zero_renders_sub_second():
    """Zero is a real measurement (a very fast turn), not missing data."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=0.0,
        fields=("latency",),
    )
    assert out == "<1s"


def test_format_footer_latency_in_field_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # On Windows os.path.expanduser("~") reads USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=68_000,
        context_length=100_000,
        cwd=str(tmp_path),
        turn_seconds=65.0,
        fields=("model", "context_pct", "latency", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · 1m05s · ~"


# ---------------------------------------------------------------------------
# Default behaviour: footer is ON with the timing + call-count fields; cwd,
# latency and served_model stay opt-in.
# ---------------------------------------------------------------------------

_DEFAULT_FOOTER_FIELDS = [
    "model", "context_pct", "turn_time", "api_time", "tool_time", "overhead_time",
    "api_calls", "tool_calls",
]
# An absolute path that survives _home_relative_cwd unchanged on every host
# (os.path.abspath("/var/data") -> "C:\\var\\data" on Windows, "/var/data" on POSIX).
_VAR_DATA = os.path.abspath("/var/data")


def test_default_fields_include_timing_and_exclude_optins():
    from gateway.runtime_footer import _DEFAULT_FIELDS
    fields = list(_DEFAULT_FIELDS)
    assert fields == _DEFAULT_FOOTER_FIELDS
    for optin in ("cwd", "latency", "served_model"):
        assert optin not in fields


def test_default_fields_have_no_standalone_reasoning_segment():
    """Order contract: the effort shorthand rides the model field, so there is
    no trailing reasoning segment; tool_calls stays last."""
    from gateway.runtime_footer import _DEFAULT_FIELDS

    assert "reasoning" not in _DEFAULT_FIELDS
    assert _DEFAULT_FIELDS[-1] == "tool_calls"


# ---------------------------------------------------------------------------
# reasoning effort — parenthesized shorthand riding the model field
# ---------------------------------------------------------------------------


def test_effort_renders_as_shorthand_after_model():
    out = format_runtime_footer(
        model="opus-5-5", context_tokens=0, context_length=None, cwd="",
        reasoning_effort="medium", fields=("model",),
    )
    assert out == "opus-5-5 (med.)"


@pytest.mark.parametrize(
    "effort,tag",
    [("minimal", "min."), ("low", "low"), ("xhigh", "xhigh"), ("off", "off")],
)
def test_effort_levels_other_than_medium_render_verbatim(effort, tag):
    out = format_runtime_footer(
        model="opus-5-5", context_tokens=0, context_length=None, cwd="",
        reasoning_effort=effort, fields=("model",),
    )
    assert out == f"opus-5-5 ({tag})"


def test_effort_skipped_when_unset():
    """None (no reachable agent / no effort) leaves the model alone."""
    out = format_runtime_footer(
        model="m", context_tokens=0, context_length=None, cwd="",
        reasoning_effort=None, fields=("model",),
    )
    assert out == "m"


def test_effort_dropped_when_no_model():
    """With no model to carry it, the effort is not rendered at all."""
    out = format_runtime_footer(
        model=None, context_tokens=0, context_length=None, cwd="",
        reasoning_effort="medium", fields=("model",),
    )
    assert out == ""


def test_enabled_without_fields_gives_timing():
    out = build_footer_line(
        model="openai/gpt-5.4",
        context_tokens=50_000, context_length=1_000_000,
        turn_time=20.0, api_time=12.0, tool_time=5.0, api_calls=3, tool_calls=9,
    )
    assert out == "gpt-5.4 · 5% · 20s · api 12s · tools 5.0s · other 3.0s · 3 calls · 9 tools"


def test_footer_compact_reference_case():
    """Contract case from the compact-format change: a claude model with
    medium effort and no tool activity drops the maker prefix, rides the
    effort as a parenthetical, renders `tools 0`, and has no trailing
    reasoning segment. Vendor-prefixed input gives the same line."""
    common = dict(
        context_tokens=23_000, context_length=100_000,
        turn_time=14.9, api_time=11.0, tool_time=0.0,
        api_calls=1, tool_calls=0, reasoning_effort="medium",
    )
    expected = "opus-5-5 (med.) · 23% · 15s · api 11s · tools 0 · other 3.9s · 1 call"
    assert build_footer_line(model="claude-opus-5-5", **common) == expected
    assert build_footer_line(model="anthropic/claude-opus-5-5", **common) == expected


def test_served_model_alias_uses_maker_stripped_name():
    """The opt-in `alias → served` line shortens the alias with the same rules
    as the model field; a served model equal to the raw (un-shortened) alias
    is still the same model and is skipped."""
    line = format_runtime_footer(
        model="anthropic/claude-opus-5-5", context_tokens=0, context_length=None, cwd="/x",
        served_model="grok-4.6", fields=["served_model"])
    assert line == "opus-5-5 → grok-4.6"
    assert format_runtime_footer(
        model="anthropic/claude-opus-5-5", context_tokens=0, context_length=None, cwd="/x",
        served_model="claude-opus-5-5", fields=["served_model"]) == ""


@pytest.mark.parametrize("count,expected", [(1, "1 tool"), (9, "9 tools"), (0, ""), (None, "")])
def test_tool_calls_field(count, expected):
    out = format_runtime_footer(model=None, context_tokens=0, context_length=None,
                                tool_calls=count, fields=("tool_calls",))
    assert out == expected


def test_turn_metrics_keeps_timing_tool_calls_and_phase_times():
    from gateway.runtime_footer import turn_metrics

    result = {"api_time": 1.5, "tool_time": 2.5, "tool_calls": 4, "final_response": "x"}
    assert turn_metrics(result) == {
        "api_time": 1.5, "tool_time": 2.5, "tool_calls": 4,
        "mem_time": None, "setup_time": None, "compress_time": None, "save_time": None,
    }
    result["mem_time"] = 3.2
    assert turn_metrics(result)["mem_time"] == 3.2
    assert turn_metrics(None) == {
        "api_time": None, "tool_time": None, "tool_calls": None,
        "mem_time": None, "setup_time": None, "compress_time": None, "save_time": None,
    }


@pytest.mark.parametrize(
    "model,tokens,window,cwd,expected",
    [
        ("openai/gpt-5.4", 50_247, 1_000_000, _VAR_DATA, "gpt-5.4 · 5%"),
        ("m", 0, None, _VAR_DATA, "m"),
        ("", 10, 100, "", "10%"),
    ],
)
def test_default_footer_omits_cwd_and_unmeasured_fields(
    monkeypatch, model, tokens, window, cwd, expected
):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model=model, context_tokens=tokens, context_length=window, cwd=cwd, turn_seconds=22.0,
    )
    assert out == expected


def test_default_build_footer_line_ignores_turn_seconds(monkeypatch):
    """build_footer_line with default fields is unaffected by turn_seconds."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    common = dict(
        model="openai/gpt-5.4",
        context_tokens=50_247,
        context_length=1_000_000,
        cwd=_VAR_DATA,
    )
    baseline = build_footer_line(**common)
    with_timing = build_footer_line(**common, turn_seconds=125.0)
    assert baseline == "gpt-5.4 · 5%"
    assert with_timing == baseline


# ---------------------------------------------------------------------------
# Default `model` field — shows short served model when proxy differs from request
# ---------------------------------------------------------------------------


def test_default_model_field_shows_served_when_differs(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model="hermes",
        context_tokens=0,
        context_length=None,
        cwd="",
        served_model="xai/grok-4.6",
    )
    assert out.startswith("grok-4.6")
    assert not out.startswith("hermes")


def test_default_model_field_unchanged_without_served_model(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model="hermes",
        context_tokens=0,
        context_length=None,
        cwd="",
        served_model=None,
    )
    assert out.startswith("hermes")


def test_default_model_field_unchanged_when_served_equals_request(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model="hermes",
        context_tokens=0,
        context_length=None,
        cwd="",
        served_model="hermes",
    )
    assert out.startswith("hermes")

    out2 = format_runtime_footer(
        model="grok-4.6",
        context_tokens=0,
        context_length=None,
        cwd="",
        served_model="xai/grok-4.6",
    )
    assert out2.startswith("grok-4.6")


def test_format_footer_served_model_is_opt_in_and_skips_same_model():
    """#54864: `served_model` renders `alias → served` only when listed AND the served model
    differs from the requested one; the default field set never shows it."""
    # Default fields: served model is invisible.
    assert "→" not in format_runtime_footer(
        model="hermes-router", context_tokens=0, context_length=None, cwd="/x",
        served_model="gpt-4o-2024-11-20")
    line = format_runtime_footer(
        model="hermes-router", context_tokens=0, context_length=None, cwd="/x",
        served_model="gpt-4o-2024-11-20", fields=["served_model"])
    assert line == "hermes-router → gpt-4o-2024-11-20"
    # Hermes fallback route: requested primary → active model.
    line = format_runtime_footer(
        model="qwen/qwen3.8-max", context_tokens=0, context_length=None, cwd="/x",
        requested_model="gpt-5.6-sol", served_model="qwen/qwen3.8-max", fields=["served_model"])
    assert line == "gpt-5.6-sol → qwen/qwen3.8-max"
    # Served == requested (no header, no fallback): field skipped, nothing empty rendered.
    assert format_runtime_footer(
        model="gpt-5.4", context_tokens=0, context_length=None, cwd="/x",
        served_model=None, fields=["served_model"]) == ""


def test_default_model_field_ignores_undisplayable_served_hash(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    hash_served = "31324f758efb073636feda516865fd641caf2eb384a3e86bd0fe7e5d80aca6c4"
    out = format_runtime_footer(
        model="hermes",
        context_tokens=0,
        context_length=None,
        cwd="",
        served_model=hash_served,
    )
    assert out.startswith("hermes")
    assert hash_served not in out

    out_served_field = format_runtime_footer(
        model="hermes",
        context_tokens=0,
        context_length=None,
        cwd="",
        served_model=hash_served,
        fields=["served_model"],
    )
    assert out_served_field == ""
