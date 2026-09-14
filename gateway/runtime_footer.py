
"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd, timing)
and appends it to the FINAL message of an agent turn when enabled. Off by
default to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true
        fields: [model, context_pct, cwd, turn_time, api_time, tool_time, overhead_time, api_calls]

Available fields:
    model        — bare model id, vendor prefix dropped (``gpt-5.4``)
    context_pct  — last-call context occupancy as a percent (``5%``)
    latency      — wall-clock duration of the turn (``22s``, ``1m05s``)
    cwd          — home-relative working dir (``~``)

``latency`` is opt-in: it is not in the default field set, so a footer whose
``fields`` are unset renders exactly as before.

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

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

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


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
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    return model.rsplit("/", 1)[-1] if model else ""


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


def resolve_footer_config(user_config: dict[str, Any] | None, platform_key: str | None = None) -> dict[str, Any]:
    """Resolve effective footer config: defaults (enabled=False) <
    ``display.runtime_footer`` < ``display.platforms.<platform_key>.runtime_footer``."""
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}
    plat_cfg = (cfg.get("platforms") or {}).get(platform_key) if platform_key else None
    sections = [cfg.get("runtime_footer"), plat_cfg.get("runtime_footer") if isinstance(plat_cfg, dict) else None]
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "enabled" in section:
            resolved["enabled"] = bool(section.get("enabled"))
        if isinstance(section.get("fields"), list) and section["fields"]:
            resolved["fields"] = [str(f) for f in section["fields"]]
    return resolved


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
                          fields: Iterable[str] = _DEFAULT_FIELDS) -> str:
    """Render the footer line, or "" if no fields have data. Fields whose data is missing (and
    unknown field names) are skipped silently — a partial footer beats ``?%`` or empty slots."""
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

    def api_calls_field() -> str:
        if api_calls is not None and api_calls > 0:
            label = "call" if api_calls == 1 else "calls"
            return f"{api_calls} {label}"
        return ""

    renderers = {
        "model": lambda: _model_short(model),
        "context_pct": context_pct,
        # Skipped when the caller did not measure (None) or the value is negative.
        "latency": lambda: _format_latency(turn_seconds) if turn_seconds is not None and turn_seconds >= 0 else "",
        "cwd": lambda: _home_relative_cwd(cwd or _env_cwd()),
        "turn_time": lambda: _format_duration(turn_time),
        "api_time": lambda: _labeled("api", api_time),
        "tool_time": lambda: _labeled("tools", tool_time),
        "overhead_time": lambda: _labeled("other", overhead_time),
        "api_calls": api_calls_field,
    }
    return _SEP.join(v for field in fields if (render := renderers.get(field)) and (v := render()))


def build_footer_line(*, user_config: dict[str, Any] | None, platform_key: str | None,
                      model: Optional[str], context_tokens: int, context_length: Optional[int],
                      cwd: Optional[str] = None, turn_seconds: Optional[float] = None,
                      turn_time: Optional[float] = None, api_time: Optional[float] = None,
                      tool_time: Optional[float] = None, api_calls: Optional[int] = None) -> str:
    """Entry point for gateway/run.py: footer text, or "" when disabled / no data. Callers append it
    to the final response themselves, preserving a single blank line of separation.
    ``turn_seconds`` is the caller-measured (``time.monotonic()``) run duration; ``None`` skips the
    ``latency`` field. The optional ``turn_time`` / ``api_time`` / ``tool_time`` breakdown and
    ``api_calls`` feed the extended timing fields and are skipped when unset."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(model=model, context_tokens=context_tokens,
                                 context_length=context_length, cwd=cwd, turn_seconds=turn_seconds,
                                 turn_time=turn_time, api_time=api_time, tool_time=tool_time,
                                 api_calls=api_calls, fields=cfg.get("fields") or _DEFAULT_FIELDS)
