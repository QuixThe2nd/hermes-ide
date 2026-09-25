"""Ticket-resolution tool for Discord threads.

Flow (all driven by this tool; the discord platform plugin only forwards
raw reaction events here):

1. ``action='propose'`` posts an embed asking the user to confirm closing
   the ticket, seeds it with the bot's own ✅/❌ reactions, and registers
   a *pending listener* with an expiry.
2. The tool listens for the user's ✅/❌ reaction on that embed for
   ``timeout_minutes`` (per-call param, else ``discord.resolve_timeout_minutes``
   in config.yaml, else 30).
3. ✅ archives the thread; ❌ marks the ticket kept-open. Either decision,
   or the timeout expiring, stops the listener — late reactions are ignored.

Safety contract: closing a ticket ONLY ever archives the thread
(PATCH {"archived": true}). This module contains no DELETE calls and
refuses to act on non-thread channels, so ticket history can never be
destroyed by this tool. Archiving is fully reversible (anyone can
unarchive / send a message to reopen).

Registered in the ``discord`` toolset, so it costs nothing on other
platforms. Reuses the REST helper and token resolution from
``tools.discord_tool`` — no duplicated plumbing.
"""

import json
import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from tools.discord_tool import (
    DiscordAPIError,
    _discord_request,
    _get_bot_token,
    check_discord_tool_requirements,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_THREAD_TYPES = {10, 11, 12}  # announcement_thread, public_thread, private_thread
_EMBED_COLOR = 0x57F287  # Discord green
_EMBED_COLOR_MUTED = 0x95A5A6  # gray

EMOJI_CONFIRM = "✅"
EMOJI_DECLINE = "❌"
_FOOTER_MARKER = "hermes ticket resolution"
_FOOTER_OPEN = f"{_FOOTER_MARKER} • archive only, never delete"

_DEFAULT_TIMEOUT_MINUTES = 30
_MAX_TIMEOUT_MINUTES = 7 * 24 * 60  # one week

# ---------------------------------------------------------------------------
# Pending-listener store
# ---------------------------------------------------------------------------
# The "listener" is a record in this JSON file: while a message_id has an
# unexpired entry, the tool is listening for that prompt's reaction.
# Decision or expiry removes the entry — that is "stop listening".


def _pending_path():
    return get_hermes_home() / "discord_resolve_pending.json"


def _load_pending() -> Dict[str, Any]:
    try:
        return json.loads(_pending_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pending(pending: Dict[str, Any]) -> None:
    path = _pending_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending, indent=1), encoding="utf-8")
    tmp.replace(path)


def _prune_expired(pending: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    return {k: v for k, v in pending.items() if v.get("expires_at", 0) > now}


def _default_timeout_minutes() -> int:
    try:
        from hermes_cli.config import load_config

        value = (load_config().get("discord") or {}).get("resolve_timeout_minutes")
        if value is not None:
            return max(1, min(int(value), _MAX_TIMEOUT_MINUTES))
    except Exception:
        pass
    return _DEFAULT_TIMEOUT_MINUTES


# ---------------------------------------------------------------------------
# Discord REST helpers
# ---------------------------------------------------------------------------


def _add_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> None:
    encoded = urllib.parse.quote(emoji, safe="")
    _discord_request(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        token,
    )


def _edit_embed(token: str, channel_id: str, message_id: str, embed: Dict[str, Any]) -> None:
    _discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        token,
        body={"embeds": [embed]},
    )


def _post_embed(token: str, channel_id: str, summary: str, timeout_minutes: int) -> Dict[str, Any]:
    embed = {
        "title": "✅ Ticket resolved?",
        "description": (
            f"{summary.strip() or 'I believe this ticket is resolved.'}\n\n"
            f"React {EMOJI_CONFIRM} to close this ticket — the thread is archived, "
            "nothing is deleted and it can be reopened at any time. "
            f"React {EMOJI_DECLINE} to keep it open. (A plain \"yes\"/\"no\" reply works too.)\n\n"
            f"_Listening for {timeout_minutes} minute{'s' if timeout_minutes != 1 else ''}, then this prompt expires._"
        ),
        "color": _EMBED_COLOR,
        "footer": {"text": _FOOTER_OPEN},
    }
    message = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        body={"embeds": [embed]},
    )
    message_id = (message or {}).get("id")
    if message_id:
        # Best-effort: without both reactions the confirm flow still works
        # via text reply, so a reaction failure is not fatal.
        for emoji in (EMOJI_CONFIRM, EMOJI_DECLINE):
            try:
                _add_reaction(token, channel_id, message_id, emoji)
            except DiscordAPIError as exc:
                logger.warning("resolve_ticket: adding %s reaction failed (%s)", emoji, exc)
    return message


def _close_thread(token: str, channel_id: str) -> Dict[str, Any]:
    """Archive a thread. Never deletes. Idempotent."""
    channel = _discord_request("GET", f"/channels/{channel_id}", token)
    channel_type = channel.get("type")
    if channel_type not in _THREAD_TYPES:
        raise DiscordAPIError(
            400,
            f"Channel {channel_id} is not a thread (type={channel_type}); "
            "refusing to close. resolve_ticket only closes threads.",
        )

    thread_meta = channel.get("thread_metadata") or {}
    if thread_meta.get("archived"):
        return {"archived": True, "already_archived": True}

    # Best-effort farewell message before archiving. If the bot can't post
    # (permissions), archiving still proceeds.
    try:
        _discord_request(
            "POST",
            f"/channels/{channel_id}/messages",
            token,
            body={"content": "🔒 Chat closed — this thread has been archived. Nothing was deleted; ask here to reopen it."},
        )
    except DiscordAPIError as exc:
        logger.warning("resolve_ticket: farewell message failed (%s); archiving anyway", exc)

    _discord_request("PATCH", f"/channels/{channel_id}", token, body={"archived": True})

    # Verify the final state independently.
    after = _discord_request("GET", f"/channels/{channel_id}", token)
    archived = bool((after.get("thread_metadata") or {}).get("archived"))
    return {"archived": archived, "already_archived": False}


# ---------------------------------------------------------------------------
# Reaction listener entry point (called by the discord platform plugin)
# ---------------------------------------------------------------------------


def handle_resolve_reaction(
    channel_id: str,
    message_id: str,
    emoji: str,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Handle a reaction added to a resolve_ticket confirmation embed.

    Called by the Discord platform plugin's ``on_raw_reaction_add`` (bot's
    own reactions are filtered there). The plugin passes its configured
    bot ``token`` explicitly: raw gateway events run outside the per-turn
    profile secret scope, so ``_get_bot_token()`` would raise
    ``UnscopedSecretError`` here. Archive-only: the confirm path uses
    :func:`_close_thread`, which never deletes.
    """
    if emoji not in (EMOJI_CONFIRM, EMOJI_DECLINE):
        return {"acted": False, "reason": "unrelated_emoji"}

    # Listener state: only react while a pending, unexpired entry exists.
    pending = _load_pending()
    entry = pending.get(message_id)
    if not entry or entry.get("channel_id") != channel_id:
        return {"acted": False, "reason": "not_listening"}

    token = (token or "").strip() or _get_bot_token()
    if not token:
        return {"acted": False, "reason": "no_token"}

    if entry.get("expires_at", 0) <= time.time():
        # Stop listening: expired. Mark the embed so late clickers get feedback.
        try:
            _edit_embed(token, channel_id, message_id, {
                "title": "⌛ Ticket prompt expired",
                "description": entry.get("summary", ""),
                "color": _EMBED_COLOR_MUTED,
                "footer": {"text": f"{_FOOTER_MARKER} • timed out"},
            })
        except DiscordAPIError as exc:
            logger.warning("resolve_ticket: timeout-embed edit failed (%s)", exc)
        pending.pop(message_id, None)
        _save_pending(_prune_expired(pending))
        return {"acted": False, "reason": "timed_out"}

    summary = entry.get("summary", "")

    if emoji == EMOJI_CONFIRM:
        try:
            _edit_embed(token, channel_id, message_id, {
                "title": "🔒 Chat closed",
                "description": summary,
                "color": _EMBED_COLOR,
                "footer": {"text": f"{_FOOTER_MARKER} • closed"},
            })
        except DiscordAPIError as exc:
            logger.warning("resolve_ticket: closed-embed edit failed (%s); archiving anyway", exc)
        result = _close_thread(token, channel_id)
        decision: Dict[str, Any] = {"acted": True, "decision": "closed", "archived": result["archived"]}
    else:
        _edit_embed(token, channel_id, message_id, {
            "title": "🎫 Ticket kept open",
            "description": summary,
            "color": _EMBED_COLOR_MUTED,
            "footer": {"text": f"{_FOOTER_MARKER} • kept open"},
        })
        decision = {"acted": True, "decision": "kept_open"}

    # Stop listening: decided.
    pending.pop(message_id, None)
    _save_pending(_prune_expired(pending))
    return decision


# ---------------------------------------------------------------------------
# Model-facing tool
# ---------------------------------------------------------------------------


def resolve_ticket(
    action: str = "propose",
    channel_id: str = "",
    summary: str = "",
    timeout_minutes: Optional[int] = None,
    task_id: Optional[str] = None,
) -> str:
    token = _get_bot_token()
    if not token:
        return tool_error("DISCORD_BOT_TOKEN is not configured.")

    channel_id = (channel_id or "").strip()
    if not channel_id:
        return tool_error("channel_id is required — pass the current thread ID from session context.")

    try:
        if action == "propose":
            minutes = _default_timeout_minutes() if timeout_minutes is None else max(1, min(int(timeout_minutes), _MAX_TIMEOUT_MINUTES))
            message = _post_embed(token, channel_id, summary, minutes)
            message_id = (message or {}).get("id")
            expires_at = time.time() + minutes * 60
            if message_id:
                pending = _prune_expired(_load_pending())
                pending[message_id] = {
                    "channel_id": channel_id,
                    "summary": summary.strip(),
                    "expires_at": expires_at,
                }
                _save_pending(pending)
            return json.dumps({
                "success": True,
                "action": "propose",
                "channel_id": channel_id,
                "message_id": message_id,
                "timeout_minutes": minutes,
                "note": (
                    f"Confirmation embed posted; listening for the user's ✅/❌ reaction for {minutes} minutes. "
                    "If the user confirms in plain text instead, call action='close'."
                ),
            })
        if action == "close":
            result = _close_thread(token, channel_id)
            return json.dumps({
                "success": bool(result["archived"]),
                "action": "close",
                "channel_id": channel_id,
                "archived": result["archived"],
                "already_archived": result["already_archived"],
                "deleted": False,
                "note": "Thread archived (reversible). Nothing was deleted.",
            })
        return tool_error(f"Unknown action '{action}'. Use 'propose' or 'close'.")
    except DiscordAPIError as exc:
        return tool_error(f"Discord API error {exc.status}: {exc.body[:500]}")


_SCHEMA: Dict[str, Any] = {
    "name": "resolve_ticket",
    "description": (
        "Use when you believe the conversation in this Discord thread is resolved. "
        "action='propose' posts an embed asking the user to confirm closing the ticket, reacts to it with "
        "✅ and ❌, and listens for the user's reaction for timeout_minutes (default from "
        "discord.resolve_timeout_minutes in config.yaml, else 30) before stopping. "
        "✅ archives the thread (fully reversible, NEVER deletes); ❌ keeps it open. "
        "A successful action='propose' call is terminal: the embed is the reply — end your turn immediately "
        "with no follow-up assistant message and no further tool calls. "
        "Only use action='close' directly AFTER the user explicitly confirms in text (e.g. 'yes', 'close it')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["propose", "close"],
                "description": "'propose' asks the user to confirm; 'close' archives the thread after confirmation.",
                "default": "propose",
            },
            "channel_id": {
                "type": "string",
                "description": "The current Discord thread/channel ID from session context.",
            },
            "summary": {
                "type": "string",
                "description": "One-line resolution summary shown in the confirmation embed (propose only).",
            },
            "timeout_minutes": {
                "type": "integer",
                "description": "How long to listen for the user's reaction before the prompt expires (propose only).",
            },
        },
        "required": ["action", "channel_id"],
    },
}

registry.register(
    name="resolve_ticket",
    toolset="discord",
    schema=_SCHEMA,
    handler=lambda args, **kw: resolve_ticket(
        action=args.get("action", "propose"),
        channel_id=args.get("channel_id", ""),
        summary=args.get("summary", ""),
        timeout_minutes=args.get("timeout_minutes"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_discord_tool_requirements,
    requires_env=["DISCORD_BOT_TOKEN"],
)
