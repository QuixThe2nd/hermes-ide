"""Public inbox-thread helpers for other in-tree plugins.

Consumers (papercuts' per-papercut threads) need three operations on the
provisioned #inbox channel: open a thread anchored on a body message, post
into an existing thread, and rename/archive a thread. They must not duplicate
the Discord REST layer, the token loader, or inbox resolution — so those live
here as the single public surface over this plugin's internals.

``hermes_starts`` never imports a consumer; the dependency points one way.
Configuration problems fail LOUD (a missing token or an unprovisioned inbox is
a setup error, and returning ``None`` would hide it behind a silent no-op), so
helpers raise ``RuntimeError``; transport failures propagate as-is. Consumers
that need total semantics wrap the call — papercuts' ``threads.py`` does.
"""

from __future__ import annotations

from typing import Dict, Optional


def _require_token() -> str:
    from plugins.hermes_starts import _read_discord_token

    token = _read_discord_token()
    if not token:
        raise RuntimeError("Discord bot token not configured (DISCORD_BOT_TOKEN)")
    return token


def _require_inbox() -> Dict[str, str]:
    from plugins.hermes_starts import provisioned_inbox

    inbox = provisioned_inbox()
    if not inbox or not inbox.get("channel_id"):
        raise RuntimeError("no provisioned inbox channel (run start_conversation setup first)")
    return inbox


def open_inbox_thread(title: str, body: str) -> Dict[str, str]:
    """Post ``body`` to the inbox and open a public thread on it.

    Returns ``{"thread_id", "message_id", "url"}``.
    """
    token = _require_token()
    inbox = _require_inbox()
    from plugins.hermes_starts import _create_thread_for_message, _post_channel_message

    message_id = _post_channel_message(token, inbox["channel_id"], body)
    if not message_id:
        raise RuntimeError("inbox anchor post returned no message id")
    thread_id = _create_thread_for_message(token, inbox["channel_id"], message_id, title)
    if not thread_id:
        raise RuntimeError("thread creation returned no id")
    guild_id = str(inbox.get("guild_id") or "")
    url = f"https://discord.com/channels/{guild_id}/{thread_id}" if guild_id else ""
    return {"thread_id": thread_id, "message_id": message_id, "url": url}


def post_to_thread(thread_id: str, content: str) -> str:
    """Post one message into a thread; returns its message id."""
    if not thread_id:
        raise RuntimeError("post_to_thread requires a thread id")
    token = _require_token()
    from plugins.hermes_starts import _post_channel_message

    return _post_channel_message(token, thread_id, content)


def update_inbox_thread(
    thread_id: str,
    name: Optional[str] = None,
    archived: Optional[bool] = None,
) -> bool:
    """Rename and/or (un)archive a thread. Returns True on success."""
    if not thread_id:
        raise RuntimeError("update_inbox_thread requires a thread id")
    token = _require_token()
    body: Dict[str, object] = {}
    if name is not None:
        body["name"] = str(name)[:100]
    if archived is not None:
        body["archived"] = bool(archived)
    if not body:
        raise RuntimeError("update_inbox_thread needs a name and/or archived flag")
    from plugins.hermes_starts import _DISCORD_API_BASE, _discord_request

    _discord_request(token, "PATCH", f"{_DISCORD_API_BASE}/channels/{thread_id}", body)
    return True
