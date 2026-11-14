"""Per-turn callback runner (progress/status/voice/run_sync) for the gateway agent turn.

``TurnRunner`` owns the per-turn callbacks ``GatewayRunner._run_agent_inner`` binds. ``gateway.run``
internals are imported lazily inside method bodies (import cycle), so ``patch("gateway.run.X")``
keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import queue
import re
import threading
import time
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.interrupt_compat import _accepts_keyword
from agent.replay_cleanup import canonicalize_replay_history
from gateway.config import Platform
from gateway.media_repair import repair_explicit_computer_use_media_paths
from gateway.runtime_footer import turn_metrics
from gateway.platforms.base import BasePlatformAdapter
from gateway.turn_context import TurnContext
from hermes_cli.config import cfg_get
from utils import is_truthy_value

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

# Consecutive turns a session's persisted transcript may lag its live cached history before
# _load_turn_history escalates the lag line from WARNING to ERROR (#114266: 11 days of WARNING).
_TRANSCRIPT_LAG_ESCALATION_TURNS = 3

# Exact refusals retained for older adapters/connectors without destination preflight.
# Substring matching would also silence transient thread-resolution errors.
_CARD_DESTINATION_REFUSALS = {
    "No Slack thread target",
    "slack task_card requires a thread anchor",
    "slack task_card requires a thread anchor (Slack streams are thread replies)",
}



# Rendered on a native clarify card whose wait ended without a click (mirrors the notice the
# Slack click handler shows on a dead entry).
_CLARIFY_EXPIRED_NOTICE = "⏳ This prompt expired — please send a new request."



class _TurnRunnerCore:
    """Sole owner of the live TurnRunner constructor."""

    def __init__(self, runner: "GatewayRunner", ctx: TurnContext) -> None:
        self._runner = runner
        self._ctx = ctx


class TurnRunner(_TurnRunnerCore):
    """Per-turn collaborator carrying ``GatewayRunner._run_agent_inner``'s tool-progress callbacks."""

    # ── shared thread→loop plumbing ─────────────────────────────────────────────────────────

    def _schedule(self, coro, log_message: str, loop=None):
        """Hop a coroutine from the agent's sync worker thread onto the gateway loop."""
        from gateway.run import safe_schedule_threadsafe
        return safe_schedule_threadsafe(
            coro, self._ctx._loop_for_step if loop is None else loop, logger=logger, log_message=log_message,
        )

    def _agent_interrupted(self) -> bool:
        """True once the user sent `stop` (agent_holder[0] is the shared agent handle)."""
        try:
            agent = self._ctx.agent_holder[0] if self._ctx.agent_holder else None
            return bool(agent is not None and getattr(agent, "is_interrupted", False))
        except Exception:
            return False

    def _stream_consumer(self):
        holder = self._ctx.stream_consumer_holder
        return holder[0] if holder else None

    def _drain_progress_queue(self) -> None:
        q = self._ctx.progress_queue
        with suppress(Exception):
            while not q.empty():
                raw = q.get_nowait()
                # Rows are discarded by design on this rail; a context-flush
                # marker's waiter must still be released.
                if self._is_flush_marker(raw):
                    self._signal_flush_marker(raw)

    def _track_progress_result(self, result) -> None:
        """Remember a delivered progress/status message id for end-of-turn cleanup."""
        ctx = self._ctx
        if ctx._cleanup_progress and getattr(result, "success", False) and getattr(result, "message_id", None):
            ctx._cleanup_msg_ids.append(str(result.message_id))

    def _track_future_cleanup_id(self, fut) -> None:
        try:
            res = fut.result()
        except Exception:
            return
        self._track_progress_result(res)

    # ── progress_callback (agent thread → progress queue) ───────────────────────────────────

    def progress_callback(self, event_type: str, tool_name: str = None, preview: str = None, args: dict = None, **kwargs):
        """Callback invoked by agent on tool lifecycle events."""
        ctx = self._ctx
        # Failed subagent → one clean user-facing notice, handled FIRST, before every progress-queue
        # gate: platforms with tool_progress off must still hear about a dead delegation.
        if event_type == "subagent.complete":
            self._progress_subagent_notice(preview, kwargs)
            return
        self._progress_live_status(event_type, tool_name, args)
        # "log" mode: append tool.started lines to the log queue, silent in chat. Handled before
        # the progress_queue guard because log mode runs without a chat progress queue.
        if ctx.log_queue is not None and event_type == "tool.started" and tool_name and tool_name != "_thinking":
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            preview_str = f' "{preview}"' if preview else ""
            ctx.log_queue.put(f"{ts}  {tool_name}:{preview_str}".rstrip())
        if not ctx.progress_queue or not ctx._run_still_current():
            return
        if event_type == "context.injected":
            self._progress_context_injection(args)
            return
        if event_type == "tool.completed" and not ctx.long_tool_hint_fired[0]:
            self._progress_onboarding_hint(kwargs)
            return
        # "_thinking" is assistant scratch text between tool calls, never ordinary tool progress:
        # only relayed when the platform explicitly opted into thinking_progress.
        if event_type == "_thinking" or tool_name == "_thinking":
            thinking_text = (preview if tool_name == "_thinking" else tool_name) if ctx._thinking_enabled else None
            if thinking_text:
                ctx.progress_queue.put(f"💬 {thinking_text}")
            return
        # Native task cards consume the ID-bearing tool_start/tool_complete callbacks instead;
        # name-correlated text events would duplicate cards and mispair concurrent same-tool calls.
        if ctx._native_slack_task_cards and event_type in {"tool.started", "tool.completed"}:
            return
        # Structured MoA stage embeds (Discord): send_tool_stage_embeds already
        # renders exactly one self-editing card per moa_ask/moa_debate invocation,
        # so the ordinary plain-text bubble for the same invocation's start would
        # be a duplicate status message. Skip only the bubble; the stage card
        # pipeline itself is untouched. Adapters without the stage-embed
        # capability (every non-card platform) fall through to normal progress.
        if (
            ctx._stage_embed_adapter is not None
            and event_type == "tool.started"
            and tool_name in ("moa_ask", "moa_debate")
        ):
            return
        # tool_progress off → only _thinking passes (above). Only tool.started renders. clarify:
        # send_clarify IS the user-facing rendering (a bubble would duplicate it, and verbose mode
        # would dump the raw args JSON right under the prompt). Post-`stop`: N parallel tool calls
        # fire N tool.started events before the interrupt check, so a late stop must not render them.
        if (
            not ctx.tool_progress_enabled
            or event_type != "tool.started"
            # The adapter's send_clarify IS the user-facing rendering (interactive buttons or the
            # numbered-text fallback), so a progress bubble is pure duplication — and in verbose mode it
            # dumps the raw tool-call args JSON ({"question": ..., "choices": [...]}) into the chat
            # (#52374). ``restart`` was suppressed alongside it (#52374, #529) on the theory that the
            # restart lifecycle notices owned the user-facing presentation — but under the
            # overlap-restart architecture the requester turn gets no lifecycle message until after
            # the bounce, so the suppressed bubble left restart turns visibly silent. ``restart``
            # now renders like any other tool (directive 2026-11-09); only ``clarify`` stays
            # suppressed here.
            or tool_name == "clarify"
            or self._agent_interrupted()
        ):
            return
        # "new" mode: only report when tool changes
        if ctx.progress_mode == "new" and tool_name == ctx.last_tool[0]:
            return
        ctx.last_tool[0] = tool_name
        msg = self._progress_build_message(tool_name, preview, args)
        if msg is not None:
            from gateway.run_claude_progress import claude_delegation_single_status

            suppress_generic = (
                tool_name == "delegate_claude_agent" and claude_delegation_single_status(ctx)
            )
            if not suppress_generic:
                self._progress_emit(msg)

    def _progress_context_injection(self, args) -> None:
        """Render model-received context through the ordered progress queue."""
        from gateway.run import (
            _format_context_injection_progress,
            _platform_config_key,
            _resolve_gateway_display_bool,
        )

        ctx = self._ctx
        if not ctx.tool_progress_enabled or not isinstance(args, dict):
            return
        content = args.get("content")
        if not isinstance(content, str) or not content:
            return
        adapter = self._runner._delivery_adapter_for(ctx.source)
        if adapter is None:
            return

        def declared_callable(name: str):
            try:
                inspect.getattr_static(adapter, name)
                candidate = getattr(adapter, name)
            except Exception:
                return None
            return candidate if callable(candidate) else None

        chat_limit_fn = declared_callable("max_message_length_for_chat")
        try:
            message_limit = int(
                chat_limit_fn(ctx.source.chat_id)
                if chat_limit_fn is not None
                else getattr(adapter, "MAX_MESSAGE_LENGTH", 4000)
            )
        except Exception:
            message_limit = 4000
        message_len_fn = len
        chat_len_fn = declared_callable("message_len_fn_for_chat")
        if chat_len_fn is not None:
            try:
                candidate_len_fn = chat_len_fn(ctx.source.chat_id)
                if callable(candidate_len_fn):
                    message_len_fn = candidate_len_fn
            except Exception:
                pass

        platform = getattr(ctx.source, "platform", None)
        try:
            platform_key = _platform_config_key(platform) if platform is not None else "cli"
            include_content = _resolve_gateway_display_bool(
                ctx.user_config or {}, platform_key, "context_injection_content", default=True,
            )
        except Exception:
            platform_key = "cli"
            include_content = True
        sources = args.get("sources") if isinstance(args.get("sources"), list) else []
        if sources == ["gateway"]:
            try:
                if not _resolve_gateway_display_bool(
                    ctx.user_config or {}, platform_key, "gateway_context_notifications", default=True,
                ):
                    return
            except Exception:
                pass
        messages = _format_context_injection_progress(
            content=content,
            injected_chars=args.get("injected_chars", len(content)),
            sources=sources,
            message_limit=message_limit,
            supports_code_blocks=bool(getattr(adapter, "supports_code_blocks", False)),
            message_len_fn=message_len_fn,
            include_content=include_content,
            recalled_count=args.get("recalled_count"),
        )
        for message in messages:
            ctx.progress_queue.put(message)
        # The first visible assistant output waits until these cards are delivered.
        ctx._context_cards_queued[0] = True
        ctx.last_was_terminal_block[0] = False
        ctx.last_progress_msg[0] = messages[-1] if messages else None
        ctx.repeat_count[0] = 0

    def _progress_subagent_notice(self, preview, kwargs: dict) -> None:
        """Only terminal failure statuses render (same notice rail as credit warnings)."""
        ctx = self._ctx
        from gateway.warning_notifications import render_notification
        status = kwargs.get("status")
        try:
            from tools.delegate_tool import SUBAGENT_FAILURE_STATUSES, format_subagent_failure_line
            if status in SUBAGENT_FAILURE_STATUSES and ctx._run_still_current():
                line = format_subagent_failure_line(
                    kwargs.get("goal"), status, error=kwargs.get("summary") or preview,
                    duration_seconds=kwargs.get("duration_seconds"), failure_reason=kwargs.get("failure_reason"),
                )
                render_notification(
                    lambda: self._schedule(self._runner._deliver_platform_notice(ctx.source, line), "subagent failure notice scheduling error"),
                    platform=ctx.source.platform, user_config=ctx.user_config)
        except Exception:
            logger.debug("subagent failure notice failed", exc_info=True)

    def _progress_live_status(self, event_type: str, tool_name, args) -> None:
        """Live status line (Slack assistant status): stash the tool phrase on the adapter; the
        _keep_typing refresh renders it. Plain dict write, safe from the sync worker thread."""
        ctx = self._ctx
        adapter = ctx._live_status_adapter
        if adapter is None or ctx._live_status_mode == "off" or tool_name == "_thinking":
            return
        try:
            if event_type == "tool.started" and tool_name and ctx._run_still_current():
                from agent.display import build_status_phrase
                adapter.set_status_text(ctx.source.chat_id, build_status_phrase(tool_name, args if ctx._live_status_mode == "full" else None))
            elif event_type == "tool.completed":
                # Between tools the model is genuinely "thinking" again — revert to the static default.
                adapter.set_status_text(ctx.source.chat_id, None)
        except Exception as err:
            logger.debug("live status update failed: %s", err)

    def _progress_onboarding_hint(self, kwargs: dict) -> None:
        """First-touch onboarding: the first time a tool exceeds _LONG_TOOL_THRESHOLD_S while
        streaming every tool (progress_mode == "all"), append a one-time /verbose hint."""
        from gateway.run import _hermes_home, _load_gateway_config
        ctx = self._ctx
        try:
            if (kwargs.get("duration") or 0) >= ctx._LONG_TOOL_THRESHOLD_S and ctx.progress_mode == "all":
                from agent.onboarding import TOOL_PROGRESS_FLAG, is_seen, mark_seen, tool_progress_hint_gateway
                cfg = _load_gateway_config()
                gate_on = is_truthy_value(cfg_get(cfg, "display", "tool_progress_command"), default=False)
                if gate_on and not is_seen(cfg, TOOL_PROGRESS_FLAG):
                    ctx.long_tool_hint_fired[0] = True
                    ctx.progress_queue.put(tool_progress_hint_gateway())
                    mark_seen(_hermes_home / "config.yaml", TOOL_PROGRESS_FLAG)
        except Exception as err:
            logger.debug("tool-progress onboarding hint failed: %s", err)

    def _preview_cap(self) -> int:
        """This turn's tool_preview_length snapshot (default 40): the one-line preview
        budget for "all"/"new" modes. Turn-owned, never the process-global preview
        length — interleaved turns (Discord unlimited vs a capped platform) must not
        change each other's rendering."""
        pl = getattr(self._ctx, "tool_preview_max_len", 0)
        return pl if pl > 0 else 40

    def _progress_terminal_blocks(self, adapter, tool_name, args, emoji):
        """(full, short) fenced blocks for a terminal command on markdown platforms, else (None, None).

        No language tag: Slack mrkdwn renders it as a literal first code line. Verbose shows the FULL
        command; "all"/"new" truncate to one line capped at ``tool_preview_length``. Consecutive
        terminal calls drop the repeated header so back-to-back commands render as adjacent blocks.
        """
        if not (
            getattr(adapter, "supports_code_blocks", False) and tool_name == "terminal" and isinstance(args, dict)
            and isinstance(args.get("command"), str) and args["command"].strip()
        ):
            return None, None
        cmd_full = args["command"].rstrip()
        header = "" if self._ctx.last_was_terminal_block[0] else f"{emoji} {tool_name}\n"
        cap = self._preview_cap()
        lines = cmd_full.splitlines()
        cmd_short = lines[0] if lines else cmd_full
        if len(cmd_short) > cap:
            cmd_short = cmd_short[:cap - 3] + "..."
        elif len(lines) > 1:
            cmd_short += " ..."
        return f"{header}```\n{cmd_full}\n```", f"{header}```\n{cmd_short}\n```"

    def _progress_build_message(self, tool_name, preview, args) -> Optional[str]:
        """Render the progress line. Verbose mode queues directly (no dedup) and returns None."""
        ctx = self._ctx
        from agent.display import get_tool_emoji
        emoji = get_tool_emoji(tool_name, default="⚙️")
        # "describe": one short human phrase — the call's ``description`` argument
        # when present, else a concise verb label. Handled before every other
        # branch: an unlimited preview budget must NOT route describe through the
        # verbose full-argument renderer, and terminal never gets a code block.
        if ctx.progress_mode == "describe":
            from agent.display import describe_tool_call
            ctx.last_was_terminal_block[0] = False
            return f"{emoji} {describe_tool_call(tool_name, args)}"
        if ctx.progress_mode == "compact":
            if isinstance(args, dict) and len(args) == 1:
                args_str = json.dumps(
                    next(iter(args.values())), ensure_ascii=False, default=str
                )
            elif args:
                args_str = json.dumps(args, ensure_ascii=False, default=str)
            else:
                args_str = "{}"
            return f"{emoji} {tool_name}({args_str})"
        try:
            adapter = self._runner._delivery_adapter_for(ctx.source)
        except Exception:
            adapter = None
        code_full, code_short = self._progress_terminal_blocks(adapter, tool_name, args, emoji)
        # Full rendering — explicit "verbose", or an unlimited preview budget
        # (tool_preview_length <= 0): the whole command and ALL arguments, never
        # a tool-specific builder summary. The budget is this turn's snapshot,
        # so an interleaved capped-platform turn cannot re-cap it (and a Discord
        # turn cannot uncap a capped one). Explicit positive caps keep the
        # compact "all"/"new" preview path below.
        pl = getattr(ctx, "tool_preview_max_len", 0)
        verbose = ctx.progress_mode == "verbose" or pl <= 0
        code = code_full if verbose else code_short
        ctx.last_was_terminal_block[0] = code is not None
        if verbose:
            if code is None and args:
                args_str = json.dumps(args, ensure_ascii=False, default=str)
                # tool_preview_length 0 (default) = no truncation in verbose mode; the user asked
                # for full detail and platform message-length limits handle the rest.
                if pl > 0 and len(args_str) > pl:
                    args_str = args_str[:pl - 3] + "..."
                code = f"{emoji} {tool_name}({list(args.keys())})\n{args_str}"
            elif code is None:
                code = f"{emoji} {tool_name}: \"{preview}\"" if preview else f"{emoji} {tool_name}..."
            ctx.progress_queue.put(code)
            return None
        if code is not None:
            return code
        if not preview:
            return f"{emoji} {tool_name}..."
        from agent.display import get_tool_verb, prepare_tool_preview, tool_verb_connector, verb_drops_preview
        prepared = prepare_tool_preview(tool_name, args, fallback=preview, max_len=self._preview_cap())
        preview = adapter.format_tool_preview(prepared) if adapter is not None else prepared.text
        # Friendly labels: human-phrased line for built-in tools ("🔍 Searching the web for ...")
        # by prefixing the verb onto the computed preview, so the command/url/query is kept.
        verb = get_tool_verb(tool_name)
        if not verb:
            return f"{emoji} {tool_name}: \"{preview}\""
        return f"{emoji} {verb}" if verb_drops_preview(tool_name) else f"{emoji} {verb}{tool_verb_connector(tool_name)}{preview}"

    def _progress_emit(self, msg: str) -> None:
        """Dedup consecutive identical lines (execute_code boilerplate), then route to the native
        stream bubble when the consumer accepts tool progress, else the progress queue."""
        ctx = self._ctx
        sc = self._stream_consumer()
        native = sc is not None and getattr(sc, "accepts_tool_progress", False)
        if msg == ctx.last_progress_msg[0]:
            ctx.repeat_count[0] += 1
            if native:
                sc.on_tool_progress(f"{msg} (×{ctx.repeat_count[0] + 1})")
            else:
                ctx.progress_queue.put(("__dedup__", msg, ctx.repeat_count[0]))
            return
        ctx.last_progress_msg[0], ctx.repeat_count[0] = msg, 0
        if native:
            sc.on_tool_progress(msg)
        else:
            ctx.progress_queue.put(msg)

    # ── Slack-native task cards (progress-queue drain) ──────────────────────────────────────

    @dataclasses.dataclass
    class _TaskCardState:
        """Task-card rail state for ``_send_native_task_card_progress``."""
        adapter: Any
        tasks: Dict[str, Dict[str, str]] = dataclasses.field(default_factory=dict)
        task_order: List[str] = dataclasses.field(default_factory=list)
        fallback_msg_id: Optional[str] = None
        native_failed: bool = False
        # TERMINAL for the turn, distinct from native_failed: no later publication
        # in this turn may deliver task text through the native lane OR the text
        # fallback. Two causes, both properties of the destination rather than of
        # one attempt: the connector's egress guard refused the chat, or the chat
        # cannot host a card (no thread anchor). Declared rather than set
        # dynamically so the state is visible where it lives.
        publication_suppressed: bool = False
        anonymous_seq: int = 0

        @staticmethod
        def _compact(value: Any, limit: int = 120) -> str:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."

        def visible_tasks(self) -> List[Dict[str, str]]:
            return [self.tasks[task_id] for task_id in self.task_order[-8:]]

        def fallback_text(self) -> str:
            labels = {"in_progress": "running", "complete": "complete", "error": "error"}
            lines = [f"- {t['title']} - {labels.get(t['status'], t['status'])}" for t in self.visible_tasks()]
            return "Hermes is working\n" + "\n".join(lines)

        def _upsert(self, call_id: str, title: str) -> Dict[str, str]:
            if call_id not in self.tasks:
                self.task_order.append(call_id)
            self.tasks[call_id] = {"id": call_id, "title": self._compact(title), "status": "in_progress"}
            return self.tasks[call_id]

        def apply_event(self, raw: Any) -> bool:
            event_type = raw.get("type") if isinstance(raw, dict) else None
            if event_type not in {"tool.started", "tool.completed"}:
                return False
            call_id = str(raw.get("tool_call_id") or "")
            if not call_id:
                self.anonymous_seq += 1
                call_id = f"anonymous_{self.anonymous_seq}"
            tool_name = str(raw.get("tool_name") or "tool")
            if event_type == "tool.started":
                preview = self._compact(raw.get("preview"), 64)
                self._upsert(call_id, f"{tool_name} - {preview}" if preview else tool_name)
                return True
            # Completion-only events are rare but valid on some runtimes; keep their real ID instead
            # of guessing a same-name pending call.
            task = self.tasks.get(call_id) or self._upsert(call_id, tool_name)
            task["status"] = "error" if raw.get("is_error") else "complete"
            return True

    async def _task_card_send_or_edit_fallback(self, st) -> None:
        ctx = self._ctx
        text = st.fallback_text()
        from gateway.relay.egress import declined_send

        if st.publication_suppressed:
            return
        if st.fallback_msg_id:
            result = await ctx.deliver_visible(lambda: st.adapter.edit_message(
                chat_id=ctx.source.chat_id, message_id=st.fallback_msg_id, content=text, metadata=ctx._progress_metadata,
            ))
            if getattr(result, "success", False):
                return
            # P5(b): R5-4 made a declined native CARD terminal but left this
            # editable-text fallback: a declined edit fell through to
            # _send_progress_text and re-sent the same task text to the refused
            # chat. The decline must set the terminal state here too.
            if declined_send(result):
                logger.warning(
                    "Task-card fallback edit DECLINED by the connector's egress "
                    "guard; suppressing progress delivery for the rest of this "
                    "turn (the destination is not approved)"
                )
                st.publication_suppressed = True
                return
        result = await self._send_progress_text(st, text)
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            st.fallback_msg_id = str(result.message_id)

    async def _task_card_publish(self, st) -> None:
        ctx = self._ctx
        if not st.tasks:
            return
        if st.publication_suppressed:
            # Publication was suppressed earlier in the turn (egress refusal or a chat
            # that cannot host a card); every later publication would re-deliver the
            # same task text there.
            return
        # Resolve eligibility in the owning adapter BEFORE transport I/O: a first
        # timeout/disconnect must not turn an un-cardable chat into text fallback.
        # Optional for older adapters; only an explicit False refuses publication.
        destination_supported = getattr(st.adapter, "native_task_card_destination_supported", None)
        if callable(destination_supported) and destination_supported(
            ctx.source.chat_id, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        ) is False:
            self._task_card_uncardable_destination(st)
            if st.publication_suppressed:
                return
        if not st.native_failed:
            result = await ctx.deliver_visible(lambda: st.adapter.send_native_task_card_progress(
                chat_id=ctx.source.chat_id, tasks=st.visible_tasks(), title="Hermes is working",
                reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata, fallback_text=st.fallback_text(),
            ))
            if getattr(result, "success", False):
                return
            # P5(b): an AUTHORIZATION decline is not a broken card lane. The
            # fallback below sends the same task text to the same chat, which
            # turns a refused card into delivered plain text. Stop the lane
            # without re-delivering; the refusal is already logged.
            from gateway.relay.egress import declined_send

            if declined_send(result):
                # TERMINAL, and stored SEPARATELY from native_failed. Reusing
                # native_failed suppressed exactly ONE update: the next progress
                # event skipped this branch (the lane is already "failed") and
                # went straight to the text fallback. A refusal does not expire
                # after one tick.
                st.publication_suppressed = True
                st.native_failed = True
                logger.warning(
                    "Slack native task-card progress DECLINED by the connector's "
                    "egress guard — suppressing the text fallback for the rest "
                    "of this turn (the destination is not approved)"
                )
                return
            st.native_failed = True
            if getattr(result, "error", None) in _CARD_DESTINATION_REFUSALS:
                self._task_card_uncardable_destination(st, getattr(result, "error", "unknown error"))
                if st.publication_suppressed:
                    return
            else:
                logger.warning(
                    "Slack native task-card progress failed; falling back "
                    "to an editable text update: %s", getattr(result, "error", "unknown error"),
                )
        # Once the native rail fails, every later lifecycle event edits the same fallback message.
        await self._task_card_send_or_edit_fallback(st)

    def _task_card_uncardable_destination(self, st, reason: str = "no thread anchor") -> None:
        """The chat cannot host a card (flat DM: no thread anchor) — a property of the destination,
        not a transient outage. The text fallback exists to keep a WORKING card lane live through a
        transient failure, not to invent text bubbles the operator never asked for: with Slack's tier
        default (``tool_progress: off``) the lane goes silent for the turn. An operator who WROTE
        ``new``/``all`` asked for text progress, so the editable fallback carries it instead."""
        if self._ctx.tool_progress_enabled:
            st.native_failed = True
            logger.info(
                "Slack native task cards are unsupported for this destination (%s); "
                "tool progress continues as an editable text update", reason,
            )
            return
        st.publication_suppressed = True
        logger.info(
            "Slack native task cards are unsupported for this destination (%s); "
            "tool progress stays off for this turn", reason,
        )

    def _task_card_drain(self, st) -> bool:
        changed = False
        try:
            while True:
                _raw = self._ctx.progress_queue.get_nowait()
                if self._is_flush_marker(_raw):
                    self._signal_flush_marker(_raw)
                    continue
                changed = st.apply_event(_raw) or changed
        except queue.Empty:
            pass
        except Exception:
            logger.debug("Slack native progress queue drain failed", exc_info=True)
        return changed

    async def _send_native_task_card_progress(self, adapter) -> None:
        """Drain progress into native cards; supported destinations retain editable fallback.
        Unsupported destinations and egress refusals suppress publication, never finalization.

        See #29483.
        """
        ctx = self._ctx
        st = self._TaskCardState(adapter)
        try:
            while ctx._run_still_current():
                try:
                    raw = ctx.progress_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue
                # Native rail drops plain text rows by design: a flush marker
                # only needs its waiter released.
                if self._is_flush_marker(raw):
                    self._signal_flush_marker(raw)
                    continue
                if self._is_hold_marker(raw):
                    self._release_hold_marker(raw)
                    continue
                if not self._agent_interrupted() and st.apply_event(raw):
                    await self._task_card_publish(st)
        except asyncio.CancelledError:
            if self._task_card_drain(st) and ctx._run_still_current() and not self._agent_interrupted():
                await self._task_card_publish(st)
        finally:
            if hasattr(adapter, "stop_native_task_card_progress"):
                # Best-effort on the turn-cleanup path: an escaping transport exception would skip
                # final-delivery logic (cleanup awaits catch only CancelledError).
                try:
                    await adapter.stop_native_task_card_progress(
                        ctx.source.chat_id, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("task-card stop failed during turn cleanup", exc_info=True)

    # ── editable progress bubbles (progress-queue drain) ────────────────────────────────────

    @dataclasses.dataclass
    class _ProgressEditState:
        """Mutable editable-bubble state shared by ``send_progress_messages`` and its helpers."""
        adapter: Any
        progress_lines: list
        progress_msg_id: Any
        can_edit: bool
        _progress_len_fn: Any
        _PROGRESS_TEXT_LIMIT: int
        _edit_accepts_metadata: bool
        # Oversized-row chunking (codebox only): a boxed row sliced across
        # several messages leaves only its tail slices editable; _row_head_len
        # counts the escaped chars of the CURRENT row already visible in the
        # earlier chunks, and _row_slices how many trailing buffer lines belong
        # to that row — unrelated earlier rows never contribute to either.
        _row_tail: bool = False
        _row_head_len: int = 0
        _row_slices: int = 0
        # Prefix of separate-mode rows whose sends succeeded; flush owns the tail.
        _delivered_rows: int = 0
        # Narration hold opened this box: show the row text, not a (×N) rewrite.
        # A content-bubble ``__reset__`` still uses the counted retry form.
        plain_repeats: bool = False

    def _progress_edit_state(self, adapter) -> "TurnRunner._ProgressEditState":
        ctx = self._ctx
        len_fn = adapter.message_len_fn if isinstance(adapter, BasePlatformAdapter) else len
        try:
            raw_limit = int(getattr(adapter, "MAX_MESSAGE_LENGTH", 4000) or 4000)
        except Exception:
            raw_limit = 4000
        # Per-chat resolution (relay adapter fronting N platforms): cap and length unit follow the
        # chat's underlying platform; native adapters return their scalar/property unchanged.
        if isinstance(adapter, BasePlatformAdapter):
            with suppress(Exception):
                raw_limit = int(adapter.max_message_length_for_chat(ctx.source.chat_id) or 4000)
                len_fn = adapter.message_len_fn_for_chat(ctx.source.chat_id)
        return self._ProgressEditState(
            adapter=adapter, progress_lines=[], progress_msg_id=None,
            # "separate" = one message per tool (pre-v0.9 behavior)
            can_edit=ctx.progress_grouping != "separate",
            _progress_len_fn=len_fn,
            # Leave room for platform quirks / formatting; tiny test adapters keep a usable limit.
            _PROGRESS_TEXT_LIMIT=max(1, raw_limit - (64 if raw_limit > 128 else 0)),
            # Overflow edits pass metadata (Telegram topic/thread routing) only when edit_message takes it.
            _edit_accepts_metadata=bool(ctx._progress_metadata) and _accepts_keyword(adapter.edit_message, "metadata"),
        )

    async def _edit_progress_message(self, st, message_id: str, content: str):
        ctx = self._ctx
        kwargs = {"chat_id": ctx.source.chat_id, "message_id": message_id, "content": content}
        if getattr(st.adapter, "REQUIRES_EDIT_FINALIZE", False):
            kwargs["finalize"] = True
        if st._edit_accepts_metadata:
            kwargs["metadata"] = ctx._progress_metadata
        # deliver_visible holds the turn's delivery lock across the edit and
        # marks only a landed edit as user-visible progress.
        return await ctx.deliver_visible(lambda: st.adapter.edit_message(**kwargs))

    def _progress_text(self, st, lines: list) -> str:
        # Code-box presentation (display.tool_progress_codebox): the whole
        # accumulated group becomes ONE unlabelled fenced block, with inner ```
        # escaped so already-fenced command/code previews render inside the box
        # instead of breaking out. Applies to every lane: accumulated bubbles,
        # "separate" per-row messages and non-editable fallback sends.
        from gateway.display_config import format_progress_group

        return format_progress_group(lines, self._codebox_active(st))

    async def _send_progress_row(self, st, row):
        """Send one row as its own message(s): "separate" grouping and non-editable fallbacks.

        An oversized boxed row ships as several limit-fitting boxes (the editable lane's
        buffer slicing is skipped here because nothing edits these rows afterwards).
        """
        from gateway.display_config import format_progress_row

        result = None
        for text in format_progress_row(row, self._codebox_active(st), st._PROGRESS_TEXT_LIMIT, st._progress_len_fn):
            result = await self._send_progress_text(st, text)
        return result

    def _split_progress_groups(self, st, lines: list) -> list[list]:
        """Partition progress lines into platform-sized editable bubbles."""
        groups: list[list] = []
        current: list = []
        for line in lines:
            candidate = current + [line]
            if current and st._progress_len_fn(self._progress_text(st, candidate)) > st._PROGRESS_TEXT_LIMIT:
                groups.append(current)
                candidate = [line]
            current = candidate
        return groups + ([current] if current else [])

    async def _send_progress_text(self, st, text: str):
        ctx = self._ctx
        result = await ctx.deliver_visible(lambda: st.adapter.send(
            chat_id=ctx.source.chat_id, content=text, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        ))
        self._track_progress_result(result)
        return result

    async def _roll_progress_overflow_if_needed(self, st) -> bool:
        """Start fresh editable progress bubbles before a bubble exceeds limit.

        Returns True when it delivered/split the buffer or a transient edit failure left it
        intact for retry — either way the caller skips the normal send/edit path this tick.
        """
        if not st.progress_lines or not st.can_edit:
            return False
        groups = self._split_progress_groups(st, st.progress_lines)
        if len(groups) <= 1:
            return False
        if st.progress_msg_id is not None:
            result = await self._edit_progress_message(st, st.progress_msg_id, self._progress_text(st, groups[0]))
            if not result.success:
                if getattr(result, "retryable", False):
                    logger.debug("[%s] Transient overflow edit failure — keeping can_edit=True", st.adapter.name)
                    return True
                st.can_edit = False
                # Fall back to the existing non-edit behavior.
                return False
            groups = groups[1:]
        for group in groups:
            result = await self._send_progress_text(st, self._progress_text(st, group))
            if result.success and result.message_id:
                st.progress_msg_id = result.message_id
        # The newest continuation is the only mutable bubble: keep just its lines so later
        # edits update it instead of replaying the full transcript into new messages.
        if st._row_tail:
            # Only the current row's slices delivered into earlier groups count
            # towards its head offset — never unrelated preceding rows.
            kept = min(st._row_slices, len(groups[-1]))
            st._row_head_len += sum(
                len(line)
                for line in st.progress_lines[len(st.progress_lines) - st._row_slices : len(st.progress_lines) - kept]
            )
            st._row_slices = kept
        st.progress_lines = groups[-1]
        return True

    @staticmethod
    def _is_reset_marker(raw) -> bool:
        return isinstance(raw, tuple) and len(raw) >= 1 and raw[0] == "__reset__"

    @staticmethod
    def _is_hold_marker(raw) -> bool:
        """Narration-send gate marker (GatewayStreamConsumer::_begin_progress_gate):
        ('__hold__', asyncio.Event) or ('__gate__', threading.Event) — a commentary /
        first-stream send is in flight."""
        return isinstance(raw, tuple) and len(raw) == 2 and raw[0] in ("__hold__", "__gate__")

    @staticmethod
    def _release_hold_marker(raw) -> None:
        """Free a gate marker's waiter without delivering anything (non-editable
        lanes, cancel drains, interrupted turns — there is no bubble to hold)."""
        with suppress(Exception):
            raw[1].set()

    async def _handle_hold_marker(self, st, raw) -> None:
        """A narration send is in flight (see GatewayStreamConsumer._progress_box_hold).

        Close the current bubble NOW so the narration lands below it, then HOLD the
        next box's first send until the send completes (or fails). Without the hold,
        the pump can open the new box ABOVE the still-in-flight narration, and the
        late ('__reset__',) from its completion closes the box this same step's
        tools just opened — tool rows split across boxes out of order.
        """
        if not self._agent_interrupted():
            with suppress(Exception):
                await self._roll_progress_overflow_if_needed(st)
                # Unsent rows belong to the box ABOVE the narration. Flush only
                # edits an existing bubble, so a never-sent buffer has to go out
                # before the reset or those rows vanish.
                if st.can_edit and st.progress_lines and st.progress_msg_id is None:
                    await self._progress_send_or_edit(st, st.progress_lines[-1])
                else:
                    await self._flush_progress_edit(st)
            self._reset_progress_bubble(st)
        # The new box is the narration's tool rows, not a post-reset retry.
        st.plain_repeats = True
        # The event may be asyncio.Event (in-loop gate) or threading.Event
        # (agent-worker-thread gate); poll — both expose is_set().
        evt = raw[1]

        async def _released():
            while not evt.is_set():
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(_released(), timeout=60.0)
        except Exception:
            logger.warning("progress hold marker never released; continuing")

    @staticmethod
    def _is_flush_marker(raw) -> bool:
        """Context-card ordering gate marker (gateway.run._CONTEXT_FLUSH_QUEUE_MARKER):
        ('__flush__', asyncio.Event) — deliver what was absorbed ahead of it, then set."""
        return isinstance(raw, tuple) and len(raw) == 2 and raw[0] == "__flush__"

    def _signal_flush_marker(self, raw) -> None:
        from gateway.run import _signal_context_flush_marker

        _signal_context_flush_marker(raw)

    async def _handle_flush_marker(self, st, raw) -> None:
        """Force-deliver the absorbed bubble (throttle bypassed), then release the
        gate's waiter. Interrupted turns keep row suppression — release only."""
        if st.progress_lines and not self._agent_interrupted():
            if not st.can_edit:
                while st._delivered_rows < len(st.progress_lines):
                    row = st.progress_lines[st._delivered_rows]
                    result = await self._send_progress_row(st, row)
                    if not result.success:
                        break
                    st._delivered_rows += 1
            elif not await self._roll_progress_overflow_if_needed(st):
                await self._progress_send_or_edit(st, st.progress_lines[-1])
        self._signal_flush_marker(raw)

    def _reset_progress_bubble(self, st) -> None:
        """Content bubble landed — close the tool-progress bubble so the next tool starts fresh
        below it; else tool edits hit the ORIGINAL message above (out of order)."""
        st.progress_msg_id, st.progress_lines = None, []
        st._delivered_rows = 0
        st._row_tail, st._row_head_len, st._row_slices = False, 0, 0
        # Producer-side dedup memory (last_progress_msg/repeat_count) survives the
        # bubble reset: it tracks the previous row emitted, not the rendered bubble.
        # Clearing it here let a retried tool call whose row is text-identical
        # (describe mode re-renders the same phrase) re-render as a second plain
        # row instead of collapsing into (×N).

    def _claude_progress_delivery(self, st):
        """Bind the shared Claude/viewer delivery rail to this bubble state."""
        from gateway.run_claude_progress import ClaudeDelegationDelivery

        async def flush_before_status() -> None:
            if not st.progress_lines:
                return
            try:
                if not await self._roll_progress_overflow_if_needed(st):
                    await self._progress_send_or_edit(st, st.progress_lines[-1])
            except Exception:
                logger.debug("progress flush before agent status failed", exc_info=True)

        return ClaudeDelegationDelivery(
            ctx=self._ctx,
            adapter=st.adapter,
            deliver_status_marker=self._deliver_agent_status_marker,
            flush_before_status=flush_before_status,
            reset_bubble=lambda: self._reset_progress_bubble(st),
            agent_interrupted=self._agent_interrupted,
        )

    def _codebox_active(self, st) -> bool:
        return bool(self._ctx.progress_codebox)

    def _chunking_active(self, st) -> bool:
        # Buffer slicing only serves the editable lane; non-editable rows are chunked at send time.
        return self._codebox_active(st) and st.can_edit

    def _progress_absorb(self, st, raw) -> Any:
        """Fold a queue item into the bubble buffer; returns the line to render this tick.

        A boxed row whose escaped body plus fence overhead alone exceeds the platform cap is
        sliced into limit-fitting lines (the group splitter can only partition between rows;
        without this the native adapter split the row and later edits truncated the tail,
        losing repeat counters).
        """
        from gateway.display_config import chunk_oversized_progress_row, repeat_row_text

        if isinstance(raw, tuple) and len(raw) == 3 and raw[0] == "__dedup__":
            _, base_msg, count = raw
            if st.plain_repeats:
                # Rows queued under a narration hold were never shown. Collapse
                # repeats to the plain row — a (×N) seed here is the row the
                # hold reset dropped, not a retry the user already saw.
                if base_msg not in st.progress_lines:
                    st.progress_lines.append(base_msg)
                    st._row_tail, st._row_slices = False, 0
                return base_msg
            if not st.progress_lines:
                # Bubble was just reset (interim content landed) but the producer's
                # dedup memory says this row repeats: seed the fresh bubble with the
                # counted row instead of a plain copy (or a row the buffer dropped).
                line = f"{base_msg} (×{count + 1})"
                if self._chunking_active(st):
                    slices = chunk_oversized_progress_row(line, st._PROGRESS_TEXT_LIMIT, st._progress_len_fn)
                    st.progress_lines.extend(slices)
                    return slices[-1]
                st.progress_lines.append(line)
                return line
            if st._row_tail:
                # The repeated row is already split across chunk messages. Slicing is
                # prefix-stable, so earlier chunks hold an unchanged prefix — re-render
                # only the editable tail with the counter instead of duplicating the head.
                # Only the trailing lines of that row are replaced; earlier pending
                # rows must stay visible.
                tail = repeat_row_text(base_msg, count, self._codebox_active(st))[st._row_head_len:]
                if self._chunking_active(st):
                    slices = chunk_oversized_progress_row(tail, st._PROGRESS_TEXT_LIMIT, st._progress_len_fn)
                    if len(slices) > 1:
                        st.progress_lines[-st._row_slices:] = slices
                        st._row_slices = len(slices)
                        return slices[-1]
                st.progress_lines[-st._row_slices:] = [tail]
                st._row_slices = 1
                return tail
            line = f"{base_msg} (×{count + 1})"
            if self._chunking_active(st):
                slices = chunk_oversized_progress_row(line, st._PROGRESS_TEXT_LIMIT, st._progress_len_fn)
                if len(slices) > 1:
                    st.progress_lines[-1:] = slices
                    st._row_tail, st._row_head_len, st._row_slices = True, 0, len(slices)
                    return slices[-1]
            st.progress_lines[-1] = line
            return st.progress_lines[-1]
        if self._chunking_active(st):
            slices = chunk_oversized_progress_row(raw, st._PROGRESS_TEXT_LIMIT, st._progress_len_fn)
            if len(slices) > 1:
                st.progress_lines.extend(slices)
                st._row_tail, st._row_head_len, st._row_slices = True, 0, len(slices)
                return slices[-1]
        st.progress_lines.append(raw)
        st._row_tail, st._row_slices = False, 0
        return raw

    async def _flush_progress_edit(self, st) -> None:
        if st.can_edit and st.progress_lines and st.progress_msg_id:
            with suppress(Exception):
                await self._edit_progress_message(st, st.progress_msg_id, self._progress_text(st, st.progress_lines))

    async def _drain_progress_on_cancel(self, st) -> None:
        from gateway.run import _is_agent_status_queue_marker
        from gateway.run_claude_progress import is_claude_delegation_start_marker

        ctx = self._ctx
        st.adapter = self._runner._delivery_adapter_for(ctx.source) or st.adapter
        claude_delivery = self._claude_progress_delivery(st)
        with suppress(Exception):
            while not ctx.progress_queue.empty():
                raw = ctx.progress_queue.get_nowait()
                if self._is_flush_marker(raw):
                    # Cancelled drain: release the ordering gate's waiter.
                    self._signal_flush_marker(raw)
                    continue
                if self._is_hold_marker(raw):
                    # Cancelled drain: the narration send releases its own hold
                    # in its finally; release here too so the pump never waits.
                    self._release_hold_marker(raw)
                    continue
                if is_claude_delegation_start_marker(raw):
                    if not self._agent_interrupted():
                        await claude_delivery.deliver_start(raw)
                    continue
                if _is_agent_status_queue_marker(raw):
                    await claude_delivery.deliver_viewer_marker(raw)
                    continue
                if self._agent_interrupted():
                    # A reset clears local bookkeeping but must not edit a stopped bubble.
                    if self._is_reset_marker(raw):
                        self._reset_progress_bubble(st)
                    continue
                if self._is_reset_marker(raw):
                    await self._roll_progress_overflow_if_needed(st)
                    await self._flush_progress_edit(st)
                    self._reset_progress_bubble(st)
                else:
                    self._progress_absorb(st, raw)
                    await self._roll_progress_overflow_if_needed(st)
        # Final edit with all remaining tools (only if editing works and the turn is live).
        if not self._agent_interrupted() and st.can_edit and st.progress_lines and st.progress_msg_id:
            await self._roll_progress_overflow_if_needed(st)
            await self._flush_progress_edit(st)

    async def _progress_restore_typing(self, st) -> None:
        ctx = self._ctx
        await asyncio.sleep(0.3)
        if ctx._run_still_current():
            await st.adapter.send_typing(ctx.source.chat_id, metadata=ctx._progress_metadata)

    async def _progress_send_or_edit(self, st, msg) -> bool:
        """Deliver this tick's bubble. Returns False on a transient edit failure (retry next tick).

        Transient network errors (ConnectError, timeouts) must not disable editing; only permanent
        failures (not found, permissions) set can_edit=False. Flood control backs off but keeps editing.
        """
        if st.can_edit and st.progress_msg_id is not None:
            result = await self._edit_progress_message(st, st.progress_msg_id, self._progress_text(st, st.progress_lines))
            if result.success:
                return True
            if getattr(result, "retryable", False):
                logger.debug("[%s] Transient edit failure — keeping can_edit=True", st.adapter.name)
                return False
            if any(w in (getattr(result, "error", "") or "").lower() for w in ("flood", "retry after")):
                logger.info("[%s] Progress edit flood control, backing off", st.adapter.name)
            else:
                st.can_edit = False
            result = await self._send_progress_row(st, msg)
            if result.success:
                st._delivered_rows = len(st.progress_lines)
            return True
        # First tool: send all accumulated text as a new message; editing unsupported: just this line.
        result = await (
            self._send_progress_text(st, self._progress_text(st, st.progress_lines))
            if st.can_edit else self._send_progress_row(st, msg)
        )
        if result.success:
            if not st.can_edit:
                st._delivered_rows = len(st.progress_lines)
            if result.message_id:
                st.progress_msg_id = result.message_id
        return True

    async def send_progress_messages(self):
        from gateway.run import _adapter_can_edit_progress, _is_agent_status_queue_marker
        from gateway.run_claude_progress import is_claude_delegation_start_marker

        ctx = self._ctx
        adapter = self._runner._delivery_adapter_for(ctx.source) if ctx.progress_queue else None
        if not adapter:
            return
        if ctx._native_slack_task_cards and hasattr(adapter, "send_native_task_card_progress"):
            await self._send_native_task_card_progress(adapter)
            return
        # Non-editable platforms drain once and return. Tool rows are dropped by
        # design, but standalone viewer notices and flush waiters must not strand.
        if not _adapter_can_edit_progress(adapter):
            while not ctx.progress_queue.empty():
                try:
                    raw = ctx.progress_queue.get_nowait()
                except Exception:
                    break
                if _is_agent_status_queue_marker(raw):
                    await self._deliver_agent_status_marker(raw)
                elif self._is_flush_marker(raw):
                    self._signal_flush_marker(raw)
                elif self._is_hold_marker(raw):
                    self._release_hold_marker(raw)
            return
        st = self._progress_edit_state(adapter)
        claude_delivery = self._claude_progress_delivery(st)
        last_edit_ts = 0.0
        EDIT_INTERVAL = 1.5  # Minimum seconds between edits (Telegram flood control)
        pending = None
        dirty = False

        async def _publish(msg) -> bool:
            """Deliver the current buffer. False = transient edit failure, retry."""
            nonlocal dirty, last_edit_ts
            if not st.progress_lines:
                dirty = False
                return True
            if not await self._roll_progress_overflow_if_needed(st):
                if not await self._progress_send_or_edit(st, msg):
                    return False
            dirty = False
            last_edit_ts = time.monotonic()
            await self._progress_restore_typing(st)
            return True

        while True:
            # Mid-turn platform reconnect swaps the registry adapter; re-resolve
            # per iteration so remaining sends/edits/typing ride the current transport.
            st.adapter = self._runner._delivery_adapter_for(ctx.source) or st.adapter
            try:
                if not ctx._run_still_current():
                    self._drain_progress_queue()
                    return
                if pending is not None:
                    raw = pending
                    pending = None
                else:
                    try:
                        raw = ctx.progress_queue.get_nowait()
                    except queue.Empty:
                        # Keep this await inside the cancellation-drain try: normal
                        # turn cleanup also cancels the consumer while it is parked.
                        # Idle flush: a throttled absorb must still land when no
                        # further row arrives. The old continue-after-sleep dropped
                        # the last row until cancel.
                        # Separate mode must not idle-flush: a dedup only rewrites
                        # the buffer, and a buffered row is sent by flush_before_status.
                        if dirty and st.can_edit and st.progress_lines:
                            remaining = EDIT_INTERVAL - (time.monotonic() - last_edit_ts)
                            if st.progress_msg_id is None or remaining <= 0:
                                if not ctx._run_still_current():
                                    return
                                if not await _publish(st.progress_lines[-1]):
                                    await asyncio.sleep(0.3)
                                continue
                            await asyncio.sleep(min(0.3, max(0.02, remaining)))
                            continue
                        await asyncio.sleep(0.3)
                        continue
                if self._is_flush_marker(raw):
                    if dirty and st.progress_lines:
                        await _publish(st.progress_lines[-1])
                    await self._handle_flush_marker(st, raw)
                    continue
                if self._is_hold_marker(raw):
                    await self._handle_hold_marker(st, raw)
                    dirty = False
                    continue
                # Late rows are suppressed after stop. Viewer markers survive:
                # the spawned agent keeps running and its link remains useful.
                if self._agent_interrupted() and not _is_agent_status_queue_marker(raw):
                    await asyncio.sleep(0)
                    continue
                if self._is_reset_marker(raw):
                    if st.progress_lines and not self._agent_interrupted():
                        if st.progress_msg_id is None:
                            await _publish(st.progress_lines[-1])
                        else:
                            await self._flush_progress_edit(st)
                    self._reset_progress_bubble(st)
                    dirty = False
                    continue
                if is_claude_delegation_start_marker(raw):
                    await claude_delivery.deliver_start(raw)
                    continue
                if _is_agent_status_queue_marker(raw):
                    await claude_delivery.deliver_viewer_marker(raw)
                    continue
                msg = self._progress_absorb(st, raw)
                dirty = True
                # Accumulate: fold every row already queued into this box before
                # its first send, stopping at a marker so a reset/hold still
                # splits in queue order. Separate mode sends one row per message.
                if st.can_edit:
                    while pending is None:
                        try:
                            nxt = ctx.progress_queue.get_nowait()
                        except queue.Empty:
                            break
                        if (
                            self._is_reset_marker(nxt)
                            or self._is_hold_marker(nxt)
                            or self._is_flush_marker(nxt)
                            or is_claude_delegation_start_marker(nxt)
                            or _is_agent_status_queue_marker(nxt)
                        ):
                            pending = nxt
                            break
                        msg = self._progress_absorb(st, nxt)
                if not await self._roll_progress_overflow_if_needed(st):
                    remaining = EDIT_INTERVAL - (time.monotonic() - last_edit_ts)
                    if st.can_edit:
                        # First send of a box is immediate so a narration-held
                        # burst is ONE send. Later edits wait; the idle path
                        # flushes them. Never sleep-and-drop the last row.
                        if st.progress_msg_id is not None and remaining > 0:
                            continue
                    elif remaining > 0:
                        # Separate: buffer inside the throttle window. The row
                        # lands via flush_before_status (before a viewer notice)
                        # or the next tick — never as a second copy of a dedup.
                        await asyncio.sleep(remaining)
                        continue
                    if not ctx._run_still_current():
                        return
                    if not await self._progress_send_or_edit(st, msg):
                        continue
                dirty = False
                last_edit_ts = time.monotonic()
                await self._progress_restore_typing(st)
            except asyncio.CancelledError:
                # A marker pulled ahead of the batch is not on the queue. Put
                # it back in front so the cancel drain still delivers it.
                if pending is not None:
                    rest = []
                    while True:
                        try:
                            rest.append(ctx.progress_queue.get_nowait())
                        except queue.Empty:
                            break
                    ctx.progress_queue.put(pending)
                    for item in rest:
                        ctx.progress_queue.put(item)
                    pending = None
                await self._drain_progress_on_cancel(st)
                return
            except Exception as e:
                logger.error("Progress message error: %s", e)
                await asyncio.sleep(1)

    # ── ID-bearing lifecycle callbacks (agent thread) ───────────────────────────────────────

    # Slack-native task cards ride agent.tool_start_callback / tool_complete_callback so start and
    # completion correlate by the REAL tool-call id; name-correlated progress_callback text events
    # would duplicate cards and mispair concurrent calls.

    def _native_card_gate(self) -> bool:
        ctx = self._ctx
        return bool(ctx.progress_queue) and ctx._run_still_current() and not self._agent_interrupted()

    # ── Slack-native task cards: ID-bearing lifecycle callbacks (#29483) ── These ride
    # agent.tool_start_callback / agent.tool_complete_callback so start/completion events correlate by the
    # REAL tool-call id — the name-correlated text events in progress_callback would duplicate cards and
    # mispair concurrent calls to the same tool.
    def native_tool_start_callback(self, call_id, tool_name, args):
        """Queue an ID-correlated native progress start from the agent thread."""
        if not self._native_card_gate():
            return
        from agent.display import build_tool_preview, describe_tool_call
        name = str(tool_name or "tool")
        if self._ctx.progress_mode == "describe":
            # Describe mode: never surface the raw command/code in the card title.
            preview = describe_tool_call(name, args or {})
        else:
            preview = build_tool_preview(name, args or {}, max_len=64) or ""
        self._ctx.progress_queue.put({
            "type": "tool.started", "tool_call_id": str(call_id or ""), "tool_name": name,
            "preview": preview,
        })

    def native_tool_complete_callback(self, call_id, tool_name, args, result):
        """Queue the matching native completion using the real tool-call ID."""
        if not self._native_card_gate():
            return
        from agent.display import _detect_tool_failure
        name = str(tool_name or "tool")
        is_error, _ = _detect_tool_failure(name, result)
        self._ctx.progress_queue.put({
            "type": "tool.completed", "tool_call_id": str(call_id or ""), "tool_name": name, "is_error": bool(is_error),
        })

    def combined_tool_start_callback(self, call_id, tool_name, args):
        """Native task-card start consumer (Slack)."""
        if self._ctx._native_slack_task_cards:
            self.native_tool_start_callback(call_id, tool_name, args)
        from gateway.run_claude_progress import claude_delegation_single_status, queue_claude_delegation_start

        if str(tool_name or "") == "delegate_claude_agent" and claude_delegation_single_status(self._ctx):
            queue_claude_delegation_start(self._ctx, call_id, tool_name, args)

    # ── hook / status bridges (agent thread → gateway loop) ────────────────────────────────

    def _step_callback_sync(self, iteration: int, prev_tools: list) -> None:
        ctx = self._ctx
        if not ctx._run_still_current():
            return
        # prev_tools may be list[str] or list[dict] with "name"/"result" keys. Normalise so
        # "tool_names" stays backward-compatible for user hooks that do ', '.join(tool_names).
        names = [(t.get("name") or "") if isinstance(t, dict) else str(t) for t in (prev_tools or [])]
        self._schedule(
            ctx._hooks_ref.emit("agent:step", {
                "platform": ctx.source.platform.value if ctx.source.platform else "",
                "user_id": ctx.source.user_id, "session_id": ctx.session_id,
                "iteration": iteration, "tool_names": names, "tools": prev_tools,
            }),
            "agent:step hook scheduling error",
        )

    def _event_callback_sync(self, event_type: str, context: dict) -> None:
        ctx = self._ctx
        try:
            asyncio.run_coroutine_threadsafe(ctx._hooks_ref.emit(event_type, context), ctx._loop_for_step)
        except Exception as e:
            logger.debug("event_callback hook error: %s", e)

    def _status_live(self) -> bool:
        """Status adapter present and this run is still the current generation."""
        return bool(self._ctx._status_adapter) and self._ctx._run_still_current()

    def _send_status_text(self, text: str, metadata, log_message: str) -> None:
        ctx = self._ctx
        # deliver_visible holds the turn's delivery lock across the send and
        # marks only a successful delivery (failed/declined never reset the
        # heartbeat silence clock).
        fut = self._schedule(
            ctx.deliver_visible(lambda: ctx._status_adapter.send(
                ctx._status_chat_id, text, metadata=metadata)), log_message)

    def _attach_session_title_callback(self, agent, ctx) -> None:
        """Wire the platform thread-rename lane onto the agent as `_on_session_title`.

        The titler runs in the turn prologue, so attach before the run, not after it.
        """
        try:
            # Gateway auto-title failures are not user-actionable, so never surface them as messages;
            # overriding the failure sink keeps CLI on _emit_auxiliary_failure while gateway logs debug.
            agent._title_failure_callback = lambda task, exc: logger.debug(
                "Gateway auto-title failure suppressed (not user-visible): %s: %s", task, exc,
            )
            session_id = getattr(agent, "session_id", None)
            source = ctx.source
            runner = self._runner
            # Both lanes spend a rate-limited platform call per title, so they use the model's title
            # only (TitleCallback); renaming twice burns Discord's 2-per-10-min budget on a throwaway.
            # Relay Discord predicate is shape-only: whether the connector auto-threaded our reply is
            # only knowable AFTER delivery, so register eagerly and let the rename lane look up the
            # cache at fire time — gating registration on the cache read meant it never registered.
            if runner._is_telegram_topic_lane(source):
                lane = "_schedule_telegram_topic_title_rename"
            elif runner._is_matrix_inbox_room_lane(source):
                lane = "_schedule_matrix_room_title_rename"
            elif runner._is_discord_auto_thread_lane(source) or runner._is_relay_discord_channel_lane(source):
                lane = "_schedule_discord_semantic_thread_rename"
            else:
                return
            agent._on_session_title = lambda title, title_source: (
                title_source == "llm" and getattr(runner, lane)(source, session_id, title)
            )
        except Exception:
            logger.debug("Failed to attach session title callback", exc_info=True)

    def _status_callback_sync(self, event_type: str, message: str) -> None:
        from gateway.run import (
            _mission_chat_suppresses_status, _prepare_gateway_status_message,
            _redact_gateway_user_facing_secrets, _send_or_update_status_coro,
        )
        from gateway.warning_notifications import is_warning_status, render_notification
        ctx = self._ctx
        if ctx.mute_notification_reply or not self._status_live():
            return
        # Mission-bound chats serve an end-user persona: suppress only the
        # operator-facing fallback notice, before preparation or delivery.
        if _mission_chat_suppresses_status(ctx._status_chat_id, event_type, message):
            logger.debug(
                "status_callback suppressed for mission chat %s/%s: %s",
                ctx.source.platform.value if ctx.source.platform else "unknown", event_type,
                _redact_gateway_user_facing_secrets(str(message or ""))[:160],
            )
            return
        prepared = _prepare_gateway_status_message(ctx.source.platform, event_type, message)
        if prepared is None:
            logger.debug(
                "status_callback suppressed for %s/%s: %s",
                ctx.source.platform.value if ctx.source.platform else "unknown", event_type,
                _redact_gateway_user_facing_secrets(str(message or ""))[:160],
            )
            return
        # Viewer notices follow the delegation's tool row through the same FIFO.
        # Non-editable and native-card consumers have no durable text queue, so
        # those surfaces retain the direct status rail.
        from gateway.run import _adapter_can_edit_progress, _agent_status_queue_marker
        if (
            ctx.tool_progress_enabled
            and ctx.progress_queue is not None
            and not ctx._native_slack_task_cards
            and _adapter_can_edit_progress(ctx._status_adapter)
        ):
            from tools.tool_status import get_tool_status_call_id, is_agent_viewer_status_line

            if is_agent_viewer_status_line(prepared):
                try:
                    viewer_call_id = get_tool_status_call_id()
                except Exception:
                    viewer_call_id = None
                ctx.progress_queue.put(
                    _agent_status_queue_marker(event_type, prepared, viewer_call_id)
                )
                return

        def present():
            fut = self._schedule(
                ctx.deliver_visible(lambda: _send_or_update_status_coro(
                    ctx._status_adapter, ctx._status_chat_id, event_type, prepared, ctx._status_thread_metadata)),
                f"status_callback ({event_type}) scheduling error",
            )
            if fut is not None and ctx._cleanup_progress:
                fut.add_done_callback(self._track_future_cleanup_id)
        render_notification(present, platform=ctx.source.platform, user_config=ctx.user_config,
                            diagnostic=is_warning_status(event_type, message))

    # ── stream consumer / interim commentary wiring ─────────────────────────────────────────

    def _setup_stream_consumer(self, platform_key):
        ctx = self._ctx
        if ctx.mute_notification_reply:
            return None, None, None, False
        stream_consumer = None
        scfg = getattr(getattr(self._runner, 'config', None), 'streaming', None)
        if scfg is None:
            from gateway.config import StreamingConfig
            scfg = StreamingConfig()
        # display.platforms.<plat>.streaming may disable streaming per platform; None = follow global.
        plat_streaming = ctx.resolve_display_setting(ctx.user_config, platform_key, "streaming")
        want_stream_deltas = not ctx.scheduled_heartbeat and (
            scfg.enabled and scfg.transport != "off" if plat_streaming is None else bool(plat_streaming)
        )
        want_interim_messages = bool(ctx.interim_assistant_messages_enabled) and not ctx.scheduled_heartbeat
        # Streaming-TTS delta sink wired by the turn setup (None when TTS is off).
        stts = ctx.streaming_tts_consumer_holder[0]
        if want_stream_deltas or want_interim_messages:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                adapter = self._runner._delivery_adapter_for(ctx.source)
                if adapter:
                    consumer_cfg, pause_typing_before_finalize = self._runner._build_stream_consumer_config(
                        ctx.source, scfg, adapter, on_missing_cursor="raise",
                    )
                    progress_queue = ctx.progress_queue

                    def _progress_narration_gate(evt):
                        """Narration gate: queue ('__gate__', evt) on the progress queue
                        BEFORE any tool rows the in-flight send's step can enqueue, so
                        the pump closes the old bubble and holds the next box's first
                        send until the narration lands (GatewayStreamConsumer::
                        _begin_progress_gate). The consumer releases the event (from
                        the agent worker thread — hence a threading.Event) whether the
                        send succeeded or failed."""
                        if progress_queue is None:
                            return
                        progress_queue.put(("__gate__", evt))

                    stream_consumer = GatewayStreamConsumer(
                        adapter=adapter, chat_id=ctx.source.chat_id, config=consumer_cfg,
                        metadata=ctx._status_thread_metadata,
                        adapter_provider=lambda: self._runner._delivery_adapter_for(
                            ctx.source
                        ),
                        on_new_message=(
                            (lambda: ctx.progress_queue.put(("__reset__",))) if ctx.progress_queue is not None else None
                        ),
                        on_progress_gate=(
                            _progress_narration_gate if ctx.progress_queue is not None else None
                        ),
                        on_before_finalize=pause_typing_before_finalize,
                        initial_reply_to_id=ctx.event_message_id, run_still_current=ctx._run_still_current,
                        on_visible_progress=ctx.mark_visible_progress,
                        visible_delivery_lock=ctx.delivery_lock(),
                        context_gate=ctx.wait_context_cards_delivered,
                    )
                    ctx.stream_consumer_holder[0] = stream_consumer
                    # #105341: a consumer created only for interim commentary (text streaming off)
                    # is never fed the final reply's deltas — mark it so the duplicate-risk
                    # diagnostic in ``_run_agent_mark_streamed_delivery`` stays silent.
                    stream_consumer.stream_deltas_enabled = want_stream_deltas
            except Exception as err:
                logger.debug("Could not set up stream consumer: %s", err)
        stream_delta_cb = None
        if stream_consumer is not None and want_stream_deltas:
            def stream_delta_cb(text: Optional[str]) -> None:
                if ctx._run_still_current():
                    stream_consumer.on_delta(text)

        def interim_assistant_cb(text: str, *, already_streamed: bool = False) -> None:
            if not ctx._run_still_current():
                return
            if stream_consumer is not None:
                stream_consumer.on_segment_break() if already_streamed else stream_consumer.on_commentary(text)
            elif not already_streamed and ctx._status_adapter and str(text or "").strip():
                self._send_status_text(text, ctx._status_thread_metadata, "interim_assistant_callback scheduling error")

        return stream_consumer, stream_delta_cb, interim_assistant_cb, want_interim_messages

    # ── agent resolution (cache reuse vs fresh build) ───────────────────────────────────────

    @dataclasses.dataclass
    class _CachedAgentLookup:
        agent: Any = None
        reused: bool = False
        evicted: Any = None  # agent evicted under the lock; released off-lock on a daemon thread

    def _skip_context_files(self, platform_key) -> bool:
        """gateway.platforms.<plat>.skip_context_files: messaging platforms may opt out of
        filesystem-heavy context-file discovery (SOUL.md, AGENTS.md, .cursorrules)."""
        platforms_cfg = (self._ctx.user_config.get("gateway") or {}).get("platforms") or {}
        # ``hermes gateway setup`` writes ``gateway.platforms`` as a LIST of enabled platform names,
        # not a dict; treat any non-dict shape as "no per-platform overrides" rather than crashing.
        if not isinstance(platforms_cfg, dict):
            return False
        return bool((platforms_cfg.get(platform_key) or {}).get("skip_context_files"))

    def _cached_sid_is_dead(self, cache_lock, cache) -> tuple:
        """(peeked cached session_id, is_dead) — checked OUTSIDE the cache lock. "cached sid != current
        sid" normally means an intentional switch (reuse), but the routing-key self-heal yields the same
        shape with an agent bound to a DEAD session; reusing it re-binds the dead sid and loops."""
        ctx = self._ctx
        peek_sid = None
        if cache_lock and cache is not None:
            with cache_lock:
                entry = cache.get(ctx.session_key)
            if entry and len(entry) > 3:
                peek_sid = entry[3]
        dead = False
        if peek_sid is not None and ctx.session_id is not None and peek_sid != ctx.session_id:
            with suppress(Exception):
                dead = self._runner.session_store._is_session_ended_in_db(peek_sid)
        return peek_sid, dead

    def _current_message_count(self):
        """Cross-process write guard input: the session's current DB message_count (or None)."""
        ctx = self._ctx
        if self._runner._session_db is None or not ctx.session_id:
            return None
        count = None
        with suppress(Exception):
            # run_sync is off-loop (executor); sync DB is fine.
            row = self._runner._session_db._db.get_session(ctx.session_id)
            if row:
                count = row.get("message_count", 0)
        return count

    def _pop_cached_agent_for_eviction(self):
        """Evict under the lock but DEFER release (release_clients can block on memory-provider /
        socket teardown while the idle sweeper waits on this lock). The turn rebuilds a fresh agent, so
        the caller does a SOFT release that keeps sandbox / browser / bg processes."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        evicted = self._runner._agent_cache.pop(self._ctx.session_key, None)
        agent = evicted[0] if isinstance(evicted, tuple) and evicted else None
        return agent if agent and agent is not _AGENT_PENDING_SENTINEL else None

    def _lookup_cached_agent(self, sig, cache_lock, cache, max_iterations, peek_sid, dead, msg_count):
        ctx = self._ctx
        out = self._CachedAgentLookup()
        if not (cache_lock and cache is not None):
            return out
        with cache_lock:
            cached = cache.get(ctx.session_key)
            if not (cached and cached[1] == sig):
                return out
            # cached[2] = message_count at cache time (stale when a second process appended rows);
            # cached[3] = the session_id the snapshot was taken for.
            cached_mc = cached[2] if len(cached) > 2 else None
            cached_sid = cached[3] if len(cached) > 3 else None
            # Same session_key, other conversation: the counts track DIFFERENT DB rows, so the
            # comparison is meaningless — REUSE rather than bust the prompt cache on every switch.
            sid_mismatch = cached_sid is not None and ctx.session_id is not None and cached_sid != ctx.session_id
            # Re-validate the outside-lock dead-session peek against the tuple read under THIS lock:
            # a stale "dead" verdict must never be applied to a different (possibly live) agent.
            if sid_mismatch and dead and cached_sid == peek_sid:
                logger.info(
                    "Agent cache invalidated for session %s: "
                    "cached agent's session_id %s is ended in "
                    "state.db (stale self-heal artifact, "
                    "#54878 x #54947) — discarding instead of "
                    "reusing across the routing recovery", ctx.session_key, cached_sid,
                )
            elif not sid_mismatch and cached_mc is not None and msg_count is not None and msg_count != cached_mc:
                logger.info(
                    "Agent cache invalidated for session %s: "
                    "message_count changed (%s -> %s), "
                    "possible cross-process write", ctx.session_key, cached_mc, msg_count,
                )
            else:
                out.agent = cached[0]
                # Refresh LRU order so cap enforcement evicts truly-oldest entries.
                if hasattr(cache, "move_to_end"):
                    with suppress(KeyError):
                        cache.move_to_end(ctx.session_key)
                self._runner._init_cached_agent_for_turn(out.agent, ctx._interrupt_depth)
                # Cached agent may have been created with old config.
                out.agent.max_iterations = max_iterations
                logger.debug("Reusing cached agent for session %s", ctx.session_key)
                out.reused = True
                return out
            out.evicted = self._pop_cached_agent_for_eviction()
        return out

    def _release_evicted_agent(self, agent) -> None:
        """Off-lock soft release on a daemon thread so teardown never blocks the gateway loop."""
        self._runner._spawn_release_thread(
            self._runner._release_evicted_agent_soft, (agent,), f"agent-xproc-evict-{str(self._ctx.session_key)[:24]}",
            inline_fallback=True,
        )

    def _build_fresh_agent(self, turn_route, platform_key, combined_ephemeral, max_iterations,
                           reasoning_config, pr, skip_context_files):
        from gateway.run import _checkpoint_agent_kwargs
        ctx = self._ctx
        runner = self._runner
        src = ctx.source
        return ctx.AIAgent(
            model=turn_route["model"], **turn_route["runtime"], **_checkpoint_agent_kwargs(ctx.user_config),
            max_iterations=max_iterations, quiet_mode=True, verbose_logging=False,
            enabled_toolsets=ctx.enabled_toolsets, disabled_toolsets=ctx.disabled_toolsets,
            ephemeral_system_prompt=combined_ephemeral or None,
            prefill_messages=runner._prefill_messages or None,
            reasoning_config=reasoning_config, service_tier=runner._service_tier,
            request_overrides=turn_route.get("request_overrides"),
            providers_allowed=pr.get("only"), providers_ignored=pr.get("ignore"), providers_order=pr.get("order"),
            provider_sort=pr.get("sort"), provider_require_parameters=pr.get("require_parameters", False),
            provider_data_collection=pr.get("data_collection"),
            session_id=ctx.session_id, platform=platform_key,
            user_id=src.user_id, user_id_alt=src.user_id_alt, user_name=src.user_name,
            chat_id=src.chat_id, chat_name=src.chat_name, chat_type=src.chat_type, thread_id=src.thread_id,
            gateway_session_key=ctx.session_key,
            session_db=getattr(runner._session_db, "_db", runner._session_db),
            # Reload from disk — do not reuse the startup snapshot.
            # See #60955.
            fallback_model=self._runner._refresh_fallback_model(),
            skip_context_files=skip_context_files,
            # Keep the persona even with minimal context: soul identity is one small file.
            load_soul_identity=True,
        )

    def _resolve_turn_agent(self, turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr):
        """Reuse this session's cached AIAgent (frozen system prompt + tool schemas → prompt cache
        hits) or build a fresh one. Returns (agent, reused_cached_agent)."""
        ctx = self._ctx
        runner = self._runner
        skip_context_files = self._skip_context_files(platform_key)
        sig = runner._agent_config_signature(
            turn_route["model"], turn_route["runtime"], ctx.enabled_toolsets, combined_ephemeral,
            cache_keys=runner._extract_cache_busting_config(ctx.user_config),
            user_id=getattr(ctx.source, "user_id", None),
            user_id_alt=getattr(ctx.source, "user_id_alt", None),
            skip_context_files=skip_context_files,
        )
        cache_lock = getattr(runner, "_agent_cache_lock", None)
        cache = getattr(runner, "_agent_cache", None)
        peek_sid, dead = self._cached_sid_is_dead(cache_lock, cache)
        msg_count = self._current_message_count()
        found = self._lookup_cached_agent(sig, cache_lock, cache, max_iterations, peek_sid, dead, msg_count)
        agent = found.agent
        # Lock released — refresh the reused agent's fallback chain from disk OUTSIDE the cache lock
        # (disk I/O under the lock stalls the idle-sweep watcher and Discord heartbeats). A chain
        # configured after caching must reach the next turn; per-session serialization keeps it safe.
        if found.reused and agent is not None:
            self._runner._apply_fallback_chain_to_agent(agent, runner._refresh_fallback_model())
        if found.evicted is not None:
            self._release_evicted_agent(found.evicted)
        if agent is None:
            agent = self._build_fresh_agent(
                turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr, skip_context_files,
            )
            if cache_lock and cache is not None:
                with cache_lock:
                    # Record the snapshot's session_id with message_count so the cross-process guard
                    # can skip the meaningless count comparison if the active session_id switches.
                    cache[ctx.session_key] = (agent, sig, msg_count, ctx.session_id)
                    runner._enforce_agent_cache_cap()
            logger.debug("Created new agent for session %s (sig=%s)", ctx.session_key, sig)
        return agent, found.reused

    # ── per-turn agent wiring ───────────────────────────────────────────────────────────────

    def _notice_callback_sync(self, notice) -> None:
        """Credits / out-of-band notices (usage bands, depletion, restored) fire from the agent's
        sync worker thread; hop onto the gateway loop. Fired-once latch lives on the cached agent."""
        from gateway.run import render_notice_line
        from gateway.warning_notifications import is_diagnostic_notice, render_notification
        if self._ctx.mute_notification_reply or not self._status_live():
            return
        diagnostic = is_diagnostic_notice(notice)
        def present():
            try:
                line = render_notice_line(notice)
            except Exception:
                logger.debug("render_notice_line failed", exc_info=True)
                return
            if line:
                self._schedule(self._runner._deliver_platform_notice(self._ctx.source, line), "notice_callback delivery scheduling error")
        render_notification(present, platform=self._ctx.source.platform,
                            user_config=self._ctx.user_config, diagnostic=diagnostic)

    def _make_bg_review_callbacks(self):
        """(send, release): background-review messages ("💾 Memory updated") are held until the
        adapter's post-delivery hook releases them after the main response lands."""
        from gateway.run import _interim_metadata, _non_conversational_metadata
        ctx = self._ctx
        release_evt = threading.Event()
        pending: list[str] = []
        pending_lock = threading.Lock()

        def deliver(message: str) -> None:
            if self._status_live():
                self._send_status_text(
                    message,
                    _interim_metadata(_non_conversational_metadata(ctx._status_thread_metadata, platform=ctx.source.platform)),
                    "background_review_callback scheduling error",
                )

        def release() -> None:
            release_evt.set()
            with pending_lock:
                queued = list(pending)
                pending.clear()
            for message in queued:
                deliver(message)

        def send(message: str) -> None:
            if not self._status_live():
                return
            if not release_evt.is_set():
                with pending_lock:
                    if not release_evt.is_set():
                        pending.append(message)
                        return
            deliver(message)

        return send, release

    @staticmethod
    def _merge_turn_request_overrides(agent, turn_route) -> None:
        """Merge, never overwrite: init-time request overrides (e.g. a custom provider's extra_body)
        must survive every reused-agent turn. Drop only the PREVIOUS turn's routing overrides before
        layering this turn's, so stale per-turn values never linger."""
        overrides = dict(getattr(agent, "request_overrides", {}) or {})
        for key, value in (getattr(agent, "_gateway_turn_request_overrides", {}) or {}).items():
            if overrides.get(key) == value:
                overrides.pop(key, None)
        turn_overrides = dict(turn_route.get("request_overrides") or {})
        overrides.update(turn_overrides)
        agent.request_overrides = overrides
        agent._gateway_turn_request_overrides = turn_overrides

    def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config,
                                   stream_delta_cb, interim_assistant_cb, want_interim_messages):
        """Per-message state — callbacks and reasoning config change every turn, so they aren't
        baked into the cached agent."""
        ctx = self._ctx
        runner = self._runner
        agent._notification_config = ctx.user_config
        agent._notification_platform = ctx.source.platform
        # ALWAYS attached (never gated to None): its body gates each event class, and subagent-
        # failure notices must fire even with tool_progress/thinking off.
        agent.tool_progress_callback = ctx.progress_callback
        # Slack's task cards ride the authoritative start callback, so they never infer
        # identity from tool names.
        agent.tool_start_callback = ctx.native_tool_start_callback if ctx._native_slack_task_cards else None
        agent.tool_complete_callback = ctx.native_tool_complete_callback if ctx._native_slack_task_cards else None
        agent.step_callback = ctx._step_callback_sync if ctx._hooks_ref.loaded_hooks else None
        agent.stream_delta_callback = stream_delta_cb
        agent.interim_assistant_callback = interim_assistant_cb if want_interim_messages else None
        agent.status_callback, agent.notice_callback = ctx._status_callback_sync, self._notice_callback_sync
        agent.notice_clear_callback = None  # sends can't be retracted
        agent.event_callback = ctx._event_callback_sync
        agent.reasoning_config, agent.service_tier = reasoning_config, runner._service_tier
        self._merge_turn_request_overrides(agent, turn_route)
        # Must-deliver notes for THIS turn ride the current user message (api_content sidecar), never
        # the system prompt. Assigned unconditionally so a reused agent never replays a stale note.
        agent._gateway_turn_context_notes = "\n\n".join(runner._consume_pending_turn_sidecar_notes(ctx.session_key))
        agent.background_review_callback, bg_release = self._make_bg_review_callbacks()
        # Register the release hook on the adapter so base.py's finally block fires it after the
        # main response is delivered.
        if ctx._status_adapter and ctx.session_key:
            if getattr(type(ctx._status_adapter), "register_post_delivery_callback", None) is not None:
                ctx._status_adapter.register_post_delivery_callback(ctx.session_key, bg_release, generation=ctx.run_generation)
            else:
                pdc = getattr(ctx._status_adapter, "_post_delivery_callbacks", None)
                if pdc is not None:
                    pdc[ctx.session_key] = bg_release
        # display.memory_notifications: off | on (generic "💾 Memory updated", default) | verbose.
        # `display:` present-but-null yields None, not the {} default (same `or {}` guard as
        # display_config.py / runtime_footer.py).
        mem_notif = (ctx.user_config.get("display") or {}).get("memory_notifications")
        if isinstance(mem_notif, bool):
            mem_notif = "on" if mem_notif else "off"
        agent.memory_notifications = str(mem_notif).lower() if mem_notif else "on"
        agent.clarify_callback = self._clarify_callback_sync
        # Thinking between tool calls is independent of tool_progress mode (Mattermost opts in
        # per platform so global scratch-text doesn't leak into threads).
        agent.thinking_progress = ctx._thinking_enabled
        if ctx.mute_notification_reply:
            # Controls and operational event/step callbacks remain wired. These
            # presentation callbacks are rebound on every next turn.
            agent.tool_progress_callback = None
            agent.tool_start_callback = None
            agent.tool_complete_callback = None
            # Keep diagnostic observers installed; concrete sinks veto display.
            agent.stream_delta_callback = None
            agent.interim_assistant_callback = None
            agent.thinking_progress = False
        ctx.agent_holder[0] = agent  # interrupt support
        # The titler fires from the turn prologue, so attach the rename lane before the run.
        self._attach_session_title_callback(agent, ctx)
        # Publish turn ownership for /stop, /new, disconnect and shutdown interrupts; older session
        # processes are outside this baseline and remain alive.
        agent._gateway_turn_process_task_id, agent._gateway_turn_process_baseline = ctx.process_task_id, ctx.process_baseline
        ctx.tools_holder[0] = getattr(agent, "tools", None)  # transcript logging

    # ── blocking prompts from the agent thread (approval / clarify) ─────────────────────────

    def _close_native_stream_boundary(self, reason: str, placeholder: str | None = None, reopen: bool = False) -> bool:
        """Native-streaming platforms (e.g. WeCom): an interrupting interaction (approval or clarify
        prompt) must finalize the current stream first, or post-interaction output keeps updating the
        OLD bubble above the prompt. Runs on the agent thread; the consumer serializes via its queue."""
        sc = self._stream_consumer()
        if not (sc and getattr(sc, "_use_native_streaming", False)):
            return True
        cancelled_flag = None
        try:
            boundary = sc.close_for_approval_prompt(placeholder, reason=reason, reopen=reopen)
            # Returns (future, cancelled_flag) or just a future.
            if isinstance(boundary, tuple):
                boundary, cancelled_flag = boundary
            if not hasattr(boundary, "result"):
                return True
            ok = boundary.result(timeout=10)
            if not ok:
                logger.warning(
                    "%s boundary failed to close stream properly — "
                    "prompt may still appear in typing bubble", reason,
                )
            return bool(ok)
        except (TimeoutError, Exception) as err:
            if cancelled_flag is not None:
                cancelled_flag["cancelled"] = True
            logger.warning("%s boundary timed out or failed: %s", reason, err)
            return False

    def _clarify_callback_sync(self, question: str, choices, multi_select: bool = False,
                               questions=None) -> str:
        """Present a clarify prompt and block on a response (clarify_tool's synchronous contract):
        schedule send_clarify on the gateway loop, block on the primitive's threading.Event with a
        timeout. Returns the response string, or a sentinel when none arrived.

        ``questions`` (clarify_tool's batch form) is answered here, one card per question, because
        this surface knows whether an answer arrived: the loop that would otherwise call this
        callback once per question can only recognize "no answer" from the returned sentinel text,
        and treated that text as the question's answer.
        """
        if questions:
            return self._clarify_batch_sync(questions)
        response, _answered = self._ask_clarify_question(question, choices, multi_select)
        return response

    def _clarify_batch_sync(self, questions) -> str:
        """Answer a batch: one card per question, stop at the first the user never answers.
        Returns the JSON shape clarify_tool's batch path reads. The stream/typing re-arm waits for
        the last question — between two cards it only opens a bubble the next boundary closes."""
        answers: Dict[str, Any] = {}
        payload: Dict[str, Any] = {"answers": answers, "timed_out": False}
        last = len(questions) - 1
        for index, entry in enumerate(questions):
            raw, answered = self._ask_clarify_question(
                entry.get("question", ""), entry.get("choices"), bool(entry.get("multi_select")),
                rearm=index == last)
            if not answered:
                # The surface's own no-answer text ("could not be delivered", "did not respond
                # within Nm") rides along as ``notice``: blank answers alone read as user
                # inactivity, which is the misreport #112684 describes for an undelivered card.
                payload.update(timed_out=True, notice=raw)
                break
            answers[entry.get("qid") or f"q{index}"] = raw
        return json.dumps(payload, ensure_ascii=False)

    def _ask_clarify_question(self, question, choices, multi_select, rearm: bool = True) -> tuple[str, bool]:
        """One card: register, send, wait, then retire it (no answer) or re-arm (answer).
        Returns ``(response, answered)``; the caller decides what "no answer" means — a sentinel
        for a single question, the batch's ``timed_out`` flag."""
        from gateway.run_turn_runner_clarify_delivery import (
            UNDELIVERED_NO_SURFACE, _clarify_send_then_wait, text_fallback_coro)
        from tools import clarify_gateway as clarify_mod
        import uuid
        ctx = self._ctx
        if not ctx._status_adapter:
            # Nothing can render the question: say so, or the batch's blank answers read as
            # user inactivity (#112684).
            return UNDELIVERED_NO_SURFACE, False
        session_key = ctx.session_key or ""
        clarify_id = uuid.uuid4().hex[:10]
        choices = list(choices) if choices else None
        send_kwargs = dict(
            chat_id=ctx._status_chat_id, question=question, choices=choices, clarify_id=clarify_id,
            session_key=session_key, metadata=ctx._status_thread_metadata,
        )

        def _text_fallback():
            """Schedule the plain-text prompt when the native card cannot render; None = no such path."""
            coro = text_fallback_coro(ctx._status_adapter, **send_kwargs)
            return None if coro is None else self._schedule(coro, "Clarify text fallback failed to schedule")
        clarify_mod.register(
            clarify_id=clarify_id, session_key=session_key, question=question, choices=choices,
            multi_select=bool(multi_select),
        )
        # Unlike approval, clarify passes reopen=True so the continuation re-opens a native stream
        # below the question; if the re-seed fails the consumer degrades to send() automatically.
        self._close_native_stream_boundary("Clarify", "💬 等待你的选择...", reopen=True)
        # Pause typing: a "thinking..." status must not obscure the prompt or block an "Other" reply
        # on platforms that disable input while typing (Slack Assistant).
        with suppress(Exception):
            ctx._status_adapter.pause_typing_for_chat(ctx._status_chat_id)
        # Ordering barrier: flush buffered assistant prose BEFORE the poll, which goes out on a
        # separate agent-thread-blocking path and would otherwise render ABOVE its own explanation.
        # Best-effort + short timeout so the agent thread never hangs if the consumer isn't running.
        flush = getattr(self._stream_consumer(), "flush_pending_sync", None)
        try:
            if callable(flush):
                flush(timeout=3.0)
        except Exception:
            logger.debug("Stream-consumer flush before clarify prompt failed", exc_info=True)
        fut = self._schedule(
            ctx._status_adapter.send_clarify(**send_kwargs),
            "Clarify send failed to schedule",
        )
        # Boundary rule (see _approval_send_outcome): a send timeout is AMBIGUOUS — the card may
        # have posted with a late ack. Only a definitive failure tears down the registration;
        # ambiguous falls through to the bounded wait so a late reply resolves. A definitive
        # failure — immediate or late — retries once as plain text before giving up.
        response, answered = _clarify_send_then_wait(
            fut, clarify_id=clarify_id, session_key=session_key, clarify_mod=clarify_mod,
            fallback=_text_fallback)
        # Branch on the explicit flag, never on the text: a real answer can start with '[' (a
        # "[A] staging" label, "[urgent] ..." free text) and must not be mistaken for a sentinel.
        if not answered:
            # No answer arrived (timeout, /new, run end): retire the native card so it stops
            # looking answerable. Adapters without a persistent card have no such method.
            retire = getattr(type(ctx._status_adapter), "retire_clarify_card", None)
            if callable(retire):
                self._schedule(
                    retire(ctx._status_adapter, clarify_id, _CLARIFY_EXPIRED_NOTICE),
                    "Clarify card retire failed to schedule")
        elif rearm:
            # Reopen typing IMMEDIATELY, not on the LLM's first post-answer token (native streaming
            # otherwise re-seeds lazily on the first delta: ~48s of dead air). request_reopen_seed is
            # a no-op outside the reopen-pending native state.
            sc = self._stream_consumer()
            if sc is not None:
                try:
                    sc.request_reopen_seed()
                except Exception:
                    logger.debug("request_reopen_seed after clarify answer failed", exc_info=True)
            try:
                ctx._status_adapter.resume_typing_for_chat(ctx._status_chat_id)
            except Exception:
                logger.debug("resume_typing_for_chat after clarify answer failed", exc_info=True)
        return response, answered


    # ── run_sync phases ─────────────────────────────────────────────────────────────────────

    def _load_turn_history(self, agent, reused_cached_agent):
        from gateway.run import (
            _build_gateway_agent_history, _collect_history_media_paths, _message_timestamps_enabled,
            _select_cached_agent_history,
        )
        ctx = self._ctx
        # Transcript rows ({role, content, timestamp}) lose timestamps; interrupt-path agent messages
        # (tool_calls/tool_call_id/reasoning) pass through intact so the API sees valid assistant→tool
        # sequences. Telegram observed=True rows are withheld from replayable history and attached to
        # the current addressed message as API-only context.
        agent_history, observed_group_context, cursor_recovery_note = _build_gateway_agent_history(
            ctx.history, channel_prompt=ctx.channel_prompt,
            inject_timestamps=_message_timestamps_enabled(ctx.user_config),
            hermes_session_id=ctx.session_id,
        )
        # Recovery preparation decides whether this note is an ordinary new
        # message or must yield to the pure continuation seam.
        self._cursor_recovery_note = cursor_recovery_note
        self._live_history_selected = False
        self._live_replay_source = None
        # FTS write-corruption guard: if persistence failed silently the reloaded transcript is stale
        # while the SAME cached agent still holds the live conversation (same-session amnesia). Only
        # for a reused agent bound to this exact session_id.
        # Replacing the live transcript with that shorter copy causes immediate same-session amnesia. See
        # #50502.
        if reused_cached_agent and getattr(agent, "session_id", None) == ctx.session_id:
            selected = _select_cached_agent_history(agent_history, getattr(agent, "_session_messages", None))
            # Consecutive lagging turns per session (cleared on /new): one lag is a blip, a streak
            # is the #114266 write outage and must escalate past a repeating WARNING.
            streaks = getattr(self._runner, "_transcript_lag_streaks", None)
            if not isinstance(streaks, dict):  # tests may build bare runners
                streaks = self._runner._transcript_lag_streaks = {}
            if selected is not agent_history:
                self._live_history_selected = True
                streak = streaks[ctx.session_key] = streaks.get(ctx.session_key, 0) + 1
                log = logger.error if streak >= _TRANSCRIPT_LAG_ESCALATION_TURNS else logger.warning
                log(
                    "Persisted transcript lagged live cached history for "
                    "session %s (disk=%d, memory=%d, consecutive_turns=%d); preserving live "
                    "conversation context (possible FTS write corruption)%s",
                    ctx.session_key, len(agent_history), len(selected), streak,
                    "; state.db is not receiving this session's writes and needs operator attention"
                    if streak >= _TRANSCRIPT_LAG_ESCALATION_TURNS else "",
                )
                # The live history bypassed _build_gateway_agent_history's cleanup — re-apply
                # the full canonicalization so no replay transform can slip through.
                # Keep the authoritative pre-cleanup rows for forced replay
                # planning; canonicalization synthesizes UNKNOWN for a
                # dangling batch and would otherwise hide the replay work.
                self._live_replay_source = selected
                agent_history = canonicalize_replay_history(selected)
            else:
                streaks.pop(ctx.session_key, None)
        # MEDIA paths already in history are excluded from this turn's extraction (compression-safe).
        return agent_history, observed_group_context, _collect_history_media_paths(agent_history)

    def _prepend_pending_note(self, attr: str) -> None:
        """Consume a one-shot per-session note (model switch, /reload-skills) into the NEXT user
        message. Nothing hits the transcript out-of-band, so alternation stays intact."""
        ctx = self._ctx
        notes = getattr(self._runner, attr, None)
        note = notes.pop(ctx.session_key, None) if notes and ctx.session_key and ctx.session_key in notes else None
        if note:
            if isinstance(ctx.message, str):
                ctx.message = note + "\n\n" + ctx.message
            else:
                # A pure continuation has no current user message. Keep the
                # one-shot note for the next real user turn.
                notes[ctx.session_key] = note

    def _resume_note_interactive(self) -> bool:
        """Interactive platforms report the restore and ask what next; event platforms (webhook,
        API server) continue the work — nobody is present to answer."""
        return bool(getattr(self._runner._delivery_adapter_for(self._ctx.source), "interactive_resume", True))

    def _prepare_turn_message(self, agent_history, observed_group_context=None):
        """Prepare ordinary guidance or transparent forced-victim recovery."""
        from gateway.run_turn_recovery import prepare_turn_message

        return prepare_turn_message(self, agent_history, observed_group_context)

    def _native_image_run_message(self):
        """Wrap the user turn as an OpenAI-style multimodal content list when
        _prepare_inbound_message_text buffered image paths; consume-and-clear so later turns on the
        same runner never re-attach stale images. Falls back to plain text when nothing is readable."""
        ctx = self._ctx
        native_imgs = self._runner._consume_pending_native_image_paths(ctx.session_key)
        if not native_imgs:
            return ctx.message
        try:
            from agent.image_routing import build_native_content_parts
            parts, skipped = build_native_content_parts(ctx.message, native_imgs)
            if skipped:
                logger.warning("Native image attachment: skipped %d unreadable path(s): %s", len(skipped), skipped)
            if any(p.get("type") == "image_url" for p in parts):
                return parts
        except Exception as exc:
            logger.warning("Native image attachment failed, falling back to text: %s", exc)
        return ctx.message

    def _run_conversation_with_session(self, agent, agent_history, observed_group_context,
                                       persist_user_message_override, persist_user_timestamp_override,
                                       *, continue_interrupted_turn=False, forced_recovery_blocked=None):
        """Run the turn with its per-session tool context bound."""
        from gateway.run import _wrap_current_message_with_observed_context
        from tools.session_context import reset_current_session_key, set_current_session_key
        ctx = self._ctx
        session_key = ctx.session_key or ""
        # Re-read the current session's flag before every turn so cached agents and restarts
        # cannot drift from the persisted session metadata.
        with suppress(Exception):
            agent._incognito = bool(self._runner._is_session_incognito(ctx.source))
        token = set_current_session_key(session_key)
        try:
            api_message = _wrap_current_message_with_observed_context(self._native_image_run_message(), observed_group_context)
            kwargs = {"conversation_history": agent_history, "task_id": ctx.session_id}
            if _accepts_keyword(agent.run_conversation, "turn_author"):
                # Sent on every transport: a provider gating durable writes needs the bot flag in a DM too.
                kwargs["turn_author"] = {"id": ctx.source.user_id or None, "name": ctx.source.user_name or None,
                                         "is_bot": bool(getattr(ctx.source, "is_bot", False))}
            if persist_user_message_override is not None:
                kwargs["persist_user_message"] = persist_user_message_override
            elif observed_group_context:
                kwargs["persist_user_message"] = ctx.message
            if ctx.persist_user_display_kind:
                # Internal self-injected turn: type the persisted user row so UIs render it as a
                # timeline notice, not a user bubble (stripped from provider payloads downstream).
                kwargs["persist_user_display_kind"] = ctx.persist_user_display_kind
            if ctx.persist_user_display_metadata:
                kwargs["persist_user_display_metadata"] = ctx.persist_user_display_metadata
            if ctx.moa_config is not None:
                kwargs["moa_config"] = ctx.moa_config
            if persist_user_timestamp_override is not None:
                kwargs["persist_user_timestamp"] = persist_user_timestamp_override
            if continue_interrupted_turn:
                # Continue the original assistant/tool loop without adding a
                # synthetic user row or re-running new-user side channels.
                kwargs["continue_interrupted_turn"] = True
            # The RAW inbound id (not event_message_id, the reply anchor) rides the persisted user
            # turn so a restart-interrupted turn is recorded WITH its id for drain-window dedup.
            if ctx.inbound_message_id is not None:
                kwargs["persist_user_platform_id"] = str(ctx.inbound_message_id)
            if forced_recovery_blocked is not None:
                # Internal control outcome: recovery could not prove a safe
                # model boundary. Never call the model or fabricate prose.
                return {
                    "final_response": None,
                    "messages": [],
                    "completed": False,
                    "failed": True,
                    "interrupted": False,
                    "error": forced_recovery_blocked,
                    "forced_recovery_control": True,
                }
            from agent.notification_presentation import notification_turn
            with notification_turn(agent, muted=ctx.mute_notification_reply, session_id=ctx.session_id or ""):
                return agent.run_conversation(api_message, **kwargs)
        finally:
            # Cancel pending clarify entries so blocked agent threads don't hang past the end of the
            # run (interrupt, completion, gateway shutdown). Idempotent.
            with suppress(Exception):
                from tools.clarify_gateway import clear_session
                clear_session(session_key)
            reset_current_session_key(token)

    def _finish_stream_consumer(self, result, agent_history, stream_consumer):
        ctx = self._ctx
        # Canonicalize a model-emitted computer-use screenshot path at the common result boundary so
        # the streaming finalizer and the non-streaming delivery path see the same response.
        if isinstance(result, dict) and isinstance(result.get("final_response"), str):
            result["final_response"] = repair_explicit_computer_use_media_paths(
                result["final_response"], result.get("messages", []), history_offset=len(agent_history),
            )
        ctx.result_holder[0] = result
        if stream_consumer is None:
            return
        # Pass final_response as the authoritative finalize payload: it includes post-stream
        # augmentation (verifier footer, explainer) the accumulator never saw. Adopt ONLY a genuinely
        # completed final: interrupt paths return {interrupted: True, completed: False} with a
        # DIAGNOSTIC final_response — adopting it would seal the partial answer over with the
        # diagnostic AND suppress the gateway's own error delivery.
        _final_for_stream = None
        if (
            isinstance(result, dict) and not result.get("failed") and not result.get("interrupted")
            and result.get("completed") is not False
        ):
            fr = result.get("final_response")
            if isinstance(fr, str) and fr.strip() and fr != "(empty)":
                _final_for_stream = fr
        if _final_for_stream is None:
            stream_consumer.finish()
            return
        # Duck-type safe: test doubles / older consumers may expose a zero-arg finish().
        try:
            stream_consumer.finish(_final_for_stream)
        except TypeError:
            stream_consumer.finish()

    def _restore_telegram_thread_id_after_split(self, agent_session_id) -> None:
        """Telegram DM whose source.thread_id was lost in the session split (synthetic/recovered
        event): restore it from the binding so _thread_metadata_for_source yields the right
        message_thread_id instead of the General thread (non-fatal)."""
        ctx = self._ctx
        try:
            # run_sync is off-loop (executor); sync DB is fine.
            binding = self._runner._session_db._db.get_telegram_topic_binding_by_session(session_id=agent_session_id)
            if binding and binding.get("thread_id"):
                ctx.source.thread_id = str(binding["thread_id"])
                logger.debug(
                    "Restored source.thread_id=%s from binding after session split %s → %s",
                    ctx.source.thread_id, ctx.session_id, agent_session_id,
                )
        except Exception:
            logger.debug("Failed to restore thread_id from binding after session split", exc_info=True)

    def _sync_session_after_run(self, agent_history):
        """Sync session_id right after run_conversation(): compression can rotate before a follow-up
        model call fails, and the failure return must still point at the compressed child.
        Returns (compacted_in_place, effective_session_id, effective_history_offset)."""
        ctx = self._ctx
        runner = self._runner
        agent = ctx.agent_holder[0]
        # In-place compaction compacts the transcript WITHOUT rotating the id, so the id-change diff
        # can't see it; compress_context() sets this flag and the gateway re-baselines as for a split.
        compacted_in_place = bool(getattr(agent, "_last_compaction_in_place", False)) if agent else False
        agent_session_id = getattr(agent, 'session_id', ctx.session_id) if agent else ctx.session_id
        session_was_split = bool(agent and ctx.session_key and agent_session_id != ctx.session_id)
        if session_was_split:
            logger.info("Session split detected: %s → %s (compression)", ctx.session_id, agent_session_id)
            entry = runner.session_store._entries.get(ctx.session_key)
            persisted = False
            if entry:
                entry_session_id = getattr(entry, "session_id", None)
                if not ctx._run_still_current():
                    logger.info(
                        "Skipping session split sync for stale run %s — "
                        "generation %s is no longer current",
                        ctx.session_key or "?", ctx.run_generation,
                    )
                elif entry_session_id == agent_session_id:
                    persisted = True
                elif entry_session_id != ctx.session_id:
                    logger.info(
                        "Skipping session split sync for %s because the "
                        "session binding moved from %s to %s before "
                        "compression finished",
                        ctx.session_key or "?", ctx.session_id, entry_session_id,
                    )
                else:
                    entry.session_id = agent_session_id
                    runner.session_store._save()
                    runner.session_store._record_gateway_session_peer(agent_session_id, ctx.session_key, ctx.source)
                    persisted = True
            # Only after this run published its split — a stale /stop→/new predecessor must not
            # mutate routing state.
            if persisted:
                src = ctx.source
                if (
                    getattr(src, "platform", None) == Platform.TELEGRAM and getattr(src, "chat_type", None) == "dm"
                    and getattr(src, "thread_id", None) is None and runner._session_db is not None
                ):
                    self._restore_telegram_thread_id_after_split(agent_session_id)
                runner._sync_telegram_topic_binding(src, entry, reason="agent-run-compression")
        runner._sync_session_model_from_agent(agent_session_id, agent)
        # history_offset=0 whenever the agent's message list lost the original history prefix
        # (split OR in-place compaction): the returned `messages` is the compacted set, persist all
        # of it; slicing past the pre-compaction length would drop everything.
        offset = 0 if (session_was_split or compacted_in_place) else len(agent_history)
        return compacted_in_place, agent_session_id, offset

    def _combined_ephemeral_prompt(self) -> str:
        """Platform context + YAML channel_prompts hint + channel_overrides system_prompt (or global
        ephemeral) + the gateway ephemeral prompt."""
        ctx = self._ctx
        combined = ctx.context_prompt or ""
        for extra in (
            (ctx.channel_prompt or "").strip(),
            self._runner._get_system_prompt_for_channel(
                ctx.source.platform, ctx.source.chat_id or "", thread_id=getattr(ctx.source, "thread_id", None),
                parent_id=getattr(ctx.source, "parent_chat_id", None),
            ),
        ):
            if extra:
                combined = (combined + "\n\n" + extra).strip()
        return combined

    def _append_auto_media_tags(self, final_response: str, result, agent_history, history_media_paths) -> str:
        """Append MEDIA:<path> tags from tool results (e.g. TTS) that the model's final text omits, so
        extract_media() delivers each file once. Scoped to THIS turn (slice at len(agent_history)) so
        a stale MEDIA: path from an earlier turn never rides a later reply; the history-path dedup is
        the secondary guard — and the sole one when mid-run compression shrank the list."""
        from gateway.run import _collect_auto_append_media_tags
        if "MEDIA:" in final_response:
            return final_response
        # Scan tool results for MEDIA:<path> tags that need to be delivered as native audio/file
        # attachments. The TTS tool embeds MEDIA: tags in its JSON response, but the model's final text
        # reply usually doesn't include them. We collect unique tags from tool results and append any that
        # aren't already present in the final response, so the adapter's extract_media() can find and
        # deliver the files exactly once. Scope the scan to THIS turn's tool results only. ``agent_history``
        # was passed into run_conversation as ``conversation_history``, so the agent's returned ``messages``
        # list is ``agent_history`` followed by the messages produced this turn. Slicing at
        # ``len(agent_history)`` isolates the current turn precisely, so a stale MEDIA: path emitted by a
        # tool several turns earlier (still present in the full message list) can never leak onto a later
        # text-only reply. (Fixes #34608) Path-based deduplication against _history_media_paths (collected
        # before run_conversation) is retained as a secondary guard. It is also the sole guard on the
        # fallback branch taken when mid-run context compression shrinks the message list below the original
        # history length, preserving the compression-safe behaviour of #160.
        media_tags = _collect_auto_append_media_tags(
            result.get("messages", []), history_offset=len(agent_history), history_media_paths=history_media_paths,
        )
        if not media_tags:
            return final_response
        unique_tags = list(dict.fromkeys(media_tags))
        return final_response + "\n" + "\n".join(unique_tags)

    def run_sync(self):
        """Executor-thread body of the turn; returns the gateway result dict.

        The turn message lives on the shared TurnContext (``ctx.message``) so ``_run_agent_inner`` sees
        every rebind. session_key propagates via contextvars (_set_session_env / set_current_session_key),
        never process-global environment state.
        """
        from gateway.run import (
            _current_max_iterations, _is_forced_recovery_control_outcome,
            _is_gateway_restart_control_outcome, _normalize_empty_agent_response,
            _sanitize_gateway_final_response,
        )
        ctx = self._ctx
        runner = self._runner
        # Platform.LOCAL ("local") maps to the "cli" hint key the agent understands.
        # session_key is propagated via contextvars in _set_session_env() and
        # set_current_session_key(), both concurrency-safe and inherited by tool worker threads.
        # We deliberately do NOT write os.environ["HERMES_SESSION_KEY"] here: os.environ is
        # process-global, so concurrent gateway sessions would clobber each other's value.
        # CLI and cron bind through contextvars; only a separate slash-worker subprocess exports
        # HERMES_SESSION_KEY from its own argv.
        platform_key = "cli" if ctx.source.platform == Platform.LOCAL else ctx.source.platform.value
        combined_ephemeral = self._combined_ephemeral_prompt()
        max_iterations = _current_max_iterations()
        try:
            model, runtime_kwargs = runner._resolve_session_agent_runtime(
                source=ctx.source, session_key=ctx.session_key, user_config=ctx.user_config,
            )
            # Stashed by _resolve_session_agent_runtime when the primary's credentials failed and a
            # fallback was resolved before any agent exists (#74349); one-shot per turn.
            pending_fallback_notice = getattr(runner, "_pre_agent_fallback_notice", None)
            runner._pre_agent_fallback_notice = None
            logger.debug(
                "run_agent resolved: model=%s provider=%s session=%s",
                model, runtime_kwargs.get("provider"), ctx.session_key or "",
            )
        except Exception as exc:
            # Model/credential resolution failed before the turn began; the raw text (URLs, status
            # codes) belongs in the log, and the chat gets the commands that fix it.
            logger.warning("Model resolution failed for session %s: %s", ctx.session_key or "", exc)
            from hermes_cli.auth import is_rate_limited_auth_error
            if is_rate_limited_auth_error(exc.__cause__):
                # Quota cap with valid credentials: /login cannot help; name the reset window (#89401).
                from gateway.run import _gateway_provider_error_reply
                return {"final_response": _gateway_provider_error_reply(str(exc)),
                        "messages": [], "api_calls": 0, "tools": [],
                        "served_model": None, "requested_model": None}
            return {
                "final_response": (
                    "⚠️ I couldn't connect to the AI model service, so this message wasn't processed. "
                    "Use /login to sign in again, or /model to pick a different model. If it keeps "
                    "failing, run `hermes doctor` on the host."),
                "messages": [], "api_calls": 0, "tools": [],
                "served_model": None, "requested_model": None,
            }
        pr = runner._provider_routing
        reasoning_config = runner._resolve_session_reasoning_config(source=ctx.source, session_key=ctx.session_key, model=model)
        runner._reasoning_config = reasoning_config
        runner._service_tier = runner._resolve_session_service_tier(source=ctx.source, session_key=ctx.session_key)
        stream_consumer, stream_delta_cb, interim_cb, want_interim = self._setup_stream_consumer(platform_key)
        turn_route = runner._resolve_turn_agent_config(ctx.message, model, runtime_kwargs)
        agent, reused_cached_agent = self._resolve_turn_agent(
            turn_route, platform_key, combined_ephemeral, max_iterations, reasoning_config, pr,
        )
        if pending_fallback_notice:
            # Reuse the in-agent one-shot notice so the pre-agent provider switch is user-visible too.
            agent._pending_fallback_notice = pending_fallback_notice
        self._wire_turn_agent_callbacks(agent, turn_route, reasoning_config, stream_delta_cb, interim_cb, want_interim)
        agent_history, observed_group_context, history_media_paths = self._load_turn_history(agent, reused_cached_agent)
        preparation = self._prepare_turn_message(agent_history, observed_group_context)
        agent_history = preparation.agent_history
        observed_group_context = preparation.observed_group_context
        history_media_paths = preparation.history_media_paths
        result = self._run_conversation_with_session(
            agent, agent_history, observed_group_context,
            preparation.persist_user_message, preparation.persist_user_timestamp,
            continue_interrupted_turn=preparation.continue_interrupted_turn,
            forced_recovery_blocked=preparation.blocked_error,
        )
        self._finish_stream_consumer(result, agent_history, stream_consumer)
        # The streaming-TTS consumer's finish() runs on the outer loop thread after the executor
        # returns, so early run_sync returns are also finalised.
        # See the outer finally/completion section below. See #60671.
        final_response = result.get("final_response")
        # Actual token counts from the agent instance used for this run.
        agent = ctx.agent_holder[0]
        has_comp = bool(agent) and hasattr(agent, "context_compressor")
        comp = agent.context_compressor if has_comp else None
        usage = {
            "last_prompt_tokens": getattr(comp, "last_prompt_tokens", 0) if has_comp else 0,
            "input_tokens": getattr(agent, "session_prompt_tokens", 0) if has_comp else 0,
            "output_tokens": getattr(agent, "session_completion_tokens", 0) if has_comp else 0,
            "model": getattr(agent, "model", None) if agent else None,
            "context_length": (getattr(comp, "context_length", 0) or 0) if has_comp else 0,
        }
        compacted_in_place, effective_session_id, history_offset = self._sync_session_after_run(agent_history)
        # failure_reason must survive the empty-response path too (TUI billing, transient-failure
        # persistence). compression_deferred (soft lock-contention defer) is distinct from
        # compression_exhausted so the gateway never auto-resets a session a concurrent compressor is
        # about to shrink.
        common = {
            "messages": result.get("messages", []), "api_calls": result.get("api_calls", 0),
            **turn_metrics(result),
            "failed": result.get("failed", False), "failure_reason": result.get("failure_reason"),
            "partial": result.get("partial", False), "completed": result.get("completed"),
            "interrupted": result.get("interrupted", False), "interrupt_message": result.get("interrupt_message"),
            "error": result.get("error"),
            "served_model": result.get("served_model"),
            "requested_model": result.get("requested_model"),
            "compression_exhausted": result.get("compression_exhausted", False),
            "compression_deferred": result.get("compression_deferred", False),
            "tools": ctx.tools_holder[0] or [],
            "history_offset": history_offset, "compacted_in_place": compacted_in_place, "session_id": effective_session_id,
            **usage,
        }
        if _is_forced_recovery_control_outcome(result):
            # Deliberately silent: no empty-response normalization, error
            # prose, auto-media tags, or persistence rows for a forced victim.
            return {"final_response": "", "forced_recovery_control": True, **common}
        if not final_response:
            restart_control = _is_gateway_restart_control_outcome(result)
            if not restart_control:
                final_response = _normalize_empty_agent_response(result, final_response or "", history_len=len(agent_history))
                final_response = _sanitize_gateway_final_response(ctx.source.platform, final_response)
                if not final_response:
                    final_response = f"⚠️ {result['error']}" if result.get("error") else ""
            # NOTE: deliberately omits agent_persisted/last_reasoning/response_* — the caller
            # defaults agent_persisted differently when the key is absent.
            return {
                "final_response": final_response,
                "gateway_restart_queued": restart_control,
                **common,
            }
        final_response = self._append_auto_media_tags(final_response, result, agent_history, history_media_paths)
        # Auto-titling runs at TURN START (agent/turn_context.py) from the user's message alone, so a
        # failed/interrupted turn is still titled.
        return {
            "final_response": final_response, "last_reasoning": result.get("last_reasoning"), **common,
            "response_previewed": result.get("response_previewed", False),
            "response_transformed": result.get("response_transformed", False),
            # Lets the persistence block tell whether the codex app-server path self-persisted (it
            # didn't — see codex_runtime.py); default True keeps skip-db for the standard runtime.
            "agent_persisted": result.get("agent_persisted", True),
        }

    # --- Tool-stage embed delivery (Discord) -------------------------------
    # Moved verbatim from gateway/run.py TurnRunner: run_turn._run_agent_inner
    # (kept by #569) subscribes turn_runner.tool_stage_event_callback and
    # drains send_tool_stage_embeds(), but builds this TurnRunner.

    def tool_stage_event_callback(self, event: dict) -> None:
        """Bus subscriber: enqueue one stage event from a tool worker thread."""
        ctx = self._ctx
        if not ctx.stage_event_queue or not ctx._run_still_current():
            return
        try:
            ctx.stage_event_queue.put_nowait(event)
        except Exception:
            logger.debug("tool stage event enqueue failed", exc_info=True)

    def _collect_final_stage_events(self) -> list:
        """Drain the stage queue; one event per invocation (terminal preferred)."""
        ctx = self._ctx
        by_invocation: Dict[str, list] = {}
        while not ctx.stage_event_queue.empty():
            try:
                event = ctx.stage_event_queue.get_nowait()
            except Exception:
                break
            invocation = str(event.get("invocation_id") or "")
            if not invocation:
                continue
            by_invocation.setdefault(invocation, []).append(event)
        to_deliver: list = []
        for events in by_invocation.values():
            terminal = next((e for e in reversed(events) if e.get("terminal")), None)
            to_deliver.append(terminal if terminal is not None else events[-1])
        return to_deliver

    _STAGE_DELIVERY_RECOVER_TIMEOUT = 10.0

    _STAGE_STOP_DRAIN_TIMEOUT = 1.0

    _STAGE_OUTCOME_DELIVERED = "delivered"

    _STAGE_OUTCOME_FAILED = "failed"

    _STAGE_OUTCOME_UNKNOWN = "unknown"

    async def _deliver_tool_stage_event(
        self, event: dict, embed_msg_ids: Dict[str, str]
    ) -> bool:
        """Return True when the adapter reported success (side effect happened)."""
        ctx = self._ctx
        adapter = ctx._stage_embed_adapter
        if adapter is None:
            return False
        invocation = str(event.get("invocation_id") or "")
        if not invocation:
            return False
        try:
            message_id = embed_msg_ids.get(invocation)
            if message_id is None:
                result = await ctx.deliver_visible(lambda: adapter.send_tool_stage_embed(
                    ctx.source.chat_id,
                    event,
                    metadata=ctx._progress_metadata,
                    reply_to=ctx._progress_reply_to,
                ))
            else:
                result = await ctx.deliver_visible(lambda: adapter.edit_tool_stage_embed(
                    ctx.source.chat_id,
                    message_id,
                    event,
                    metadata=ctx._progress_metadata,
                ))
            if result is not None and getattr(result, "success", False):
                new_id = getattr(result, "message_id", None)
                if new_id:
                    embed_msg_ids[invocation] = str(new_id)
                return True
            # Fail soft: forget the mapping so the NEXT event
            # (usually the terminal one) sends a fresh embed
            # instead of hammering a dead message id. A rendering
            # failure must never alter the MoA tool's result.
            embed_msg_ids.pop(invocation, None)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(
                "tool stage embed delivery failed: %s", e, exc_info=True
            )
            embed_msg_ids.pop(invocation, None)
            return False

    _STAGE_STATE_EMPTY = "empty"

    _STAGE_STATE_IN_FLIGHT = "in_flight"

    _STAGE_STATE_DELIVERED = "delivered"

    _STAGE_STATE_FAILED = "failed"

    _STAGE_STATE_FAILED_FINAL = "failed_final"

    _STAGE_STATE_UNKNOWN = "unknown"

    @staticmethod
    def _fresh_stage_delivery_record() -> dict:
        return {"state": TurnRunner._STAGE_STATE_EMPTY, "attempts": 0, "task": None}

    def _sync_consume_stage_delivery_task(self, record: dict) -> None:
        task = record.get("task")
        if task is None or not task.done():
            return
        try:
            delivered = bool(task.result())
        except Exception:
            delivered = False
        if delivered:
            record["state"] = self._STAGE_STATE_DELIVERED
        elif record["attempts"] >= 2:
            record["state"] = self._STAGE_STATE_FAILED_FINAL
        else:
            record["state"] = self._STAGE_STATE_FAILED
        record["task"] = None

    def _pin_stage_delivery_task(self, task: asyncio.Task) -> None:
        detached = getattr(self, "_stage_delivery_detached_tasks", None)
        if detached is None:
            detached = self._stage_delivery_detached_tasks = set()
        detached.add(task)

        def _release_detached(t: asyncio.Task) -> None:
            if t is not task:
                return
            if not t.cancelled():
                t.exception()
            detached.discard(t)

        task.add_done_callback(_release_detached)

    def _claim_tool_stage_delivery(
        self,
        event: dict,
        embed_msg_ids: Dict[str, str],
        records: Dict[str, dict],
        *,
        retry: bool = False,
    ) -> Optional[asyncio.Task]:
        """Synchronous claim — the only ``create_task`` site for stage deliveries."""
        invocation = str(event.get("invocation_id") or "")
        if not invocation:
            return None

        record = records.setdefault(invocation, self._fresh_stage_delivery_record())

        if record["state"] == self._STAGE_STATE_UNKNOWN:
            task = record.get("task")
            if task is not None and not task.done():
                return None

        self._sync_consume_stage_delivery_task(record)

        if retry:
            if (
                record["state"] != self._STAGE_STATE_FAILED
                or record["attempts"] != 1
            ):
                return None
        else:
            if record["state"] in (
                self._STAGE_STATE_DELIVERED,
                self._STAGE_STATE_FAILED_FINAL,
            ):
                record = records[invocation] = self._fresh_stage_delivery_record()
            elif record["state"] == self._STAGE_STATE_FAILED:
                record = records[invocation] = self._fresh_stage_delivery_record()
            elif record["state"] == self._STAGE_STATE_UNKNOWN:
                task = record.get("task")
                if task is not None and not task.done():
                    return None
                record = records[invocation] = self._fresh_stage_delivery_record()
            else:
                task = record.get("task")
                if (
                    record["state"] == self._STAGE_STATE_IN_FLIGHT
                    and task is not None
                    and not task.done()
                ):
                    return None

        record["attempts"] += 1
        task = asyncio.create_task(
            self._deliver_tool_stage_event(event, embed_msg_ids)
        )
        record["task"] = task
        record["state"] = self._STAGE_STATE_IN_FLIGHT
        return task

    async def _harvest_tool_stage_delivery(
        self, task: asyncio.Task, record: dict
    ) -> str:
        """Bounded harvest via ``asyncio.wait`` — never cancels the delivery task."""
        done, _pending = await asyncio.wait(
            {task}, timeout=self._STAGE_DELIVERY_RECOVER_TIMEOUT
        )
        if task in done:
            try:
                delivered = bool(task.result())
            except Exception:
                delivered = False
            if delivered:
                record["state"] = self._STAGE_STATE_DELIVERED
                record["task"] = None
                return self._STAGE_OUTCOME_DELIVERED
            if record["attempts"] >= 2:
                record["state"] = self._STAGE_STATE_FAILED_FINAL
                record["task"] = None
                return self._STAGE_OUTCOME_FAILED
            record["state"] = self._STAGE_STATE_FAILED
            record["task"] = None
            return self._STAGE_OUTCOME_FAILED

        record["state"] = self._STAGE_STATE_UNKNOWN
        record["task"] = task
        self._pin_stage_delivery_task(task)
        return self._STAGE_OUTCOME_UNKNOWN

    def _handle_stage_delivery_cancel(
        self, task: asyncio.Task, record: dict
    ) -> None:
        """Fully synchronous cancel handler — no awaits, no new deliveries."""
        if task.done():
            self._sync_consume_stage_delivery_task(record)
        else:
            record["state"] = self._STAGE_STATE_UNKNOWN
            record["task"] = task
            self._pin_stage_delivery_task(task)

    def _invocation_has_live_unknown_delivery(
        self, records: Dict[str, dict], invocation_id: str
    ) -> bool:
        if not invocation_id:
            return False
        record = records.get(invocation_id)
        if record is None or record["state"] != self._STAGE_STATE_UNKNOWN:
            return False
        task = record.get("task")
        return task is not None and not task.done()

    async def _drain_parked_tool_stage_events(
        self,
        embed_msg_ids: Dict[str, str],
        records: Dict[str, dict],
        parked: Dict[str, list],
    ) -> None:
        """Deliver parked events once their invocation's UNKNOWN pin resolves."""
        if not parked:
            return
        for invocation in list(parked.keys()):
            if self._invocation_has_live_unknown_delivery(records, invocation):
                continue
            events = parked.pop(invocation, [])
            for event in events:
                await self._process_tool_stage_event(
                    event, embed_msg_ids, records, parked
                )

    async def _process_tool_stage_event(
        self,
        event: dict,
        embed_msg_ids: Dict[str, str],
        records: Dict[str, dict],
        parked: Optional[Dict[str, list]] = None,
    ) -> None:
        invocation = str(event.get("invocation_id") or "")
        if (
            parked is not None
            and invocation
            and self._invocation_has_live_unknown_delivery(records, invocation)
        ):
            parked.setdefault(invocation, []).append(event)
            return

        task = self._claim_tool_stage_delivery(event, embed_msg_ids, records)
        if task is None:
            return

        record = records[str(event.get("invocation_id") or "")]
        try:
            outcome = await self._harvest_tool_stage_delivery(task, record)
        except asyncio.CancelledError:
            self._handle_stage_delivery_cancel(task, record)
            raise

        if (
            outcome == self._STAGE_OUTCOME_FAILED
            and event.get("terminal")
            and record["state"] == self._STAGE_STATE_FAILED
            and record["attempts"] == 1
        ):
            retry_task = self._claim_tool_stage_delivery(
                event, embed_msg_ids, records, retry=True
            )
            if retry_task is None:
                return
            try:
                await self._harvest_tool_stage_delivery(retry_task, record)
            except asyncio.CancelledError:
                self._handle_stage_delivery_cancel(retry_task, record)
                raise

    async def _stop_drain_remaining_stage_embeds(
        self,
        embed_msg_ids: Dict[str, str],
        records: Dict[str, dict],
        parked: Optional[Dict[str, list]] = None,
    ) -> None:
        """Normal stop path: deliver terminal/last queued events via claim+harvest."""
        if parked is None:
            parked = {}
        deadline = time.monotonic() + self._STAGE_STOP_DRAIN_TIMEOUT
        while True:
            if time.monotonic() >= deadline:
                return
            await self._drain_parked_tool_stage_events(
                embed_msg_ids, records, parked
            )
            for event in self._collect_final_stage_events():
                try:
                    await self._process_tool_stage_event(
                        event, embed_msg_ids, records, parked
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            if not parked:
                break
            if any(
                self._invocation_has_live_unknown_delivery(records, inv)
                for inv in list(parked.keys())
            ):
                if time.monotonic() >= deadline:
                    return
                await asyncio.sleep(0.2)
                continue

    async def send_tool_stage_embeds(self):
        ctx = self._ctx
        if not ctx.stage_event_queue:
            return
        if ctx._stage_embed_adapter is None:
            return

        # invocation_id -> embed message id. Concurrent invocations of the
        # same tool each own exactly one message, so their updates can
        # never cross-edit each other's embed.
        embed_msg_ids: Dict[str, str] = {}
        records: Dict[str, dict] = {}
        parked: Dict[str, list] = {}

        try:
            while True:
                if not ctx._run_still_current():
                    await self._stop_drain_remaining_stage_embeds(
                        embed_msg_ids, records, parked
                    )
                    return

                await self._drain_parked_tool_stage_events(
                    embed_msg_ids, records, parked
                )

                try:
                    event = ctx.stage_event_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.2)
                    continue

                await self._process_tool_stage_event(
                    event, embed_msg_ids, records, parked
                )
        except asyncio.CancelledError:
            raise

    async def _deliver_agent_status_marker(self, marker: tuple) -> Optional[str]:
        """Send one queued branded agent-viewer status line as its own message.

        The consumer side of the ``_status_callback_sync`` reorder: the exact
        line the direct rail would have sent goes through the same
        ``_send_or_update_status_coro`` door with the same status wiring
        (adapter, chat id, thread metadata, cleanup tracking), so platform
        rendering — Discord's branded embed conversion, Telegram's
        status-bubble editing — is byte-identical. Only the scheduling
        changed: queue-ordered instead of independently scheduled. Returns
        the sent message id on success (``None`` otherwise) so correlated
        consumers can record the exact message for later in-place edits.
        Never raises on transport failure, so a dead platform call cannot
        take the progress consumer down with it (and lose every later tool
        row).
        """
        # Late seam (as in #569): these helpers live in the gateway.run facade.
        from gateway.run import (
            _is_agent_status_queue_marker,
            _retain_undelivered_status_notice,
            _send_or_update_status_coro,
            _status_failure_is_transient,
        )
        ctx = self._ctx
        if not _is_agent_status_queue_marker(marker):
            return None
        # No adapter re-resolution: _status_callback_sync only enqueues a
        # marker once ctx._status_adapter is set, and the direct rail sends
        # through that same adapter — the marker rail must not invent one.
        adapter = ctx._status_adapter
        if adapter is None:
            return None
        _kind, event_type, content = marker[:3]
        try:
            result = await ctx.deliver_visible(lambda: _send_or_update_status_coro(
                adapter,
                ctx._status_chat_id,
                event_type,
                content,
                ctx._status_thread_metadata))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "agent-viewer status marker delivery failed", exc_info=True
            )
            return None
        if (
            ctx._cleanup_progress
            and getattr(result, "success", False)
            and getattr(result, "message_id", None)
        ):
            ctx._cleanup_msg_ids.append(str(result.message_id))
        # Same transient-failure retention as the direct status rail: the
        # marker carries the exact line the direct rail would have sent, so
        # it gets the same durable second chance after reconnect.
        if (
            event_type != "retry_progress"
            and _status_failure_is_transient(result)
        ):
            await _retain_undelivered_status_notice(
                adapter=adapter,
                chat_id=ctx._status_chat_id,
                thread_metadata=ctx._status_thread_metadata,
                session_key=str(getattr(ctx, "session_key", "") or ""),
                status_key=event_type,
                content=content,
            )
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            return str(result.message_id)
        return None
