"""Per-platform display/verbosity resolver (``resolve_display_setting``).

Resolution order, first non-None wins: ``display.platforms.<platform>.<key>`` →
``display.<key>`` → ``_PLATFORM_DEFAULTS[platform][key]`` → ``_GLOBAL_DEFAULTS[key]``.
Exception: ``display.streaming`` is CLI-only; gateway streaming follows the top-level
``streaming`` config unless a per-platform override sets it. Legacy
``display.tool_progress_overrides`` is still read as a ``tool_progress`` fallback.
"""

from __future__ import annotations

from typing import Any

from gateway.runtime_footer import _format_duration

# ---------------------------------------------------------------------------
# Overrideable display settings and their global defaults
# ---------------------------------------------------------------------------
# These are the settings that can be configured per-platform.
# Other display settings (compact, personality, skin, etc.) are CLI-only
# and don't participate in per-platform resolution.

_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",
    "tool_progress_grouping": "accumulate",  # "accumulate" = edit one bubble; "separate" = one msg per tool
    "show_reasoning": False,
    # How a reasoning/thinking summary is rendered when show_reasoning is on.
    #   "code"      -> 💭 **Reasoning:** + fenced code block (legacy default)
    #   "blockquote"-> each line prefixed with "> "
    #   "subtext"   -> each line prefixed with "-# " (Discord small grey subtext)
    #   "compact"   -> single "💭 thought for Xs" duration line, no CoT text;
    #                  appends " (N tokens)" when this turn's count is known
    # Discord defaults to "subtext"; everywhere else defaults to "code".
    "reasoning_style": "code",
    "tool_preview_length": 0,
    "streaming": None,  # None = follow top-level streaming config
    # Gateway-only assistant/status chatter; mobile platforms opt down to final-answer-first.
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
    # Whether busy_input_mode=steer sends a visible "Steered into current run"
    # acknowledgment after successfully injecting the user's mid-turn message.
    # Disable when the platform should steer silently (the text still lands in
    # the active run; only the confirmation echo is suppressed).
    "busy_steer_ack_enabled": True,
    # Whether steer mode ALSO sends the follow-up "✅ Steer delivered" bubble
    # at the moment the steered text is actually injected into the model's
    # context (the immediate ack only promises that it WILL arrive). Disable
    # to keep only the initial acknowledgment.
    "busy_steer_delivered_ack_enabled": True,
    # When true, delete tool-progress / "⏳ Working — N min" / status bubbles
    # after the final response lands on platforms that support message
    # deletion (e.g. Telegram). Off by default — progress is still shown
    # live, just cleaned up after success so the chat doesn't fill up with
    # stale breadcrumbs. Failed runs leave bubbles in place as breadcrumbs.
    "cleanup_progress": False,
    # Working-state text on text-rendering indicators (Slack assistant status): "full"/true = verb +
    # argument preview, "verb" = verb only (keeps paths out of shared channels), "off"/false = static.
    "live_status": "full",
    # Opt-in LIVE retry/fallback progress. Retry chatter is buffered by design
    # (dropped on recovery, flushed only on terminal failure) so a transient
    # 429 doesn't flood the chat. Turning this on ALSO mirrors those buffered
    # lines live on the "retry_progress" status rail, so a user stuck in a
    # multi-minute provider stall sees why instead of a silent spinner.
    # Deliberately absent from every tier: no platform gets it by default.
    "retry_progress": False,
}

# Tiers: HIGH = editing, personal/team use; MEDIUM = editing but customer-facing;
# LOW = no edit support (progress messages are permanent); MINIMAL = batch delivery.
_TIER_HIGH = {
    "tool_progress": "all", "show_reasoning": False, "tool_preview_length": 40,
    "streaming": None,  # follow global
    "interim_assistant_messages": True, "long_running_notifications": True, "busy_ack_detail": True,
}
_TIER_MEDIUM = {**_TIER_HIGH, "tool_progress": "new"}
_TIER_LOW = {
    **_TIER_HIGH, "tool_progress": "off", "streaming": False,
    "interim_assistant_messages": False, "long_running_notifications": False, "busy_ack_detail": False,
}
_TIER_MINIMAL = {**_TIER_LOW, "tool_preview_length": 0}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    # Tier 1 — full edit support, personal/team use
    # Telegram is usually a mobile inbox: keep tool_progress quiet and skip
    # the verbose busy-ack iteration counter, but DO surface real mid-turn
    # assistant commentary (interim_assistant_messages) and DO send periodic
    # heartbeats (long_running_notifications) so the user has signal between
    # turn start and final answer. Otherwise it looks like "typing..." for
    # 30 minutes with nothing happening. Opt in to verbose iteration detail
    # via display.platforms.telegram.busy_ack_detail / tool_progress.
    "telegram":    {
        **_TIER_HIGH,
        "tool_progress": "off",
        "busy_ack_detail": False,
    },
    # Discord has a native "subtext" primitive (-# small grey text) that reads
    # as metadata rather than content, so reasoning summaries default to it
    # here instead of the fenced code block used elsewhere.
    # Discord's long-running heartbeat defaults to the phase line ("⏳ terminal
    # 1m42s" / "⏳ grok-4.6 38s" / "⏳ packing 12s") — the current wait plus its
    # elapsed time — instead of the raw diagnostic join ("⏳ Working — 3 min —
    # iteration N/M, <provider wait notice>"). "generic" keeps the catalog
    # phrase ("⏳ still working"); raw detail remains available via
    # display.platforms.discord.long_running_notifications: true (plus
    # busy_ack_detail for the iteration counter).
    "discord":     {**_TIER_HIGH, "reasoning_style": "subtext", "long_running_notifications": "phase"},

    # Tier 2 — edit support, often customer/workspace channels
    # Slack: tool_progress off by default — Bolt posts cannot be edited like CLI;
    # "new"/"all" spam permanent lines in channels (hermes-agent#14663).
    "slack":           {
        **_TIER_MEDIUM,
        "tool_progress": "off",
        "long_running_notifications": False,
        "busy_ack_detail": False,
    },
    "mattermost":      _TIER_MEDIUM,
    "matrix":          _TIER_MEDIUM,
    "feishu":          _TIER_MEDIUM,
    # Buzz (Nostr relay via buzz-cli): messages can be edited in place
    # (`buzz messages edit`), so grouped/accumulating progress works, but
    # channels are shared community spaces — keep the medium tier. Without
    # this entry Buzz inherited the verbose _GLOBAL_DEFAULTS and every
    # interim update became a separate permanent channel post (#95841).
    "buzz":            _TIER_MEDIUM,

    # Tier 3 — no edit support, progress messages are permanent
    "signal":          _TIER_LOW,
    "whatsapp":        _TIER_MEDIUM,  # Baileys bridge supports /edit
    # WhatsApp Cloud API: Meta added message editing in 2023 but the
    # Hermes Cloud adapter doesn't implement edit_message yet, so we
    # stay on TIER_LOW (tool_progress off) to avoid spamming each
    # status update as a separate message. Promote to TIER_MEDIUM once
    # Cloud's edit_message lands.
    "whatsapp_cloud":  _TIER_LOW,
    # Photon (managed iMessage over the gRPC sidecar) and BlueBubbles are both
    # permanent-message iMessage inboxes with no message-edit support, so both
    # stay TIER_LOW. This keeps tool progress, interim scratch commentary,
    # "still working" heartbeats, and busy-ack iteration detail out of the
    # user's iMessage thread. Without this entry Photon inherited the noisy
    # global ("all") defaults and compacted/narrated on nearly every turn.
    "photon":          _TIER_LOW,
    "bluebubbles":     _TIER_LOW,
    "weixin":          _TIER_LOW,
    # WeCom is technically non-editable but exposes a native streaming
    # transport (msgtype: "stream" via aibot_respond_msg) that the gateway
    # consumer routes mid-stream content through. Enable streaming by default
    # so the WeCom client renders the typing animation and cumulative content
    # updates instead of a single one-shot markdown drop.
    "wecom":           {**_TIER_LOW, "streaming": True},
    "wecom_callback":  _TIER_LOW,
    "dingtalk":        _TIER_LOW,

    # Tier 4 — batch or non-interactive delivery
    "email":           _TIER_MINIMAL,
    "sms":             _TIER_MINIMAL,
    "webhook":         _TIER_MINIMAL,
    "homeassistant":   _TIER_MINIMAL,
    "api_server":      {**_TIER_HIGH, "tool_preview_length": 0},
}

# Canonical set of per-platform overrideable keys (for validation).
OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())


def resolve_display_setting(user_config: dict, platform_key: str, setting: str, fallback: Any = None) -> Any:
    """Resolve a display setting with per-platform override support (see module docstring for order).

    ``platform_key`` is the platform config key (``"telegram"``; see ``_platform_config_key`` in
    gateway/run.py). Returns *fallback* when nothing is configured.
    """
    display_cfg = user_config.get("display") or {}
    plat_overrides = (display_cfg.get("platforms") or {}).get(platform_key)
    if isinstance(plat_overrides, dict) and plat_overrides.get(setting) is not None:
        return _normalise(setting, plat_overrides[setting])
    if setting == "tool_progress":  # legacy display.tool_progress_overrides.<platform>
        legacy = display_cfg.get("tool_progress_overrides")
        if isinstance(legacy, dict) and legacy.get(platform_key) is not None:
            return _normalise(setting, legacy[platform_key])
    if setting != "streaming" and display_cfg.get(setting) is not None:  # display.streaming is CLI-only
        return _normalise(setting, display_cfg[setting])
    val = _PLATFORM_DEFAULTS.get(platform_key, {}).get(setting)
    if val is None:
        val = _GLOBAL_DEFAULTS.get(setting)
    return fallback if val is None else val


# --- Normalisation of YAML quirks (bare ``off`` → False in YAML 1.1, etc.) ---

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no"}


def _norm_tristate(on: str, off: str, choices: set, extra_truthy: set = frozenset()):
    """Normaliser for bool-or-keyword settings: bools/truthy tokens → *on*, falsy → *off*, else a known choice or *on*."""
    def norm(value: Any) -> str:
        if isinstance(value, bool):
            return on if value else off
        val = str(value).strip().lower()
        if val in _FALSY:
            return off
        if val in _TRUTHY | extra_truthy:
            return on
        return val if val in choices else on
    return norm


def _norm_bool(value: Any) -> bool:
    return value.strip().lower() in _TRUTHY | {"raw", "verbose"} if isinstance(value, str) else bool(value)


def _norm_long_running(value: Any) -> Any:
    if isinstance(value, str):
        val = value.strip().lower()
        if val in {"generic", "phase"}:
            return val
    return _norm_bool(value)


def _norm_cleanup_progress(value: Any) -> bool:
    return value.lower() in _TRUTHY if isinstance(value, str) else bool(value)


def _norm_choice(choices: tuple[str, ...]) -> Any:
    def norm(value: Any) -> str:
        val = str(value).lower()
        return val if val in choices else choices[0]

    return norm


def _norm_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_NORMALISERS: dict[str, Any] = {
    "tool_progress": _norm_tristate("all", "off", {"off", "new", "all", "verbose", "log"}),
    "show_reasoning": _norm_bool,
    "streaming": _norm_bool,
    "interim_assistant_messages": _norm_bool,
    "long_running_notifications": _norm_long_running,
    "busy_ack_detail": _norm_bool,
    "busy_steer_ack_enabled": _norm_bool,
    # Fork additions: the second steer ack + live retry-progress rail.
    "busy_steer_delivered_ack_enabled": _norm_bool,
    "retry_progress": _norm_bool,
    "thinking_progress": _norm_bool,
    "cleanup_progress": _norm_cleanup_progress,
    "live_status": _norm_tristate("full", "off", {"full", "verb", "off"}, extra_truthy={"all"}),
    "tool_progress_grouping": _norm_choice(("accumulate", "separate")),
    # "compact" (fork): single "💭 thought for Xs" duration line, no CoT text.
    "reasoning_style": _norm_choice(("code", "blockquote", "subtext", "compact")),
    "tool_preview_length": _norm_int,
}


def format_reasoning_prefix(
    style: str,
    last_reasoning: str | None,
    turn_seconds: float | None,
    platform_key: str,
    thought_tokens: int | None = None,
) -> str:
    """Render the reasoning block prepended to a final response.

    Returns ``""`` when there is nothing to show — no reasoning text, or the
    ``compact`` style with no measurable duration. Callers own the
    ``show_reasoning`` gate and prepend the result as ``f"{prefix}\\n\\n{response}"``.

    ``compact`` never includes chain-of-thought text: it renders a single
    💭-marked duration line (``-# 💭 thought for 12s`` on Discord,
    ``_💭 thought for 12s_`` elsewhere) so users see that the model reasoned
    without dumping the reasoning itself. When ``thought_tokens`` carries
    THIS turn's reasoning/output token count, the line gains a
    ``(N tokens)`` suffix (``-# 💭 thought for 12s (N tokens)``); a
    missing/zero count leaves the bare duration line. Duration formatting
    reuses :func:`gateway.runtime_footer._format_duration`.
    """
    if not last_reasoning:
        return ""
    if style == "compact":
        duration = _format_duration(turn_seconds)
        if not duration:
            return ""
        if thought_tokens:
            duration += f" ({thought_tokens} tokens)"
        if platform_key == "discord":
            return f"-# 💭 thought for {duration}"
        return f"_💭 thought for {duration}_"
    # Collapse long reasoning to keep messages readable
    lines = last_reasoning.strip().splitlines()
    if len(lines) > 15:
        display_reasoning = "\n".join(lines[:15])
        display_reasoning += f"\n_... ({len(lines) - 15} more lines)_"
    else:
        display_reasoning = last_reasoning.strip()
    if style == "subtext":
        quoted = "\n".join(
            f"-# {ln}" if ln else "-#" for ln in display_reasoning.splitlines()
        )
        return f"-# 💭 Reasoning\n{quoted}"
    if style == "blockquote":
        quoted = "\n".join(
            f"> {ln}" if ln else ">" for ln in display_reasoning.splitlines()
        )
        return f"> 💭 **Reasoning:**\n{quoted}"
    # Default "code" style: escape ``` inside reasoning so inner fences don't
    # break the outer code block used to render it.
    from gateway.stream_consumer_fences import escape_code_fences_for_display

    return (
        f"💭 **Reasoning:**\n"
        f"```\n{escape_code_fences_for_display(display_reasoning)}\n```"
    )


def _normalise(setting: str, value: Any) -> Any:
    """Normalise a user-supplied value for *setting*; unknown settings pass through."""
    norm = _NORMALISERS.get(setting)
    return norm(value) if norm else value
