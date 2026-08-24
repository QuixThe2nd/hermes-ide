"""Restart-progress channel renaming (opt-in, Discord-style adapters).

While draining before a shutdown/restart, a configured channel is renamed
to e.g. ``restarting-4-agents`` where N is the number of agents still
running at drain start. Once the gateway finishes booting, the channel is
restored to its base name.

Discord rate-limits channel name edits to roughly 2 per 10 minutes per
channel, so this deliberately does NOT implement a live tick-down
counter: each restart cycle performs at most two edits (set + restore).
Restore runs on every completed boot, which also recovers a channel left
renamed after a crash mid-drain.

Config (config.yaml):

    gateway:
      restart_channel_rename:
        platform: discord          # optional, default discord
        channel_id: "1541012892462223391"
        base_name: gateway-restarts
        renamed_template: "restarting-{agents}-agents"  # optional

Everything is best-effort: failures log at debug/info and never affect
shutdown or startup sequencing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "discord"
DEFAULT_BASE_NAME = "gateway-restarts"
DEFAULT_TEMPLATE = "restarting-{agents}-agents"


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
    if not base_name:
        return {}
    return {
        "platform": platform,
        "channel_id": channel_id,
        "base_name": base_name,
        "template": template,
    }


def _render_label(template: str, agents: int) -> str:
    try:
        label = template.format(agents=agents)
    except (KeyError, IndexError, ValueError):
        logger.debug(
            "[restart-channel-rename] bad template %r; using default", template
        )
        label = DEFAULT_TEMPLATE.format(agents=agents)
    return label.strip() or DEFAULT_TEMPLATE.format(agents=agents)


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


async def rename_on_shutdown(runner: Any) -> None:
    """Rename the configured channel to the restarting-N-agents label."""
    cfg = parse_restart_channel_rename_config(
        getattr(getattr(runner, "config", None), "restart_channel_rename", None)
    )
    if not cfg:
        return
    try:
        agents = int(runner._running_agent_count())
    except Exception:
        agents = 0
    label = _render_label(cfg["template"], max(agents, 0))
    adapter = _resolve_adapter(runner, cfg)
    if adapter is None:
        logger.debug(
            "[restart-channel-rename] no %s adapter during shutdown rename",
            cfg["platform"],
        )
        return
    if await _edit_channel_name(adapter, cfg["channel_id"], label):
        logger.info(
            "[restart-channel-rename] %s channel %s renamed to %r "
            "(%d running agents)",
            cfg["platform"], cfg["channel_id"], label, agents,
        )
    else:
        logger.debug(
            "[restart-channel-rename] shutdown rename of %s did not apply "
            "(throttled or unsupported adapter)", cfg["channel_id"],
        )


async def restore_on_startup(runner: Any) -> None:
    """Restore the configured channel to its base name after boot."""
    cfg = parse_restart_channel_rename_config(
        getattr(getattr(runner, "config", None), "restart_channel_rename", None)
    )
    if not cfg:
        return
    adapter = _resolve_adapter(runner, cfg)
    if adapter is None:
        logger.debug(
            "[restart-channel-rename] no %s adapter during startup restore",
            cfg["platform"],
        )
        return
    if await _edit_channel_name(adapter, cfg["channel_id"], cfg["base_name"]):
        logger.info(
            "[restart-channel-rename] %s channel %s restored to %r",
            cfg["platform"], cfg["channel_id"], cfg["base_name"],
        )
    else:
        logger.debug(
            "[restart-channel-rename] startup restore of %s did not apply "
            "(throttled or unsupported adapter)", cfg["channel_id"],
        )
