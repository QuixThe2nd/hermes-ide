"""Inbox-opening delivery for ``start_conversation``.

This module owns the single anchor-and-thread delivery path over this plugin's
Discord REST internals: the configured owner mention, lossless splitting,
anchor post, thread creation, member add, and tail posts. ``start_conversation``
tolerates thread-creation failure so it can finish in the channel. The
package never imports a consumer; any dependency on this module points one
way.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


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
    thread-creation failure so it can finish in the channel; callers that need
    a thread must say so with ``require_thread``. Member-add failures are
    always non-fatal, matching the original start behavior.
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
