"""Discord Guests — private lounge channels for invited guests.

Registers one action-based tool, ``discord_guests``, that provisions a private
lounge channel under the Lounges category when a guest (a bot or a friend) is
invited. Only that member — plus the people who already see Lounges, i.e. the
owner and bots with admin — can view the lounge. @everyone stays view-denied
everywhere. Access is per-channel overwrites only; nothing beyond the channel
itself is ever created or assigned.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

_DISCORD_API_BASE = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"

_ALLOWED_ACTIONS = {"setup", "add", "remove", "list"}

# Discord permission bits (api docs: Permissions > Flags).
_PERMISSION_ADMINISTRATOR = 1 << 3
_PERMISSION_VIEW_CHANNEL = 1 << 10

# Everything a guest is granted on their lounge — and the ceiling: nothing
# administrative (no ADMINISTRATOR, MANAGE_*, BAN, KICK, MENTION_EVERYONE)
# may ever appear in an allow mask this plugin writes.
_PERMISSION_ADD_REACTIONS = 1 << 6
_PERMISSION_SEND_MESSAGES = 1 << 11
_PERMISSION_EMBED_LINKS = 1 << 14
_PERMISSION_ATTACH_FILES = 1 << 15
_PERMISSION_READ_MESSAGE_HISTORY = 1 << 16
_PERMISSION_SEND_MESSAGES_IN_THREADS = 1 << 38
_GUEST_ALLOW = (
    _PERMISSION_VIEW_CHANNEL
    | _PERMISSION_SEND_MESSAGES
    | _PERMISSION_READ_MESSAGE_HISTORY
    | _PERMISSION_ADD_REACTIONS
    | _PERMISSION_EMBED_LINKS
    | _PERMISSION_ATTACH_FILES
    | _PERMISSION_SEND_MESSAGES_IN_THREADS
)

# REST write pacing: at least 0.3s between writes, and 429 retry_after is
# always honoured before a retry.
_WRITE_PACE_SECONDS = 0.3
_MAX_RATE_LIMIT_RETRIES = 3
_MAX_RETRY_AFTER_SECONDS = 60.0

# The lounge-parent category is auto-resolved by name, canonical first: a
# category named Lounges (the home-server template's name) wins, with the
# pre-rename Chat still honoured as a legacy fallback. Both are matched
# case-insensitively, and the canonical name wins regardless of channel order.
_LOUNGES_CATEGORY_NAME = "lounges"
_LEGACY_CATEGORY_NAME = "chat"
_DEFAULT_HOST_SLUG = "agent"  # last-resort fallback only
_LOUNGE_SUFFIX = "lounge"
_MAX_SLUG_LEN = 80
_CHANNEL_TYPE_GUILD_TEXT = 0
_CHANNEL_TYPE_GUILD_CATEGORY = 4
_OVERWRITE_TYPE_ROLE = 0
_OVERWRITE_TYPE_MEMBER = 1

DISCORD_GUESTS_SCHEMA = {
    "name": "discord_guests",
    "description": (
        "Manage guest lounges on Discord: adding a guest (bot or friend) "
        "auto-creates a private text channel named #<guest>-<host>-lounge under "
        "the Lounges category, visible only to that guest plus whoever already "
        "sees Lounges (the owner and bots with admin). @everyone stays "
        "view-denied everywhere. Use action='setup' once to pin the guild and "
        "Lounges category "
        "(and, on first setup, deny @everyone view on every category and "
        "top-level channel), action='add' to invite a guest, action='remove' to "
        "revoke a guest's access (the lounge and its history are kept), and "
        "action='list' to see current guests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALLOWED_ACTIONS),
                "description": "Operation to perform. Defaults to list.",
            },
            "user_id": {
                "type": "string",
                "description": (
                    "Discord user ID of the guest (add/remove). Takes "
                    "precedence over member."
                ),
            },
            "member": {
                "type": "string",
                "description": (
                    "Guest lookup by display-name/username prefix via guild "
                    "member search (add/remove) — used when user_id is not "
                    "given."
                ),
            },
            "host": {
                "type": "string",
                "description": (
                    "Host slug override for the lounge name — without it the "
                    "host comes from plugin settings (host_slug) or the bot's "
                    "own display name. The channel is named "
                    "#<guest>-<host>-lounge, or #<guest>-lounge when the "
                    "guest and host slugs match (add action)."
                ),
            },
            "guild_id": {
                "type": "string",
                "description": (
                    "Discord guild (server) ID. Defaults to the saved one, else "
                    "the only guild when the bot is in exactly one; an "
                    "ambiguous bot is an error."
                ),
            },
            "chat_category_id": {
                "type": "string",
                "description": (
                    "Explicit Lounges category ID (setup action). Without it "
                    "the category named Lounges is resolved "
                    "case-insensitively, with the legacy name Chat as a "
                    "fallback."
                ),
            },
            "lockdown": {
                "type": "boolean",
                "description": (
                    "Deny @everyone view on every category and top-level "
                    "channel (setup action). Defaults to true on first setup "
                    "only; later setups are a no-op unless explicitly set."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _env_path() -> Path:
    return get_hermes_home() / ".env"


def _state_path() -> Path:
    return get_hermes_home() / "discord_guests" / "state.json"


def _parse_token_line(line: str) -> str:
    value = line.split("=", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _read_discord_token() -> str:
    try:
        with _env_path().open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return _parse_token_line(line)
    except OSError:
        pass
    return ""


def _empty_state() -> Dict[str, Any]:
    return {
        "guild_id": "",
        "chat_category_id": "",
        "guests": [],
    }


def _load_state() -> Dict[str, Any]:
    try:
        with _state_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        guests_raw = data.get("guests", [])
        guests = []
        if isinstance(guests_raw, list):
            for entry in guests_raw:
                if not isinstance(entry, dict):
                    continue
                user_id = str(entry.get("user_id") or "")
                if not user_id:
                    continue
                guests.append(
                    {
                        "user_id": user_id,
                        "name": str(entry.get("name") or ""),
                        "channel_id": str(entry.get("channel_id") or ""),
                    }
                )
        return {
            "guild_id": str(data.get("guild_id") or ""),
            "chat_category_id": str(data.get("chat_category_id") or ""),
            "guests": guests,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_state()


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "guild_id": str(state.get("guild_id") or ""),
        "chat_category_id": str(state.get("chat_category_id") or ""),
        "guests": [
            {
                "user_id": str(guest.get("user_id") or ""),
                "name": str(guest.get("name") or ""),
                "channel_id": str(guest.get("channel_id") or ""),
            }
            for guest in state.get("guests", [])
            if isinstance(guest, dict) and guest.get("user_id")
        ],
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


_last_write_at = 0.0  # time.monotonic() of the last REST write


def _pace_write() -> None:
    """Keep at least _WRITE_PACE_SECONDS between REST writes."""
    global _last_write_at
    elapsed = time.monotonic() - _last_write_at
    remaining = _WRITE_PACE_SECONDS - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_write_at = time.monotonic()


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float:
    """Extract retry_after from a 429 body, clamped to a sane ceiling."""
    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
        seconds = float(payload.get("retry_after") or 0)
    except Exception:
        seconds = 0.0
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


def _discord_request(token: str, method: str, url: str, body: Any = None) -> Dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    is_write = method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    rate_limit_retries = 0
    while True:
        if is_write:
            _pace_write()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
            if not raw:
                return {}
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
            return {"data": payload}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and rate_limit_retries < _MAX_RATE_LIMIT_RETRIES:
                rate_limit_retries += 1
                time.sleep(_retry_after_seconds(exc))
                continue
            raise


def _resolve_guild_id(token: str, guild_id: str) -> Dict[str, Any]:
    """guild_id arg, else saved state, else single-guild bot; ambiguity errors."""
    if guild_id:
        return {"success": True, "guild_id": guild_id}

    state = _load_state()
    if state["guild_id"]:
        return {"success": True, "guild_id": state["guild_id"]}

    guilds_payload = _discord_request(
        token,
        "GET",
        f"{_DISCORD_API_BASE}/users/@me/guilds",
    )
    if isinstance(guilds_payload, list):
        guilds = guilds_payload
    else:
        guilds = guilds_payload.get("data") or []

    if not guilds:
        return {"success": False, "error": "bot is not in any guild"}

    if len(guilds) > 1:
        return {
            "success": False,
            "error": "bot is in multiple guilds; pass guild_id",
            "guilds": [
                {"id": str(g.get("id", "")), "name": str(g.get("name", ""))}
                for g in guilds
                if isinstance(g, dict)
            ],
        }

    return {"success": True, "guild_id": str(guilds[0].get("id", ""))}


def _slugify(name: str) -> str:
    """lowercase, non-alnum → hyphen, hyphens collapsed, trimmed, max 80."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:_MAX_SLUG_LEN].rstrip("-")


def _member_display_name(member: Dict[str, Any]) -> str:
    """The member's display name: nick, else global name, else username."""
    user = member.get("user") or {}
    for candidate in (
        member.get("nick"),
        user.get("global_name"),
        user.get("username"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return str(user.get("id") or "")


def _host_slug_from_settings() -> str:
    """``plugins.entries.discord_guests.settings.host_slug`` from config.yaml.

    Read the same way ``hermes_starts`` reads its settings block. Any failure
    (no config, missing block, import error) is simply "not set" — host
    resolution then falls through to the bot's own display name.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return ""
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("discord_guests") or {}
    return str((entry.get("settings") or {}).get("host_slug") or "").strip()


def _fetch_bot_user(token: str) -> Dict[str, Any]:
    """GET /users/@me — the bot's own user object; {} when unavailable."""
    try:
        return _discord_request(token, "GET", f"{_DISCORD_API_BASE}/users/@me")
    except Exception:
        return {}


def _bot_user_id(bot_user: Optional[Dict[str, Any]]) -> str:
    return str((bot_user or {}).get("id") or "")


def _resolve_host_slug(
    token: str,
    guild_id: str,
    host_override: str,
    bot_user: Optional[Dict[str, Any]] = None,
) -> str:
    """The host part of the lounge name — first match wins:

    1. ``host_override`` (the per-call ``host`` arg)
    2. ``host_slug`` from plugin settings
    3. the bot's own display name in the guild, slugified: GET /users/@me
       for the bot id, GET /guilds/{gid}/members/{bot_id} for the nick,
       then nick, else global name, else username
    4. the literal fallback "agent"

    The API is only hit when the first two come up empty. ``bot_user`` is a
    /users/@me payload the caller already fetched, if any. Discord answers
    /guilds/{gid}/members/@me with 400 — that path is never called.
    """
    slug = _slugify(host_override) or _slugify(_host_slug_from_settings())
    if slug:
        return slug

    bot_id = _bot_user_id(bot_user if bot_user is not None else _fetch_bot_user(token))
    member: Dict[str, Any] = {}
    if bot_id:
        try:
            member = _discord_request(
                token,
                "GET",
                f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/{bot_id}",
            )
        except Exception:
            member = {}
    return _slugify(_member_display_name(member)) or _DEFAULT_HOST_SLUG


def _lounge_channel_name(guest_slug: str, host: str) -> str:
    host_slug = _slugify(host) or _DEFAULT_HOST_SLUG
    if guest_slug == host_slug:
        return f"{guest_slug}-{_LOUNGE_SUFFIX}"
    return f"{guest_slug}-{host_slug}-{_LOUNGE_SUFFIX}"


def _fetch_guild_channels(token: str, guild_id: str) -> List[Dict[str, Any]]:
    payload = _discord_request(
        token,
        "GET",
        f"{_DISCORD_API_BASE}/guilds/{guild_id}/channels",
    )
    if isinstance(payload, list):
        return [ch for ch in payload if isinstance(ch, dict)]
    return [ch for ch in (payload.get("data") or []) if isinstance(ch, dict)]


def _find_chat_category(
    channels: List[Dict[str, Any]],
    *,
    chat_category_id: str,
    guild_id: str,
) -> str:
    """Explicit id wins; else Lounges, else the legacy Chat.

    Names are tried in priority order over the whole channel list, so a
    canonical Lounges category beats a legacy Chat one no matter which comes
    first in the list. Both match case-insensitively.
    """
    if chat_category_id:
        return chat_category_id
    for name in (_LOUNGES_CATEGORY_NAME, _LEGACY_CATEGORY_NAME):
        for channel in channels:
            if channel.get("type") == _CHANNEL_TYPE_GUILD_CATEGORY:
                if str(channel.get("name") or "").strip().lower() == name:
                    return str(channel.get("id") or "")
    return ""


def _lockdown_targets(channels: List[Dict[str, Any]]) -> List[str]:
    """Every category and every parentless channel — children inherit."""
    targets: List[str] = []
    for channel in channels:
        if channel.get("type") == _CHANNEL_TYPE_GUILD_CATEGORY or channel.get("parent_id") is None:
            channel_id = str(channel.get("id") or "")
            if channel_id:
                targets.append(channel_id)
    return targets


def _put_everyone_deny_view(token: str, channel_id: str, guild_id: str) -> None:
    _discord_request(
        token,
        "PUT",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/permissions/{guild_id}",
        {
            "id": guild_id,
            "type": _OVERWRITE_TYPE_ROLE,
            "allow": 0,
            "deny": _PERMISSION_VIEW_CHANNEL,
        },
    )


def _put_member_allow(token: str, channel_id: str, user_id: str) -> None:
    _discord_request(
        token,
        "PUT",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/permissions/{user_id}",
        {
            "id": user_id,
            "type": _OVERWRITE_TYPE_MEMBER,
            "allow": _GUEST_ALLOW,
            "deny": 0,
        },
    )


def _delete_member_overwrite(token: str, channel_id: str, user_id: str) -> None:
    _discord_request(
        token,
        "DELETE",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/permissions/{user_id}",
    )


def _fetch_member(
    token: str,
    guild_id: str,
    *,
    user_id: str,
    member_prefix: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the guest member by user_id, else by name-prefix search.

    Returns (member, error). The member object carries user/roles/nick as the
    Discord API returns them.
    """
    if user_id:
        member = _discord_request(
            token,
            "GET",
            f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}",
        )
        resolved_id = str(member.get("user", {}).get("id") or "")
        if not resolved_id:
            return None, f"member {user_id} not found in guild"
        return member, None

    if member_prefix:
        query = urllib.parse.urlencode({"query": member_prefix})
        payload = _discord_request(
            token,
            "GET",
            f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/search?{query}",
        )
        if isinstance(payload, list):
            matches = payload
        else:
            matches = payload.get("data") or []
        matches = [m for m in matches if isinstance(m, dict)]
        if not matches:
            return None, f"no guild member matches {member_prefix!r}"
        return matches[0], None

    return None, "member required: pass user_id or member (name prefix)"


def _member_is_admin(
    token: str,
    guild_id: str,
    member: Dict[str, Any],
) -> bool:
    """True when any of the member's existing guild roles grants ADMINISTRATOR."""
    payload = _discord_request(
        token,
        "GET",
        f"{_DISCORD_API_BASE}/guilds/{guild_id}/roles",
    )
    if isinstance(payload, list):
        roles = payload
    else:
        roles = payload.get("data") or []

    member_role_ids = {str(rid) for rid in (member.get("roles") or [])}
    member_role_ids.add(guild_id)  # @everyone always applies
    for role in roles:
        if not isinstance(role, dict):
            continue
        if str(role.get("id") or "") not in member_role_ids:
            continue
        try:
            permissions = int(str(role.get("permissions") or "0"))
        except ValueError:
            continue
        if permissions & _PERMISSION_ADMINISTRATOR:
            return True
    return False


def _handle_setup(args: Dict[str, Any], token: str) -> str:
    guild_id = str(args.get("guild_id") or "").strip()
    chat_category_id = str(args.get("chat_category_id") or "").strip()

    state = _load_state()
    first_setup = not (state["guild_id"] and state["chat_category_id"])

    resolved = _resolve_guild_id(token, guild_id)
    if not resolved.get("success"):
        return json.dumps(resolved)
    resolved_guild_id = str(resolved["guild_id"])

    # Lockdown defaults to true on first setup only; afterwards it is a no-op
    # unless explicitly requested.
    lockdown_arg = args.get("lockdown")
    run_lockdown = first_setup if lockdown_arg is None else bool(lockdown_arg)

    if not chat_category_id:
        channels = _fetch_guild_channels(token, resolved_guild_id)
        chat_category_id = _find_chat_category(
            channels,
            chat_category_id="",
            guild_id=resolved_guild_id,
        )
        if not chat_category_id:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "no Lounges category found; pass chat_category_id or "
                        "create a category named Lounges"
                    ),
                }
            )
        lockdown_channels = channels
    else:
        lockdown_channels = (
            _fetch_guild_channels(token, resolved_guild_id) if run_lockdown else []
        )

    denied: List[str] = []
    if run_lockdown:
        for target_id in _lockdown_targets(lockdown_channels):
            _put_everyone_deny_view(token, target_id, resolved_guild_id)
            denied.append(target_id)

    state["guild_id"] = resolved_guild_id
    state["chat_category_id"] = chat_category_id
    _save_state(state)

    result: Dict[str, Any] = {
        "success": True,
        "action": "setup",
        "guild_id": resolved_guild_id,
        "chat_category_id": chat_category_id,
        "lockdown": run_lockdown,
    }
    if run_lockdown:
        result["everyone_denied_view_on"] = denied
    return json.dumps(result)


def _resolve_guest(
    state: Dict[str, Any],
    *,
    user_id: str,
    member_prefix: str,
) -> Optional[Dict[str, Any]]:
    """Find a saved guest entry by user_id or name-prefix match."""
    if user_id:
        for guest in state["guests"]:
            if guest["user_id"] == user_id:
                return guest
        return None
    prefix = member_prefix.strip().lower()
    if prefix:
        for guest in state["guests"]:
            if guest["name"].strip().lower().startswith(prefix):
                return guest
    return None


def _handle_add(args: Dict[str, Any], token: str) -> str:
    guild_id = str(args.get("guild_id") or "").strip()
    user_id = str(args.get("user_id") or "").strip()
    member_prefix = str(args.get("member") or "").strip()
    host_override = str(args.get("host") or "").strip()

    if not user_id and not member_prefix:
        return json.dumps(
            {"success": False, "error": "member required: pass user_id or member (name prefix)"}
        )

    resolved = _resolve_guild_id(token, guild_id)
    if not resolved.get("success"):
        return json.dumps(resolved)
    resolved_guild_id = str(resolved["guild_id"])

    member, member_error = _fetch_member(
        token, resolved_guild_id, user_id=user_id, member_prefix=member_prefix
    )
    if member is None:
        return json.dumps({"success": False, "error": member_error})

    guest_user_id = str(member.get("user", {}).get("id") or "")
    if not guest_user_id:
        return json.dumps({"success": False, "error": "member resolved without a user id"})

    # Anyone holding ADMINISTRATOR is refused — except the host bot itself,
    # which carries admin yet is its own guest like anyone else.
    bot_user: Optional[Dict[str, Any]] = None
    if _member_is_admin(token, resolved_guild_id, member):
        bot_user = _fetch_bot_user(token)
        if guest_user_id != _bot_user_id(bot_user):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"member {_member_display_name(member)} has ADMINISTRATOR; "
                        "they already see everything and are not added as a guest"
                    ),
                }
            )

    state = _load_state()
    state["guild_id"] = state["guild_id"] or resolved_guild_id

    channels = _fetch_guild_channels(token, resolved_guild_id)
    chat_category_id = _find_chat_category(
        channels,
        chat_category_id=state["chat_category_id"],
        guild_id=resolved_guild_id,
    )
    if not chat_category_id:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "no Lounges category found; run action='setup' or create a "
                    "category named Lounges"
                ),
            }
        )

    display_name = _member_display_name(member)
    guest_slug = _slugify(display_name)
    if not guest_slug:
        guest_slug = guest_user_id
    host_slug = _resolve_host_slug(token, resolved_guild_id, host_override, bot_user)
    channel_name = _lounge_channel_name(guest_slug, host_slug)

    # Idempotent: reuse a lounge of this exact name under the category when
    # present.
    channel_id = ""
    for channel in channels:
        if str(channel.get("parent_id") or "") != chat_category_id:
            continue
        if str(channel.get("name") or "").strip().lower() == channel_name:
            channel_id = str(channel.get("id") or "")
            break

    created = False
    if not channel_id:
        created_channel = _discord_request(
            token,
            "POST",
            f"{_DISCORD_API_BASE}/guilds/{resolved_guild_id}/channels",
            {
                "name": channel_name,
                "type": _CHANNEL_TYPE_GUILD_TEXT,
                "parent_id": chat_category_id,
            },
        )
        channel_id = str(created_channel.get("id") or "")
        if not channel_id:
            return json.dumps(
                {"success": False, "error": "lounge creation did not return an id"}
            )
        created = True

    _put_member_allow(token, channel_id, guest_user_id)
    # Belt-and-braces: the lounge stays private even if category perms drift.
    _put_everyone_deny_view(token, channel_id, resolved_guild_id)

    state["chat_category_id"] = chat_category_id
    guests = [g for g in state["guests"] if g["user_id"] != guest_user_id]
    guests.append(
        {
            "user_id": guest_user_id,
            "name": display_name,
            "channel_id": channel_id,
        }
    )
    state["guests"] = guests
    _save_state(state)

    result: Dict[str, Any] = {
        "success": True,
        "action": "add",
        "guild_id": resolved_guild_id,
        "user_id": guest_user_id,
        "name": display_name,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "chat_category_id": chat_category_id,
        "created": created,
    }
    if not user_id:
        result["matched_by"] = "name prefix"
    return json.dumps(result)


def _handle_remove(args: Dict[str, Any], token: str) -> str:
    guild_id = str(args.get("guild_id") or "").strip()
    user_id = str(args.get("user_id") or "").strip()
    member_prefix = str(args.get("member") or "").strip()

    if not user_id and not member_prefix:
        return json.dumps(
            {"success": False, "error": "member required: pass user_id or member (name prefix)"}
        )

    resolved = _resolve_guild_id(token, guild_id)
    if not resolved.get("success"):
        return json.dumps(resolved)
    resolved_guild_id = str(resolved["guild_id"])

    state = _load_state()
    guest = _resolve_guest(state, user_id=user_id, member_prefix=member_prefix)

    if guest is None:
        # Not a tracked guest — resolve the member so the overwrite can still
        # be removed by id, locating the lounge by its conventional name.
        member, member_error = _fetch_member(
            token, resolved_guild_id, user_id=user_id, member_prefix=member_prefix
        )
        if member is None:
            return json.dumps({"success": False, "error": member_error})
        guest_user_id = str(member.get("user", {}).get("id") or "")
        display_name = _member_display_name(member)
        host_slug = _resolve_host_slug(
            token, resolved_guild_id, str(args.get("host") or "").strip()
        )
        channel_name = _lounge_channel_name(_slugify(display_name) or guest_user_id, host_slug)
        channels = _fetch_guild_channels(token, resolved_guild_id)
        chat_category_id = _find_chat_category(
            channels,
            chat_category_id=state["chat_category_id"],
            guild_id=resolved_guild_id,
        )
        channel_id = ""
        for channel in channels:
            if str(channel.get("parent_id") or "") != chat_category_id:
                continue
            if str(channel.get("name") or "").strip().lower() == channel_name:
                channel_id = str(channel.get("id") or "")
                break
        if not channel_id:
            return json.dumps(
                {"success": False, "error": f"guest {display_name} has no lounge to remove"}
            )
        guest = {"user_id": guest_user_id, "name": display_name, "channel_id": channel_id}

    # Only the member overwrite comes off — the lounge (and its history) stays.
    _delete_member_overwrite(token, guest["channel_id"], guest["user_id"])

    state["guests"] = [
        g for g in state["guests"] if g["user_id"] != guest["user_id"]
    ]
    _save_state(state)

    return json.dumps(
        {
            "success": True,
            "action": "remove",
            "guild_id": resolved_guild_id,
            "user_id": guest["user_id"],
            "name": guest["name"],
            "channel_id": guest["channel_id"],
            "channel_kept": True,
        }
    )


def _handle_list(args: Dict[str, Any], token: str) -> str:
    guild_id = str(args.get("guild_id") or "").strip()

    state = _load_state()
    resolved = _resolve_guild_id(token, guild_id)
    if not resolved.get("success"):
        return json.dumps(resolved)
    resolved_guild_id = str(resolved["guild_id"])

    guests = state["guests"]
    live_ids = set()
    if guests:
        for channel in _fetch_guild_channels(token, resolved_guild_id):
            live_ids.add(str(channel.get("id") or ""))

    return json.dumps(
        {
            "success": True,
            "action": "list",
            "guild_id": resolved_guild_id,
            "chat_category_id": state["chat_category_id"],
            "guests": [
                {
                    "user_id": guest["user_id"],
                    "name": guest["name"],
                    "channel_id": guest["channel_id"],
                    "channel_exists": guest["channel_id"] in live_ids,
                }
                for guest in guests
            ],
        }
    )


def handle_discord_guests(args: dict, **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "list").strip().lower()
        if action not in _ALLOWED_ACTIONS:
            return json.dumps(
                {"success": False, "error": f"action must be one of {sorted(_ALLOWED_ACTIONS)}"}
            )

        token = _read_discord_token()
        if not token:
            return json.dumps({"success": False, "error": "Discord bot token not configured"})

        if action == "setup":
            return _handle_setup(args, token)
        if action == "add":
            return _handle_add(args, token)
        if action == "remove":
            return _handle_remove(args, token)
        return _handle_list(args, token)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_requirements() -> bool:
    try:
        path = _env_path()
        if not path.is_file():
            return False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return bool(_parse_token_line(line))
        return False
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(
        name="discord_guests",
        toolset="discord_guests",
        schema=DISCORD_GUESTS_SCHEMA,
        handler=handle_discord_guests,
        check_fn=check_requirements,
        emoji="🪪",
    )
