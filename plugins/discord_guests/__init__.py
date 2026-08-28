"""Discord Guests — a Guest role for invited bots and friends.

Registers one action-based tool, ``discord_guests``, that provisions a
zero-permission ``Guest`` role on a server where ``@everyone`` cannot
VIEW_CHANNEL, then hands named members view/send access to chosen categories
and channels — never moderation or administration.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

_DISCORD_API_BASE = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"

ROLE_NAME = "Guest"

# REST-only, no discord.py and no Gateway — so writes are paced by hand and
# 429s are honoured rather than hammered through.
_MIN_WRITE_INTERVAL = 0.3
_MAX_ATTEMPTS = 4
_MAX_RETRY_AFTER = 15.0

_ALLOWED_ACTIONS = {"setup", "add", "remove", "grant", "revoke", "list"}

# Permission bits (Discord API).
_PERM_KICK_MEMBERS = 1 << 1
_PERM_BAN_MEMBERS = 1 << 2
_PERM_ADMINISTRATOR = 1 << 3
_PERM_MANAGE_CHANNELS = 1 << 4
_PERM_MANAGE_GUILD = 1 << 5
_PERM_ADD_REACTIONS = 1 << 6
_PERM_VIEW_CHANNEL = 1 << 10
_PERM_SEND_MESSAGES = 1 << 11
_PERM_EMBED_LINKS = 1 << 14
_PERM_ATTACH_FILES = 1 << 15
_PERM_READ_MESSAGE_HISTORY = 1 << 16
_PERM_MENTION_EVERYONE = 1 << 17
_PERM_CONNECT = 1 << 20
_PERM_SPEAK = 1 << 21
_PERM_MANAGE_ROLES = 1 << 28
_PERM_SEND_MESSAGES_IN_THREADS = 1 << 38

# Everything a Guest may ever be granted: read and participate, nothing else.
_GUEST_ALLOW = (
    _PERM_VIEW_CHANNEL
    | _PERM_SEND_MESSAGES
    | _PERM_READ_MESSAGE_HISTORY
    | _PERM_ADD_REACTIONS
    | _PERM_EMBED_LINKS
    | _PERM_ATTACH_FILES
    | _PERM_CONNECT
    | _PERM_SPEAK
    | _PERM_SEND_MESSAGES_IN_THREADS
)

# Bits that must never appear in a Guest allow mask, whatever the caller asks
# for. A role holding any of these is an operator, not a guest.
_FORBIDDEN_PERMS = (
    _PERM_ADMINISTRATOR
    | _PERM_MANAGE_GUILD
    | _PERM_MANAGE_ROLES
    | _PERM_MANAGE_CHANNELS
    | _PERM_BAN_MEMBERS
    | _PERM_KICK_MEMBERS
    | _PERM_MENTION_EVERYONE
)

_CHANNEL_CATEGORY = 4
_OVERWRITE_ROLE = 0

DISCORD_GUESTS_SCHEMA = {
    "name": "discord_guests",
    "description": (
        "Treat invited Discord bots and friends as Guests on a private server where "
        "@everyone cannot VIEW_CHANNEL. One action-based tool: action='setup' creates "
        "the zero-permission Guest role and (first time only, unless lockdown=true is "
        "passed again) denies VIEW_CHANNEL to @everyone on every category and top-level "
        "channel; action='add' gives a member the Guest role and optionally VIEW on named "
        "channels; action='grant' allows the Guest role on chosen categories/channels; "
        "action='revoke' removes those allows; action='remove' takes the role off a "
        "member; action='list' shows role, members, and allowed channels. Guests are "
        "never granted moderation or administration permissions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALLOWED_ACTIONS),
                "description": "Operation to perform.",
            },
            "guild_id": {
                "type": "string",
                "description": "Discord guild (server) ID, when the bot is in multiple guilds.",
            },
            "member": {
                "type": "string",
                "description": (
                    "Discord user ID, or a name prefix resolved via guild member search "
                    "(add and remove actions)."
                ),
            },
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Channel or category IDs, or names resolved case-insensitively "
                    "(add, grant, and revoke actions)."
                ),
            },
            "lockdown": {
                "type": "boolean",
                "description": (
                    "setup: deny VIEW_CHANNEL to @everyone on every category and every "
                    "top-level channel. Defaults to true on first setup and to false "
                    "afterwards, so pass true explicitly to re-run it."
                ),
            },
        },
        "required": ["action"],
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
    return {"guild_id": "", "role_id": ""}


def _load_state() -> Dict[str, Any]:
    try:
        with _state_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        return {
            "guild_id": str(data.get("guild_id") or ""),
            "role_id": str(data.get("role_id") or ""),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_state()


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "guild_id": str(state.get("guild_id") or ""),
        "role_id": str(state.get("role_id") or ""),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ── REST transport ─────────────────────────────────────────────────────────

_last_write_at = 0.0


def _pace_write() -> None:
    """Sleep just enough that consecutive writes stay >= the pace interval."""
    interval = _MIN_WRITE_INTERVAL
    if interval <= 0:
        return
    remaining = _last_write_at + interval - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _mark_write() -> None:
    global _last_write_at
    _last_write_at = time.monotonic()


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """retry_after Discord asked for, clamped, defaulting to 1s."""
    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    try:
        return max(0.0, min(float(payload.get("retry_after")), _MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        return 1.0


def _parse_payload(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return payload
    return {"data": payload}


def _request(token: str, method: str, url: str, body: Any = None) -> Dict[str, Any]:
    """One Discord REST call: paced on writes, 429-aware, never echoing headers.

    Errors surface as exceptions so callers can decide which ones are part of
    the answer (a 404 on a member lookup is an answer, not a crash).
    """
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    is_write = method.upper() != "GET"
    last_error: Optional[urllib.error.HTTPError] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if is_write:
            _pace_write()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
            if is_write:
                _mark_write()
            return _parse_payload(raw)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < _MAX_ATTEMPTS:
                time.sleep(_retry_after(exc))
                continue
            raise
    raise last_error  # pragma: no cover - loop always returns or raises


def _as_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _parse_permissions(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


# ── guild / role / channel lookups ─────────────────────────────────────────


def _resolve_guild(args: Dict[str, Any], token: str) -> Dict[str, Any]:
    """guild_id argument, else saved state, else the bot's only guild."""
    guild_id = str(args.get("guild_id") or "").strip()
    if guild_id:
        return {"success": True, "guild_id": guild_id}

    saved = str(_load_state().get("guild_id") or "")
    if saved:
        return {"success": True, "guild_id": saved}

    payload = _request(token, "GET", f"{_DISCORD_API_BASE}/users/@me/guilds")
    guilds = _as_list(payload)

    if not guilds:
        return {"success": False, "error": "bot is not in any guild"}

    if len(guilds) > 1:
        return {
            "success": False,
            "error": "bot is in multiple guilds; pass guild_id",
            "guilds": [
                {"id": str(g.get("id", "")), "name": str(g.get("name", ""))}
                for g in guilds
            ],
        }

    return {"success": True, "guild_id": str(guilds[0].get("id", ""))}


def _resolve_guild_checked(
    args: Dict[str, Any], token: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Guild id for the call, or the error payload to answer with instead."""
    resolved = _resolve_guild(args, token)
    if not resolved.get("success"):
        return "", resolved
    guild_id = str(resolved["guild_id"])
    if not guild_id:
        return "", {"success": False, "error": "could not resolve guild id"}
    return guild_id, None


def _find_role(token: str, guild_id: str) -> Optional[Dict[str, Any]]:
    roles = _as_list(
        _request(token, "GET", f"{_DISCORD_API_BASE}/guilds/{guild_id}/roles")
    )
    return next(
        (
            r
            for r in roles
            if str(r.get("name", "")).strip().lower() == ROLE_NAME.lower()
        ),
        None,
    )


def _ensure_role(token: str, guild_id: str) -> Dict[str, Any]:
    """Find or create the Guest role, then persist {guild_id, role_id}.

    Creation is the only side effect — a fresh Guest role has no permissions,
    so setup grants nothing until an explicit grant call says where.
    """
    existing = _find_role(token, guild_id)
    created = existing is None
    if existing is not None:
        role_id = str(existing.get("id") or "")
    else:
        role = _request(
            token,
            "POST",
            f"{_DISCORD_API_BASE}/guilds/{guild_id}/roles",
            {
                "name": ROLE_NAME,
                "permissions": "0",
                "color": 0,
                "hoist": True,
                "mentionable": False,
            },
        )
        role_id = str(role.get("id") or "")

    if not role_id:
        return {"success": False, "error": "Guest role did not return an id"}

    _save_state({"guild_id": guild_id, "role_id": role_id})
    return {"success": True, "role_id": role_id, "created": created}


def _load_channels(token: str, guild_id: str) -> List[Dict[str, Any]]:
    return _as_list(
        _request(token, "GET", f"{_DISCORD_API_BASE}/guilds/{guild_id}/channels")
    )


def _resolve_channels(
    channels: List[Dict[str, Any]], wanted: List[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """IDs straight through; names matched case-insensitively against the guild."""
    resolved: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    for item in wanted:
        needle = str(item).strip()
        if not needle:
            continue
        if needle.isdigit():
            match = next((c for c in channels if str(c.get("id")) == needle), None)
            if match is None:
                unresolved.append(needle)
                continue
            resolved.append(match)
            continue

        lowered = needle.lower()
        matches = [
            c for c in channels if str(c.get("name") or "").strip().lower() == lowered
        ]
        if len(matches) == 1:
            resolved.append(matches[0])
        elif not matches:
            unresolved.append(needle)
        else:
            # Same-named text and voice channel — the caller must use IDs.
            unresolved.append(
                f"{needle} (ambiguous: "
                + ", ".join(str(m.get("id")) for m in matches)
                + ")"
            )
    return resolved, unresolved


def _find_overwrite(
    channel: Dict[str, Any], target_id: str
) -> Optional[Dict[str, Any]]:
    for overwrite in channel.get("permission_overwrites") or []:
        if isinstance(overwrite, dict) and str(overwrite.get("id")) == str(target_id):
            return overwrite
    return None


def _assert_safe_allow(allow: int) -> None:
    """Refuse to write an allow mask carrying operator-grade permissions."""
    unsafe = allow & _FORBIDDEN_PERMS
    if unsafe:
        raise ValueError(
            "refusing to grant Guest the operator permissions "
            f"{unsafe} (ADMINISTRATOR/MANAGE_*/BAN/KICK/MENTION_EVERYONE are never allowed)"
        )


def _put_overwrite(
    token: str,
    channel_id: str,
    target_id: str,
    allow: int,
    deny: int,
    *,
    enforce_safe: bool = True,
) -> None:
    if enforce_safe:
        _assert_safe_allow(allow)
    _request(
        token,
        "PUT",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/permissions/{target_id}",
        {
            "id": str(target_id),
            "type": _OVERWRITE_ROLE,
            "allow": str(allow),
            "deny": str(deny),
        },
    )


def _delete_overwrite(token: str, channel_id: str, target_id: str) -> None:
    _request(
        token,
        "DELETE",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/permissions/{target_id}",
    )


def _allow_role_on_channel(
    token: str, channel: Dict[str, Any], role_id: str, allow: int
) -> bool:
    """Merge ``allow`` into the role's existing overwrite on this channel.

    Merging, not replacing, keeps any narrower allow a previous call made and
    clears the deny side of anything we now allow. Returns False when the
    overwrite already says exactly this, so idempotent re-grants write nothing.
    """
    existing = _find_overwrite(channel, role_id)
    cur_allow = _parse_permissions(existing.get("allow")) if existing else 0
    cur_deny = _parse_permissions(existing.get("deny")) if existing else 0
    new_allow = cur_allow | allow
    new_deny = cur_deny & ~allow
    if existing is not None and new_allow == cur_allow and new_deny == cur_deny:
        return False
    _put_overwrite(token, str(channel.get("id")), role_id, new_allow, new_deny)
    return True


def _revoke_role_on_channel(token: str, channel: Dict[str, Any], role_id: str) -> bool:
    if _find_overwrite(channel, role_id) is None:
        return False
    _delete_overwrite(token, str(channel.get("id")), role_id)
    return True


# ── actions ─────────────────────────────────────────────────────────────────


def _lockdown_everyone(token: str, guild_id: str) -> List[str]:
    """Deny @everyone VIEW_CHANNEL on every category and top-level channel.

    Categorised children are left alone on purpose: they inherit the category
    deny. Other targets' overwrites are preserved untouched — only the
    @everyone overwrite is rewritten, and only by merging the new deny bit in.
    """
    locked: List[str] = []
    for channel in _load_channels(token, guild_id):
        is_category = channel.get("type") == _CHANNEL_CATEGORY
        if not is_category and channel.get("parent_id") is not None:
            continue
        existing = _find_overwrite(channel, guild_id)
        allow = _parse_permissions(existing.get("allow")) if existing else 0
        deny = _parse_permissions(existing.get("deny")) if existing else 0
        if deny & _PERM_VIEW_CHANNEL and not allow & _PERM_VIEW_CHANNEL:
            continue  # already locked; a re-run writes nothing
        _put_overwrite(
            token,
            str(channel.get("id")),
            guild_id,
            allow & ~_PERM_VIEW_CHANNEL,
            deny | _PERM_VIEW_CHANNEL,
            enforce_safe=False,  # preserves @everyone's own prior allow bits
        )
        locked.append(str(channel.get("id")))
    return locked


def _member_permissions(
    guild_id: str, roles: List[Dict[str, Any]], member: Dict[str, Any]
) -> int:
    """Effective guild permissions for a member: @everyone plus each own role."""
    by_id = {str(r.get("id")): r for r in roles}
    perms = 0
    everyone = by_id.get(str(guild_id))
    if everyone is not None:
        perms |= _parse_permissions(everyone.get("permissions"))
    for role_id in member.get("roles") or []:
        role = by_id.get(str(role_id))
        if role is not None:
            perms |= _parse_permissions(role.get("permissions"))
    return perms


def _member_names(member: Dict[str, Any]) -> List[str]:
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    return [
        str(value).lower()
        for value in (user.get("username"), user.get("global_name"), member.get("nick"))
        if value
    ]


def _member_label(member: Dict[str, Any]) -> str:
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    name = str(user.get("global_name") or user.get("username") or "")
    return f"{name} ({user.get('id')})" if name else str(user.get("id") or "?")


def _resolve_member(token: str, guild_id: str, ref: str) -> Dict[str, Any]:
    """A member by user ID, or by name prefix through guild member search."""
    ref = str(ref).strip()
    if not ref:
        return {"success": False, "error": "member is required"}

    if ref.isdigit():
        try:
            member = _request(
                token, "GET", f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/{ref}"
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"success": False, "error": f"member {ref} is not in this guild"}
            raise
        return {"success": True, "member": member}

    query = urllib.parse.quote(ref)
    try:
        payload = _request(
            token,
            "GET",
            f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/search"
            f"?query={query}&limit=100",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return {
                "success": False,
                "error": (
                    "guild member search was denied (403) — enable the Server Members "
                    f"Intent or pass {ref}'s user ID instead"
                ),
            }
        raise

    prefix = ref.lower()
    matches = [
        m
        for m in _as_list(payload)
        if any(n.startswith(prefix) for n in _member_names(m))
    ]
    if not matches:
        return {"success": False, "error": f"no guild member matches '{ref}'"}
    if len(matches) > 1:
        return {
            "success": False,
            "error": f"'{ref}' matches several members; pass a user ID",
            "members": [_member_label(m) for m in matches],
        }
    return {"success": True, "member": matches[0]}


def _member_id(member: Dict[str, Any]) -> str:
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    return str(user.get("id") or "")


def _guard_non_admin(
    token: str, guild_id: str, member: Dict[str, Any]
) -> Optional[str]:
    """A member holding ADMINISTRATOR is an operator and cannot be guested."""
    roles = _as_list(
        _request(token, "GET", f"{_DISCORD_API_BASE}/guilds/{guild_id}/roles")
    )
    if _member_permissions(guild_id, roles, member) & _PERM_ADMINISTRATOR:
        return (
            f"{_member_label(member)} has ADMINISTRATOR via a role; "
            "guesting an operator is refused"
        )
    return None


def _normalise_channels(args: Dict[str, Any]) -> List[str]:
    raw = args.get("channels")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw]


def _handle_setup(args: Dict[str, Any], token: str) -> str:
    guild_id, error = _resolve_guild_checked(args, token)
    if error is not None:
        return json.dumps(error)

    state = _load_state()
    # lockdown defaults to true on the first setup for this guild, and to
    # false afterwards — re-locking an already-locked server is opt-in.
    first_setup = not (state.get("role_id") and str(state.get("guild_id")) == guild_id)
    lockdown_arg = args.get("lockdown")
    lockdown = first_setup if lockdown_arg is None else bool(lockdown_arg)

    try:
        role = _ensure_role(token, guild_id)
        if not role.get("success"):
            return json.dumps(role)
        role_id = str(role["role_id"])

        locked: List[str] = []
        if lockdown:
            locked = _lockdown_everyone(token, guild_id)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})

    return json.dumps({
        "success": True,
        "action": "setup",
        "guild_id": guild_id,
        "role_id": role_id,
        "role_created": bool(role.get("created")),
        "lockdown_applied": lockdown,
        "locked_channel_ids": locked,
    })


def _handle_add(args: Dict[str, Any], token: str) -> str:
    guild_id, error = _resolve_guild_checked(args, token)
    if error is not None:
        return json.dumps(error)

    ref = str(args.get("member") or "").strip()
    if not ref:
        return json.dumps({"success": False, "error": "member is required for add"})

    try:
        found = _resolve_member(token, guild_id, ref)
        if not found.get("success"):
            return json.dumps(found)
        member = found["member"]
        member_id = _member_id(member)
        if not member_id:
            return json.dumps({
                "success": False,
                "error": "member did not return an id",
            })

        # An operator cannot be guested: whatever role carries their
        # ADMINISTRATOR would ride along with the Guest grant.
        admin_error = _guard_non_admin(token, guild_id, member)
        if admin_error:
            return json.dumps({"success": False, "error": admin_error, "refused": True})

        role = _ensure_role(token, guild_id)
        if not role.get("success"):
            return json.dumps(role)
        role_id = str(role["role_id"])

        _request(
            token,
            "PUT",
            f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/{member_id}/roles/{role_id}",
        )

        granted: List[str] = []
        wanted = _normalise_channels(args)
        if wanted:
            channels = _load_channels(token, guild_id)
            targets, unresolved = _resolve_channels(channels, wanted)
            if unresolved:
                return json.dumps({
                    "success": False,
                    "error": "unknown channel(s): " + ", ".join(unresolved),
                    "member_id": member_id,
                    "role_id": role_id,
                })
            for target in targets:
                if _allow_role_on_channel(token, target, role_id, _PERM_VIEW_CHANNEL):
                    granted.append(str(target.get("id")))

        return json.dumps({
            "success": True,
            "action": "add",
            "guild_id": guild_id,
            "member_id": member_id,
            "role_id": role_id,
            "granted_channel_ids": granted,
        })
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _handle_remove(args: Dict[str, Any], token: str) -> str:
    guild_id, error = _resolve_guild_checked(args, token)
    if error is not None:
        return json.dumps(error)

    ref = str(args.get("member") or "").strip()
    if not ref:
        return json.dumps({"success": False, "error": "member is required for remove"})

    try:
        found = _resolve_member(token, guild_id, ref)
        if not found.get("success"):
            return json.dumps(found)
        member_id = _member_id(found["member"])

        role = _ensure_role(token, guild_id)
        if not role.get("success"):
            return json.dumps(role)
        role_id = str(role["role_id"])

        # Only the membership goes: the role stays, and any Guest channel
        # overwrites stay too, so other guests keep their access.
        _request(
            token,
            "DELETE",
            f"{_DISCORD_API_BASE}/guilds/{guild_id}/members/{member_id}/roles/{role_id}",
        )
        return json.dumps({
            "success": True,
            "action": "remove",
            "guild_id": guild_id,
            "member_id": member_id,
            "role_id": role_id,
        })
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _children_by_parent(
    channels: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for channel in channels:
        parent = channel.get("parent_id")
        if parent is not None:
            grouped.setdefault(str(parent), []).append(channel)
    return grouped


def _handle_grant(args: Dict[str, Any], token: str) -> str:
    guild_id, error = _resolve_guild_checked(args, token)
    if error is not None:
        return json.dumps(error)

    wanted = _normalise_channels(args)
    if not wanted:
        return json.dumps({"success": False, "error": "channels is required for grant"})

    try:
        role = _ensure_role(token, guild_id)
        if not role.get("success"):
            return json.dumps(role)
        role_id = str(role["role_id"])

        channels = _load_channels(token, guild_id)
        targets, unresolved = _resolve_channels(channels, wanted)
        if unresolved:
            return json.dumps({
                "success": False,
                "error": "unknown channel(s): " + ", ".join(unresolved),
            })

        children = _children_by_parent(channels)
        granted: List[str] = []
        for target in targets:
            target_id = str(target.get("id"))
            if _allow_role_on_channel(token, target, role_id, _GUEST_ALLOW):
                granted.append(target_id)
            if target.get("type") != _CHANNEL_CATEGORY:
                continue
            # One overwrite on the category covers every synced child. A child
            # carrying its own @everyone overwrite is unsynced and would stay
            # dark, so it gets the same allow written directly.
            for child in children.get(target_id, []):
                if _find_overwrite(child, guild_id) is None:
                    continue
                if _allow_role_on_channel(token, child, role_id, _GUEST_ALLOW):
                    granted.append(str(child.get("id")))

        return json.dumps({
            "success": True,
            "action": "grant",
            "guild_id": guild_id,
            "role_id": role_id,
            "allow": str(_GUEST_ALLOW),
            "granted_channel_ids": granted,
        })
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _handle_revoke(args: Dict[str, Any], token: str) -> str:
    guild_id, error = _resolve_guild_checked(args, token)
    if error is not None:
        return json.dumps(error)

    wanted = _normalise_channels(args)
    if not wanted:
        return json.dumps({
            "success": False,
            "error": "channels is required for revoke",
        })

    try:
        role = _ensure_role(token, guild_id)
        if not role.get("success"):
            return json.dumps(role)
        role_id = str(role["role_id"])

        channels = _load_channels(token, guild_id)
        targets, unresolved = _resolve_channels(channels, wanted)
        if unresolved:
            return json.dumps({
                "success": False,
                "error": "unknown channel(s): " + ", ".join(unresolved),
            })

        children = _children_by_parent(channels)
        revoked: List[str] = []
        for target in targets:
            target_id = str(target.get("id"))
            if _revoke_role_on_channel(token, target, role_id):
                revoked.append(target_id)
            if target.get("type") != _CHANNEL_CATEGORY:
                continue
            # Mirror of grant: the children grant touched directly get cleared.
            for child in children.get(target_id, []):
                if _revoke_role_on_channel(token, child, role_id):
                    revoked.append(str(child.get("id")))

        return json.dumps({
            "success": True,
            "action": "revoke",
            "guild_id": guild_id,
            "role_id": role_id,
            "revoked_channel_ids": revoked,
        })
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _handle_list(args: Dict[str, Any], token: str) -> str:
    guild_id, error = _resolve_guild_checked(args, token)
    if error is not None:
        return json.dumps(error)

    try:
        # Read-only: listing reports on the Guest role, it never provisions one.
        existing = _find_role(token, guild_id)
        if existing is None:
            return json.dumps({
                "success": False,
                "error": "Guest role not found; run action='setup' first",
            })
        role_id = str(existing.get("id") or "")

        members: List[Dict[str, str]] = []
        note = ""
        try:
            payload = _request(
                token,
                "GET",
                f"{_DISCORD_API_BASE}/guilds/{guild_id}/members?limit=1000",
            )
            for member in _as_list(payload):
                if role_id in [str(r) for r in member.get("roles") or []]:
                    user = (
                        member.get("user")
                        if isinstance(member.get("user"), dict)
                        else {}
                    )
                    members.append({
                        "id": str(user.get("id") or ""),
                        "name": str(
                            user.get("global_name") or user.get("username") or ""
                        ),
                    })
        except urllib.error.HTTPError as exc:
            # Listing members needs the privileged members intent; the rest of
            # the answer is still useful without it.
            if exc.code == 403:
                note = "could not list members (403) — the Server Members Intent is off"
            else:
                note = f"could not list members (HTTP {exc.code})"

        channels = _load_channels(token, guild_id)
        allowed = [
            {
                "id": str(c.get("id")),
                "name": str(c.get("name") or ""),
                "type": c.get("type"),
            }
            for c in channels
            if _parse_permissions((_find_overwrite(c, role_id) or {}).get("allow"))
        ]

        result: Dict[str, Any] = {
            "success": True,
            "action": "list",
            "guild_id": guild_id,
            "role_id": role_id,
            "members": members,
            "allowed_channels": allowed,
        }
        if note:
            result["note"] = note
        return json.dumps(result)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


_HANDLERS = {
    "setup": _handle_setup,
    "add": _handle_add,
    "remove": _handle_remove,
    "grant": _handle_grant,
    "revoke": _handle_revoke,
    "list": _handle_list,
}


def handle_discord_guests(args: dict, **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "").strip().lower()
        if action not in _ALLOWED_ACTIONS:
            return json.dumps({
                "success": False,
                "error": f"action must be one of {sorted(_ALLOWED_ACTIONS)}",
            })

        token = _read_discord_token()
        if not token:
            return json.dumps({
                "success": False,
                "error": "Discord bot token not configured",
            })

        return _HANDLERS[action](args, token)
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
