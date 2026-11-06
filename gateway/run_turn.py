"""Agent-turn execution for GatewayRunner: _handle_message_with_agent, _run_agent*, proxy path,
background tasks, MCP reload. Bound onto ``GatewayRunner`` via the MRO; ``gateway.run`` internals
are imported lazily inside method bodies (import cycle) so ``patch("gateway.run.X")`` still works.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import asyncio
import dataclasses
import inspect
import json
import os
import queue
import threading
import time
from agent.i18n import t
from agent.session_activity import format_iteration_progress
from agent.turn_failure_copy import FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE
from contextlib import nullcontext, suppress
from contextvars import copy_context
from gateway.config import Platform
from gateway.media_repair import repair_explicit_computer_use_media_paths
from gateway.platforms.base import BasePlatformAdapter, ProcessingOutcome
from gateway.platforms.event import MessageEvent
from gateway.response_filters import display_kind_for_event, is_machinery_display_kind
from gateway.warning_notifications import diagnostic_metadata, diagnostic_turn_muted, diagnostic_wake_muted
from gateway.session import (
    SessionSource, _session_key_namespace, build_channel_continuity_note,
    build_session_context,
)
from gateway.session_transcript import TranscriptReadError
from gateway.turn_context import TurnContext
from gateway.turn_lease import DEFAULT_LEASE_WAIT, TurnLeaseTimeoutError
from hermes_constants import get_hermes_home_override
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from utils import base_url_hostname

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

_tool_call_logger_lock = threading.Lock()


def _tool_call_logger() -> logging.Logger:
    """Process-wide ``hermes.tool_calls`` Logger + one RotatingFileHandler on logs/tool_calls.log.
    Named Loggers live in ``logging.Logger.manager.loggerDict`` forever, so the former per-turn name
    (``hermes.tool_calls.<id(log_queue)>``) leaked one Logger per logged turn (#62950); a single
    shared handler also keeps concurrent turns from double-writing lines."""
    tool_logger = logging.getLogger("hermes.tool_calls")
    with _tool_call_logger_lock:
        if not tool_logger.handlers:
            from logging.handlers import RotatingFileHandler
            from agent.redact import RedactingFormatter
            from gateway.run import _hermes_home

            log_dir = _hermes_home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_dir / "tool_calls.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            handler.setFormatter(RedactingFormatter("%(message)s"))
            tool_logger.setLevel(logging.INFO)
            tool_logger.propagate = False
            tool_logger.addHandler(handler)
    return tool_logger



_CONTEXT_OVERFLOW_ERROR_PHRASES = (
    "context length", "context size", "context window",
    "maximum context", "token limit", "too many tokens",
    "reduce the length", "exceeds the limit",
    "request entity too large", "prompt is too long",
    "payload too large", "input is too long",
)

_UNEXPECTED_SILENCE_REPLY = (
    "⚠️ The model returned only a silence marker for a message that needed a reply. "
    "Try again or rephrase."
)


def _bg_prompt_preview(prompt: str, limit: int = 60) -> str:
    """Short single-line quote of a /bg prompt for its failure notice (the task id means nothing to the user)."""
    text = " ".join(str(prompt or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_context_overflow_failure_result(agent_result: dict, history_len: int) -> bool:
    """One verdict for "this failed turn is a context overflow", shared by transcript persistence
    (#1630 skip) and the user-facing reply so the two can never disagree.

    Multi-word phrases (not bare "exceed"/"token") avoid matching "rate limit exceeded" or
    "invalid authentication token"; a bare 400 only counts on a long session."""
    if not agent_result.get("failed"):
        return False
    if agent_result.get("compression_exhausted"):
        return True
    err = str(agent_result.get("error") or "").lower()
    return any(p in err for p in _CONTEXT_OVERFLOW_ERROR_PHRASES) or ("400" in err and history_len > 50)


# Setup/prefix rows rather than conversation: the agent rebuilds its own system prompt, and a
# transcript meta row is logging-only — neither reaches the model, but both are the head a
# fail-closed payload keeps.
_HYGIENE_SETUP_ROLES = ("system", "session_meta")


def bound_model_input_without_hygiene(history: List[Any], limit: int) -> List[Any]:
    """Fail-closed in-context bound for a turn where hygiene has not landed (#111988).

    Keeps the leading ``system``/``session_meta`` setup rows plus the newest tail, total <= ``limit``.
    Deterministic (the same transcript always yields the same cut) and payload-only: the stored
    transcript is never touched, so the agent's durable-prefix slice (``history_offset``) is
    unaffected. Returns ``history`` unchanged — same object — when nothing needs dropping, so the
    landed-compression and below-the-limit paths stay byte-identical.
    """
    if len(history) <= limit:
        return history
    head_end = 0
    while (head_end < len(history) and isinstance(history[head_end], dict)
           and history[head_end].get("role") in _HYGIENE_SETUP_ROLES):
        head_end += 1
    # Always leave room for the newest row: a setup-only payload would answer nothing.
    head_end = min(head_end, limit - 1)
    tail_start = len(history) - (limit - head_end)
    # Never start the kept tail on a tool result: its parent assistant(tool_calls) row is dropped
    # with it, and an orphaned tool result is an invalid sequence for every provider.
    while (tail_start < len(history) and isinstance(history[tail_start], dict)
           and history[tail_start].get("role") == "tool"):
        tail_start += 1
    return history[:head_end] + history[tail_start:]


def hygiene_no_commit_reason(agent) -> str:
    """Name WHY a hygiene compression left the session id unchanged with no in-place commit.
    The terminal ``else`` used to blame "no session_db on the hygiene agent" for every route into it,
    but that is one of several causes (#71097): an attempt that ABORTED before any commit boundary
    (lock skip, transient cooldown, summary timeout, codex thread interrupted) leaves
    ``_last_compression_attempt_in_place`` at ``None``; a DB-less agent is only the case when
    ``_session_db`` really is missing. Read the per-attempt signals the compressor sets, in that order."""
    if not bool(getattr(agent, "_last_compression_attempt_recorded", False)):
        return "compression did not run"
    lock_skip = getattr(agent, "_compression_skipped_due_to_lock", None)
    if lock_skip is True or isinstance(lock_skip, str):
        return "attempt skipped: compression lease held by another process"
    blocked = getattr(agent, "_compression_blocked_transient", None)
    if blocked:
        return f"attempt blocked: {blocked}"
    if getattr(agent, "_last_compression_attempt_in_place", None) is None:
        detail = "summary timed out" if getattr(agent, "_last_compression_timed_out", False) else "aborted before commit"
        warning = getattr(agent, "_last_compression_summary_warning", None)
        return f"attempt {detail}" + (f": {warning}" if warning else "")
    if getattr(agent, "_session_db", None) is None:
        return "no session_db on the hygiene agent"
    return "in-place commit did not complete"


class GatewayTurnMixin:
    """Agent-turn execution for GatewayRunner (see module docstring)."""

    def _resolve_session_agent_runtime(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Resolve model/runtime for a session.

        Priority (highest first): session ``/model`` → ``channel_overrides`` →
        global config/env (``_resolve_gateway_model(user_config)`` and default
        provider resolution).
        """
        from gateway.run import (
            _credential_pool_for_provider,
            _get_channel_override,
            _resolve_gateway_model,
            _resolve_runtime_agent_kwargs,
            _resolve_runtime_agent_kwargs_for_provider,
            logger,
        )

        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        # Every exit path starts clean: the /model-override fast path returns before the pop below,
        # and hygiene/inbound callers resolve without a turn runner consuming the stash — a stale
        # notice must never attach to another session's next turn (#74349).
        self._pre_agent_fallback_notice = None

        model = _resolve_gateway_model(user_config)
        if resolved_session_key:
            self._rehydrate_session_model_override(resolved_session_key)
        _override_state = (
            self._peek_session_state(resolved_session_key)
            if resolved_session_key
            else None
        )
        override = (
            _override_state.conversation.model_override if _override_state else None
        )
        if override:
            override_model = override.get("model", model)
            override_runtime = {
                "provider": override.get("provider"),
                "requested_provider": override.get("requested_provider"),
                "api_key": override.get("api_key"),
                "base_url": override.get("base_url"),
                "api_mode": override.get("api_mode"),
                "max_tokens": override.get("max_tokens"),
                "credential_pool": override.get("credential_pool"),
                "request_overrides": override.get("request_overrides"),
                "capabilities": dict(override.get("capabilities") or {}),
            }
            if override_runtime.get("api_key"):
                if override_runtime.get("credential_pool") is None:
                    override_runtime["credential_pool"] = _credential_pool_for_provider(
                        override.get("provider")
                    )
                logger.debug(
                    "Session model override (fast): session=%s config_model=%s -> override_model=%s provider=%s",
                    resolved_session_key or "", model, override_model,
                    override_runtime.get("provider"),
                )
                return override_model, override_runtime
            # Override exists but has no api_key — fall through to env-based
            # resolution and apply model/provider from the override on top.
            logger.debug(
                "Session model override (no api_key, fallback): session=%s config_model=%s override_model=%s",
                resolved_session_key or "", model, override_model,
            )
        else:
            logger.debug(
                "No session model override: session=%s config_model=%s override_keys=%s",
                resolved_session_key or "", model,
                [
                    _key
                    for _key, _st in list(self._sessions_map().items())
                    if _st.conversation.model_override is not None
                ][:5] or "[]",
            )

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        # Private notice metadata must never reach an ``AIAgent(**runtime_kwargs)`` spread; the turn
        # runner surfaces it through the agent's one-shot fallback notice (#74349).
        self._pre_agent_fallback_notice = runtime_kwargs.pop("_fallback_notice", None)
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            logger.info(
                "Runtime provider supplied explicit model override: %s -> %s",
                model,
                runtime_model,
            )
            model = runtime_model

        cfg = getattr(self, "config", None)
        if cfg and source is not None:
            chat_id = str(source.chat_id) if source.chat_id else ""
            thread_id = (
                str(source.thread_id) if getattr(source, "thread_id", None) else None
            )
            parent_id = (
                str(source.parent_chat_id)
                if getattr(source, "parent_chat_id", None)
                else None
            )
            ch = _get_channel_override(
                cfg,
                source.platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
            if ch:
                if ch.model:
                    model = ch.model
                if ch.provider:
                    runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                        ch.provider
                    )
                    ch_runtime_model = runtime_kwargs.pop("model", None)
                    # Only adopt the provider's bundled model when the override
                    # did not specify an explicit model.
                    if ch_runtime_model and not ch.model:
                        model = ch_runtime_model

        if override and resolved_session_key:
            model, runtime_kwargs = self._apply_session_model_override(
                resolved_session_key, model, runtime_kwargs
            )

        # When the config has no model.default but a provider was resolved
        # (e.g. user ran `hermes auth add openai-codex` without `hermes model`),
        # fall back to the provider's first catalog model so the API call
        # doesn't fail with "model must be a non-empty string".
        if not model and runtime_kwargs.get("provider"):
            try:
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        model, runtime_kwargs["provider"],
                    )
            except Exception:
                pass

        # Final safety net (#35314): if resolution still produced an empty
        # model — e.g. a transient config-cache miss during a post-interrupt
        # recovery turn returned an empty user_config — reuse the last model we
        # successfully resolved for this session (or, failing that, the most
        # recent one resolved process-wide). Building an agent with model=""
        # makes every API call fail HTTP 400 "No models provided" and the
        # session goes silent until the user manually re-sends. ``getattr``
        # guards against bare test runners built via ``object.__new__``.
        if not model:
            _lr_state = (
                self._peek_session_state(resolved_session_key)
                if resolved_session_key
                else None
            )
            _lr_star = self._peek_session_state("*")
            _recovered = (
                (_lr_state.conversation.last_resolved_model if _lr_state else "")
                or (_lr_star.conversation.last_resolved_model if _lr_star else "")
            )
            if _recovered:
                logger.warning(
                    "Empty model resolved for session=%s — recovering "
                    "last-known-good model %s (config read likely returned "
                    "empty; see #35314)",
                    resolved_session_key or "", _recovered,
                )
                model = _recovered
        elif model:
            # Cache the good resolution for future recovery turns.
            if resolved_session_key:
                self._session_state(
                    resolved_session_key
                ).conversation.last_resolved_model = model
            self._session_state("*").conversation.last_resolved_model = model

        return model, runtime_kwargs

    def _resolve_turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict) -> dict:
        """Build the effective model/runtime config for a single turn.

        Always uses the session's primary model/provider.  If `/fast` is
        enabled and the model supports Priority Processing / Anthropic fast
        mode, attach `request_overrides` so the API call is marked
        accordingly.

        Per-provider ``request_overrides`` resolved by
        ``resolve_runtime_provider`` (e.g. a ``custom_providers`` ``extra_body``
        carrying ``chat_template_kwargs``) are preserved here and merged *under*
        the fast-mode overrides, so a provider's configured request body still
        reaches the model on the gateway turn path.
        """
        from gateway.run import _deep_merge_request_overrides

        from hermes_cli.models import resolve_fast_mode_overrides

        runtime = {
            "api_key": runtime_kwargs.get("api_key"),
            "base_url": runtime_kwargs.get("base_url"),
            "provider": runtime_kwargs.get("provider"),
            "requested_provider": runtime_kwargs.get("requested_provider"),
            "api_mode": runtime_kwargs.get("api_mode"),
            "command": runtime_kwargs.get("command"),
            "args": list(runtime_kwargs.get("args") or []),
            "credential_pool": runtime_kwargs.get("credential_pool"),
            "max_tokens": runtime_kwargs.get("max_tokens"),
            "capabilities": dict(runtime_kwargs.get("capabilities") or {}),
        }
        base_request_overrides = dict(runtime_kwargs.get("request_overrides") or {})
        route = {
            "model": model,
            "runtime": runtime,
            "signature": (
                model,
                runtime["provider"],
                runtime["requested_provider"],
                runtime["base_url"],
                runtime["api_mode"],
                runtime["command"],
                tuple(runtime["args"]),
            ),
        }

        # Provider-level request_overrides (e.g. a custom_providers extra_body)
        # resolved upstream by resolve_runtime_provider().  These were being
        # dropped by the runtime whitelist above, so a custom provider's
        # configured extra_body (chat_template_kwargs, etc.) never reached the
        # model on the gateway path -- only /fast service-tier overrides did.
        service_tier = getattr(self, "_service_tier", None)
        if service_tier != "priority":
            # None (normal) or auto/cold — the bounded window is applied per
            # request by agent.fast_mode, not pinned into request_overrides.
            route["request_overrides"] = base_request_overrides
            return route

        try:
            overrides = resolve_fast_mode_overrides(
                route["model"],
                provider=runtime["provider"],
                base_url=runtime["base_url"],
            )
        except Exception:
            overrides = None
        # Fast-mode overrides (service_tier / speed) are top-level keys and do
        # not collide with extra_body; deep-merge them over the provider overrides.
        route["request_overrides"] = _deep_merge_request_overrides(
            base_request_overrides,
            overrides or {},
        )
        return route

    def _sync_session_model_from_agent(self, session_id: str, agent: Any) -> None:
        """Persist the runtime model/provider actually used by a gateway turn.

        Provider fallback can switch ``agent.model``/``agent.provider`` after the
        session row was created. Keep the session DB metadata in sync so session
        lists, desktop/dashboard details, and follow-up session tooling report the
        backend that actually answered the latest turn.

        Called from the ``run_sync`` closure, which executes off the event loop
        in the executor thread — so the synchronous ``SessionDB`` (``_db``) is
        used directly rather than awaiting the AsyncSessionDB forwarder.
        """
        from gateway.run import logger

        if not session_id or agent is None or self._session_db is None:
            return
        model = getattr(agent, "model", None)
        if not model:
            return
        runtime = {
            "provider": getattr(agent, "provider", None),
            "base_url": getattr(agent, "base_url", None),
            "api_mode": getattr(agent, "api_mode", None),
            "fallback_active": bool(getattr(agent, "_fallback_activated", False)),
        }
        runtime = {k: v for k, v in runtime.items() if v not in (None, "")}

        try:
            db = self._session_db._db
            row = db.get_session(session_id)
            if not row:
                return
            current_model = row.get("model")
            raw_config = row.get("model_config")
            try:
                config = json.loads(raw_config) if raw_config else {}
            except Exception:
                config = {}
            if not isinstance(config, dict):
                config = {}
            gateway_runtime = dict(config.get("gateway_runtime") or {})
            if current_model == model and all(
                gateway_runtime.get(k) == v for k, v in runtime.items()
            ):
                return
            config["gateway_runtime"] = runtime
            db.update_session_meta(session_id, json.dumps(config), model=model)
        except Exception:
            logger.debug("Failed to sync gateway session model metadata", exc_info=True)

    def _event_thread_metadata(self, event, source):
        """Thread metadata for a send that replies to ``event`` on ``source``."""
        return self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

    @staticmethod
    def _pop_post_delivery_callback(adapter, key, generation):
        """Pop the adapter's deferred post-delivery callback for ``key`` (legacy dict fallback)."""
        if getattr(type(adapter), "pop_post_delivery_callback", None) is not None:
            return adapter.pop_post_delivery_callback(key, generation=generation)
        if adapter and hasattr(adapter, "_post_delivery_callbacks"):
            return adapter._post_delivery_callbacks.pop(key, None)
        return None

    @staticmethod
    def _is_intentional_silence(agent_result, response) -> bool:
        try:
            from gateway.response_filters import is_intentional_silence_agent_result
            return is_intentional_silence_agent_result(agent_result, response)
        except Exception:
            return False

    async def _hmwa_resolve_session(self, event, source):
        """Resolve ``source`` to its session entry (topic recovery, internal-route guards, Telegram
        topic-binding heal). Returns ``(source, session_entry, session_key)`` or ``None`` to drop
        the event."""
        from gateway.run import logger

        # Topic-mode DMs: rewrite a stale/foreign thread_id to the user's last-active topic so a
        # cross-topic Reply doesn't fragment the conversation.
        event_metadata = getattr(event, "metadata", None) or {}
        expected_session_key = str(event_metadata.get("gateway_session_key") or "").strip()
        recovered = (await asyncio.to_thread(self._recover_telegram_topic_thread_id, source)
                     if not expected_session_key else None)
        if recovered is not None:
            logger.info(
                "telegram topic recovery: chat=%s user=%s %r -> %s",
                source.chat_id, source.user_id, source.thread_id, recovered,
            )
            source = dataclasses.replace(source, thread_id=recovered)
            with suppress(Exception):
                event.source = source

        if expected_session_key:
            derived_session_key = self._session_key_for_source(source)
            if derived_session_key != expected_session_key:
                logger.warning(
                    "Dropping internally routed event after route recovery: expected session=%s derived=%s",
                    expected_session_key, derived_session_key,
                )
                return

        strict_session = bool(event_metadata.get("gateway_session_strict"))
        pinned_session_id = str(event_metadata.get("gateway_session_id") or "").strip()
        if strict_session:
            session_entry = await self.async_session_store.lookup_by_session_key(expected_session_key)
            if session_entry is None or not pinned_session_id or session_entry.session_id != pinned_session_id:
                logger.warning(
                    "Dropping internally routed event: expected session id=%s is no longer current for key=%s",
                    pinned_session_id or "missing", expected_session_key or "missing",
                )
                return
        else:
            # Internal wakes observe reset policy without counting as user activity, or periodic
            # notifications keep the routing key alive across every daily/idle boundary.
            session_entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=not bool(getattr(event, "internal", False)),
            )
        session_key = session_entry.session_key
        if not strict_session and pinned_session_id:
            resolved_entry = await self._resolve_async_delegation_session(session_entry, pinned_session_id)
            if resolved_entry is None:
                return
            session_entry = resolved_entry
        self._cache_session_source(session_key, source)
        if await asyncio.to_thread(self._is_telegram_topic_lane, source):
            session_entry = await self._hmwa_heal_telegram_topic_binding(source, session_entry, session_key)
        from gateway.run_heartbeat_acceptance import resolve_heartbeat_owner
        if not await resolve_heartbeat_owner(self, event, session_entry):
            return
        return source, session_entry, session_key

    async def _hmwa_heal_telegram_topic_binding(self, source, session_entry, session_key):
        """Follow the (chat_id, thread_id) topic binding — healed to its compression tip — or record
        a fresh one. Returns the (possibly switched) session entry."""
        from gateway.run import logger

        binding = None
        try:
            if self._session_db:
                binding = await self._session_db.get_telegram_topic_binding(
                    chat_id=str(source.chat_id), thread_id=str(source.thread_id),
                    profile_name=self._telegram_topic_profile_name(source),
                )
        except Exception:
            logger.debug("Failed to read Telegram topic binding", exc_info=True)
        if not binding:
            try:
                await asyncio.to_thread(self._record_telegram_topic_binding, source, session_entry)
            except Exception:
                logger.debug("Failed to record Telegram topic binding", exc_info=True)
            return session_entry
        stored_session_id = str(binding.get("session_id") or "")
        bound_session_id = stored_session_id
        # A binding pointing at a pre-compression parent is walked forward to the tip so the next
        # message resumes the compressed child instead of reloading the oversized parent.
        # Returns the input unchanged when the session isn't a compression parent, so this is cheap and
        # safe. See #20470, #29712, #33414.
        if bound_session_id and self._session_db is not None:
            try:
                canonical_session_id = await self._session_db.get_compression_tip(bound_session_id)
            except Exception:
                logger.debug("compression-tip lookup failed for %s", bound_session_id, exc_info=True)
                canonical_session_id = bound_session_id
            if canonical_session_id and canonical_session_id != bound_session_id:
                bound_session_id = canonical_session_id
        if bound_session_id and bound_session_id != session_entry.session_id:
            # Route through SessionStore so the key → id mapping persists and the previous lane
            # session ends cleanly (in-place mutation split-brained the JSON index). The two DB
            # awaits above are a window for /new or /resume to move the route; the CAS on the
            # snapshot id lets that win instead of being clobbered by a stale binding.
            switched = await self.async_session_store.switch_session(
                session_key, bound_session_id, expected_session_id=session_entry.session_id,
            )
            if switched is not None:
                session_entry = switched
        if bound_session_id and bound_session_id != stored_session_id:
            # The stored binding pointed at a parent: rewrite it to the canonical descendant.
            await asyncio.to_thread(
                self._sync_telegram_topic_binding, source, session_entry, reason="compression-tip-walk",
            )
        return session_entry

    async def _hmwa_open_session(self, session_entry, session_key, source):
        """Consume auto-reset / fresh-reset flags and emit ``session:start`` for new sessions.
        Returns ``(_was_auto_reset, _is_new_session)``."""
        # Consume was_auto_reset immediately so it cannot re-fire and wipe overrides set between turns.
        # Capture and immediately consume was_auto_reset so it does not re-fire on subsequent messages —
        # preventing the cleanup from wiping model/reasoning overrides set between turns (Closes #48031).
        _was_auto_reset = getattr(session_entry, "was_auto_reset", False)
        if _was_auto_reset:
            # Conversation boundary: the funnel clears every conversation-scoped dict; evict the cached
            # agent so context_compressor._previous_summary cannot leak into new summaries.
            # Treat auto-reset as a full conversation boundary — clear every conversation-scoped per-session
            # dict in one funnel call so the fresh session does not inherit the previous conversation's
            # model/reasoning overrides, a queued "/model switched" note, or a stale resolved-model cache
            # (#48031, #58403). See _CONVERSATION_SCOPED_STATE.
            self._clear_conversation_scope(session_key, reason="auto_reset")
            self._evict_cached_agent(session_key)
            session_entry.was_auto_reset = False

        _is_fresh_reset = getattr(session_entry, "is_fresh_reset", False)
        _is_new_session = session_entry.created_at == session_entry.updated_at or _was_auto_reset or _is_fresh_reset
        # Consume is_fresh_reset so it doesn't leak onto later messages in the same session.
        if _is_fresh_reset:
            # See #6508.
            session_entry.is_fresh_reset = False
        if _is_new_session:
            await self.hooks.emit("session:start", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "session_key": session_key,
            })
        return _was_auto_reset, _is_new_session

    async def _hmwa_deliver_auto_reset_notice(self, session_entry, source, turn_sidecar_notes):
        """Stage the auto-reset sidecar note for the agent and notify the user (policy-gated)."""
        from gateway.run import logger

        from gateway.run import _AUTO_RESET_CONTEXT_NOTES
        reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'suspended'
        context_note = _AUTO_RESET_CONTEXT_NOTES.get(reset_reason, _AUTO_RESET_CONTEXT_NOTES["suspended"])
        # Long-lived channels: point the agent at the prior same-channel session for session_search.
        try:
            # Returns None (appends nothing) for other platforms or when there's no prior activity to
            # recall. Deterministic — no extra API/DB calls (#36220).
            continuity_note = build_channel_continuity_note(session_entry, source)
        except Exception:
            continuity_note = None
        if continuity_note:
            context_note = context_note + "\n\n" + continuity_note
        turn_sidecar_notes.append(context_note)

        try:
            should_notify = reset_reason == "suspended"
            adapter = self._delivery_adapter_for(source) if should_notify else None
            if adapter:
                notice = (
                    "◐ Session reset after being stopped. "
                    f"Conversation history cleared.\n"
                    f"Use /resume to browse and restore a previous session.\n"
                )
                with suppress(Exception):
                    session_info = await asyncio.to_thread(self._reset_notice_session_info, source)
                    if session_info:
                        notice = f"{notice}\n\n{session_info}"
                await adapter.send(source.chat_id, notice, metadata=self._thread_metadata_for_source(source))
        except Exception as e:
            logger.debug("Auto-reset notification failed (non-fatal): %s", e)

        # was_auto_reset was consumed in _hmwa_open_session; only the reason needs clearing.
        session_entry.auto_reset_reason = None

    def _hmwa_auto_load_skills(self, event, _auto, _quick_key, session_key):
        """Prepend topic/channel-bound skill payload(s) to ``event.text`` on a new session."""
        from gateway.run import logger

        _skill_names = [_auto] if isinstance(_auto, str) else list(_auto)
        try:
            from agent.skill_commands import _load_skill_payload, _build_skill_message
            _combined_parts: list[str] = []
            _loaded_names: list[str] = []
            for _sname in _skill_names:
                _loaded = _load_skill_payload(_sname, task_id=_quick_key)
                if not _loaded:
                    logger.warning("[Gateway] Auto-skill '%s' not found", _sname)
                    continue
                _loaded_skill, _skill_dir, _display_name = _loaded
                _part = _build_skill_message(
                    _loaded_skill, _skill_dir,
                    f'[IMPORTANT: The "{_display_name}" skill is auto-loaded. '
                    f"Follow its instructions for this session.]",
                )
                if _part:
                    _combined_parts.append(_part)
                    _loaded_names.append(_sname)
            if _combined_parts:
                _combined_parts.append(event.text)  # user's original text after the payloads
                event.text = "\n\n".join(_combined_parts)
                logger.info("[Gateway] Auto-loaded skill(s) %s for session %s", _loaded_names, session_key)
        except Exception as e:
            logger.warning("[Gateway] Failed to auto-load skill(s) %s: %s", _skill_names, e)

    async def _hmwa_acquire_turn_lease(self, _quick_key, run_generation, session_entry, _session_env_tokens):
        """Serialize [load history → run → flush] per resolved SESSION_ID so another routing key on
        the same session waits for the prior flush. Fail-closed on timeout (outer dispatch returns
        a resend notice). Released in _handle_message's finally, granted per (routing key, run
        generation) so a stale unwind can't release a newer turn's."""
        from gateway.run import _float_env
        _lease_registry = getattr(self, "_turn_leases", None)
        if _lease_registry is None:
            return
        try:
            _lease_token = await _lease_registry.acquire(
                session_entry.session_id, owner_key=_quick_key, generation=run_generation,
                timeout=_float_env("HERMES_TURN_LEASE_TIMEOUT", DEFAULT_LEASE_WAIT),
            )
        except TurnLeaseTimeoutError:
            # The cleanup finally starts later; restore the tokens here or this exit leaks identity.
            self._clear_session_env(_session_env_tokens)
            raise
        if _lease_token is not None:
            self._session_state(_quick_key).turn.lease_tokens[run_generation] = _lease_token

    @dataclasses.dataclass
    class _HygienePlan:
        """Hygiene pre-check outcome for one turn."""

        needs_compress: bool
        approx_tokens: int
        msg_count: int
        warn_token_threshold: int

    @staticmethod
    def _hmwa_hygiene_read_config(hs, data):
        """Apply model / compression knobs from the gateway config onto ``hs`` (invalid values keep the defaults)."""
        # Resolve model name (same logic as run_sync)
        _model_cfg = data.get("model", {})
        if isinstance(_model_cfg, str):
            hs.model = _model_cfg
        elif isinstance(_model_cfg, dict):
            hs.model = _model_cfg.get("default") or _model_cfg.get("model") or hs.model
            _raw_ctx = _model_cfg.get("context_length")
            if _raw_ctx is not None:
                with suppress(TypeError, ValueError):
                    hs.config_context_length = int(_raw_ctx)
            hs.provider = _model_cfg.get("provider") or None
            hs.base_url = _model_cfg.get("base_url") or None

        # Only the enabled flag is shared with the agent's compression config (hygiene runs higher).
        _comp_cfg = data.get("compression", {})
        if not isinstance(_comp_cfg, dict):
            return
        hs.compression_enabled = str(_comp_cfg.get("enabled", True)).lower() in {"true", "1", "yes"}

        def _knob(key, current, cast, allow_zero=False):
            raw = _comp_cfg.get(key)
            if raw is None:
                return current
            try:
                parsed = cast(raw)
            except (TypeError, ValueError):
                return current
            return parsed if (parsed >= 0 if allow_zero else parsed > 0) else current

        hs.hard_msg_limit = _knob("hygiene_hard_message_limit", hs.hard_msg_limit, int)
        hs.timeout_seconds = _knob("hygiene_timeout_seconds", hs.timeout_seconds, float)
        hs.total_ceiling_seconds = _knob("hygiene_total_ceiling_seconds", hs.total_ceiling_seconds, float)
        # The ceiling can never be tighter than one idle window, or the extension loop would be dead code.
        hs.total_ceiling_seconds = max(hs.total_ceiling_seconds, hs.timeout_seconds)
        hs.max_turn_hold_seconds = _knob("hygiene_max_turn_hold_seconds", hs.max_turn_hold_seconds, float)
        hs.failure_cooldown_seconds = _knob(
            "hygiene_failure_cooldown_seconds", hs.failure_cooldown_seconds, float, allow_zero=True,
        )

    async def _hmwa_hygiene_settings(self, source, session_key):
        """Resolve model/provider/context-length + hygiene knobs (fail-soft: errors keep defaults).

        The 0.85 threshold is deliberately HIGHER than the agent's compressor (0.50): a safety net
        for sessions that grew between turns. ``max_turn_hold_seconds`` bounds the TURN wait
        (compressor keeps running detached, commit fenced); kept below transport idle-timeouts."""
        from gateway.run import _load_gateway_config
        hs = self._HygieneSettings(
            model="anthropic/claude-sonnet-4.6", threshold_pct=0.85, compression_enabled=True,
            hard_msg_limit=5000, timeout_seconds=30.0, total_ceiling_seconds=600.0,
            max_turn_hold_seconds=10.0, failure_cooldown_seconds=300.0, config_context_length=None,
            provider=None, base_url=None, api_key=None, data={},
        )
        try:
            hs.data = _load_gateway_config()
            if hs.data:
                self._hmwa_hygiene_read_config(hs, hs.data)
            configured_model, configured_provider, configured_base_url = hs.model, hs.provider, hs.base_url

            with suppress(Exception):
                hs.model, _hyg_runtime = self._resolve_session_agent_runtime(
                    source=source, session_key=session_key,
                    user_config=hs.data if isinstance(hs.data, dict) else None,
                )
                hs.provider = _hyg_runtime.get("provider") or hs.provider
                hs.base_url = _hyg_runtime.get("base_url") or hs.base_url
                hs.api_key = _hyg_runtime.get("api_key") or hs.api_key

            if hs.config_context_length is not None:
                try:
                    from hermes_cli.route_identity import should_clear_context_pin_async

                    if await should_clear_context_pin_async(
                        configured_model, hs.model, configured_base_url, hs.base_url,
                        configured_provider, hs.provider,
                    ):
                        hs.config_context_length = None
                except Exception:
                    hs.config_context_length = None

            # custom_providers per-model context_length fallback (as in run_agent.py); needs base_url.
            if hs.config_context_length is None and hs.base_url:
                with suppress(TypeError, ValueError):
                    try:
                        from hermes_cli.config import (
                            get_compatible_custom_providers as _gw_gcp,
                            get_custom_provider_context_length as _gw_gccl,
                        )
                        _hyg_custom_providers = _gw_gcp(hs.data)
                    except Exception:
                        _hyg_custom_providers = hs.data.get("custom_providers")
                        if not isinstance(_hyg_custom_providers, list):
                            _hyg_custom_providers = []
                    _hyg_custom_ctx = _gw_gccl(
                        model=hs.model, base_url=hs.base_url, custom_providers=_hyg_custom_providers,
                    )
                    if _hyg_custom_ctx:
                        hs.config_context_length = int(_hyg_custom_ctx)
        except Exception:
            pass
        return hs

    async def _hmwa_hygiene_plan(self, hs, history, session_entry, session_key):
        """Decide whether hygiene compression fires this turn (token/message thresholds, DB-backed
        failure cooldown, in-flight compression)."""
        from gateway.run import logger

        from agent.model_metadata import estimate_messages_tokens_rough, get_model_context_length_async
        _hyg_context_length = await get_model_context_length_async(
            hs.model, base_url=hs.base_url or "", api_key=hs.api_key or "",
            config_context_length=hs.config_context_length, provider=hs.provider or "",
        )
        _compress_token_threshold = int(_hyg_context_length * hs.threshold_pct)
        _warn_token_threshold = int(_hyg_context_length * 0.95)
        _msg_count = len(history)

        # Real usage decides: the API-reported prompt count, else the anchor persisted on the session
        # row (real count + delta of what was appended since, survives gateway restarts), else the
        # rough estimate (runs 30-50% high, which only fires hygiene early — safe). Do NOT compensate
        # with a threshold multiplier.
        from agent.image_token_cost import image_cost_context, learned_image_token_cost
        _anchored = None
        # Images in any local delta/estimate are priced at the cost learned from this model's usage.
        with image_cost_context(learned_image_token_cost(hs.model, hs.base_url)):
            if session_entry.last_prompt_tokens <= 0:
                from agent.usage_anchor import persisted_anchor_tokens
                _session_db = getattr(self, "_session_db", None)
                _anchored = persisted_anchor_tokens(
                    getattr(_session_db, "_db", _session_db), session_entry.session_id, history,
                )
            if session_entry.last_prompt_tokens > 0:
                _approx_tokens, _token_source = session_entry.last_prompt_tokens, "actual"
            elif _anchored is not None:
                _approx_tokens, _token_source = _anchored, "anchored"
            else:
                _approx_tokens, _token_source = estimate_messages_tokens_rough(history), "estimated"

        # Hard safety valve: force compression at an extreme message count regardless of tokens,
        # breaking the disconnect → no token data → no compression spiral. 5000 clears 1M+ sessions.
        _needs_compress = _approx_tokens >= _compress_token_threshold or _msg_count >= hs.hard_msg_limit

        if _needs_compress:
            # DB-backed cooldown (shared with context_compressor.py): survives gateway restarts, so a
            # failing compression is not re-triggered on every restart.
            # The in-memory dict was reset on every restart, re-triggering the same failing compression and
            # wedging session storage (#74136).
            _session_db = getattr(self, "_session_db", None)
            _getter = getattr(getattr(_session_db, "_db", _session_db), "get_compression_failure_cooldown", None)
            if _getter is not None:
                _cooldown_state = None
                with suppress(Exception):
                    _cooldown_state = _getter(session_entry.session_id)
                if _cooldown_state and _cooldown_state.get("remaining_seconds", 0) > 0:
                    logger.info(
                        "Session hygiene: skipping compression for %s; "
                        "previous failure cooldown active for %.1fs",
                        session_entry.session_id, _cooldown_state["remaining_seconds"],
                    )
                    _needs_compress = False

        if _needs_compress and await self._session_has_compression_in_flight(session_key):
            # A prior compression still holds the durable lock (e.g. a shielded worker left by /stop):
            # another attempt would wait up to 600s behind a commit the fence will refuse.
            logger.info(
                "Session hygiene: skipping compression for %s; "
                "another compression is already in flight", session_entry.session_id,
            )
            _needs_compress = False

        if _needs_compress:
            logger.info(
                "Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing "
                "(threshold: %s%% of %s = %s tokens)",
                _msg_count, f"{_approx_tokens:,}", _token_source,
                int(hs.threshold_pct * 100), f"{_hyg_context_length:,}", f"{_compress_token_threshold:,}",
            )
        return self._HygienePlan(_needs_compress, _approx_tokens, _msg_count, _warn_token_threshold)

    async def _hmwa_hygiene_wait_for_summary(self, attempt, hs, session_entry):
        """Progress-aware inline wait for the detached hygiene compressor. Returns the compressed
        transcript; raises ``HygieneTurnHoldExceeded`` (turn-hold budget) or
        ``asyncio.TimeoutError`` (idle/ceiling/fence cancel) for the caller's handlers.

        Idle timeout (fence ticks per streamed token) + hard ceiling + turn-hold cap."""
        from gateway.run import logger

        from gateway.run import HygieneTurnHoldExceeded, hygiene_wait_should_extend
        fence = attempt.commit_fence
        while True:
            if fence.is_cancelled:
                raise asyncio.TimeoutError
            # Charge the idle budget from the LAST PROGRESS event, else silence can approach 2x timeout.
            _hyg_waited = time.monotonic() - attempt.wait_started
            _slice = min(
                max(hs.timeout_seconds - fence.seconds_since_progress(), 0.005),
                max(hs.total_ceiling_seconds - _hyg_waited, 0.005),
            )
            # Cap the slice at the remaining turn-hold budget so a continuously-streaming worker can't
            # hold the turn until the ceiling. Budget exhausted → immediate timeout → abandonment.
            _turn_hold_remaining = hs.max_turn_hold_seconds - (time.monotonic() - attempt.wait_started)
            _slice = 0.005 if _turn_hold_remaining <= 0 else min(_slice, max(_turn_hold_remaining, 0.005))
            # Short poll so a /stop or /restart cancel is not stuck behind a full idle window.
            _idle_left = max(hs.timeout_seconds - fence.seconds_since_progress(), 0.005)
            _slice = min(_slice, 0.25)
            try:
                _compressed, _ = await asyncio.wait_for(asyncio.shield(attempt.future), timeout=_slice)
                return _compressed
            except asyncio.TimeoutError:
                if fence.is_cancelled:
                    raise
                _hyg_waited = time.monotonic() - attempt.wait_started
                _idle = fence.seconds_since_progress()
                # Never hold the TURN past the budget even while the summary streams: proceed on the
                # uncompressed transcript so the wire never trips a transport idle-timeout.
                if _hyg_waited >= hs.max_turn_hold_seconds:
                    logger.info(
                        "Session hygiene compression for session %s exceeded the turn-hold "
                        "budget (%.1fs >= %.1fs) — abandoning inline wait, proceeding "
                        "without compression this turn",
                        session_entry.session_id, _hyg_waited, hs.max_turn_hold_seconds,
                    )
                    raise HygieneTurnHoldExceeded(
                        f"turn-hold budget {hs.max_turn_hold_seconds:.1f}s "
                        f"elapsed after {_hyg_waited:.1f}s"
                    )
                if hygiene_wait_should_extend(
                    idle=_idle, timeout=hs.timeout_seconds, waited=_hyg_waited,
                    ceiling=hs.total_ceiling_seconds, fence_cancelled=fence.is_cancelled,
                ):
                    if _slice >= _idle_left - 1e-9:
                        logger.info(
                            "Session hygiene compression for session %s still streaming after "
                            "%.0fs (last progress %.1fs ago) — extending wait (ceiling %.0fs)",
                            session_entry.session_id, _hyg_waited, _idle, hs.total_ceiling_seconds,
                        )
                    continue
                raise

    async def _hmwa_hygiene_cancel_or_adopt(self, attempt, context):
        """Cancel the worker at the commit fence; on success release its lease and defer agent
        cleanup, returning ``None``. When the worker already crossed into its commit, consume and
        return the compressed transcript instead (a successful compaction is never a timeout; the
        turn may be held past the budget by up to the commit duration — by design). The lock-free
        ``commit_in_flight`` marker keeps the poll from spinning on a hung commit."""
        fence = attempt.commit_fence
        while not fence.commit_in_flight:
            cancelled = fence.try_cancel_before_commit()
            if cancelled is None:
                await asyncio.sleep(0.025)
            elif cancelled:
                fence.release_cancelled_compression_lock()
                self._hmwa_hygiene_defer_cleanup(attempt, context)
                return None
            else:
                break
        _compressed, _ = await attempt.future
        return _compressed

    def _hmwa_hygiene_defer_cleanup(self, attempt, context):
        """Hand the agent's cleanup to the still-running worker future and mark it deferred."""
        self._defer_agent_cleanup_until_future_done(attempt.future, attempt.agent, context=context)
        attempt.cleanup_deferred = True

    @staticmethod
    def _hmwa_hygiene_stamp(agent, desc, provenance_name, debug_label):
        from agent.session_activity import ActivityProvenance
        from gateway.run import _stamp_hygiene_compression_provenance
        _stamp_hygiene_compression_provenance(agent, desc, getattr(ActivityProvenance, provenance_name), debug_label)

    async def _hmwa_hygiene_notify(self, source, meta, message, what):
        """Best-effort user notice on the hygiene thread; failure is logged, never raised."""
        from gateway.run import logger

        try:
            _adapter = self._delivery_adapter_for(source)
            if _adapter and source.chat_id:
                await _adapter.emit_warning(source.chat_id, message, metadata=meta,
                                            logical_platform=source.platform)
        except Exception as _werr:
            logger.warning("Failed to deliver %s to user: %s", what, _werr)

    async def _hmwa_hygiene_record_failure_cooldown(self, hs, session_key, session_id, reason):
        """Escalate the failure streak (off-loop) and persist the cooldown, when enabled."""
        from gateway.run import _hygiene_cooldown_for_failure, _record_hygiene_cooldown
        if hs.failure_cooldown_seconds < 0:
            return
        _hyg_cooldown = await asyncio.to_thread(
            _hygiene_cooldown_for_failure, self, session_key, hs.failure_cooldown_seconds,
        )
        _record_hygiene_cooldown(self, session_id, _hyg_cooldown, reason)

    async def _hmwa_hygiene_on_turn_hold(self, attempt, hs, session_entry, session_key, source):
        """``except HygieneTurnHoldExceeded`` body: keep or cancel the worker's commit admission,
        notify the user, and re-raise; returns the compressed transcript only when the worker
        was already committing.

        Turn-hold expiry is an availability boundary, not a failure: the streak must NOT advance,
        only flat retry spacing is recorded. A watermark-fenced commit (rows appended after
        compression start survive as cloned tail) KEEPS admission: the turn proceeds uncompressed
        now and the summary is adopted at the worker's fenced commit — always cancelling burned
        every attempt for thinking summary models. Without the fence a late commit could clobber
        newer turns, so cancel."""
        from gateway.run import logger

        from gateway.run import (
            _HYGIENE_TURNHOLD_RETRY_SECONDS, _record_hygiene_cooldown, _reset_hygiene_failure_streak
        )
        fence = attempt.commit_fence
        _hyg_keep_admission = bool(getattr(fence, "commit_watermark_fenced", False)) and not fence.is_cancelled
        if _hyg_keep_admission:
            self._hmwa_hygiene_defer_cleanup(attempt, "session hygiene turn-hold")
            # NO retry-after here (it would also block the agent-side preflight compressor); spacing
            # comes from the durable compression lock. The done-callback records the flat retry-after
            # ONLY if the worker ends without committing anything.
            _sid, _skey, _agent = session_entry.session_id, session_key, attempt.agent

            def _hyg_adopt_or_space_retry(_fut, _gw=self, _sid=_sid, _skey=_skey, _agent=_agent):
                try:
                    _exc = _fut.exception()
                except (asyncio.CancelledError, Exception):
                    _committed = False
                else:
                    _committed = _exc is None and (
                        bool(getattr(_agent, "_last_compaction_in_place", False))
                        or getattr(_agent, "session_id", _sid) != _sid
                    )
                if _committed:
                    logger.info(
                        "Session hygiene compression for session %s finished after the "
                        "turn-hold was released — summary adopted at the watermark-fenced "
                        "commit boundary (#97963)", _sid,
                    )
                    try:
                        _reset_hygiene_failure_streak(_gw, _skey)
                    except Exception as _rs_err:
                        logger.debug("hygiene streak reset after deferred adoption failed: %s", _rs_err)
                else:
                    # Nothing to adopt (summary failed / fence refused / superseded): flat spacing so
                    # sustained traffic doesn't spawn and abandon a compressor every turn.
                    _record_hygiene_cooldown(
                        _gw, _sid, _HYGIENE_TURNHOLD_RETRY_SECONDS,
                        "hygiene compression deferred: turn-hold budget expired and the "
                        "detached attempt did not commit",
                    )

            attempt.future.add_done_callback(_hyg_adopt_or_space_retry)
            _log_suffix = (
                " — the watermark-fenced worker keeps its commit admission and the summary "
                "will be adopted when it finishes"
            )
        else:
            _adopted = await self._hmwa_hygiene_cancel_or_adopt(attempt, "session hygiene turn-hold")
            if _adopted is not None:
                return _adopted
            # Short flat retry-after, else every turn re-spawns, holds and cancels a compressor.
            _record_hygiene_cooldown(
                self, session_entry.session_id, _HYGIENE_TURNHOLD_RETRY_SECONDS,
                "hygiene compression deferred: turn-hold budget expired while the "
                "summary was still streaming",
            )
            _log_suffix = ""
        self._hmwa_hygiene_stamp(
            attempt.agent, "session hygiene compression turn-hold",
            "AGENT_COMPRESSION_TURNHOLD", "hygiene compression turn-hold activity stamp failed",
        )
        logger.info(
            "Session hygiene compression for session %s exceeded turn-hold budget (%.1fs); "
            "proceeding without compression this turn%s",
            session_entry.session_id, time.monotonic() - attempt.wait_started, _log_suffix,
        )
        await self._hmwa_hygiene_notify(
            source, attempt.meta, t("gateway.compress.turnhold_deferred"), "compression-turnhold notice",
        )
        raise

    async def _hmwa_hygiene_on_timeout(self, attempt, hs, session_entry, session_key, source):
        """``except asyncio.TimeoutError`` body: cancel at the commit fence, record the failure
        cooldown, warn the user, and re-raise; returns the compressed transcript only when the
        worker crossed the commit boundary first."""
        from gateway.run import logger

        from gateway.run import _hygiene_compression_timeout_message
        fence = attempt.commit_fence
        _hyg_waited = time.monotonic() - attempt.wait_started
        _hyg_total_exhausted = _hyg_waited >= hs.total_ceiling_seconds or fence.deadline_exceeded
        if _hyg_total_exhausted:
            # The worker checks this deadline between digest calls; keep its lease until it exits so
            # an unchanged session cannot overlap a retry (the release below is then a no-op).
            fence.retain_compression_lock_until_worker_done()
        # Capture fence state BEFORE try_cancel (which itself sets is_cancelled).
        _hyg_fence_cancelled = fence.is_cancelled
        _adopted = await self._hmwa_hygiene_cancel_or_adopt(attempt, "session hygiene timeout")
        if _adopted is not None:
            return _adopted
        await self._hmwa_hygiene_record_failure_cooldown(
            hs, session_key, session_entry.session_id,
            "session hygiene compression " + (
                "cancelled at commit fence" if _hyg_fence_cancelled
                else "total ceiling exhausted" if _hyg_total_exhausted
                else "timed out with no output from the summary model"
            ),
        )
        self._hmwa_hygiene_stamp(
            attempt.agent,
            "session hygiene compression cancelled at commit fence" if _hyg_fence_cancelled
            else "session hygiene compression timed out",
            "AGENT_COMPRESSION_TIMEOUT", "hygiene compression timeout activity stamp failed",
        )
        if _hyg_fence_cancelled:
            logger.warning(
                "Session hygiene compression for session %s was cancelled at the "
                "commit fence; continuing without compression", session_entry.session_id,
            )
            raise
        _hyg_elapsed = time.monotonic() - attempt.wait_started
        if _hyg_total_exhausted:
            logger.warning(
                "Session hygiene compression for session %s reached its total ceiling after "
                "%.1fs (progress observed=%s); continuing without compression",
                session_entry.session_id, _hyg_elapsed, fence.progress_observed,
            )
        else:
            logger.warning(
                "Session hygiene compression for session %s made no progress for %.1fs "
                "(total wait %.1fs, ceiling %.1fs); continuing without compression",
                session_entry.session_id, fence.seconds_since_progress(), _hyg_elapsed, hs.total_ceiling_seconds,
            )
        await self._hmwa_hygiene_notify(
            source, attempt.meta,
            _hygiene_compression_timeout_message(
                total_exhausted=_hyg_total_exhausted, elapsed=_hyg_elapsed,
                idle_timeout=hs.timeout_seconds, progress_observed=fence.progress_observed,
            ),
            "compression-timeout warning",
        )
        raise

    def _hmwa_hygiene_on_unwind(self, attempt, hs, session_entry, session_key):
        """``except BaseException`` body (caller re-raises): revoke commit admission BEFORE the host
        unwinds so the detached worker can never commit later, and record a cooldown — otherwise
        the next turn re-arms hygiene and waits up to 600s behind a fence that refuses again."""
        from gateway.run import logger

        from gateway.run import _hygiene_cooldown_for_failure, _record_hygiene_cooldown
        attempt.commit_fence.revoke_commit_admission()
        if not attempt.cleanup_deferred:
            self._hmwa_hygiene_defer_cleanup(attempt, "session hygiene unwind")
        if hs.failure_cooldown_seconds >= 0:
            try:
                _record_hygiene_cooldown(
                    self, session_entry.session_id,
                    _hygiene_cooldown_for_failure(self, session_key, hs.failure_cooldown_seconds),
                    "session hygiene compression cancelled at commit fence",
                )
            except Exception as _cd_err:
                logger.debug("hygiene unwind cooldown record failed: %s", _cd_err)

    async def _hmwa_hygiene_adopt_transcript(
        self, attempt, _compressed, history, plan, *, session_entry, source, _quick_key, run_generation,
    ):
        """Adopt a finished compression (rotation / in-place / refused); publishes the transcript to
        continue with on ``attempt.history``. Returns ``(rotated, in_place, new_count, new_tokens)``.

        Rewrite only on rotation (NEW session id): in-place compaction already soft-archived the
        old rows and rewrite_transcript() would DELETE them; neither rotation nor in-place signals
        FAILURE and an unconditional rewrite would leave only the summary. Write-before-repoint:
        a repoint-then-failed-rewrite would point the live entry at an empty session."""
        from gateway.run import logger

        from agent.model_metadata import estimate_messages_tokens_rough
        _hyg_agent = attempt.agent
        # _compress_context rotates to a NEW session_id so the old transcript stays intact/searchable.
        _hyg_new_sid = _hyg_agent.session_id
        _hyg_rotated = _hyg_new_sid != session_entry.session_id
        _hyg_in_place = bool(getattr(_hyg_agent, "_last_compaction_in_place", False))
        # Anti-growth guard: refuse a compression that did not shrink the transcript (seen 427K→598K).
        _hyg_in_toks = estimate_messages_tokens_rough(history)
        _hyg_out_toks = estimate_messages_tokens_rough(_compressed)
        if _hyg_rotated and _hyg_out_toks > _hyg_in_toks:
            logger.warning(
                "Gateway hygiene compression for session %s would grow transcript (~%s -> ~%s "
                "tokens); keeping the original transcript unchanged",
                session_entry.session_id, f"{_hyg_in_toks:,}", f"{_hyg_out_toks:,}",
            )
            _hyg_rotated = False
            _compressed = history
        # Only rewrite the transcript when rotation produced a NEW session id. In-place compaction does NOT
        # need a rewrite: archive_and_compact() has already soft-archived the previous active rows and
        # inserted the compacted messages as the new active set inside _compress_context(). Calling
        # rewrite_transcript() after in-place compaction would invoke replace_messages(active_only=False)
        # which DELETEs ALL rows — including the archived turns that archive_and_compact() deliberately
        # preserved (silent data loss, #61145). The danger this guards against (mirrors the /compress fix
        # #44794/#39704): if _compress_context returns a summary but neither rotates nor completes
        # archive_and_compact(), the session_id is unchanged for a FAILURE reason, and an unconditional
        # rewrite_transcript() would DELETE the original messages and replace them with only the compressed
        # summary (permanent data loss, #21301). Write-before-repoint (mirrors manual /compress): if we
        # repointed session_entry onto the child SID and rewrite_transcript then failed (lock/ENOSPC), the
        # live entry would already reference a brand-new empty session while the turn continues — the
        # conversation silently vanishes. Persist the child transcript first; only then rebind the live
        # entry.
        if _hyg_rotated:
            if not await self.async_session_store.rewrite_transcript(_hyg_new_sid, _compressed):
                logger.error(
                    "Session hygiene: failed to persist compressed transcript for rotated session "
                    "%s → %s; keeping the live entry on the original session so the "
                    "conversation is not dropped", session_entry.session_id, _hyg_new_sid,
                )
                # Fail closed: treat like no rotation.
                _hyg_rotated = False
                _hyg_in_place = False
            else:
                session_entry.session_id = _hyg_new_sid
                # The held turn lease follows the rotation (alias keys still serialize on this turn).
                self._rebind_turn_lease(_quick_key, run_generation, _hyg_new_sid)
                await self.async_session_store._save()
                await asyncio.to_thread(
                    self._sync_telegram_topic_binding, source, session_entry, reason="hygiene-compression",
                )

        if _hyg_rotated or _hyg_in_place:
            # Rewritten (rotation) or persisted by archive_and_compact() (in-place): reset token count.
            session_entry.last_prompt_tokens = 0
            attempt.history = _compressed
            _new_count = len(_compressed)
            _new_tokens = estimate_messages_tokens_rough(_compressed)
        else:
            # No rewrite happened — post-compression counts equal the pre-compression ones.
            _new_count = plan.msg_count
            _new_tokens = plan.approx_tokens
            logger.warning(
                "Gateway hygiene compression for session %s did not rotate or compact in place (%s) — "
                "preserving the original transcript instead of overwriting it with the summary (#21301).",
                session_entry.session_id, hygiene_no_commit_reason(_hyg_agent),
            )

        logger.info(
            "Session hygiene: compressed %s → %s msgs, ~%s → ~%s tokens",
            plan.msg_count, _new_count, f"{plan.approx_tokens:,}", f"{_new_tokens:,}",
        )
        if _new_tokens >= plan.warn_token_threshold:
            logger.warning("Session hygiene: still ~%s tokens after compression", f"{_new_tokens:,}")
        return _hyg_rotated, _hyg_in_place, _new_count, _new_tokens

    async def _hmwa_hygiene_apply_result(
        self, attempt, hs, _compressed, history, plan, *,
        session_entry, session_key, source, _quick_key, run_generation,
    ):
        """Adopt a finished hygiene compression, rebind the session + turn lease, record
        streak/cooldown, and warn the user on abort."""
        from gateway.run import logger

        from gateway.run import _reset_hygiene_failure_streak, hygiene_compaction_recovered
        _hyg_rotated, _hyg_in_place, _new_count, _new_tokens = await self._hmwa_hygiene_adopt_transcript(
            attempt, _compressed, history, plan, session_entry=session_entry, source=source,
            _quick_key=_quick_key, run_generation=run_generation,
        )
        # Summary failure aborts the compressor (nothing dropped). Warn the user visibly — agent.log
        # is invisible on TG/Discord — so they know the chat is "frozen" and can /compress or /reset.
        _comp = getattr(attempt.agent, "context_compressor", None)
        _hyg_aborted = _comp is not None and getattr(_comp, "_last_compress_aborted", False)
        # A fence-cancelled _compress_context returns the original transcript with
        # _last_compress_aborted False: treat that no-op as an abort so hygiene records a cooldown
        # instead of retrying into the 600s wait. A committed rotate/in-place is never an abort.
        _hyg_fence_cancelled = bool(attempt.commit_fence.is_cancelled and not _hyg_rotated and not _hyg_in_place)
        if _hyg_fence_cancelled:
            _hyg_aborted = True
        # Recovery decision lives in the unit-tested predicate: the "neither rotated nor in place"
        # path reuses pre-compression counts, so a numbers-only check would read a no-op as success.
        if not _hyg_aborted and hygiene_compaction_recovered(
            aborted=_hyg_aborted, rotated=_hyg_rotated, in_place=_hyg_in_place,
            msg_count=plan.msg_count, new_count=_new_count, approx_tokens=plan.approx_tokens,
            new_tokens=_new_tokens,
        ):
            await asyncio.to_thread(_reset_hygiene_failure_streak, self, session_key)
        if _hyg_aborted:
            await self._hmwa_hygiene_record_failure_cooldown(
                hs, session_key, session_entry.session_id,
                "session hygiene compression cancelled at commit fence" if _hyg_fence_cancelled
                else getattr(_comp, "_last_summary_error", None),
            )
            self._hmwa_hygiene_stamp(
                attempt.agent, "session hygiene compression aborted",
                "AGENT_COMPRESSION_COOLDOWN", "hygiene compression abort activity stamp failed",
            )
            if not _hyg_fence_cancelled:
                # Force-redact: provider exception text may contain credentials; this reaches users.
                from agent.redact import redact_sensitive_text
                _err = redact_sensitive_text(getattr(_comp, "_last_summary_error", None) or "unknown error", force=True)
                logger.warning("Session hygiene compression aborted: %s", _err)
                await self._hmwa_hygiene_notify(
                    source, attempt.meta,
                    "⚠️ Shortening the conversation history failed, so I kept everything as-is. "
                    "Run /compress to try again or /new to start fresh. If this keeps happening, "
                    "run `hermes doctor` on the host.",
                    "compression-failure warning",
                )
        # Configured aux model failed, recovered on the main model: only the user can fix that config.
        elif _comp is not None and getattr(_comp, "_last_aux_model_failure_model", None):
            _aux_model = getattr(_comp, "_last_aux_model_failure_model", "")
            _aux_err = getattr(_comp, "_last_aux_model_failure_error", None) or "unknown error"
            await self._hmwa_hygiene_notify(
                source, attempt.meta, f"ℹ️ Configured compression model `{_aux_model}` "
                f"failed ({_aux_err}). Recovered using your main "
                "model — context is intact — but you may want to "
                "check `auxiliary.compression.model` in config.yaml.",
                "aux-model-fallback notice",
            )

    async def _hmwa_hygiene_codex_compaction(self, hs, plan, history, session_entry, session_key, _hyg_runtime):
        """codex app-server runtime: the real context is the server-side thread, not the transcript
        mirror. The detached-agent path would only rewrite the mirror and its finally-eviction
        would destroy the live thread (next turn starts blank), so use the cached agent's
        thread/compact/start and KEEP it cached."""
        from gateway.run import logger

        from gateway.run import run_codex_hygiene_compaction
        # codex app-server runtime: the model's real context is the app-server's server-side thread, not the
        # transcript mirror. See #73503.
        _hyg_codex_auto = "native"
        _hyg_comp_cfg = hs.data.get("compression") if isinstance(hs.data, dict) else None
        if isinstance(_hyg_comp_cfg, dict):
            _hyg_codex_auto = str(_hyg_comp_cfg.get("codex_app_server_auto", "native") or "native")
        _hyg_codex_outcome = await run_codex_hygiene_compaction(
            self, session_key, session_entry.session_id, auto_mode=_hyg_codex_auto, history=history,
            approx_tokens=plan.approx_tokens, timeout_seconds=hs.total_ceiling_seconds,
            failure_cooldown_seconds=hs.failure_cooldown_seconds,
        )
        logger.info(
            "Session hygiene (codex app-server): %s (session=%s, mode=%s, ~%s tokens)",
            _hyg_codex_outcome, session_entry.session_id, _hyg_codex_auto, f"{plan.approx_tokens:,}",
        )

    async def _hmwa_hygiene_build_agent(self, _hyg_model, _hyg_runtime, session_entry):
        """Build the detached hygiene ``AIAgent`` with the live session's system prompt. Returns
        ``(agent, sync_session_db)``."""
        from gateway.run import logger

        from gateway.run import _GATEWAY_HYGIENE_PLATFORM, _seed_hygiene_system_prompt
        from run_agent import AIAgent
        try:
            _hyg_session_row = await self._session_db.get_session(session_entry.session_id)
        except Exception as exc:
            _hyg_session_row = None
            logger.warning(
                "Session hygiene could not restore the system prompt for session %s: %s. "
                "Preserving an empty prompt so the live turn rebuilds it with its "
                "configured providers.", session_entry.session_id, exc, exc_info=True,
            )
        _hyg_session_db = getattr(self._session_db, "_db", self._session_db)
        # With compression.checkpoint_required on, load the memory provider so the checkpoint exists
        # before any mutation; otherwise keep the fast path (no provider init).
        from hermes_cli.config import load_config as _load_cfg
        from utils import is_truthy_value as _is_truthy

        _hyg_checkpoint_required = _is_truthy(
            ((_load_cfg() or {}).get("compression") or {}).get("checkpoint_required"), default=False,
        )
        _hyg_agent = AIAgent(
            **_hyg_runtime, model=_hyg_model, max_iterations=4, quiet_mode=True,
            skip_memory=not _hyg_checkpoint_required, enabled_toolsets=["memory"],
            session_id=session_entry.session_id, session_db=_hyg_session_db,
        )
        _seed_hygiene_system_prompt(_hyg_agent, _hyg_session_row)
        # A rebuilt (not retained) prompt is deliberately stale for every real gateway surface.
        _hyg_agent.platform = _GATEWAY_HYGIENE_PLATFORM
        return _hyg_agent, _hyg_session_db

    async def _hmwa_hygiene_detached_attempt(
        self, attempt, hs, plan, history, _hyg_msgs, _hyg_model, _hyg_runtime,
        source, session_entry, session_key, _quick_key, run_generation,
    ):
        """Run one detached hygiene compression attempt end to end; publishes the transcript to
        continue with (compressed or original) on ``attempt.history``."""
        from gateway.run import HygieneTurnHoldExceeded
        from agent.conversation_compression import CompressionCommitFence
        _hyg_agent, _hyg_session_db = await self._hmwa_hygiene_build_agent(_hyg_model, _hyg_runtime, session_entry)
        attempt.agent = _hyg_agent
        try:
            # Hygiene owns the session binding, so prefer in-place compaction over minting a
            # continuation child. Without a SessionDB this stays False.
            _hyg_agent.compression_in_place = True
            _bind_hyg_state = getattr(getattr(_hyg_agent, "context_compressor", None), "bind_session_state", None)
            if callable(_bind_hyg_state):
                _bind_hyg_state(_hyg_session_db, session_entry.session_id)
            # Never finalize on close() — that would end the live gateway session row.
            _hyg_agent._end_session_on_close = False
            _hyg_agent._print_fn = lambda *a, **kw: None

            loop = asyncio.get_running_loop()
            _hyg_commit_fence = CompressionCommitFence(total_ceiling_seconds=hs.total_ceiling_seconds)
            # Default executor (NOT self._get_executor): a hung summary must never occupy an
            # agent-work slot. MUST run in the caller's contextvars (multiplex secret scope).
            attempt.commit_fence = _hyg_commit_fence
            attempt.future = loop.run_in_executor(
                None,
                # But it MUST run inside the caller's contextvars: under multiplex_profiles the profile
                # secret scope / HERMES_HOME override live in ContextVars, and a bare run_in_executor worker
                # starts with an empty Context — the summary model's get_secret(<PROVIDER>_API_KEY) then
                # fails closed (UnscopedSecretError) and every hygiene compaction silently degrades to a
                # lossy truncation (#100849 bundle).
                copy_context().run,
                # task_id = the live turn's dedup bucket (session row id), never "" (= reset every task).
                lambda: _hyg_agent._compress_context(
                    _hyg_msgs, "", approx_tokens=plan.approx_tokens, commit_fence=_hyg_commit_fence,
                    task_id=session_entry.session_id or "default",
                ),
            )
            attempt.wait_started = time.monotonic()
            try:
                _compressed = await self._hmwa_hygiene_wait_for_summary(attempt, hs, session_entry)
            except HygieneTurnHoldExceeded:
                _compressed = await self._hmwa_hygiene_on_turn_hold(attempt, hs, session_entry, session_key, source)
            except asyncio.TimeoutError:
                _compressed = await self._hmwa_hygiene_on_timeout(attempt, hs, session_entry, session_key, source)
            except BaseException:
                self._hmwa_hygiene_on_unwind(attempt, hs, session_entry, session_key)
                raise

            await self._hmwa_hygiene_apply_result(
                attempt, hs, _compressed, history, plan, session_entry=session_entry,
                session_key=session_key, source=source, _quick_key=_quick_key,
                run_generation=run_generation,
            )
        finally:
            # Evict the cached agent so the next turn rebuilds its system prompt.
            self._evict_cached_agent(session_key)
            if not attempt.cleanup_deferred:
                await self._cleanup_agent_resources_off_loop(_hyg_agent, context="session hygiene")

    async def _hmwa_run_session_hygiene(
        self, event, source, session_entry, session_key, history, _quick_key, run_generation,
    ):
        """Auto-compress pathologically large transcripts before the agent starts so oversized
        histories don't cause repeated truncation/context failures. Token source: the API's
        prompt_tokens from the last turn, else a char/4 estimate."""
        from gateway.run import logger

        from gateway.run import HygieneTurnHoldExceeded
        if not history or len(history) < 4:
            return history

        hs = await self._hmwa_hygiene_settings(source, session_key)
        # Hygiene can never land with compression disabled; a sub-limit transcript is the identity (#111988).
        if not hs.compression_enabled:
            return self._bound_hygiene_payload(history, hs, session_entry)
        plan = await self._hmwa_hygiene_plan(hs, history, session_entry, session_key)
        # No compression this turn (under both thresholds, cooldown, or one already in flight): without
        # the bound the model would get the full uncompressed transcript.
        if not plan.needs_compress:
            return self._bound_hygiene_payload(history, hs, session_entry)

        attempt = self._HygieneAttempt(agent=None, meta=self._event_thread_metadata(event, source), history=history)
        try:
            _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                source=source, session_key=session_key,
                user_config=hs.data if isinstance(hs.data, dict) else None,
            )
            if str(_hyg_runtime.get("api_mode") or "").lower() == "codex_app_server":
                await self._hmwa_hygiene_codex_compaction(hs, plan, history, session_entry, session_key, _hyg_runtime)
            elif _hyg_runtime.get("api_key"):
                # Pass the FULL transcript (tool results included) as the agent loop does: filtering
                # to user/assistant starved the compressor (tool results are the bulk of context).
                _hyg_msgs = [m for m in history if m.get("role") in {"user", "assistant", "tool"}]
                if len(_hyg_msgs) >= 4:
                    await self._hmwa_hygiene_detached_attempt(
                        attempt, hs, plan, history, _hyg_msgs, _hyg_model, _hyg_runtime,
                        source, session_entry, session_key, _quick_key, run_generation,
                    )
        except HygieneTurnHoldExceeded:
            # Availability boundary, not a failure — already logged at INFO by the turn-hold handler.
            # Must not hit the generic "auto-compress failed" warning below: that log is how thinking-model
            # deployments read as permanently broken (#97963; surfaced by @686f6c61 in PR #99657).
            pass
        except Exception as e:
            logger.warning("Session hygiene auto-compress failed: %s", e)
        # A landed compression published a NEW transcript on attempt.history: leave it byte-identical.
        # Anything else (turn-hold, timeout, unwind, codex path) left the FULL uncompressed transcript
        # there — that is the fail-closed case (#111988).
        if attempt.history is history:
            return self._bound_hygiene_payload(history, hs, session_entry)
        return attempt.history

    @staticmethod
    def _bound_hygiene_payload(history, hs, session_entry):
        """``bound_model_input_without_hygiene`` over ``hs.hard_msg_limit``, with one INFO line when the
        cut is real. Below the limit this is the identity — no allocation, no behaviour change."""
        from gateway.run import logger

        bounded = bound_model_input_without_hygiene(history, hs.hard_msg_limit)
        if bounded is not history:
            logger.info(
                "Session hygiene did not land for %s: bounding the model payload to %s of %s "
                "messages (hard limit %s) — the stored transcript is unchanged",
                session_entry.session_id, len(bounded), len(history), hs.hard_msg_limit,
            )
        return bounded

    async def _hmwa_first_contact_notes(self, source, history, turn_sidecar_notes):
        """First-ever-message onboarding note + one-time 'no home channel' prompt (both only when
        the session has no history). Delivered on the user message (sidecar), NOT the ephemeral
        system prompt: present-on-turn-1/absent-on-turn-2 was a guaranteed prompt diff + rebuild."""
        from gateway.run import logger

        from gateway.run import _hermes_home, _home_target_env_var, _load_gateway_config
        if history:
            return
        if not await self.async_session_store.has_any_sessions():
            _intro_note = (
                "[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available commands. "
                "Keep the introduction concise -- one or two sentences max.]"
            )
            # onboarding.profile_build == "ask" (default) and not yet offered: swap the plain intro for
            # a consent-gated profile-build directive. Fires at most once.
            try:
                from agent.onboarding import (
                    PROFILE_BUILD_FLAG, is_seen, mark_seen, profile_build_directive,
                    profile_build_mode,
                )
                _onb_cfg = _load_gateway_config()
                if profile_build_mode(_onb_cfg) == "ask" and not is_seen(_onb_cfg, PROFILE_BUILD_FLAG):
                    turn_sidecar_notes.append(profile_build_directive().strip())
                    mark_seen(_hermes_home / "config.yaml", PROFILE_BUILD_FLAG)
                else:
                    turn_sidecar_notes.append(_intro_note)
            except Exception as _pb_err:
                logger.debug("Profile-build onboarding directive failed, using plain intro: %s", _pb_err)
                turn_sidecar_notes.append(_intro_note)

        # One-time prompt if no home channel is set (webhooks deliver to configured targets instead).
        if not source.platform or source.platform in (Platform.LOCAL, Platform.WEBHOOK):
            return
        platform_name = source.platform.value
        env_key = _home_target_env_var(platform_name)
        # Multiplex: the home channel may live only in the profile secret scope, not os.environ.
        home_env = ""
        if env_key:
            with suppress(Exception):
                from agent.secret_scope import get_secret
                home_env = (get_secret(env_key) or "").strip()
            home_env = home_env or (os.getenv(env_key) or "").strip()
        # Also honor in-memory / yaml home_channel on this platform.
        with suppress(Exception):
            if not home_env and self.config.get_home_channel(source.platform):
                home_env = "set"
        # Secondary-profile platforms may only exist under that profile's config — re-read in scope.
        if not home_env:
            with suppress(Exception):
                from gateway.config import load_gateway_config as _lgc
                prof = (getattr(source, "profile", None) or "").strip()
                if prof and prof != "default" and _lgc().get_home_channel(source.platform):
                    home_env = "set"
        if not home_env:
            # Slack routes every command through the parent `/hermes`; bare `/sethome` would fail.
            sethome_cmd = "/hermes sethome" if source.platform == Platform.SLACK else "/sethome"
            await self._deliver_platform_notice(
                source, f"📬 No home channel is set for {platform_name.title()}. "
                f"A home channel is where Hermes delivers cron job results and cross-platform "
                f"messages.\n\nType {sethome_cmd} to make this chat your home channel, or ignore "
                f"to skip.",
            )

    def _hmwa_apply_message_timestamp(self, event, message_text):
        """Capture the platform event time as message metadata and keep the persisted transcript
        clean — strip any leading timestamp prefix and the Discord triggering-message note (a
        model instruction, not authored text) — regardless of the toggle; only the in-context
        RENDER is gated behind gateway.message_timestamps.enabled (default OFF)."""
        from gateway.run import logger

        from gateway.run import _load_gateway_config, _message_timestamps_enabled
        from gateway.run_inbound import strip_discord_triggering_note
        persist_user_message = None
        persist_user_timestamp = None
        try:
            from hermes_time import get_timezone as _get_evt_tz
            from gateway.message_timestamps import (
                coerce_message_timestamp as _coerce_msg_ts,
                render_user_content_with_timestamp as _render_msg_ts,
                strip_leading_message_timestamps as _strip_msg_ts,
            )
            _evt_tz = _get_evt_tz()
            if message_text and isinstance(message_text, str):
                _clean_message_text, _embedded_ts = _strip_msg_ts(message_text, tz=_evt_tz)
                persist_user_message = strip_discord_triggering_note(event, _clean_message_text)
                _event_epoch = _coerce_msg_ts(getattr(event, "timestamp", None), tz=_evt_tz)
                persist_user_timestamp = _event_epoch if _event_epoch is not None else _embedded_ts
                if _message_timestamps_enabled(_load_gateway_config()):
                    message_text = _render_msg_ts(_clean_message_text, persist_user_timestamp, tz=_evt_tz)
                else:
                    # Toggle off: the model sees the clean message; timestamp stored for later opt-in.
                    message_text = _clean_message_text
        except Exception as _ts_err:
            logger.debug("Message timestamp injection failed (non-fatal): %s", _ts_err)
        return message_text, persist_user_message, persist_user_timestamp

    async def _hmwa_stop_typing_for_turn(self, event, source):
        """Stop the typing indicator (never raises). Slack AI status is scoped to a thread/
        workspace, so preserve the routing metadata used by the response delivery path."""
        with suppress(Exception):
            _typing_adapter = self._delivery_adapter_for(source)
            _kind = type(_typing_adapter)
            if _typing_adapter and callable(getattr(_kind, "_stop_typing_with_metadata", None)):
                await _typing_adapter._stop_typing_with_metadata(source.chat_id, self._event_thread_metadata(event, source))
            elif _typing_adapter and callable(getattr(_kind, "stop_typing", None)):
                await _typing_adapter.stop_typing(source.chat_id)

    async def _hmwa_shape_agent_response(
        self, agent_result, source, history, session_entry, session_key,
        _quick_key, run_generation, _run_start_session_id, _platform_name, _msg_start_time,
        persist_user_display_kind: Optional[str] = None,
    ):
        """Turn the raw agent result into the outbound text: sentinel/silence handling, response
        logging, resume-pending clear, empty-response normalization, and identity-guarded
        post-compression session_id propagation. Returns
        ``(response, _intentional_silence, agent_messages)``."""
        from gateway.run import logger

        from gateway.run import (
            _is_gateway_hidden_reasoning_incomplete_turn, _normalize_empty_agent_response,
            _sanitize_gateway_final_response, _should_clear_resume_pending_after_turn,
        )
        response = agent_result.get("final_response") or ""
        # Hidden-reasoning-only retry exhaustion: the loop's sentinel text doubles as final_response
        # and would be delivered verbatim (peer agents would ingest it as a completed turn).
        if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
            response = ""
        _intentional_silence = self._is_intentional_silence(agent_result, response)
        # A queued (/queue) chain's TERMINAL turn owns the silence verdict, not the event that
        # opened the chain: an internal follow-up may go silent, a human one must not.
        _silence_kind = agent_result.get("queued_terminal_display_kind", persist_user_display_kind)
        if _intentional_silence and not is_machinery_display_kind(_silence_kind):
            logger.warning(
                "silence marker rejected on a user turn: platform=%s chat=%s",
                _platform_name, source.chat_id or "unknown",
            )
            _intentional_silence = False
            response = _UNEXPECTED_SILENCE_REPLY

        # "(empty)" = the model produced no visible content after exhausting all retries. One
        # text with the CLI explainer and the desktop (agent/turn_explainers.py) so the user
        # reads the same words on every surface.
        if response == "(empty)" and not _intentional_silence:
            from agent.turn_explainers import EMPTY_RESPONSE_EXPLANATION

            _model = str(agent_result.get("model") or "").strip() or "The model"
            response = "⚠️ " + EMPTY_RESPONSE_EXPLANATION.format(model=_model)
        agent_messages = agent_result.get("messages", [])
        logger.info(
            "response ready: platform=%s chat=%s session=%s time=%.1fs api_calls=%d response=%d chars",
            _platform_name, source.chat_id or "unknown", session_key or "unknown",
            time.time() - _msg_start_time, agent_result.get("api_calls", 0), len(response),
        )

        # Successful turn: clear the consecutive-restart stuck-loop counter and resume_pending (set
        # by drain-timeout shutdown) so later messages don't get the restart-interruption note.
        if session_key and _should_clear_resume_pending_after_turn(agent_result):
            await self._clear_restart_failure_count(session_key)
            try:
                await self.async_session_store.clear_resume_pending(session_key)
            except Exception as _e:
                logger.debug("clear_resume_pending failed for %s: %s", session_key, _e)

        # Normalize empty responses: surface errors, partial failures, and work-without-text.
        # Fix for #18765.
        if not _intentional_silence:
            response = _normalize_empty_agent_response(agent_result, response, history_len=len(history))
            response = _sanitize_gateway_final_response(source.platform, response)

        # The agent thread already updated the contextvar; propagate to SessionEntry + _save() only
        # if the binding still points at the session this run was launched against.
        if agent_result.get("session_id") and agent_result["session_id"] != session_entry.session_id:
            if session_entry.session_id == _run_start_session_id:
                session_entry.session_id = agent_result["session_id"]
                # The held turn lease follows the rotation (persistence writes to the NEW id).
                self._rebind_turn_lease(_quick_key, run_generation, session_entry.session_id)
                await self.async_session_store._save()
                await self.async_session_store._record_gateway_session_peer(
                    session_entry.session_id, session_key, source,
                )
                await asyncio.to_thread(
                    self._sync_telegram_topic_binding, source, session_entry, reason="agent-result-compression",
                )
            else:
                logger.info(
                    "Skipping agent-result session split sync for %s because the session binding "
                    "moved from %s to %s before compression finished",
                    session_key or "?", _run_start_session_id, session_entry.session_id,
                )
        return response, _intentional_silence, agent_messages

    # reasoning_style → (header line, per-line quote prefix for blank / non-blank lines)
    _REASONING_QUOTE_STYLES = {
        "subtext": ("-# 💭 Reasoning", "-# ", "-#"), "blockquote": ("> 💭 **Reasoning:**", "> ", ">")
    }

    def _hmwa_prepend_reasoning(self, agent_result, response, source, _intentional_silence):
        """Prepend the last reasoning block when show_reasoning is on for this platform. Mattermost
        requires an explicit per-platform opt-in (scratch text, not final-answer content)."""
        from gateway.run import _load_gateway_config, _platform_config_key, _resolve_gateway_display_bool
        try:
            _show_reasoning_effective = _resolve_gateway_display_bool(
                _load_gateway_config(), _platform_config_key(source.platform), "show_reasoning",
                default=bool(getattr(self, "_show_reasoning", False)), platform=source.platform,
            )
        except Exception:
            _show_reasoning_effective = getattr(self, "_show_reasoning", False)
        last_reasoning = agent_result.get("last_reasoning")
        if not (_show_reasoning_effective and response and not _intentional_silence and last_reasoning):
            return response
        from gateway.stream_consumer_fences import escape_code_fences_for_display
        # Collapse long reasoning to keep messages readable
        lines = last_reasoning.strip().splitlines()
        if len(lines) > 15:
            display_reasoning = "\n".join(lines[:15]) + f"\n_... ({len(lines) - 15} more lines)_"
        else:
            display_reasoning = last_reasoning.strip()
        # Per-platform render style from config (default fenced code block; "-# " subtext when configured).
        try:
            from gateway.display_config import resolve_display_setting
            _reasoning_style = resolve_display_setting(
                _load_gateway_config(), _platform_config_key(source.platform), "reasoning_style", "code",
            )
        except Exception:
            _reasoning_style = "code"
        _quote = self._REASONING_QUOTE_STYLES.get(_reasoning_style)
        if _quote:
            header, prefix, empty = _quote
            _quoted = "\n".join(f"{prefix}{ln}" if ln else empty for ln in display_reasoning.splitlines())
            return f"{header}\n{_quoted}\n\n{response}"
        # Escape ``` inside reasoning so inner fences don't break the outer code block.
        display_reasoning = escape_code_fences_for_display(display_reasoning)
        return f"💭 **Reasoning:**\n```\n{display_reasoning}\n```\n\n{response}"

    def _hmwa_runtime_footer_line(self, agent_result, source, _turn_seconds):
        """Runtime-metadata footer for the FINAL message of the turn; off by default
        (display.runtime_footer.enabled=false)."""
        from gateway.run import logger

        from gateway.run import _load_gateway_config, _platform_config_key, _terminal_scope_cwd
        try:
            from gateway.runtime_footer import build_footer_line as _bfl
            return _bfl(
                user_config=_load_gateway_config(),
                platform_key=_platform_config_key(source.platform), model=agent_result.get("model"),
                context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,
                context_length=agent_result.get("context_length") or None,
                cwd=_terminal_scope_cwd(""), turn_seconds=_turn_seconds,
                requested_model=agent_result.get("requested_model"),
                served_model=agent_result.get("served_model"),
            )
        except Exception as _footer_err:
            logger.debug("runtime_footer build failed: %s", _footer_err)
            return ""

    async def _hmwa_post_turn_hooks(self, hook_ctx, agent_result, response):
        """agent:end hook, process-watcher scheduling, and watch-notification drain."""
        from gateway.run import logger

        await self.hooks.emit("agent:end", {
            **hook_ctx, "response": (response or "")[:500], "model": agent_result.get("model", ""),
            "provider": agent_result.get("provider", ""),
        })

        # Pending process watchers (check_interval on background processes)
        try:
            from tools.process_registry import process_registry
            # Detach the batch atomically (reassign, not clear()) so concurrent appends aren't dropped.
            watchers = process_registry.pending_watchers
            process_registry.pending_watchers = []
            for i, watcher in enumerate(watchers):
                asyncio.create_task(self._run_process_watcher(watcher))
                if i % 100 == 99:
                    await asyncio.sleep(0)
        except Exception as e:
            logger.error("Process watcher setup error: %s", e)

        # Drain watch notifications that arrived during the run; the queue also carries process /
        # async-delegation completions owned elsewhere — inject only watch-type events.
        try:
            from tools.process_registry import process_registry as _pr
            await self._drain_watch_notifications(_pr.completion_queue)
        except Exception as e:
            logger.debug("Watch queue drain error: %s", e)

    # One owner for the boundary copy (agent/turn_failure_copy.py): the core closer in
    # agent/conversation_loop.py writes the same row on the paths that never reach this layer.

    def _hmwa_add_failed_turn_notice(self, response, notice):
        """Make failed-turn delivery explicit without replacing the provider-specific guidance."""
        response = str(response or "").strip()
        return f"{response}\n\n{notice}" if response else notice

    def _hmwa_failed_turn_notice(self, agent_result):
        """Choose retry guidance without assuming completed tool effects can be repeated safely."""
        from gateway.media_repair import _current_turn_messages
        # Compression during the failed turn can move the slice boundary; the shared helper falls
        # back to the last user row so tool evidence is not silently dropped.
        turn_messages = _current_turn_messages(
            agent_result.get("messages", []) or [], agent_result.get("history_offset", 0),
        )
        if any(
            message.get("role") == "tool"
            or (message.get("role") == "assistant" and message.get("tool_calls"))
            for message in turn_messages
        ):
            return PARTIAL_FAILED_TURN_NOTICE
        return FAILED_TURN_NOTICE

    async def _hmwa_close_failed_turn(self, session_id, notice):
        """Append the gateway-owned assistant boundary iff the durable tail is an open user row.

        The tail, not "did the gateway write the user row", is the key: on the primary path the
        agent's turn-start flush already persisted the row (so the platform-id dedupe skips the
        gateway write), and a platform redelivery of an already-closed turn must not stack a
        second assistant row."""
        if await self.async_session_store.transcript_tail_role(session_id) != "user":
            return
        await self.async_session_store.append_to_transcript(session_id, {
            "role": "assistant", "content": notice, "timestamp": time.time(),
        })

    def _hmwa_classify_turn_failure(self, agent_result, history, session_entry):
        """Classify a finished turn for transcript persistence. Returns
        ``(agent_failed_early, hidden_reasoning_incomplete, is_context_overflow_failure)``.

        Context-overflow failures must NOT persist the user message (session would grow and
        reproduce the failure forever); transient failures (429/timeout/5xx) DO."""
        from gateway.run import logger

        from gateway.run import _is_gateway_hidden_reasoning_incomplete_turn
        # Save the full conversation to the transcript, including tool calls. This preserves the complete
        # agent loop (tool_calls, tool results, intermediate reasoning) so sessions can be resumed with full
        # context and transcripts are useful for debugging and training data. IMPORTANT: For
        # context-overflow failures (compression exhausted, generic 400 on large sessions) we must NOT
        # persist the user's message — doing so would grow the session further and cause the same failure on
        # the next attempt, an infinite loop. (#1630, #9893) Transient failures (429, timeout, connection
        # error, provider 5xx) are different: the session is not oversized, and silently dropping the user
        # message causes severe context loss on retry — the agent forgets what was just asked. Persist the
        # user turn so the conversation is preserved. (#7100)
        agent_failed_early = bool(agent_result.get("failed"))
        hidden_reasoning_incomplete = _is_gateway_hidden_reasoning_incomplete_turn(agent_result)
        is_context_overflow_failure = is_context_overflow_failure_result(agent_result, len(history))
        if is_context_overflow_failure:
            logger.info(
                "Skipping transcript persistence for context-overflow "
                "failure in session %s to prevent session growth loop.", session_entry.session_id,
            )
        elif agent_failed_early:
            logger.info(
                "Transient agent failure in session %s — persisting user "
                "message so conversation context is preserved on retry.", session_entry.session_id,
            )
        elif hidden_reasoning_incomplete:
            logger.warning(
                "Suppressing hidden-reasoning-only incomplete gateway turn for session %s: %s",
                session_entry.session_id, agent_result.get("error", "processing incomplete"),
            )
        return agent_failed_early, hidden_reasoning_incomplete, is_context_overflow_failure

    async def _hmwa_compression_exhaustion_reset(
        self, agent_result, response, session_entry, session_key, source,
    ):
        """Auto-reset a permanently oversized session so the next message starts fresh instead of
        replaying the oversized context forever. Never on a lock-contended defer — that is the
        OPPOSITE case (a concurrent path holds the lock and is shrinking it). Returns
        ``(response, session_entry)``."""
        from gateway.run import logger

        # When compression is exhausted, the session is permanently too large to process. (#9893) Never wipe
        # the session for that — retry-next-message semantics apply (#69870 lock-skip consumer; salvaged
        # from #49874).
        if agent_result.get("compression_deferred"):
            logger.info(
                "Compression deferred for session %s — the compression "
                "lock is held by a concurrent compressor. Keeping the "
                "session intact; the next message retries normally.",
                session_entry.session_id if session_entry else "?",
            )
        elif agent_result.get("compression_exhausted") and session_entry and session_key:
            logger.info("Auto-resetting session %s after compression exhaustion.", session_entry.session_id)
            new_entry = await self.async_session_store.reset_session(session_key)
            self._evict_cached_agent(session_key)
            # Conversation boundary: the funnel clears every conversation-scoped per-session dict.
            self._clear_conversation_scope(session_key, reason="compression_exhausted_reset")
            if new_entry is not None:
                # Re-point the Telegram topic binding at the fresh session, or the binding-heal walk
                # switches the next message back onto the bloated child and re-triggers exhaustion
                # forever. No-op on non-topic lanes.
                # Compression rotated session_entry.session_id to the oversized compressed child earlier
                # this turn (the agent-result sync above), and that _sync also rewrote the (chat_id,
                # thread_id) -> bloated-child binding. reset_session swaps in a clean, parentless session,
                # but without re-syncing the binding the next inbound message in this topic gets
                # switch_session'd back onto the bloated child by the binding-heal walk, reloads the
                # oversized transcript, and re-triggers compression exhaustion forever (#35809 — regression
                # of the #9893/#10063 auto-reset).
                session_entry = new_entry
                await asyncio.to_thread(
                    self._sync_telegram_topic_binding, source, session_entry, reason="compression-exhausted-reset",
                )
            response = (response or "") + (
                "\n\n🔄 Session auto-reset — the conversation exceeded the maximum context size and "
                "could not be compressed further. Your next message will start a fresh session."
            )
        return response, session_entry

    @staticmethod
    def _hmwa_user_transcript_entry(event, prepared, ts):
        """Transcript row for the inbound user turn (clean text + event time when captured)."""
        # Transient failure (429/timeout/5xx): persist the user message so the next message can load a
        # transcript that reflects what was said. The caller pairs it with a stable assistant safety
        # boundary rather than the provider error text. Hidden-reasoning-only incomplete turns follow the
        # same persistence rule so peer-agent channels don't ingest provider details. (#7100, #51628)
        _user_entry = {
            "role": "user",
            "content": (
                prepared.persist_user_message if prepared.persist_user_message is not None
                else prepared.message_text
            ),
            "timestamp": prepared.persist_user_timestamp if prepared.persist_user_timestamp is not None else ts,
        }
        if prepared.persist_user_display_kind:
            _user_entry["display_kind"] = prepared.persist_user_display_kind
        if prepared.persistence_owner:
            _user_entry["display_metadata"] = {"gateway_input_owner": prepared.persistence_owner}
        if getattr(event, "message_id", None):
            _user_entry["message_id"] = str(event.message_id)
        return _user_entry

    async def _hmwa_persist_turn_transcript(
        self, *, event, source, session_entry, session_key, agent_result, agent_messages,
        prepared, response, agent_failed_early, hidden_reasoning_incomplete, is_context_overflow_failure,
    ):
        """Persist this turn to the transcript (session_meta on first turn, closed failed turn on
        transient failure, nothing on context overflow), update last_prompt_tokens, and re-baseline the
        cached agent's message count."""
        from gateway.run import logger

        from gateway.run import _resolve_gateway_model
        ts = time.time()  # Unix epoch float — consistent with DB storage
        store = self.async_session_store
        sid = session_entry.session_id
        history = prepared.history
        # The agent already persisted this turn's rows (codex app-server reports agent_persisted=True
        # too); skip the DB write. Default = a session DB exists; non-persisting runtimes pass False.
        # The agent already persisted these messages to SQLite via _flush_messages_to_session_db(), so skip
        # the DB write here to prevent the duplicate-write bug (#860 / #42039). This holds for the codex
        # app-server runtime too: although it early-returns and bypasses conversation_loop's per-step
        # flushes, it flushes its own projected assistant/tool messages before returning and reports
        # agent_persisted=True (see agent/codex_runtime.py). Reading the flag (default = self._session_db is
        # not None) keeps the persistence contract explicit and lets any future non-persisting runtime opt
        # into a gateway-side write by returning False.
        agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)
        _user_row = self._hmwa_user_transcript_entry(event, prepared, ts)

        if is_context_overflow_failure:
            pass  # Skip all transcript writes — don't grow a broken session
        else:
            if not history:
                # Fresh session: the tool definitions (as sent in the API request) make the transcript
                # self-describing.
                await store.append_to_transcript(sid, {
                    "role": "session_meta",
                    "tools": agent_result.get("tools", []) or [],
                    "model": _resolve_gateway_model(),
                    "platform": source.platform.value if source.platform else "",
                    "timestamp": ts,
                })
            if agent_failed_early or hidden_reasoning_incomplete:
                # Transient failure / hidden-reasoning incomplete: persist the user message without
                # the provider error text (a gateway hint, not model output). Dedupe on platform
                # message_id (Telegram retries after transient failures).
                if event.message_id and await store.has_platform_message_id(sid, str(event.message_id)):
                    logger.info(
                        "Skipping duplicate user turn (message_id=%s) in session %s",
                        event.message_id, sid,
                    )
                else:
                    await store.append_to_transcript(sid, _user_row, skip_db=agent_persisted)
                # Close the failed turn: a user-only tail lets alternation repair merge this request
                # into an unrelated future message and replay stale side effects (#107070).
                await self._hmwa_close_failed_turn(sid, self._hmwa_failed_turn_notice(agent_result))
            else:
                # Only the NEW messages: history_offset (what the agent saw), not len(history), which
                # counts session_meta entries stripped before the agent saw them.
                history_len = agent_result.get("history_offset", len(history))
                new_messages = agent_messages[history_len:] if len(agent_messages) > history_len else []
                if not new_messages:
                    # Edge case: fall back to simple user/assistant rows.
                    await store.append_to_transcript(sid, _user_row, skip_db=agent_persisted)
                    if response:
                        await store.append_to_transcript(
                            sid, {"role": "assistant", "content": response, "timestamp": ts},
                            skip_db=agent_persisted,
                        )
                else:
                    # Attach the inbound platform message_id to the first user entry so platform-level
                    # quote-resolution (e.g. Yuanbao) can find earlier @bot messages by original id.
                    _user_msg_id_attached = False
                    for msg in new_messages:
                        if msg.get("role") == "system":
                            continue  # rebuilt each run
                        entry = {**msg, "timestamp": ts}
                        if (
                            not _user_msg_id_attached
                            and msg.get("role") == "user"
                            and event.message_id
                            and "message_id" not in entry
                        ):
                            entry["message_id"] = str(event.message_id)
                            _user_msg_id_attached = True
                        await store.append_to_transcript(sid, entry, skip_db=agent_persisted)

        # The agent persists token counts/model itself; keep only last_prompt_tokens for hygiene.
        await store.update_session(
            session_entry.session_key, last_prompt_tokens=agent_result.get("last_prompt_tokens", 0),
            touch_activity=not bool(getattr(event, "internal", False)),
        )

        # Re-baseline the cached agent's message_count now that ALL of this turn's writes are done:
        # the coherence guard snapshots at agent-BUILD time, so our own writes would otherwise
        # trigger a rebuild next turn (destroying prompt caching).
        await self._refresh_agent_cache_message_count(session_key, sid)

    async def _hmwa_deliver_turn_response(
        self, event, source, session_entry, session_key, run_generation,
        agent_result, agent_messages, response, _footer_line, _intentional_silence,
    ):
        """Final delivery decisions: intentional silence, voice reply, streamed-turn media/footer.
        Returns the text for the adapter to send, or ``None`` when already delivered."""
        from gateway.run import logger

        if diagnostic_wake_muted(event):
            return None
        # Intentional silence is a delivery decision: the [SILENT] turn stays persisted (alternation).
        if _intentional_silence:
            logger.info("Suppressing intentional silence marker for session %s", session_entry.session_id)
            response = ""

        adapter = self._delivery_adapter_for(source)
        # Auto voice reply (TTS audio before the text) unless streaming TTS already delivered audio.
        _streaming_tts_done = adapter is not None and bool(
            getattr(adapter, "_streaming_tts_turn_completed", lambda *_a, **_k: False)(session_key, run_generation)
        )
        if not _streaming_tts_done and self._should_send_voice_reply(
            event, response, agent_messages, already_sent=bool(agent_result.get("already_sent")),
        ):
            await self._send_voice_reply(event, response)

        # Streamed responses still need MEDIA: files delivered (chunks carry the tags verbatim). Never
        # skip when the agent failed: the error text is new content streaming didn't show.
        if agent_result.get("already_sent") and not agent_result.get("failed"):
            if response and adapter:
                await self._deliver_media_from_response(response, event, adapter)
            # Streaming delivered the body, but the footer was held back (`not already_sent` gate).
            if _footer_line and adapter:
                try:
                    await adapter.send(source.chat_id, _footer_line, metadata=self._event_thread_metadata(event, source))
                except Exception as _e:
                    logger.debug("trailing footer send failed: %s", _e)
            # Return None so the body isn't sent twice; stash the delivered text on the event for the
            # /loop and /goal hooks that read the return value.
            with suppress(Exception):
                event._streamed_final_response = str(response or "")
            return None

        return response

    # Chat-side next steps keyed by HTTP status; Hermes commands only (/login is the gateway's own
    # sign-in, `{relogin}` the profile-aware host equivalent, filled from the turn's agent provider).
    _STATUS_HINTS = {
        401: (" Your sign-in to the AI model service has expired or the API key is wrong. "
              "Use /login here, or run `{relogin}` on the host."),
        402: " Your AI model service balance or quota is used up. Top it up on the service's website, or use /model to switch models.",
        529: " The AI model service is temporarily overloaded. Wait a moment, then use /retry.",
    }

    async def _hmwa_agent_error_reply(self, e, event, source, session_entry, session_key, prepared):
        """``except Exception`` body of the agent turn: stop typing, log, persist the inbound user
        turn once and close it, and build the sanitized user-facing error reply."""
        from gateway.run import logger

        # Retain Slack thread/workspace routing so a failed turn cannot leave its status visible.
        await self._hmwa_stop_typing_for_turn(event, source)
        logger.exception("Agent error in session %s", session_key)
        status_code = getattr(e, "status_code", None)
        if status_code in {400, 500} and len(prepared.history) > 50:
            # Context overflow / payload too large: a deterministic rejection (#107567), and the same
            # no-grow rule as the persist path (#1630) — nothing is written into an oversized session.
            from gateway.run import _CONTEXT_OVERFLOW_REPLY
            return _CONTEXT_OVERFLOW_REPLY
        # Replay can coalesce inputs; only this input's durable marker establishes ownership.
        try:
            if prepared.message_text is not None and session_entry is not None:
                _owned = await self.async_session_store.has_input_owner(
                    prepared.persistence_session_id, prepared.persistence_owner,
                )
                if not _owned:
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id, self._hmwa_user_transcript_entry(event, prepared, time.time()),
                    )
                # Tool effects are unknown after an exception.
                await self._hmwa_close_failed_turn(session_entry.session_id, PARTIAL_FAILED_TURN_NOTICE)
        except Exception:
            logger.debug("Failed to persist inbound user message after agent exception", exc_info=True)
        # Never expose raw exception types/messages to end users (info-leakage risk).
        status_hint = self._STATUS_HINTS.get(status_code, "")
        if status_code == 401:
            from agent.turn_failure_copy import relogin_command_hint

            _turn_agent = getattr(self._session_state(session_key).turn, "agent", None)
            status_hint = status_hint.format(relogin=relogin_command_hint(getattr(_turn_agent, "provider", None)))
        elif status_code == 429:
            # Plan usage limit (resets on a schedule) vs a transient rate limit
            _err_json = {}
            with suppress(Exception):
                _err_json = e.response.json().get("error", {})
            if not isinstance(_err_json, dict):
                _err_json = {}
            _resets_in = _err_json.get("resets_in_seconds")
            if _err_json.get("type") != "usage_limit_reached":
                status_hint = " You are being rate-limited. Please wait a moment and try again."
            elif _resets_in and _resets_in > 0:
                import math
                status_hint = f" Your plan's usage limit has been reached. It resets in ~{math.ceil(_resets_in / 3600)}h."
            else:
                status_hint = " Your plan's usage limit has been reached. Please wait until it resets."
        elif status_code == 400:
            status_hint = " The AI model service rejected the request."
        return self._hmwa_add_failed_turn_notice(
            f"⚠️ Something went wrong and I couldn't finish this reply.{status_hint}\n"
            "Use /retry to try again, or /new to start a fresh conversation. "
            "Technical details are in the gateway log (`hermes logs`).",
            PARTIAL_FAILED_TURN_NOTICE,
        )

    def _hmwa_discard_stale_result(self, source, _quick_key, run_generation):
        """A newer run generation superseded this turn: drop its deferred post-delivery callback."""
        from gateway.run import logger

        logger.info(
            "Discarding stale agent result for %s — generation %d is no longer current",
            _quick_key or "?", run_generation,
        )
        self._pop_post_delivery_callback(self._delivery_adapter_for(source), _quick_key, run_generation)

    @dataclasses.dataclass
    class _PreparedTurn:
        """Inputs to the agent run assembled by ``_hmwa_prepare_turn``."""

        history: Any
        context_prompt: str
        message_text: Any
        persist_user_message: Any
        persist_user_timestamp: Any
        persist_user_display_kind: Optional[str]
        persistence_session_id: Optional[str] = None
        persistence_owner: Optional[str] = None

    async def _hmwa_prepare_turn(self, event, source, session_entry, session_key, _quick_key, run_generation):
        """Everything between session resolution and the agent run: session open, task-local env,
        context prompt, sidecar notes, turn lease, transcript load + hygiene, inbound text. Returns
        ``(_PreparedTurn, env_tokens)``; a ``str`` first element is a reply to send instead of
        running (history unreadable); ``None`` drops the turn (inbound text rejected)."""
        from gateway.run import _load_gateway_config
        _was_auto_reset, _is_new_session = await self._hmwa_open_session(session_entry, session_key, source)
        context = build_session_context(source, self.config, session_entry)
        # Session context variables for tools (task-local, concurrency-safe)
        _session_env_tokens = self._set_session_env(context)
        # Self-injected turns (MessageEvent(internal=True)) persist with a DB-only display_kind so
        # UIs render timeline notices, not user bubbles; role/content untouched.
        persist_user_display_kind = display_kind_for_event(event)
        _redact_pii = False  # privacy.redact_pii, re-read per message
        with suppress(Exception):
            _redact_pii = bool((_load_gateway_config().get("privacy") or {}).get("redact_pii", False))

        # The context prompt render is pinned per session, keyed by a hash of the renderer inputs, so
        # the system prompt cannot drift turn-over-turn; a miss (thread rename, /sethome) re-renders.
        context_prompt = self._pinned_session_context_prompt(context, _redact_pii, session_key)

        # Per-turn notes ride the user message via the api_content sidecar, NOT context_prompt
        # (appending to the ephemeral system prompt forced a full agent rebuild).
        turn_sidecar_notes: List[str] = []
        if _was_auto_reset:
            await self._hmwa_deliver_auto_reset_notice(session_entry, source, turn_sidecar_notes)

        # Auto-load bound skill(s) only on NEW sessions; ongoing ones carry the content in history.
        _auto = getattr(event, "auto_skill", None)
        if _is_new_session and _auto:
            self._hmwa_auto_load_skills(event, _auto, _quick_key, session_key)

        await self._hmwa_acquire_turn_lease(_quick_key, run_generation, session_entry, _session_env_tokens)

        # A turn becomes durable recovery work only after it owns the per-session lease; marking
        # earlier would falsely recover a message that never began processing.
        await self._mark_durable_active_turn(event, session_entry.session_key)

        # An unreadable store is not an empty conversation: stop before the agent invents continuity
        # from []. Restore task-local context here (before the broad cleanup finally).
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
            history = await self._hmwa_run_session_hygiene(
                event, source, session_entry, session_key, history, _quick_key, run_generation,
            )
        except TranscriptReadError:
            self._clear_session_env(_session_env_tokens)
            return (
                "⚠️ This session's history is temporarily unavailable, so this message was not "
                "processed. Ask the operator to inspect state.db, then resend after it is healthy. "
                "Use /reset only if you intentionally want to start a new conversation."
            ), _session_env_tokens

        await self._hmwa_first_contact_notes(source, history, turn_sidecar_notes)

        # Voice channel state rides the user message ONLY when changed (in the system prompt it
        # forced a rebuild + prompt-cache re-key per message).
        _vc_note = self._voice_channel_sidecar_note(event, source, session_key)
        if _vc_note:
            turn_sidecar_notes.append(_vc_note)

        # Auto-analyze user images so the model gets a description plus the local path.
        message_text = await self._prepare_profile_scoped_inbound_message_text(
            event=event, source=source, history=history, session_key=session_key,
        )
        if message_text is None:
            return None, _session_env_tokens

        message_text, persist_user_message, persist_user_timestamp = (
            self._hmwa_apply_message_timestamp(event, message_text)
        )

        # Stage the notes (one-shot; consumed in run_sync) AFTER the early-out so an aborted turn
        # cannot leak them into the next turn.
        if turn_sidecar_notes and session_key:
            self._set_pending_turn_sidecar_notes(session_key, turn_sidecar_notes)

        # Bind this run generation to the adapter so deferred post-delivery callbacks are released
        # by the run that registered them.
        self._bind_adapter_run_generation(self._delivery_adapter_for(source), session_key, run_generation)
        # Delivery IDs are only unique in their transport namespace. Keyless turns
        # need their own identity, even when another process writes to this session.
        import uuid
        namespace = [source.platform.value, source.profile, source.scope_id,
                     source.chat_id, source.thread_id, str(event.message_id)]
        owner = (str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(namespace)))
                 if event.message_id else str(uuid.uuid4()))
        return self._PreparedTurn(
            history, context_prompt, message_text, persist_user_message, persist_user_timestamp,
            persist_user_display_kind, session_entry.session_id, owner,
        ), _session_env_tokens

    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        from gateway.run import (
            _HYGIENE_TURNHOLD_RETRY_SECONDS,
            _float_env,
            _hermes_home,
            _hygiene_compression_timeout_message,
            _hygiene_cooldown_for_failure,
            _load_gateway_config,
            _message_timestamps_enabled,
            _platform_config_key,
            _record_hygiene_cooldown,
            _reset_hygiene_failure_streak,
            _resolve_gateway_display_bool,
            _resolve_gateway_model,
            _stamp_hygiene_compression_provenance,
            _terminal_scope_cwd,
            hygiene_wait_should_extend,
            run_codex_hygiene_compaction,
        )
        import uuid

        from gateway.run import (
            HygieneTurnHoldExceeded,
            _exempt_from_empty_response_normalization,
            _is_forced_recovery_control_outcome,
            _is_gateway_hidden_reasoning_incomplete_turn,
            _is_gateway_restart_control_outcome,
            _normalize_empty_agent_response,
            _sanitize_gateway_final_response,
            _should_clear_resume_pending_after_turn,
            logger,
        )

        _msg_start_time = time.perf_counter()
        _platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        _msg_preview = (event.text or "")[:80].replace("\n", " ")
        _reply_id = getattr(event, "reply_to_message_id", None)
        _reply_txt = (getattr(event, "reply_to_text", None) or "")[:80].replace("\n", " ")
        logger.info(
            "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
            _platform_name, source.user_name or source.user_id or "unknown",
            source.chat_id or "unknown", _msg_preview, _reply_id, _reply_txt,
        )

        # Get or create session
        # Topic-mode DMs: rewrite a stale/foreign thread_id to the user's
        # last-active topic so a cross-topic Reply or stripped plain reply
        # doesn't fragment the conversation across sessions.
        recovered = await asyncio.to_thread(self._recover_telegram_topic_thread_id, source)
        if recovered is not None:
            logger.info(
                "telegram topic recovery: chat=%s user=%s %r -> %s",
                source.chat_id, source.user_id, source.thread_id, recovered,
            )
            source = dataclasses.replace(source, thread_id=recovered)
            try:
                event.source = source
            except Exception:
                pass

        event_metadata = getattr(event, "metadata", None) or {}
        expected_session_key = str(
            event_metadata.get("gateway_session_key") or ""
        ).strip()
        if expected_session_key:
            derived_session_key = self._session_key_for_source(source)
            if derived_session_key != expected_session_key:
                logger.warning(
                    "Dropping internally routed event after route recovery: "
                    "expected session=%s derived=%s",
                    expected_session_key,
                    derived_session_key,
                )
                return

        strict_session = bool(event_metadata.get("gateway_session_strict"))
        pinned_session_id = str(event_metadata.get("gateway_session_id") or "").strip()
        if strict_session:
            session_entry = await self.async_session_store.lookup_by_session_key(
                expected_session_key
            )
            if (
                session_entry is None
                or not pinned_session_id
                or session_entry.session_id != pinned_session_id
            ):
                logger.warning(
                    "Dropping internally routed event: expected session id=%s is no "
                    "longer current for key=%s",
                    pinned_session_id or "missing",
                    expected_session_key or "missing",
                )
                return
        else:
            # Internal wakes must observe reset policy without becoming user
            # activity themselves. Otherwise periodic process
            # notifications keep the stable routing key alive across every
            # daily/idle boundary.
            session_entry = await self.async_session_store.get_or_create_session(
                source,
                touch_activity=not bool(getattr(event, "internal", False)),
            )
        session_key = session_entry.session_key
        if not strict_session and pinned_session_id:
            resolved_entry = await self._resolve_async_delegation_session(
                session_entry,
                pinned_session_id,
            )
            if resolved_entry is None:
                return
            session_entry = resolved_entry
        self._cache_session_source(session_key, source)

        # Remember the last REAL user message id per session (persisted via
        # session metadata, so it survives restarts). Synthetic injections —
        # async-delegation completions, background-process notifications, loop
        # wakeups — carry no platform message_id of their own, which left their
        # turn-final replies without a Discord/Telegram reply anchor and
        # therefore without a user ping. Those injections now fall back to this
        # remembered id (see _inject_watch_notification / _loop_wakeup_watcher).
        if not getattr(event, "internal", False) and getattr(event, "message_id", None):
            try:
                await self.async_session_store.set_session_metadata(
                    session_key,
                    "_last_user_message_id",
                    str(event.message_id),
                )
            except Exception:
                logger.debug(
                    "Failed to persist last-user-message-id for %s",
                    session_key, exc_info=True,
                )
        await self._persist_resume_inbound_for_event(session_key, event)
        if await asyncio.to_thread(self._is_telegram_topic_lane, source):
            try:
                binding = (await self._session_db.get_telegram_topic_binding(
                    chat_id=str(source.chat_id),
                    thread_id=str(source.thread_id),
                    profile_name=self._telegram_topic_profile_name(source),
                )) if self._session_db else None
            except Exception:
                logger.debug("Failed to read Telegram topic binding", exc_info=True)
                binding = None
            if binding:
                bound_session_id = str(binding.get("session_id") or "")
                # Heal bindings that point at a pre-compression parent: walk
                # the compression-continuation chain forward to its tip so the
                # next message resumes the compressed child instead of
                # reloading the oversized parent transcript (#20470/#29712/
                # #33414). Returns the input unchanged when the session isn't
                # a compression parent, so this is cheap and safe.
                if bound_session_id and self._session_db is not None:
                    try:
                        canonical_session_id = await self._session_db.get_compression_tip(
                            bound_session_id,
                        )
                    except Exception:
                        logger.debug(
                            "compression-tip lookup failed for %s",
                            bound_session_id, exc_info=True,
                        )
                        canonical_session_id = bound_session_id
                    if (
                        canonical_session_id
                        and canonical_session_id != bound_session_id
                    ):
                        bound_session_id = canonical_session_id
                if bound_session_id and bound_session_id != session_entry.session_id:
                    # Route the override through SessionStore so the session_key
                    # → session_id mapping is persisted to disk and the previous
                    # lane session is ended cleanly. Mutating session_entry in
                    # place here created a split-brain state where the JSON
                    # index pointed at one id but code downstream used another.
                    switched = await self.async_session_store.switch_session(session_key, bound_session_id)
                    if switched is not None:
                        session_entry = switched
                # If the stored binding pointed at a parent, rewrite it to the
                # canonical descendant now that we've followed the chain.
                if (
                    bound_session_id
                    and bound_session_id != str(binding.get("session_id") or "")
                ):
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compression-tip-walk",
                    )
            else:
                try:
                    await asyncio.to_thread(self._record_telegram_topic_binding, source, session_entry)
                except Exception:
                    logger.debug("Failed to record Telegram topic binding", exc_info=True)
        # Capture and immediately consume was_auto_reset so it does not
        # re-fire on subsequent messages — preventing the cleanup from
        # wiping model/reasoning overrides set between turns (Closes #48031).
        _was_auto_reset = getattr(session_entry, "was_auto_reset", False)
        if _was_auto_reset:
            # Treat auto-reset as a full conversation boundary — clear every
            # conversation-scoped per-session dict in one funnel call so the
            # fresh session does not inherit the previous conversation's
            # model/reasoning overrides, a queued "/model switched" note, or
            # a stale resolved-model cache (#48031, #58403). See
            # _CONVERSATION_SCOPED_STATE.
            self._clear_conversation_scope(session_key, reason="auto_reset")
            # Evict the cached agent so the fresh session does not inherit the
            # previous conversation's context_compressor._previous_summary —
            # the cache is keyed on the stable session_key, so an auto-reset
            # otherwise reuses the old agent and leaks prior history into new
            # compaction summaries. Mirrors /reset and the compression-exhausted
            # path (#9893). Covers daily/idle/suspended auto-reset.
            self._evict_cached_agent(session_key)
            session_entry.was_auto_reset = False

        # Emit session:start for new or auto-reset sessions
        _is_new_session = (
            session_entry.created_at == session_entry.updated_at
            or _was_auto_reset
            or getattr(session_entry, "is_fresh_reset", False)
        )
        # Consume the is_fresh_reset flag immediately so it doesn't leak
        # onto subsequent messages in the same session (issue #6508).
        if getattr(session_entry, "is_fresh_reset", False):
            session_entry.is_fresh_reset = False
        if _is_new_session:
            await self.hooks.emit("session:start", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "session_key": session_key,
            })

        # Build session context
        context = build_session_context(source, self.config, session_entry)

        # Set session context variables for tools (task-local, concurrency-safe)
        _session_env_tokens = self._set_session_env(context)

        # Read privacy.redact_pii from config (re-read per message)
        _redact_pii = False
        persist_user_message = None
        persist_user_timestamp = None
        # Synthetic self-injected turns (async-delegation batch completions,
        # background watch notifications, resume wake-ups) arrive as
        # MessageEvent(internal=True). Persist their user row typed with
        # display_kind="internal_notification" so transcripts/UIs can render
        # them as timeline notices instead of user bubbles (#82888). Role and
        # content are untouched — display_kind is a DB-only sidecar stripped
        # from every provider-bound payload (see conversation_loop's
        # api_msg.pop("display_kind")).
        persist_user_display_kind = (
            "internal_notification" if getattr(event, "internal", False) else None
        )
        try:
            _pcfg = _load_gateway_config()
            _redact_pii = bool((_pcfg.get("privacy") or {}).get("redact_pii", False))
        except Exception:
            pass

        # Build the context prompt to inject.  The render is pinned per
        # session, keyed by a hash of the exact renderer inputs
        # (_ephemeral_change_key).  A key hit reuses the pinned bytes verbatim
        # so the composed system prompt cannot drift turn-over-turn; a key
        # miss (thread rename, /setnotify, redact_pii flip, ...) re-renders
        # once — the only legitimate cache busts.
        context_prompt = self._pinned_session_context_prompt(
            context, _redact_pii, session_key
        )

        # Per-turn must-deliver notes.  These used to be appended to
        # context_prompt (the ephemeral system prompt), which guaranteed a
        # turn1→turn2 system-prompt diff and a full agent rebuild.  They now
        # ride the current user message via the api_content sidecar instead
        # (staged below, consumed in run_sync → build_turn_context).
        turn_sidecar_notes: List[str] = []

        # If the previous session expired and was auto-reset, deliver a notice
        # so the agent knows this is a fresh conversation (not an intentional /reset).
        if _was_auto_reset:
            reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'idle'
            if reset_reason == "suspended":
                context_note = "[System note: The user's previous session was stopped and suspended. This is a fresh conversation with no prior context.]"
            elif reset_reason == "daily":
                context_note = "[System note: The user's session was automatically reset by the daily schedule. This is a fresh conversation with no prior context.]"
            elif reset_reason == "resume_pending_expired":
                context_note = "[System note: The previous gateway session could not be recovered after a restart (API recovery timed out). This is a fresh conversation — use /resume to restore history if needed.]"
            else:
                context_note = "[System note: The user's previous session expired due to inactivity. This is a fresh conversation with no prior context.]"
            # Slack/Discord channels/threads are long-lived: point the agent at
            # the specific prior same-channel session so it recalls that context
            # via session_search instead of an unrelated recent session.  Returns
            # None (appends nothing) for other platforms or when there's no prior
            # activity to recall.  Deterministic — no extra API/DB calls (#36220).
            try:
                continuity_note = build_channel_continuity_note(session_entry, source)
            except Exception:
                continuity_note = None
            if continuity_note:
                context_note = context_note + "\n\n" + continuity_note
            turn_sidecar_notes.append(context_note)

            # Send a user-facing notification explaining the reset, unless:
            # - notifications are disabled in config
            # - the platform is excluded (e.g. api_server, webhook)
            # - the expired session had no activity (nothing was cleared)
            try:
                policy = self.session_store.config.get_reset_policy(
                    platform=source.platform,
                    session_type=getattr(source, 'chat_type', 'dm'),
                )
                platform_name = source.platform.value if source.platform else ""
                had_activity = getattr(session_entry, 'reset_had_activity', False)
                # Suspended and restart-recovery-expired sessions always notify
                # regardless of policy.notify — the user had an active session
                # that was silently replaced, so they need to know they can
                # /resume it.  Idle/daily resets respect the policy flag.
                should_notify = reset_reason in {"suspended", "resume_pending_expired"} or (
                    policy.notify
                    and had_activity
                    and platform_name not in policy.notify_exclude_platforms
                )
                if should_notify:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        if reset_reason == "suspended":
                            reason_text = "previous session was stopped or interrupted"
                        elif reset_reason == "resume_pending_expired":
                            reason_text = "gateway restart recovery timed out"
                        elif reset_reason == "daily":
                            reason_text = f"daily schedule at {policy.at_hour}:00"
                        else:
                            hours = policy.idle_minutes // 60
                            mins = policy.idle_minutes % 60
                            duration = f"{hours}h" if not mins else f"{hours}h {mins}m" if hours else f"{mins}m"
                            reason_text = f"inactive for {duration}"
                        notice = (
                            f"◐ Session automatically reset ({reason_text}). "
                            f"Conversation history cleared.\n"
                            f"Use /resume to browse and restore a previous session.\n"
                            f"Adjust reset timing in config.yaml under session_reset."
                        )
                        try:
                            session_info = await asyncio.to_thread(
                                self._reset_notice_session_info, source
                            )
                            if session_info:
                                notice = f"{notice}\n\n{session_info}"
                        except Exception:
                            pass
                        await adapter.send(
                            source.chat_id, notice,
                            metadata=self._thread_metadata_for_source(source),
                        )
            except Exception as e:
                logger.debug("Auto-reset notification failed (non-fatal): %s", e)

            # was_auto_reset is already consumed in the cleanup block above
            # (single source of truth); only the reset reason needs clearing here.
            session_entry.auto_reset_reason = None

        # Auto-load skill(s) for topic/channel bindings (Telegram DM Topics,
        # Discord channel_skill_bindings).  Supports a single name or ordered list.
        # Only inject on NEW sessions — ongoing conversations already have the
        # skill content in their conversation history from the first message.
        _auto = getattr(event, "auto_skill", None)
        if _is_new_session and _auto:
            _skill_names = [_auto] if isinstance(_auto, str) else list(_auto)
            try:
                from agent.skill_commands import _load_skill_payload, _build_skill_message
                _combined_parts: list[str] = []
                _loaded_names: list[str] = []
                for _sname in _skill_names:
                    _loaded = _load_skill_payload(_sname, task_id=_quick_key)
                    if _loaded:
                        _loaded_skill, _skill_dir, _display_name = _loaded
                        _note = (
                            f'[IMPORTANT: The "{_display_name}" skill is auto-loaded. '
                            f"Follow its instructions for this session.]"
                        )
                        _part = _build_skill_message(_loaded_skill, _skill_dir, _note)
                        if _part:
                            _combined_parts.append(_part)
                            _loaded_names.append(_sname)
                    else:
                        logger.warning("[Gateway] Auto-skill '%s' not found", _sname)
                if _combined_parts:
                    # Append the user's original text after all skill payloads
                    _combined_parts.append(event.text)
                    event.text = "\n\n".join(_combined_parts)
                    logger.info(
                        "[Gateway] Auto-loaded skill(s) %s for session %s",
                        _loaded_names, session_key,
                    )
            except Exception as e:
                logger.warning("[Gateway] Failed to auto-load skill(s) %s: %s", _skill_names, e)

        # ── Turn lease (#64934) ────────────────────────────────────────
        # Session resolution is FINAL here (get_or_create → async-delegation
        # pinning → topic tip-walk switch_session are all above). Serialize
        # the [load history → run → flush] region per resolved SESSION_ID:
        # when a second routing key is mapped to this same session_id, its
        # turn waits here for the previous turn's flush instead of loading a
        # stale history base and interleaving transcript writes. Same-key
        # messages never reach this point mid-turn (adapter + runner guards
        # hold them), so the lock is uncontended outside the alias-key route.
        # Fail-closed on timeout: never enter the transcript region without a
        # lease. Outer dispatch returns a bounded rejection/resend notice rather
        # than recreating the exact concurrent-turn corruption this lease exists
        # to prevent. Released in _handle_message's finally via
        # _release_turn_lease — granted per (routing key, run generation) so a
        # stale unwind can't release a newer turn's lease.
        _lease_registry = getattr(self, "_turn_leases", None)
        if _lease_registry is not None:
            try:
                _lease_token = await _lease_registry.acquire(
                    session_entry.session_id,
                    owner_key=_quick_key,
                    generation=run_generation,
                    timeout=_float_env(
                        "HERMES_TURN_LEASE_TIMEOUT", DEFAULT_LEASE_WAIT
                    ),
                )
            except TurnLeaseTimeoutError:
                # The broad session-context cleanup finally starts later in this
                # method. Restore the tokens here before propagating the rejection
                # to outer dispatch, or this early exit leaks task-local identity.
                self._clear_session_env(_session_env_tokens)
                raise
            if _lease_token is not None:
                _lease_state = self._session_state(_quick_key).turn
                _lease_state.lease_token = _lease_token
                _lease_state.lease_generation = run_generation

        # A turn only becomes durable recovery work after it owns (or has
        # explicitly degraded past) the per-session lease.  Marking before the
        # await above would falsely recover an alias-routed message that never
        # began processing if the gateway died while it was still waiting.
        await self._mark_durable_active_turn(event, session_entry.session_key)

        # Load conversation history from transcript. An unreadable canonical
        # store is not an empty conversation: stop before the agent can invent
        # continuity from a plausible-looking []. This return happens before
        # the broad cleanup finally below, so restore task-local context here;
        # the outer dispatch still clears the durable marker and turn lease.
        try:
            history = await self.async_session_store.load_transcript(
                session_entry.session_id
            )
        except TranscriptReadError:
            self._clear_session_env(_session_env_tokens)
            return (
                "⚠️ This session's history is temporarily unavailable, so "
                "this message was not processed. Ask the operator to inspect "
                "state.db, then resend after it is healthy. Use /reset only "
                "if you intentionally want to start a new conversation."
            )

        # -----------------------------------------------------------------
        # Session hygiene: auto-compress pathologically large transcripts
        #
        # Long-lived gateway sessions can accumulate enough history that
        # every new message rehydrates an oversized transcript, causing
        # repeated truncation/context failures.  Detect this early and
        # compress proactively — before the agent even starts.  (#628)
        #
        # Token source priority:
        # 1. Actual API-reported prompt_tokens from the last turn
        #    (stored in session_entry.last_prompt_tokens)
        # 2. Rough char-based estimate (str(msg)//4). Overestimates
        #    by 30-50% on code/JSON-heavy sessions, but that just
        #    means hygiene fires a bit early — safe and harmless.
        # -----------------------------------------------------------------
        if history and len(history) >= 4:
            from agent.model_metadata import (
                estimate_messages_tokens_rough,
                get_model_context_length_async,
            )

            # Read model + compression config from config.yaml.
            # NOTE: hygiene threshold is intentionally HIGHER than the agent's
            # own compressor (0.85 vs 0.50).  Hygiene is a safety net for
            # sessions that grew too large between turns — it fires pre-agent
            # to prevent API failures.  The agent's own compressor handles
            # normal context management during its tool loop with accurate
            # real token counts.  Having hygiene at 0.50 caused premature
            # compression on every turn in long gateway sessions.
            _hyg_model = "anthropic/claude-sonnet-4.6"
            _hyg_threshold_pct = 0.85
            _hyg_compression_enabled = True
            _hyg_hard_msg_limit = 5000
            _hyg_timeout_seconds = 30.0
            _hyg_total_ceiling_seconds = 600.0
            # Max wall-clock the user's TURN is held waiting on hygiene
            # compression before the gateway stops waiting and proceeds on the
            # uncompressed transcript (#TKT-0029). The compressor keeps running
            # detached; its commit is fenced (revoke_commit_admission) so a
            # stale result can never clobber turns appended after the wait was
            # abandoned. Capped well below typical transport idle-timeouts
            # (Telegram ~30s) so the wire never goes silent long enough to sever.
            _hyg_max_turn_hold_seconds = 10.0
            _hyg_failure_cooldown_seconds = 300.0
            _hyg_config_context_length = None
            _hyg_provider = None
            _hyg_base_url = None
            _hyg_api_key = None
            _hyg_configured_model = None
            _hyg_configured_provider = None
            _hyg_configured_base_url = None
            _hyg_data = {}
            try:
                _hyg_data = _load_gateway_config()
                if _hyg_data:
                    # Resolve model name (same logic as run_sync)
                    _model_cfg = _hyg_data.get("model", {})
                    if isinstance(_model_cfg, str):
                        _hyg_model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _hyg_model = _model_cfg.get("default") or _model_cfg.get("model") or _hyg_model
                        # Read explicit context_length override from model config
                        # (same as run_agent.py lines 995-1005)
                        _raw_ctx = _model_cfg.get("context_length")
                        if _raw_ctx is not None:
                            try:
                                _hyg_config_context_length = int(_raw_ctx)
                            except (TypeError, ValueError):
                                pass
                        # Read provider for accurate context detection
                        _hyg_provider = _model_cfg.get("provider") or None
                        _hyg_base_url = _model_cfg.get("base_url") or None

                    # Read compression settings — only use enabled flag.
                    # The threshold is intentionally separate from the agent's
                    # compression.threshold (hygiene runs higher).
                    _comp_cfg = _hyg_data.get("compression", {})
                    if isinstance(_comp_cfg, dict):
                        _hyg_compression_enabled = str(
                            _comp_cfg.get("enabled", True)
                        ).lower() in {"true", "1", "yes"}
                        _raw_hard_limit = _comp_cfg.get("hygiene_hard_message_limit")
                        if _raw_hard_limit is not None:
                            try:
                                _parsed = int(_raw_hard_limit)
                                if _parsed > 0:
                                    _hyg_hard_msg_limit = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_timeout = _comp_cfg.get("hygiene_timeout_seconds")
                        if _raw_timeout is not None:
                            try:
                                _parsed = float(_raw_timeout)
                                if _parsed > 0:
                                    _hyg_timeout_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_ceiling = _comp_cfg.get("hygiene_total_ceiling_seconds")
                        if _raw_ceiling is not None:
                            try:
                                _parsed = float(_raw_ceiling)
                                if _parsed > 0:
                                    _hyg_total_ceiling_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        # The ceiling can never be tighter than one idle
                        # window, or the extension loop would be dead code.
                        _hyg_total_ceiling_seconds = max(
                            _hyg_total_ceiling_seconds, _hyg_timeout_seconds,
                        )
                        _raw_turn_hold = _comp_cfg.get("hygiene_max_turn_hold_seconds")
                        if _raw_turn_hold is not None:
                            try:
                                _parsed = float(_raw_turn_hold)
                                if _parsed > 0:
                                    _hyg_max_turn_hold_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_cooldown = _comp_cfg.get("hygiene_failure_cooldown_seconds")
                        if _raw_cooldown is not None:
                            try:
                                _parsed = float(_raw_cooldown)
                                if _parsed >= 0:
                                    _hyg_failure_cooldown_seconds = _parsed
                            except (TypeError, ValueError):
                                pass

                _hyg_configured_model = _hyg_model
                _hyg_configured_provider = _hyg_provider
                _hyg_configured_base_url = _hyg_base_url

                try:
                    _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                        source=source,
                        session_key=session_key,
                        user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                    )
                    _hyg_provider = _hyg_runtime.get("provider") or _hyg_provider
                    _hyg_base_url = _hyg_runtime.get("base_url") or _hyg_base_url
                    _hyg_api_key = _hyg_runtime.get("api_key") or _hyg_api_key
                except Exception:
                    pass

                if _hyg_config_context_length is not None:
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async

                        if await should_clear_context_pin_async(
                            _hyg_configured_model,
                            _hyg_model,
                            _hyg_configured_base_url,
                            _hyg_base_url,
                            _hyg_configured_provider,
                            _hyg_provider,
                        ):
                            _hyg_config_context_length = None
                    except Exception:
                        _hyg_config_context_length = None

                # Check custom_providers per-model context_length
                # (same fallback as run_agent.py lines 1171-1189).
                # Must run after runtime resolution so _hyg_base_url is set.
                if _hyg_config_context_length is None and _hyg_base_url:
                    try:
                        try:
                            from hermes_cli.config import (
                                get_compatible_custom_providers as _gw_gcp,
                                get_custom_provider_context_length as _gw_gccl,
                            )
                            _hyg_custom_providers = _gw_gcp(_hyg_data)
                        except Exception:
                            _hyg_custom_providers = _hyg_data.get("custom_providers")
                            if not isinstance(_hyg_custom_providers, list):
                                _hyg_custom_providers = []
                        _hyg_custom_ctx = _gw_gccl(
                            model=_hyg_model,
                            base_url=_hyg_base_url,
                            custom_providers=_hyg_custom_providers,
                        )
                        if _hyg_custom_ctx:
                            _hyg_config_context_length = int(_hyg_custom_ctx)
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass

            if _hyg_compression_enabled:
                _hyg_context_length = await get_model_context_length_async(
                    _hyg_model,
                    base_url=_hyg_base_url or "",
                    api_key=_hyg_api_key or "",
                    config_context_length=_hyg_config_context_length,
                    provider=_hyg_provider or "",
                )
                _compress_token_threshold = int(
                    _hyg_context_length * _hyg_threshold_pct
                )
                _warn_token_threshold = int(_hyg_context_length * 0.95)

                _msg_count = len(history)

                # Prefer actual API-reported tokens from the last turn
                # (stored in session entry) over the rough char-based estimate.
                _stored_tokens = session_entry.last_prompt_tokens
                if _stored_tokens > 0:
                    _approx_tokens = _stored_tokens
                    _token_source = "actual"
                else:
                    _approx_tokens = estimate_messages_tokens_rough(history)
                    _token_source = "estimated"
                    # Note: rough estimates overestimate by 30-50% for code/JSON-heavy
                    # sessions, but that just means hygiene fires a bit early — which
                    # is safe and harmless.  The 85% threshold already provides ample
                    # headroom (agent's own compressor runs at 50%).  A previous 1.4x
                    # multiplier tried to compensate by inflating the threshold, but
                    # 85% * 1.4 = 119% of context — which exceeds the model's limit
                    # and prevented hygiene from ever firing for ~200K models (GLM-5).

                # Hard safety valve: force compression if message count is
                # extreme, regardless of token estimates.  This breaks the
                # death spiral where API disconnects prevent token data
                # collection, which prevents compression, which causes more
                # disconnects.  5000 messages is far above any normal session
                # but catches truly runaway growth before it becomes
                # unrecoverable.  Set well clear of legitimate large-context
                # (1M+) sessions doing thousands of short turns — those
                # compress on the token threshold, not this count-based floor.
                # Threshold is configurable via
                # compression.hygiene_hard_message_limit.
                # (#2153)
                _HARD_MSG_LIMIT = _hyg_hard_msg_limit
                _needs_compress = (
                    _approx_tokens >= _compress_token_threshold
                    or _msg_count >= _HARD_MSG_LIMIT
                )

                if _needs_compress:
                    # Use the persistent DB-backed cooldown (same as the
                    # in-conversation compression path in context_compressor.py)
                    # so the cooldown survives gateway restarts. The in-memory
                    # dict was reset on every restart, re-triggering the same
                    # failing compression and wedging session storage (#74136).
                    _session_db = getattr(self, "_session_db", None)
                    if _session_db is not None:
                        _session_db = getattr(_session_db, "_db", _session_db)
                        _getter = getattr(_session_db, "get_compression_failure_cooldown", None)
                        if _getter is not None:
                            try:
                                _cooldown_state = _getter(session_entry.session_id)
                            except Exception:
                                _cooldown_state = None
                            if _cooldown_state and _cooldown_state.get("remaining_seconds", 0) > 0:
                                logger.info(
                                    "Session hygiene: skipping compression for %s; "
                                    "previous failure cooldown active for %.1fs",
                                    session_entry.session_id,
                                    _cooldown_state["remaining_seconds"],
                                )
                                _needs_compress = False

                if _needs_compress and await self._session_has_compression_in_flight(
                    session_key
                ):
                    # A prior hygiene/agent compression still holds the
                    # durable lock (typically a shielded worker left behind
                    # by /stop or /restart). Starting another attempt waits
                    # up to the 600s ceiling behind a commit that the fence
                    # will refuse, while inbound messages demote to queue
                    # (#96953).
                    logger.info(
                        "Session hygiene: skipping compression for %s; "
                        "another compression is already in flight",
                        session_entry.session_id,
                    )
                    _needs_compress = False

                if _needs_compress:
                    logger.info(
                        "Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing "
                        "(threshold: %s%% of %s = %s tokens)",
                        _msg_count, f"{_approx_tokens:,}", _token_source,
                        int(_hyg_threshold_pct * 100),
                        f"{_hyg_context_length:,}",
                        f"{_compress_token_threshold:,}",
                    )

                    _hyg_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

                    try:
                        from agent.conversation_compression import CompressionCommitFence
                        from run_agent import AIAgent
                        from gateway.run import (
                            _GATEWAY_HYGIENE_PLATFORM,
                            _seed_hygiene_system_prompt,
                            hygiene_compaction_recovered,
                        )

                        _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                            source=source,
                            session_key=session_key,
                            user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                        )
                        _hyg_api_mode = str(
                            _hyg_runtime.get("api_mode") or ""
                        ).lower()
                        if _hyg_api_mode == "codex_app_server":
                            # codex app-server runtime: the model's real
                            # context is the app-server's server-side thread,
                            # not the transcript mirror. The detached-agent
                            # block below could only rewrite the mirror (a
                            # guaranteed no-op for the thread) and its
                            # finally-clause eviction would destroy the live
                            # thread — the next turn then starts blank
                            # (#73503). Route to the live cached agent's
                            # thread/compact/start instead and KEEP it cached.
                            _hyg_codex_auto = "native"
                            _hyg_comp_cfg = (
                                _hyg_data.get("compression")
                                if isinstance(_hyg_data, dict)
                                else None
                            )
                            if isinstance(_hyg_comp_cfg, dict):
                                _hyg_codex_auto = str(
                                    _hyg_comp_cfg.get(
                                        "codex_app_server_auto", "native"
                                    )
                                    or "native"
                                )
                            _hyg_codex_outcome = await run_codex_hygiene_compaction(
                                self,
                                session_key,
                                session_entry.session_id,
                                auto_mode=_hyg_codex_auto,
                                history=history,
                                approx_tokens=_approx_tokens,
                                timeout_seconds=_hyg_total_ceiling_seconds,
                                failure_cooldown_seconds=_hyg_failure_cooldown_seconds,
                            )
                            logger.info(
                                "Session hygiene (codex app-server): %s "
                                "(session=%s, mode=%s, ~%s tokens)",
                                _hyg_codex_outcome,
                                session_entry.session_id,
                                _hyg_codex_auto,
                                f"{_approx_tokens:,}",
                            )
                        elif _hyg_runtime.get("api_key"):
                            # Pass the FULL transcript (tool results included).
                            # Filtering to user/assistant-only starved the
                            # compressor: tool results are usually the bulk of
                            # the context, _prune_old_tool_results never saw
                            # them, and short filtered histories tripped the
                            # protect-first/last early-return so nothing was
                            # compressed at all (#3854). The agent loop passes
                            # its full message list to _compress_context — the
                            # gateway now matches.
                            _hyg_msgs = [
                                m for m in history
                                if m.get("role") in {"user", "assistant", "tool"}
                            ]

                            if len(_hyg_msgs) >= 4:
                                try:
                                    _hyg_session_row = await self._session_db.get_session(
                                        session_entry.session_id
                                    )
                                except Exception as exc:
                                    _hyg_session_row = None
                                    logger.warning(
                                        "Session hygiene could not restore the system "
                                        "prompt for session %s: %s. Preserving an empty "
                                        "prompt so the live turn rebuilds it with its "
                                        "configured providers.",
                                        session_entry.session_id,
                                        exc,
                                        exc_info=True,
                                    )
                                _hyg_session_db = getattr(self._session_db, "_db", self._session_db)
                                # Hygiene performs the same lossy rewrite as
                                # normal compression. When the operator enabled
                                # compression.checkpoint_required, the memory
                                # provider must be loaded so the required
                                # checkpoint is created before any transcript
                                # mutation; otherwise keep the historical fast
                                # path (no provider init, no best-effort hook)
                                # for hygiene.
                                from hermes_cli.config import load_config as _load_cfg
                                from utils import is_truthy_value as _is_truthy

                                _hyg_checkpoint_required = _is_truthy(
                                    ((_load_cfg() or {}).get("compression") or {}).get(
                                        "checkpoint_required"
                                    ),
                                    default=False,
                                )
                                _hyg_agent = AIAgent(
                                    **_hyg_runtime,
                                    model=_hyg_model,
                                    max_iterations=4,
                                    quiet_mode=True,
                                    skip_memory=not _hyg_checkpoint_required,
                                    enabled_toolsets=["memory"],
                                    session_id=session_entry.session_id,
                                    session_db=_hyg_session_db,
                                )
                                _seed_hygiene_system_prompt(
                                    _hyg_agent,
                                    _hyg_session_row,
                                )
                                # If compression must rebuild instead of retaining
                                # the cached prompt, make the persisted result
                                # deliberately stale for every real gateway surface.
                                _hyg_agent.platform = _GATEWAY_HYGIENE_PLATFORM
                                _hyg_cleanup_deferred = False
                                try:
                                    # Gateway hygiene runs before the user turn
                                    # starts and already owns the session binding.
                                    # Prefer in-place compaction here: it archives
                                    # old rows under the same session id instead of
                                    # minting a continuation child that then has to
                                    # be published back to SessionStore/topic
                                    # bindings.  If no SessionDB is available,
                                    # compress_context leaves this flag false and
                                    # the guard below preserves the transcript.
                                    _hyg_agent.compression_in_place = True
                                    _bind_hyg_state = getattr(
                                        getattr(_hyg_agent, "context_compressor", None),
                                        "bind_session_state",
                                        None,
                                    )
                                    if callable(_bind_hyg_state):
                                        _bind_hyg_state(
                                            _hyg_session_db,
                                            session_entry.session_id,
                                        )
                                    # It must never finalize on close() — close()
                                    # would end the live gateway session row.
                                    _hyg_agent._end_session_on_close = False
                                    _hyg_agent._print_fn = lambda *a, **kw: None

                                    loop = asyncio.get_running_loop()
                                    _hyg_commit_fence = CompressionCommitFence(
                                        total_ceiling_seconds=_hyg_total_ceiling_seconds
                                    )
                                    # Default executor (NOT self._get_executor):
                                    # a fence-cancelled hung summary must never
                                    # occupy one of the gateway's agent-work
                                    # slots. But it MUST run inside the caller's
                                    # contextvars: under multiplex_profiles the
                                    # profile secret scope / HERMES_HOME override
                                    # live in ContextVars, and a bare
                                    # run_in_executor worker starts with an empty
                                    # Context — the summary model's
                                    # get_secret(<PROVIDER>_API_KEY) then fails
                                    # closed (UnscopedSecretError) and every
                                    # hygiene compaction silently degrades to a
                                    # lossy truncation (#100849 bundle).
                                    _hyg_future = loop.run_in_executor(
                                        None,
                                        copy_context().run,
                                        # task_id is the session row id: the live turn
                                        # scopes skill_view/read_file dedup to it, and the
                                        # boundary reset must hit the same bucket or a
                                        # post-compaction re-read stubs (#98206).
                                        lambda: _hyg_agent._compress_context(
                                            _hyg_msgs, "",
                                            approx_tokens=_approx_tokens,
                                            commit_fence=_hyg_commit_fence,
                                            task_id=session_entry.session_id or "default",
                                        ),
                                    )
                                    # Discord episode card (presentation-only,
                                    # mirrors the in-agent rail): one start
                                    # line the moment the summary work begins,
                                    # edited in place through the lifecycle
                                    # below. Non-rail adapters are byte-identical
                                    # to before (no card, no event). The token
                                    # keys every later edit to THIS attempt so a
                                    # superseded attempt's late edit can never
                                    # clobber its successor's episode. The card
                                    # lifecycle itself lives in
                                    # gateway/run_hygiene_compression.py.
                                    _hyg_episode_token = uuid.uuid4().hex
                                    await self._hygiene_episode_emit_start(
                                        source=source,
                                        session_key=session_key,
                                        metadata=_hyg_meta,
                                        attempt_token=_hyg_episode_token,
                                    )
                                    try:
                                        # Progress-aware wait: the timeout is an
                                        # INACTIVITY budget, not a total one. The
                                        # compression worker streams its summary
                                        # call and ticks the fence per token
                                        # (CompressionCommitFence.touch_progress),
                                        # so a slow reasoning model that is still
                                        # generating keeps extending the deadline;
                                        # only a genuinely silent worker times out.
                                        # A hard ceiling bounds the total wait so
                                        # a degenerate trickle stream can't hold
                                        # the turn forever.
                                        _hyg_wait_started = time.monotonic()
                                        while True:
                                            if _hyg_commit_fence.is_cancelled:
                                                raise asyncio.TimeoutError
                                            # #76354 S3: charge the idle budget
                                            # from the LAST PROGRESS event, not
                                            # from the start of this wait slice —
                                            # otherwise silence can approach 2x
                                            # the configured timeout.
                                            _hyg_waited = (
                                                time.monotonic() - _hyg_wait_started
                                            )
                                            _slice = min(
                                                max(
                                                    _hyg_timeout_seconds
                                                    - _hyg_commit_fence.seconds_since_progress(),
                                                    0.005,
                                                ),
                                                max(
                                                    _hyg_total_ceiling_seconds
                                                    - _hyg_waited,
                                                    0.005,
                                                ),
                                            )
                                            # Bounded turn-hold (#TKT-0029): cap
                                            # this slice at the remaining
                                            # turn-hold budget so the wait is
                                            # re-evaluated against
                                            # _hyg_max_turn_hold_seconds at
                                            # least that often — otherwise a
                                            # continuously-streaming worker
                                            # (which keeps the inactivity slice
                                            # large) would hold the turn until
                                            # the total ceiling before the
                                            # budget check ever runs.
                                            _turn_hold_remaining = (
                                                _hyg_max_turn_hold_seconds
                                                - (time.monotonic() - _hyg_wait_started)
                                            )
                                            if _turn_hold_remaining <= 0:
                                                # Budget already exhausted —
                                                # force an immediate timeout so
                                                # the abandonment path below runs.
                                                _slice = 0.005
                                            else:
                                                _slice = min(
                                                    _slice,
                                                    max(_turn_hold_remaining, 0.005),
                                                )
                                            # Re-check the fence on a short poll so a
                                            # /stop or /restart cancel is not stuck
                                            # behind a full idle window (#96953).
                                            _idle_left = max(
                                                _hyg_timeout_seconds
                                                - _hyg_commit_fence.seconds_since_progress(),
                                                0.005,
                                            )
                                            _slice = min(_slice, 0.25)
                                            try:
                                                _compressed, _ = await asyncio.wait_for(
                                                    asyncio.shield(_hyg_future),
                                                    timeout=_slice,
                                                )
                                                break
                                            except asyncio.TimeoutError:
                                                if _hyg_commit_fence.is_cancelled:
                                                    raise
                                                _hyg_waited = time.monotonic() - _hyg_wait_started
                                                _idle = _hyg_commit_fence.seconds_since_progress()
                                                # Bounded turn-hold (#TKT-0029):
                                                # never hold the user's TURN
                                                # longer than
                                                # _hyg_max_turn_hold_seconds,
                                                # even if the summary model is
                                                # still streaming. Past the
                                                # budget we stop waiting and
                                                # fall through to the timeout
                                                # path below, which revokes
                                                # commit admission and proceeds
                                                # on the uncompressed
                                                # transcript — the wire never
                                                # stays silent long enough to
                                                # trip a transport idle-timeout.
                                                if (
                                                    _hyg_waited
                                                    >= _hyg_max_turn_hold_seconds
                                                ):
                                                    logger.info(
                                                        "Session hygiene compression for "
                                                        "session %s exceeded the turn-hold "
                                                        "budget (%.1fs >= %.1fs) — "
                                                        "abandoning inline wait, proceeding "
                                                        "without compression this turn",
                                                        session_entry.session_id,
                                                        _hyg_waited,
                                                        _hyg_max_turn_hold_seconds,
                                                    )
                                                    raise HygieneTurnHoldExceeded(
                                                        f"turn-hold budget {_hyg_max_turn_hold_seconds:.1f}s "
                                                        f"elapsed after {_hyg_waited:.1f}s"
                                                    )
                                                if hygiene_wait_should_extend(
                                                        idle=_idle,
                                                        timeout=_hyg_timeout_seconds,
                                                        waited=_hyg_waited,
                                                        ceiling=_hyg_total_ceiling_seconds,
                                                        fence_cancelled=_hyg_commit_fence.is_cancelled,
                                                ):
                                                    if _slice >= _idle_left - 1e-9:
                                                        logger.info(
                                                            "Session hygiene compression for "
                                                            "session %s still streaming after "
                                                            "%.0fs (last progress %.1fs ago) — "
                                                            "extending wait (ceiling %.0fs)",
                                                            session_entry.session_id,
                                                            _hyg_waited, _idle,
                                                            _hyg_total_ceiling_seconds,
                                                        )
                                                    continue
                                                raise
                                    except HygieneTurnHoldExceeded:
                                        # Turn-hold expiry is an availability boundary,
                                        # not a failure. The compressor is healthy and
                                        # still streaming; we simply cannot hold the
                                        # current user turn any longer. Share the safe
                                        # mechanics (fence, release, defer, proceed
                                        # uncompressed) but with distinct provenance,
                                        # user message, and NO failure-cooldown
                                        # increment.
                                        #
                                        # #97963: decouple the TURN from the
                                        # COMPRESSION. When the worker's commit is
                                        # watermark-fenced (it captured the session's
                                        # active-row watermark at compression start,
                                        # so rows appended after that point — this
                                        # released turn included — survive its late
                                        # commit verbatim as cloned concurrent tail),
                                        # the already-running attempt KEEPS its commit
                                        # admission: the user's turn proceeds on the
                                        # uncompressed transcript NOW, and the summary
                                        # is adopted when the detached worker reaches
                                        # its own watermark-fenced commit transaction
                                        # (archive_and_compact / the rotation publish
                                        # path — the next safe boundary). Before this,
                                        # the fence was ALWAYS cancelled here, burning
                                        # the full summary attempt — for a thinking
                                        # summary model whose reasoning prefix alone
                                        # exceeds the 10s hold, that made hygiene
                                        # auto-compression fail 100% of the time while
                                        # paying the summary model per turn. The turn
                                        # itself is still released at the same budget:
                                        # only the fate of the detached worker's
                                        # RESULT changes. If the commit is NOT
                                        # watermark-fenced (no session_db, watermark
                                        # capture failed, legacy lock API), a late
                                        # commit could clobber newer turns, so cancel
                                        # exactly as before — never worse than the
                                        # status quo.
                                        _hyg_keep_admission = bool(
                                            getattr(
                                                _hyg_commit_fence,
                                                "commit_watermark_fenced",
                                                False,
                                            )
                                        ) and not _hyg_commit_fence.is_cancelled
                                        if _hyg_keep_admission:
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene turn-hold",
                                            )
                                            _hyg_cleanup_deferred = True
                                            # NO retry-after here (#97963 (b)): the
                                            # attempt is still running toward a real
                                            # commit, and arming the flat 60s
                                            # retry-after would ALSO block the
                                            # agent-side preflight compressor from a
                                            # fresh chance ("Skipping preflight
                                            # compression: same-session cooldown
                                            # active"). Re-attempt spacing is covered
                                            # by the durable compression lock instead:
                                            # the next turn's hygiene pre-check skips
                                            # while this worker's lease is held
                                            # (_session_has_compression_in_flight).
                                            # The flat retry-after is recorded by the
                                            # done-callback below ONLY if the worker
                                            # ends without committing anything.
                                            # The adoption/did-not-commit
                                            # done-callback (including its
                                            # episode-card terminals) lives in
                                            # gateway/run_hygiene_compression.py.
                                            _hyg_future.add_done_callback(
                                                self._hygiene_deferred_adoption_callback(
                                                    session_id=session_entry.session_id,
                                                    session_key=session_key,
                                                    agent=_hyg_agent,
                                                    source=source,
                                                    metadata=_hyg_meta,
                                                    loop=loop,
                                                    attempt_token=_hyg_episode_token,
                                                    before_messages=_msg_count,
                                                    before_tokens=_approx_tokens,
                                                )
                                            )
                                            from agent.session_activity import (
                                                ActivityProvenance,
                                            )
                                            _stamp_hygiene_compression_provenance(
                                                _hyg_agent,
                                                "session hygiene compression turn-hold",
                                                ActivityProvenance.AGENT_COMPRESSION_TURNHOLD,
                                                "hygiene compression turn-hold "
                                                "activity stamp failed",
                                            )
                                            logger.info(
                                                "Session hygiene compression for session %s "
                                                "exceeded turn-hold budget (%.1fs); "
                                                "proceeding without compression this turn — "
                                                "the watermark-fenced worker keeps its "
                                                "commit admission and the summary will be "
                                                "adopted when it finishes",
                                                session_entry.session_id,
                                                time.monotonic() - _hyg_wait_started,
                                            )
                                            # Episode card: ONE non-terminal edit
                                            # to the deferred ("still running in
                                            # the background") state — the
                                            # adoption or did-not-commit
                                            # boundary later replaces it with
                                            # the real terminal. On Discord this
                                            # replaces the plain deferral
                                            # notice; everywhere else the
                                            # notice posts exactly as before.
                                            await self._hygiene_episode_emit_deferred_or_notice(
                                                source=source,
                                                session_key=session_key,
                                                metadata=_hyg_meta,
                                                attempt_token=_hyg_episode_token,
                                            )
                                            raise
                                        _cancelled = None
                                        while _cancelled is None:
                                            if _hyg_commit_fence.commit_in_flight:
                                                _cancelled = False
                                                break
                                            _cancelled = (
                                                _hyg_commit_fence.try_cancel_before_commit()
                                            )
                                            if _cancelled is None:
                                                await asyncio.sleep(0.025)
                                        if not _cancelled:
                                            # NOTE: bounded overshoot by design.
                                            # The turn can be held past
                                            # _hyg_max_turn_hold_seconds by up to the
                                            # commit duration (summary apply + storage
                                            # write). Aborting mid-commit would corrupt
                                            # the message-store transaction — the
                                            # overshoot is the cheaper failure mode.
                                            # Do NOT "fix" this into a mid-commit
                                            # cancellation.
                                            _compressed, _ = await _hyg_future
                                        else:
                                            _hyg_commit_fence.release_cancelled_compression_lock()
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene turn-hold",
                                            )
                                            _hyg_cleanup_deferred = True
                                            # Short, NON-escalating retry-after. Without
                                            # it, every subsequent turn re-spawns a fresh
                                            # compressor, holds it for the turn-hold
                                            # budget, and cancels it again — a per-turn
                                            # summary-model token burn that never commits
                                            # under sustained traffic. This is deliberately
                                            # NOT _hygiene_cooldown_for_failure: the
                                            # compressor is healthy, so the failure streak
                                            # must not advance (behavior witness below);
                                            # only the flat retry spacing is recorded.
                                            _record_hygiene_cooldown(
                                                self, session_entry.session_id,
                                                _HYGIENE_TURNHOLD_RETRY_SECONDS,
                                                "hygiene compression deferred: "
                                                "turn-hold budget expired while the "
                                                "summary was still streaming",
                                            )
                                            from agent.session_activity import (
                                                ActivityProvenance,
                                            )
                                            _stamp_hygiene_compression_provenance(
                                                _hyg_agent,
                                                "session hygiene compression turn-hold",
                                                ActivityProvenance.AGENT_COMPRESSION_TURNHOLD,
                                                "hygiene compression turn-hold "
                                                "activity stamp failed",
                                            )
                                            logger.info(
                                                "Session hygiene compression for session %s "
                                                "exceeded turn-hold budget (%.1fs); "
                                                "proceeding without compression this turn",
                                                session_entry.session_id,
                                                time.monotonic() - _hyg_wait_started,
                                            )
                                            # Episode card: the unfenced attempt
                                            # was fence-CANCELLED here — a
                                            # failure terminal (⚠️), never a
                                            # deferral. On Discord it replaces
                                            # the plain notice; everywhere else
                                            # the notice posts exactly as before.
                                            await self._hygiene_episode_emit_turnhold_cancelled_or_notice(
                                                source=source,
                                                session_key=session_key,
                                                metadata=_hyg_meta,
                                                attempt_token=_hyg_episode_token,
                                            )
                                            raise
                                    except asyncio.TimeoutError:
                                        _hyg_waited = time.monotonic() - _hyg_wait_started
                                        _hyg_total_exhausted = (
                                            _hyg_waited >= _hyg_total_ceiling_seconds
                                            or _hyg_commit_fence.deadline_exceeded
                                        )
                                        if _hyg_total_exhausted:
                                            # The worker cooperatively checks this
                                            # deadline between digest calls. Keep
                                            # its lease until it exits so an
                                            # unchanged session cannot overlap a
                                            # retry. Inactivity timeouts retain the
                                            # established release behavior for a
                                            # provider call that may never return.
                                            _hyg_commit_fence.retain_compression_lock_until_worker_done()
                                        # Capture fence state BEFORE try_cancel
                                        # — that call itself sets is_cancelled,
                                        # which would mis-label a genuine idle
                                        # timeout as a fence cancel (#96953).
                                        _hyg_fence_cancelled = (
                                            _hyg_commit_fence.is_cancelled
                                        )
                                        _cancelled = None
                                        while _cancelled is None:
                                            # #76354 F1: a hung commit retains the
                                            # fence lock; the lock-free phase
                                            # marker keeps this loop from spinning
                                            # forever while the commit blocks.
                                            if _hyg_commit_fence.commit_in_flight:
                                                _cancelled = False
                                                break
                                            _cancelled = (
                                                _hyg_commit_fence.try_cancel_before_commit()
                                            )
                                            if _cancelled is None:
                                                # Round-2 #5: transient
                                                # lock-setup windows ride
                                                # write patience for seconds;
                                                # 25ms keeps sub-tick latency
                                                # without 1kHz spin.
                                                await asyncio.sleep(0.025)
                                        if not _cancelled:
                                            # The worker crossed the commit boundary just
                                            # before the timeout. The fence poll waited for
                                            # that boundary to finish, so consume the
                                            # completed result instead of treating a
                                            # successful compaction as a timeout.
                                            _compressed, _ = await _hyg_future
                                        else:
                                            # Release an inactivity-timed-out
                                            # worker's holder-qualified lease
                                            # promptly. Total-ceiling attempts
                                            # retained it above, so this is a
                                            # no-op until worker cleanup there.
                                            _hyg_commit_fence.release_cancelled_compression_lock()
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene timeout",
                                            )
                                            _hyg_cleanup_deferred = True
                                            _hyg_timeout_error = (
                                                "session hygiene compression "
                                                "cancelled at commit fence"
                                                if _hyg_fence_cancelled
                                                else (
                                                    "session hygiene compression "
                                                    "timed out with no output from "
                                                    "the summary model"
                                                )
                                            )
                                            if _hyg_failure_cooldown_seconds >= 0:
                                                _hyg_cooldown = await asyncio.to_thread(
                                                    _hygiene_cooldown_for_failure,
                                                    self,
                                                    session_key,
                                                    _hyg_failure_cooldown_seconds,
                                                )
                                                _timeout_reason = (
                                                    _hyg_timeout_error
                                                    if _hyg_fence_cancelled
                                                    else (
                                                        "session hygiene compression total "
                                                        "ceiling exhausted"
                                                        if _hyg_total_exhausted
                                                        else "session hygiene compression "
                                                        "timed out with no output from the "
                                                        "summary model"
                                                    )
                                                )
                                                _record_hygiene_cooldown(
                                                    self, session_entry.session_id,
                                                    _hyg_cooldown,
                                                    _timeout_reason,
                                                )
                                            from agent.session_activity import (
                                                ActivityProvenance,
                                            )
                                            _stamp_hygiene_compression_provenance(
                                                _hyg_agent,
                                                (
                                                    "session hygiene compression "
                                                    "cancelled at commit fence"
                                                    if _hyg_fence_cancelled
                                                    else "session hygiene compression timed out"
                                                ),
                                                ActivityProvenance.AGENT_COMPRESSION_TIMEOUT,
                                                "hygiene compression timeout "
                                                "activity stamp failed",
                                            )
                                            # Episode card: timeout-cancel is a
                                            # failure terminal (⚠️) — fence
                                            # cancels and silent workers alike.
                                            # On Discord it replaces the plain
                                            # timeout warning below.
                                            _hyg_episode_owned = (
                                                await self._hygiene_episode_emit_timeout_terminal(
                                                    source=source,
                                                    session_key=session_key,
                                                    metadata=_hyg_meta,
                                                    attempt_token=_hyg_episode_token,
                                                    fence_cancelled=_hyg_fence_cancelled,
                                                    timeout_error=_hyg_timeout_error,
                                                )
                                            )
                                            if _hyg_fence_cancelled:
                                                logger.warning(
                                                    "Session hygiene compression for "
                                                    "session %s was cancelled at the "
                                                    "commit fence; continuing without "
                                                    "compression",
                                                    session_entry.session_id,
                                                )
                                            else:
                                                _hyg_elapsed = (
                                                    time.monotonic() - _hyg_wait_started
                                                )
                                                if _hyg_total_exhausted:
                                                    logger.warning(
                                                        "Session hygiene compression for session %s "
                                                        "reached its total ceiling after %.1fs "
                                                        "(progress observed=%s); continuing without "
                                                        "compression",
                                                        session_entry.session_id,
                                                        _hyg_elapsed,
                                                        _hyg_commit_fence.progress_observed,
                                                    )
                                                else:
                                                    logger.warning(
                                                        "Session hygiene compression for session %s "
                                                        "made no progress for %.1fs (total wait "
                                                        "%.1fs, ceiling %.1fs); continuing without "
                                                        "compression",
                                                        session_entry.session_id,
                                                        _hyg_commit_fence.seconds_since_progress(),
                                                        _hyg_elapsed,
                                                        _hyg_total_ceiling_seconds,
                                                    )
                                                if not _hyg_episode_owned:
                                                    _timeout_msg = (
                                                        _hygiene_compression_timeout_message(
                                                            total_exhausted=_hyg_total_exhausted,
                                                            elapsed=_hyg_elapsed,
                                                            idle_timeout=_hyg_timeout_seconds,
                                                            progress_observed=(
                                                                _hyg_commit_fence.progress_observed
                                                            ),
                                                        )
                                                    )
                                                    try:
                                                        _adapter = self._adapter_for_source(source)
                                                        if _adapter and source.chat_id:
                                                            await _adapter.send(
                                                                source.chat_id,
                                                                _timeout_msg,
                                                                metadata=_hyg_meta,
                                                            )
                                                    except Exception as _werr:
                                                        logger.warning(
                                                            "Failed to deliver compression-timeout "
                                                            "warning to user: %s",
                                                            _werr,
                                                        )
                                            raise
                                    except BaseException:
                                        # #76354 F2: non-timeout unwind while the
                                        # detached hygiene worker may still run —
                                        # KeyboardInterrupt, task cancellation, or
                                        # any unexpected error. Revoke commit
                                        # admission (and release the worker's
                                        # durable lease via the holder-qualified
                                        # hook) BEFORE the host unwinds so the
                                        # worker can never commit later.
                                        _hyg_commit_fence.revoke_commit_admission()
                                        if not _hyg_cleanup_deferred:
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene unwind",
                                            )
                                            _hyg_cleanup_deferred = True
                                        # #96953: restart drain / task cancel used
                                        # to re-raise with no cooldown, so the
                                        # next turn immediately re-armed hygiene
                                        # and waited up to 600s behind a fence
                                        # that would refuse the commit again.
                                        if _hyg_failure_cooldown_seconds >= 0:
                                            try:
                                                _hyg_cooldown = _hygiene_cooldown_for_failure(
                                                    self,
                                                    session_key,
                                                    _hyg_failure_cooldown_seconds,
                                                )
                                                _record_hygiene_cooldown(
                                                    self, session_entry.session_id,
                                                    _hyg_cooldown,
                                                    "session hygiene compression "
                                                    "cancelled at commit fence",
                                                )
                                            except Exception as _cd_err:
                                                logger.debug(
                                                    "hygiene unwind cooldown "
                                                    "record failed: %s",
                                                    _cd_err,
                                                )
                                        # Episode card: close the open card as
                                        # stopped (mid-flight unwind cannot
                                        # prove the transcript survived, so the
                                        # line claims nothing). Scheduled
                                        # detached so the unwind itself is
                                        # never delayed by delivery.
                                        self._hygiene_episode_schedule_unwind_terminal(
                                            source=source,
                                            session_key=session_key,
                                            metadata=_hyg_meta,
                                            attempt_token=_hyg_episode_token,
                                            loop=loop,
                                        )
                                        raise

                                    # _compress_context ends the old session and creates
                                    # a new session_id.  Write compressed messages into
                                    # the NEW session so the old transcript stays intact
                                    # and searchable via session_search.
                                    _hyg_new_sid = _hyg_agent.session_id
                                    _hyg_rotated = _hyg_new_sid != session_entry.session_id
                                    _hyg_in_place = bool(
                                        getattr(_hyg_agent, "_last_compaction_in_place", False)
                                    )
                                    # Anti-growth guard: refuse a compression
                                    # that did not shrink the transcript
                                    # (observed: 427K -> 598K). Compare
                                    # like-for-like rough estimates.
                                    _hyg_in_toks = estimate_messages_tokens_rough(history)
                                    _hyg_out_toks = estimate_messages_tokens_rough(_compressed)
                                    if _hyg_rotated and _hyg_out_toks > _hyg_in_toks:
                                        logger.warning(
                                            "Gateway hygiene compression for session %s "
                                            "would grow transcript (~%s -> ~%s tokens); "
                                            "keeping the original transcript unchanged",
                                            session_entry.session_id,
                                            f"{_hyg_in_toks:,}",
                                            f"{_hyg_out_toks:,}",
                                        )
                                        _hyg_rotated = False
                                        _compressed = history
                                    # Only rewrite the transcript when rotation produced
                                    # a NEW session id.  In-place compaction does NOT
                                    # need a rewrite: archive_and_compact() has already
                                    # soft-archived the previous active rows and inserted
                                    # the compacted messages as the new active set inside
                                    # _compress_context().  Calling rewrite_transcript()
                                    # after in-place compaction would invoke
                                    # replace_messages(active_only=False) which DELETEs
                                    # ALL rows — including the archived turns that
                                    # archive_and_compact() deliberately preserved
                                    # (silent data loss, #61145).
                                    #
                                    # The danger this guards against (mirrors the
                                    # /compress fix #44794/#39704): if _compress_context
                                    # returns a summary but neither rotates nor completes
                                    # archive_and_compact(), the session_id is unchanged
                                    # for a FAILURE reason, and an unconditional
                                    # rewrite_transcript() would DELETE the original
                                    # messages and replace them with only the compressed
                                    # summary (permanent data loss, #21301).
                                    #
                                    # Write-before-repoint (mirrors manual /compress):
                                    # if we repointed session_entry onto the child SID
                                    # and rewrite_transcript then failed (lock/ENOSPC),
                                    # the live entry would already reference a brand-new
                                    # empty session while the turn continues — the
                                    # conversation silently vanishes. Persist the child
                                    # transcript first; only then rebind the live entry.
                                    if _hyg_rotated:
                                        if not await self.async_session_store.rewrite_transcript(
                                            _hyg_new_sid, _compressed
                                        ):
                                            logger.error(
                                                "Session hygiene: failed to persist "
                                                "compressed transcript for rotated "
                                                "session %s → %s; keeping the live "
                                                "entry on the original session so the "
                                                "conversation is not dropped",
                                                session_entry.session_id,
                                                _hyg_new_sid,
                                            )
                                            # Fail closed: treat like no rotation.
                                            _hyg_rotated = False
                                            _hyg_in_place = False
                                        else:
                                            session_entry.session_id = _hyg_new_sid
                                            # The held turn lease follows the
                                            # rotation so an alias key resolving
                                            # the fresh child still serializes
                                            # against this turn (#64934).
                                            self._rebind_turn_lease(
                                                _quick_key, run_generation, _hyg_new_sid
                                            )
                                            await self.async_session_store._save()
                                            await asyncio.to_thread(
                                                self._sync_telegram_topic_binding,
                                                source, session_entry,
                                                reason="hygiene-compression",
                                            )

                                    if _hyg_rotated:
                                        # Reset stored token count — transcript rewritten
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(
                                            _compressed
                                        )
                                    elif _hyg_in_place:
                                        # archive_and_compact() already persisted the
                                        # compacted transcript inside _compress_context.
                                        # Reset counts to match the new active set.
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(
                                            _compressed
                                        )
                                    else:
                                        # No rewrite happened — transcript preserved
                                        # unchanged, so the post-compression counts equal
                                        # the pre-compression ones.
                                        _new_count = _msg_count
                                        _new_tokens = _approx_tokens
                                        logger.warning(
                                            "Gateway hygiene compression for session %s "
                                            "did not rotate or compact in place "
                                            "(no session_db on the hygiene agent) — "
                                            "preserving the original transcript instead "
                                            "of overwriting it with the summary (#21301).",
                                            session_entry.session_id,
                                        )

                                    logger.info(
                                        "Session hygiene: compressed %s → %s msgs, "
                                        "~%s → ~%s tokens",
                                        _msg_count, _new_count,
                                        f"{_approx_tokens:,}", f"{_new_tokens:,}",
                                    )

                                    if _new_tokens >= _warn_token_threshold:
                                        logger.warning(
                                            "Session hygiene: still ~%s tokens after "
                                            "compression",
                                            f"{_new_tokens:,}",
                                        )

                                    # If summary generation failed, the
                                    # compressor aborts entirely and returns
                                    # messages unchanged — nothing is dropped.
                                    # Surface a visible warning to the gateway
                                    # user — agent.log alone is invisible on
                                    # TG/Discord/etc. — so they know the chat
                                    # is "frozen" at the current size and can
                                    # /compress to retry or /reset to start
                                    # fresh.
                                    _comp = getattr(_hyg_agent, "context_compressor", None)
                                    _hyg_aborted = _comp is not None and getattr(
                                        _comp, "_last_compress_aborted", False
                                    )
                                    # Fence-cancelled _compress_context returns
                                    # the original transcript with
                                    # _last_compress_aborted still False
                                    # (failure_class=commit_fence_cancelled,
                                    # chunk_count=0). Treat that no-op as an
                                    # abort so hygiene records a cooldown
                                    # instead of retrying into the 600s wait
                                    # (#96953). A successful rotate/in-place
                                    # commit is not an abort even if a later
                                    # invalidation flipped the fence.
                                    _hyg_fence_cancelled = bool(
                                        _hyg_commit_fence.is_cancelled
                                        and not _hyg_rotated
                                        and not _hyg_in_place
                                    )
                                    if _hyg_fence_cancelled:
                                        _hyg_aborted = True
                                    # Episode card terminal for the inline
                                    # (awaited) path: ✅ ONLY for a committed
                                    # rotate/in-place compaction — every
                                    # did-not-commit outcome (summary abort,
                                    # fence-cancelled no-op, anti-growth
                                    # refusal, missing session_db) closes the
                                    # card as ⚠️. A no-op is never a success.
                                    _hyg_episode_owned = (
                                        await self._hygiene_episode_emit_inline_terminal(
                                            source=source,
                                            session_key=session_key,
                                            metadata=_hyg_meta,
                                            attempt_token=_hyg_episode_token,
                                            agent=_hyg_agent,
                                            rotated=_hyg_rotated,
                                            in_place=_hyg_in_place,
                                            aborted=_hyg_aborted,
                                            fence_cancelled=_hyg_fence_cancelled,
                                            before_messages=_msg_count,
                                            after_messages=_new_count,
                                            before_tokens=_approx_tokens or None,
                                            after_tokens=_new_tokens or None,
                                        )
                                    )
                                    if not _hyg_aborted:
                                        # Recovery decision lives in the
                                        # extracted, unit-tested predicate — the
                                        # degenerate "did not rotate or compact
                                        # in place" path (#21301) sets both flags
                                        # False and reuses the pre-compression
                                        # counts, so a numbers-only check would
                                        # read a no-op as success and clear the
                                        # streak on every wedged run (#79624).
                                        if hygiene_compaction_recovered(
                                            aborted=_hyg_aborted,
                                            rotated=_hyg_rotated,
                                            in_place=_hyg_in_place,
                                            msg_count=_msg_count,
                                            new_count=_new_count,
                                            approx_tokens=_approx_tokens,
                                            new_tokens=_new_tokens,
                                        ):
                                            await asyncio.to_thread(
                                                _reset_hygiene_failure_streak,
                                                self,
                                                session_key,
                                            )
                                    if _hyg_aborted:
                                        if _hyg_failure_cooldown_seconds >= 0:
                                            _hyg_cooldown = await asyncio.to_thread(
                                                _hygiene_cooldown_for_failure,
                                                self,
                                                session_key,
                                                _hyg_failure_cooldown_seconds,
                                            )
                                            _record_hygiene_cooldown(
                                                self, session_entry.session_id,
                                                _hyg_cooldown,
                                                (
                                                    "session hygiene compression "
                                                    "cancelled at commit fence"
                                                    if _hyg_fence_cancelled
                                                    else getattr(
                                                        _comp, "_last_summary_error", None
                                                    )
                                                ),
                                            )
                                        from agent.session_activity import (
                                            ActivityProvenance,
                                        )
                                        _stamp_hygiene_compression_provenance(
                                            _hyg_agent,
                                            "session hygiene compression aborted",
                                            ActivityProvenance.AGENT_COMPRESSION_COOLDOWN,
                                            "hygiene compression abort "
                                            "activity stamp failed",
                                        )
                                        if not _hyg_fence_cancelled and not _hyg_episode_owned:
                                            _err = getattr(_comp, "_last_summary_error", None) or "unknown error"
                                            # Force-redact: provider exception text
                                            # may contain credentials; this message
                                            # reaches gateway users directly.
                                            from agent.redact import redact_sensitive_text
                                            _err = redact_sensitive_text(_err, force=True)
                                            _warn_msg = (
                                                "⚠️ Context compression aborted "
                                                f"({_err}). No messages were dropped — "
                                                "conversation is unchanged. Run /compress "
                                                "to retry, /reset for a clean session, or "
                                                "check your auxiliary.compression model "
                                                "configuration."
                                            )
                                            try:
                                                _adapter = self._adapter_for_source(source)
                                                if _adapter and source.chat_id:
                                                    await _adapter.send(source.chat_id, _warn_msg, metadata=_hyg_meta)
                                            except Exception as _werr:
                                                logger.warning(
                                                    "Failed to deliver compression-failure warning to user: %s",
                                                    _werr,
                                                )
                                    # Separately: if the user's CONFIGURED aux
                                    # model failed and we recovered by falling
                                    # back to the main model, tell them — a
                                    # misconfigured auxiliary.compression.model
                                    # is something only they can fix, and
                                    # silent recovery would hide it.
                                    elif _comp is not None and getattr(_comp, "_last_aux_model_failure_model", None):
                                        _aux_model = getattr(_comp, "_last_aux_model_failure_model", "")
                                        _aux_err = getattr(_comp, "_last_aux_model_failure_error", None) or "unknown error"
                                        _aux_msg = (
                                            f"ℹ️ Configured compression model `{_aux_model}` "
                                            f"failed ({_aux_err}). Recovered using your main "
                                            "model — context is intact — but you may want to "
                                            "check `auxiliary.compression.model` in config.yaml."
                                        )
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _aux_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver aux-model-fallback notice to user: %s",
                                                _werr,
                                            )
                                finally:
                                    # Evict the cached agent so the next turn
                                    # rebuilds its system prompt from current
                                    # SOUL.md, memory, and skills.
                                    self._evict_cached_agent(session_key)
                                    if not _hyg_cleanup_deferred:
                                        await self._cleanup_agent_resources_off_loop(
                                            _hyg_agent, context="session hygiene"
                                        )

                    except HygieneTurnHoldExceeded:
                        # Availability boundary, not a failure — already logged
                        # at INFO by the turn-hold handler. Must not hit the
                        # generic "auto-compress failed" warning below: that
                        # log is how thinking-model deployments read as
                        # permanently broken (#97963; surfaced by @686f6c61
                        # in PR #99657).
                        pass
                    except Exception as e:
                        logger.warning(
                            "Session hygiene auto-compress failed: %s", e
                        )

        # First-message onboarding -- only on the very first interaction ever.
        # Delivered on the current user message (sidecar), NOT the ephemeral
        # system prompt: present-on-turn-1/absent-on-turn-2 was a guaranteed
        # system-prompt diff and agent rebuild.
        if not history and not await self.async_session_store.has_any_sessions():
            # Default first-contact note: a brief self-introduction.
            _intro_note = (
                "[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available commands. "
                "Keep the introduction concise -- one or two sentences max.]"
            )
            # Opt-in structured profile-build path. When enabled (default
            # "ask") and not yet offered on this install, swap the plain intro
            # for a consent-gated directive that offers to build a user
            # profile and persists confirmed facts via memory(target="user").
            # The offer fires at most once (onboarding.seen flag); set
            # onboarding.profile_build: off in config.yaml to disable.
            try:
                from agent.onboarding import (
                    PROFILE_BUILD_FLAG,
                    is_seen,
                    mark_seen,
                    profile_build_directive,
                    profile_build_mode,
                )
                _onb_cfg = _load_gateway_config()
                if (
                    profile_build_mode(_onb_cfg) == "ask"
                    and not is_seen(_onb_cfg, PROFILE_BUILD_FLAG)
                ):
                    turn_sidecar_notes.append(profile_build_directive().strip())
                    mark_seen(_hermes_home / "config.yaml", PROFILE_BUILD_FLAG)
                else:
                    turn_sidecar_notes.append(_intro_note)
            except Exception as _pb_err:
                logger.debug(
                    "Profile-build onboarding directive failed, using plain intro: %s",
                    _pb_err,
                )
                turn_sidecar_notes.append(_intro_note)

        # -----------------------------------------------------------------
        # Voice channel awareness — deliver current voice channel state so
        # the agent knows who is in the channel and who is speaking, without
        # needing a separate tool call.  Delivered on the current user
        # message and ONLY when it changed since the previous turn: the
        # member/speaking serialization differs essentially every turn, and
        # appending it to the ephemeral system prompt forced a full agent
        # rebuild + prompt-cache re-key per message.  The system prompt
        # carries a static pointer line instead (gateway/session.py).
        # -----------------------------------------------------------------
        _vc_note = self._voice_channel_sidecar_note(event, source, session_key)
        if _vc_note:
            turn_sidecar_notes.append(_vc_note)

        # -----------------------------------------------------------------
        # Auto-analyze images sent by the user
        #
        # If the user attached image(s), we run the vision tool eagerly so
        # the conversation model always receives a text description.  The
        # local file path is also included so the model can re-examine the
        # image later with a more targeted question via vision_analyze.
        #
        # We filter to image paths only (by media_type) so that non-image
        # attachments (documents, audio, etc.) are not sent to the vision
        # tool even when they appear in the same message.
        # -----------------------------------------------------------------
        message_text = await self._prepare_profile_scoped_inbound_message_text(
            event=event,
            source=source,
            history=history,
            session_key=session_key,
        )
        if message_text is None:
            return

        # Capture the platform event time as message metadata and keep the
        # persisted transcript clean (strip any leading timestamp prefix).
        # This runs regardless of the toggle so storage stays clean and the
        # send-time is preserved. Only the in-context RENDER (prepending the
        # human-readable prefix the model sees) is gated behind
        # gateway.message_timestamps.enabled — default OFF.
        try:
            from hermes_time import get_timezone as _get_evt_tz
            from gateway.message_timestamps import (
                coerce_message_timestamp as _coerce_msg_ts,
                render_user_content_with_timestamp as _render_msg_ts,
                strip_leading_message_timestamps as _strip_msg_ts,
            )
            _evt_tz = _get_evt_tz()
            _evt_ts = getattr(event, "timestamp", None)
            if message_text and isinstance(message_text, str):
                _clean_message_text, _embedded_ts = _strip_msg_ts(
                    message_text, tz=_evt_tz)
                persist_user_message = _clean_message_text
                _event_epoch = _coerce_msg_ts(_evt_ts, tz=_evt_tz)
                persist_user_timestamp = (
                    _event_epoch if _event_epoch is not None else _embedded_ts
                )
                if _message_timestamps_enabled(_load_gateway_config()):
                    message_text = _render_msg_ts(
                        _clean_message_text,
                        persist_user_timestamp,
                        tz=_evt_tz,
                    )
                else:
                    # Toggle off: model sees the clean message; the timestamp
                    # is still stored as metadata for later opt-in.
                    message_text = _clean_message_text
        except Exception as _ts_err:
            logger.debug("Message timestamp injection failed (non-fatal): %s", _ts_err)

        # Stage the collected must-deliver notes for this turn's agent run
        # (one-shot; consumed in run_sync).  Staged AFTER the message_text
        # early-out above so an aborted turn cannot leak its notes into the
        # next turn's user message.
        if turn_sidecar_notes and session_key:
            self._set_pending_turn_sidecar_notes(session_key, turn_sidecar_notes)

        # Bind this gateway run generation to the adapter's active-session
        # event so deferred post-delivery callbacks can be released by the
        # same run that registered them.
        self._bind_adapter_run_generation(
            self._adapter_for_source(source),
            session_key,
            run_generation,
        )

        try:
            # Emit agent:start hook
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "chat_id": source.chat_id or "",
                "thread_id": str(getattr(source, "thread_id", None)) if getattr(source, "thread_id", None) else "",
                "chat_type": getattr(source, "chat_type", "") or "",
                "session_id": session_entry.session_id,
                "message": message_text[:500],
            }
            await self.hooks.emit("agent:start", hook_ctx)

            # Run the agent. Capture the session id that this run was launched
            # against so post-run compression publication can be identity-guarded
            # below; a /new or another lifecycle transition may move
            # session_entry.session_id while the old run is still unwinding.
            _run_start_session_id = session_entry.session_id
            _turn_started_monotonic = time.monotonic()
            agent_result = await self._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=_run_start_session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=self._reply_anchor_for_event(event),
                inbound_message_id=self._inbound_message_id_for_event(event),
                channel_prompt=event.channel_prompt,
                moa_config=getattr(event, "_moa_config", None),
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
                persist_user_display_kind=persist_user_display_kind,
                message_type=event.message_type,
            )
            _turn_seconds = time.monotonic() - _turn_started_monotonic

            # Stop persistent typing indicator now that the agent is done.
            # Slack AI status is scoped to a thread/workspace, so preserve the
            # same routing metadata used by the response delivery path.
            try:
                _typing_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(
                    type(_typing_adapter), "_stop_typing_with_metadata", None
                )
                _stop_typing = getattr(type(_typing_adapter), "stop_typing", None)
                if _typing_adapter and callable(_stop_with_metadata):
                    await _typing_adapter._stop_typing_with_metadata(
                        source.chat_id,
                        self._thread_metadata_for_source(
                            source, self._reply_anchor_for_event(event)
                        ),
                    )
                elif _typing_adapter and callable(_stop_typing):
                    await _typing_adapter.stop_typing(source.chat_id)
            except Exception:
                pass

            if not self._is_session_run_current(_quick_key, run_generation):
                logger.info(
                    "Discarding stale agent result for %s — generation %d is no longer current",
                    _quick_key or "?",
                    run_generation,
                )
                _stale_adapter = self._adapter_for_source(source)
                if getattr(type(_stale_adapter), "pop_post_delivery_callback", None) is not None:
                    _stale_adapter.pop_post_delivery_callback(
                        _quick_key,
                        generation=run_generation,
                    )
                elif _stale_adapter and hasattr(_stale_adapter, "_post_delivery_callbacks"):
                    _stale_adapter._post_delivery_callbacks.pop(_quick_key, None)
                return None

            response = agent_result.get("final_response") or ""
            # Hidden-reasoning-only retry exhaustion: the loop's sentinel text
            # ("Codex response remained incomplete after 3 continuation
            # attempts") doubles as final_response, so it would be delivered
            # verbatim into the channel — where peer agents can ingest it as a
            # completed assistant turn (#51628). Blank it here so the normal
            # empty-response handling (and the suppression below) applies.
            if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
                response = ""
            try:
                from gateway.response_filters import is_intentional_silence_agent_result
                _intentional_silence = is_intentional_silence_agent_result(
                    agent_result, response,
                )
            except Exception:
                _intentional_silence = False

            # Convert the agent's internal "(empty)" sentinel into a
            # user-friendly message.  "(empty)" means the model failed to
            # produce visible content after exhausting all retries (nudge,
            # prefill, empty-retry, fallback).  Sending the raw sentinel
            # looks like a bug; a short explanation is more helpful.
            if response == "(empty)" and not _intentional_silence:
                response = (
                    "⚠️ The model returned no response after processing tool "
                    "results. This can happen with some models — try again or "
                    "rephrase your question."
                )
            agent_messages = agent_result.get("messages", [])
            _response_time = time.perf_counter() - _msg_start_time
            _api_calls = agent_result.get("api_calls", 0)
            _resp_len = len(response)
            logger.info(
                "response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars",
                _platform_name, source.chat_id or "unknown",
                _response_time, _api_calls, _resp_len,
            )

            # NOTE: the cross-process cache-coherence re-baseline
            # (_refresh_agent_cache_message_count) is intentionally deferred
            # until AFTER this turn's transcript persistence block below — it
            # must include the first-turn `session_meta` marker row and the
            # compression session_id swap, both of which happen later.  See
            # the call site after the `update_session(...)` write.

            # Successful turn — clear any stuck-loop counter for this session.
            # This ensures the counter only accumulates across CONSECUTIVE
            # restarts where the session was active (never completed).
            #
            # Also clear the resume_pending flag (set by drain-timeout
            # shutdown) — the turn ran to completion, so recovery
            # succeeded and subsequent messages should no longer receive
            # the restart-interruption system note.
            if session_key and _should_clear_resume_pending_after_turn(agent_result):
                await self._clear_restart_failure_count(session_key)
                _resume_entry = None
                try:
                    if getattr(self, "session_store", None) is not None:
                        _resume_entry = self.session_store._entries.get(session_key)
                except Exception:
                    _resume_entry = None
                from gateway.restart_wind_down import (
                    should_preserve_cooperative_restart_marker,
                )

                if should_preserve_cooperative_restart_marker(
                    draining=bool(getattr(self, "_draining", False)),
                    resume_reason=getattr(_resume_entry, "resume_reason", None),
                ):
                    logger.info(
                        "Preserving cooperative-restart resume_pending for %s "
                        "so startup can continue the parked turn",
                        session_key,
                    )
                else:
                    try:
                        await self.async_session_store.clear_resume_pending(session_key)
                        await self._clear_resume_inbound_metadata(session_key)
                    except Exception as _e:
                        logger.debug(
                            "clear_resume_pending failed for %s: %s",
                            session_key, _e,
                        )

            # Normalize empty responses: surface errors, partial failures, and
            # the case where agent did work but returned no text. Fix for #18765.
            # A forced-recovery CONTROL outcome is exempt by contract: the
            # failed recovery must surface NO prose to the forced victim. So
            # is a terminal gateway-restart control outcome: its silence is
            # the contract, and the restart lifecycle UI is the only
            # user-facing output for it.
            if not _intentional_silence and not _exempt_from_empty_response_normalization(
                agent_result
            ):
                response = _normalize_empty_agent_response(
                    agent_result, response, history_len=len(history),
                )
                response = _sanitize_gateway_final_response(source.platform, response)

            # Ordering contract: the agent thread already updated the contextvar
            # in conversation_compression.py; propagate to SessionEntry + _save().
            # If the agent's session_id changed during compression, update
            # session_entry so transcript writes below go to the right session.
            if agent_result.get("session_id") and agent_result["session_id"] != session_entry.session_id:
                if session_entry.session_id == _run_start_session_id:
                    session_entry.session_id = agent_result["session_id"]
                    # The held turn lease follows the rotation: the transcript
                    # persistence below writes to the NEW id, so the
                    # serialization boundary must move with it or an alias
                    # key resolving the fresh child could interleave (#64934).
                    self._rebind_turn_lease(
                        _quick_key, run_generation, session_entry.session_id
                    )
                    await self.async_session_store._save()
                    await self.async_session_store._record_gateway_session_peer(
                        session_entry.session_id,
                        session_key,
                        source,
                    )
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="agent-result-compression",
                    )
                else:
                    logger.info(
                        "Skipping agent-result session split sync for %s because "
                        "the session binding moved from %s to %s before "
                        "compression finished",
                        session_key or "?",
                        _run_start_session_id,
                        session_entry.session_id,
                    )

            # Prepend reasoning/thinking if display is enabled (per-platform).
            # Mattermost requires explicit per-platform opt-in because this is
            # scratch text, not ordinary final-answer content.
            #
            # Scoped to the serving profile (see _display_config_scope) so the
            # reasoning prepend AND the runtime footer below resolve display.*
            # from the profile that actually served this turn.
            with self._display_config_scope(source):
                try:
                    _show_reasoning_effective = _resolve_gateway_display_bool(
                        _load_gateway_config(),
                        _platform_config_key(source.platform),
                        "show_reasoning",
                        default=bool(getattr(self, "_show_reasoning", False)),
                        platform=source.platform,
                    )
                except Exception:
                    _show_reasoning_effective = getattr(self, "_show_reasoning", False)
                if _show_reasoning_effective and response and not _intentional_silence:
                    last_reasoning = agent_result.get("last_reasoning")
                    if last_reasoning:
                        # Render style is per-platform: Discord defaults to "-# "
                        # subtext (native small grey metadata text); "compact"
                        # shows only a one-line "thought for Xs" duration note
                        # (plus this turn's "(N tokens)" when the provider
                        # reported a count); other platforms keep the fenced
                        # code block.
                        try:
                            from gateway.display_config import resolve_display_setting
                            _reasoning_style = resolve_display_setting(
                                _load_gateway_config(),
                                _platform_config_key(source.platform),
                                "reasoning_style",
                                "code",
                            )
                        except Exception:
                            _reasoning_style = "code"
                        from gateway.display_config import format_reasoning_prefix
                        _reasoning_prefix = format_reasoning_prefix(
                            _reasoning_style,
                            last_reasoning,
                            _turn_seconds,
                            _platform_config_key(source.platform),
                            thought_tokens=agent_result.get("thought_tokens"),
                        )
                        if _reasoning_prefix:
                            response = f"{_reasoning_prefix}\n\n{response}"

                # Runtime-metadata footer — only on the FINAL message of the turn.
                # Off by default (display.runtime_footer.enabled=false).  When
                # streaming already delivered the body, we can't mutate the sent
                # text, so we fire a separate trailing send below.
                _footer_line = ""
                try:
                    from gateway.runtime_footer import build_footer_line as _bfl
                    _footer_line = _bfl(
                        user_config=_load_gateway_config(),
                        platform_key=_platform_config_key(source.platform),
                        model=agent_result.get("model"),
                        context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,
                        context_length=agent_result.get("context_length") or None,
                        cwd=_terminal_scope_cwd(""),
                        turn_seconds=_turn_seconds,
                        turn_time=_response_time,
                        api_time=agent_result.get("api_time"),
                        tool_time=agent_result.get("tool_time"),
                        api_calls=_api_calls,
                        requested_model=agent_result.get("requested_model"),
                        served_model=agent_result.get("served_model"),
                    )
                except Exception as _footer_err:
                    logger.debug("runtime_footer build failed: %s", _footer_err)
                    _footer_line = ""
                if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:
                    response = f"{response}\n\n{_footer_line}"

            # Emit agent:end hook
            await self.hooks.emit("agent:end", {
                **hook_ctx,
                "response": (response or "")[:500],
                "model": agent_result.get("model", ""),
                "provider": agent_result.get("provider", ""),
            })

            # Check for pending process watchers (check_interval on background processes)
            try:
                from tools.process_registry import process_registry
                # Detach the current batch atomically (see crash-recovery drain
                # above): reassign to a fresh list so a watcher appended by a
                # concurrent session during the yield isn't dropped by clear().
                watchers = process_registry.pending_watchers
                process_registry.pending_watchers = []
                for i, watcher in enumerate(watchers):
                    asyncio.create_task(self._run_process_watcher(watcher))
                    if i % 100 == 99:
                        await asyncio.sleep(0)
            except Exception as e:
                logger.error("Process watcher setup error: %s", e)

            # Drain watch pattern notifications that arrived during the agent run.
            # Watch events and completions share the same queue; process
            # completions are already handled by the per-process watcher task
            # above, so we only inject watch-type events here.
            #
            # Async-delegation completions ALSO ride this shared queue but are
            # owned by the dedicated _async_delegation_watcher (started at
            # boot), which covers both the idle and post-turn cases with a
            # single consumer — so we leave them on the queue here.
            try:
                from tools.process_registry import process_registry as _pr
                await self._drain_watch_notifications(_pr.completion_queue)
            except Exception as e:
                logger.debug("Watch queue drain error: %s", e)

            # NOTE: Dangerous command approvals are now handled inline by the
            # blocking gateway approval mechanism in tools/approval.py.  The agent
            # thread blocks until the user responds with /approve or /deny, so by
            # the time we reach here the approval has already been resolved.  The
            # old post-loop pop_pending + approval_hint code was removed in favour
            # of the blocking approach that mirrors CLI's synchronous input().

            # Save the full conversation to the transcript, including tool calls.
            # This preserves the complete agent loop (tool_calls, tool results,
            # intermediate reasoning) so sessions can be resumed with full context
            # and transcripts are useful for debugging and training data.
            #
            # IMPORTANT: For context-overflow failures (compression exhausted,
            # generic 400 on large sessions) we must NOT persist the user's
            # message — doing so would grow the session further and cause the
            # same failure on the next attempt, an infinite loop. (#1630, #9893)
            #
            # Transient failures (429, timeout, connection error, provider 5xx)
            # are different: the session is not oversized, and silently dropping
            # the user message causes severe context loss on retry — the agent
            # forgets what was just asked.  Persist the user turn so the
            # conversation is preserved. (#7100)
            agent_failed_early = bool(agent_result.get("failed"))
            _forced_recovery_control = _is_forced_recovery_control_outcome(agent_result)
            hidden_reasoning_incomplete = _is_gateway_hidden_reasoning_incomplete_turn(
                agent_result
            )
            _err_str_for_classify = str(agent_result.get("error", "")).lower()
            # Use specific multi-word phrases (not bare "exceed" or "token")
            # to avoid false positives on transient errors like "rate limit
            # exceeded" or "invalid auth token". Matches run_agent.py's
            # own context-length classifier.
            is_context_overflow_failure = agent_failed_early and (
                bool(agent_result.get("compression_exhausted"))
                or any(p in _err_str_for_classify for p in (
                    "context length", "context size", "context window",
                    "maximum context", "token limit", "too many tokens",
                    "reduce the length", "exceeds the limit",
                    "request entity too large", "prompt is too long",
                    "payload too large", "input is too long",
                ))
                or ("400" in _err_str_for_classify and len(history) > 50)
            )
            if is_context_overflow_failure:
                logger.info(
                    "Skipping transcript persistence for context-overflow "
                    "failure in session %s to prevent session growth loop.",
                    session_entry.session_id,
                )
            elif agent_failed_early:
                if _forced_recovery_control:
                    # Internal forced-recovery failure: nothing may be
                    # persisted — no user row (not even a synthetic blank
                    # startup row), no assistant text, no synthetic turn.
                    # The queued/raced user text, if any, was preserved
                    # byte-for-byte by the recovery boundary itself.
                    logger.info(
                        "Forced-recovery control outcome in session %s "
                        "(%s) — skipping transcript persistence; "
                        "resume_pending retained.",
                        session_entry.session_id,
                        agent_result.get("error"),
                    )
                else:
                    logger.info(
                        "Transient agent failure in session %s — persisting user "
                        "message so conversation context is preserved on retry.",
                        session_entry.session_id,
                    )
            elif hidden_reasoning_incomplete:
                logger.warning(
                    "Suppressing hidden-reasoning-only incomplete gateway turn "
                    "for session %s: %s",
                    session_entry.session_id,
                    agent_result.get("error", "processing incomplete"),
                )

            # When compression is exhausted, the session is permanently too
            # large to process.  Auto-reset it so the next message starts
            # fresh instead of replaying the same oversized context in an
            # infinite fail loop.  (#9893)
            #
            # A lock-contended defer is the OPPOSITE case: the session is
            # temporarily uncompressible only because a concurrent path holds
            # the compression lock and is actively shrinking it. Never wipe
            # the session for that — retry-next-message semantics apply
            # (#69870 lock-skip consumer; salvaged from #49874).
            if agent_result.get("compression_deferred"):
                logger.info(
                    "Compression deferred for session %s — the compression "
                    "lock is held by a concurrent compressor. Keeping the "
                    "session intact; the next message retries normally.",
                    session_entry.session_id if session_entry else "?",
                )
            elif agent_result.get("compression_exhausted") and session_entry and session_key:
                logger.info(
                    "Auto-resetting session %s after compression exhaustion.",
                    session_entry.session_id,
                )
                new_entry = await self.async_session_store.reset_session(session_key)
                self._evict_cached_agent(session_key)
                # Conversation boundary: one funnel call clears every
                # conversation-scoped per-session dict (#58403 and siblings).
                # See _CONVERSATION_SCOPED_STATE.
                self._clear_conversation_scope(
                    session_key, reason="compression_exhausted_reset"
                )
                if new_entry is not None:
                    # Drop the stale reference to the bloated compressed child and
                    # re-point the Telegram topic binding at the fresh session.
                    # Compression rotated session_entry.session_id to the oversized
                    # compressed child earlier this turn (the agent-result sync
                    # above), and that _sync also rewrote the (chat_id, thread_id)
                    # -> bloated-child binding. reset_session swaps in a clean,
                    # parentless session, but without re-syncing the binding the
                    # next inbound message in this topic gets switch_session'd back
                    # onto the bloated child by the binding-heal walk, reloads the
                    # oversized transcript, and re-triggers compression exhaustion
                    # forever (#35809 — regression of the #9893/#10063 auto-reset).
                    # No-op on non-topic lanes.
                    session_entry = new_entry
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compression-exhausted-reset",
                    )
                response = (response or "") + (
                    "\n\n🔄 Session auto-reset — the conversation exceeded the "
                    "maximum context size and could not be compressed further. "
                    "Your next message will start a fresh session."
                )

            ts = time.time()  # Unix epoch float — consistent with DB storage

            # If this is a fresh session (no history), write the full tool
            # definitions as the first entry so the transcript is self-describing
            # -- the same list of dicts sent as tools=[...] in the API request.
            if is_context_overflow_failure:
                pass  # Skip all transcript writes — don't grow a broken session
            elif not history:
                tool_defs = agent_result.get("tools", [])
                await self.async_session_store.append_to_transcript(
                    session_entry.session_id,
                    {
                        "role": "session_meta",
                        "tools": tool_defs or [],
                        "model": _resolve_gateway_model(),
                        "platform": source.platform.value if source.platform else "",
                        "timestamp": ts,
                    }
                )

            # The agent already persisted these messages to SQLite via
            # _flush_messages_to_session_db(), so skip the DB write here
            # to prevent the duplicate-write bug (#860 / #42039). This holds
            # for the codex app-server runtime too: although it early-returns
            # and bypasses conversation_loop's per-step flushes, it flushes its
            # own projected assistant/tool messages before returning and
            # reports agent_persisted=True (see agent/codex_runtime.py). Reading
            # the flag (default = self._session_db is not None) keeps the
            # persistence contract explicit and lets any future non-persisting
            # runtime opt into a gateway-side write by returning False.
            agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)

            # Find only the NEW messages from this turn (skip history we loaded).
            # Use the filtered history length (history_offset) that was actually
            # passed to the agent, not len(history) which includes session_meta
            # entries that were stripped before the agent saw them.
            if is_context_overflow_failure:
                pass  # handled above — skip all transcript writes
            elif _forced_recovery_control:
                # Forced-recovery control outcome: NOTHING is persisted —
                # not the raced user text (still queued byte-for-byte at
                # the recovery boundary) and not a synthetic blank startup
                # row for an internal auto-resume event.  The recovery
                # stays retryable because resume_pending was not cleared.
                pass
            elif agent_failed_early or hidden_reasoning_incomplete:
                # Transient failure (429/timeout/5xx): persist only the user
                # message so the next message can load a transcript that
                # reflects what was said.  Skip the assistant error text since
                # it's a gateway-generated hint, not model output. Hidden-
                # reasoning-only incomplete turns follow the same persistence
                # rule so peer-agent channels don't ingest them as completed
                # assistant turns. (#7100, #51628)
                _user_entry = {
                    "role": "user",
                    "content": (
                        persist_user_message
                        if persist_user_message is not None
                        else message_text
                    ),
                    "timestamp": (
                        persist_user_timestamp
                        if persist_user_timestamp is not None
                        else ts
                    ),
                }
                if persist_user_display_kind:
                    _user_entry["display_kind"] = persist_user_display_kind
                # Internal synthetic turns borrow the session's remembered
                # last-user-message-id as a reply anchor; that id already has
                # its own transcript row, so never stamp it here — the
                # platform message_id dedupe (#47237) would otherwise drop
                # the whole internal turn as a duplicate.
                if event.message_id and not getattr(event, "internal", False):
                    _user_entry["message_id"] = str(event.message_id)
                # Dedupe: skip if this platform message_id is already in the
                # transcript (prevents duplicate user turns on Telegram retries
                # after transient failures). #47237. Internal synthetic turns
                # borrow the remembered anchor id, which IS already in the
                # transcript — they must never be treated as duplicates.
                _skip_persist = (
                    event.message_id
                    and not getattr(event, "internal", False)
                    and await self.async_session_store.has_platform_message_id(
                        session_entry.session_id, str(event.message_id)
                    )
                )
                if _skip_persist:
                    logger.info(
                        "Skipping duplicate user turn "
                        "(message_id=%s) in session %s",
                        event.message_id, session_entry.session_id,
                    )
                else:
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id,
                        _user_entry,
                        skip_db=agent_persisted,
                    )
            else:
                history_len = agent_result.get("history_offset", len(history))
                new_messages = agent_messages[history_len:] if len(agent_messages) > history_len else []

                # If no new messages found (edge case), fall back to simple user/assistant
                if not new_messages:
                    _user_entry = {
                        "role": "user",
                        "content": (
                            persist_user_message
                            if persist_user_message is not None
                            else message_text
                        ),
                        "timestamp": (
                            persist_user_timestamp
                            if persist_user_timestamp is not None
                            else ts
                        ),
                    }
                    if persist_user_display_kind:
                        _user_entry["display_kind"] = persist_user_display_kind
                    # Same internal-anchor rule as the main persist path above.
                    if event.message_id and not getattr(event, "internal", False):
                        _user_entry["message_id"] = str(event.message_id)
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id,
                        _user_entry,
                        skip_db=agent_persisted,
                    )
                    if response:
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id,
                            {"role": "assistant", "content": response, "timestamp": ts},
                            skip_db=agent_persisted,
                        )
                else:
                    # Attach the inbound platform message_id to the first user
                    # entry written this turn so platform-level quote-resolution
                    # (e.g. Yuanbao QuoteContextMiddleware's transcript fallback)
                    # can find earlier @bot messages by their original message_id.
                    _user_msg_id_attached = False
                    for msg in new_messages:
                        # Skip system messages (they're rebuilt each run)
                        if msg.get("role") == "system":
                            continue
                        # Add timestamp to each message for debugging
                        entry = {**msg, "timestamp": ts}
                        if (
                            not _user_msg_id_attached
                            and msg.get("role") == "user"
                            and event.message_id
                            and "message_id" not in entry
                        ):
                            entry["message_id"] = str(event.message_id)
                            _user_msg_id_attached = True
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id, entry,
                            skip_db=agent_persisted,
                        )

            # Token counts and model are now persisted by the agent directly.
            # Keep only last_prompt_tokens here for context-window tracking and
            # compression decisions.
            await self.async_session_store.update_session(
                session_entry.session_key,
                last_prompt_tokens=agent_result.get("last_prompt_tokens", 0),
                touch_activity=not bool(getattr(event, "internal", False)),
            )

            # Re-baseline the cached agent's message_count snapshot now that
            # ALL of this turn's transcript writes are done — the agent's
            # flushed user/assistant/tool rows AND the first-turn `session_meta`
            # marker appended above.  The cross-process coherence guard (#45966)
            # snapshots the count at agent-BUILD time (before this turn's own
            # writes) and never refreshes it on reuse, so without this the
            # process's own turn grows message_count and the next turn sees a
            # mismatch and rebuilds the agent — destroying prompt caching.
            #
            # This MUST run after the `session_meta` append: that row also
            # increments message_count, so re-baselining before it (the old
            # position) left the snapshot one short and the guard mis-fired on
            # turn 2 of EVERY fresh gateway conversation, rebuilding the cached
            # agent and busting the prompt cache.  Running here also uses the
            # compaction-updated session_id (the agent_result session_id swap
            # above), matching this function's documented contract.  Refreshing
            # here makes the guard fire only on a DIFFERENT process's writes.
            # Fail-safe inside the helper.
            await self._refresh_agent_cache_message_count(
                session_key, session_entry.session_id
            )

            # Intentional silence is a delivery decision, not a transcript
            # mutation.  The agent's [SILENT]/NO_REPLY assistant turn above is
            # still persisted in session history so later turns keep normal
            # user/assistant alternation; only the outbound chat delivery is
            # suppressed.
            if _intentional_silence:
                logger.info(
                    "Suppressing intentional silence marker for session %s",
                    session_entry.session_id,
                )
                response = ""

            if _forced_recovery_control:
                # Internal forced-recovery failure: deliver NOTHING — no
                # recovery prose, no error hint, no typing follow-up.  The
                # turn stays invisible to the forced victim; recovery is
                # retried (resume_pending retained) or the owning worker
                # completes it.
                logger.info(
                    "Suppressing delivery of forced-recovery control outcome "
                    "for session %s (%s)",
                    session_key,
                    agent_result.get("error"),
                )
                response = ""

            if _is_gateway_restart_control_outcome(agent_result):
                # A committed gateway restart ended this turn: deliver
                # NOTHING — no model-generated continuation, no fabricated
                # empty-response prose. The restart confirmation/drain/
                # comeback UI is the only user-facing lifecycle output, and
                # the requester's comeback notice arrives after the bounce.
                logger.info(
                    "Suppressing delivery of gateway-restart control outcome "
                    "for session %s",
                    session_key,
                )
                response = ""

            # Auto voice reply: send TTS audio before the text response
            _already_sent = bool(agent_result.get("already_sent"))
            # Skip when streaming TTS already delivered audio for this turn (#60671).
            _stts_adapter = self._adapter_for_source(source)
            _streaming_tts_done = (
                _stts_adapter is not None
                and bool(getattr(_stts_adapter, "_streaming_tts_turn_completed", lambda *_a, **_k: False)(session_key, run_generation))
            )
            if (
                not _streaming_tts_done
                and self._should_send_voice_reply(event, response, agent_messages, already_sent=_already_sent)
            ):
                await self._send_voice_reply(event, response)

            # If streaming already delivered the response, extract and
            # deliver any MEDIA: files before returning None.  Streaming
            # sends raw text chunks that include MEDIA: tags — the normal
            # post-processing in _process_message_background is skipped
            # when already_sent is True, so media files would never be
            # delivered without this.
            #
            # Never skip when the agent failed — the error message is new
            # content the user hasn't seen (streaming only sent earlier
            # partial output before the failure).  Without this guard,
            # users see the agent "stop responding without explanation."
            if agent_result.get("already_sent") and not agent_result.get("failed"):
                if response:
                    _media_adapter = self._adapter_for_source(source)
                    if _media_adapter:
                        await self._deliver_media_from_response(
                            response, event, _media_adapter,
                        )
                # Streaming already delivered the body text, but the footer was
                # intentionally held back (see the `not already_sent` gate above).
                # Send it now as a small trailing message so Telegram/Discord/etc.
                # still surface the runtime metadata on the final reply.
                if _footer_line:
                    try:
                        _foot_adapter = self._adapter_for_source(source)
                        if _foot_adapter:
                            await _foot_adapter.send(
                                source.chat_id,
                                _footer_line,
                                metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)),
                            )
                    except Exception as _e:
                        logger.debug("trailing footer send failed: %s", _e)
                # This branch returns None so the adapter does not send the
                # body twice. /loop and /goal hooks in _handle_message read
                # the return value, so stash the delivered text on the event
                # or those hooks never run and a /loop tick stays awaiting.
                try:
                    event._streamed_final_response = str(response or "")
                except Exception:
                    pass
                return None

            return response

        except Exception as e:
            # Stop typing indicator on error too, retaining Slack thread/workspace
            # routing so a failed turn cannot leave its status visible.
            try:
                _err_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(
                    type(_err_adapter), "_stop_typing_with_metadata", None
                )
                _stop_typing = getattr(type(_err_adapter), "stop_typing", None)
                if _err_adapter and callable(_stop_with_metadata):
                    await _err_adapter._stop_typing_with_metadata(
                        source.chat_id,
                        self._thread_metadata_for_source(
                            source, self._reply_anchor_for_event(event)
                        ),
                    )
                elif _err_adapter and callable(_stop_typing):
                    await _err_adapter.stop_typing(source.chat_id)
            except Exception:
                pass
            logger.exception("Agent error in session %s", session_key)
            # Crash-resilience for failures that happen before AIAgent enters
            # run_conversation() (for example: provider/httpx client init
            # failures). In that path the agent cannot persist the current
            # inbound turn itself, so append the user message here once. If the
            # agent already reached its early turn-start persistence, the latest
            # transcript user row will match and we skip the duplicate.
            try:
                if 'message_text' in locals() and message_text is not None and session_entry is not None:
                    _already_persisted = False
                    try:
                        _recent_transcript = await self.async_session_store.load_transcript(session_entry.session_id)
                    except Exception:
                        _recent_transcript = []
                    for _msg in reversed(_recent_transcript[-10:]):
                        if _msg.get("role") == "user":
                            _expected_user_content = (
                                persist_user_message
                                if persist_user_message is not None
                                else message_text
                            )
                            _already_persisted = (_msg.get("content") == _expected_user_content)
                            break
                    if not _already_persisted:
                        _user_entry = {
                            "role": "user",
                            "content": (
                                persist_user_message
                                if persist_user_message is not None
                                else message_text
                            ),
                            "timestamp": (
                                persist_user_timestamp
                                if persist_user_timestamp is not None
                                else time.time()
                            ),
                        }
                        if 'persist_user_display_kind' in locals() and persist_user_display_kind:
                            _user_entry["display_kind"] = persist_user_display_kind
                        if getattr(event, "message_id", None):
                            _user_entry["message_id"] = str(event.message_id)
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id,
                            _user_entry,
                        )
            except Exception:
                logger.debug("Failed to persist inbound user message after agent exception", exc_info=True)
            # Log full details server-side only; never expose raw exception
            # types or messages to end users (info-leakage risk).
            status_hint = ""
            status_code = getattr(e, "status_code", None)
            _hist_len = len(history) if 'history' in locals() else 0
            if status_code == 401:
                status_hint = " Check your API key or run `claude /login` to refresh OAuth credentials."
            elif status_code == 402:
                status_hint = " Your API balance or quota is exhausted. Check your provider dashboard."
            elif status_code == 429:
                # Check if this is a plan usage limit (resets on a schedule) vs a transient rate limit
                _err_body = getattr(e, "response", None)
                _err_json = {}
                try:
                    if _err_body is not None:
                        _err_json = _err_body.json().get("error", {})
                        if not isinstance(_err_json, dict):
                            _err_json = {}
                except Exception:
                    pass
                if _err_json.get("type") == "usage_limit_reached":
                    _resets_in = _err_json.get("resets_in_seconds")
                    if _resets_in and _resets_in > 0:
                        import math
                        _hours = math.ceil(_resets_in / 3600)
                        status_hint = f" Your plan's usage limit has been reached. It resets in ~{_hours}h."
                    else:
                        status_hint = " Your plan's usage limit has been reached. Please wait until it resets."
                else:
                    status_hint = " You are being rate-limited. Please wait a moment and try again."
            elif status_code == 529:
                status_hint = " The API is temporarily overloaded. Please try again shortly."
            elif status_code in {400, 500}:
                # 400 with a large session is context overflow.
                # 500 with a large session often means the payload is too large
                # for the API to process — treat it the same way.
                if _hist_len > 50:
                    return (
                        "⚠️ Session too large for the model's context window.\n"
                        "Use /compact to compress the conversation, or "
                        "/reset to start fresh."
                    )
                elif status_code == 400:
                    status_hint = " The request was rejected by the API."
            return (
                f"Sorry, I encountered an unexpected error.{status_hint}\n"
                "Try again or use /reset to start a fresh session."
            )
        finally:
            # Restore session context variables to their pre-handler state
            self._clear_session_env(_session_env_tokens)

    def _profile_scope_for_source(self, source: SessionSource):
        """``_profile_runtime_scope`` for ``source``'s profile when a secret scope is required.

        Under multiplexing config/skills/memory resolve to the source profile's home AND credentials
        come from its secret scope (never process-global ``os.environ``). A standalone gateway
        (``multiplex_profiles`` off) still binds once a hosted room has flipped the process-wide
        credential guard — see ``_standalone_launch_scope``."""
        from gateway.run import _profile_runtime_scope
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return _profile_runtime_scope(self._resolve_profile_home_for_source(source))
        return self._standalone_launch_scope()

    @staticmethod
    def _standalone_launch_scope():
        """Scope for a standalone gateway's own (launch-profile) work: a no-op until the process hosts
        another profile home, then the launch profile's OWN runtime scope.

        A native hosted room running a second profile calls
        ``tui_gateway.launch_profile_policy.activate_multi_profile_hosting`` inside the gateway process,
        so ``get_secret`` fails closed for every unscoped read afterwards — including the standalone
        gateway's ordinary turns, which never bound a scope because ``multiplex_profiles`` is off
        (#112878). The launch profile is a profile too: bind its ``.env`` over the env frozen at
        activation (a key injected by systemd / ``op run`` has no file to rebuild it from), never a
        secondary's scope and never live ``os.environ``."""
        from agent.secret_scope import is_multiplex_active
        if not is_multiplex_active():
            return nullcontext()
        from hermes_constants import get_process_hermes_home
        from tui_gateway.launch_profile_policy import launch_profile_runtime_scope
        return launch_profile_runtime_scope(get_process_hermes_home())

    def _media_delivery_scope_for_source(self, source: SessionSource):
        """Home + terminal-policy scope for validating a turn's MEDIA / local-file paths on the
        adapter's delivery side, which runs after the routed turn scope was reset.

        Docker path translation (``platforms/base.py::_translate_docker_container_media_path``)
        infers the producing container from the ACTIVE profile (``get_active_profile_name``) and the
        scope-aware ``TERMINAL_DOCKER_VOLUMES``; without this a secondary's ``MEDIA:/output/x.png``
        resolves against the default profile's sandbox and mounts (#109024). No secret hydration:
        path validation reads no credentials and this runs on the event loop."""
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return nullcontext()
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(self._resolve_profile_home_for_source(source), {})

    def _reset_notice_session_info(self, source: SessionSource) -> str:
        """Session-info block for the auto-reset notice, resolved inside the profile serving ``source``.

        Call via ``asyncio.to_thread``: resolution can block (credential refresh, context-length
        probes), and the scope is entered here so contextvars behave in the worker thread."""
        with self._profile_scope_for_source(source):
            return self._format_session_info()

    def _format_session_info(self) -> str:
        """Model / provider / context-length / endpoint block so users can spot bad context detection."""
        from gateway.run import _resolve_gateway_model_context
        resolved = _resolve_gateway_model_context()
        context_length = resolved.context_length
        ctx_source = {
            "config": "config",
            "default": "default — set model.context_length in config to override",
        }.get(resolved.context_source, "detected")
        ctx_display = (
            f"{context_length / 1_000_000:.1f}M" if context_length >= 1_000_000
            else f"{context_length // 1_000}K" if context_length >= 1_000 else str(context_length)
        )
        lines = [
            f"◆ Model: `{resolved.model}`",
            f"◆ Provider: {resolved.provider or 'openrouter'}",
            f"◆ Context: {ctx_display} tokens ({ctx_source})",
        ]
        if (resolved.provider or "") == "moa":
            # The preset name hides who pays: the aggregator runs every tool-loop step (#112359).
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config
            agg = normalize_moa_config(load_config().get("moa"))["presets"].get(resolved.model, {}).get("aggregator") or {}
            if agg:
                lines.append(f"◆ Acting model (billed for the run): {agg.get('provider')}:{agg.get('model')}")
        base_url = resolved.base_url
        if base_url and base_url_hostname(base_url) in ("localhost", "127.0.0.1", "0.0.0.0"):
            lines.append(f"◆ Endpoint: {base_url}")
        return "\n".join(lines)

    async def _run_background_task(
        self,
        prompt: str,
        source: "SessionSource",
        task_id: str,
        event_message_id: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Profile-scoping wrapper around the background agent task.

        When multiplexing is active, resolve the inbound source's profile and
        run the whole task inside ``_profile_runtime_scope`` so credentials
        resolve from that profile's secret scope. Mirrors the pattern in
        ``_run_agent``.
        """
        from gateway.run import _profile_runtime_scope

        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return await self._run_background_task_inner(
                prompt, source, task_id, event_message_id, media_urls, media_types,
            )

        profile_home = self._resolve_profile_home_for_source(source)
        with _profile_runtime_scope(profile_home):
            return await self._run_background_task_inner(
                prompt, source, task_id, event_message_id, media_urls, media_types,
            )

    def _resolve_enabled_toolsets_for_source(
        self,
        user_config: dict,
        source: "SessionSource",
        platform_key: str,
    ) -> list:
        """Resolve enabled toolsets for an agent run, honoring per-source overrides.

        Asks the receiving adapter for a ``toolsets_for_source()`` override
        (e.g. per-route webhook toolsets). When present, the override list is
        validated through the SAME ``_get_platform_tools`` path as normal
        platform config — by substituting it as the platform's toolset list —
        so unknown names and platform-restricted toolsets are dropped rather
        than trusted. When absent, falls back to standard
        ``platform_toolsets.<platform>`` resolution.
        """
        from hermes_cli.tools_config import _get_platform_tools

        override = None
        try:
            adapter = self._adapter_for_source(source)
            if adapter is not None:
                override = adapter.toolsets_for_source(source)
        except Exception:
            override = None

        if override and isinstance(override, list):
            cfg = dict(user_config)
            pts = dict(cfg.get("platform_toolsets") or {})
            pts[platform_key] = [str(t) for t in override]
            cfg["platform_toolsets"] = pts
            return sorted(_get_platform_tools(cfg, platform_key))

        return sorted(_get_platform_tools(user_config, platform_key))

    def _resolve_turn_toolsets(self, user_config: dict, source: "SessionSource", platform_key: str):
        """``(enabled_toolsets, disabled_toolsets)`` for an agent run on ``source``."""
        from agent.skill_utils import parse_config_string_list
        enabled = self._resolve_enabled_toolsets_for_source(user_config, source, platform_key)
        disabled = parse_config_string_list((user_config.get("agent") or {}).get("disabled_toolsets")) or None
        return enabled, disabled

    async def _run_background_task_inner(
        self,
        prompt: str,
        source: "SessionSource",
        task_id: str,
        event_message_id: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Execute a background agent task and deliver the result to the chat."""
        from gateway.run import (
            _load_gateway_config,
            _platform_config_key,
        )
        from gateway.run import _checkpoint_agent_kwargs, _current_max_iterations, logger

        from run_agent import AIAgent

        media_urls = media_urls or []
        media_types = media_types or []

        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.warning("No adapter for platform %s in background task %s", source.platform, task_id)
            return

        _thread_metadata = self._thread_metadata_for_source(source, event_message_id)

        try:
            user_config = _load_gateway_config()
            model, runtime_kwargs = self._resolve_session_agent_runtime(
                source=source,
                user_config=user_config,
            )
            if not runtime_kwargs.get("api_key"):
                await adapter.send(
                    source.chat_id,
                    f"❌ Background task {task_id} failed: no provider credentials configured.",
                    metadata=_thread_metadata,
                )
                return

            platform_key = _platform_config_key(source.platform)

            enabled_toolsets = self._resolve_enabled_toolsets_for_source(
                user_config, source, platform_key
            )
            agent_cfg = user_config.get("agent") or {}
            from agent.skill_utils import parse_config_string_list

            disabled_toolsets = parse_config_string_list(agent_cfg.get("disabled_toolsets")) or None

            pr = self._provider_routing
            max_iterations = _current_max_iterations()
            reasoning_config = self._resolve_session_reasoning_config(
                source=source, model=model
            )
            self._reasoning_config = reasoning_config
            self._service_tier = self._resolve_session_service_tier(source=source)
            turn_route = self._resolve_turn_agent_config(prompt, model, runtime_kwargs)

            # Enrich the prompt with image descriptions so the background
            # agent can see user-attached images (same as the main flow).
            enriched_prompt = prompt
            if media_urls:
                image_paths = []
                for i, path in enumerate(media_urls):
                    mtype = media_types[i] if i < len(media_types) else ""
                    if mtype.startswith("image/"):
                        image_paths.append(path)
                if image_paths:
                    try:
                        enriched_prompt = await self._enrich_message_with_vision(
                            prompt, image_paths,
                        )
                    except Exception as e:
                        logger.warning("Background task vision enrichment failed: %s", e)

            # Box filled in on the worker thread once the AIAgent exists, so
            # a cancellation that defers this work to the shutdown registry
            # can hand the interrupt hook the live agent.
            agent_holder: dict = {}

            def run_sync():
                agent = AIAgent(
                    model=turn_route["model"],
                    **turn_route["runtime"],
                    **_checkpoint_agent_kwargs(user_config),
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    reasoning_config=reasoning_config,
                    service_tier=self._service_tier,
                    request_overrides=turn_route.get("request_overrides"),
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=task_id,
                    platform=platform_key,
                    user_id=source.user_id,
                    user_id_alt=source.user_id_alt,
                    user_name=source.user_name,
                    chat_id=source.chat_id,
                    chat_name=source.chat_name,
                    chat_type=source.chat_type,
                    thread_id=source.thread_id,
                    session_db=getattr(self._session_db, "_db", self._session_db),
                    # Reload from disk — do not reuse the startup snapshot (#60955).
                    fallback_model=self._refresh_fallback_model(),
                )
                agent_holder["agent"] = agent
                try:
                    return agent.run_conversation(
                        user_message=enriched_prompt,
                        task_id=task_id,
                    )
                finally:
                    self._cleanup_agent_resources(agent)

            # Cancellation-safe executor dispatch: a cancelled /bg coroutine
            # must leave the still-running worker counted as deferred work,
            # not silently drop it from the restart accounting (#77184).
            result = await self._run_background_agent_executor_work(
                run_sync, agent_holder
            )

            response = result.get("final_response", "") if result else ""
            if not response and result and result.get("error"):
                response = f"Error: {result['error']}"

            # Background tasks start a fresh conversation (no prior history),
            # so history_offset=0: every message in the run belongs to this
            # turn. Mirrors the repair on the main turn path.
            if response:
                response = repair_explicit_computer_use_media_paths(
                    response,
                    result.get("messages", []),
                )

            # Extract media files from the response
            if response:
                media_files, response = adapter.extract_media(response)
                from gateway.platforms.base import BasePlatformAdapter
                media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
                images, text_content = adapter.extract_images(response)

                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                header = f'✅ Background task complete\nPrompt: "{preview}"\n\n'

                if text_content:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + text_content,
                        metadata=_thread_metadata,
                    )
                elif not images and not media_files:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + "(No response generated)",
                        metadata=_thread_metadata,
                    )

                # Send extracted images
                for image_url, alt_text in (images or []):
                    try:
                        await adapter.send_image(
                            chat_id=source.chat_id,
                            image_url=image_url,
                            caption=alt_text,
                            metadata=_thread_metadata,
                        )
                    except Exception:
                        pass

                # Send media files, routing each by type so a TTS clip
                # arrives as a voice bubble / a clip as a video rather than
                # a generic document. Mirrors the streaming path.
                from gateway.platforms.base import (
                    should_send_media_as_audio as _should_send_media_as_audio,
                )
                _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
                for media_path, _is_voice in (media_files or []):
                    _ext = os.path.splitext(media_path)[1].lower()
                    try:
                        if _should_send_media_as_audio(source.platform, _ext, _is_voice):
                            await adapter.send_voice(
                                chat_id=source.chat_id,
                                audio_path=media_path,
                                metadata=_thread_metadata,
                                is_voice=_is_voice,
                            )
                        elif _ext in _VIDEO_EXTS:
                            await adapter.send_video(
                                chat_id=source.chat_id,
                                video_path=media_path,
                                metadata=_thread_metadata,
                            )
                        elif _ext in _IMAGE_EXTS:
                            await adapter.send_image_file(
                                chat_id=source.chat_id,
                                image_path=media_path,
                                metadata=_thread_metadata,
                            )
                        else:
                            await adapter.send_document(
                                chat_id=source.chat_id,
                                file_path=media_path,
                                metadata=_thread_metadata,
                            )
                    except Exception:
                        pass
            else:
                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f'✅ Background task complete\nPrompt: "{preview}"\n\n(No response generated)',
                    metadata=_thread_metadata,
                )

        except Exception as e:
            logger.exception("Background task %s failed", task_id)
            try:
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f"❌ Background task {task_id} failed: {e}",
                    metadata=_thread_metadata,
                )
            except Exception:
                pass

    def _mcp_reload_refresh_cached_agents(self, multiplex: bool, profile) -> None:
        """Refresh cached agents so existing sessions see new MCP tools on their next turn without
        a history-destroying ``/new``. Each agent keeps its build-time toolset selection EXACTLY: a
        session built with restricted enabled_toolsets (e.g. ["safe"]) must NOT silently gain tools."""
        from gateway.run import logger

        try:
            from tools.mcp_tool_agent import refresh_agent_mcp_tools
            _cache = getattr(self, "_agent_cache", None)
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            if _cache_lock is None or not _cache:
                return
            # Multiplex: only this profile's sessions (another profile's agent would get this registry).
            _ns_prefix = _session_key_namespace(profile) + ":" if multiplex else None
            with _cache_lock:
                for _sess_key, _entry in list(_cache.items()):
                    if _ns_prefix and not str(_sess_key).startswith(_ns_prefix):
                        continue
                    _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                    if _agent is not None:
                        refresh_agent_mcp_tools(_agent, quiet_mode=True)
        except Exception as _exc:
            logger.debug("Failed to update cached agent tools after MCP reload: %s", _exc)

    async def _execute_mcp_reload(self, event: MessageEvent) -> str:
        """Actually disconnect, reconnect, and notify MCP tool changes.

        Split out from ``_handle_reload_mcp_command`` so the confirmation
        wrapper can invoke the same path whether the user confirmed via
        button, text reply, or has the confirm gate disabled.

        Under multiplex the reload runs inside the requesting profile's
        runtime scope (entered here when the caller — e.g. a button-confirm
        callback — did not), and only that profile's servers are torn down
        and rediscovered (#95518).
        """
        from gateway.run import _profile_runtime_scope, logger

        multiplex = bool(getattr(self.config, "multiplex_profiles", False))
        if multiplex and not get_hermes_home_override():
            profile_home = self._resolve_profile_home_for_source(event.source)
            with _profile_runtime_scope(Path(profile_home)):
                return await self._execute_mcp_reload(event)
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _servers, _lock
            from tools.mcp_tool import _server_scope_keys, reprobe_tool_availability
            from tools.registry import registry

            reload_scope = registry.current_scope_key() if multiplex else None

            def _scoped_server_names() -> set:
                with _lock:
                    return {
                        name for name in _servers
                        if reload_scope is None or _server_scope_keys.get(name) == reload_scope
                    }

            # Capture old server names before shutdown
            old_servers = _scoped_server_names()

            # Read new config before shutting down, so we know what will be added/removed
            # Shutdown existing connections
            await self._run_in_executor_with_context(
                lambda: shutdown_mcp_servers(scope=reload_scope)
            )
            # Explicit reload also re-probes tool availability (check_fn).
            reprobe_tool_availability()

            # Reconnect by discovering tools (reads config.yaml fresh)
            new_tools = await self._run_in_executor_with_context(discover_mcp_tools)

            # Compute what changed
            connected_servers = _scoped_server_names()
            if reload_scope is not None:
                from tools.mcp_tool import _mcp_tool_server_names

                with _lock:
                    new_tools = [
                        n for n in new_tools
                        if _mcp_tool_server_names.get(n) in connected_servers
                    ]

            added = connected_servers - old_servers
            removed = old_servers - connected_servers
            reconnected = connected_servers & old_servers

            lines = [t("gateway.reload_mcp.header")]
            if reconnected:
                lines.append(t("gateway.reload_mcp.reconnected", names=", ".join(sorted(reconnected))))
            if added:
                lines.append(t("gateway.reload_mcp.added", names=", ".join(sorted(added))))
            if removed:
                lines.append(t("gateway.reload_mcp.removed", names=", ".join(sorted(removed))))
            if not connected_servers:
                lines.append(t("gateway.reload_mcp.none_connected"))
            else:
                lines.append(t("gateway.reload_mcp.tools_available", tools=len(new_tools), servers=len(connected_servers)))

            # Refresh cached agents so existing sessions see new MCP tools on
            # their next turn — without this, the user has to `/new` (which
            # discards conversation history) to pick up tools from a server
            # that was just added or reconnected. The user has already
            # consented to the prompt-cache invalidation via the slash-confirm
            # gate in _handle_reload_mcp_command before we reach this point.
            try:
                from tools.mcp_tool import refresh_agent_mcp_tools
                _cache = getattr(self, "_agent_cache", None)
                _cache_lock = getattr(self, "_agent_cache_lock", None)
                if _cache_lock is not None and _cache:
                    # Multiplex: only this profile's sessions. Rebuilding
                    # another profile's agent inside this scope would hand it
                    # this profile's tool registry.
                    _ns_prefix = (
                        _session_key_namespace(event.source.profile) + ":"
                        if multiplex else None
                    )
                    with _cache_lock:
                        for _sess_key, _entry in list(_cache.items()):
                            if _ns_prefix and not str(_sess_key).startswith(_ns_prefix):
                                continue
                            try:
                                _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                            except Exception:
                                continue
                            if _agent is None:
                                continue
                            # Preserve each cached agent's build-time toolset
                            # selection EXACTLY: a gateway session built with a
                            # restricted enabled_toolsets (e.g. ["safe"]) must
                            # NOT silently gain tools after a reload. This is the
                            # opposite of the interactive CLI/TUI /reload-mcp,
                            # which is a single user re-applying their own config
                            # edit; gateway agents are per-session and may be
                            # deliberately locked down. (Contract is asserted by
                            # test_reload_mcp_preserves_per_agent_toolset_overrides.)
                            refresh_agent_mcp_tools(_agent, quiet_mode=True)
            except Exception as _exc:
                logger.debug(
                    "Failed to update cached agent tools after MCP reload: %s",
                    _exc,
                )

            # Inject a message at the END of the session history so the
            # model knows tools changed on its next turn.  Appended after
            # all existing messages to preserve prompt-cache for the prefix.
            change_parts = []
            if added:
                change_parts.append(f"Added servers: {', '.join(sorted(added))}")
            if removed:
                change_parts.append(f"Removed servers: {', '.join(sorted(removed))}")
            if reconnected:
                change_parts.append(f"Reconnected servers: {', '.join(sorted(reconnected))}")
            tool_summary = f"{len(new_tools)} MCP tool(s) now available" if new_tools else "No MCP tools available"
            change_detail = ". ".join(change_parts) + ". " if change_parts else ""
            reload_msg = {
                "role": "user",
                "content": f"[IMPORTANT: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]",
            }
            try:
                session_entry = await self.async_session_store.get_or_create_session(event.source)
                await self.async_session_store.append_to_transcript(
                    session_entry.session_id, reload_msg
                )
            except Exception:
                pass  # Best-effort; don't fail the reload over a transcript write

            return "\n".join(lines)

        except Exception as e:
            logger.warning("MCP reload failed: %s", e)
            return t("gateway.reload_mcp.failed", error=e)

    def _get_proxy_url(self) -> Optional[str]:
        """Proxy URL if proxy mode is configured (GATEWAY_PROXY_URL env wins over ``gateway.proxy_url``).
        Per-profile like GATEWAY_PROXY_KEY: under multiplex a raw environ read would ship a secondary's
        turns (authenticated with ITS scoped key) to the default profile's proxy. Same fallback shape as
        the key — only ``UnscopedSecretError`` (the unscoped default-profile path) reads the env."""
        from gateway.run import _load_gateway_config
        from agent.secret_scope import UnscopedSecretError, get_secret
        try:
            url = (get_secret("GATEWAY_PROXY_URL") or "").strip()
        except UnscopedSecretError:
            url = os.getenv("GATEWAY_PROXY_URL", "").strip()
        if not url:
            url = ((_load_gateway_config().get("gateway") or {}).get("proxy_url") or "").strip()
        return url.rstrip("/") if url else None

    def _build_stream_consumer_config(
        self, source: "SessionSource", scfg: Any, adapter: Any, *, on_missing_cursor: str,
    ) -> "tuple[Any, Optional[Callable[[], None]]]":
        """Build the shared ``StreamConsumerConfig`` and optional Telegram pause-typing closure.
        For non-editing adapters ``on_missing_cursor="fallback"`` streams with an empty cursor;
        ``"raise"`` raises ``RuntimeError`` so the caller skips streaming entirely."""
        from gateway.stream_consumer import StreamConsumerConfig
        _pause_typing_before_finalize = None
        if source.platform == Platform.TELEGRAM and hasattr(adapter, "pause_typing_for_chat"):
            def _pause_typing_before_finalize(_adapter=adapter, _chat_id=source.chat_id) -> None:
                _adapter.pause_typing_for_chat(_chat_id)
        # Non-editing platforms (QQ, WeChat) skip streaming — the partial first message could never
        # be updated — unless they have a native-streaming transport (WeCom msgtype "stream").
        _adapter_supports_edit = getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
        _adapter_supports_native_stream = bool(getattr(adapter, "SUPPORTS_NATIVE_STREAMING", False))
        if not _adapter_supports_edit and not _adapter_supports_native_stream and on_missing_cursor == "raise":
            raise RuntimeError("skip streaming for non-editable platform")
        _effective_cursor = scfg.cursor if _adapter_supports_edit else ""
        # Some Matrix clients render the cursor as tofu: stream text, no cursor.
        _buffer_only = source.platform == Platform.MATRIX
        if _buffer_only:
            _effective_cursor = ""
        # Fresh-final applies to Telegram only (others edit in place cheaply).
        # Fresh-final applies to Telegram only — other platforms either edit in place cheaply (Discord,
        # Slack) or don't have the timestamp-on-edit / edit-timestamp-stays-stale problem. (Ported from
        # openclaw/openclaw#72038.)
        _fresh_final_secs = (
            float(getattr(scfg, "fresh_final_after_seconds", 0.0) or 0.0)
            if source.platform == Platform.TELEGRAM else 0.0
        )
        _consumer_cfg = StreamConsumerConfig(
            edit_interval=scfg.edit_interval, buffer_threshold=scfg.buffer_threshold,
            cursor=_effective_cursor, buffer_only=_buffer_only,
            fresh_final_after_seconds=_fresh_final_secs, transport=scfg.transport or "edit",
            chat_type=getattr(source, "chat_type", "") or "",
        )
        return _consumer_cfg, _pause_typing_before_finalize

    def _run_still_current_fn(self, session_key: Optional[str], run_generation: Optional[int]) -> Callable[[], bool]:
        """Predicate: does this run's generation still own ``session_key``? (always True when untracked)."""
        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)
        return _run_still_current

    @staticmethod
    def _proxy_error_result(text: str) -> Dict[str, Any]:
        return {"final_response": text, "messages": [], "api_calls": 0, "tools": []}

    def _proxy_stream_consumer(self, source: "SessionSource", event_message_id, _thread_metadata, _run_still_current):
        """Platform stream consumer for the proxy path when streaming is enabled, else ``None``."""
        from gateway.run import logger

        from gateway.run import _load_gateway_config, _platform_config_key
        _scfg = getattr(getattr(self, "config", None), "streaming", None)
        # #60671 — streaming TTS consumer is created on the outer event-loop thread before run_sync
        # launches.  run_sync only reads it via ``streaming_tts_consumer_holder[0]`` for delta callback
        # wiring.
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()
        from gateway.display_config import resolve_display_setting
        _plat_streaming = resolve_display_setting(_load_gateway_config(), _platform_config_key(source.platform), "streaming")
        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off" if _plat_streaming is None else bool(_plat_streaming)
        )
        if not _streaming_enabled:
            return None
        try:
            from gateway.stream_consumer import GatewayStreamConsumer
            _adapter = self._delivery_adapter_for(source)
            if not _adapter:
                return None
            _consumer_cfg, _pause_typing_before_finalize = self._build_stream_consumer_config(
                source, _scfg, _adapter, on_missing_cursor="fallback",
            )
            return GatewayStreamConsumer(
                adapter=_adapter, chat_id=source.chat_id, config=_consumer_cfg,
                metadata=_thread_metadata, on_before_finalize=_pause_typing_before_finalize,
                initial_reply_to_id=event_message_id, run_still_current=_run_still_current,
                adapter_provider=lambda: self._delivery_adapter_for(source),
            )
        except Exception as _sc_err:
            logger.debug("Proxy: could not set up stream consumer: %s", _sc_err)
            return None

    async def _run_agent_via_proxy(
        self, message: str, context_prompt: str, history: List[Dict[str, Any]],
        source: "SessionSource", session_id: str, session_key: str = None,
        run_generation: Optional[int] = None, event_message_id: Optional[str] = None,
        scheduled_heartbeat: bool = False,
    ) -> Dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of running a local AIAgent.

        Lets a Docker container handle Matrix E2EE while the agent runs on the host with full
        access to local files, memory, skills, and a unified session store."""
        from gateway.run import logger

        from gateway.run import _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS
        try:
            from aiohttp import ClientSession as _AioClientSession, ClientTimeout
        except ImportError:
            return self._proxy_error_result("⚠️ Proxy mode requires aiohttp. Install with: pip install aiohttp")

        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return self._proxy_error_result("⚠️ Proxy URL not configured (GATEWAY_PROXY_URL or gateway.proxy_url)")

        # The proxy key is a per-profile credential: honor the installed secret scope under multiplex.
        # Only UnscopedSecretError / import failures fall back to the env; any other get_secret()
        # error propagates (same as BASE) rather than silently degrading to the ambient key.
        try:
            from agent.secret_scope import UnscopedSecretError, get_secret

            try:
                proxy_key = (get_secret("GATEWAY_PROXY_KEY") or "").strip()
            except UnscopedSecretError:
                proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()
        except Exception:
            proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()

        _run_still_current = self._run_still_current_fn(session_key, run_generation)

        def _stale_result(what: str) -> Dict[str, Any]:
            logger.info(
                "Discarding stale proxy %s for %s — generation %d is no longer current",
                what, session_key or "?", run_generation or 0,
            )
            return {
                "final_response": "", "messages": [], "api_calls": 0, "tools": [],
                "history_offset": len(history), "session_id": session_id, "response_previewed": False,
            }

        # OpenAI chat format. The remote keeps continuity via X-Hermes-Session-Id; send the current
        # message plus a compact text-only history for a remote that has none yet.
        api_messages: List[Dict[str, str]] = [{"role": "system", "content": context_prompt}] if context_prompt else []
        api_messages += [
            {"role": msg.get("role"), "content": msg.get("content")}
            for msg in history if msg.get("role") in {"user", "assistant"} and msg.get("content")
        ]
        api_messages.append({"role": "user", "content": message})

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if proxy_key:
            headers["Authorization"] = f"Bearer {proxy_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        body = {"model": "hermes-agent", "messages": api_messages, "stream": True}

        _thread_metadata: Optional[Dict[str, Any]] = self._thread_metadata_for_source(source, event_message_id)
        _stream_consumer = (
            None if scheduled_heartbeat
            else self._proxy_stream_consumer(source, event_message_id, _thread_metadata, _run_still_current)
        )
        stream_task = asyncio.create_task(_stream_consumer.run()) if _stream_consumer else None

        _adapter = self._delivery_adapter_for(source)
        if _adapter and not scheduled_heartbeat:
            with suppress(Exception):
                await _adapter.send_typing(source.chat_id, metadata=_thread_metadata)

        full_response = ""
        _start = time.time()
        saw_done = False

        def _consume_sse_line(line: str) -> bool:
            """Parse one SSE line into full_response; True when the terminal ``[DONE]`` was seen.

            Malformed frames (bad JSON, ``choices: [null]``, non-dict deltas) are skipped —
            one bad chunk must not abort the whole stream."""
            nonlocal full_response
            line = line.strip()
            if not line.startswith("data: "):
                return False
            data = line[6:]
            if data.strip() == "[DONE]":
                return True
            try:
                choices = json.loads(data).get("choices") or []
                content = choices[0].get("delta", {}).get("content", "") if choices else ""
            except (json.JSONDecodeError, TypeError, AttributeError, IndexError):
                return False
            if content:
                full_response += content
                if _stream_consumer:
                    _stream_consumer.on_delta(content)
            return False

        try:
            # sock_connect bounds the TCP connect phase so an unreachable proxy host
            # (DNS fail, firewall, remote down) fails fast instead of hanging on the OS default.
            _timeout = ClientTimeout(total=0, sock_read=1800, sock_connect=30)
            async with _AioClientSession(timeout=_timeout) as session:
                async with session.post(f"{proxy_url}/v1/chat/completions", json=body, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning("Proxy error (%d) from %s: %s", resp.status, proxy_url, error_text[:500])
                        return self._proxy_error_result(f"⚠️ Proxy error ({resp.status}): {error_text[:300]}")

                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        if saw_done:
                            # A buggy upstream that holds the connection open after [DONE]
                            # would otherwise block us for up to sock_read seconds.
                            break
                        if not _run_still_current():
                            return _stale_result("stream")
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            if _consume_sse_line(line):
                                saw_done = True
                                break
                        if len(buffer) > _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS:
                            raise ValueError("Proxy SSE stream exceeded max buffer size without a line boundary")
                    # The final SSE frame may not be newline-terminated: flush the residual
                    # buffer after EOF instead of silently dropping its content.
                    if not saw_done and buffer:
                        saw_done = _consume_sse_line(buffer)
                    if not saw_done:
                        # Clean EOF without [DONE] — the upstream dropped the response
                        # mid-stream. Keep any partial text but say so instead of
                        # presenting the truncation as a complete answer.
                        logger.warning(
                            "Proxy SSE stream from %s ended without [DONE] — response may be truncated "
                            "(%d chars received)", proxy_url, len(full_response),
                        )
                        if not full_response:
                            return self._proxy_error_result(
                                "⚠️ Proxy connection closed before the response completed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Proxy connection error to %s: %s", proxy_url, e)
            if not full_response:
                return self._proxy_error_result(f"⚠️ Proxy connection error: {e}")
            # Partial response — return what we got
        finally:
            if _stream_consumer:
                _stream_consumer.finish()
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()

        _elapsed = time.time() - _start
        if not _run_still_current():
            return _stale_result("result")
        logger.info(
            "proxy response: url=%s session=%s time=%.1fs response=%d chars",
            proxy_url, (session_id or "")[:20], _elapsed, len(full_response),
        )
        return {
            "final_response": full_response or "(No response from remote agent)",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": full_response},
            ],
            "api_calls": 1,
            "tools": [],
            "history_offset": len(history),
            "session_id": session_id,
            "response_previewed": _stream_consumer is not None and bool(full_response),
        }

    async def _run_agent(
        self, message: str, context_prompt: str, history: List[Dict[str, Any]],
        source: SessionSource, session_id: str, **turn_kwargs,
    ) -> Dict[str, Any]:
        """Profile-scoping wrapper around ``_run_agent_inner`` (same keyword parameters; pass-through
        when multiplexing is off)."""
        with self._profile_scope_for_source(source):
            return await self._run_agent_inner(message, context_prompt, history, source, session_id, **turn_kwargs)

    @dataclasses.dataclass
    class _RunAgentDisplay:
        """Per-turn display-resolution result: every display/progress/status setting
        ``_run_agent_inner`` resolved for THIS turn, snapshotted so the turn renders
        against its own configuration for its whole lifetime (concurrent turns on
        other platforms cannot change it). ``_DISPLAY_TO_TURN_CTX`` copies the
        turn-facing fields onto the ``TurnContext``."""

        user_config: Any = None
        platform_key: str = ""
        enabled_toolsets: Any = None
        disabled_toolsets: Any = None
        resolve_display_setting: Any = None
        progress_mode: str = "all"
        progress_grouping: str = "accumulate"
        progress_codebox: bool = False
        _display_surface_mode: Any = None
        tool_progress_enabled: bool = False
        # This turn's safely parsed tool_preview_length (0 = unlimited). The
        # renderers read this turn-owned snapshot, never the process-global
        # agent.display budget an interleaved turn may have overwritten.
        tool_preview_max_len: int = 0
        _live_status_mode: str = "off"
        _live_status_adapter_snapshot: Any = None
        _live_status_adapter_provider: Any = None
        log_mode_enabled: bool = False
        log_queue: Any = None
        interim_assistant_messages_enabled: bool = False
        _thinking_enabled: bool = False
        _native_slack_task_cards: bool = False
        needs_progress_queue: bool = False
        _generic_status_phrase: Any = None

    def _run_agent_display_settings(self, source: SessionSource) -> "GatewayRunner._RunAgentDisplay":
        """Resolve per-platform display, progress, status and streaming-surface settings for a turn."""
        from gateway.run import logger

        from gateway.run import (
            _gateway_platform_value, _has_platform_display_override, _load_gateway_config,
            _platform_config_key,
        )
        from gateway.display_config import resolve_display_setting, resolve_tool_progress
        from gateway.status_phrases import choose_status_phrase, resolve_status_phrase_catalog
        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)
        enabled_toolsets, disabled_toolsets = self._resolve_turn_toolsets(user_config, source, platform_key)
        adapter = self._delivery_adapter_for(source)
        # Tool preview length (0 = no limit), snapshotted on the turn below, and
        # friendly tool labels (default on), per-platform. The legacy global write
        # stays for non-turn consumers; interleaved turns read the snapshot.
        tool_preview_max_len = 0
        with suppress(Exception):
            tool_preview_max_len = max(int(resolve_display_setting(
                user_config, platform_key, "tool_preview_length", 0) or 0), 0)
        for _setter, _setting, _default, _cast in (
            ("set_tool_preview_max_len", "tool_preview_length", 0, lambda v: int(v) if v else 0),
            ("set_friendly_tool_labels", "friendly_tool_labels", True, bool),
        ):
            with suppress(Exception):
                from agent import display as _agent_display
                _val = resolve_display_setting(user_config, platform_key, _setting, _default)
                getattr(_agent_display, _setter)(_cast(_val))

        # Resolve the mode and its provenance together: null inherits, tier off is not intent.
        progress_mode, _tool_progress_explicit = resolve_tool_progress(
            user_config, platform_key, os.getenv("HERMES_TOOL_PROGRESS_MODE"),
        )
        # "accumulate" (edit one bubble) or "separate" (one msg per tool)
        progress_grouping = resolve_display_setting(user_config, platform_key, "tool_progress_grouping") or "accumulate"
        # Code-box presentation for accumulated groups (opt-in; plain otherwise)
        progress_codebox = bool(
            resolve_display_setting(user_config, platform_key, "tool_progress_codebox")
        )
        _generic_status_recent: List[str] = []
        _generic_status_catalog = resolve_status_phrase_catalog(user_config, platform_key)

        def _display_surface_mode(
            setting: str, *, default: bool = False,
            require_platform_override_for: set[Any] | None = None, allow_generic: bool = False,
        ) -> str:
            """Return off|raw|generic for a gateway visibility surface."""
            if require_platform_override_for:
                current_platform = _gateway_platform_value(source.platform)
                platform_only = {_gateway_platform_value(item) for item in require_platform_override_for}
                if (
                    current_platform in platform_only
                    and not _has_platform_display_override(user_config, platform_key, setting)
                ):
                    return "off"
            value = resolve_display_setting(user_config, platform_key, setting, default)
            if isinstance(value, str) and value.strip().lower() == "generic":
                return "generic" if allow_generic else "off"
            return "raw" if bool(value) else "off"

        def _generic_status_phrase(kind: str, *, tool_name: str | None = None, preview: str | None = None, args: Any = None) -> str:
            try:
                return choose_status_phrase(
                    kind, tool_name=tool_name, preview=preview, args=args,
                    recent=_generic_status_recent, catalog=_generic_status_catalog,
                )
            except Exception as _phrase_err:
                logger.debug("generic status phrase selection failed: %s", _phrase_err)
                return "still on it" if kind in {"heartbeat", "waiting", "long_running", "status"} else "one sec"

        # Webhooks can't edit messages, so tool progress / log mode are off there.
        is_webhook = source.platform == Platform.WEBHOOK
        tool_progress_enabled = progress_mode not in {"off", "log"} and not is_webhook
        # Live status for text-rendering typing indicators (Slack); independent of tool_progress.
        _live_status_mode = resolve_display_setting(user_config, platform_key, "live_status", "full")
        _live_status_adapter_snapshot = (
            adapter if getattr(adapter, "supports_status_text", False) and _live_status_mode != "off" else None
        )
        def _live_status_adapter_provider():
            if _live_status_mode == "off":
                return None
            a = self._delivery_adapter_for(source)
            return a if getattr(a, "supports_status_text", False) else None
        # "log" mode: tool calls go to ~/.hermes/logs/tool_calls.log instead of the chat. Gateway-only.
        log_mode_enabled = progress_mode == "log" and not is_webhook
        # Interim assistant messages and thinking_progress are independent of tool progress (same
        # queue). Mattermost requires a per-platform opt-in: scratch text leaks into public threads.
        interim_assistant_messages_mode = _display_surface_mode(
            "interim_assistant_messages", default=True,
        )
        interim_assistant_messages_enabled = not is_webhook and interim_assistant_messages_mode != "off"
        _thinking_enabled = _display_surface_mode(
            "thinking_progress", default=False,
        ) != "off"
        # Slack-native task cards need the progress queue even with text tool_progress off.
        # Slack-native task cards (#29483): when the Slack adapter's opt-in is set, tool progress renders as
        # native plan/task cards via chat.startStream — the progress queue is needed even though Slack keeps
        # ordinary text tool_progress off by default (requiring both flags would silently leave the native
        # feature inactive).
        # Cards are still tool progress. Slack's TIER default (``off``) only quiets the text lane so
        # cards stay on for unconfigured installs, but an operator who WRITES ``tool_progress: off``
        # (global, platform override, or legacy overrides) has asked for no tool progress at all and
        # gets no cards either. Every other explicit mode keeps the card lane.
        _native_slack_task_cards = False
        if (
            source.platform == Platform.SLACK
            and hasattr(adapter, "native_task_cards_enabled")
            and not (_tool_progress_explicit and progress_mode == "off")
        ):
            try:
                _native_slack_task_cards = bool(adapter.native_task_cards_enabled())
            except Exception:
                logger.debug("Slack native task-card config check failed", exc_info=True)
        return self._RunAgentDisplay(
            user_config=user_config, platform_key=platform_key, enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets, resolve_display_setting=resolve_display_setting,
            progress_mode=progress_mode, progress_grouping=progress_grouping,
            progress_codebox=progress_codebox,
            _display_surface_mode=_display_surface_mode, tool_preview_max_len=tool_preview_max_len,
            tool_progress_enabled=tool_progress_enabled, _live_status_mode=_live_status_mode,
            _live_status_adapter_snapshot=_live_status_adapter_snapshot,
            _live_status_adapter_provider=_live_status_adapter_provider,
            log_mode_enabled=log_mode_enabled,
            log_queue=queue.Queue() if log_mode_enabled else None,
            interim_assistant_messages_enabled=interim_assistant_messages_enabled,
            _thinking_enabled=_thinking_enabled, _native_slack_task_cards=_native_slack_task_cards,
            needs_progress_queue=tool_progress_enabled or _thinking_enabled or _native_slack_task_cards,
            _generic_status_phrase=_generic_status_phrase,
        )

    # _RunAgentDisplay fields copied verbatim onto the TurnContext.
    _DISPLAY_TO_TURN_CTX = (
        "_live_status_adapter_snapshot", "_live_status_adapter_provider",
        "_live_status_mode", "_thinking_enabled", "progress_mode",
        "progress_grouping", "progress_codebox", "tool_preview_max_len", "tool_progress_enabled", "log_queue",
        "resolve_display_setting", "user_config", "enabled_toolsets", "disabled_toolsets",
        "log_mode_enabled", "interim_assistant_messages_enabled", "needs_progress_queue",
        "_native_slack_task_cards",
    )

    def _run_agent_build_turn_context(
        self, disp: "GatewayRunner._RunAgentDisplay", AIAgent: Any, *, message: str, source: SessionSource,
        session_key: Optional[str], run_generation: Optional[int], **turn_params,
    ) -> Tuple[TurnContext, TurnRunner, Any]:
        """Build the ``TurnContext`` and its ``TurnRunner``; ``turn_params`` (history, context_prompt,
        session_id, persist_user_*, …) are stored verbatim. Returns ``(turn_ctx, turn_runner,
        cleanup_adapter)``."""
        from gateway.run_turn_runner import TurnRunner
        # Discord voice "verbal ack" on the FIRST tool call (discord.voice_fx.enabled): resolve the
        # guild whose voice connection is bound to this text channel (mirrors DiscordAdapter.play_tts).
        _voice_ack_guild: List[Optional[int]] = [None]
        if source.platform == Platform.DISCORD:
            _va = self.adapters.get(Platform.DISCORD)
            _vtc = getattr(_va, "_voice_text_channels", None)
            if isinstance(_vtc, dict) and hasattr(_va, "voice_mixer_active"):
                _voice_ack_guild[0] = next(
                    (_gid for _gid, _tc in _vtc.items() if str(_tc) == str(source.chat_id) and _va.voice_mixer_active(_gid)),
                    None,
                )

        # Auto-cleanup of temporary progress bubbles needs a real ``delete_message`` (getattr on the
        # type: a fake adapter without it means "can't delete", not a crash).
        _cleanup_progress = bool(
            disp.resolve_display_setting(disp.user_config, disp.platform_key, "cleanup_progress")
        )
        _cleanup_adapter = self._delivery_adapter_for(source) if _cleanup_progress else None
        if _cleanup_adapter is not None and getattr(type(_cleanup_adapter), "delete_message", None) in (
            None, BasePlatformAdapter.delete_message,
        ):
            _cleanup_progress = False
            _cleanup_adapter = None

        # The one-slot progress/holder containers shared with the callbacks are TurnContext defaults.
        turn_ctx = TurnContext(
            source=source, message=message, AIAgent=AIAgent, session_key=session_key,
            run_generation=run_generation, _cleanup_progress=_cleanup_progress,
            _run_still_current=self._run_still_current_fn(session_key, run_generation),
            progress_queue=queue.Queue() if disp.needs_progress_queue else None,
            _voice_ack_guild=_voice_ack_guild, _voice_ack_loop=asyncio.get_running_loop(),
            **{name: getattr(disp, name) for name in self._DISPLAY_TO_TURN_CTX}, **turn_params,
        )
        turn_runner = TurnRunner(self, turn_ctx)
        turn_ctx.mute_notification_reply = diagnostic_turn_muted(
            turn_ctx.persist_user_display_metadata, source.platform, turn_ctx.user_config)
        # Agent tool-lifecycle callbacks live on the runner (bound methods, same signatures).
        turn_ctx.progress_callback = turn_runner.progress_callback
        turn_ctx.voice_ack_callback = turn_runner.voice_ack_callback
        turn_ctx.native_tool_start_callback = turn_runner.combined_tool_start_callback
        turn_ctx.native_tool_complete_callback = turn_runner.native_tool_complete_callback
        return turn_ctx, turn_runner, _cleanup_adapter

    def _thread_metadata_for_progress(
        self, source: SessionSource, event_message_id: Optional[str], _progress_thread_id: Any,
        _relay_prospective_thread_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Thread metadata for a progress-lane send; relay Discord auto-thread lane falls back to the reply anchor.

        The connector will auto-thread on the reply anchor (thread is born on its FIRST send), so
        carrying it routes progress / status bubbles into the same thread as the final reply."""
        if not _progress_thread_id:
            metadata = None
        elif _progress_thread_id == source.thread_id:
            metadata = self._thread_metadata_for_source(source, event_message_id)
        else:
            metadata = self._thread_metadata_for_target(
                source.platform, source.chat_id, _progress_thread_id,
                chat_type=getattr(source, "chat_type", None), reply_to_message_id=event_message_id,
            )
        if metadata is None and _relay_prospective_thread_id:
            metadata = {"reply_to_message_id": event_message_id}
        return metadata

    def _run_agent_progress_threading(
        self, source: SessionSource, event_message_id: Optional[str], _native_slack_task_cards: bool
    ) -> Tuple[Optional[dict], Optional[str], Optional[dict]]:
        """Resolve where progress bubbles are threaded (platform-specific).

        Returns ``(progress_metadata, progress_reply_to, status_thread_metadata)``; the latter is
        for status / approval / stream sends (Feishu topics need the triggering message id via the
        reply API, so carry it as a fallback). Slack and Buzz honour the user's reply_in_thread
        opt-out: never synthesise a thread for progress, or every later reply inherits it."""
        from gateway.run import _non_conversational_metadata, _resolve_progress_thread_id
        is_buzz = str(getattr(source.platform, "value", source.platform) or "").lower() == "buzz"
        _progress_reply_in_thread = True
        _adapter = self._delivery_adapter_for(source) if source.platform == Platform.SLACK or is_buzz else None
        if _adapter is not None:
            try:
                if is_buzz:
                    _progress_reply_in_thread = getattr(_adapter, "_reply_to_mode", "first") != "off"
                else:
                    # Relay lane: the adapter owns mode resolution; native lane: flat extra key.
                    _mode_fn = getattr(_adapter, "_effective_reply_in_thread", None)
                    _progress_reply_in_thread = bool(
                        _mode_fn() if callable(_mode_fn) else _adapter.config.extra.get("reply_in_thread", True)
                    )
            except Exception:
                _progress_reply_in_thread = True
        _progress_thread_id = _resolve_progress_thread_id(
            source.platform, source.thread_id, event_message_id, reply_in_thread=_progress_reply_in_thread,
        )
        # Relay Discord auto-thread lane: the connector stamps prospective_thread_id at ingest.
        _relay_prospective_thread_id = (
            str(getattr(source, "prospective_thread_id", None))
            if source.platform == Platform.DISCORD
            and getattr(source, "delivered_via_upstream_relay", False)
            and getattr(source, "prospective_thread_id", None)
            and not source.thread_id
            else None
        )
        _progress_metadata = _non_conversational_metadata(
            self._thread_metadata_for_progress(
                source, event_message_id, _progress_thread_id, _relay_prospective_thread_id,
            ),
            platform=source.platform,
        )
        if _native_slack_task_cards:
            # chat.startStream in channels requires the recipient team/user pair; harmless elsewhere.
            _progress_metadata = dict(_progress_metadata or {})
            if source.scope_id:
                _progress_metadata.setdefault("recipient_team_id", source.scope_id)
                _progress_metadata.setdefault("slack_team_id", source.scope_id)
            if source.user_id:
                _progress_metadata.setdefault("recipient_user_id", source.user_id)
        # Buzz has no native thread_id: thread via reply-to unless the user opted out.
        _progress_reply_to = (
            event_message_id
            if (source.platform in (Platform.FEISHU, ) and source.thread_id and event_message_id)
            or (is_buzz and event_message_id and _progress_reply_in_thread)
            or _relay_prospective_thread_id
            else None
        )
        if source.platform == Platform.FEISHU and source.thread_id and event_message_id:
            _status_thread_metadata = {"thread_id": _progress_thread_id, "reply_to_message_id": event_message_id}
        else:
            _status_thread_metadata = self._thread_metadata_for_progress(
                source, event_message_id, _progress_thread_id, _relay_prospective_thread_id,
            )
        return _progress_metadata, _progress_reply_to, _status_thread_metadata

    async def _run_agent_write_tool_log(self, log_queue: Any) -> None:
        """Drain log_queue and append tool-call lines to tool_calls.log (tool_progress=log).

        RotatingFileHandler (5MB × 3) bounds the log; RedactingFormatter keeps secrets off disk."""
        from gateway.run import logger

        if log_queue is None:
            return
        tool_logger = _tool_call_logger()
        try:
            while True:
                try:
                    tool_logger.info("%s", log_queue.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error("write_tool_log error: %s", e)
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Drain remaining entries so late tool calls from the final iteration aren't lost.
            with suppress(Exception):
                while True:
                    tool_logger.info("%s", log_queue.get_nowait())
            with suppress(Exception):
                for handler in tool_logger.handlers:
                    handler.flush()

    def _run_agent_start_streaming_tts(
        self, source: SessionSource, message_type: Optional[str],
        _status_thread_metadata: Optional[Dict[str, Any]], streaming_tts_consumer_holder: list,
    ) -> None:
        """Start the streaming-TTS consumer for a voice-input turn on an auto-TTS chat.

        Created on the gateway loop thread (not run_sync's executor); an inactive consumer leaves
        the holder None so the whole-file fallback path runs."""
        from gateway.run import logger

        # Skip when streaming TTS already delivered audio for this turn (#60671).
        # This avoids a cross-scope NameError: the outer interrupt / finalisation paths reference the
        # consumer via ``streaming_tts_consumer_holder[0]``. Gates: voice input, auto-TTS enabled for this
        # chat, adapter supports streaming, and a usable streaming TTS provider configured. See #60671.
        _stts_adapter = self._delivery_adapter_for(source)
        _is_voice_input = (
            message_type is not None
            and str(getattr(message_type, "value", message_type)).lower() == "voice"
        )
        if _stts_adapter is None or not _is_voice_input or not _stts_adapter._should_auto_tts_for_chat(source.chat_id):
            return
        try:
            from gateway.streaming_tts_consumer import StreamingTTSConsumer
            from tools.tts_tool import _load_tts_config
            _stts_consumer = StreamingTTSConsumer(
                adapter=_stts_adapter, chat_id=source.chat_id, tts_config=_load_tts_config(),
                loop=self._gateway_loop or asyncio.get_event_loop(),
                metadata=_status_thread_metadata,
            )
            if _stts_consumer.active:
                streaming_tts_consumer_holder[0] = _stts_consumer
                _stts_consumer.start()
        except Exception as _stts_err:
            logger.debug("Could not set up streaming TTS consumer: %s", _stts_err)

    async def _run_agent_stream_consumer_task(self, stream_consumer_holder: list) -> None:
        """Wait (up to 10s) for the stream consumer to be created inside run_sync, then run it."""
        for _ in range(200):
            if stream_consumer_holder[0] is not None:
                await stream_consumer_holder[0].run()
                return
            await asyncio.sleep(0.05)

    @staticmethod
    async def _await_stream_task(stream_task) -> None:
        """Give the stream consumer task 5s to flush, then cancel it."""
        try:
            await asyncio.wait_for(stream_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

    async def _run_agent_track_agent(self, turn_ctx: TurnContext) -> None:
        """Track this agent as running for the session (interrupt support) once it is created — only
        if this run is still current, else leave the newer run's slot alone."""
        from gateway.run import logger

        session_key, run_generation, agent_holder = turn_ctx.session_key, turn_ctx.run_generation, turn_ctx.agent_holder
        while agent_holder[0] is None:
            await asyncio.sleep(0.05)
        if not session_key:
            return
        if run_generation is not None and not self._is_session_run_current(session_key, run_generation):
            logger.info(
                "Skipping stale agent promotion for %s — generation %s is no longer current",
                session_key or "", run_generation,
            )
            return
        self._session_state(session_key).turn.agent = agent_holder[0]
        if self._draining:
            self._update_runtime_status("draining")

    async def _run_agent_fire_pending_interrupt(
        self, adapter: Any, agent: Any, source: SessionSource, session_key: str,
        _interrupt_detected: "asyncio.Event", streaming_tts_consumer_holder: list, *,
        log_context: str, log: Callable[[], None],
    ) -> None:
        """Peek the adapter's pending event, transcribe voice, then signal the agent + abort streaming TTS.

        Peek WITHOUT consuming: the event must stay for the post-run ``_dequeue_pending_event()``
        (popping races the agent finishing). Transcribe BEFORE signaling so voice interrupts carry
        the real transcript."""
        from gateway.run import _build_media_placeholder
        _peek_event = adapter._pending_messages.get(session_key)
        pending_text = None
        if _peek_event is not None:
            pending_text = _peek_event.text or ""
            if self._pending_event_audio_paths(_peek_event):
                pending_text, _ = await self._transcribe_and_echo_pending_voice(
                    _peek_event, adapter, source, pending_text, log_context=log_context,
                    metadata={"thread_id": source.thread_id} if source.thread_id else None,
                )
            elif not pending_text and (getattr(_peek_event, "media_urls", None) or []):
                pending_text = _build_media_placeholder(_peek_event)
        log()
        agent.interrupt(pending_text)
        _interrupt_detected.set()
        # Abort streaming TTS on barge-in.
        # See #60671.
        # See #60671.
        # See #60671.
        # Finalize the streaming-TTS consumer (#60671). finish() is called from the outer event-loop thread
        # (not the executor worker) so early returns from run_sync are also finalised.  wait_complete()
        # drains queued audio; on timeout the consumer is aborted unconditionally — if audio was audible,
        # suppression is preserved so the gateway does not replay from the beginning; if no audio was
        # audible, the whole-file fallback path is permitted.
        _stts = streaming_tts_consumer_holder[0]
        if _stts is not None:
            _stts.abort("barge-in")

    async def _run_agent_monitor_for_interrupt(self, turn_ctx: TurnContext, _interrupt_detected: "asyncio.Event") -> None:
        """Poll the adapter for interrupts (new messages) every 200ms and signal the agent.

        Level 1 (base.py) catches regular text before _handle_message(); the inactivity poll loop
        has a BACKUP check in case this task dies. Keyed by session_key, NOT source.chat_id."""
        from gateway.run import logger

        source, session_key, agent_holder = turn_ctx.source, turn_ctx.session_key, turn_ctx.agent_holder
        streaming_tts_consumer_holder = turn_ctx.streaming_tts_consumer_holder
        if not session_key:
            return
        while True:
            await asyncio.sleep(0.2)
            try:
                # Re-resolve the adapter each iteration so reconnects don't leave a stale reference.
                _adapter = self._delivery_adapter_for(source)
                if not _adapter:
                    continue
                if hasattr(_adapter, 'has_pending_interrupt') and _adapter.has_pending_interrupt(session_key):
                    agent = agent_holder[0]
                    if agent:
                        await self._run_agent_fire_pending_interrupt(
                            _adapter, agent, source, session_key, _interrupt_detected,
                            streaming_tts_consumer_holder, log_context="Voice-interrupt",
                            log=lambda: logger.debug("Interrupt detected from adapter, signaling agent..."),
                        )
                        break
            except asyncio.CancelledError:
                raise
            except Exception as _mon_err:
                logger.debug("monitor_for_interrupt error (will retry): %s", _mon_err)

    async def _run_agent_backup_interrupt_check(
        self, turn_ctx: TurnContext, _interrupt_detected: "asyncio.Event", interrupt_monitor: "asyncio.Task",
    ) -> None:
        """Backup interrupt check: if the monitor task died or missed the interrupt, catch it here."""
        from gateway.run import logger

        source, session_key = turn_ctx.source, turn_ctx.session_key
        if _interrupt_detected.is_set() or not session_key:
            return
        _backup_adapter = self._delivery_adapter_for(source)
        _backup_agent = turn_ctx.agent_holder[0]
        if (_backup_adapter and _backup_agent
                and hasattr(_backup_adapter, 'has_pending_interrupt')
                and _backup_adapter.has_pending_interrupt(session_key)):
            await self._run_agent_fire_pending_interrupt(
                _backup_adapter, _backup_agent, source, session_key, _interrupt_detected,
                turn_ctx.streaming_tts_consumer_holder,
                log_context="Voice-backup-interrupt",
                log=lambda: logger.info(
                    "Backup interrupt detected for session %s (monitor task state: %s)",
                    session_key, "done" if interrupt_monitor.done() else "running",
                ),
            )

    @staticmethod
    def _run_agent_stream_confirmed_final_delivery(consumer, final_text: str, *, previewed: bool = False) -> bool:
        """True only when the actual final reply reached the user: a finalize call may carry only the
        last preview snapshot, so reconcile against the recorded payload — a demonstrable mismatch
        (False) overrides the flag; None keeps legacy trust."""
        if consumer is None:
            return False
        if getattr(consumer, "final_response_sent", False):
            matcher = getattr(consumer, "delivered_final_matches", None)
            if callable(matcher):
                with suppress(Exception):
                    if matcher(final_text) is False:
                        return False
            return True
        # Exact-text match against what the consumer DURABLY delivered (commentary, segments, and the
        # visible prefix only once a real send landed) — safe without the ``previewed`` flag. The codex
        # app-server bridge delivers the final agentMessage through the commentary path and never sets
        # response_previewed (#74248 / #80519); gating on the flag re-sent every such reply. Mismatching
        # commentary still returns False, so a distinct final answer is never suppressed (#65919). Draft
        # frames are ephemeral and must not count: after draft streaming + a failed finalize send this
        # predicate must stay False so the fallback final send still fires (#51828 / #33793).
        has_delivered_text = getattr(consumer, "has_durably_delivered_text", None)
        if callable(has_delivered_text):
            try:
                return bool(has_delivered_text(final_text))
            except Exception:
                return False
        return False

    def _run_agent_start_turn_worker(self, turn_ctx: TurnContext, run_sync: Callable[[], Any]) -> "GatewayRunner._RunAgentWorker":
        """Schedule ``run_sync`` on the executor plus the inactivity watchdog thread.

        *Inactivity* timeout (agent.gateway_timeout / HERMES_AGENT_TIMEOUT, env wins; 0 = unlimited),
        not wall-clock. The daemon watchdog is independent of asyncio: cgroup memory reclaim can
        starve the loop that runs the normal timeout poll."""
        from gateway.run import _float_env, _watch_gateway_turn_inactivity
        from tools.process_registry import process_registry
        agent_holder, session_key, run_generation = turn_ctx.agent_holder, turn_ctx.session_key, turn_ctx.run_generation
        _agent_timeout, _agent_warning = (
            v if v > 0 else None
            for v in (_float_env("HERMES_AGENT_TIMEOUT", 1800), _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900))
        )

        # background=true processes survive a turn: reap only children created by THIS turn on timeout.
        _turn_task_id = turn_ctx.session_id or ""
        # The daemon watchdog is independent of asyncio: cgroup memory reclaim may starve the event loop
        # that runs the normal timeout poll, but it need not also postpone cleanup until the loop recovers
        # (#76115).
        _turn_process_baseline = process_registry.snapshot_running_ids(_turn_task_id)
        turn_ctx.process_task_id = _turn_task_id
        turn_ctx.process_baseline = _turn_process_baseline
        # task_id is session-scoped: gate the reap on this claim still being current so a replacement
        # turn's fresh process isn't killed by this turn's stale baseline.
        worker = self._RunAgentWorker(
            agent_timeout=_agent_timeout, agent_warning=_agent_warning, task_id=_turn_task_id,
            process_baseline=_turn_process_baseline, worker_done=threading.Event(),
            timeout_fired=threading.Event(), cleanup_lock=threading.Lock(),
            is_current=(
                (lambda: self._is_session_run_current(session_key, run_generation))
                if run_generation is not None
                else (lambda: True)
            ),
        )

        def _run_sync_with_timeout_lifecycle():
            try:
                return run_sync()
            finally:
                worker.worker_done.set()
                # `.turn.agent` stays reachable until the *next* turn is claimed; clearing the
                # ownership markers now means a /stop on the finished turn no longer reaps background
                # work it left running.
                # `.turn.agent` on the session state is only reset to _AGENT_PENDING_SENTINEL when the
                # *next* turn is claimed (see _session_state(...).turn.agent = ... at claim time), so a
                # stale reference to this exact agent instance stays reachable from
                # _interrupt_and_clear_session() until then. See #76115.
                _finished_agent = agent_holder[0] if agent_holder else None
                if _finished_agent is not None:
                    _finished_agent._gateway_turn_process_task_id = ""
                    _finished_agent._gateway_turn_process_baseline = frozenset()

        if _agent_timeout is not None:
            threading.Thread(
                target=_watch_gateway_turn_inactivity,
                kwargs={
                    "agent_holder": agent_holder, "timeout": _agent_timeout, "poll_interval": 5.0,
                    **self._reaper_kwargs(worker),
                },
                name=f"gateway-turn-watchdog-{_turn_task_id[:12]}",
                daemon=True,
            ).start()
        worker.executor_task = asyncio.ensure_future(
            self._run_in_executor_with_context(_run_sync_with_timeout_lifecycle)
        )
        return worker

    @staticmethod
    def _reaper_kwargs(worker: "GatewayRunner._RunAgentWorker") -> dict:
        """Shared kwargs of the watchdog + timeout-reaper threads."""
        return {
            **{k: getattr(worker, k) for k in ("task_id", "process_baseline", "worker_done", "timeout_fired", "cleanup_lock")},
            "is_still_current": worker.is_current,
        }

    @staticmethod
    def _agent_activity_summary(agent: Any) -> dict:
        """``agent.get_activity_summary()`` or ``{}`` when unavailable / failing."""
        if agent and hasattr(agent, "get_activity_summary"):
            with suppress(Exception):
                return agent.get_activity_summary()
        return {}

    async def _run_agent_inactivity_warning(self, worker, source, _status_thread_metadata) -> None:
        """Staged one-shot warning before the inactivity timeout escalates."""
        from gateway.run import logger

        from gateway.run import _interim_metadata
        _warn_adapter = self._delivery_adapter_for(source)
        if not _warn_adapter:
            return
        try:
            await _warn_adapter.emit_warning(
                source.chat_id, f"⚠️ I seem to be stuck (no activity for {int(worker.agent_warning // 60) or 1} min). "
                "If nothing happens in the next "
                f"{int((worker.agent_timeout - worker.agent_warning) // 60) or 1} min I'll give up on this task. "
                "You can keep waiting, send /stop to cancel it, or /new to start a fresh conversation.",
                metadata=_interim_metadata(_status_thread_metadata), logical_platform=source.platform,
            )
        except Exception as _warn_err:
            logger.debug("Inactivity warning send error: %s", _warn_err)

    def _run_agent_timeout_result(self, worker, turn_ctx: TurnContext) -> dict:
        """Synthetic failed run dict for an inactivity timeout, with the activity-tracker diagnostic;
        interrupts the agent if it is still running so the thread pool worker is freed."""
        from gateway.run import logger

        from gateway.run import _INTERRUPT_REASON_TIMEOUT, _INTERRUPT_TOOL_REASON_TIMEOUT, request_hard_interrupt
        session_key, result_holder, tools_holder = turn_ctx.session_key, turn_ctx.result_holder, turn_ctx.tools_holder
        _timed_out_agent = turn_ctx.agent_holder[0]
        _activity = self._agent_activity_summary(_timed_out_agent)
        _last_desc = _activity.get("last_activity_desc", "unknown")
        _secs_ago = _activity.get("seconds_since_activity", 0)
        _cur_tool = _activity.get("current_tool")
        _iter_n = _activity.get("api_call_count", 0)
        _iter_max = _activity.get("max_iterations", 0)
        # Operator-facing log keeps the raw resolved value; only the user-facing lines hide the sentinel.
        logger.error(
            "Agent idle for %.0fs (timeout %.0fs) in session %s "
            "| last_activity=%s | iteration=%s/%s | tool=%s",
            _secs_ago, worker.agent_timeout, session_key, _last_desc, _iter_n, _iter_max,
            _cur_tool or "none",
        )
        if _timed_out_agent:
            request_hard_interrupt(_timed_out_agent, _INTERRUPT_REASON_TIMEOUT, tool_reason=_INTERRUPT_TOOL_REASON_TIMEOUT)
        _timeout_mins = int(worker.agent_timeout // 60) or 1
        _iter_progress = format_iteration_progress(_iter_n, _iter_max)
        _diag_lines = [
            f"⏱️ Agent inactive for {_timeout_mins} min — no tool calls or API responses."
        ]
        if _cur_tool:
            _diag_lines.append(
                f"The agent appears stuck on tool `{_cur_tool}` ({_secs_ago:.0f}s since last "
                f"activity, {_iter_progress})."
            )
        else:
            _diag_lines.append(
                f"Last activity: {_last_desc} ({_secs_ago:.0f}s ago, "
                f"{_iter_progress}). "
                "The agent may have been waiting on an API response."
            )
        _diag_lines.append(
            "To increase the limit, set agent.gateway_timeout in config.yaml (value in seconds, 0 "
            "= no limit) and restart the gateway.\nTry again, or use /reset to start fresh."
        )
        return {
            "final_response": "\n".join(_diag_lines),
            "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
            "api_calls": _iter_n,
            "tools": tools_holder[0] or [],
            "history_offset": 0,
            "failed": True,
        }

    async def _run_agent_await_turn_worker(
        self, worker: "GatewayRunner._RunAgentWorker", turn_ctx: TurnContext,
        _interrupt_detected: "asyncio.Event", interrupt_monitor: "asyncio.Task",
    ) -> Any:
        """Poll the executor future (inactivity timeout + backup interrupt checks); return its result,
        or a synthetic failed run dict on inactivity timeout. Polls even with an unlimited timeout
        so the backup interrupt check runs if monitor_for_interrupt() silently died."""
        from gateway.run import _abandon_timed_out_gateway_turn
        agent_holder = turn_ctx.agent_holder
        _warning_fired = False
        while True:
            done, _ = await asyncio.wait({worker.executor_task}, timeout=5.0)
            if done:
                # Prefer the real result even if the watchdog fired in the same window (the run already
                # persisted its reply).
                return worker.executor_task.result()
            if worker.agent_timeout is not None:
                if worker.timeout_fired.is_set():
                    break
                _idle_secs = self._agent_activity_summary(agent_holder[0]).get("seconds_since_activity", 0.0)
                if not _warning_fired and worker.agent_warning is not None and _idle_secs >= worker.agent_warning:
                    _warning_fired = True
                    await self._run_agent_inactivity_warning(worker, turn_ctx.source, turn_ctx._status_thread_metadata)
                if _idle_secs >= worker.agent_timeout:
                    threading.Thread(
                        target=_abandon_timed_out_gateway_turn,
                        kwargs={"agent_holder": agent_holder, **self._reaper_kwargs(worker)},
                        name=f"gateway-turn-reaper-{worker.task_id[:12]}", daemon=True,
                    ).start()
                    break
            await self._run_agent_backup_interrupt_check(turn_ctx, _interrupt_detected, interrupt_monitor)
        return self._run_agent_timeout_result(worker, turn_ctx)

    def _run_agent_evict_on_fallback(self, turn_ctx: TurnContext) -> None:
        """Evict the cached agent when a fallback model activated on a SUCCESSFUL run (so /model shows
        the active model and the next message retries the primary). Skip failed runs: evicting
        would loop bad model → fallback → evict → recreate."""
        from gateway.run import _resolve_gateway_model
        session_key = turn_ctx.session_key
        _agent = turn_ctx.agent_holder[0]
        _result_for_fb = turn_ctx.result_holder[0]
        if _agent is None or not hasattr(_agent, 'model') or (_result_for_fb and _result_for_fb.get("failed")):
            return
        _cfg_model = _resolve_gateway_model()
        # Normalize as AIAgent.__init__ does (vendor prefix stripped on native providers), else the
        # cached agent is evicted every turn, destroying prompt caching.
        with suppress(Exception):
            from hermes_cli.model_normalize import _AGGREGATOR_PROVIDERS, normalize_model_for_provider
            _agent_provider = getattr(_agent, 'provider', '') or ''
            if _agent_provider and _agent_provider not in _AGGREGATOR_PROVIDERS:
                _cfg_model = normalize_model_for_provider(_cfg_model, _agent_provider)
        if _agent.model != _cfg_model and not self._is_intentional_model_switch(session_key, _agent, _cfg_model):
            self._evict_cached_agent(session_key)

    async def _run_agent_finalize_streaming_tts(self, turn_ctx: TurnContext, adapter: Any) -> None:
        """Finalize the streaming-TTS consumer on the outer event-loop thread (covers early returns
        from run_sync). On drain timeout abort to free the task — audible streams keep whole-file
        suppression, silent streams stay eligible for the whole-file fallback."""
        from gateway.run import logger

        _stts = turn_ctx.streaming_tts_consumer_holder[0]
        if _stts is None:
            return
        _stts.finish()
        try:
            await _stts.wait_complete(timeout=10.0)
        except Exception as _stts_done_err:
            logger.debug("streaming TTS wait_complete error: %s", _stts_done_err)
        if not _stts.done:
            _stts.abort("streaming TTS finalisation timeout")
            await _stts.wait_complete(timeout=2.0)
        if _stts.suppress_whole_file and adapter is not None:
            _mark_turn = getattr(adapter, "_mark_streaming_tts_completed_turn", None)
            if callable(_mark_turn):
                _mark_turn(turn_ctx.session_key, turn_ctx.run_generation)

    async def _run_agent_drain_pending(
        self, result: Any, adapter: Any, source: SessionSource, session_key: Optional[str]
    ) -> Tuple[Any, Optional[str]]:
        """Dequeue the adapter's pending / interrupt / leftover-steer follow-up as ``(pending_event, pending)``.

        Keyed by session_key (not source.chat_id) to match the adapter's storage keys."""
        from gateway.run import logger

        from gateway.run import (
            _build_media_placeholder, _dequeue_pending_event, _is_control_interrupt_message
        )
        pending_event = None
        pending = None
        if result and adapter and session_key:
            pending_event = _dequeue_pending_event(adapter, session_key)
            # /queue overflow: promote the next queued event into the consumed "next-up" slot so the
            # recursive drain sees it (keeps FIFO order; a mid-chain /queue can't jump the queue).
            pending_event = self._promote_queued_event(session_key, adapter, pending_event)
            if result.get("interrupted") and not pending_event and result.get("interrupt_message"):
                interrupt_message = result.get("interrupt_message")
                if _is_control_interrupt_message(interrupt_message):
                    logger.info(
                        "Ignoring control interrupt message for session %s: %s",
                        session_key or "?", interrupt_message,
                    )
                else:
                    pending = interrupt_message
            elif pending_event:
                # Transcribe audio BEFORE it becomes the next user turn (real transcript, not a path).
                _pending_text = pending_event.text or ""
                if self._pending_event_audio_paths(pending_event):
                    pending, _ = await self._transcribe_and_echo_pending_voice(
                        pending_event, adapter, source, _pending_text, log_context="Voice-drain",
                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                    )
                    pending = pending or _build_media_placeholder(pending_event)
                else:
                    pending = _pending_text or _build_media_placeholder(pending_event)
                if pending:
                    logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])

        # Leftover /steer (arrived after the last tool batch): deliver as the next user turn.
        if result and not pending and not pending_event and result.get("pending_steer"):
            pending = result.get("pending_steer")
            logger.debug("Delivering leftover /steer as next turn: '%s...'", pending[:40])

        # Safety net: a pending slash command is never passed to the agent as user input.
        if pending and pending.strip().startswith("/"):
            _pending_cmd_word = pending.strip().split(None, 1)[0][1:].lower()
            if _pending_cmd_word:
                with suppress(Exception):
                    from hermes_cli.commands import resolve_command as _rc_pending
                    if _rc_pending(_pending_cmd_word):
                        logger.info(
                            "Discarding command '/%s' from pending queue — "
                            "commands must not be passed as agent input", _pending_cmd_word,
                        )
                        pending_event = None
                        pending = None

        if self._draining and (pending_event or pending):
            logger.info(
                "Discarding pending follow-up for session %s during gateway %s",
                session_key or "?", self._status_action_label(),
            )
            pending_event = None
            pending = None
        return pending_event, pending

    async def _run_agent_deliver_first_response(
        self, turn_ctx: TurnContext, adapter: Any, response: Any, result: Any, stream_task: Any,
    ) -> None:
        """Deliver the first response before a queued follow-up runs, unless streaming already did."""
        from gateway.run import logger

        if turn_ctx.mute_notification_reply:
            return
        session_key = turn_ctx.session_key
        _sc = turn_ctx.stream_consumer_holder[0]
        if _sc and stream_task:
            try:
                await self._await_stream_task(stream_task)
            except Exception as e:
                logger.debug("Stream consumer wait before queued message failed: %s", e)
        # Delivery uses the finalized task result (empty/failure normalization), not raw ``result``.
        _delivery_result = response if isinstance(response, dict) else (result or {})
        first_response = _delivery_result.get("final_response", "")
        _already_streamed = self._run_agent_stream_confirmed_final_delivery(
            _sc, first_response, previewed=bool(_delivery_result.get("response_previewed")),
        )
        # Same silence predicate as the normal path, else this branch leaks the literal marker.
        if self._is_intentional_silence(_delivery_result, first_response):
            if is_machinery_display_kind(turn_ctx.persist_user_display_kind):
                logger.info(
                    "Queued follow-up for session %s: suppressing intentional silence marker before continuing.",
                    session_key or "?",
                )
                first_response = ""
            else:
                logger.warning(
                    "Queued follow-up for session %s: replacing a human-turn silence marker.",
                    session_key or "?",
                )
                first_response = _UNEXPECTED_SILENCE_REPLY
                _already_streamed = False
        if first_response:
            logger.info(
                "Queued follow-up for session %s: final text delivery confirmed; delivering explicit media before continuing."
                if _already_streamed else
                "Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.",
                session_key or "?",
            )
            try:
                await self._deliver_queued_first_response(
                    first_response, source=turn_ctx.source, adapter=adapter,
                    metadata=turn_ctx._status_thread_metadata, event_message_id=turn_ctx.event_message_id,
                    text_already_delivered=_already_streamed,
                    deliver_media=not _delivery_result.get("failed"), stream_consumer=_sc,
                    # The text send records a delivery-ledger obligation under this key, keyed on
                    # the raw inbound id (the anchor above is only the reply target).
                    session_key=session_key, inbound_message_id=turn_ctx.inbound_message_id,
                )
            except Exception as e:
                logger.warning("Failed to send first response before queued message: %s", e)
        # Release deferred bg-review notifications: pop (no double-fire in base.py's finally) and call.
        _bg_cb = self._pop_post_delivery_callback(adapter, session_key, turn_ctx.run_generation)
        if callable(_bg_cb):
            with suppress(Exception):
                _bg_result = _bg_cb()
                if inspect.isawaitable(_bg_result):
                    await _bg_result

    async def _run_agent_queued_followup(
        self, turn_ctx: TurnContext, adapter: Any, pending: Optional[str], pending_event: Any,
        response: Any, result: Any, stream_task: Any,
    ) -> Any:
        """Run the queued / interrupting follow-up as the next turn (recursive ``_run_agent``)."""
        from gateway.run import logger

        from gateway.platforms.base import merge_pending_message_event
        from gateway.run import _preserve_queued_followup_history_offset
        source, session_id, session_key, run_generation = (
            turn_ctx.source, turn_ctx.session_id, turn_ctx.session_key, turn_ctx.run_generation,
        )
        _interrupt_depth, history, _status_thread_metadata = (
            turn_ctx._interrupt_depth, turn_ctx.history, turn_ctx._status_thread_metadata,
        )
        logger.debug("Processing pending message: '%s...'", pending[:40])

        # Clear the interrupt event so the recursive _run_agent isn't re-interrupted (infinite loop).
        _active = getattr(adapter, "_active_sessions", None) if adapter else None
        if _active and session_key and session_key in _active:
            _active[session_key].clear()

        # Cap recursion depth (user keeps sending while the agent keeps failing).
        # (#816)
        if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
            logger.warning(
                "Interrupt recursion depth %d reached for session %s — "
                "queueing message instead of recursing.", _interrupt_depth, session_key,
            )
            adapter = self._delivery_adapter_for(source)
            if adapter and pending_event:
                merge_pending_message_event(adapter._pending_messages, session_key, pending_event)
            elif adapter and hasattr(adapter, 'queue_message'):
                adapter.queue_message(session_key, pending)
            return turn_ctx.result_holder[0] or {"final_response": response, "messages": history}

        # Interrupted: discard the response ("Operation interrupted." is noise).
        if not result.get("interrupted"):
            await self._run_agent_deliver_first_response(turn_ctx, adapter, response, result, stream_task)

        updated_history = result.get("messages", history)
        next_source, next_message, next_session_key = source, pending, session_key
        # message_type is carried into the recursive call so queued voice turns can stream TTS.
        next_message_id = next_channel_prompt = next_message_type = None
        # The raw inbound id keys the delivery-ledger obligation for the follow-up's own final send,
        # distinct from the reply anchor above (None in forum topics). Carry it or two chained
        # topic turns with the same text would collide on one obligation id (queued-final-ledger).
        next_inbound_id = None
        # Queued Discord turns carry the same routing note as first turns; persist the authored text.
        next_persist_message = None
        next_display_kind = display_kind_for_event(pending_event)
        # See #60671.
        if pending_event is not None:
            next_source = getattr(pending_event, "source", None) or source
            if self._is_goal_continuation_event(pending_event) and not self._goal_still_active_for_session(session_id):
                logger.info(
                    "Discarding stale goal continuation for session %s — goal is no longer active",
                    session_key or "?",
                )
                return result
            # Resolve the follow-up's session key BEFORE preparing the inbound text: native image
            # paths are buffered under the key given and consumed under next_session_key.
            try:
                next_session_key = self._session_key_for_source(next_source)
            except Exception:
                logger.debug(
                    "Queued follow-up session-key resolution failed; reusing %s",
                    session_key or "?", exc_info=True,
                )
            next_message = await self._prepare_profile_scoped_inbound_message_text(
                event=pending_event, source=next_source, history=updated_history, session_key=next_session_key,
            )
            if next_message is None:
                return result
            from gateway.run_inbound import strip_discord_triggering_note
            next_persist_message = strip_discord_triggering_note(pending_event, next_message)
            next_message_id = self._reply_anchor_for_event(pending_event)
            next_inbound_id = str(pending_event.message_id) if getattr(pending_event, "message_id", None) else None
            next_channel_prompt = getattr(pending_event, "channel_prompt", None)
            next_message_type = getattr(pending_event, "message_type", None)

        # Clear the prior turn's streaming-TTS completion marker so the recursive turn isn't suppressed.
        # See #60671.
        _clear_adapter = self._delivery_adapter_for(source)
        _completed_turns = getattr(_clear_adapter, "_streaming_tts_completed_turns", None)
        _prior_key = getattr(_clear_adapter, "_streaming_tts_turn_key", None)
        if _completed_turns is not None and callable(_prior_key) and session_key and run_generation is not None:
            _pk = _prior_key(session_key, run_generation)
            if _pk:
                _completed_turns.discard(_pk)

        # Restart the typing indicator; the outer typing task may be stale.
        if _clear_adapter:
            with suppress(Exception):
                await _clear_adapter.send_typing(source.chat_id, metadata=_status_thread_metadata)

        # Re-baseline the cached agent's message_count before recursing, else the coherence guard
        # rebuilds on OUR OWN flushed rows (the outer handler re-baselines only after the chain).
        # Re-baseline the cached agent's message_count snapshot before recursing into the in-band queued
        # (/queue) follow-up turn. The first turn has completed and flushed its own user + assistant rows to
        # the SessionDB, so the cross-process coherence guard (#45966) — which this recursive _run_agent
        # call re-enters — would otherwise see the grown on-disk count against the stale build-time snapshot
        # and rebuild the agent on THIS process's OWN writes, destroying the prompt-cache prefix #46237 was
        # merged to preserve. The existing re-baseline in _handle_message_with_agent only runs after the
        # whole _run_agent chain unwinds — too late for the in-band follow-up. Use the same (session_key,
        # session_id) the recursive call runs under so the snapshot matches exactly what the follow-up's
        # guard will consult. Fail-safe in helper.
        # Acknowledge the follow-up the way an idle-session message is: this in-band drain is the only
        # place a queued/interrupting message ever runs, so base.py's hook site is never entered for it.
        # Resolve the adapter from the follow-up's OWN source — a multiplexed gateway can route it to a
        # different profile's adapter, and only that instance holds the per-message reaction state.
        from gateway.run_turn_followup_ack import _followup_cancel_outcome, _run_followup_processing_hook
        _hook_adapter = self._intake_adapter_for(next_source) if pending_event is not None else None
        await _run_followup_processing_hook(_hook_adapter, pending_event, "on_processing_start")
        # The re-baseline sits inside the try: a /stop landing on its DB await must still close the marker
        # (the helper's own ``except Exception`` does not catch cancellation).
        try:
            await self._refresh_agent_cache_message_count(session_key, session_id)

            followup_result = await self._run_agent(
                message=next_message, context_prompt=turn_ctx.context_prompt, history=updated_history,
                source=next_source, session_id=session_id, session_key=next_session_key,
                run_generation=run_generation, _interrupt_depth=_interrupt_depth + 1,
                event_message_id=next_message_id, inbound_message_id=next_inbound_id,
                channel_prompt=next_channel_prompt, message_type=next_message_type,
                persist_user_message=next_persist_message,
                persist_user_display_kind=next_display_kind,
                persist_user_display_metadata=diagnostic_metadata(pending_event) or None,
            )
        except asyncio.CancelledError:
            await _run_followup_processing_hook(
                _hook_adapter, pending_event, "on_processing_complete", _followup_cancel_outcome(_hook_adapter))
            raise
        except BaseException:
            await _run_followup_processing_hook(
                _hook_adapter, pending_event, "on_processing_complete", ProcessingOutcome.FAILURE)
            raise
        await _run_followup_processing_hook(
            _hook_adapter, pending_event, "on_processing_complete", ProcessingOutcome.SUCCESS)
        merged = _preserve_queued_followup_history_offset(result, followup_result)
        # The TERMINAL turn of the chain owns the ledger identity for the outer final send, which
        # the adapter brackets against the event that OPENED the chain. Without this the terminal
        # reply is recorded under the first message's id, so a first reply that was refused (flood
        # control) has its outstanding row replaced and marked delivered by an identical-text
        # terminal reply, and is never redelivered. A deeper recursion has already set its own id,
        # so only fill the key while it is still absent: the innermost turn wins.
        if isinstance(merged, dict) and "queued_terminal_inbound_id" not in merged:
            merged = {
                **merged,
                "queued_terminal_inbound_id": next_inbound_id,
                "queued_terminal_display_kind": next_display_kind,
                "queued_terminal_notification_category": (
                    (pending_event.metadata or {}).get("notification_category", "result")
                    if pending_event is not None and pending_event.internal else "result"),
            }
        return merged

    async def _run_agent_cleanup_turn_tasks(
        self, turn_ctx: TurnContext, *, progress_task: Any, log_task: Any, interrupt_monitor: "asyncio.Task",
        _notify_task: "asyncio.Task", tracking_task: "asyncio.Task", stream_task: Any,
    ) -> None:
        """``finally`` half of a turn: cancel background tasks, flush stream, release the session slot."""
        from gateway.run import logger

        stream_consumer_holder, session_key = turn_ctx.stream_consumer_holder, turn_ctx.session_key
        for task in (progress_task, log_task, interrupt_monitor, _notify_task):
            if task:
                task.cancel()

        if stream_task:
            # No stream consumer was created: nothing to flush, cancel instead of waiting out 5s.
            if not (stream_consumer_holder and stream_consumer_holder[0] is not None):
                stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stream_task
            else:
                await self._await_stream_task(stream_task)

        # Abort + bounded wait for streaming TTS: covers paths where normal finalisation was skipped.
        _stts_finally = turn_ctx.streaming_tts_consumer_holder[0]
        # See #60671.
        if _stts_finally is not None and not _stts_finally.done:
            _stts_finally.abort("cleanup")
            with suppress(Exception):
                await _stts_finally.wait_complete(timeout=2.0)

        tracking_task.cancel()
        if session_key:
            # Release the slot only if this run's generation still owns it (/stop or /new may have
            # installed its own state).
            self._release_running_agent_state(session_key, run_generation=turn_ctx.run_generation)
        if self._draining:
            self._update_runtime_status("draining")

        for task in (progress_task, log_task, interrupt_monitor, tracking_task, _notify_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # A background task that died of a real error must not abort the cleanup path.
                    logger.debug("background turn task failed during cleanup", exc_info=True)

    async def _run_agent_edit_streamed_message(
        self, _sc, source, response, content, *, _sk, ok, fail_result, fail_exc,
    ) -> None:
        """Edit the stream consumer's message in place with ``content``; on success mark
        ``response["already_sent"]`` and log ``ok``. ``fail_result`` (None = trust the call) logs a
        returned failure as ``(session, error)``; ``fail_exc`` logs an exception as ``(session, exc)``."""
        from gateway.run import logger

        try:
            _res = await _sc.adapter.edit_message(
                chat_id=source.chat_id, message_id=_sc.message_id, content=content, finalize=True,
            )
        except Exception as _edit_err:
            logger.warning(fail_exc, _sk, _edit_err)
            return
        if fail_result is not None and not getattr(_res, "success", True):
            logger.warning(fail_result, _sk, getattr(_res, "error", None))
            return
        response["already_sent"] = True
        logger.info(*ok)

    async def _run_agent_mark_streamed_delivery(self, response: Any, turn_ctx: TurnContext) -> None:
        """Set ``response["already_sent"]`` when streaming already delivered the final reply.

        Never when the agent failed (the error is unseen content) or on "(empty)". Both suppression
        flags reflect call success, not content, so reconcile against the recorded turn-final
        payload: a mismatch (False, incl. payload-less split delivery) never suppresses; None (no
        record) keeps legacy trust."""
        from gateway.run import logger

        _sc, source, session_key = turn_ctx.stream_consumer_holder[0], turn_ctx.source, turn_ctx.session_key
        if not isinstance(response, dict) or response.get("failed"):
            return
        _final = response.get("final_response") or ""
        _is_empty_sentinel = not _final or _final == "(empty)"
        # response_previewed: only suppress if that EXACT text was delivered, not unrelated commentary.
        # Unrelated commentary/progress must not be mistaken for the final response (#14238).
        _previewed = bool(response.get("response_previewed"))
        _content_delivered = bool(_sc and getattr(_sc, "final_content_delivered", False))
        # #71643: a *successful* finalize edit can still carry only the last preview snapshot — deltas
        # generated between that edit and stream completion never reach any API call, and both suppression
        # flags are set from the call's success rather than its content. Reconcile the consumer's recorded
        # turn-final payload against the completed response: on a demonstrable mismatch (False) neither
        # final_response_sent nor final_content_delivered may suppress the normal final send. False also
        # covers payload-less multi-message split delivery (#78541). None (no record on a non-split legacy
        # path) keeps legacy trust; the failed-finalize family (#51828 / #33793) is unaffected because those
        # paths leave the flags False or record the complete fallback payload.
        _stale_finalized = False
        if _content_delivered and not _is_empty_sentinel:
            _matcher = getattr(_sc, "delivered_final_matches", None)
            if callable(_matcher):
                with suppress(Exception):
                    _stale_finalized = _matcher(_final) is False
            if _stale_finalized:
                _content_delivered = False
        # Plugin hooks may append content after streaming finished — then send the final version.
        _transformed = bool(response.get("response_transformed"))
        # Suppress the normal send only when the actual final reply reached the user.
        _streamed = self._run_agent_stream_confirmed_final_delivery(_sc, _final, previewed=_previewed)
        if _is_empty_sentinel:
            return
        _sk = session_key or "?"
        if not _transformed and (_streamed or _content_delivered):
            logger.info(
                "Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).",
                _sk, _streamed, _previewed, _content_delivered,
            )
            response["already_sent"] = True
        elif not _transformed and _stale_finalized and _sc is not None:
            # Stale finalize: edit the streamed message up to the complete response (on failure the
            # normal send delivers). Not for split delivery — message_id is only the LAST chunk.
            _sc_msg_id = _sc.message_id
            if getattr(_sc, "_turn_split_delivery", False):
                logger.info(
                    "Stale streamed finalize detected for session %s on a multi-message split; skipping the in-place reconciliation edit and delivering the complete response via normal final send (#78541).",
                    _sk,
                )
            elif _sc_msg_id and _sc_msg_id != "__no_edit__" and getattr(_sc, "adapter", None) is not None:
                await self._run_agent_edit_streamed_message(
                    _sc, source, response, _final, _sk=_sk,
                    ok=("Reconciled stale streamed finalize for session %s: edited message %s with the complete response (#71643).", _sk, _sc_msg_id),
                    fail_result="Stale-finalize reconciliation edit failed for session %s (%s); sending complete response via normal final send.",
                    fail_exc="Stale-finalize reconciliation edit failed for session %s: %s; sending complete response via normal final send.",
                )
            else:
                logger.info(
                    "Stale streamed finalize detected for session %s with no editable message; delivering complete response via normal final send (#71643).",
                    _sk,
                )
        elif _transformed and _sc is not None:
            # Transformed after streaming: edit the streamed message instead of sending a duplicate.
            if _sc.message_id:
                await self._run_agent_edit_streamed_message(
                    _sc, source, response, response["final_response"], _sk=_sk,
                    ok=("Edited streamed message %s for session %s to include plugin-transformed content.", _sc.message_id, _sk),
                    fail_result=None, fail_exc="Failed to edit streamed message for session %s: %s",
                )
        elif _sc is not None and getattr(_sc, "stream_deltas_enabled", True):
            # DUPLICATE-RISK DIAGNOSTIC: a stream consumer existed but suppression did NOT fire; log
            # the decision inputs ("signal never set" vs "ack-pending race"). Skipped for consumers
            # never fed the final's deltas (interim-only wiring, #105341) — they cannot have raced
            # the normal final send, so the warning would be a guaranteed false positive.
            logger.warning(
                "Normal final-send NOT suppressed despite active stream consumer for session %s: "
                "streamed=%s previewed=%s content_delivered=%s transformed=%s final_len=%d — "
                "possible duplicate send (see wecom ack-timeout RCA).",
                _sk, _streamed, _previewed, _content_delivered, _transformed, len(_final),
            )

    def _run_agent_schedule_bubble_cleanup(self, response: Any, _cleanup_adapter: Any, turn_ctx: TurnContext) -> None:
        """Schedule deletion of tracked temporary progress bubbles after the final response lands.

        Failed runs keep them as breadcrumbs. Only on adapters with ``delete_message``; failures swallowed."""
        from gateway.run import logger

        from gateway.run import safe_schedule_threadsafe
        _cleanup_msg_ids, session_key = turn_ctx._cleanup_msg_ids, turn_ctx.session_key
        if not (
            turn_ctx._cleanup_progress
            and _cleanup_adapter is not None
            and _cleanup_msg_ids
            and session_key
            and isinstance(response, dict)
            and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            return
        _ids_snapshot = list(_cleanup_msg_ids)
        _chat_id_snapshot = turn_ctx.source.chat_id
        _loop_snapshot = asyncio.get_running_loop()

        def _cleanup_temp_bubbles() -> None:
            async def _delete_all() -> None:
                for _mid in _ids_snapshot:
                    with suppress(Exception):
                        await _cleanup_adapter.delete_message(_chat_id_snapshot, _mid)
            with suppress(Exception):
                safe_schedule_threadsafe(
                    _delete_all(), _loop_snapshot, logger=logger,
                    log_message="Temp bubble cleanup scheduling error",
                )

        try:
            _cleanup_adapter.register_post_delivery_callback(
                session_key, _cleanup_temp_bubbles, generation=turn_ctx.run_generation,
            )
        except Exception as _rpe:
            logger.debug("Post-delivery cleanup registration failed: %s", _rpe)

    def _run_agent_bind_turn_wiring(
        self, turn_ctx: TurnContext, turn_runner: TurnRunner, source: SessionSource,
        event_message_id: Optional[str], _native_slack_task_cards: bool,
    ) -> Optional[Dict[str, Any]]:
        """Resolve progress threading, then publish progress metadata and the sync→async bridges onto
        ``turn_ctx`` (the one-slot holders shared with run_sync's executor thread are TurnContext
        defaults). Returns ``_status_thread_metadata``."""
        turn_ctx._progress_metadata, turn_ctx._progress_reply_to, _status_thread_metadata = (
            self._run_agent_progress_threading(source, event_message_id, _native_slack_task_cards)
        )
        # Bridges: sync step/event/status callbacks → async hooks.emit and adapter.send.
        turn_ctx._loop_for_step = asyncio.get_running_loop()
        turn_ctx._hooks_ref = self.hooks
        turn_ctx._step_callback_sync = turn_runner._step_callback_sync
        turn_ctx._event_callback_sync = turn_runner._event_callback_sync
        turn_ctx._status_callback_sync = turn_runner._status_callback_sync
        turn_ctx._status_adapter = self._delivery_adapter_for(source)
        turn_ctx._status_adapter_provider = lambda: self._delivery_adapter_for(source)
        turn_ctx._status_chat_id = source.chat_id
        turn_ctx._status_thread_metadata = _status_thread_metadata
        return _status_thread_metadata

    async def _run_agent_notify_long_running(
        self, disp: "GatewayRunner._RunAgentDisplay", turn_ctx: TurnContext, _executor_task_holder: list,
    ) -> None:
        """Periodic "still working" heartbeat, edited in place where supported. Stops once this run
        no longer owns the session slot or the executor finished. ``_executor_task_holder[0]`` is
        bound just after this task is scheduled (reads as None until then).

        Interval: agent.gateway_notify_interval / HERMES_AGENT_NOTIFY_INTERVAL (default 180s; 0 or
        long_running_notifications=off disables). The interval is measured from the turn's last
        USER-VISIBLE progress (delivered tool updates, commentary, successful message edits and
        streamed output — see TurnContext.mark_visible_progress), so continuous visible output
        suppresses the heartbeat and a genuinely quiet turn still hears from us after one full
        interval of silence."""
        from gateway.run import logger

        from gateway.run import (
            _float_env, _interim_metadata, _non_conversational_metadata,
            _sleep_heartbeat_silence_deadline,
        )
        _notify_start = time.time()
        _NOTIFY_INTERVAL = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        _long_running_mode = disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True)
        if _NOTIFY_INTERVAL <= 0 or _long_running_mode == "off":
            return
        source, session_key, agent_holder = turn_ctx.source, turn_ctx.session_key, turn_ctx.agent_holder
        _status_thread_metadata = turn_ctx._status_thread_metadata
        _notify_adapter = self._delivery_adapter_for(source)
        if not _notify_adapter:
            return
        _heartbeat_msg_id: Optional[str] = None
        _heartbeat_sent_text: Optional[str] = None
        # Silence clock origin: the later of the turn start, the last visible
        # progress and the last heartbeat tick (shared wait helper in gateway.run).
        _silence_origin = [time.monotonic()]

        while True:
            await _sleep_heartbeat_silence_deadline(_NOTIFY_INTERVAL, turn_ctx, _silence_origin)
            # A consumed tick (heartbeat attempt or a suppressed skip) restarts the
            # silence clock so this loop never fires faster than one interval.
            _silence_origin[0] = time.monotonic()
            if not self._should_emit_long_running_notification(
                session_key, agent_holder[0], _executor_task_holder[0]
            ):
                break
            _elapsed_mins = int((time.time() - _notify_start) // 60)
            # Terse heartbeat by default; the iteration counter is gated on busy_ack_detail.
            _status_detail = ""
            _want_iteration_detail = bool(
                disp.resolve_display_setting(disp.user_config, disp.platform_key, "busy_ack_detail", True)
            )
            _a = self._agent_activity_summary(agent_holder[0])
            with suppress(Exception):
                if _a:
                    _parts = []
                    if _want_iteration_detail:
                        _parts.append(format_iteration_progress(_a["api_call_count"], _a["max_iterations"]))
                    _action = _a.get("current_tool") or _a.get("last_activity_desc")
                    if _action:
                        _parts.append(str(_action))
                    if _parts:
                        _status_detail = " — " + ", ".join(_parts)
            _heartbeat_text = (
                disp._generic_status_phrase("status")
                if _long_running_mode == "generic"
                else f"⏳ Working — {_elapsed_mins} min{_status_detail}"
            )
            if _heartbeat_msg_id is not None and _heartbeat_sent_text == _heartbeat_text:
                continue
            try:
                _notify_res = None
                # The turn's delivery lock is the ordering boundary between the
                # heartbeat and actual visible progress: while we hold it, no
                # same-turn tool/commentary/stream transport call can interleave,
                # so progress cannot land mid-edit and make this heartbeat stale
                # before it is even sent.
                async with turn_ctx.delivery_lock():
                    # Revalidate under the guard: same-turn visible progress
                    # that landed while this tick waited for the lock restarted
                    # the silence clock - skip the whole tick (the next attempt
                    # comes a full interval after that delivery).
                    _lp_now = turn_ctx.last_visible_progress_monotonic()
                    if _lp_now is not None and _lp_now > _silence_origin[0]:
                        continue
                    if not self._should_emit_long_running_notification(
                        session_key, agent_holder[0], _executor_task_holder[0]
                    ):
                        break
                    if _heartbeat_msg_id:
                        try:
                            _notify_res = await _notify_adapter.edit_message(source.chat_id, _heartbeat_msg_id, _heartbeat_text)
                        except Exception as _ee:
                            logger.debug("Heartbeat edit failed: %s", _ee)
                            _notify_res = None
                if _notify_res is not None and getattr(_notify_res, "success", False):
                    _heartbeat_sent_text = _heartbeat_text
                    continue
                # The edit failed (or none existed): RELEASE the lock and
                # yield so same-turn progress queued behind the failed edit
                # lands FIRST, then REACQUIRE and revalidate everything - a
                # delivery that landed invalidates the fresh-bubble fallback
                # and the next tick comes a full interval after it.
                await asyncio.sleep(0)
                async with turn_ctx.delivery_lock():
                    _lp_now = turn_ctx.last_visible_progress_monotonic()
                    if _lp_now is not None and _lp_now > _silence_origin[0]:
                        continue
                    # A drain/restart notice may have gone out meanwhile, and
                    # a fresh "Working" bubble after it reads as a contradiction (#10990).
                    if not self._should_emit_long_running_notification(
                        session_key, agent_holder[0], _executor_task_holder[0]
                    ):
                        break
                    _notify_res = await _notify_adapter.send(
                        source.chat_id, _heartbeat_text,
                        metadata=_interim_metadata(_non_conversational_metadata(_status_thread_metadata, platform=source.platform)),
                    )
                    if getattr(_notify_res, "success", False) and getattr(_notify_res, "message_id", None):
                        _heartbeat_msg_id = str(_notify_res.message_id)
                        if turn_ctx._cleanup_progress:
                            turn_ctx._cleanup_msg_ids.append(_heartbeat_msg_id)
                        _heartbeat_sent_text = _heartbeat_text
            except Exception as _ne:
                logger.debug("Long-running notification error: %s", _ne)

    async def _run_agent_inner(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        inbound_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
        moa_config: Optional[dict] = None,
        persist_user_message: Optional[Any] = None,
        persist_user_timestamp: Optional[float] = None,
        persist_user_display_kind: Optional[str] = None,
        message_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.

        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool

        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        from gateway.run import (
            _INTERRUPT_REASON_TIMEOUT,
            _abandon_timed_out_gateway_turn,
            _build_media_placeholder,
            _dequeue_pending_event,
            _float_env,
            _interim_metadata,
            _is_control_interrupt_message,
            _load_gateway_config,
            _non_conversational_metadata,
            _platform_config_key,
            _preserve_queued_followup_history_offset,
            _resolve_gateway_model,
            _resolve_progress_thread_id,
            _watch_gateway_turn_inactivity,
            merge_pending_message_event,
            request_hard_interrupt,
        )
        from gateway.run import logger

        # ---- Proxy mode: delegate to remote API server ----
        if self._get_proxy_url():
            return await self._run_agent_via_proxy(
                message=message,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=event_message_id,
            )

        from run_agent import AIAgent
        import queue

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)

        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)

        enabled_toolsets = self._resolve_enabled_toolsets_for_source(
            user_config, source, platform_key
        )
        agent_cfg_local = user_config.get("agent") or {}
        from agent.skill_utils import parse_config_string_list

        disabled_toolsets = parse_config_string_list(agent_cfg_local.get("disabled_toolsets")) or None

        display_config = user_config.get("display", {})
        if not isinstance(display_config, dict):
            display_config = {}

        # Per-platform display settings — resolve via display_config module
        # which checks display.platforms.<platform>.<key> first, then
        # display.<key> global, then built-in platform defaults.
        from gateway.display_config import resolve_display_setting

        # Apply tool preview length config (0 = no limit). The global stays for
        # legacy non-turn consumers; the turn snapshots the resolved budget so
        # interleaved turns (Discord unlimited vs a capped platform) never
        # change each other's rendering mid-flight.
        _tool_preview_max_len = 0
        try:
            from agent.display import set_tool_preview_max_len
            _tpl = resolve_display_setting(user_config, platform_key, "tool_preview_length", 0)
            _tool_preview_max_len = max(int(_tpl) if _tpl else 0, 0)
            set_tool_preview_max_len(_tool_preview_max_len)
        except Exception:
            pass

        # Apply friendly tool labels config (default on) — per-platform aware
        try:
            from agent.display import set_friendly_tool_labels
            _ftl = resolve_display_setting(user_config, platform_key, "friendly_tool_labels", True)
            set_friendly_tool_labels(bool(_ftl))
        except Exception:
            pass

        # Tool progress mode — resolved per-platform with env var fallback
        _resolved_tp = resolve_display_setting(user_config, platform_key, "tool_progress")
        _env_tp = os.getenv("HERMES_TOOL_PROGRESS_MODE")
        _display_cfg = display_config if isinstance(display_config, dict) else {}
        _platforms_cfg = _display_cfg.get("platforms") or {}
        _platform_cfg = _platforms_cfg.get(platform_key) or {}
        _legacy_tp_overrides = _display_cfg.get("tool_progress_overrides") or {}
        _tool_progress_configured = (
            "tool_progress" in _display_cfg
            or (
                isinstance(_platform_cfg, dict)
                and "tool_progress" in _platform_cfg
            )
            or (
                isinstance(_legacy_tp_overrides, dict)
                and platform_key in _legacy_tp_overrides
            )
        )
        progress_mode = (
            _env_tp
            if _env_tp and not _tool_progress_configured
            else (_resolved_tp or _env_tp or "all")
        )
        # Tool progress grouping: "accumulate" (edit one bubble) or "separate" (one msg per tool)
        progress_grouping = resolve_display_setting(user_config, platform_key, "tool_progress_grouping") or "accumulate"
        # Code-box presentation for accumulated groups (opt-in; plain otherwise)
        _progress_codebox = bool(
            resolve_display_setting(user_config, platform_key, "tool_progress_codebox")
        )
        from gateway.status_phrases import (
            choose_status_phrase,
            format_phase_heartbeat,
            resolve_status_phrase_catalog,
        )
        _generic_status_recent: List[str] = []
        _generic_status_catalog = resolve_status_phrase_catalog(user_config, platform_key)

        def _display_surface_mode(
            setting: str,
            *,
            default: bool = False,
            require_platform_override_for: set[Any] | None = None,
            allow_generic: bool = False,
        ) -> str:
            """Return off|raw|generic|phase for a gateway visibility surface."""
            from gateway.run import (
                _gateway_platform_value,
                _has_platform_display_override,
            )
            if require_platform_override_for:
                current_platform = _gateway_platform_value(source.platform)
                platform_only = {
                    _gateway_platform_value(item)
                    for item in require_platform_override_for
                }
                if (
                    current_platform in platform_only
                    and not _has_platform_display_override(user_config, platform_key, setting)
                ):
                    return "off"
            value = resolve_display_setting(user_config, platform_key, setting, default)
            if isinstance(value, str):
                val = value.strip().lower()
                if val in {"generic", "phase"}:
                    return val if allow_generic else "off"
            return "raw" if bool(value) else "off"

        def _generic_status_phrase(kind: str, *, tool_name: str | None = None, preview: str | None = None, args: Any = None) -> str:
            try:
                return choose_status_phrase(
                    kind,
                    tool_name=tool_name,
                    preview=preview,
                    args=args,
                    recent=_generic_status_recent,
                    catalog=_generic_status_catalog,
                )
            except Exception as _phrase_err:
                logger.debug("generic status phrase selection failed: %s", _phrase_err)
                return "still on it" if kind in {"heartbeat", "waiting", "long_running", "status"} else "one sec"
        # Disable tool progress for webhooks - they don't support message editing,
        # so each progress line would be sent as a separate message.
        from gateway.config import Platform
        tool_progress_enabled = progress_mode not in {"off", "log"} and source.platform != Platform.WEBHOOK
        # Live working-state status for text-rendering typing indicators
        # (Slack's assistant status line). Independent of tool_progress —
        # Slack defaults tool_progress off (permanent lines spam channels)
        # but the status line is ephemeral, so live status stays useful
        # there. Rendering rides the existing _keep_typing refresh: the
        # callback only stores a phrase on the adapter, costing zero extra
        # platform API calls.
        _live_status_mode = resolve_display_setting(
            user_config, platform_key, "live_status", "full"
        )
        _live_status_adapter = self._adapter_for_source(source)
        if not getattr(_live_status_adapter, "supports_status_text", False):
            _live_status_adapter = None
        if _live_status_mode == "off":
            _live_status_adapter = None
        # "log" mode: tool calls are written to ~/.hermes/logs/tool_calls.log
        # instead of the chat (#3459 / #3458). Gateway-only by design.
        log_mode_enabled = progress_mode == "log" and source.platform != Platform.WEBHOOK
        log_queue: "queue.Queue | None" = queue.Queue() if log_mode_enabled else None
        # Natural assistant status messages are intentionally independent from
        # tool progress and token streaming. Users can keep tool_progress quiet
        # in chat platforms while opting into concise mid-turn updates.
        interim_assistant_messages_mode = _display_surface_mode(
            "interim_assistant_messages",
            default=True,
        )
        interim_assistant_messages_enabled = (
            source.platform != Platform.WEBHOOK
            and interim_assistant_messages_mode != "off"
        )
        # thinking_progress is independent — if enabled, we need the progress
        # queue even when tool_progress is off (thinking relay uses same infra).
        # Mattermost requires a per-platform opt-in: global scratch-text display
        # is too easy to leak into busy public threads.
        _thinking_mode = _display_surface_mode(
            "thinking_progress",
            default=False,
        )
        _thinking_enabled = _thinking_mode != "off"
        # Slack-native task cards (#29483): when the Slack adapter's opt-in
        # is set, tool progress renders as native plan/task cards via
        # chat.startStream — the progress queue is needed even though Slack
        # keeps ordinary text tool_progress off by default (requiring both
        # flags would silently leave the native feature inactive).
        _progress_adapter_for_native = self._adapter_for_source(source)
        _native_slack_task_cards = False
        if (
            source.platform == Platform.SLACK
            and _progress_adapter_for_native is not None
            and hasattr(_progress_adapter_for_native, "native_task_cards_enabled")
        ):
            try:
                _native_slack_task_cards = bool(
                    _progress_adapter_for_native.native_task_cards_enabled()
                )
            except Exception:
                logger.debug("Slack native task-card config check failed", exc_info=True)
        needs_progress_queue = (
            tool_progress_enabled or _thinking_enabled or _native_slack_task_cards
        )


        # Queue for progress messages (thread-safe)
        progress_queue = queue.Queue() if needs_progress_queue else None
        # Structured tool-stage embeds (moa_ask / moa_debate): subscribe
        # to agent.moa_loop's stage-event bus only when this turn's adapter
        # can render stage embeds (Discord today) AND the user hasn't turned
        # tool progress off. On every other platform no subscriber exists,
        # so the MoA tools' stage reports are silent no-ops and the existing
        # accumulated-progress behavior is untouched.
        _stage_embed_adapter = None
        if tool_progress_enabled and source.platform == Platform.DISCORD:
            _stage_candidate = self._adapter_for_source(source)
            if _stage_candidate is not None and all(
                hasattr(_stage_candidate, _attr)
                for _attr in ("send_tool_stage_embed", "edit_tool_stage_embed")
            ):
                _stage_embed_adapter = _stage_candidate
        stage_event_queue = queue.Queue() if _stage_embed_adapter is not None else None
        last_tool = [None]  # Mutable container for tracking in closure
        last_progress_msg = [None]  # Track last message for dedup
        repeat_count = [0]  # How many times the same message repeated
        # True when the previously enqueued progress line was a terminal
        # fenced code block — consecutive terminal calls then drop the
        # repeated "💻 terminal" header and render back-to-back blocks.
        last_was_terminal_block = [False]

        # ── Discord voice "verbal ack before tool calls" ────────────────
        # When the bot is in a voice channel with the continuous mixer
        # installed (discord.voice_fx.enabled), speak a short phrase ("let me
        # look into that") over the ambient idle bed on the FIRST tool call of
        # the turn.  Fires from tool_start_callback (independent of the
        # tool-progress text gate), at most once per turn.  No-op on every
        # other platform / when not in a voice channel.
        _voice_ack_fired = [False]
        _voice_ack_guild: List[Optional[int]] = [None]
        if source.platform == Platform.DISCORD:
            _va = self.adapters.get(Platform.DISCORD)
            # source.chat_id is the linked text channel; resolve the guild whose
            # voice connection is bound to it (mirrors DiscordAdapter.play_tts).
            _vtc = getattr(_va, "_voice_text_channels", None)
            if isinstance(_vtc, dict) and hasattr(_va, "voice_mixer_active"):
                for _gid, _tc in _vtc.items():
                    if str(_tc) == str(source.chat_id) and _va.voice_mixer_active(_gid):
                        _voice_ack_guild[0] = _gid
                        break
        _voice_ack_loop = asyncio.get_running_loop()

        # voice_ack_callback extracted to TurnRunner.voice_ack_callback
        # (published onto turn_ctx after the runner is constructed below).

        # Auto-cleanup of temporary progress bubbles (Telegram + any adapter
        # that implements ``delete_message``). When enabled via
        # ``display.platforms.<platform>.cleanup_progress: true``, message IDs
        # from the tool-progress / "⏳ Working — N min" / status-callback bubbles
        # are collected here and deleted after the final response lands.
        # Failed runs skip cleanup so the bubbles remain as breadcrumbs.
        _cleanup_progress = bool(
            resolve_display_setting(user_config, platform_key, "cleanup_progress")
        )
        _cleanup_adapter = self._adapter_for_source(source) if _cleanup_progress else None
        # getattr, not attribute access — same duck-typed-adapter guard as the
        # edit_message check in send_progress_messages below: a fake/minimal
        # adapter without delete_message means "can't delete", not a crash.
        _cleanup_delete = getattr(type(_cleanup_adapter), "delete_message", None) if _cleanup_adapter is not None else None
        if _cleanup_adapter is not None and (
            _cleanup_delete is None
            or _cleanup_delete is BasePlatformAdapter.delete_message
        ):
            # Adapter doesn't support deletion — silently disable.
            _cleanup_progress = False
            _cleanup_adapter = None
        _cleanup_msg_ids: List[str] = []
        # First-touch onboarding latch: fires at most once per run, even if
        # several tools exceed the threshold.
        long_tool_hint_fired = [False]
        _LONG_TOOL_THRESHOLD_S = 30.0

        def _live_status_adapter_provider():
            if _live_status_mode == "off":
                return None
            a = self._adapter_for_source(source)
            return a if getattr(a, "supports_status_text", False) else None

        turn_ctx = TurnContext(
            source=source,
            _run_still_current=_run_still_current,
            _live_status_adapter_snapshot=_live_status_adapter,
            _live_status_adapter_provider=_live_status_adapter_provider,
            _live_status_mode=_live_status_mode,
            _thinking_enabled=_thinking_enabled,
            progress_mode=progress_mode,
            progress_grouping=progress_grouping,
            progress_codebox=_progress_codebox,
            tool_preview_max_len=_tool_preview_max_len,
            tool_progress_enabled=tool_progress_enabled,
            progress_queue=progress_queue,
            log_queue=log_queue,
            stage_event_queue=stage_event_queue,
            _stage_embed_adapter=_stage_embed_adapter,
            last_progress_msg=last_progress_msg,
            last_tool=last_tool,
            last_was_terminal_block=last_was_terminal_block,
            repeat_count=repeat_count,
            long_tool_hint_fired=long_tool_hint_fired,
            _LONG_TOOL_THRESHOLD_S=_LONG_TOOL_THRESHOLD_S,
            _cleanup_progress=_cleanup_progress,
            _cleanup_msg_ids=_cleanup_msg_ids,
            message=message,
            AIAgent=AIAgent,
            resolve_display_setting=resolve_display_setting,
            user_config=user_config,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            log_mode_enabled=log_mode_enabled,
            interim_assistant_messages_enabled=interim_assistant_messages_enabled,
            needs_progress_queue=needs_progress_queue,
            _native_slack_task_cards=_native_slack_task_cards,
            _voice_ack_fired=_voice_ack_fired,
            _voice_ack_guild=_voice_ack_guild,
            _voice_ack_loop=_voice_ack_loop,
            history=history,
            context_prompt=context_prompt,
            channel_prompt=channel_prompt,
            session_id=session_id,
            session_key=session_key,
            run_generation=run_generation,
            _interrupt_depth=_interrupt_depth,
            event_message_id=event_message_id,
            inbound_message_id=inbound_message_id,
            moa_config=moa_config,
            persist_user_message=persist_user_message,
            persist_user_timestamp=persist_user_timestamp,
            persist_user_display_kind=persist_user_display_kind,
        )
        turn_runner = TurnRunner(self, turn_ctx)
        # Callback invoked by agent on tool lifecycle events — extracted to
        # TurnRunner.progress_callback (bound method, same signature).
        turn_ctx.progress_callback = turn_runner.progress_callback
        turn_ctx.voice_ack_callback = turn_runner.voice_ack_callback
        turn_ctx.native_tool_start_callback = turn_runner.combined_tool_start_callback
        turn_ctx.native_tool_complete_callback = (
            turn_runner.native_tool_complete_callback
        )

        # Background task to send progress messages
        # Accumulates tool lines into a single message that gets edited.
        #
        # Threading metadata is platform-specific:
        # - Slack DM threading needs event_message_id fallback (reply thread)
        # - Telegram forum topics use message_thread_id; Hermes-created private
        #   DM topic lanes require both thread metadata and a reply anchor
        # - Feishu only honors reply_in_thread when sending a reply, so topic
        #   progress uses the triggering event message as the reply target
        # - Other platforms should use explicit source.thread_id only
        #
        # Slack honours platforms.slack.extra.reply_in_thread=false: if the
        # user has opted out of threaded replies, don't synthesise a thread
        # for progress messages either — the very first progress message
        # would otherwise create a thread that all subsequent replies
        # (including the final answer) would inherit (#18859).
        _progress_reply_in_thread = True
        if source.platform == Platform.SLACK:
            _slack_adapter_for_progress = self._adapter_for_source(source)
            if _slack_adapter_for_progress is not None:
                try:
                    # Relay lane: the adapter owns mode resolution (nested
                    # platforms.relay.extra.slack subset with flat-key
                    # fallback). Native lane: read the flat extra as before.
                    _mode_fn = getattr(
                        _slack_adapter_for_progress,
                        "_effective_reply_in_thread",
                        None,
                    )
                    if callable(_mode_fn):
                        _progress_reply_in_thread = bool(_mode_fn())
                    else:
                        _progress_reply_in_thread = bool(
                            _slack_adapter_for_progress.config.extra.get(
                                "reply_in_thread", True
                            )
                        )
                except Exception:
                    _progress_reply_in_thread = True
        elif str(getattr(source.platform, "value", source.platform) or "").lower() == "buzz":
            # Buzz honours the same opt-out (reply_to_mode: off /
            # extra.reply_in_thread: false). When the user asked for flat
            # channel replies, progress must not synthesise a thread either.
            _buzz_adapter_for_progress = self._adapter_for_source(source)
            if _buzz_adapter_for_progress is not None:
                try:
                    _progress_reply_in_thread = (
                        getattr(_buzz_adapter_for_progress, "_reply_to_mode", "first")
                        != "off"
                    )
                except Exception:
                    _progress_reply_in_thread = True
        _progress_thread_id = _resolve_progress_thread_id(
            source.platform, source.thread_id, event_message_id,
            reply_in_thread=_progress_reply_in_thread,
        )
        # Relay Discord auto-thread lane: a channel-initiating message has no
        # thread_id at ingest (the thread is born on the connector's FIRST
        # send). The connector stamps prospective_thread_id (the anchor message
        # id, == the id of the thread it will create) and auto-threads any
        # outbound carrying that anchor as reply_to. Without it, the progress /
        # tool-status bubble is sent flat (no thread, no anchor) and lands in
        # the PARENT channel while the final reply threads — the search-status
        # updates leaked outside the thread (staging repro 2026-08-02). Carry
        # the anchor on the progress send so it routes into the SAME auto-thread.
        _relay_prospective_thread_id = (
            str(getattr(source, "prospective_thread_id", None))
            if source.platform == Platform.DISCORD
            and getattr(source, "delivered_via_upstream_relay", False)
            and getattr(source, "prospective_thread_id", None)
            and not source.thread_id
            else None
        )
        _progress_metadata = (
            self._thread_metadata_for_source(source, event_message_id)
            if _progress_thread_id == source.thread_id
            else self._thread_metadata_for_target(
                source.platform,
                source.chat_id,
                _progress_thread_id,
                chat_type=getattr(source, "chat_type", None),
                reply_to_message_id=event_message_id,
            )
        ) if _progress_thread_id else None
        if _progress_metadata is None and _relay_prospective_thread_id:
            # No real thread yet, but the connector will auto-thread on the
            # reply anchor; carry it so progress joins that thread.
            _progress_metadata = {"reply_to_message_id": event_message_id}
        _progress_metadata = _non_conversational_metadata(_progress_metadata, platform=source.platform)
        if _native_slack_task_cards:
            # chat.startStream in channels requires the recipient team/user
            # pair; harmless extras elsewhere, so stamp them whenever known.
            _progress_metadata = dict(_progress_metadata or {})
            if source.scope_id:
                _progress_metadata.setdefault("recipient_team_id", source.scope_id)
                _progress_metadata.setdefault("slack_team_id", source.scope_id)
            if source.user_id:
                _progress_metadata.setdefault("recipient_user_id", source.user_id)
        _progress_reply_to = (
            event_message_id
            if (
                source.platform in (Platform.FEISHU, )
                and source.thread_id
                and event_message_id
            )
            or (
                # Buzz has no native thread_id; threading is always via reply-to
                # the triggering event id (channel clutter otherwise). Skipped
                # when the user opted out of threaded replies.
                str(getattr(source.platform, "value", source.platform) or "").lower() == "buzz"
                and event_message_id
                and _progress_reply_in_thread
            )
            or _relay_prospective_thread_id
            else None
        )

        async def write_tool_log():
            """Drain log_queue and append tool-call lines to tool_calls.log.

            Only active when ``display.tool_progress`` is ``log``. Uses a
            RotatingFileHandler (5MB × 3 backups) so the audit log can't grow
            unbounded, and the shared RedactingFormatter so secrets never land
            on disk.
            """
            from gateway.run import _hermes_home
            if log_queue is None:
                return
            from logging.handlers import RotatingFileHandler

            from agent.redact import RedactingFormatter

            log_dir = _hermes_home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "tool_calls.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(RedactingFormatter("%(message)s"))
            tool_logger = logging.getLogger(f"hermes.tool_calls.{id(log_queue)}")
            tool_logger.setLevel(logging.INFO)
            tool_logger.propagate = False
            tool_logger.addHandler(file_handler)
            try:
                while True:
                    try:
                        tool_logger.info("%s", log_queue.get_nowait())
                    except queue.Empty:
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error("write_tool_log error: %s", e)
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                # Drain remaining entries before closing so late tool calls
                # from the final iteration aren't lost.
                while True:
                    try:
                        tool_logger.info("%s", log_queue.get_nowait())
                    except queue.Empty:
                        break
                    except Exception:
                        break
                tool_logger.removeHandler(file_handler)
                try:
                    file_handler.flush()
                    file_handler.close()
                except Exception:
                    pass

        # Extracted to TurnRunner.send_progress_messages. The threading
        # metadata computed above is published onto the shared TurnContext
        # exactly where the original closure's captured locals were bound.
        turn_ctx._progress_metadata = _progress_metadata
        turn_ctx._progress_reply_to = _progress_reply_to
        send_progress_messages = turn_runner.send_progress_messages

        # We need to share the agent instance for interrupt support
        agent_holder = [None]  # Mutable container for the agent instance
        turn_ctx.agent_holder = agent_holder
        result_holder = [None]  # Mutable container for the result
        tools_holder = [None]   # Mutable container for the tool definitions
        stream_consumer_holder = [None]  # Mutable container for stream consumer
        # #60671 — streaming PCM audio consumer.  Created on the gateway
        # event-loop thread (NOT inside run_sync's executor worker) so the
        # outer finalisation / interrupt paths can reference it without a
        # cross-scope NameError.
        streaming_tts_consumer_holder: list = [None]
        turn_ctx.result_holder = result_holder
        turn_ctx.tools_holder = tools_holder
        turn_ctx.stream_consumer_holder = stream_consumer_holder
        turn_ctx.streaming_tts_consumer_holder = streaming_tts_consumer_holder

        # Bridge sync step_callback → async hooks.emit for agent:step events
        _loop_for_step = asyncio.get_running_loop()
        _hooks_ref = self.hooks

        # Bridge extracted to TurnRunner._step_callback_sync; the loop and
        # hooks refs bound just above are published at their original site.
        turn_ctx._loop_for_step = _loop_for_step
        turn_ctx._hooks_ref = _hooks_ref
        turn_ctx._step_callback_sync = turn_runner._step_callback_sync

        # Bridge sync event_callback → async hooks.emit for lifecycle events
        # (e.g. session:compress fires after context compression splits a session)
        # Bridge extracted to TurnRunner._event_callback_sync.
        turn_ctx._event_callback_sync = turn_runner._event_callback_sync

        # Bridge sync status_callback → async adapter.send for context pressure
        _status_adapter = self._adapter_for_source(source)
        _status_chat_id = source.chat_id
        if source.platform == Platform.FEISHU and source.thread_id and event_message_id:
            # Feishu topics only keep messages inside the topic when they are
            # sent via the reply API with reply_in_thread=true. Status/interim,
            # approval, and stream-consumer paths usually only receive metadata,
            # so carry the triggering message id as a Feishu-specific fallback.
            _status_thread_metadata: Optional[Dict[str, Any]] = {
                "thread_id": _progress_thread_id,
                "reply_to_message_id": event_message_id,
            }
        else:
            _status_thread_metadata = (
                self._thread_metadata_for_source(source, event_message_id)
                if _progress_thread_id == source.thread_id
                else self._thread_metadata_for_target(
                    source.platform,
                    source.chat_id,
                    _progress_thread_id,
                    chat_type=getattr(source, "chat_type", None),
                    reply_to_message_id=event_message_id,
                )
            ) if _progress_thread_id else None
            if _status_thread_metadata is None and _relay_prospective_thread_id:
                # Relay Discord auto-thread lane (see _progress_metadata above):
                # carry the reply anchor so status/interim bubbles route into
                # the same connector-created thread as the final reply.
                _status_thread_metadata = {
                    "reply_to_message_id": event_message_id
                }

        # Bridge extracted to TurnRunner._status_callback_sync; publish the
        # status wiring computed above onto the shared TurnContext at the
        # exact original binding site.
        turn_ctx._status_adapter = _status_adapter
        turn_ctx._status_adapter_provider = lambda: self._adapter_for_source(source)
        turn_ctx._status_chat_id = _status_chat_id
        turn_ctx._status_thread_metadata = _status_thread_metadata
        turn_ctx._status_callback_sync = turn_runner._status_callback_sync

        # ---- Streaming TTS consumer setup (#60671) ----
        # Created on the gateway event-loop thread (here, in _run_agent_inner),
        # NOT inside run_sync's executor worker.  This avoids a cross-scope
        # NameError: the outer interrupt / finalisation paths reference the
        # consumer via ``streaming_tts_consumer_holder[0]``.
        #
        # Gates: voice input, auto-TTS enabled for this chat, adapter
        # supports streaming, and a usable streaming TTS provider configured.
        _stts_adapter = self._adapter_for_source(source)
        _is_voice_input = (
            message_type is not None
            and str(getattr(message_type, "value", message_type)).lower() == "voice"
        )
        if (
            _stts_adapter is not None
            and _is_voice_input
            and _stts_adapter._should_auto_tts_for_chat(source.chat_id)
        ):
            try:
                from gateway.streaming_tts_consumer import StreamingTTSConsumer
                from tools.tts_tool import _load_tts_config
                _tts_cfg = _load_tts_config()
                _gateway_loop = self._gateway_loop or asyncio.get_event_loop()
                _stts_consumer = StreamingTTSConsumer(
                    adapter=_stts_adapter,
                    chat_id=source.chat_id,
                    tts_config=_tts_cfg,
                    loop=_gateway_loop,
                    metadata=_status_thread_metadata,
                )
                if _stts_consumer.active:
                    streaming_tts_consumer_holder[0] = _stts_consumer
                    _stts_consumer.start()
                # else: consumer inactive (no streaming provider) — leave
                # the holder as None so the whole-file fallback path runs.
            except Exception as _stts_err:
                logger.debug("Could not set up streaming TTS consumer: %s", _stts_err)

        # run_sync extracted to TurnRunner.run_sync (bound method; the
        # executor call below is unchanged).  Its closed-over locals travel
        # on turn_ctx; `nonlocal message` rebinds became ctx.message writes.
        run_sync = turn_runner.run_sync

        # Start progress message sender if enabled. Gate on needs_progress_queue
        # (tool_progress OR thinking_progress), not tool_progress alone: the
        # sender drains BOTH tool-progress lines and _thinking scratch bubbles.
        # With the old tool_progress-only gate, a thinking_progress:true /
        # tool_progress:off user had the callback queue _thinking messages that
        # no task ever drained — so they silently never appeared.
        progress_task = None
        if needs_progress_queue:
            progress_task = asyncio.create_task(send_progress_messages())
            # The ordering gate (TurnContext.wait_context_cards_delivered)
            # releases when this drain ends — bind the owning task so the gate
            # never waits on an invented timeout, and never outlives the
            # worker that owns delivery.
            turn_ctx.bind_progress_task(progress_task)

        # Tool-stage embed drain (Discord): one self-editing embed per MoA
        # tool invocation. The subscription is turn-scoped — unsubscribed in
        # the finally below so a queued follow-up turn for the same session
        # installs its own and never double-renders.
        stage_task = None
        _unsubscribe_stage_events = None
        if stage_event_queue is not None:
            from agent.moa_loop import subscribe_tool_stage_events

            _unsubscribe_stage_events = subscribe_tool_stage_events(
                session_id, turn_runner.tool_stage_event_callback
            )
            stage_task = asyncio.create_task(turn_runner.send_tool_stage_embeds())

        # Start the tool-call log writer when tool_progress == "log".
        log_task = None
        if log_mode_enabled:
            log_task = asyncio.create_task(write_tool_log())

        # Start stream consumer task — polls for consumer creation since it
        # happens inside run_sync (thread pool) after the agent is constructed.
        stream_task = None

        async def _start_stream_consumer():
            """Wait for the stream consumer to be created, then run it."""
            for _ in range(200):  # Up to 10s wait
                if stream_consumer_holder[0] is not None:
                    await stream_consumer_holder[0].run()
                    return
                await asyncio.sleep(0.05)

        stream_task = asyncio.create_task(_start_stream_consumer())

        # Track this agent as running for this session (for interrupt support)
        # We do this in a callback after the agent is created
        async def track_agent():
            # Wait for agent to be created
            while agent_holder[0] is None:
                await asyncio.sleep(0.05)
            if not session_key:
                return
            # Only promote the sentinel to the real agent if this run is still
            # current.  If /stop or /new bumped the generation while we were
            # spinning up, leave the newer run's slot alone — we'll be
            # discarded by the stale-result check in _handle_message_with_agent.
            if run_generation is not None and not self._is_session_run_current(
                session_key, run_generation
            ):
                logger.info(
                    "Skipping stale agent promotion for %s — generation %s is no longer current",
                    session_key or "",
                    run_generation,
                )
                return
            self._session_state(session_key).turn.agent = agent_holder[0]
            if self._draining:
                self._update_runtime_status("draining")

        tracking_task = asyncio.create_task(track_agent())

        # Monitor for interrupts from the adapter (new messages arriving).
        # This is the PRIMARY interrupt path for regular text messages —
        # Level 1 (base.py) catches them before _handle_message() is reached,
        # so the Level 2 running_agent.interrupt() path never fires.
        # The inactivity poll loop below has a BACKUP check in case this
        # task dies (no error handling = silent death = lost interrupts).
        _interrupt_detected = asyncio.Event()  # shared with backup check

        async def monitor_for_interrupt():
            from gateway.run import _build_media_placeholder
            if not session_key:
                return

            while True:
                await asyncio.sleep(0.2)  # Check every 200ms
                try:
                    # Re-resolve adapter each iteration so reconnects don't
                    # leave us holding a stale reference.
                    _adapter = self._adapter_for_source(source)
                    if not _adapter:
                        continue
                    # Check if adapter has a pending interrupt for this session.
                    # Must use session_key (build_session_key output) — NOT
                    # source.chat_id — because the adapter stores interrupt events
                    # under the full session key.
                    if hasattr(_adapter, 'has_pending_interrupt') and _adapter.has_pending_interrupt(session_key):
                        agent = agent_holder[0]
                        if agent:
                            # Peek at the pending message text WITHOUT consuming it.
                            # The message must remain in _pending_messages so the
                            # post-run dequeue at _dequeue_pending_event() can
                            # retrieve the full MessageEvent (with media metadata).
                            # If we pop here, a race exists: the agent may finish
                            # before checking _interrupt_requested, and the message
                            # is lost — neither the interrupt path nor the dequeue
                            # path finds it.
                            _peek_event = _adapter._pending_messages.get(session_key)
                            pending_text = None
                            if _peek_event is not None:
                                pending_text = _peek_event.text or ""
                                # Transcribe audio media BEFORE signaling the
                                # agent, so voice messages interrupt with the
                                # real transcript instead of an empty string
                                # (or file-path placeholder). Matches the UX
                                # of fresh voice messages including the
                                # optional 🎙️ echo back to the user.
                                _media_urls = getattr(_peek_event, "media_urls", None) or []
                                if self._pending_event_audio_paths(_peek_event):
                                    pending_text, _ = await self._transcribe_and_echo_pending_voice(
                                        _peek_event,
                                        _adapter,
                                        source,
                                        pending_text,
                                        log_context="Voice-interrupt",
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                                    )
                                elif not pending_text and _media_urls:
                                    pending_text = _build_media_placeholder(_peek_event)
                            logger.debug("Interrupt detected from adapter, signaling agent...")
                            agent.interrupt(pending_text)
                            _interrupt_detected.set()
                            # Abort streaming TTS on barge-in (#60671).
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort("barge-in")
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as _mon_err:
                    logger.debug("monitor_for_interrupt error (will retry): %s", _mon_err)

        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())

        # Periodic "still working" notifications for long-running tasks.
        # Fires every N seconds so the user knows the agent hasn't died.
        # Config: agent.gateway_notify_interval in config.yaml, or
        # HERMES_AGENT_NOTIFY_INTERVAL env var.  Default 180s (3 min).
        # 0 = disable notifications.  The interval is measured from the turn's
        # last USER-VISIBLE progress (see TurnContext.mark_visible_progress), so
        # continuous visible output suppresses the heartbeat while a genuinely
        # quiet turn still gets one after a full interval of silence.
        _NOTIFY_INTERVAL_RAW = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        _NOTIFY_INTERVAL = _NOTIFY_INTERVAL_RAW if _NOTIFY_INTERVAL_RAW > 0 else None
        _long_running_mode = _display_surface_mode(
            "long_running_notifications",
            default=True,
            allow_generic=True,
        )
        if _long_running_mode == "off":
            _NOTIFY_INTERVAL = None
        _notify_start = time.time()

        async def _notify_long_running():
            from gateway.run import (
                _interim_metadata,
                _non_conversational_metadata,
                _sleep_heartbeat_silence_deadline,
            )
            if _NOTIFY_INTERVAL is None:
                return  # Notifications disabled (gateway_notify_interval: 0)
            _notify_adapter = self._adapter_for_source(source)
            if not _notify_adapter:
                return
            # Track the heartbeat message id so we can edit-in-place on
            # platforms that support it (Telegram, Discord, Slack, etc.)
            # instead of spamming a new "Still working" bubble every
            # interval. Falls back to send-new when edit fails or isn't
            # supported by the adapter.
            _heartbeat_msg_id: Optional[str] = None
            _heartbeat_sent_text: Optional[str] = None
            # Silence clock origin: the later of the turn start, the last visible
            # progress (turn_ctx.mark_visible_progress) and the last heartbeat tick.
            _silence_origin = [time.monotonic()]

            while True:
                await _sleep_heartbeat_silence_deadline(_NOTIFY_INTERVAL, turn_ctx, _silence_origin)
                # A consumed tick (heartbeat attempt or a suppressed skip) restarts
                # the silence clock so this loop never fires faster than one interval.
                _silence_origin[0] = time.monotonic()
                # Stop heartbeating once this run no longer owns the session
                # slot or the executor has finished — otherwise a stale
                # "running: delegate_agent" bubble can outlive the run that
                # spawned it (#12029). _executor_task is a closure var bound
                # just after this task is scheduled; tolerate the brief window
                # before then (the first wake is _NOTIFY_INTERVAL away anyway).
                try:
                    _exec_ref = _executor_task
                except NameError:
                    _exec_ref = None
                if not self._should_emit_long_running_notification(
                    session_key, agent_holder[0], _exec_ref
                ):
                    break
                _elapsed_mins = int((time.time() - _notify_start) // 60)
                # Include agent activity context if available. Default
                # heartbeat is terse: elapsed + current tool. Verbose
                # iteration counter is gated on busy_ack_detail so users
                # who want it can opt in per platform.
                _agent_ref = agent_holder[0]
                _status_detail = ""
                _phase_snapshot: dict = {}
                _want_iteration_detail = bool(
                    resolve_display_setting(
                        user_config,
                        platform_key,
                        "busy_ack_detail",
                        True,
                    )
                )
                if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                    try:
                        _phase_snapshot = _agent_ref.get_activity_summary()
                    except Exception:
                        pass
                _status_detail = self._format_long_running_status_detail(
                    _phase_snapshot, include_iterations=_want_iteration_detail
                )
                if _long_running_mode == "phase":
                    # Phase heartbeat (Discord default): one line naming the
                    # current wait (tool / model / packing) and its elapsed
                    # time — no iteration counter, no wait-notice essay.
                    _heartbeat_text = format_phase_heartbeat(_phase_snapshot)
                elif _long_running_mode == "generic":
                    _heartbeat_text = _generic_status_phrase("status")
                else:
                    _heartbeat_text = f"⏳ Working — {_elapsed_mins} min{_status_detail}"
                if _heartbeat_msg_id is not None and _heartbeat_sent_text == _heartbeat_text:
                    continue
                try:
                    _notify_res = None
                    # The turn's delivery lock is the ordering boundary between
                    # the heartbeat and actual visible progress: while we hold
                    # it, no same-turn tool/commentary/stream transport call can
                    # interleave, so progress cannot land mid-edit and make this
                    # heartbeat stale before it is even sent.
                    async with turn_ctx.delivery_lock():
                        # Revalidate under the guard: same-turn visible
                        # progress that landed while this tick waited for the
                        # lock restarted the silence clock - skip the whole
                        # tick (the next attempt comes a full interval after
                        # that delivery).
                        _lp_now = turn_ctx.last_visible_progress_monotonic()
                        if _lp_now is not None and _lp_now > _silence_origin[0]:
                            continue
                        if not self._should_emit_long_running_notification(
                            session_key, agent_holder[0], _exec_ref
                        ):
                            break
                        if _heartbeat_msg_id:
                            try:
                                _notify_res = await _notify_adapter.edit_message(
                                    source.chat_id,
                                    _heartbeat_msg_id,
                                    _heartbeat_text,
                                )
                            except Exception as _ee:
                                logger.debug("Heartbeat edit failed: %s", _ee)
                                _notify_res = None
                    if _notify_res is not None and getattr(_notify_res, "success", False):
                        _heartbeat_sent_text = _heartbeat_text
                        continue
                    # The edit failed (or none existed): RELEASE the lock and
                    # yield so same-turn progress queued behind the failed edit
                    # lands FIRST, then REACQUIRE and revalidate everything - a
                    # delivery that landed invalidates the fresh-bubble fallback
                    # and the next tick comes a full interval after it.
                    await asyncio.sleep(0)
                    async with turn_ctx.delivery_lock():
                        # Revalidate under the guard: same-turn visible
                        # progress that landed while this tick waited for the
                        # lock restarted the silence clock - skip the whole
                        # tick (the next attempt comes a full interval after
                        # that delivery).
                        _lp_now = turn_ctx.last_visible_progress_monotonic()
                        if _lp_now is not None and _lp_now > _silence_origin[0]:
                            continue
                        if not self._should_emit_long_running_notification(
                            session_key, agent_holder[0], _exec_ref
                        ):
                            break
                        _notify_res = await _notify_adapter.send(
                            source.chat_id,
                            _heartbeat_text,
                            metadata=_interim_metadata(_non_conversational_metadata(_status_thread_metadata, platform=source.platform)),
                        )
                        if getattr(_notify_res, "success", False) and getattr(
                            _notify_res, "message_id", None
                        ):
                            _heartbeat_msg_id = str(_notify_res.message_id)
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(_heartbeat_msg_id)
                            _heartbeat_sent_text = _heartbeat_text
                except Exception as _ne:
                    logger.debug("Long-running notification error: %s", _ne)

        _notify_task = asyncio.create_task(_notify_long_running())

        def _stream_confirmed_final_delivery(
            consumer,
            final_text: str,
            *,
            previewed: bool = False,
        ) -> bool:
            """Return True only when the actual final reply reached the user."""
            if consumer is None:
                return False
            if getattr(consumer, "final_response_sent", False):
                # A successful finalize call is not proof the *content* was
                # final: the edit may have carried only the last preview
                # snapshot while the tail generated between that snapshot and
                # stream completion never reached any API call (#71643).
                # Reconcile the recorded turn-final payload against the
                # completed response; only a demonstrable mismatch (False)
                # overrides the flag — including payload-less multi-message
                # split delivery (#78541). None (no record on a non-split
                # legacy path) keeps the legacy trust so ambiguous-timeout
                # dedup is not regressed.
                matcher = getattr(consumer, "delivered_final_matches", None)
                if callable(matcher):
                    try:
                        if matcher(final_text) is False:
                            return False
                    except Exception:
                        pass
                return True
            if previewed:
                has_delivered_text = getattr(consumer, "has_delivered_text", None)
                if callable(has_delivered_text):
                    try:
                        return bool(has_delivered_text(final_text))
                    except Exception:
                        return False
            return False

        async def _finalize_stream_delivery_state(candidate) -> None:
            """Finalize stream delivery before any early return can escape.

            `_run_agent_inner` has several returns inside its main try block.
            Python evaluates those return values before running `finally`, so
            delivery state must be written from the finally block itself. If it
            is written afterward, the caller sees `already_sent=False`, appends
            the footer to a body Discord already streamed, and then suppresses
            that unsent combined response.
            """
            _sc = stream_consumer_holder[0]
            if not isinstance(candidate, dict) or candidate.get("failed"):
                return

            _final = candidate.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            _previewed = bool(candidate.get("response_previewed"))
            _content_delivered = bool(
                _sc and getattr(_sc, "final_content_delivered", False)
            )
            # #71643/#78541: a successful finalize can carry only the last
            # preview snapshot, and a payload-less multi-message split records
            # no complete turn-final payload at all. When the consumer's
            # recorded payload demonstrably mismatches the completed response,
            # final_content_delivered must not claim delivery — leave
            # already_sent unset so the normal final send delivers the
            # complete text (mirrors the in-try suppression block).
            if _content_delivered and not _is_empty_sentinel:
                _matcher = getattr(_sc, "delivered_final_matches", None)
                if callable(_matcher):
                    try:
                        if _matcher(_final) is False:
                            _content_delivered = False
                    except Exception:
                        pass
            _transformed = bool(candidate.get("response_transformed"))
            _streamed = _stream_confirmed_final_delivery(
                _sc,
                _final,
                previewed=_previewed,
            )

            if (
                not _is_empty_sentinel
                and not _transformed
                and (_streamed or _content_delivered)
            ):
                candidate["already_sent"] = True
                logger.info(
                    "Suppressing normal final send for session %s: final delivery "
                    "already confirmed (streamed=%s previewed=%s "
                    "content_delivered=%s).",
                    session_key or "?",
                    _streamed,
                    _previewed,
                    _content_delivered,
                )
            elif not _is_empty_sentinel and _transformed and _sc is not None:
                # Plugin hooks transformed the response after streaming. Edit
                # the existing message before returning rather than sending a
                # duplicate final response.
                _sc_msg_id = _sc.message_id
                if _sc_msg_id:
                    try:
                        await _sc.adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=_sc_msg_id,
                            content=candidate["final_response"],
                            finalize=True,
                        )
                        candidate["already_sent"] = True
                        logger.info(
                            "Edited streamed message %s for session %s to include "
                            "plugin-transformed content.",
                            _sc_msg_id,
                            session_key or "?",
                        )
                    except Exception as _edit_err:
                        logger.warning(
                            "Failed to edit streamed message for session %s: %s",
                            session_key or "?",
                            _edit_err,
                        )

        try:
            # Run in thread pool to not block.  Use an *inactivity*-based
            # timeout instead of a wall-clock limit: the agent can run for
            # hours if it's actively calling tools / receiving stream tokens,
            # but a hung API call or stuck tool with no activity for the
            # configured duration is caught and killed.  (#4815)
            #
            # Config: agent.gateway_timeout in config.yaml, or
            # HERMES_AGENT_TIMEOUT env var (env var takes precedence).
            # Default 1800s (30 min inactivity).  0 = unlimited.
            _agent_timeout_raw = _float_env("HERMES_AGENT_TIMEOUT", 1800)
            _agent_timeout = _agent_timeout_raw if _agent_timeout_raw > 0 else None
            _agent_warning_raw = _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900)
            _agent_warning = _agent_warning_raw if _agent_warning_raw > 0 else None
            _warning_fired = False

            # A background=true process intentionally survives a successful
            # turn, so capture existing IDs and reap only children created by
            # THIS turn if it times out. The daemon watchdog is independent of
            # asyncio: cgroup memory reclaim may starve the event loop that runs
            # the normal timeout poll, but it need not also postpone cleanup
            # until the loop recovers (#76115).
            from tools.process_registry import process_registry

            _turn_task_id = session_id or ""
            _turn_process_baseline = process_registry.snapshot_running_ids(_turn_task_id)
            turn_ctx.process_task_id = _turn_task_id
            turn_ctx.process_baseline = _turn_process_baseline
            _turn_worker_done = threading.Event()
            _turn_timeout_fired = threading.Event()
            _turn_cleanup_lock = threading.Lock()
            # task_id above is session-scoped, not turn-scoped (#76115
            # review): gate the eventual reap on this exact claim still
            # being current, so a replacement turn that starts on the same
            # session before the watchdog fires doesn't get its own fresh
            # process killed by this turn's stale baseline.
            _turn_run_generation = run_generation
            _turn_is_current = (
                (lambda: self._is_session_run_current(session_key, _turn_run_generation))
                if _turn_run_generation is not None
                else (lambda: True)
            )

            def _run_sync_with_timeout_lifecycle():
                try:
                    return run_sync()
                finally:
                    _turn_worker_done.set()
                    # `.turn.agent` on the session state is only reset to
                    # _AGENT_PENDING_SENTINEL when the *next* turn is
                    # claimed (see _session_state(...).turn.agent = ... at
                    # claim time), so a stale reference to this exact agent
                    # instance stays reachable from
                    # _interrupt_and_clear_session() until then. Clearing
                    # the ownership markers here — the instant this turn's
                    # own worker finishes — closes that window: an
                    # explicit /stop landing on the already-finished turn
                    # no longer reaps background work the turn deliberately
                    # left running (#76115).
                    _finished_agent = agent_holder[0] if agent_holder else None
                    if _finished_agent is not None:
                        _finished_agent._gateway_turn_process_task_id = ""
                        _finished_agent._gateway_turn_process_baseline = frozenset()

            if _agent_timeout is not None:
                threading.Thread(
                    target=_watch_gateway_turn_inactivity,
                    kwargs={
                        "agent_holder": agent_holder,
                        "task_id": _turn_task_id,
                        "process_baseline": _turn_process_baseline,
                        "timeout": _agent_timeout,
                        "worker_done": _turn_worker_done,
                        "timeout_fired": _turn_timeout_fired,
                        "cleanup_lock": _turn_cleanup_lock,
                        "poll_interval": 5.0,
                        "is_still_current": _turn_is_current,
                    },
                    name=f"gateway-turn-watchdog-{_turn_task_id[:12]}",
                    daemon=True,
                ).start()
            _executor_task = asyncio.ensure_future(
                self._run_in_executor_with_context(_run_sync_with_timeout_lifecycle)
            )

            _inactivity_timeout = False
            _POLL_INTERVAL = 5.0

            if _agent_timeout is None:
                # Unlimited — still poll periodically for backup interrupt
                # detection in case monitor_for_interrupt() silently died.
                response = None
                while True:
                    done, _ = await asyncio.wait(
                        {_executor_task}, timeout=_POLL_INTERVAL
                    )
                    if done:
                        response = _executor_task.result()
                        break
                    # Backup interrupt check: if the monitor task died or
                    # missed the interrupt, catch it here.
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if (_backup_adapter and _backup_agent
                                and hasattr(_backup_adapter, 'has_pending_interrupt')
                                and _backup_adapter.has_pending_interrupt(session_key)):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            if _bp_event is not None:
                                _bp_media_urls = getattr(_bp_event, "media_urls", None) or []
                                if self._pending_event_audio_paths(_bp_event):
                                    _bp_text, _ = await self._transcribe_and_echo_pending_voice(
                                        _bp_event,
                                        _backup_adapter,
                                        source,
                                        _bp_text or "",
                                        log_context="Voice-backup-interrupt",
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                                    )
                                elif not _bp_text and _bp_media_urls:
                                    _bp_text = _build_media_placeholder(_bp_event)
                            logger.info(
                                "Backup interrupt detected for session %s "
                                "(monitor task state: %s)",
                                session_key,
                                "done" if interrupt_monitor.done() else "running",
                            )
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
                            # Abort streaming TTS on barge-in (#60671).
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort("barge-in")

            else:
                # Poll loop: check the agent's built-in activity tracker
                # (updated by _touch_activity() on every tool call, API
                # call, and stream delta) every few seconds.
                response = None
                while True:
                    done, _ = await asyncio.wait(
                        {_executor_task}, timeout=_POLL_INTERVAL
                    )
                    if done:
                        # Prefer the real result when the worker finished,
                        # even if the watchdog fired in the same window: the
                        # completed run already persisted its reply to session
                        # history, so surfacing the "agent inactive" diagnostic
                        # here would contradict the stored transcript. This
                        # mirrors _abandon_timed_out_gateway_turn's own
                        # worker_done-wins tiebreak (under cleanup_lock).
                        response = _executor_task.result()
                        break
                    if _turn_timeout_fired.is_set():
                        _inactivity_timeout = True
                        break
                    # Agent still running — check inactivity.
                    _agent_ref = agent_holder[0]
                    _idle_secs = 0.0
                    if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                        try:
                            _act = _agent_ref.get_activity_summary()
                            _idle_secs = _act.get("seconds_since_activity", 0.0)
                        except Exception:
                            pass
                    # Staged warning: fire once before escalating to full timeout.
                    if (not _warning_fired and _agent_warning is not None
                            and _idle_secs >= _agent_warning):
                        _warning_fired = True
                        _warn_adapter = self._adapter_for_source(source)
                        if _warn_adapter:
                            _elapsed_warn = int(_agent_warning // 60) or 1
                            _remaining_mins = int((_agent_timeout - _agent_warning) // 60) or 1
                            try:
                                await _warn_adapter.send(
                                    source.chat_id,
                                    f"⚠️ No activity for {_elapsed_warn} min. "
                                    f"If the agent does not respond soon, it will "
                                    f"be timed out in {_remaining_mins} min. "
                                    f"You can continue waiting or use /reset.",
                                    metadata=_interim_metadata(_status_thread_metadata),
                                )
                            except Exception as _warn_err:
                                logger.debug("Inactivity warning send error: %s", _warn_err)
                    if _idle_secs >= _agent_timeout:
                        _inactivity_timeout = True
                        threading.Thread(
                            target=_abandon_timed_out_gateway_turn,
                            kwargs={
                                "agent_holder": agent_holder,
                                "task_id": _turn_task_id,
                                "process_baseline": _turn_process_baseline,
                                "worker_done": _turn_worker_done,
                                "timeout_fired": _turn_timeout_fired,
                                "cleanup_lock": _turn_cleanup_lock,
                                "is_still_current": _turn_is_current,
                            },
                            name=f"gateway-turn-reaper-{_turn_task_id[:12]}",
                            daemon=True,
                        ).start()
                        break
                    # Backup interrupt check (same as unlimited path).
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if (_backup_adapter and _backup_agent
                                and hasattr(_backup_adapter, 'has_pending_interrupt')
                                and _backup_adapter.has_pending_interrupt(session_key)):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            if _bp_event is not None:
                                _bp_media_urls = getattr(_bp_event, "media_urls", None) or []
                                if self._pending_event_audio_paths(_bp_event):
                                    _bp_text, _ = await self._transcribe_and_echo_pending_voice(
                                        _bp_event,
                                        _backup_adapter,
                                        source,
                                        _bp_text or "",
                                        log_context="Voice-backup-interrupt",
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                                    )
                                elif not _bp_text and _bp_media_urls:
                                    _bp_text = _build_media_placeholder(_bp_event)
                            logger.info(
                                "Backup interrupt detected for session %s "
                                "(monitor task state: %s)",
                                session_key,
                                "done" if interrupt_monitor.done() else "running",
                            )
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
                            # Abort streaming TTS on barge-in (#60671).
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort("barge-in")

            if _inactivity_timeout:
                # Build a diagnostic summary from the agent's activity tracker.
                _timed_out_agent = agent_holder[0]
                _activity = {}
                if _timed_out_agent and hasattr(_timed_out_agent, "get_activity_summary"):
                    try:
                        _activity = _timed_out_agent.get_activity_summary()
                    except Exception:
                        pass

                _last_desc = _activity.get("last_activity_desc", "unknown")
                _secs_ago = _activity.get("seconds_since_activity", 0)
                _cur_tool = _activity.get("current_tool")
                _iter_n = _activity.get("api_call_count", 0)
                _iter_max = _activity.get("max_iterations", 0)

                logger.error(
                    "Agent idle for %.0fs (timeout %.0fs) in session %s "
                    "| last_activity=%s | iteration=%s/%s | tool=%s",
                    _secs_ago, _agent_timeout, session_key,
                    _last_desc, _iter_n, _iter_max,
                    _cur_tool or "none",
                )

                # Interrupt the agent if it's still running so the thread
                # pool worker is freed.
                if _timed_out_agent:
                    request_hard_interrupt(_timed_out_agent, _INTERRUPT_REASON_TIMEOUT)

                _timeout_mins = int(_agent_timeout // 60) or 1

                # Construct a user-facing message with diagnostic context.
                _diag_lines = [
                    f"⏱️ Agent inactive for {_timeout_mins} min — no tool calls "
                    f"or API responses."
                ]
                if _cur_tool:
                    _diag_lines.append(
                        f"The agent appears stuck on tool `{_cur_tool}` "
                        f"({_secs_ago:.0f}s since last activity, "
                        f"iteration {_iter_n}/{_iter_max})."
                    )
                else:
                    _diag_lines.append(
                        f"Last activity: {_last_desc} ({_secs_ago:.0f}s ago, "
                        f"iteration {_iter_n}/{_iter_max}). "
                        "The agent may have been waiting on an API response."
                    )
                _diag_lines.append(
                    "To increase the limit, set agent.gateway_timeout in config.yaml "
                    "(value in seconds, 0 = no limit) and restart the gateway.\n"
                    "Try again, or use /reset to start fresh."
                )

                response = {
                    "final_response": "\n".join(_diag_lines),
                    "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                    "api_calls": _iter_n,
                    "tools": tools_holder[0] or [],
                    "history_offset": 0,
                    "failed": True,
                }

            # Track fallback model state: if the agent switched to a
            # fallback model during this run, persist it so /model shows
            # the actually-active model instead of the config default.
            # Skip eviction when the run failed — evicting a failed agent
            # forces MCP reinit on the next message for no benefit (the
            # same error will recur).  This was the root cause of #7130:
            # a bad model ID triggered fallback → eviction → recreation →
            # MCP reinit → same 400 → loop, burning 91% CPU for hours.
            _agent = agent_holder[0]
            _result_for_fb = result_holder[0]
            _run_failed = _result_for_fb.get("failed") if _result_for_fb else False
            if _agent is not None and hasattr(_agent, 'model') and not _run_failed:
                _cfg_model = _resolve_gateway_model()
                # Normalize _cfg_model the same way AIAgent.__init__ does, so a
                # vendor-prefixed config value (e.g. "deepseek/deepseek-v4-pro")
                # matches the agent's stripped model ("deepseek-v4-pro") on
                # native providers. Without this, _agent.model != _cfg_model is
                # always true for vendor-prefixed config and the cached agent is
                # evicted on every successful turn — destroying prompt caching.
                # Aggregators (openrouter, etc.) keep the vendor/model slug, so
                # they're left untouched.
                try:
                    from hermes_cli.model_normalize import (
                        _AGGREGATOR_PROVIDERS,
                        normalize_model_for_provider,
                    )
                    _agent_provider = getattr(_agent, 'provider', '') or ''
                    if _agent_provider and _agent_provider not in _AGGREGATOR_PROVIDERS:
                        _cfg_model = normalize_model_for_provider(_cfg_model, _agent_provider)
                except Exception:
                    pass
                if _agent.model != _cfg_model and not self._is_intentional_model_switch(session_key, _agent, _cfg_model):
                    # Fallback activated on a successful run — evict cached
                    # agent so the next message retries the primary model.
                    self._evict_cached_agent(session_key)

            # Check if we were interrupted OR have a queued message (/queue).
            result = result_holder[0]
            adapter = self._adapter_for_source(source)

            # Finalize the streaming-TTS consumer (#60671).
            #
            # finish() is called from the outer event-loop thread (not the
            # executor worker) so early returns from run_sync are also
            # finalised.  wait_complete() drains queued audio; on timeout
            # the consumer is aborted unconditionally — if audio was
            # audible, suppression is preserved so the gateway does not
            # replay from the beginning; if no audio was audible, the
            # whole-file fallback path is permitted.
            _stts = streaming_tts_consumer_holder[0]
            if _stts is not None:
                _stts.finish()
                try:
                    await _stts.wait_complete(timeout=10.0)
                except Exception as _stts_done_err:
                    logger.debug("streaming TTS wait_complete error: %s", _stts_done_err)
                if not _stts.done:
                    # Timeout before or after audible audio: abort to free
                    # the consumer task.  Audible streams retain suppression;
                    # silent streams remain eligible for whole-file fallback.
                    _stts.abort("streaming TTS finalisation timeout")
                    await _stts.wait_complete(timeout=2.0)
                if _stts.suppress_whole_file and adapter is not None:
                    _mark_turn = getattr(adapter, "_mark_streaming_tts_completed_turn", None)
                    if callable(_mark_turn):
                        _mark_turn(session_key, run_generation)

            # Get pending message from adapter.
            # Use session_key (not source.chat_id) to match adapter's storage keys.
            pending_event = None
            pending = None
            if result and adapter and session_key:
                pending_event = _dequeue_pending_event(adapter, session_key)
                # /queue overflow: after consuming the adapter's "next-up"
                # slot, promote the next queued event into it so the
                # recursive run's drain will see it.  This keeps the slot
                # occupied for the full FIFO chain, which (a) preserves
                # order, and (b) causes any mid-chain /queue to correctly
                # route to overflow rather than jumping the queue.
                pending_event = self._promote_queued_event(session_key, adapter, pending_event)
                if result.get("interrupted") and not pending_event and result.get("interrupt_message"):
                    interrupt_message = result.get("interrupt_message")
                    if _is_control_interrupt_message(interrupt_message):
                        logger.info(
                            "Ignoring control interrupt message for session %s: %s",
                            session_key or "?",
                            interrupt_message,
                        )
                    else:
                        pending = interrupt_message
                elif pending_event:
                    # Transcribe audio media on the dequeued event BEFORE it is
                    # handed back as the next user turn, so queued/interrupting
                    # voice messages drain with the real transcript instead of
                    # a file-path placeholder. When configured, echo each
                    # transcript back to the user in the same 🎙️ format as
                    # fresh voice messages.
                    _pending_text = pending_event.text or ""
                    _media_urls = getattr(pending_event, "media_urls", None) or []
                    if self._pending_event_audio_paths(pending_event):
                        pending, _ = await self._transcribe_and_echo_pending_voice(
                            pending_event,
                            adapter,
                            source,
                            _pending_text,
                            log_context="Voice-drain",
                            metadata={"thread_id": source.thread_id} if source.thread_id else None,
                        )
                        if not pending:
                            pending = _build_media_placeholder(pending_event)
                    else:
                        pending = _pending_text or _build_media_placeholder(pending_event)
                    if pending:
                        logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])

            # Leftover /steer: if a steer arrived after the last tool batch
            # (e.g. during the final API call), the agent couldn't inject it
            # and returned it in result["pending_steer"]. Deliver it as the
            # next user turn so it isn't silently dropped.
            if result and not pending and not pending_event:
                _leftover_steer = result.get("pending_steer")
                if _leftover_steer:
                    pending = _leftover_steer
                    logger.debug("Delivering leftover /steer as next turn: '%s...'", pending[:40])

            # Safety net: if the pending text is a slash command (e.g. "/stop",
            # "/new"), discard it — commands should never be passed to the agent
            # as user input.  The primary fix is in base.py (commands bypass the
            # active-session guard), but this catches edge cases where command
            # text leaks through the interrupt_message fallback.
            if pending and pending.strip().startswith("/"):
                _pending_parts = pending.strip().split(None, 1)
                _pending_cmd_word = _pending_parts[0][1:].lower() if _pending_parts else ""
                if _pending_cmd_word:
                    try:
                        from hermes_cli.commands import resolve_command as _rc_pending
                        if _rc_pending(_pending_cmd_word):
                            logger.info(
                                "Discarding command '/%s' from pending queue — "
                                "commands must not be passed as agent input",
                                _pending_cmd_word,
                            )
                            pending_event = None
                            pending = None
                    except Exception:
                        pass

            if self._draining and (pending_event or pending):
                logger.info(
                    "Discarding pending follow-up for session %s during gateway %s",
                    session_key or "?",
                    self._status_action_label(),
                )
                pending_event = None
                pending = None

            if pending_event or pending:
                logger.debug("Processing pending message: '%s...'", pending[:40])

                # Clear the adapter's interrupt event so the next _run_agent call
                # doesn't immediately re-trigger the interrupt before the new agent
                # even makes its first API call (this was causing an infinite loop).
                if adapter and hasattr(adapter, '_active_sessions') and session_key and session_key in adapter._active_sessions:
                    adapter._active_sessions[session_key].clear()

                # Cap recursion depth to prevent resource exhaustion when the
                # user sends multiple messages while the agent keeps failing. (#816)
                if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
                    logger.warning(
                        "Interrupt recursion depth %d reached for session %s — "
                        "queueing message instead of recursing.",
                        _interrupt_depth, session_key,
                    )
                    adapter = self._adapter_for_source(source)
                    if adapter and pending_event:
                        merge_pending_message_event(adapter._pending_messages, session_key, pending_event)
                    elif adapter and hasattr(adapter, 'queue_message'):
                        adapter.queue_message(session_key, pending)
                    return result_holder[0] or {"final_response": response, "messages": history}

                was_interrupted = result.get("interrupted")
                if not was_interrupted:
                    # Queued message after normal completion — deliver the first
                    # response before processing the queued follow-up.
                    # Skip if streaming already delivered it.
                    _sc = stream_consumer_holder[0]
                    if _sc and stream_task:
                        try:
                            await asyncio.wait_for(stream_task, timeout=5.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            stream_task.cancel()
                            try:
                                await stream_task
                            except asyncio.CancelledError:
                                pass
                        except Exception as e:
                            logger.debug("Stream consumer wait before queued message failed: %s", e)
                    # The queued branch needs raw ``result`` for interruption,
                    # history, and recursion state, but delivery must use the
                    # finalized task result. The latter contains empty/failure
                    # normalization and any final response processing applied by
                    # _run_agent_task; sending the raw copy bypasses those steps.
                    _delivery_result = response if isinstance(response, dict) else (result or {})
                    _previewed = bool(_delivery_result.get("response_previewed"))
                    first_response = _delivery_result.get("final_response", "")
                    _already_streamed = _stream_confirmed_final_delivery(
                        _sc,
                        first_response,
                        previewed=_previewed,
                    )
                    # Apply the same predicate as the normal completed-turn path.
                    # This direct queued-send branch predates intentional-silence
                    # filtering, so without this check it leaks the literal marker.
                    try:
                        from gateway.response_filters import is_intentional_silence_agent_result
                        _intentional_silence = is_intentional_silence_agent_result(
                            _delivery_result, first_response,
                        )
                    except Exception:
                        _intentional_silence = False
                    if _intentional_silence:
                        logger.info(
                            "Queued follow-up for session %s: suppressing intentional silence marker before continuing.",
                            session_key or "?",
                        )
                    elif first_response:
                        try:
                            if _already_streamed:
                                logger.info(
                                    "Queued follow-up for session %s: final text delivery confirmed; delivering explicit media before continuing.",
                                    session_key or "?",
                                )
                            else:
                                logger.info(
                                    "Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.",
                                    session_key or "?",
                                )
                            await self._deliver_queued_first_response(
                                first_response,
                                source=source,
                                adapter=adapter,
                                metadata=_status_thread_metadata,
                                event_message_id=event_message_id,
                                text_already_delivered=_already_streamed,
                                deliver_media=not _delivery_result.get("failed"),
                                stream_consumer=_sc,
                            )
                        except Exception as e:
                            logger.warning("Failed to send first response before queued message: %s", e)
                    # Release deferred bg-review notifications now that the
                    # first response has been delivered.  Pop from the
                    # adapter's callback dict (prevents double-fire in
                    # base.py's finally block) and call it.
                    if getattr(type(adapter), "pop_post_delivery_callback", None) is not None:
                        _bg_cb = adapter.pop_post_delivery_callback(
                            session_key,
                            generation=run_generation,
                        )
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                    elif adapter and hasattr(adapter, "_post_delivery_callbacks"):
                        _bg_cb = adapter._post_delivery_callbacks.pop(session_key, None)
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                # else: interrupted — discard the interrupted response ("Operation
                # interrupted." is just noise; the user already knows they sent a
                # new message).

                updated_history = result.get("messages", history)
                next_source = source
                next_message = pending
                next_message_id = None
                next_channel_prompt = None
                next_session_key = session_key
                # #60671 — carry the pending event's message_type into the
                # recursive call so queued voice turns can stream TTS and
                # re-mark the generation for the final delivered turn.
                next_message_type = None
                if pending_event is not None:
                    next_source = getattr(pending_event, "source", None) or source
                    if self._is_goal_continuation_event(pending_event) and not self._goal_still_active_for_session(session_id):
                        logger.info(
                            "Discarding stale goal continuation for session %s — goal is no longer active",
                            session_key or "?",
                        )
                        return result
                    # Resolve the follow-up's session key BEFORE preparing the
                    # inbound text: _prepare_inbound_message_text buffers native
                    # image paths under the key it is given, and the recursive
                    # _run_agent below consumes them under next_session_key.
                    # The write and consume keys must match or the images drop.
                    try:
                        next_session_key = self._session_key_for_source(next_source)
                    except Exception:
                        logger.debug(
                            "Queued follow-up session-key resolution failed; reusing %s",
                            session_key or "?",
                            exc_info=True,
                        )
                    next_message = await self._prepare_profile_scoped_inbound_message_text(
                        event=pending_event,
                        source=next_source,
                        history=updated_history,
                        session_key=next_session_key,
                    )
                    if next_message is None:
                        return result
                    next_message_id = self._reply_anchor_for_event(pending_event)
                    next_channel_prompt = getattr(pending_event, "channel_prompt", None)
                    next_message_type = getattr(pending_event, "message_type", None)

                # Clear the completed streaming marker from the prior logical
                # turn so the recursive turn's streaming TTS is not suppressed
                # by the prior turn's completion (#60671).
                _clear_adapter = self._adapter_for_source(source)
                if _clear_adapter is not None and session_key and run_generation is not None:
                    _completed_turns = getattr(_clear_adapter, "_streaming_tts_completed_turns", None)
                    if _completed_turns is not None:
                        _prior_key = getattr(_clear_adapter, "_streaming_tts_turn_key", None)
                        if callable(_prior_key):
                            _pk = _prior_key(session_key, run_generation)
                            if _pk:
                                _completed_turns.discard(_pk)

                # Restart typing indicator so the user sees activity while
                # the follow-up turn runs.  The outer _process_message_background
                # typing task is still alive but may be stale.
                _followup_adapter = self._adapter_for_source(source)
                if _followup_adapter:
                    try:
                        await _followup_adapter.send_typing(
                            source.chat_id,
                            metadata=_status_thread_metadata,
                        )
                    except Exception:
                        pass

                # Re-baseline the cached agent's message_count snapshot before
                # recursing into the in-band queued (/queue) follow-up turn.
                # The first turn has completed and flushed its own user +
                # assistant rows to the SessionDB, so the cross-process
                # coherence guard (#45966) — which this recursive _run_agent
                # call re-enters — would otherwise see the grown on-disk count
                # against the stale build-time snapshot and rebuild the agent
                # on THIS process's OWN writes, destroying the prompt-cache
                # prefix #46237 was merged to preserve.  The existing
                # re-baseline in _handle_message_with_agent only runs after the
                # whole _run_agent chain unwinds — too late for the in-band
                # follow-up.  Use the same (session_key, session_id) the
                # recursive call runs under so the snapshot matches exactly
                # what the follow-up's guard will consult.  Fail-safe in helper.
                await self._refresh_agent_cache_message_count(session_key, session_id)

                followup_result = await self._run_agent(
                    message=next_message,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=next_source,
                    session_id=session_id,
                    session_key=next_session_key,
                    run_generation=run_generation,
                    _interrupt_depth=_interrupt_depth + 1,
                    event_message_id=next_message_id,
                    channel_prompt=next_channel_prompt,
                    message_type=next_message_type,
                )
                return _preserve_queued_followup_history_offset(result, followup_result)
        finally:
            # Capture the exact mutable object an early return may already have
            # evaluated before awaiting stream cleanup below.
            _held_agent_result = result_holder[0]

            # Ordering gate for the non-streamed final reply — run BEFORE the
            # progress drain is cancelled below: a turn with no stream consumer
            # and no interim lane delivers its FIRST visible assistant output
            # from the outer handler's send after this unwind, and the flush
            # marker must reach a LIVE drain. No-op when no context cards were
            # queued, an earlier lane already cleared the gate, or the turn is
            # stale; the wait ends when the marker is delivered or the bound
            # drain task ends (TurnContext.wait_context_cards_delivered).
            try:
                await turn_ctx.wait_context_cards_delivered()
            except Exception:
                logger.debug("context-card gate before cleanup failed", exc_info=True)

            # Stop progress sender, interrupt monitor, and notification task
            if progress_task:
                progress_task.cancel()
            if stage_task:
                stage_task.cancel()
            if _unsubscribe_stage_events is not None:
                try:
                    _unsubscribe_stage_events()
                except Exception:
                    pass
            if log_task:
                log_task.cancel()
            interrupt_monitor.cancel()
            _notify_task.cancel()

            # Wait for stream consumer to finish its final edit
            if stream_task:
                # If the agent never created a stream consumer (e.g. non-
                # streaming code path, or a test stub returning synchronously)
                # there is nothing to flush — cancel immediately instead of
                # waiting out the 5s timeout on a task that's just polling for
                # a consumer that will never arrive.  This was a 5-second
                # cost per non-streaming test run.
                _has_stream_consumer = (
                    stream_consumer_holder
                    and stream_consumer_holder[0] is not None
                )
                if not _has_stream_consumer:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                else:
                    try:
                        await asyncio.wait_for(stream_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        stream_task.cancel()
                        try:
                            await stream_task
                        except asyncio.CancelledError:
                            pass

            # This must run inside `finally`: returns from the try block are
            # evaluated before finally executes, and code after finally is
            # skipped for those paths. Mutating the held result here ensures
            # the caller sees definitive stream-delivery state and can send a
            # trailing runtime footer instead of appending it to an unsent body.
            try:
                _delivery_response = locals().get("response")
                _delivery_candidate = (
                    _delivery_response
                    if isinstance(_delivery_response, dict)
                    else _held_agent_result
                )
                await _finalize_stream_delivery_state(_delivery_candidate)
                # Some early-return branches return the original agent result,
                # while the normal path returns the gateway's enriched response
                # dict. Propagate the confirmed flag to both mutable objects so
                # the exact object evaluated by `return` observes the update.
                if (
                    isinstance(_delivery_candidate, dict)
                    and _delivery_candidate.get("already_sent")
                    and isinstance(_held_agent_result, dict)
                ):
                    _held_agent_result["already_sent"] = True
            except Exception as _stream_state_err:
                # Delivery bookkeeping must never mask the agent's real result.
                logger.debug(
                    "stream delivery finalization failed for session %s: %s",
                    session_key or "?",
                    _stream_state_err,
                )

            # Unconditional abort + bounded wait for the streaming-TTS
            # consumer (#60671 hardening).  Covers cancellation / exception
            # paths where the normal finalisation block was skipped.
            _stts_finally = streaming_tts_consumer_holder[0]
            if _stts_finally is not None and not _stts_finally.done:
                _stts_finally.abort("cleanup")
                try:
                    await _stts_finally.wait_complete(timeout=2.0)
                except Exception:
                    pass

            # Clean up tracking
            tracking_task.cancel()
            if session_key:
                # Only release the slot if this run's generation still owns
                # it.  A /stop or /new that bumped the generation while we
                # were unwinding has already installed its own state; this
                # guard prevents an old run from clobbering it on the way
                # out.
                self._release_running_agent_state(
                    session_key, run_generation=run_generation
                )
            if self._draining:
                self._update_runtime_status("draining")

            # Wait for cancelled tasks
            for task in [progress_task, stage_task, log_task, interrupt_monitor, tracking_task, _notify_task]:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        # A background task that died of a non-cancellation
                        # error (transport drop in a progress/card publish)
                        # must not abort the cleanup path — everything after
                        # this loop (final-delivery bookkeeping) still runs
                        # (review B7).
                        logger.debug(
                            "background turn task failed during cleanup",
                            exc_info=True,
                        )

        # If streaming already delivered the response, mark it so the
        # caller's send() is skipped (avoiding duplicate messages).
        # BUT: never suppress delivery when the agent failed — the error
        # message is new content the user hasn't seen, and it must reach
        # them even if streaming had sent earlier partial output.
        #
        # Also never suppress when the final response is "(empty)" — this
        # means the model failed to produce content after tool calls (common
        # with mimo-v2-pro, GLM-5, etc.).  The stream consumer may have
        # sent intermediate text ("Let me search for that…") alongside the
        # tool call, setting already_sent=True, but that text is NOT the
        # final answer.  Suppressing delivery here leaves the user staring
        # at silence.  (#10xxx — "agent stops after web search")
        _sc = stream_consumer_holder[0]
        if isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            # response_previewed means the interim_assistant_callback already
            # saw the final text, but only suppress the normal send if that
            # exact final text was delivered. Unrelated commentary/progress
            # must not be mistaken for the final response (#14238).
            _previewed = bool(response.get("response_previewed"))
            _content_delivered = bool(
                _sc and getattr(_sc, "final_content_delivered", False)
            )
            # #71643: a *successful* finalize edit can still carry only the
            # last preview snapshot — deltas generated between that edit and
            # stream completion never reach any API call, and both suppression
            # flags are set from the call's success rather than its content.
            # Reconcile the consumer's recorded turn-final payload against the
            # completed response: on a demonstrable mismatch (False) neither
            # final_response_sent nor final_content_delivered may suppress the
            # normal final send. False also covers payload-less multi-message
            # split delivery (#78541). None (no record on a non-split legacy
            # path) keeps legacy trust; the failed-finalize family
            # (#51828 / #33793) is unaffected because those paths leave the
            # flags False or record the complete fallback payload.
            _stale_finalized = False
            if _content_delivered and not _is_empty_sentinel:
                _matcher = getattr(_sc, "delivered_final_matches", None)
                if callable(_matcher):
                    try:
                        _stale_finalized = _matcher(_final) is False
                    except Exception:
                        _stale_finalized = False
                if _stale_finalized:
                    _content_delivered = False
            # Plugin hooks (e.g. transform_llm_output) may have appended content
            # after streaming finished — when the response was transformed, always
            # send the final version so the appended content reaches the client.
            _transformed = bool(response.get("response_transformed"))
            # Only suppress the normal send when the actual final reply reached
            # the user: the stream consumer streamed it (final_response_sent /
            # final_content_delivered), or the interim preview delivered that
            # *exact* final text. Unrelated commentary/progress shown during a
            # compression/session split must not be mistaken for the final
            # response (#14238).
            _streamed = _stream_confirmed_final_delivery(
                _sc,
                _final,
                previewed=_previewed,
            )
            if not _is_empty_sentinel and not _transformed and (_streamed or _content_delivered):
                logger.info(
                    "Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).",
                    session_key or "?",
                    _streamed,
                    _previewed,
                    _content_delivered,
                )
                response["already_sent"] = True
            elif not _is_empty_sentinel and not _transformed and _stale_finalized and _sc is not None:
                # Stale finalize (#71643): the streamed message holds only the
                # last preview snapshot. Prefer editing it up to the complete
                # response (same shape as the transformed branch below) so the
                # user gets one corrected message; on edit failure fall through
                # with already_sent unset so the normal final send delivers the
                # complete text.
                #
                # Not valid for a multi-message split delivery: there
                # ``message_id`` is only the LAST chunk, so editing it with the
                # complete response would repeat every sealed head chunk's text
                # inside the tail message. Fall through to the normal final send
                # instead (#78541).
                _sc_msg_id = _sc.message_id
                _sc_adapter = getattr(_sc, "adapter", None)
                if getattr(_sc, "_turn_split_delivery", False):
                    logger.info(
                        "Stale streamed finalize detected for session %s on a multi-message split; skipping the in-place reconciliation edit and delivering the complete response via normal final send (#78541).",
                        session_key or "?",
                    )
                elif _sc_msg_id and _sc_msg_id != "__no_edit__" and _sc_adapter is not None:
                    try:
                        _reconcile_res = await _sc_adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=_sc_msg_id,
                            content=_final,
                            finalize=True,
                        )
                        if getattr(_reconcile_res, "success", True):
                            response["already_sent"] = True
                            logger.info(
                                "Reconciled stale streamed finalize for session %s: edited message %s with the complete response (#71643).",
                                session_key or "?", _sc_msg_id,
                            )
                        else:
                            logger.warning(
                                "Stale-finalize reconciliation edit failed for session %s (%s); sending complete response via normal final send.",
                                session_key or "?",
                                getattr(_reconcile_res, "error", None),
                            )
                    except Exception as _edit_err:
                        logger.warning(
                            "Stale-finalize reconciliation edit failed for session %s: %s; sending complete response via normal final send.",
                            session_key or "?", _edit_err,
                        )
                else:
                    logger.info(
                        "Stale streamed finalize detected for session %s with no editable message; delivering complete response via normal final send (#71643).",
                        session_key or "?",
                    )
            elif not _is_empty_sentinel and _transformed and _sc is not None:
                # Plugin hooks transformed the response after streaming — edit the
                # existing streamed message instead of sending a duplicate.
                _sc_msg_id = _sc.message_id
                if _sc_msg_id:
                    try:
                        await _sc.adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=_sc_msg_id,
                            content=response["final_response"],
                            finalize=True,
                        )
                        response["already_sent"] = True
                        logger.info(
                            "Edited streamed message %s for session %s to include plugin-transformed content.",
                            _sc_msg_id, session_key or "?",
                        )
                    except Exception as _edit_err:
                        logger.warning(
                            "Failed to edit streamed message for session %s: %s",
                            session_key or "?", _edit_err,
                        )
            elif _sc is not None and not _is_empty_sentinel:
                # DUPLICATE-RISK DIAGNOSTIC: a stream consumer existed for this
                # turn but suppression did NOT fire, so the gateway's normal
                # final-send is about to run. On WeCom this is the exact window
                # that produced "回复了两条" — a final-frame ack still in flight
                # (final_content_delivered not yet set) while this send races
                # ahead. Log the decision inputs so a recurrence can be pinned to
                # "signal never set" vs "ack-pending race".
                # See docs/rca-wecom-stream-final-ack-timeout-duplicate.md.
                logger.warning(
                    "Normal final-send NOT suppressed despite active stream "
                    "consumer for session %s: streamed=%s previewed=%s "
                    "content_delivered=%s transformed=%s final_len=%d — "
                    "possible duplicate send (see wecom ack-timeout RCA).",
                    session_key or "?",
                    _streamed,
                    _previewed,
                    _content_delivered,
                    _transformed,
                    len(_final),
                )

        # Schedule deletion of tracked temporary progress bubbles after the
        # final response lands. Failed runs skip this so bubbles remain as
        # breadcrumbs for the user to see what work happened. Only fires on
        # adapters that support ``delete_message`` (see init above); failures
        # are swallowed — deletion is best-effort.
        if (
            _cleanup_progress
            and _cleanup_adapter is not None
            and _cleanup_msg_ids
            and session_key
            and isinstance(response, dict)
            and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            _ids_snapshot = list(_cleanup_msg_ids)
            _chat_id_snapshot = source.chat_id
            _adapter_snapshot = _cleanup_adapter
            _loop_snapshot = asyncio.get_running_loop()

            def _cleanup_temp_bubbles() -> None:
                from gateway.run import safe_schedule_threadsafe
                async def _delete_all() -> None:
                    for _mid in _ids_snapshot:
                        try:
                            await _adapter_snapshot.delete_message(
                                _chat_id_snapshot, _mid
                            )
                        except Exception:
                            pass
                try:
                    safe_schedule_threadsafe(
                        _delete_all(), _loop_snapshot,
                        logger=logger,
                        log_message="Temp bubble cleanup scheduling error",
                    )
                except Exception:
                    pass

            try:
                _cleanup_adapter.register_post_delivery_callback(
                    session_key,
                    _cleanup_temp_bubbles,
                    generation=run_generation,
                )
            except Exception as _rpe:
                logger.debug("Post-delivery cleanup registration failed: %s", _rpe)

        return response
