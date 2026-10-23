"""Discord Claude delegation single-message rail (sibling of ``gateway/run.py``).

Owns this feature's correlation/notice helpers: the standalone
delegate_claude_agent start notice keyed by the REAL native tool-call id,
and the same-ID combination of that notice with the spawned run's viewer
card. The FIFO, its consumer loop, the cancellation drain, the generic
start/status callbacks and the delivery seam (``_deliver_agent_status_marker``)
stay owned by ``gateway/run.py``; this sibling only produces and combines,
through callables the facade hands it (thin wiring there, per AGENTS.md).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from gateway.config import Platform

logger = logging.getLogger(__name__)

# Standalone delegate_claude_agent start notice, queued by the ID-bearing
# native tool-start callback (combined_tool_start_callback) so the notice is
# delivered as its own message — keyed by the REAL tool-call id — and can be
# edited in place into the single combined card when that call's viewer
# marker arrives. See ``queue_claude_delegation_start``.
CLAUDE_DELEGATION_START_MARKER = "__claude_delegation_start__"


def is_claude_delegation_start_marker(raw: Any) -> bool:
    return (
        isinstance(raw, tuple)
        and len(raw) == 3
        and raw[0] == CLAUDE_DELEGATION_START_MARKER
    )


def claude_delegation_single_status(ctx) -> bool:
    """True when this turn's Discord Claude delegations use the
    single-message rail: a standalone ID-correlated start notice that
    the viewer marker later edits in place. Requires an editable
    progress adapter (the edit is the whole point) and the FIFO to be
    live; every other platform/mode keeps the ordinary row + standalone
    viewer card."""
    # The FIFO also exists when only thinking is enabled; only the
    # authoritative progress gate entitles delegations to the rail.
    from gateway.run import _adapter_can_edit_progress

    return bool(
        ctx.tool_progress_enabled
        and ctx.progress_queue
        and ctx.source.platform == Platform.DISCORD
        and ctx._status_adapter is not None
        and _adapter_can_edit_progress(ctx._status_adapter)
    )


def queue_claude_delegation_start(ctx, call_id, tool_name, args) -> None:
    """Queue an ID-correlated standalone delegation notice for Discord.

    Rides the authoritative native start callback (real tool-call id —
    the name-correlated text rail cannot distinguish concurrent
    delegations). The generic progress row for this call is suppressed
    (see the facade's progress_callback); this marker delivers the notice
    as its own message and the consumer records the sent message id, so
    the spawned run's viewer marker later EDITs that exact message into
    the single combined card instead of posting a second one.
    """
    if not ctx.progress_queue or not ctx._run_still_current():
        return
    try:
        _agent = ctx.agent_holder[0] if ctx.agent_holder else None
        if _agent is not None and getattr(_agent, "is_interrupted", False):
            return
    except Exception:
        pass
    call_id = str(call_id or "")
    if not call_id:
        return
    ctx.progress_queue.put((
        CLAUDE_DELEGATION_START_MARKER,
        call_id,
        claude_delegation_notice_text(str(tool_name or ""), args or {}),
    ))


def claude_delegation_notice_text(tool_name: str, args: dict) -> str:
    """Format the delegation notice like the ordinary progress row.

    Uses the same display helper the describe modes use for this tool —
    its curated phrase ("Delegating to a Claude agent in <workdir>") —
    so the single-message rail's notice reads like the ordinary
    delegation notice it replaces, workdir surfaced, task prompt never
    rendered.
    """
    from agent.display import describe_tool_call, get_tool_emoji

    emoji = get_tool_emoji(tool_name, default="⚙️")
    return f"{emoji} {describe_tool_call(tool_name, args)}"


class ClaudeDelegationDelivery:
    """The consumer-side same-ID notice/viewer combination for one
    ``send_progress_messages`` loop (live iterations and the
    cancellation drain alike).

    Everything platform-facing goes through the facade-owned seams handed
    in at construction — ``deliver_status_marker`` (the standalone status
    send), ``flush_before_status`` (flush pending tool rows), ``reset_bubble``
    (start a fresh editable progress bubble) and ``agent_interrupted`` —
    so delivery, cleanup tracking and rendering are byte-identical to the
    facade's other rails.
    """

    def __init__(
        self,
        ctx,
        adapter,
        deliver_status_marker: Callable[[tuple], Any],
        flush_before_status: Callable[[], Any],
        reset_bubble: Callable[[], None],
        agent_interrupted: Callable[[], bool],
    ):
        self._ctx = ctx
        self._adapter = adapter
        self._deliver_status_marker = deliver_status_marker
        self._flush_before_status = flush_before_status
        self._reset_bubble = reset_bubble
        self._agent_interrupted = agent_interrupted

    async def deliver_start(self, raw: tuple) -> None:
        """Send one delegation's standalone notice; remember its message id.

        The notice goes out through the same door as agent-viewer markers
        (``_deliver_agent_status_marker``) so platform rendering and
        cleanup tracking are unchanged. Only a SUCCESSFUL send records
        the (message id, notice text) pair under the call's native
        tool-call id — a failed notice leaves the later viewer marker on
        its legacy standalone delivery.
        """
        from gateway.run import _AGENT_STATUS_QUEUE_MARKER

        ctx = self._ctx
        _kind, call_id, notice_text = raw
        if not self._agent_interrupted():
            await self._flush_before_status()
        message_id = await self._deliver_status_marker(
            (_AGENT_STATUS_QUEUE_MARKER, "agent.status", notice_text)
        )
        if message_id:
            ctx._claude_delegation_messages[call_id] = {
                "id": str(message_id),
                "text": notice_text,
            }
        # Fresh bubble below the delivered notice, exactly like the
        # viewer-marker rail, so later tool rows edit a message BELOW it.
        self._reset_bubble()

    async def deliver_viewer_marker(self, raw: tuple) -> None:
        """Deliver one queued branded agent-viewer marker (shared by the
        live loop and the cancellation drain).

        A Discord Claude delegation whose notice was already delivered by
        the correlated start marker does NOT get a second message: that
        exact recorded message id is EDITED in place into the single
        combined card (notice text stays as the message content, the
        already-validated viewer line renders as the branded embed via
        the Discord adapter's narrowly metadata-gated edit path). A
        duplicate viewer event for a call whose card went out is dropped;
        success is marked only AFTER a successful edit or delivery. A
        failed edit falls back to the legacy standalone delivery and
        never touches the delivered notice text. Every other case
        (Cursor, other platforms, uncorrelated markers) keeps the
        standalone delivery byte-identical to before.
        """
        from tools.tool_status import CLAUDE_AGENT_VIEWER_STATUS_PREFIX

        ctx = self._ctx
        content = raw[2]
        call_id = str(raw[3]) if len(raw) > 3 and raw[3] else ""
        if (
            call_id
            and ctx.source.platform == Platform.DISCORD
            and content.startswith(CLAUDE_AGENT_VIEWER_STATUS_PREFIX)
        ):
            if call_id in ctx._agent_viewer_cards_sent:
                return
            entry = ctx._claude_delegation_messages.get(call_id)
            _edit = getattr(self._adapter, "edit_message", None)
            if entry and callable(_edit):
                combined = f"{entry['text']}\n{content}"
                edit_result = None
                try:
                    edit_result = await ctx.deliver_visible(lambda: _edit(
                        ctx._status_chat_id,
                        entry["id"],
                        combined,
                        metadata={"claude_agent_progress_combined": True},
                    ))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        "combined delegation card edit failed",
                        exc_info=True,
                    )
                if getattr(edit_result, "success", False):
                    # Dedup only after success: a failed edit left the
                    # notice intact and a later retry free.
                    ctx._agent_viewer_cards_sent.add(call_id)
                    return
        if not self._agent_interrupted():
            await self._flush_before_status()
        delivered_id = await self._deliver_status_marker(raw)
        if call_id and delivered_id:
            ctx._agent_viewer_cards_sent.add(call_id)
        # Start a fresh progress bubble so later tool lines edit a
        # message BELOW the notice instead of the one above it.
        self._reset_bubble()
