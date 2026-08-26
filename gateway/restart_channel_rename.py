"""Channel rename for live agent count and drain progress.

When idle, a configured Discord channel is named ``agents-N`` (N =
currently running agents). While draining before a shutdown, it is
renamed once to ``restarting-N-agents``. Boot restores the idle
``agents-N`` label.

Discord rate-limits channel name edits to roughly 2 per 10 minutes per
channel. Idle updates fire only when the count actually changes, and
failed/throttled edits are ignored. Drain is still set-once + restore,
not a live tick-down.

Config (config.yaml):

    gateway:
      restart_channel_rename:
        platform: discord          # optional, default discord
        channel_id: "1541012892462223391"
        idle_template: "agents-{agents}"              # optional
        renamed_template: "restarting-{agents}-agents"  # optional

Everything is best-effort: failures log at debug/info and never affect
shutdown, startup, or turn sequencing.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "discord"
DEFAULT_BASE_NAME = "gateway-restarts"
DEFAULT_TEMPLATE = "restarting-{agents}-agents"
DEFAULT_IDLE_TEMPLATE = "agents-{agents}"


def parse_restart_channel_rename_config(raw: Any) -> Dict[str, str]:
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
    }


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


def _resolve_adapter(runner: Any, cfg: Dict[str, str]):
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


def _config_for(runner: Any) -> Dict[str, str]:
    return parse_restart_channel_rename_config(
        getattr(getattr(runner, "config", None), "restart_channel_rename", None)
    )


def _is_draining(runner: Any) -> bool:
    return bool(
        getattr(runner, "_draining", False)
        or getattr(runner, "_restart_requested", False)
    )


async def _apply_label(
    runner: Any, cfg: Dict[str, str], label: str, *, agents: int, reason: str
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
    if await _edit_channel_name(adapter, cfg["channel_id"], label):
        runner._restart_channel_rename_last = label
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
    """Rename the configured channel to the restarting-N-agents label."""
    cfg = _config_for(runner)
    if not cfg:
        return
    agents = _agent_count(runner)
    label = _render_label(cfg["template"], agents)
    await _apply_label(runner, cfg, label, agents=agents, reason="drain")


async def restore_on_startup(runner: Any) -> None:
    """Restore the configured channel to the idle agents-N label after boot."""
    await refresh_idle_name(runner, reason="boot")


async def refresh_idle_name(runner: Any, *, reason: str = "idle") -> None:
    """Set the idle ``agents-N`` label when the gateway is not draining."""
    if _is_draining(runner):
        return
    cfg = _config_for(runner)
    if not cfg:
        return
    agents = _agent_count(runner)
    label = _render_label(
        cfg["idle_template"], agents, fallback=DEFAULT_IDLE_TEMPLATE
    )
    await _apply_label(runner, cfg, label, agents=agents, reason=reason)


def schedule_idle_refresh(runner: Any) -> None:
    """Best-effort schedule of an idle rename from a sync turn boundary.

    Coalesces bursts: if a refresh is already queued, just mark dirty so
    the in-flight task re-reads the count once more.
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
                await refresh_idle_name(runner)
                if not getattr(runner, "_idle_channel_rename_dirty", False):
                    break
                runner._idle_channel_rename_dirty = False
        except Exception:
            logger.debug(
                "[restart-channel-rename] idle refresh failed", exc_info=True
            )

    runner._idle_channel_rename_dirty = False
    runner._idle_channel_rename_task = loop.create_task(_run())
