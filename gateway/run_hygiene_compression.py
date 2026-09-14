"""Session-hygiene compression Discord episode-card lifecycle for GatewayRunner.

Split out of ``gateway/run.py`` (Codex review P1 on PR #221): the tool-style
``context_compress`` episode card for the gateway's pre-agent session-hygiene
compression pass lives here instead of enlarging the facade. Bound onto
``GatewayRunner`` via the MRO. ``gateway.run`` internals are imported lazily
inside method bodies (import cycle), so ``patch("gateway.run.X")`` keeps
intercepting them at call time.

Presentation-only twin of the in-agent rail: the hygiene pass runs in a
detached ``AIAgent`` whose platform is deliberately stale
(``_GATEWAY_HYGIENE_PLATFORM``) and has no ``status_callback``, so
``emit_compression_tool_status`` can never fire for it. The gateway — which
already holds the source adapter, session key, and thread metadata — drives
the SAME episode machinery under its OWN ``hygiene``-railed key, so a
hygiene attempt gets the identical send-once/edit-in-place
``context_compress`` card WITHOUT sharing episode state with the agent-side
rail on the same chat.

No compression policy lives here: triggers, timeouts, fencing, cooldowns,
and commit/adoption control flow stay in the facade's
``_handle_message_with_agent`` hygiene block, which calls into this module
at each lifecycle edge (start / deferred / adoption / timeout / unwind /
inline terminal). All user-facing line text comes from the
``agent/compression_status.py`` builders (single source of truth). Success
lines are fed from the hygiene compressor's own telemetry
(``_last_compression_telemetry`` / ``_last_summary_fallback_used`` /
``_last_feasibility_skip``) exactly like the agent-side rail
(``agent/conversation_compression.py``), so the card reports the actually
selected provider/model and a deterministic LOCAL summary is labelled as
such — never credited to the failed/skipped provider.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from agent.async_utils import safe_schedule_threadsafe
from agent.compression_status import (
    COMPRESSION_TOOL_STATUS_EVENT,
    compression_tool_aborted_line,
    compression_tool_deferred_line,
    compression_tool_failure_line,
    compression_tool_start_line,
    compression_tool_success_line,
)
from agent.i18n import t

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


class GatewayHygieneCompressionMixin:
    """Discord episode-card lifecycle for gateway session-hygiene compression."""

    async def _hygiene_compression_episode_update(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        line: str,
        attempt_token: Optional[str],
    ) -> bool:
        """Best-effort Discord episode-card update for session-hygiene compression.

        Presentation-only twin of the in-agent rail: hygiene compression runs
        in a detached ``AIAgent`` whose platform is deliberately stale
        (``_GATEWAY_HYGIENE_PLATFORM``) and has no ``status_callback``, so
        ``emit_compression_tool_status`` can never fire for it. The gateway —
        which already holds the source adapter, session key, and thread
        metadata — drives the SAME episode machinery under its OWN
        ``hygiene``-railed key, so a hygiene attempt gets the identical
        send-once/edit-in-place ``context_compress`` card WITHOUT sharing
        episode state with the agent-side rail on the same chat: an open
        hygiene card can never drop the agent rail's raw warnings or hand its
        bubble to an agent attempt (and vice versa). All line text comes
        from the ``agent/compression_status.py`` builders.

        Every hygiene emission point runs on the event loop (the summary
        itself is the only executor-threaded part), so no thread-safe hop is
        needed here; the detached done-callback schedules this coro with
        ``safe_schedule_threadsafe``. Returns True when the episode rail owns
        this chat's compression surface (Discord) — callers then skip the
        legacy plain notice for the same edge. Never raises and never touches
        compression policy, timeouts, fencing, or cooldowns.
        """
        from gateway.run import (
            _COMPRESSION_EPISODE_RAIL_HYGIENE,
            _adapter_uses_compression_episode_rail,
            _compression_episode_decide,
            _send_or_update_compression_episode_coro,
        )

        try:
            chat_id = getattr(source, "chat_id", None)
            if not chat_id:
                return False
            adapter = self._adapter_for_source(source)
            if not _adapter_uses_compression_episode_rail(adapter):
                return False
            action, content = _compression_episode_decide(
                adapter,
                chat_id,
                COMPRESSION_TOOL_STATUS_EVENT,
                line,
                session_key=session_key,
                attempt_token=attempt_token,
                rail=_COMPRESSION_EPISODE_RAIL_HYGIENE,
            )
            if action == "deliver" and content:
                await _send_or_update_compression_episode_coro(
                    adapter,
                    chat_id,
                    content,
                    metadata,
                    session_key=session_key,
                    rail=_COMPRESSION_EPISODE_RAIL_HYGIENE,
                )
            return True
        except Exception:
            logger.debug(
                "hygiene compression episode update failed", exc_info=True
            )
            return False

    # ------------------------------------------------------------------
    # Line construction (P2: compressor attribution)
    # ------------------------------------------------------------------

    @staticmethod
    def _hygiene_episode_success_line(
        agent: Any,
        *,
        before_messages: Optional[int] = None,
        after_messages: Optional[int] = None,
        before_tokens: Optional[int] = None,
        after_tokens: Optional[int] = None,
    ) -> str:
        """Terminal success line fed from the hygiene compressor's own telemetry.

        Mirrors the agent-side rail's success emission
        (``agent/conversation_compression.py``): the route is named only when
        ``call_llm`` actually recorded it (``aux_route_known``) — telemetry's
        provider/model fallbacks are config guesses, not proof of summarizer
        identity. When the engine inserted its deterministic LOCAL summary
        (``_last_summary_fallback_used``), no route is shown and the line
        labels the local summary (``"skipped"`` for a feasibility skip,
        ``"unavailable"`` otherwise): the failed/skipped provider must not be
        credited with a summary it did not produce.
        """
        compressor = getattr(agent, "context_compressor", None)
        telemetry = getattr(compressor, "_last_compression_telemetry", None)
        if not isinstance(telemetry, dict):
            telemetry = {}
        route_known = bool(telemetry.get("aux_route_known"))
        local_summary: Optional[str] = None
        if getattr(compressor, "_last_summary_fallback_used", False):
            local_summary = (
                "skipped"
                if getattr(compressor, "_last_feasibility_skip", False)
                else "unavailable"
            )
        return compression_tool_success_line(
            (
                telemetry.get("aux_provider")
                if route_known and not local_summary
                else None
            ),
            (
                telemetry.get("aux_model")
                if route_known and not local_summary
                else None
            ),
            before_messages=before_messages,
            after_messages=after_messages,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            local_summary=local_summary,
        )

    # ------------------------------------------------------------------
    # Lifecycle edges called from the facade's hygiene block
    # ------------------------------------------------------------------

    async def _hygiene_episode_emit_start(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        attempt_token: str,
    ) -> bool:
        """Card posted the moment the hygiene summary work begins (executor
        spawned). Non-terminal start line; later edges edit it in place."""
        return await self._hygiene_compression_episode_update(
            source=source,
            session_key=session_key,
            metadata=metadata,
            line=compression_tool_start_line(),
            attempt_token=attempt_token,
        )

    async def _hygiene_episode_send_plain_notice(
        self,
        source: Any,
        metadata: Any,
        text: str,
        log_label: str,
    ) -> None:
        """Legacy one-off notice for chats the episode rail does not own
        (non-Discord adapters) — byte-identical to the pre-card behavior."""
        try:
            adapter = self._adapter_for_source(source)
            if adapter and source.chat_id:
                await adapter.send(source.chat_id, text, metadata=metadata)
        except Exception as err:
            logger.warning(
                "Failed to deliver %s to user: %s", log_label, err
            )

    async def _hygiene_episode_emit_deferred_or_notice(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        attempt_token: str,
    ) -> bool:
        """ONE non-terminal edit to the deferred ("still running in the
        background") state after the turn-hold budget released the turn while
        the watermark-fenced worker keeps its commit admission — the adoption
        or did-not-commit boundary later replaces it with the real terminal.
        On Discord this replaces the plain deferral notice; everywhere else
        the notice posts exactly as before."""
        owned = await self._hygiene_compression_episode_update(
            source=source,
            session_key=session_key,
            metadata=metadata,
            line=compression_tool_deferred_line(),
            attempt_token=attempt_token,
        )
        if not owned:
            await self._hygiene_episode_send_plain_notice(
                source,
                metadata,
                t("gateway.compress.turnhold_deferred"),
                "compression-turnhold notice",
            )
        return owned

    async def _hygiene_episode_emit_turnhold_cancelled_or_notice(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        attempt_token: str,
    ) -> bool:
        """The unfenced attempt was fence-CANCELLED at the turn-hold budget —
        a failure terminal (⚠️), never a deferral. On Discord it replaces the
        plain notice; everywhere else the notice posts exactly as before."""
        owned = await self._hygiene_compression_episode_update(
            source=source,
            session_key=session_key,
            metadata=metadata,
            line=compression_tool_aborted_line(
                "turn-hold budget expired — attempt cancelled"
            ),
            attempt_token=attempt_token,
        )
        if not owned:
            await self._hygiene_episode_send_plain_notice(
                source,
                metadata,
                t("gateway.compress.turnhold_deferred"),
                "compression-turnhold notice",
            )
        return owned

    async def _hygiene_episode_emit_timeout_terminal(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        attempt_token: str,
        fence_cancelled: bool,
        timeout_error: str,
    ) -> bool:
        """Timeout-cancel is a failure terminal (⚠️) — fence cancels and
        silent workers alike. Returns whether the rail owns this chat's
        compression surface; the facade posts its legacy timeout warning
        only when it does not."""
        return await self._hygiene_compression_episode_update(
            source=source,
            session_key=session_key,
            metadata=metadata,
            line=(
                compression_tool_aborted_line("cancelled at commit fence")
                if fence_cancelled
                else compression_tool_failure_line(timeout_error)
            ),
            attempt_token=attempt_token,
        )

    def _hygiene_episode_schedule_unwind_terminal(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        attempt_token: str,
        loop: Any,
    ) -> None:
        """Close the open card as stopped on a non-timeout unwind (mid-flight
        unwind cannot prove the transcript survived, so the line claims
        nothing). Scheduled detached so the unwind itself is never delayed by
        delivery."""
        safe_schedule_threadsafe(
            self._hygiene_compression_episode_update(
                source=source,
                session_key=session_key,
                metadata=metadata,
                line=compression_tool_aborted_line(
                    "cancelled",
                    context_preservation=None,
                ),
                attempt_token=attempt_token,
            ),
            loop,
            logger=logger,
            log_message=(
                "hygiene compression episode "
                "unwind scheduling error"
            ),
        )

    async def _hygiene_episode_emit_inline_terminal(
        self,
        *,
        source: Any,
        session_key: Any,
        metadata: Any,
        attempt_token: str,
        agent: Any,
        rotated: bool,
        in_place: bool,
        aborted: bool,
        fence_cancelled: bool,
        before_messages: int,
        after_messages: int,
        before_tokens: Optional[int],
        after_tokens: Optional[int],
    ) -> bool:
        """Episode card terminal for the inline (awaited) path: ✅ ONLY for a
        committed rotate/in-place compaction — every did-not-commit outcome
        (summary abort, fence-cancelled no-op, anti-growth refusal, missing
        session_db) closes the card as ⚠️. A no-op is never a success.

        Returns True only when the rail owns this chat AND the failure
        terminal was emitted (the facade's legacy abort warning posts only in
        that case's absence).
        """
        owned = False
        if rotated or in_place:
            await self._hygiene_compression_episode_update(
                source=source,
                session_key=session_key,
                metadata=metadata,
                line=self._hygiene_episode_success_line(
                    agent,
                    before_messages=before_messages,
                    after_messages=after_messages,
                    before_tokens=before_tokens,
                    after_tokens=after_tokens,
                ),
                attempt_token=attempt_token,
            )
        elif aborted and not fence_cancelled:
            # The card's failure terminal carries the sanitized reason +
            # preservation fact; the facade's raw warning still posts
            # wherever the rail is absent.
            compressor = getattr(agent, "context_compressor", None)
            owned = await self._hygiene_compression_episode_update(
                source=source,
                session_key=session_key,
                metadata=metadata,
                line=compression_tool_failure_line(
                    getattr(compressor, "_last_summary_error", None)
                ),
                attempt_token=attempt_token,
            )
        else:
            await self._hygiene_compression_episode_update(
                source=source,
                session_key=session_key,
                metadata=metadata,
                line=compression_tool_aborted_line(
                    "cancelled at commit fence"
                    if fence_cancelled
                    else "did not rotate or compact in place"
                ),
                attempt_token=attempt_token,
            )
        return owned

    # ------------------------------------------------------------------
    # Deferred (post-turn-hold) adoption boundary
    # ------------------------------------------------------------------

    def _hygiene_deferred_adoption_callback(
        self,
        *,
        session_id: Any,
        session_key: Any,
        agent: Any,
        source: Any,
        metadata: Any,
        loop: Any,
        attempt_token: str,
        before_messages: int,
        before_tokens: int,
    ):
        """Done-callback for the watermark-fenced deferred hygiene worker.

        Fires when the detached worker exits after the turn-hold released the
        turn: adopt the summary at the watermark-fenced commit boundary
        (success terminal, ✅) or, when nothing committed, restore the
        pre-#97963 flat retry spacing and close the card as stopped (⚠️,
        never success). The gateway (``self``) is captured by the closure; it
        only reads state and schedules the episode coro thread-safely.
        """

        def _hyg_adopt_or_space_retry(_fut):
            from agent.model_metadata import estimate_messages_tokens_rough

            from gateway.run import (
                _HYGIENE_TURNHOLD_RETRY_SECONDS,
                _record_hygiene_cooldown,
                _reset_hygiene_failure_streak,
            )

            _gw = self
            _sid = session_id
            _skey = session_key
            _agent = agent
            _src = source
            _meta = metadata
            _loop = loop
            _tok = attempt_token
            _pre_msgs = before_messages
            _pre_toks = before_tokens
            try:
                _exc = _fut.exception()
            except (
                asyncio.CancelledError,
                Exception,
            ):
                _exc = None
                _committed = False
            else:
                _committed = _exc is None and (
                    bool(
                        getattr(
                            _agent,
                            "_last_compaction_in_place",
                            False,
                        )
                    )
                    or getattr(
                        _agent, "session_id", _sid
                    )
                    != _sid
                )
            if _committed:
                logger.info(
                    "Session hygiene compression for "
                    "session %s finished after the "
                    "turn-hold was released — summary "
                    "adopted at the watermark-fenced "
                    "commit boundary (#97963)",
                    _sid,
                )
                try:
                    _reset_hygiene_failure_streak(
                        _gw, _skey
                    )
                except Exception as _rs_err:
                    logger.debug(
                        "hygiene streak reset after "
                        "deferred adoption failed: %s",
                        _rs_err,
                    )
                # Episode card: the ONLY success terminal for a deferred
                # attempt — fired at the watermark-fenced adoption boundary
                # itself, not when the turn was released. The done-callback
                # is sync on the loop, so the coro is scheduled thread-safely
                # (it is also safe from a worker thread that resolved the
                # future). The line carries the compressor's own telemetry
                # (provider/model when the route is known; a local
                # deterministic summary labelled as such).
                try:
                    _adopted = _fut.result()
                    _adopted_msgs = (
                        _adopted[0]
                        if isinstance(
                            _adopted, tuple
                        )
                        and _adopted
                        else None
                    )
                except Exception:
                    _adopted_msgs = None
                safe_schedule_threadsafe(
                    _gw._hygiene_compression_episode_update(
                        source=_src,
                        session_key=_skey,
                        metadata=_meta,
                        line=_gw._hygiene_episode_success_line(
                            _agent,
                            before_messages=_pre_msgs,
                            after_messages=(
                                len(_adopted_msgs)
                                if isinstance(
                                    _adopted_msgs,
                                    list,
                                )
                                else None
                            ),
                            before_tokens=(
                                _pre_toks or None
                            ),
                            after_tokens=(
                                estimate_messages_tokens_rough(
                                    _adopted_msgs
                                )
                                if isinstance(
                                    _adopted_msgs,
                                    list,
                                )
                                else None
                            ),
                        ),
                        attempt_token=_tok,
                    ),
                    _loop,
                    logger=logger,
                    log_message=(
                        "hygiene compression episode "
                        "adoption scheduling error"
                    ),
                )
            else:
                # Nothing to adopt (summary failed, fence refused the commit,
                # or the attempt was superseded). Restore the pre-#97963
                # spacing so sustained traffic does not spawn and abandon a
                # fresh compressor every turn. Flat and non-escalating: the
                # streak must not advance for a deferral.
                _record_hygiene_cooldown(
                    _gw, _sid,
                    _HYGIENE_TURNHOLD_RETRY_SECONDS,
                    "hygiene compression deferred: "
                    "turn-hold budget expired and the "
                    "detached attempt did not commit",
                )
                # Episode card: the deferred attempt ended WITHOUT committing
                # (summary failed, fence refused the commit, or the attempt
                # was superseded) — close the card as stopped, never as
                # success.
                safe_schedule_threadsafe(
                    _gw._hygiene_compression_episode_update(
                        source=_src,
                        session_key=_skey,
                        metadata=_meta,
                        line=compression_tool_aborted_line(
                            _exc
                            if _exc is not None
                            else "detached attempt "
                            "did not commit"
                        ),
                        attempt_token=_tok,
                    ),
                    _loop,
                    logger=logger,
                    log_message=(
                        "hygiene compression episode "
                        "terminal scheduling error"
                    ),
                )

        return _hyg_adopt_or_space_retry
