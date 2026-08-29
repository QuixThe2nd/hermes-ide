"""Channel rename for live agent count and drain progress.

When idle, a configured Discord channel is named ``agents-N`` (N =
currently running agents). While draining before a shutdown, it is
renamed once to ``restarting-N-agents``. Boot restores the idle
``agents-N`` label.

Discord rate-limits channel name edits to roughly 2 per 10 minutes per
channel. Idle updates therefore honor a cooldown: every successful edit
stamps a shared last-edit clock, and further idle refreshes requested
within ``min_interval_seconds`` (default 600) of it are deferred, not
dropped — the scheduled task waits for the window to open and then
applies the newest agent count. Drain and boot renames are one-shot
and exempt from the cooldown, but they too stamp the clock so the next
idle refresh spaces itself after them. Idle updates also fire only
when the count actually changes, and failed/throttled edits are
ignored. Drain is still set-once + restore, not a live tick-down.

Config (config.yaml):

    gateway:
      restart_channel_rename:
        platform: discord          # optional, default discord
        channel_id: "1541012892462223391"
        idle_template: "agents-{agents}"              # optional
        renamed_template: "restarting-{agents}-agents"  # optional
        min_interval_seconds: 600   # optional, 0 disables the cooldown

Everything is best-effort: failures log at debug/info and never affect
shutdown, startup, or turn sequencing.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "discord"
DEFAULT_BASE_NAME = "gateway-restarts"
DEFAULT_TEMPLATE = "restarting-{agents}-agents"
DEFAULT_IDLE_TEMPLATE = "agents-{agents}"
DEFAULT_MIN_INTERVAL_SECONDS = 600.0


def parse_restart_channel_rename_config(raw: Any) -> Dict[str, Any]:
    """Normalize the gateway.restart_channel_rename config value.

    Returns an empty dict when unset/malformed/disabled so callers can
    treat "no config" uniformly.
    """
    if not isinstance(raw, dict):
        return {}
    channel_id = str(raw.get("channel_id") or "").strip()
    if not channel_id.isdigit():
        return {}
    platform = str(raw.get("platform") or DEFAULT_PLATFORM).strip().lower()
    base_name = str(raw.get("base_name") or DEFAULT_BASE_NAME).strip()
    template = str(
        raw.get("renamed_template")
        if raw.get("renamed_template") is not None
        else DEFAULT_TEMPLATE
    ).strip()
    idle_template = str(
        raw.get("idle_template")
        if raw.get("idle_template") is not None
        else DEFAULT_IDLE_TEMPLATE
    ).strip()
    if not base_name:
        return {}
    return {
        "platform": platform,
        "channel_id": channel_id,
        "base_name": base_name,
        "template": template,
        "idle_template": idle_template or DEFAULT_IDLE_TEMPLATE,
        "min_interval_seconds": _parse_min_interval(
            raw.get("min_interval_seconds")
        ),
    }


def _parse_min_interval(raw: Any) -> float:
    """Coerce ``min_interval_seconds``; missing -> default, negative -> 0.

    Non-finite values (``inf`` — including YAML ``.inf`` — and ``nan``)
    also fall back to the default: an infinite interval would make the
    deferred task sleep forever waiting for a window that never opens.
    """
    if raw is None:
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value):
        logger.debug(
            "[restart-channel-rename] bad min_interval_seconds %r; "
            "using default %.0f",
            raw,
            DEFAULT_MIN_INTERVAL_SECONDS,
        )
        return DEFAULT_MIN_INTERVAL_SECONDS
    return max(0.0, value)


def _render_label(
    template: str, agents: int, *, fallback: str = DEFAULT_TEMPLATE
) -> str:
    try:
        label = template.format(agents=agents)
    except (KeyError, IndexError, ValueError):
        logger.debug(
            "[restart-channel-rename] bad template %r; using default", template
        )
        label = fallback.format(agents=agents)
    return label.strip() or fallback.format(agents=agents)


def _agent_count(runner: Any) -> int:
    try:
        return max(0, int(runner._running_agent_count()))
    except Exception:
        return 0


async def _edit_channel_name(adapter: Any, channel_id: str, name: str) -> bool:
    rename = getattr(adapter, "rename_thread", None)
    if not callable(rename):
        return False
    try:
        return bool(await rename(str(channel_id), name))
    except TypeError:
        return False
    except Exception:
        logger.debug(
            "[restart-channel-rename] rename of %s failed", channel_id,
            exc_info=True,
        )
        return False


def _resolve_adapter(runner: Any, cfg: Dict[str, Any]):
    try:
        from gateway.session import Platform

        platform = Platform(cfg["platform"])
    except Exception:
        logger.debug(
            "[restart-channel-rename] unknown platform %r", cfg["platform"]
        )
        return None
    adapters = getattr(runner, "adapters", {}) or {}
    # Production gateways key adapters by Platform enum; accept plain
    # string keys too so test fakes and alternate runners work.
    return adapters.get(platform) or adapters.get(cfg["platform"])


def _config_for(runner: Any) -> Dict[str, Any]:
    return parse_restart_channel_rename_config(
        getattr(getattr(runner, "config", None), "restart_channel_rename", None)
    )


def _is_draining(runner: Any) -> bool:
    return bool(
        getattr(runner, "_draining", False)
        or getattr(runner, "_restart_requested", False)
    )


def _min_interval(cfg: Dict[str, Any]) -> float:
    return max(
        0.0,
        float(cfg.get("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)),
    )


def _remaining_interval(runner: Any, cfg: Dict[str, Any]) -> float:
    """Seconds until the min-interval window opens; 0 = open/no prior edit."""
    min_interval = _min_interval(cfg)
    if min_interval <= 0:
        return 0.0
    last_ts = getattr(runner, "_restart_channel_rename_last_ts", None)
    if last_ts is None:
        return 0.0
    return max(0.0, min_interval - (time.monotonic() - last_ts))


async def _wait_for_rename_window(runner: Any, cfg: Dict[str, Any]) -> None:
    """Sleep until the cooldown allows the next channel edit."""
    min_interval = _min_interval(cfg)
    remaining = _remaining_interval(runner, cfg)
    while remaining > 0:
        # Cap each sleep at the interval so a bogus future timestamp
        # can never park the task in one unbounded sleep.
        await asyncio.sleep(min(remaining, min_interval))
        remaining = _remaining_interval(runner, cfg)


async def _apply_label(
    runner: Any,
    cfg: Dict[str, Any],
    label: str,
    *,
    agents: int,
    reason: str,
    enforce_interval: bool = False,
) -> bool:
    if getattr(runner, "_restart_channel_rename_last", None) == label:
        return True
    adapter = _resolve_adapter(runner, cfg)
    if adapter is None:
        logger.debug(
            "[restart-channel-rename] no %s adapter during %s",
            cfg["platform"],
            reason,
        )
        return False
    if enforce_interval:
        remaining = _remaining_interval(runner, cfg)
        if remaining > 0:
            logger.debug(
                "[restart-channel-rename] deferring %s rename to %r: "
                "%.1fs of cooldown left (min_interval_seconds=%.1f)",
                reason,
                label,
                remaining,
                _min_interval(cfg),
            )
            return False
    if await _edit_channel_name(adapter, cfg["channel_id"], label):
        runner._restart_channel_rename_last = label
        runner._restart_channel_rename_last_ts = time.monotonic()
        logger.info(
            "[restart-channel-rename] %s channel %s renamed to %r "
            "(%d running agents, %s)",
            cfg["platform"], cfg["channel_id"], label, agents, reason,
        )
        return True
    logger.debug(
        "[restart-channel-rename] %s of %s did not apply "
        "(throttled or unsupported adapter)",
        reason, cfg["channel_id"],
    )
    return False


async def rename_on_shutdown(runner: Any) -> None:
    """Rename the configured channel to the restarting-N-agents label.

    Exempt from the min-interval cooldown — the drain rename is one-shot
    and must land — but a successful edit still stamps the shared
    last-edit timestamp.
    """
    cfg = _config_for(runner)
    if not cfg:
        return
    agents = _agent_count(runner)
    label = _render_label(cfg["template"], agents)
    await _apply_label(
        runner, cfg, label, agents=agents, reason="drain",
        enforce_interval=False,
    )


async def restore_on_startup(runner: Any) -> None:
    """Restore the configured channel to the idle agents-N label after boot.

    Exempt from the min-interval cooldown, but a successful edit still
    stamps the shared last-edit timestamp.
    """
    await refresh_idle_name(runner, reason="boot", enforce_interval=False)


async def refresh_idle_name(
    runner: Any, *, reason: str = "idle", enforce_interval: bool = True
) -> None:
    """Set the idle ``agents-N`` label when the gateway is not draining.

    Subject to the min-interval cooldown unless ``enforce_interval`` is
    False (the boot path defers to whatever a drain rename just did).
    """
    if _is_draining(runner):
        return
    cfg = _config_for(runner)
    if not cfg:
        return
    agents = _agent_count(runner)
    label = _render_label(
        cfg["idle_template"], agents, fallback=DEFAULT_IDLE_TEMPLATE
    )
    await _apply_label(
        runner, cfg, label, agents=agents, reason=reason,
        enforce_interval=enforce_interval,
    )


def schedule_idle_refresh(runner: Any) -> None:
    """Best-effort schedule of an idle rename from a sync turn boundary.

    Coalesces bursts: if a refresh is already queued, just mark dirty so
    the in-flight task re-reads the count once more. The task waits out
    any remaining min-interval cooldown before each edit, so a refresh
    requested inside the cooldown is deferred until the window opens and
    then applies the newest count rather than being dropped.
    """
    if _is_draining(runner) or not _config_for(runner):
        return
    pending: Optional[asyncio.Task] = getattr(
        runner, "_idle_channel_rename_task", None
    )
    if pending is not None and not pending.done():
        runner._idle_channel_rename_dirty = True
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            while True:
                cfg = _config_for(runner)
                if cfg:
                    await _wait_for_rename_window(runner, cfg)
                # Clear dirty BEFORE applying: a refresh marked while the
                # task was waiting out the cooldown is satisfied by this
                # edit, and only requests arriving during the edit itself
                # earn one more spaced lap.
                runner._idle_channel_rename_dirty = False
                await refresh_idle_name(runner)
                if not getattr(runner, "_idle_channel_rename_dirty", False):
                    break
        except Exception:
            logger.debug(
                "[restart-channel-rename] idle refresh failed", exc_info=True
            )

    runner._idle_channel_rename_dirty = False
    runner._idle_channel_rename_task = loop.create_task(_run())
