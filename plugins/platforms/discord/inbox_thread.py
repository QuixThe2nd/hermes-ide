"""Public-thread anchoring for outbound sends to the Hermes Starts inbox.

The inbox channel's contract is one opening message per thread: every
bot-authored top-level message there must anchor a public thread, whatever
path delivered it — adapter ``send()``, agent-progress embeds, ``hermes
send``/``send_message``, cron delivery. ``start_conversation`` already
threads its own openings over REST; this hook closes the adapter gap so the
invariant no longer depends on the model choosing that tool.

The inbox channel id is resolved from hermes_starts state (falling back to
the shared home_server inbox) — nothing is hardcoded, and a missing or
corrupt state makes the whole hook a no-op, so the adapter keeps working
when the plugin is absent or unprovisioned.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Discord error code for "thread already created for this message" — the
# message anchoring a start_conversation opening (or any other pre-threaded
# message) already satisfies the invariant, so this is success, not failure.
_THREAD_ALREADY_EXISTS_CODE = "160004"


def _hermes_starts_module() -> Optional[Any]:
    """The hermes_starts plugin module, or ``None`` when it cannot load.

    Lazy so importing the Discord adapter never pulls plugin state code, and
    blanket-caught so a broken plugin install degrades to "no inbox" rather
    than a broken send path.
    """
    try:
        import plugins.hermes_starts as hermes_starts

        return hermes_starts
    except Exception:
        return None


def _hermes_starts_disabled() -> bool:
    """Whether hermes_starts is on the config deny-list.

    Same ``plugins.disabled`` read the plugin manager's gate uses; a bundled
    ``default_enabled: true`` plugin is otherwise always on. A disabled
    plugin must not thread sends even if its state file still names a
    channel. Read failures count as enabled — same fail-open as the gate.
    """
    try:
        from hermes_cli.plugins_discovery import _get_disabled_plugins

        return "hermes_starts" in _get_disabled_plugins()
    except Exception:
        return False


def _inbox_channel_id(hermes_starts: Any) -> str:
    """Configured inbox channel id: hermes_starts state, then the shared inbox.

    ``adopt_home_server_inbox`` normally copies the home_server inbox into
    Starts' own state, but adoption only runs on setup/wiring — reading both
    keeps sends threaded even before that first reconciliation.
    """
    try:
        channel_id = str(hermes_starts._load_state().get("channel_id") or "")
        if not channel_id:
            channel_id = str(hermes_starts._home_server_inbox().get("channel_id") or "")
        return channel_id
    except Exception:
        return ""


def _cannot_host_thread(channel: Any) -> bool:
    """True for destinations that are not a plain top-level text channel.

    Duck-typed on purpose: DMs, existing threads, and forum parents can never
    be the anchor site, and the checks must hold for discord.py objects and
    test doubles alike.
    """
    if channel is None:
        return True
    if getattr(channel, "recipient", None) is not None:  # DM
        return True
    if getattr(channel, "parent_id", None) is not None:  # already inside a thread
        return True
    channel_type = getattr(channel, "type", None)
    type_value = getattr(channel_type, "value", channel_type)
    return type_value == 15  # forum parent (same test as _is_forum_parent)


def _thread_already_exists(exc: urllib.error.HTTPError) -> bool:
    """Whether *exc* is Discord rejecting a second thread on one message."""
    if exc.code != 400:
        return False
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        return False
    return _THREAD_ALREADY_EXISTS_CODE in body or "already" in body.lower()


async def ensure_inbox_thread(
    adapter: Any,
    channel: Any,
    message: Any,
    content: str,
    *,
    thread_id: Optional[str] = None,
) -> None:
    """Anchor a public thread on a just-sent top-level inbox message.

    Called by the Discord adapter's ``send()`` on every successful top-level
    delivery — never raises, never threads anything but the configured inbox
    parent channel (other channels, DMs, threads, forums and failed or empty
    sends are skipped before this runs). Thread creation reuses
    hermes_starts' REST primitive, the same one ``start_conversation``
    threads its openings with, so an adapter-delivered opening is anchored on
    exactly the message that was posted and gets the same public type-11
    thread. The name is derived from the sent content (there is no
    ``kind`` on this path). A message that already has a thread is success:
    no second thread, no error. Any other creation failure degrades to a
    logged warning — the send itself already succeeded, and losing it to a
    threading problem would turn a degraded post into a lost one.
    """
    if thread_id:
        return
    if message is None:
        return
    hermes_starts = _hermes_starts_module()
    if hermes_starts is None or _hermes_starts_disabled():
        return
    inbox_id = _inbox_channel_id(hermes_starts)
    if not inbox_id or str(getattr(channel, "id", "") or "") != inbox_id:
        return
    if _cannot_host_thread(channel):
        return

    token = str(getattr(getattr(adapter, "config", None), "token", None) or "")
    if not token:
        token = hermes_starts._read_discord_token()
    if not token:
        logger.warning(
            "[%s] No Discord token available to thread inbox message; "
            "the send stands without its thread",
            getattr(adapter, "name", "discord"),
        )
        return

    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return
    thread_name = adapter._derive_auto_thread_name(content or "")
    try:
        new_thread_id = await asyncio.to_thread(
            hermes_starts._create_thread_for_message,
            token,
            inbox_id,
            message_id,
            thread_name,
        )
    except urllib.error.HTTPError as exc:
        if _thread_already_exists(exc):
            logger.info(
                "[%s] Inbox message %s already anchors a thread; leaving it as is",
                getattr(adapter, "name", "discord"),
                message_id,
            )
            return
        logger.warning(
            "[%s] Inbox thread creation for message %s failed: HTTP %s "
            "(message delivered without its thread)",
            getattr(adapter, "name", "discord"),
            message_id,
            exc.code,
        )
        return
    except Exception as exc:
        logger.warning(
            "[%s] Inbox thread creation for message %s failed: %s: %s "
            "(message delivered without its thread)",
            getattr(adapter, "name", "discord"),
            message_id,
            type(exc).__name__,
            exc,
        )
        return
    if not new_thread_id:
        logger.warning(
            "[%s] Inbox thread creation for message %s returned no id "
            "(message delivered without its thread)",
            getattr(adapter, "name", "discord"),
            message_id,
        )
        return

    # Replies inside the new thread must not need an @mention. The adapter's
    # live tracker is the one its ingress gates consult; hermes_starts'
    # persistent mark is the fallback for adapters without it.
    tracker = getattr(adapter, "_threads", None)
    mark = getattr(tracker, "mark", None)
    try:
        if mark is not None:
            mark(new_thread_id)
        else:
            hermes_starts._mark_participated_thread(new_thread_id)
    except Exception as exc:
        logger.warning(
            "[%s] Failed to mark inbox thread %s as participated: %s",
            getattr(adapter, "name", "discord"),
            new_thread_id,
            exc,
        )
