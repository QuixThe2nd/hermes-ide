"""Channel directory -- cached map of reachable channels/contacts per platform.

Built on gateway startup, refreshed every 5 min, saved to ``<HERMES_HOME>/channel_directory.json``.
send_message reads it for action="list" and to resolve friendly channel names to IDs.
"""

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli.config import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Paths resolve lazily: a multiplexed gateway serves several profile homes from one
# process, so an import-time constant would pin every profile to whichever home imported
# first. These globals are explicit overrides (tests patch them); None = current home.
DIRECTORY_PATH: Optional[Path] = None
# User-maintained friendly-name overlay {"<platform>": {"<chat_id>": "<friendly name>"}},
# re-applied on every build AND load (hand-edits to the regenerated
# channel_directory.json don't survive); also lets a chat be pre-named before first traffic.
CHANNEL_ALIASES_PATH: Optional[Path] = None

# Platforms whose historical session origins must never become send targets.
_SKIP_SESSION_DISCOVERY = frozenset({"local", "api_server", "webhook"})


def _directory_path() -> Path:
    return DIRECTORY_PATH or get_hermes_home() / "channel_directory.json"


def _aliases_path() -> Path:
    return CHANNEL_ALIASES_PATH or get_hermes_home() / "channel_aliases.json"


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _load_json_dict(path: Path) -> Dict[str, Any]:
    """Read a JSON object from *path*; {} when missing, unreadable, or not a dict."""
    if not path.exists():
        return {}
    try:
        data = _read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_channel_aliases(platforms: Dict[str, Any]) -> None:
    """Overlay friendly names onto directory entries by chat_id, in place.

    An aliased id not yet discovered gets a placeholder entry so a freshly-created
    group is addressable by name before its first message.
    """
    for plat_name, id_map in _load_json_dict(_aliases_path()).items():
        if not isinstance(id_map, dict):
            continue
        entries = platforms.setdefault(plat_name, [])
        if not isinstance(entries, list):
            continue
        for chat_id, friendly in id_map.items():
            if not isinstance(friendly, str) or not friendly.strip():
                continue
            chat_id, friendly = str(chat_id), friendly.strip()
            matches = [e for e in entries if isinstance(e, dict) and e.get("id") == chat_id]
            for e in matches:
                e["name"] = friendly
            if not matches:
                entries.append({"id": chat_id, "name": friendly, "thread_id": None,
                                "type": "group" if chat_id.endswith("@g.us") else "dm"})


def _normalize_channel_query(value: str) -> str:
    return value.lstrip("#").strip().lower()


def _channel_target_name(platform_name: str, channel: Dict[str, Any]) -> str:
    """Human-facing target label for a channel entry."""
    name = channel["name"]
    if platform_name == "discord":
        return f"#{name}" if channel.get("guild") else name
    return f"{name} ({channel['type']})" if channel.get("type") else name


def _session_entry_id(origin: Dict[str, Any]) -> Optional[str]:
    chat_id = origin.get("chat_id")
    if not chat_id:
        return None
    return f"{chat_id}:{thread_id}" if (thread_id := origin.get("thread_id")) else str(chat_id)


def _session_entry_name(origin: Dict[str, Any]) -> str:
    base_name = origin.get("chat_name") or origin.get("user_name") or str(origin.get("chat_id"))
    if not (thread_id := origin.get("thread_id")):
        return base_name
    return f"{base_name} / {origin.get('chat_topic') or f'topic {thread_id}'}"


# --- Build / refresh -------------------------------------------------------

async def build_channel_directory(adapters: Dict[Any, Any]) -> Dict[str, Any]:
    """Build the directory from connected adapters + session data and persist it."""
    from gateway.config import Platform
    platforms: Dict[str, List[Dict[str, str]]] = {}
    for platform, adapter in adapters.items():
        try:
            list_channels = getattr(adapter, "list_channels", None)
            if callable(list_channels):
                platform_channels = await list_channels()
                if platform_channels is not None:
                    platforms[platform.value] = _normalize_adapter_channels(platform_channels)
                    continue
            if platform == Platform.DISCORD:
                platforms["discord"] = await asyncio.to_thread(_build_discord, adapter)
        except Exception as e:
            logger.warning("Channel directory: failed to build %s: %s", platform.value, e)
    # Platforms without channel enumeration get session-based discovery, but only when
    # connected in THIS gateway process: origins for disabled or decommissioned
    # platforms must not resurface as stale send targets.
    adapter_platform_names = {getattr(p, "value", str(p)) for p in adapters}
    async def _discover(plat_name: str) -> None:
        if plat_name in _SKIP_SESSION_DISCOVERY or plat_name in platforms or plat_name not in adapter_platform_names:
            return
        platforms[plat_name] = await asyncio.to_thread(_build_from_sessions, plat_name)
    for plat in Platform:
        await _discover(plat.value)
    # Plugin platforms are dynamic enum members missing from Platform.__members__.
    with contextlib.suppress(Exception):
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            await _discover(entry.name)
    _apply_channel_aliases(platforms)
    directory = {"updated_at": datetime.now().isoformat(), "platforms": platforms}
    try:
        await asyncio.to_thread(atomic_json_write, _directory_path(), directory)
    except Exception as e:
        logger.warning("Channel directory: failed to write: %s", e)
    return directory


def _build_discord(adapter) -> List[Dict[str, str]]:
    """Enumerate text + forum channels the Discord bot can see, plus session DMs."""
    channels = []
    client = getattr(adapter, "_client", None)
    if not client:
        return channels
    try:
        import discord as _discord  # noqa: F401 — SDK presence check
    except ImportError:
        return channels
    from gateway.platforms.helpers import is_discord_channel_obfuscated

    for guild in client.guilds:
        # Forum channels (type 15): creating a message auto-spawns a thread post.
        forums = getattr(guild, "forum_channels", None) or []
        for chs, ch_type in ((guild.text_channels, "channel"), (forums, "forum")):
            for ch in chs:
                # Obfuscated placeholders (no VIEW_CHANNEL) can never be posted to. #90154
                if is_discord_channel_obfuscated(ch):
                    continue
                channels.append({"id": str(ch.id), "name": ch.name, "guild": guild.name, "type": ch_type})
    # DM-capable users aren't reachable via guild enumeration; they come from sessions.
    channels.extend(_build_from_sessions("discord"))
    return channels


def _normalize_adapter_channels(raw_channels: Any) -> List[Dict[str, Any]]:
    """Validate and dedupe entries returned by an adapter's ``list_channels()`` hook."""
    channels: List[Dict[str, Any]] = []
    seen_ids = set()
    for raw in raw_channels if isinstance(raw_channels, list) else ():
        if not isinstance(raw, dict):
            continue
        channel_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or channel_id).strip()
        if not channel_id or not name or channel_id in seen_ids:
            continue
        entry: Dict[str, Any] = {"id": channel_id, "name": name, "type": str(raw.get("type") or "dm")}
        entry.update({key: str(raw[key]) for key in ("thread_id", "guild") if raw.get(key)})
        channels.append(entry)
        seen_ids.add(channel_id)
    return channels


def _build_from_sessions(platform_name: str) -> List[Dict[str, str]]:
    """Known channels/contacts from session origins: state.db first, sessions.json fallback (pre-migration).

    state.db is the primary source (#9006): gateway session rows persist origin_json.
    """
    return _build_from_sessions_db(platform_name) or _build_from_sessions_json(platform_name)


def _entries_from_origins(platform_name: str, source: str, origins_fn) -> List[Dict[str, Any]]:
    """Deduped entries for the (origin, chat_type) pairs from ``origins_fn()``; a mid-iteration
    failure keeps entries read so far."""
    entries: List[Dict[str, Any]] = []
    try:
        seen_ids = set()
        for origin, chat_type in origins_fn():
            entry_id = _session_entry_id(origin)
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append({
                "id": entry_id, "name": _session_entry_name(origin),
                "type": chat_type, "thread_id": origin.get("thread_id"),
            })
    except Exception as e:
        logger.debug("Channel directory: %s for %s: %s", source, platform_name, e)
    return entries


def _build_from_sessions_db(platform_name: str) -> List[Dict[str, str]]:
    """Pull channels/contacts from state.db gateway session rows."""
    def _origins() -> Iterable[Tuple[Dict[str, Any], Any]]:
        from hermes_state_registry import acquire, release_or_close
        db = acquire()
        try:
            lister = getattr(db, "list_gateway_sessions", None)
            if not callable(lister):
                return
            rows = lister(platform=platform_name, active_only=False)
        finally:
            release_or_close(db)
        for row in rows:
            origin = None
            with contextlib.suppress(TypeError, ValueError):
                origin = json.loads(row["origin_json"]) if row.get("origin_json") else None
            if not isinstance(origin, dict) or not origin:
                origin = {"chat_id": row.get("chat_id"), "thread_id": row.get("thread_id"), "chat_name": row.get("display_name")}
            yield origin, row.get("chat_type") or "dm"
    return _entries_from_origins(platform_name, "state.db session read failed", _origins)


def _build_from_sessions_json(platform_name: str) -> List[Dict[str, str]]:
    """Legacy fallback: pull channels/contacts from sessions.json origin data."""
    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return []
    def _origins() -> Iterable[Tuple[Dict[str, Any], Any]]:
        for _key, session in _read_json(sessions_path).items():
            # Keys starting with "_" (e.g. the gateway's "_README") are metadata sentinels.
            if str(_key).startswith("_") or not isinstance(session, dict):
                continue
            origin = session.get("origin") or {}
            if origin.get("platform") == platform_name:
                yield origin, session.get("chat_type", "dm")
    return _entries_from_origins(platform_name, "failed to read sessions", _origins)


# --- Read / resolve --------------------------------------------------------

def load_directory() -> Dict[str, Any]:
    """Load the cached directory from disk, with aliases re-applied on read."""
    directory_path = _directory_path()
    if directory_path.exists():
        with contextlib.suppress(Exception):
            data = _read_json(directory_path)
            # Aliases apply on read too, so new names take effect between timed rebuilds.
            _apply_channel_aliases(data.setdefault("platforms", {}))
            return data
    base = {"updated_at": None, "platforms": {}}
    _apply_channel_aliases(base["platforms"])
    return base


def lookup_channel_type(platform_name: str, chat_id: str) -> Optional[str]:
    """Channel ``type`` string (e.g. ``"channel"``, ``"forum"``) for *chat_id*, or None if unknown."""
    channels = load_directory().get("platforms", {}).get(platform_name, [])
    return next((ch.get("type") for ch in channels if ch.get("id") == chat_id), None)


def resolve_channel_name(platform_name: str, name: str) -> Optional[str]:
    """Resolve a friendly channel name (e.g. "bot-home", "#bot-home", "GuildName/bot-home",
    Slack "#engineering") to an ID; case-insensitive, first match wins."""
    channels = load_directory().get("platforms", {}).get(platform_name, [])
    if not channels:
        return None
    # 0. Exact ID match — case-sensitive, no normalization, so raw platform IDs (e.g. Slack
    # "C0B0QV5434G") work even when _parse_target_ref's format guard didn't recognize them.
    raw = name.strip()
    for ch in channels:
        if ch.get("id") == raw:
            return ch["id"]
    query = _normalize_channel_query(name)
    # 1. Exact name match, including the display labels shown by send_message(action="list")
    for ch in channels:
        if query in (_normalize_channel_query(ch["name"]), _normalize_channel_query(_channel_target_name(platform_name, ch))):
            return ch["id"]
    # 2. Guild-qualified match for Discord ("GuildName/channel")
    if "/" in query:
        guild_part, ch_part = query.rsplit("/", 1)
        for ch in channels:
            guild = ch.get("guild", "").strip().lower()
            if guild == guild_part and _normalize_channel_query(ch["name"]) == ch_part:
                return ch["id"]
    # 3. Partial prefix match (only if unambiguous)
    matches = [ch for ch in channels if _normalize_channel_query(ch["name"]).startswith(query)]
    return matches[0]["id"] if len(matches) == 1 else None


def format_directory_for_display(platforms: Optional[Dict[str, Any]] = None) -> str:
    """Format the channel directory as a human-readable list for the model.

    ``platforms`` overrides the on-disk directory (``hermes send --list`` merges in
    configured-but-undiscovered platforms); an empty channel list renders a "(no channels
    discovered yet)" hint because the platform is still a valid send target.
    """
    if platforms is None:
        platforms = load_directory().get("platforms", {})
    if not platforms:
        return "No messaging platforms connected or no channels discovered yet."
    lines = ["Available messaging targets:\n"]
    for plat_name, channels in sorted(platforms.items()):
        if not channels:
            lines.append(f"{plat_name.title()}:")
            lines.append(
                f"  (no channels discovered yet — send directly with "
                f"{plat_name}:<chat_id>)"
            )
        elif plat_name == "discord":
            # Group Discord channels by guild (sorted by name); DMs last, in discovery order.
            guilds: Dict[str, List] = {}
            dms: List = []
            for ch in channels:
                (guilds.setdefault(ch["guild"], []) if ch.get("guild") else dms).append(ch)
            groups = [(f"Discord ({g}):", sorted(chs, key=lambda c: c["name"])) for g, chs in sorted(guilds.items())]
            if dms:
                groups.append(("Discord (DMs):", dms))
            for header, group in groups:
                lines.append(header)
                lines.extend(f"  discord:{_channel_target_name(plat_name, ch)}" for ch in group)
        else:
            lines.append(f"{plat_name.title()}:")
            lines.extend(f"  {plat_name}:{_channel_target_name(plat_name, ch)}" for ch in channels)
        lines.append("")
    lines.append('Use these as the "target" parameter when sending.')
    return "\n".join(lines)
