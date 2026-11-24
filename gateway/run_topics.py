"""Discord auto-thread and Matrix inbox-room binding/rename methods for GatewayRunner (MRO mixin).
``gateway.run`` internals are imported lazily inside method bodies (import cycle), so
``patch("gateway.run.X")`` keeps intercepting them at call time."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Optional, Tuple

from gateway.config import Platform
from gateway.platforms.base import _prefix_within_utf16_limit, utf16_len
from gateway.session import SessionSource

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")



def _collapse_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip() or "Hermes Chat"


class GatewayTopicThreadsMixin:
    """Discord auto-thread and Matrix inbox-room binding/rename methods for GatewayRunner."""


    def _sanitize_discord_thread_title(self, title: str) -> str:
        """Discord-safe thread title: the 100-char cap is measured in UTF-16 code units (emoji count
        double), so truncate with the UTF-16 helpers rather than Python code-point slices."""
        cleaned = _collapse_title(title)
        return cleaned if utf16_len(cleaned) <= 80 else _prefix_within_utf16_limit(cleaned, 77).rstrip() + "..."

    # ── Discord auto-thread lanes ───────────────────────────────────────────────────────────

    def _is_discord_auto_thread_lane(self, source: SessionSource) -> bool:
        """Return True only for Discord threads Hermes just auto-created."""
        return (
            source.platform == Platform.DISCORD and source.chat_type == "thread"
            and bool(getattr(source, "auto_thread_created", False)) and bool(source.thread_id)
            and bool(getattr(source, "auto_thread_initial_name", None))
        )

    def _is_relay_discord_channel_lane(self, source: SessionSource) -> bool:
        """Shape-only check: a relay-delivered Discord CHANNEL event whose reply the connector MAY
        auto-thread (title-turn registration gate). Deliberately does NOT consult the send-result
        cache — before delivery the feedback can't exist; the rename lane polls it at fire time."""
        return (
            source.platform == Platform.DISCORD and bool(source.chat_id) and not source.thread_id
            and source.chat_type in ("group", "channel")
            and getattr(source, "delivered_via_upstream_relay", False) is True
        )

    def _relay_auto_thread_info(self, source: SessionSource) -> Optional[Tuple[str, str]]:
        """(thread_id, initial_name) when the RELAY connector auto-threaded our reply — the title-turn
        sibling of _is_discord_auto_thread_lane (whose markers only exist from turn 2 on; the title
        turn's source is the PARENT channel event).

        Preferred: the connector's per-message ``prospective_thread_id`` stamp (anchor message id ==
        the thread it will create), exact even when several auto-threads spawn from one channel;
        the connector's created-name guard enforces no-clobber. Fallback: the per-chat send-result
        cache (older connectors), which only ever renamed the FIRST thread.
        """
        from gateway.run import _as_thread_info
        if (
            source.platform != Platform.DISCORD or not source.chat_id
            or not getattr(source, "delivered_via_upstream_relay", False)
        ):
            return None
        prospective = getattr(source, "prospective_thread_id", None)
        if prospective:
            # Deterministic per-thread identity; the empty initial-name marker signals the caller
            # to rely on the connector-side no-clobber guard.
            return (str(prospective), "")
        info_fn = getattr(self._delivery_adapter_for(source), "auto_thread_info_for_chat", None)
        if not callable(info_fn):
            return None
        with suppress(Exception):
            return _as_thread_info(info_fn(str(source.chat_id)))
        return None

    async def _await_relay_auto_thread_info(
        self, source: SessionSource
    ) -> Optional[Tuple[str, str]]:
        """``_relay_auto_thread_info``, waited out until this turn delivers.

        A connector-stamped prospective id identifies the exact future thread,
        but it is known before the reply exists. That path still waits for the
        send result: a failed send must not rename a thread that was never
        created, and a stale per-chat send cache must not target a sibling.
        """
        from gateway.run import _as_thread_info, _float_env

        if not source.chat_id:
            return None
        adapter = self._adapter_for_source(source)
        prospective = getattr(source, "prospective_thread_id", None)
        # 0 means the operator disabled the turn limit; the backstop still needs one.
        timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800) or 1800
        if prospective:
            prospective = str(prospective)
            wait_send = getattr(adapter, "wait_for_next_send", None)
            if not callable(wait_send):
                return None
            try:
                if await wait_send(
                    str(source.chat_id),
                    timeout,
                    expected_thread_id=prospective,
                ):
                    return (prospective, "")
            except Exception:
                return None
            return None
        wait_fn = getattr(adapter, "wait_for_auto_thread_info", None)
        if not callable(wait_fn):
            return None
        expected_reply_to = getattr(source, "message_id", None)
        try:
            return _as_thread_info(
                await wait_fn(
                    str(source.chat_id),
                    timeout,
                    expected_reply_to=(
                        str(expected_reply_to) if expected_reply_to else None
                    ),
                )
            )
        except TypeError:
            # Compatibility with older relay adapters that predate correlation.
            try:
                return _as_thread_info(await wait_fn(str(source.chat_id), timeout))
            except Exception:
                return None
        except Exception:
            return None

    async def _rename_discord_auto_thread_for_session_title(
        self, source: SessionSource, session_id: str, title: str,
        relay_info: Optional[Tuple[str, str]] = None,
    ) -> None:
        """Best-effort semantic rename of a newly auto-created Discord thread. ``relay_info`` is the
        connector's (thread_id, initial_name) feedback, supplied on the title turn where the source
        is the parent-channel event without auto-thread markers (see _relay_auto_thread_info)."""
        if relay_info is None and not await asyncio.to_thread(self._is_discord_auto_thread_lane, source):
            # Relay title turn with no feedback captured at schedule time: the title comes off the
            # user's opening message, so it beats the delivery that produces the connector's
            # send-result feedback by the whole length of the turn. None here = a true miss.
            if not self._is_relay_discord_channel_lane(source):
                return
            relay_info = await self._await_relay_auto_thread_info(source)
            if relay_info is None:
                return
        adapter = self._delivery_adapter_for(source) if getattr(self, "adapters", None) else None
        rename_thread = getattr(adapter, "rename_thread", None)
        if rename_thread is None:
            return
        relay = relay_info is not None
        target_thread_id = relay_info[0] if relay else str(source.thread_id)
        thread_name = self._sanitize_discord_thread_title(title)
        # Relay: ask the CONNECTOR to enforce the no-clobber guard from its own created-name memory —
        # the gateway can't reproduce the initial name byte-for-byte (normalization drift silently
        # declined every rename). Its egress guard resolves the tenant from caches keyed by the PARENT
        # channel chat_id, so pass it or the lookup misses. Native: the source IS the thread; guard on
        # the initial name.
        rename_kwargs = (
            {"prefer_connector_created": True, "parent_chat_id": str(source.chat_id) if source.chat_id else None}
            if relay else {"only_if_current_name": getattr(source, "auto_thread_initial_name", None)}
        )
        logger.info(
            "discord auto-thread rename: thread=%s lane=%s new_title=%r",
            target_thread_id, "relay" if relay else "native", thread_name,
        )
        try:
            renamed = await rename_thread(target_thread_id, thread_name, **rename_kwargs)
            logger.info("discord auto-thread rename result: thread=%s applied=%s", target_thread_id, bool(renamed))
        except TypeError:
            logger.warning(
                "Discord semantic thread rename raised TypeError (adapter=%s)", type(adapter).__name__, exc_info=True,
            )
        except Exception:
            logger.debug("Failed to rename Discord auto-thread for generated session title", exc_info=True)

    # ── Matrix inbox-room title rename lane ─────────────────────────────────────────────────

    def _is_matrix_inbox_room_lane(self, source: SessionSource) -> bool:
        """True for a Hermes-created Matrix inbox child room. Child rooms resolve as
        ``chat_type="dm"`` (member count <= 2 in _resolve_room_identity), so the
        auto-thread markers distinguish them from a plain DM."""
        return (
            source.platform == Platform.MATRIX and source.chat_type == "dm"
            and bool(getattr(source, "auto_thread_created", False))
            and bool(getattr(source, "chat_id", None))
            and bool(getattr(source, "auto_thread_initial_name", None))
        )

    def _sanitize_matrix_room_title(self, title: str) -> str:
        return _collapse_title(title)[:255].rstrip()

    def _schedule_matrix_room_title_rename(self, source: SessionSource, session_id: str, title: str) -> None:
        """Schedule a Matrix inbox-room rename from the auto-title background thread."""
        if not title or not self._is_matrix_inbox_room_lane(source):
            return
        self._schedule_rename_from_title_thread(
            source,
            lambda copied: self._rename_matrix_room_for_session_title(copied, session_id, title),
            "Matrix room title rename",
        )

    async def _rename_matrix_room_for_session_title(
        self, source: SessionSource, session_id: str, title: str,
    ) -> None:
        """Best-effort rename of a newly created Matrix inbox child room."""
        if not await asyncio.to_thread(self._is_matrix_inbox_room_lane, source):
            return
        if not source.chat_id:
            return
        adapter = self._delivery_adapter_for(source) if getattr(self, "adapters", None) else None
        rename_room = getattr(adapter, "rename_room", None)
        if rename_room is None:
            return
        room_name = self._sanitize_matrix_room_title(title)
        logger.info(
            "matrix inbox-room rename: room=%s new_title=%r", str(source.chat_id), room_name)
        try:
            renamed = await rename_room(
                str(source.chat_id), room_name,
                only_if_current_name=getattr(source, "auto_thread_initial_name", None))
            logger.info(
                "matrix inbox-room rename result: room=%s applied=%s", str(source.chat_id), bool(renamed))
        except TypeError:
            logger.warning(
                "Matrix room rename raised TypeError (adapter=%s)", type(adapter).__name__, exc_info=True)
        except Exception:
            logger.debug("Failed to rename Matrix inbox room for generated session title", exc_info=True)

    # ── title-thread → loop rename scheduling ───────────────────────────────────────────────

    def _schedule_rename_from_title_thread(self, source: SessionSource, make_coro, label: str) -> None:
        """Schedule a best-effort rename coroutine onto the gateway loop from the auto-title thread.
        The source is copied so the thread never shares the live dataclass with the loop; failures
        are logged at debug and never propagate."""
        from gateway.run import safe_schedule_threadsafe
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "_gateway_loop", None)
        if loop is None or loop.is_closed():
            return
        copied_source = source
        with suppress(Exception):
            # Keep the live transport owner and identity; multiplex routes may run under a
            # profile that does not own the Discord adapter/token.
            from gateway.session_identity import replace_source
            copied_source = replace_source(source)
        future = safe_schedule_threadsafe(
            make_coro(copied_source), loop, logger=logger, log_message=f"{label} failed to schedule",
        )

        def _log_rename_failure(fut) -> None:
            try:
                fut.result()
            except Exception:
                logger.debug("%s failed", label, exc_info=True)

        if future is not None:
            future.add_done_callback(_log_rename_failure)

    def _schedule_discord_semantic_thread_rename(self, source: SessionSource, session_id: str, title: str) -> None:
        """Schedule Discord auto-thread rename from the auto-title background thread."""
        if not title:
            return
        if not self._is_discord_auto_thread_lane(source):
            # Relay title turn: the source is the PARENT channel event (thread didn't exist at
            # ingest). The auto-title races the delivery that fills the send-result cache, so a
            # miss HERE is not a verdict: schedule whenever the SHAPE matches; the async rename
            # lane polls the cache (bounded wait) and no-ops on a true miss. relay_info is NOT
            # pre-captured: at schedule time the prospective stamp is known but the reply send
            # has not completed, and the cache fallback may still hold a stale sibling's entry.
            # Passing either in would skip _await_relay_auto_thread_info's wait-for-the-reply
            # and rename a thread that was never (or wrongly) created.
            if (
                self._relay_auto_thread_info(source) is None
                and not self._is_relay_discord_channel_lane(source)
            ):
                return
        self._schedule_rename_from_title_thread(
            source,
            lambda copied: self._rename_discord_auto_thread_for_session_title(
                copied, session_id, title
            ),
            "Discord semantic thread rename",
        )
