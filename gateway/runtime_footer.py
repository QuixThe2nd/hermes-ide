
"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, timing) and
appends it to the FINAL message of an agent turn. Always on with the default
field set (``_DEFAULT_FIELDS``); not user-configurable.

The footer is appended to the final response text in ``gateway/run.py`` right
before returning the response to the adapter send path, so it only lands on
the final message a user sees, not on tool-progress updates or streaming
partials. When streaming is on and the final text has already been delivered
piecemeal, the footer is sent as a separate trailing message via
``send_trailing_footer()``.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = (
    "model", "context_pct", "turn_time", "api_time", "tool_time", "overhead_time",
    "api_calls", "tool_calls",
)
_PHASE_ORDER: tuple[str, ...] = ("mem", "setup", "compress", "save")
_PHASE_MIN_SECONDS = 0.1
_SEP = " · "
_MAKER_PREFIX = "claude-"
_EFFORT_SHORT = {"medium": "med.", "minimal": "min."}


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``. Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``) and the Anthropic
    maker prefix (``claude-opus-5-5`` → ``opus-5-5``). Names without the prefixes
    (``gpt-5.4``, ``gemini-3.1-pro``, a bare ``claude``) are unchanged."""
    if not model:
        return ""
    short = model.rsplit("/", 1)[-1]
    if short.startswith(_MAKER_PREFIX) and len(short) > len(_MAKER_PREFIX):
        return short[len(_MAKER_PREFIX):]
    return short


def _effort_short(effort: Optional[str]) -> str:
    """Parenthetical footer shorthand: ``medium`` → ``med.`` and ``minimal`` →
    ``min.``; every other level (``low``, ``high``, ``xhigh``, ``off``) is
    rendered verbatim."""
    if not effort:
        return ""
    return _EFFORT_SHORT.get(effort, effort)


def _format_duration(seconds: Optional[float]) -> str:
    """Human-friendly duration: 0.4s, 12s, 2m03s, 1h23m."""
    if seconds is None or seconds < 0:
        return ""
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _env_cwd() -> str:
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def format_runtime_footer(*, model: Optional[str], context_tokens: int,
                          context_length: Optional[int], cwd: Optional[str] = None,
                          turn_seconds: Optional[float] = None,
                          turn_time: Optional[float] = None,
                          api_time: Optional[float] = None,
                          tool_time: Optional[float] = None,
                          api_calls: Optional[int] = None,
                          tool_calls: Optional[int] = None,
                          requested_model: Optional[str] = None, served_model: Optional[str] = None,
                          reasoning_effort: Optional[str] = None,
                          phase_times: Optional[dict] = None,
                          fields: Iterable[str] = _DEFAULT_FIELDS) -> str:
    """Render the footer line, or "" if no fields have data. Fields whose data is missing (and
    unknown field names) are skipped silently — a partial footer beats ``?%`` or empty slots."""

    try:
        from agent.served_model import is_displayable_served_model
    except ImportError:
        def is_displayable_served_model(_value: str) -> bool:
            return True

    def context_pct() -> str:
        if context_length and context_length > 0 and context_tokens >= 0:
            return f"{max(0, min(100, round((context_tokens / context_length) * 100)))}%"
        return ""

    # Fork extension: per-turn timing breakdown accumulated by the conversation
    # loop (turn/api/tool), the derived overhead, and the API call count.
    overhead_time: Optional[float] = None
    if turn_time is not None and api_time is not None and tool_time is not None:
        overhead_time = max(0.0, float(turn_time) - float(api_time or 0.0) - float(tool_time or 0.0))

    def _labeled(prefix: str, seconds: Optional[float]) -> str:
        duration = _format_duration(seconds)
        return f"{prefix} {duration}" if duration else ""

    def _phase_tail() -> str:
        # Each category renders only at/above the threshold but always counts
        # toward the residual; no mapping keeps the legacy single `other`.
        if overhead_time is None:
            return ""
        if not phase_times:
            return _labeled("other", overhead_time)
        segments: list[str] = []
        attributed = 0.0
        for key in _PHASE_ORDER:
            seconds = phase_times.get(key)
            if not isinstance(seconds, (int, float)):
                continue
            attributed += max(0.0, float(seconds))
            if seconds >= _PHASE_MIN_SECONDS:
                segments.append(_labeled(key, float(seconds)))
        residual = max(0.0, overhead_time - attributed)
        if residual >= _PHASE_MIN_SECONDS:
            segments.append(_labeled("other", residual))
        return _SEP.join(segments)

    def api_calls_field() -> str:
        if api_calls is not None and api_calls > 0:
            label = "call" if api_calls == 1 else "calls"
            return f"{api_calls} {label}"
        return ""

    def tool_calls_field() -> str:
        if tool_calls is not None and tool_calls > 0:
            return f"{tool_calls} {'tool' if tool_calls == 1 else 'tools'}"
        return ""

    def served() -> str:
        requested = requested_model or model
        alias = _model_short(requested)
        raw_alias = requested.rsplit("/", 1)[-1] if requested else ""
        if served_model and is_displayable_served_model(served_model) \
                and served_model not in (alias, raw_alias, requested):
            return f"{alias} → {served_model}"
        return ""

    def model_field() -> str:
        name = _model_short(model)
        if served_model and is_displayable_served_model(served_model):
            req = requested_model or model
            if _model_short(served_model) != _model_short(req):
                name = _model_short(served_model)
        if not name:
            return ""
        tag = _effort_short(reasoning_effort)
        return f"{name} ({tag})" if tag else name

    def tool_time_field() -> str:
        # Zero tool activity renders without a unit; unmeasured/negative stays skipped.
        if tool_time is None or tool_time < 0:
            return ""
        if tool_time == 0 or tool_calls == 0:
            return "tools 0"
        return _labeled("tools", tool_time)

    renderers = {
        "model": model_field,
        "served_model": served,
        "context_pct": context_pct,
        # Skipped when the caller did not measure (None) or the value is negative.
        "latency": lambda: _format_latency(turn_seconds) if turn_seconds is not None and turn_seconds >= 0 else "",
        "cwd": lambda: _home_relative_cwd(cwd or _env_cwd()),
        "turn_time": lambda: _format_duration(turn_time),
        "api_time": lambda: _labeled("api", api_time),
        "tool_time": tool_time_field,
        "overhead_time": _phase_tail,
        "api_calls": api_calls_field,
        "tool_calls": tool_calls_field,
    }
    return _SEP.join(v for field in fields if (render := renderers.get(field)) and (v := render()))


def build_footer_line(*, model: Optional[str], context_tokens: int, context_length: Optional[int],
                      cwd: Optional[str] = None, turn_seconds: Optional[float] = None,
                      turn_time: Optional[float] = None, api_time: Optional[float] = None,
                      tool_time: Optional[float] = None, api_calls: Optional[int] = None,
                      tool_calls: Optional[int] = None,
                      requested_model: Optional[str] = None, served_model: Optional[str] = None,
                      phase_times: Optional[dict] = None,
                      reasoning_effort: Optional[str] = None) -> str:
    """Entry point for gateway/run.py: footer text, or "" when no field has data. Callers append it
    to the final response themselves, preserving a single blank line of separation.
    ``turn_seconds`` is the caller-measured (``time.monotonic()``) run duration; ``None`` skips the
    ``api_calls`` / ``tool_calls`` feed the extended timing fields and are skipped when unset.
    ``phase_times`` (see ``phase_times_from_result``) splits the remainder into measured
    categories; ``None``/empty keeps the single legacy ``other`` segment."""
    return format_runtime_footer(model=model, context_tokens=context_tokens,
                                 context_length=context_length, cwd=cwd, turn_seconds=turn_seconds,
                                 turn_time=turn_time, api_time=api_time, tool_time=tool_time,
                                 api_calls=api_calls, tool_calls=tool_calls,
                                 phase_times=phase_times,
                                 requested_model=requested_model, served_model=served_model,
                                 reasoning_effort=reasoning_effort)


def phase_times_from_result(result: dict[str, Any] | None) -> Optional[dict]:
    """Measured phase seconds keyed by phase, or ``None`` when no category was
    timed (the footer then keeps its legacy single-``other`` render)."""
    result = result or {}
    measured = {
        key: float(result[f"{key}_time"])
        for key in _PHASE_ORDER
        if isinstance(result.get(f"{key}_time"), (int, float))
    }
    return measured or None


def footer_phase_times(result: dict[str, Any] | None, *, turn_ended_at: Optional[float] = None) -> Optional[dict]:
    """Agent phase buckets plus the gateway's spans outside ``run_conversation`` (so no api/tool
    overlap): ``gateway_setup_time`` into ``setup``; ``conversation_ended_at`` → ``turn_ended_at``
    (perf_counter, delivery drain) into ``save``. Per-key sum skipping missing, non-numeric and
    negative values; ``None`` if nothing counted. Go twin: ``footerfmt.FooterPhases``."""
    result = result or {}
    gateway = {"setup": result.get("gateway_setup_time")}
    ended_at = result.get("conversation_ended_at")
    if turn_ended_at is not None and isinstance(ended_at, (int, float)):
        gateway["save"] = turn_ended_at - ended_at
    merged: dict[str, float] = {}
    for phases in (gateway, phase_times_from_result(result) or {}):
        for key, seconds in phases.items():
            if isinstance(seconds, (int, float)) and seconds >= 0:
                merged[key] = merged.get(key, 0.0) + float(seconds)
    return merged or None


def turn_metrics(result: dict[str, Any] | None) -> dict[str, Any]:
    """Footer inputs carried from an agent result onto the gateway's agent_result dict; spread
    it (``**turn_metrics(result)``) into every return path so the timing fields never get dropped."""
    result = result or {}
    return {
        key: result.get(key)
        for key in ("api_time", "tool_time", "tool_calls", *(f"{k}_time" for k in _PHASE_ORDER))
    }
