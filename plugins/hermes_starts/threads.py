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

from typing import Any, Callable, Dict, List, Optional


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


def _deliver_inbox_opening(
    token: str,
    channel_id: str,
    title: str,
    body: str,
    *,
    require_thread: bool,
    on_anchor: Optional[Callable[[str], None]] = None,
    on_thread: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Deliver one inbox opening with its configured owner notification.

    This is the only anchor-and-thread path. ``start_conversation`` tolerates
    thread-creation failure so it can finish in the channel; public helpers
    require the thread they promise to return. Member-add failures are always
    non-fatal, matching the original start behavior.
    """
    import urllib.error

    from plugins.hermes_starts import (
        _add_thread_member,
        _create_thread_for_message,
        _mention_user_id,
        _post_channel_message,
        _split_delivery,
    )

    mention_uid = _mention_user_id()
    parts = _split_delivery(body, mention_uid)
    message_id = _post_channel_message(token, channel_id, parts[0])
    if require_thread and not message_id:
        raise RuntimeError("inbox anchor post returned no message id")
    if on_anchor is not None:
        on_anchor(message_id)

    warnings: List[str] = []
    thread_id = ""
    try:
        thread_id = _create_thread_for_message(token, channel_id, message_id, title)
    except urllib.error.HTTPError as exc:
        if require_thread:
            raise
        warnings.append(f"thread creation failed: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        if require_thread:
            raise
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        warnings.append(f"thread creation failed: {reason}")
    except Exception as exc:
        if require_thread:
            raise
        warnings.append(f"thread creation failed: {type(exc).__name__}: {exc}")

    if not thread_id:
        if require_thread:
            raise RuntimeError("thread creation returned no id")
        if not warnings:
            warnings.append("thread creation returned no id")
        return {
            "message_id": message_id,
            "thread_id": "",
            "mention_user_id": mention_uid,
            "parts": parts,
            "channel_message_ids": [message_id],
            "thread_message_ids": [],
            "warnings": warnings,
        }

    if on_thread is not None:
        on_thread(thread_id)
    if mention_uid:
        try:
            _add_thread_member(token, thread_id, mention_uid)
        except urllib.error.HTTPError as exc:
            warnings.append(f"thread member add failed for {mention_uid}: HTTP {exc.code}")
        except urllib.error.URLError as exc:
            reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
            warnings.append(f"thread member add failed for {mention_uid}: {reason}")
        except Exception as exc:
            warnings.append(
                f"thread member add failed for {mention_uid}: {type(exc).__name__}: {exc}"
            )

    thread_message_ids = [
        _post_channel_message(token, thread_id, part) for part in parts[1:]
    ]
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "mention_user_id": mention_uid,
        "parts": parts,
        "channel_message_ids": [message_id],
        "thread_message_ids": thread_message_ids,
        "warnings": warnings,
    }


def open_inbox_thread(title: str, body: str) -> Dict[str, str]:
    """Post a split, owner-pinged opening and open a public thread on it.

    Returns ``{"thread_id", "message_id", "url"}``.
    """
    token = _require_token()
    inbox = _require_inbox()
    delivered = _deliver_inbox_opening(
        token, inbox["channel_id"], title, body, require_thread=True
    )
    thread_id = str(delivered["thread_id"])
    guild_id = str(inbox.get("guild_id") or "")
    url = f"https://discord.com/channels/{guild_id}/{thread_id}" if guild_id else ""
    return {
        "thread_id": thread_id,
        "message_id": str(delivered["message_id"]),
        "url": url,
    }


def post_to_thread(thread_id: str, content: str) -> str:
    """Post content into a thread, splitting it without loss; return the first id."""
    if not thread_id:
        raise RuntimeError("post_to_thread requires a thread id")
    token = _require_token()
    from plugins.hermes_starts import _post_channel_message, _split_delivery

    first_message_id = ""
    for part in _split_delivery(content, ""):
        message_id = _post_channel_message(token, thread_id, part)
        if not first_message_id:
            first_message_id = message_id
    return first_message_id


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
