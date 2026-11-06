"""Inbound message pipeline (_handle_message, text/media preparation, durable-turn markers, plugin injection) for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import asyncio
import concurrent.futures
import dataclasses
import json
import os
import re
import time
from contextlib import suppress
from gateway.config import Platform
from gateway.platforms.base import DeferredReply, EphemeralReply
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_common import _UNSET
from gateway.run_inbound_unauthorized import (
    PAIRING_RATE_LIMITED_REPLY, UnauthorizedOwnerNotifier, pairing_code_reply, pairing_profile_arg,
    unauthorized_owner_hint,
)
from gateway.session import (
    SessionSource, is_shared_multi_user_session, neutralize_untrusted_inline_text
)
from gateway.turn_lease import TurnLeaseTimeoutError
from typing import Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def discord_triggering_note(message_id: Any) -> str:
    """Model-facing routing note for a Discord turn (rides the API-bound user message only)."""
    return (
        f"[Triggering message id: `{message_id}` — use as `message_id` for reply/react/pin "
        f"via the discord tools.]"
    )


def strip_discord_triggering_note(event: Any, message_text: Any) -> Any:
    """Authored text for the durable user row: peel off exactly the note
    ``_prepend_inbound_reply_context`` added for THIS event, if present. The note is a
    model instruction, not something the user wrote — persisted as ``content`` it renders
    verbatim in every transcript surface and pollutes FTS/memory (#71304, #114719). It
    keeps riding ``message_text`` (and the replay-only ``api_content`` sidecar)."""
    message_id = getattr(event, "message_id", None)
    if not message_id or not isinstance(message_text, str):
        return message_text
    prefix = f"{discord_triggering_note(message_id)}\n\n"
    return message_text[len(prefix):] if message_text.startswith(prefix) else message_text


class GatewayInboundMixin:
    """Inbound message pipeline (_handle_message, text/media preparation, durable-turn markers, plugin injection) for GatewayRunner."""

    def _hm_pre_gateway_dispatch_hook(
        self, event: "MessageEvent", source: SessionSource
    ) -> Optional["MessageEvent"]:
        """Run the ``pre_gateway_dispatch`` plugin hook; None = drop, else the (maybe rewritten) event.
        Results: ``{"action": "skip"}`` → drop; ``{"action": "rewrite", "text"}`` → replace ``event.text``;
        ``allow``/None → normal dispatch. Runs BEFORE auth so plugins can handle unauthorized senders."""
        from gateway.run import logger

        try:
            from hermes_cli.lifecycle import invoke_hook as _invoke_hook
            _hook_results = _invoke_hook(
                "pre_gateway_dispatch", event=event, gateway=self,
                # getattr: bare-runner tests build GatewayRunner via object.__new__ without __init__.
                session_store=getattr(self, "session_store", None),
            )
        except Exception as _hook_exc:
            logger.warning("pre_gateway_dispatch invocation failed: %s", _hook_exc)
            _hook_results = []

        for _result in _hook_results:
            if not isinstance(_result, dict):
                continue
            _action = _result.get("action")
            if _action == "skip":
                logger.info(
                    "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s",
                    _result.get("reason"), source.platform.value if source.platform else "unknown",
                    source.chat_id or "unknown",
                )
                return None
            if _action == "rewrite":
                _new_text = _result.get("text")
                if isinstance(_new_text, str):
                    event = dataclasses.replace(event, text=_new_text)
                break
            if _action == "allow":
                break
        return event

    async def _hm_offer_pairing_code(self, source: SessionSource) -> None:
        """DM an unauthorized sender a pairing code (rate-limited; groups never reach here)."""
        from gateway.run import logger

        platform_name = source.platform.value if source.platform else "unknown"
        pairing_store = self._pairing_store_for(source)
        if pairing_store is None:
            logger.error("Cannot offer pairing code on %s: no pairing store", platform_name)
            return
        # Rate-limit ALL pairing responses (code or rejection) so a burst of DMs doesn't spam.
        if pairing_store._is_rate_limited(platform_name, source.user_id):
            return
        code = pairing_store.generate_code(platform_name, source.user_id, source.user_name or "")
        adapter = self._delivery_adapter_for(source)
        if code:
            reply = pairing_code_reply(platform_name, code, pairing_profile_arg(pairing_store))
        else:
            reply = PAIRING_RATE_LIMITED_REPLY
        if adapter:
            await adapter.send(source.chat_id, reply)
        if not code:
            # Record rate limit so subsequent messages are silently ignored
            pairing_store._record_rate_limit(platform_name, source.user_id)

    async def _hm_send_unauthorized_decline(self, source: SessionSource) -> None:
        """``decline`` behavior: one short refusal per sender per DECLINE_DEDUPE_SECONDS, then silence
        (port of qwibitai/nanoclaw#3260, #88028). The stamp is written BEFORE the send so a delivery
        hiccup cannot become a decline storm; without a store there is no dedupe state → stay silent."""
        from gateway.run import logger

        from gateway.config import DEFAULT_UNAUTHORIZED_DM_DECLINE_MESSAGE
        platform_name = source.platform.value if source.platform else "unknown"
        pairing_store = self._pairing_store_for(source)
        if pairing_store is None or pairing_store.has_recent_decline(platform_name, source.user_id):
            return
        pairing_store.record_decline(platform_name, source.user_id)
        adapter = self._delivery_adapter_for(source)
        if not adapter:
            return
        config = getattr(self, "config", None)
        text = str(getattr(config, "unauthorized_dm_decline_message", "") or "").strip()
        try:
            await adapter.send(source.chat_id, text or DEFAULT_UNAUTHORIZED_DM_DECLINE_MESSAGE)
        except Exception:
            logger.warning("Failed to deliver unauthorized-DM decline on %s", platform_name, exc_info=True)

    async def _hm_report_ignored_dm(self, source: SessionSource) -> None:
        """Unauthorized DM under behaviour ``ignore``: nothing goes to the sender. The owner gets the
        sender's ID and the allowlist fix in the WARNING log and, once per sender, in the home channel."""
        from gateway.run import logger

        from hermes_constants import display_hermes_home
        platform_name = source.platform.value if source.platform else "unknown"
        hint = unauthorized_owner_hint(
            platform_name, source.user_id, source.user_name or "", hermes_home=display_hermes_home(),
        )
        logger.warning("Unauthorized user (ignored): %s", hint)
        notifier = getattr(self, "_unauthorized_owner_notifier", None)
        if notifier is None:
            notifier = self._unauthorized_owner_notifier = UnauthorizedOwnerNotifier()
        if notifier.first_time(platform_name, source.user_id) and getattr(self, "config", None) is not None:
            await notifier.notify(self, source, hint)

    async def _hm_admit_event(
        self, event: "MessageEvent"
    ) -> Optional[Tuple["MessageEvent", SessionSource, bool]]:
        """Ingress gates for ``_handle_message``; None when dropped, else ``(event, source, is_internal)``
        (the ``pre_gateway_dispatch`` hook may have rewritten ``event``)."""
        from gateway.run import logger

        from gateway.run import _is_slack_ignored_channel
        source = event.source
        # getattr(self, ...) throughout: bare test runners build GatewayRunner via object.__new__.
        _config = getattr(self, "config", None)

        # 🔴 Cross-session leak guard: this per-message task was create_task()'d with a copy of the
        # spawning context, which may carry ANOTHER message's HERMES_SESSION_* ContextVars; until
        # _set_session_env binds ours a subprocess would read the foreign identity. Reset to _UNSET.
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug("reset_session_vars failed at handler entry", exc_info=True)

        # Identity FIRST. Most adapters canonicalize at their own ingress; internal/voice paths
        # construct SessionSource directly, so this is the shared fail-closed gate. Strict boolean
        # marker: require the literal True so duck-typed test/internal sources with dynamic
        # attributes are not mistaken for a rejection.
        if getattr(_config, "multiplex_profiles", False):
            self._canonicalize(source)
        if getattr(source, "profile_route_rejected", False) is True:
            logger.warning(
                "Dropping inbound message because its explicit profile route "
                "targets an unserved profile"
            )
            return None

        is_internal = bool(getattr(event, "internal", False))  # e.g. background-process notifications

        # Ignored-channel guard runs FIRST — before startup-restore queueing, plugin hooks, auth,
        # and session setup — so an ignored channel can never reach pairing/auth/session state.
        _chat_id = getattr(source, "chat_id", None)
        if not is_internal and getattr(source, "platform", None) == Platform.SLACK:
            # The routed adapter's extra carries a secondary profile's own list; ``_config`` is the default's.
            _slack_adapter = None
            with suppress(Exception):
                _slack_adapter = self._intake_adapter_for(source)
        if (
            # See #51899.
            not is_internal
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(_config, _chat_id, _slack_adapter)
        ):
            logger.info("Dropping Slack message from configured ignored channel %s", _chat_id)
            return None

        if (
            getattr(self, "_startup_restore_in_progress", False)
            and not is_internal
            and not getattr(event, "_hermes_startup_restore_replay", False)
        ):
            self._queue_startup_restore_event(event)
            return None

        if is_internal:
            return event, source, True

        # scale-to-zero: only real user-originated inbound stamps the last-inbound clock;
        # counting internal/system events would keep a genuinely idle gateway awake.
        self._scale_to_zero_note_real_inbound()
        event = self._hm_pre_gateway_dispatch_hook(event, source)
        if event is None:
            return None
        source = event.source

        if not self._is_user_authorized_for_source(source):
            if source.user_id is None:
                # No user identity (Telegram service messages, channel forwards, anonymous admin
                # posts, sender_chat): can't be paired but may be authorized via a chat allowlist.
                logger.debug("Ignoring message with no user_id from %s", source.platform.value)
                return None
            # DMs get a pairing code or a one-time decline, groups are ignored. A bot cannot pair, and
            # answering one mid-cooldown is outbound traffic.
            pairable_dm = source.chat_type == "dm" and not getattr(source, "is_bot", False)
            behavior = self._get_unauthorized_dm_behavior(source.platform, profile=source.profile) if pairable_dm else None
            if behavior == "pair":
                logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
                await self._hm_offer_pairing_code(source)
            elif behavior == "decline":
                logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
                await self._hm_send_unauthorized_decline(source)
            elif pairable_dm:
                await self._hm_report_ignored_dm(source)
            else:
                logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            return None
        # The busy path charged this event on arrival; a drained follow-up must not pay twice.
        if not getattr(event, "_bot_loop_admitted", False) and not self._admit_bot_message_for_source(source):
            return None
        return event, source, False

    def _hm_estop_turn_allowed(self, event: "MessageEvent", source: SessionSource) -> bool:
        """Whether a turn may bypass the global emergency stop: pause blocks NEW agent turns, never
        running work or control traffic — recognized slash commands (incl. /pause off, the in-band
        resume) and replies owned by in-flight work (pending update prompt, running session,
        pending slash-confirm, dangerous-command approval) all pass through."""
        with suppress(Exception):
            _estop_cmd = event.get_command()
            if _estop_cmd:
                from hermes_cli.commands import resolve_command as _resolve_estop_cmd
                if _resolve_estop_cmd(_estop_cmd) is not None:
                    return True
        with suppress(Exception):
            _estop_key = self._session_key_for_source(source)
            _estop_state = self._peek_session_state(_estop_key)
            if _estop_state is not None and _estop_state.persistent.update_prompt_pending:
                return True
            # A running session covers steering plus pending clarify / tool approvals it holds.
            if self._is_session_running(_estop_key):
                return True
            from tools import slash_confirm as _estop_confirm_mod
            if _estop_confirm_mod.get_pending(_estop_key):
                return True
            from tools.approval import has_blocking_approval as _estop_has_approval
            if _estop_has_approval(_estop_key):
                return True
        return False

    def _hm_estop_gate(
        self, event: "MessageEvent", source: SessionSource, is_internal: bool
    ) -> Optional[str]:
        """Global emergency-stop (`hermes pause`) notice when this turn must be blocked, else None.
        Placed after auth so unauthorized senders can't probe pause state."""
        from gateway.run import logger

        if is_internal:
            return None
        try:
            from agent.estop import paused_reply as _estop_paused_reply
        except ImportError:
            return None
        _paused_notice = _estop_paused_reply()
        if _paused_notice is None or self._hm_estop_turn_allowed(event, source):
            return None
        logger.info(
            "Gateway turn paused by global emergency stop (platform=%s chat=%s)",
            getattr(getattr(source, "platform", None), "value", "unknown"),
            getattr(source, "chat_id", None) or "unknown",
        )
        return _paused_notice

    @staticmethod
    def _hm_write_update_response(response_text: str) -> Optional[str]:
        """Atomically hand *response_text* to the detached update process; returns the OSError str."""
        from gateway.run import _hermes_home
        response_path = _hermes_home / ".update_response"
        try:
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(response_text, encoding="utf-8")
            tmp.replace(response_path)
            (_hermes_home / ".update_prompt.json").unlink(missing_ok=True)
        except OSError as e:
            return str(e)
        return None

    def _hm_update_prompt_reply(self, event: "MessageEvent", _quick_key: str) -> Optional[str]:
        """Consume a reply to a pending ``/update`` prompt (routed to the detached update process via
        ``.update_response``); None when nothing was consumed. Recognized slash commands must bypass
        this or /new, /help etc. get silently consumed as update answers."""
        from gateway.run import logger

        _up_state = self._peek_session_state(_quick_key)
        if _up_state is None or not _up_state.persistent.update_prompt_pending:
            return None
        # Accept /approve and /deny as shorthand for yes/no
        cmd = event.get_command()
        _recognized_cmd = None
        if cmd in {"approve", "yes"}:
            response_text = "y"
        elif cmd in {"deny", "no"}:
            response_text = "n"
        else:
            if cmd:
                with suppress(Exception):
                    from hermes_cli.commands import resolve_command as _resolve_update_cmd
                    _cmd_def = _resolve_update_cmd(cmd)
                    _recognized_cmd = _cmd_def.name if _cmd_def else None
            response_text = "" if _recognized_cmd else (event.text or "").strip()
        if response_text:
            err = self._hm_write_update_response(response_text)
            if err is not None:
                logger.warning("Failed to write update response: %s", err)
                return f"✗ Failed to send response to update process: {err}"
            _up_state.persistent.update_prompt_pending = False
            label = response_text if len(response_text) <= 20 else response_text[:20] + "…"
            return f"✓ Sent `{label}` to the update process."
        # Recognized slash command during a pending update prompt: write a blank response so the
        # detached update's ``_gateway_prompt`` returns the prompt's default (typically a safe
        # "n" / skip) and exits instead of blocking on stdin until the watcher timeout.
        if _recognized_cmd:
            err = self._hm_write_update_response("")
            if err is None:
                logger.info(
                    "Recognized /%s during pending update prompt for %s; "
                    "cancelled prompt with default and dispatching command",
                    _recognized_cmd, _quick_key,
                )
            else:
                logger.warning("Failed to write cancel response for pending update prompt: %s", err)
            _up_state.persistent.update_prompt_pending = False
        return None

    async def _hm_clarify_reply(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str
    ) -> Optional[str]:
        """Intercept a reply to a pending clarify prompt; None when the message falls through.
        Free text answers open-ended/"Other" prompts; "2" answers a multi-choice one. Resolved/retained
        replies return "" so adapters don't double-post — the agent produces the next user-facing message."""
        from gateway.run import logger

        try:
            from tools import clarify_gateway as _clarify_mod
            _pending_clarify = _clarify_mod.get_pending_for_session(_quick_key, include_choice_prompts=True)
        except Exception:
            return None
        if _pending_clarify is None:
            return None
        _clarify_has_audio = bool(self._pending_event_audio_paths(event))
        _raw_clarify_reply = await self._prepare_clarify_reply_text(event)

        def _retain(why: str) -> str:
            logger.info(
                "Gateway retained pending clarify after %s (session=%s, id=%s)",
                why, _quick_key, _pending_clarify.clarify_id,
            )
            return ""

        if _clarify_has_audio and not _raw_clarify_reply:
            return _retain("voice transcription produced no usable text")
        # Slash commands: the user wanted a command, not to answer the clarify. Leave it pending so
        # they can retry; on timeout the agent unblocks with an empty response.
        if not _raw_clarify_reply or _raw_clarify_reply.startswith("/"):
            return None
        _text_outcome = _clarify_mod.attempt_text_response_for_session(_quick_key, _raw_clarify_reply)
        if _text_outcome == _clarify_mod.TEXT_RESOLVED:
            logger.info(
                "Gateway intercepted clarify text response (session=%s, id=%s)",
                _quick_key, _pending_clarify.clarify_id,
            )
            # The clarify callback pauses the platform typing/status indicator while waiting so
            # Slack users can type; the active agent resumes now, so re-enable its indicator.
            _clarify_adapter = self._delivery_adapter_for(source)
            if _clarify_adapter:
                try:
                    _clarify_adapter.resume_typing_for_chat(source.chat_id)
                except Exception:
                    logger.debug("Failed to resume typing after clarify response", exc_info=True)
                # A typed answer to a native card (numeric pick, or text after "Other") never
                # reaches the click handler, so the card would keep its buttons forever.
                if callable(getattr(type(_clarify_adapter), "retire_clarify_card", None)):
                    try:
                        await _clarify_adapter.retire_clarify_card(
                            _pending_clarify.clarify_id,
                            f"✅ answered: {_pending_clarify.response or _raw_clarify_reply}")
                    except Exception:
                        logger.debug("Failed to retire clarify card after typed answer", exc_info=True)
            return ""
        if _text_outcome == _clarify_mod.TEXT_REJECTED_SELECTION:
            # Selection-shaped but invalid (out-of-range number, bad comma-list): keep the clarify
            # armed for retry — don't cancel, don't treat as an unrelated follow-up.
            return _retain("invalid selection attempt")
        if _text_outcome == _clarify_mod.TEXT_REJECTED_PROSE:
            # Native-choice prompts reject unmatched prose so it continues through normal busy
            # routing. Release this clarify first: redirect() degrades to steer() while tools
            # execute, and that steer cannot drain until the clarify tool returns.
            if _clarify_mod.resolve_gateway_clarify(_pending_clarify.clarify_id, ""):
                # Adapters with a persistent native card (Slack Block Kit) retire it now, before the
                # prose is routed, so its buttons stop advertising a dead answer path. The pop inside
                # retire_clarify_card runs before its first await, so the agent thread's own expiry
                # notice (scheduled once the wait unblocks) finds nothing and stays a no-op.
                _clarify_adapter = self._delivery_adapter_for(source)
                # Class lookup: a MagicMock adapter must not fabricate the method.
                if callable(getattr(type(_clarify_adapter), "retire_clarify_card", None)):
                    try:
                        await _clarify_adapter.retire_clarify_card(
                            _pending_clarify.clarify_id,
                            "↩️ Clarification cancelled — your message will be handled as a follow-up.")
                    except Exception:
                        logger.debug("Failed to retire clarify card after prose cancellation", exc_info=True)
        return None

    # Reply → choice for a pending slash-confirm prompt; the command spelling wins over the
    # bang/slash-stripped free-text spelling.
    _SLASH_CONFIRM_CMD_CHOICES = {
        "approve": "once", "yes": "once", "ok": "once", "confirm": "once",
        "always": "always", "remember": "always",
        "cancel": "cancel", "no": "cancel", "deny": "cancel", "nevermind": "cancel",
    }
    _SLASH_CONFIRM_TEXT_CHOICES = {
        "approve": "once", "approve once": "once", "once": "once",
        "always": "always", "always approve": "always",
        "cancel": "cancel", "nevermind": "cancel", "no": "cancel",
    }

    async def _hm_slash_confirm_reply(self, event: "MessageEvent", _quick_key: str) -> Optional[str]:
        """Resolve a reply (/approve, /always, /cancel + aliases) to a pending slash-confirm prompt;
        None when it falls through — a stale pending confirm does NOT block other commands. A pending
        dangerous-command approval takes precedence: /approve there unblocks the waiting tool thread."""
        from tools import slash_confirm as _slash_confirm_mod
        _pending_confirm = _slash_confirm_mod.get_pending(_quick_key)
        if not _pending_confirm:
            return None
        with suppress(Exception):
            from tools.approval import has_blocking_approval
            if has_blocking_approval(_quick_key):
                return None
        # Accept bang-prefixed replies (`!always`, `!cancel`) verbatim: Slack/Matrix show the `!`
        # prefix (typed `/` is blocked in Slack threads) and adapters only rewrite
        # `!<known-command>` — confirm keywords aren't commands, so the `!` survives to here.
        _norm_reply = (event.text or "").strip().lstrip("!/").lower()
        _confirm_choice = (
            self._SLASH_CONFIRM_CMD_CHOICES.get(event.get_command())
            or self._SLASH_CONFIRM_TEXT_CHOICES.get(_norm_reply)
        )
        if _confirm_choice is not None:
            _resolved = await _slash_confirm_mod.resolve(
                _quick_key, _pending_confirm.get("confirm_id"), _confirm_choice,
            )
            return _resolved or ""
        # Stale pending + unrelated command: the user moved on, so drop the pending state rather
        # than let the confirm block normal usage indefinitely.
        _slash_confirm_mod.clear_if_stale(_quick_key)
        return None

    def _hm_evict_idle_stale_agent(self, _quick_key: str) -> None:
        """Evict a leaked lock from a hung/crashed handler: only when the agent has been *idle* past
        the threshold (active tasks can run for hours), or has no activity tracker and an extreme
        wall-clock age. The pending sentinel is never evicted (no get_activity_summary() → idle
        reads inf and would race the async setup path)."""
        from gateway.run import logger

        from gateway.run import _AGENT_PENDING_SENTINEL, _float_env
        _raw_stale_timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800)
        _quick_state = self._peek_session_state(_quick_key)
        _stale_ts = _quick_state.turn.started_ts if _quick_state else 0
        if _quick_state is None or _quick_state.turn.agent is None or not _stale_ts:
            return
        _stale_age = time.time() - _stale_ts
        _stale_agent = _quick_state.turn.agent
        _stale_idle = float("inf")  # assume idle if we can't check
        _stale_detail = ""
        _activity_summary_valid = False
        if _stale_agent and hasattr(_stale_agent, "get_activity_summary"):
            with suppress(Exception):
                _sa = _stale_agent.get_activity_summary()
                from gateway.session_stall import resolve_session_idle_seconds_from_activity

                _sa_d = _sa if isinstance(_sa, dict) else {}
                _resolved_idle = resolve_session_idle_seconds_from_activity(
                    _sa if isinstance(_sa, dict) else None, now=time.time(),
                )
                if _resolved_idle is not None:
                    _stale_idle = _resolved_idle
                    _activity_summary_valid = True
                _stale_detail = (
                    f" | last_activity={_sa_d.get('last_activity_desc', 'unknown')} "
                    f"({_stale_idle:.0f}s ago) "
                    f"| iteration={_sa_d.get('api_call_count', 0)}/{_sa_d.get('max_iterations', 0)}"
                )
        # A valid activity clock is authoritative: total age alone never makes an actively
        # progressing turn stale. The emergency wall TTL is only a fallback when the agent cannot
        # report usable activity.
        _wall_ttl = max(_raw_stale_timeout * 10, 7200) if _raw_stale_timeout > 0 else float("inf")
        _should_evict = _stale_agent is not _AGENT_PENDING_SENTINEL and (
            (_activity_summary_valid and _raw_stale_timeout > 0 and _stale_idle >= _raw_stale_timeout)
            or (not _activity_summary_valid and _stale_age > _wall_ttl)
        )
        if _should_evict:
            logger.warning(
                "Evicting stale _running_agents entry for %s "
                "(age: %.0fs, idle: %.0fs, timeout: %.0fs)%s",
                _quick_key, _stale_age, _stale_idle, _raw_stale_timeout, _stale_detail,
            )
            self._hm_evict_running_agent(_quick_key, "stale_running_agent_eviction")

    def _hm_evict_reaped_agent(self, _quick_key: str) -> None:
        """Evict the in-memory turn slot of a session whose durable row was ended while the gateway
        lived (``ws_orphan_reap`` / ``agent_close``): otherwise the fast-path queues every next
        message into the dead runtime. The cold path re-attaches via ``get_or_create_session``."""
        from gateway.run import logger

        try:
            # #99106: durable-reaped guard. This is the live-gateway variant of #54878 and the #632
            # detached/ 405 suppressions in production. Evict the stale slot so the next message falls
            # through to the cold path and re-attaches or creates a fresh session; /status then correctly
            # shows 代理运行中: 否 before the heal and a live turn after.
            _reap_store = getattr(self, "session_store", None)
            # Public, lock-held accessors: peek_session_id returns a non-str on stubbed stores in
            # bare test runners — the isinstance() / ``is True`` gates keep this inert unless a
            # real SessionStore answers.
            _reap_peek = getattr(_reap_store, "peek_session_id", None)
            _is_ended = getattr(_reap_store, "_is_session_ended_in_db", None)
            _reap_sid = _reap_peek(_quick_key) if callable(_reap_peek) else None
            if isinstance(_reap_sid, str) and _reap_sid and callable(_is_ended) and _is_ended(_reap_sid) is True:
                logger.warning(
                    "Evicting stale _running_agents entry for %s — "
                    "durable session %s is ended (reaped) in state.db; "
                    "healing routing on next message (#99106)", _quick_key, _reap_sid,
                )
                self._hm_evict_running_agent(_quick_key, "reaped_session_eviction")
        except Exception:
            logger.debug("reaped-session staleness check failed", exc_info=True)

    def _hm_evict_running_agent(self, _quick_key: str, reason: str) -> None:
        from gateway.run import _INTERRUPT_REASON_EVICTED, _INTERRUPT_TOOL_REASON_EVICTED
        _generation_at_interrupt = self._interrupt_running_turn(
            _quick_key, interrupt_reason=_INTERRUPT_REASON_EVICTED, invalidation_reason=reason,
            tool_reason=_INTERRUPT_TOOL_REASON_EVICTED)
        self._drop_turn_slot(_quick_key, run_generation=_generation_at_interrupt)

    def _hm_merge_pending_for_source(
        self, source: SessionSource, _quick_key: str, event: "MessageEvent", *, merge_text: bool = False
    ) -> None:
        """Merge *event* into the source adapter's pending slot (no-op without an adapter)."""
        from gateway.platforms.base import merge_pending_message_event
        adapter = self._delivery_adapter_for(source)
        if adapter:
            merge_pending_message_event(adapter._pending_messages, _quick_key, event, merge_text=merge_text)

    async def _hm_busy_slash_or_photo(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str
    ) -> Tuple[bool, Optional[str]]:
        """Slash-command / photo-burst handling on the busy fast-path → ``(handled, result)``. Each
        command's mid-run behavior is declared on its CommandDef (busy_policy / busy_handler)."""
        from gateway.run import logger

        from hermes_cli.commands import resolve_command as _resolve_cmd_inner
        _evt_cmd = event.get_command()
        _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None

        if _cmd_def_inner:
            # /status and /context are intentionally pre-gate so users always see session state.
            if _cmd_def_inner.name == "status":
                return True, await self._handle_status_command(event)
            if _cmd_def_inner.name == "context":
                return True, await self._handle_context_command(event)
            # Slash access control mirrors the cold-path gate so non-admins can't bypass gating
            # just because an agent is busy. /help and /whoami are the always-allowed floor.
            _denied = self._check_slash_access(source, _cmd_def_inner.name)
            if _denied is not None:
                return True, _denied
            # Any recognized slash command dispatches per its declared busy_policy (dispatch /
            # interrupt_then_dispatch / reject). Unrecognized commands and plain text fall through.
            return True, await self._dispatch_busy_slash_command(event, _cmd_def_inner, _quick_key, source)

        # Telegram photo bursts arrive as near-simultaneous updates — never interrupt for a
        # photo-only follow-up; adapter-level batching absorbs them.
        if event.message_type == MessageType.PHOTO:
            logger.debug("PRIORITY photo follow-up for session %s — queueing without interrupt", _quick_key)
            self._hm_merge_pending_for_source(source, _quick_key, event)
            return True, None
        return False, None

    def _hm_busy_telegram_grace_queue(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str, effective_busy_input_mode: str
    ) -> bool:
        """Queue a Telegram text follow-up that lands within the post-start grace window."""
        from gateway.run import logger

        _grace = float(os.getenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0"))
        _grace_state = self._peek_session_state(_quick_key)
        _started_at = _grace_state.turn.started_ts if _grace_state else 0
        if not (
            source.platform == Platform.TELEGRAM and event.message_type == MessageType.TEXT
            and _grace > 0 and _started_at and (time.time() - _started_at) <= _grace
        ):
            return False
        logger.debug(
            "Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt",
            time.time() - _started_at, _quick_key,
        )
        if effective_busy_input_mode != "queue":
            self._hm_merge_pending_for_source(source, _quick_key, event, merge_text=True)
        else:
            adapter = self._delivery_adapter_for(source)
            if adapter:
                self._enqueue_fifo(_quick_key, event, adapter)
        return True

    @staticmethod
    def _hm_text_only(event: "MessageEvent") -> bool:
        return event.message_type == MessageType.TEXT and not event.media_urls and not event.media_types

    def _hm_busy_steer(self, event: "MessageEvent", running_agent: Any, _quick_key: str) -> bool:
        """Steer mode: inject text mid-run via ``agent.steer()``, else fall back to queue semantics.

        Returns whether the steer landed; the async caller (_hm_handle_running_session_message)
        turns a True result into the same receipts the classic busy handler emits."""
        from gateway.run import logger

        steer_text = (event.text or "").strip()
        steered = False
        if self._hm_text_only(event) and steer_text and hasattr(running_agent, "steer"):
            try:
                steered = self._steer_running_agent(running_agent, self._steer_text_with_origin(steer_text, event))
            except Exception as exc:
                logger.warning("PRIORITY steer failed for session %s: %s", _quick_key, exc)
        if steered:
            logger.debug("PRIORITY steer for session %s", _quick_key)
            return True
        logger.debug("PRIORITY steer-fallback-to-queue for session %s", _quick_key)
        self._queue_or_replace_pending_event(_quick_key, event)
        return False

    async def _hm_busy_interrupt(
        self, event: "MessageEvent", source: SessionSource, running_agent: Any, _quick_key: str
    ) -> None:
        """Interrupt path: redirect text-only corrections when supported, else ``agent.interrupt()``."""
        from gateway.run import _build_media_placeholder, logger

        # Text-only corrections redirect the live turn (preserving displayed context) when the
        # runtime supports it; media/voice and older runtimes use the interrupt path below.
        _can_redirect = getattr(running_agent, "_supports_active_turn_redirect", False) is True
        if self._hm_text_only(event) and _can_redirect and hasattr(running_agent, "redirect"):
            try:
                if running_agent.redirect(
                    self._steer_text_with_origin((event.text or "").strip(), event)
                ):
                    logger.debug("PRIORITY redirect for session %s", _quick_key)
                    return
            except Exception as exc:
                logger.warning("PRIORITY redirect failed for session %s: %s", _quick_key, exc)
        logger.debug("PRIORITY interrupt for session %s", _quick_key)
        _interrupt_text = event.text
        if self._pending_event_audio_paths(event):
            _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                event, self._adapter_for_source(source), source, event.text or "",
                log_context="Voice-priority-interrupt",
            )
        elif not _interrupt_text and getattr(event, "media_urls", None):
            _interrupt_text = _build_media_placeholder(event)
        # Delivered via adapter._pending_messages (read by _run_agent); never also buffered on self
        # — that copy was never consumed and grew unbounded.
        running_agent.interrupt(_interrupt_text)

    async def _hm_handle_running_session_message(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str
    ) -> Optional[str]:
        """Fast-path while this session's agent is running: interrupt by default (minimal latency);
        busy_input_mode queue/steer, subagent and compression protection demote to queue."""
        from gateway.run import logger

        from gateway.run import _AGENT_PENDING_SENTINEL
        _handled, _result = await self._hm_busy_slash_or_photo(event, source, _quick_key)
        if _handled:
            return _result

        effective_busy_input_mode = self._effective_busy_input_mode(source)
        if self._hm_busy_telegram_grace_queue(event, source, _quick_key, effective_busy_input_mode):
            return None

        _ra_state = self._peek_session_state(_quick_key)
        running_agent = _ra_state.turn.agent if _ra_state else None
        if running_agent is _AGENT_PENDING_SENTINEL:  # agent still being set up
            if event.get_command() == "stop":  # force-clean the sentinel so the session is unlocked
                self._release_running_agent_state(_quick_key)
                logger.info("HARD STOP (pending) for session %s — sentinel cleared", _quick_key)
                return EphemeralReply("⚡ Force-stopped. The agent was still starting — session unlocked.")
            self._hm_merge_pending_for_source(source, _quick_key, event, merge_text=True)  # picked up after start
            return None
        from gateway.overlap_restart import admits_owned_work
        if self._draining and not admits_owned_work(self, event.source):
            queue_during_drain = self._queue_during_drain_enabled(effective_busy_input_mode)
            if queue_during_drain:
                self._queue_or_replace_pending_event(_quick_key, event)
            return DeferredReply(
                f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
                if queue_during_drain
                else f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."
            )
        if effective_busy_input_mode == "queue":
            logger.debug("PRIORITY queue follow-up for session %s", _quick_key)
            self._queue_or_replace_pending_event(_quick_key, event)
            return None
        if effective_busy_input_mode == "steer":
            steered = self._hm_busy_steer(event, running_agent, _quick_key)
            if steered:
                # Receipt parity with the classic busy handler
                # (_handle_active_session_busy_message): a steered follow-up
                # gets the Discord ⏩ read-receipt on its own source message
                # plus the one-shot "steer delivered" ack. The priority path
                # stays bubble-silent — these two receipts are all it emits —
                # and a failure here must never unwind a delivered steer.
                try:
                    if self._steer_delivered_ack_enabled(event):
                        self._register_steer_delivered_ack(event, _quick_key, running_agent)
                    await self._maybe_react_discord_busy_steer(
                        event, self._delivery_adapter_for(source), _quick_key,
                        steered=True, is_steer_mode=True,
                    )
                except Exception:
                    logger.debug(
                        "PRIORITY steer receipt failed for session %s", _quick_key, exc_info=True
                    )
            return None
        # Subagent protection: an interrupt cascades through ``_active_children`` and aborts
        # in-flight delegate_task work (/stop reached its handler above — still an escape hatch).
        # Compression protection: an interrupt would start a new turn on the pre-rotation parent
        # while compression rotates the id away, forking orphaned siblings.
        if self._agent_has_active_subagents(running_agent):
            _demote = "because the running agent has active subagents (#30170)"
        elif await self._session_has_compression_in_flight(_quick_key):
            _demote = "because context compression is in flight (#56391)"
        else:
            await self._hm_busy_interrupt(event, source, running_agent, _quick_key)
            return None
        logger.info("PRIORITY interrupt demoted to queue for session %s %s", _quick_key, _demote)
        self._queue_or_replace_pending_event(_quick_key, event)
        return None

    def _hm_quick_commands(self) -> dict:
        """User-defined ``quick_commands`` mapping from config (empty dict when unset/malformed)."""
        cfg = self.config
        qc = (cfg.get("quick_commands") if isinstance(cfg, dict) else getattr(cfg, "quick_commands", None)) or {}
        return qc if isinstance(qc, dict) else {}

    @staticmethod
    def _hm_expand_alias_quick_command(event: "MessageEvent", qcmd: dict) -> Optional[str]:
        """Rewrite ``event.text`` to an alias quick command's target; returns the new command name."""
        target = (qcmd.get("target") or "").strip()
        if not target:
            return None
        target = target if target.startswith("/") else f"/{target}"
        event.text = f"{target} {event.get_command_args().strip()}".strip()
        target_command = target.lstrip("/")
        return target_command.split()[0] if target_command else target_command

    async def _hm_command_hooks(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str, command: str, canonical: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Fire ``pre_command`` (observer) and ``command:<canonical>`` (interceptor) hooks →
        ``(handled, result, new_command)`` (``new_command`` set when a handler rewrote the command).
        The running-agent path deliberately does NOT fire these — a slow or hostile plugin must not
        interfere with the operator's escape hatches for a live agent."""
        from gateway.run import logger

        raw_args = event.get_command_args().strip()
        platform = source.platform.value if source.platform else ""
        try:
            from hermes_cli.plugins import fire_pre_command_hook
            fire_pre_command_hook(
                surface="gateway", command=str(canonical), alias_used=str(command),
                args_raw=raw_args, session_key=_quick_key, platform=platform,
            )
        except Exception as _pre_cmd_err:
            logger.debug("pre_command hook dispatch failed (non-fatal): %s", _pre_cmd_err)

        # Handlers may return ``{"decision": "deny" | "handled" | "rewrite", ...}`` to intercept
        # dispatch; handlers returning nothing behave as plain observers.
        hook_ctx = {
            "platform": platform, "user_id": source.user_id, "command": canonical,
            "raw_command": command, "args": raw_args, "raw_args": raw_args,
        }
        try:
            hook_results = await self.hooks.emit_collect(f"command:{canonical}", hook_ctx)
        except Exception as _hook_err:
            logger.debug("command:%s hook dispatch failed (non-fatal): %s", canonical, _hook_err)
            hook_results = []

        for hook_result in hook_results:
            if not isinstance(hook_result, dict):
                continue
            decision = str(hook_result.get("decision", "")).strip().lower()
            message = hook_result.get("message")
            message = message if isinstance(message, str) and message else None
            if decision == "deny":
                return True, message or f"Command `/{command}` was blocked by a hook.", None
            if decision == "handled":
                return True, message, None
            if decision == "rewrite":
                new_command = str(hook_result.get("command_name", "")).strip().lstrip("/")
                if new_command:
                    event.text = f"/{new_command} {str(hook_result.get('raw_args', '')).strip()}".strip()
                    return False, None, event.get_command()
        return False, None, None

    async def _hm_resolve_command(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """Resolve the slash command (aliases, access gate, hooks) → ``(handled, result, command,
        canonical)``; when ``handled`` the caller returns ``result`` as-is (may be None)."""
        from hermes_cli.commands import is_gateway_known_command, resolve_command as _resolve_cmd

        def _canon(cmd):
            # Aliases resolve to the canonical name so dispatch and hook names don't depend on them.
            _def = _resolve_cmd(cmd) if cmd else None
            return _def, (_def.name if _def else cmd)

        command = event.get_command()
        _cmd_def, canonical = _canon(command)

        # Expand alias quick commands before built-in dispatch so targets like /model openai/gpt-5.5
        # --provider openrouter reach the /model handler. Built-ins keep precedence: aliases only
        # need early handling when the typed command is not already known.
        if command and _cmd_def is None:
            qcmd = self._hm_quick_commands().get(command)
            if qcmd is not None and qcmd.get("type") == "alias":
                new_command = self._hm_expand_alias_quick_command(event, qcmd)
                if new_command is not None:
                    command = new_command
                    _cmd_def, canonical = _canon(command)

        if not (command and canonical and is_gateway_known_command(canonical)):
            return False, None, command, canonical

        # Per-platform slash access control: only active when the operator set ``allow_admin_from``
        # for the source's scope; then non-admins get ``user_allowed_commands`` plus the
        # /help, /whoami floor. Plain chat is never gated.
        _denied = self._check_slash_access(source, canonical)
        if _denied is not None:
            return True, _denied, command, canonical

        _handled, _result, new_command = await self._hm_command_hooks(
            event, source, _quick_key, command, canonical
        )
        if _handled:
            return True, _result, command, canonical
        if new_command is not None:
            command = new_command
            _cmd_def, canonical = _canon(command)
        return False, None, command, canonical

    async def _hm_confirm_destructive(self, event, command: str, detail: str, handler) -> Tuple[bool, Optional[str]]:
        async def _execute():
            return await handler(event)
        return True, await self._maybe_confirm_destructive_slash(
            event=event, command=command, title=f"/{command}", detail=detail, execute=_execute,
        )

    async def _hm_cmd_new(self, event, source, _quick_key):
        if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
            return True, self._telegram_topic_root_new_message()
        return await self._hm_confirm_destructive(
            event, "new", "This starts a fresh session and discards the current conversation history.",
            self._handle_reset_command,
        )

    async def _hm_cmd_start(self, event, source, _quick_key):
        from gateway.run import logger

        logger.info("Ignoring /start platform ping for session %s", _quick_key)
        return True, ""

    async def _hm_cmd_egress(self, event, source, _quick_key):
        from hermes_cli.proxy_cli import format_status_text
        return True, format_status_text()

    async def _hm_rewrite_turn_to_prompt(self, event, source, name: str, ack: str, build) -> Tuple[bool, Optional[str]]:
        """Ack, then rewrite the turn to ``build()`` and fall through to the agent (keeps role
        alternation; works on any backend). A failing builder replies with a retry hint."""
        await self._send_command_ack(source, ack, name)
        try:
            event.text = build()
        except Exception:
            return True, f"Could not start /{name} — please try again."
        return False, None

    # /learn and /plan: ack, rewrite the turn to a builder prompt, fall through to the agent.
    async def _hm_cmd_learn(self, event, source, _quick_key):
        from agent.learn_prompt import build_learn_prompt

        req = event.get_command_args().strip()
        _ack = f"Learning a skill from {'what you described' if req else 'this conversation'}…"
        return await self._hm_rewrite_turn_to_prompt(event, source, "learn", _ack, lambda: build_learn_prompt(req))

    async def _hm_cmd_plan(self, event, source, _quick_key):
        from agent.plan_prompt import build_plan_prompt

        task = event.get_command_args().strip()
        _ack = f"Planning: {task[:80]}{'…' if len(task) > 80 else ''}" if task else "Planning from this conversation's context…"
        return await self._hm_rewrite_turn_to_prompt(event, source, "plan", _ack, lambda: build_plan_prompt(task))

    async def _hm_cmd_init(self, event, source, _quick_key):
        # /init builds the prompt first: the ack wording depends on whether AGENTS.md exists.
        from hermes_cli.init_command import build_init_prompt_for_cwd

        try:
            _init_prompt = build_init_prompt_for_cwd(extra=event.get_command_args().strip())
        except Exception:
            return True, "Could not start /init — please try again."
        _ack = (
            "Updating AGENTS.md from a project scan…"
            if "UPDATE the existing AGENTS.md" in _init_prompt
            else "Generating AGENTS.md from a project scan…"
        )
        await self._send_command_ack(source, _ack, "init")
        event.text = _init_prompt
        return False, None

    async def _hm_cmd_blueprint(self, event, source, _quick_key):
        _blueprint_result = await self._handle_blueprint_command(event)
        _text = getattr(_blueprint_result, "text", "") or ""
        _blueprint_seed = getattr(_blueprint_result, "agent_seed", None)
        if not _blueprint_seed:
            return True, _text or None
        # Blueprint matched — rewrite the turn to the seed and fall through so the agent collects
        # each slot value conversationally, then calls the cronjob tool (the /steer pattern).
        if _text:
            await self._send_command_ack(source, _text, "blueprint")
        try:
            event.text = _blueprint_seed
        except Exception:
            return True, _text or None
        return False, None

    async def _hm_cmd_undo(self, event, source, _quick_key):
        _undo_n = 1
        _undo_raw = event.get_command_args().strip()
        if _undo_raw:
            with suppress(ValueError, IndexError):
                _undo_n = max(1, int(_undo_raw.split()[0]))
        _undo_detail = (
            "This removes the last user/assistant exchange from history."
            if _undo_n == 1
            else f"This removes the last {_undo_n} user turns from history."
        )
        return await self._hm_confirm_destructive(event, "undo", _undo_detail, self._handle_undo_command)

    # /queue and /steer on the idle path: no agent is running, so strip the prefix and send the
    # payload as a regular user turn; an empty payload surfaces the usage hint.
    async def _hm_cmd_queue(self, event, source, _quick_key):
        return self._hm_send_payload_as_turn(event, "Usage: /queue <prompt>")

    async def _hm_cmd_steer(self, event, source, _quick_key):
        return self._hm_send_payload_as_turn(
            event, "Usage: /steer <prompt>  (no agent is running; sending as a normal message)"
        )

    @staticmethod
    def _hm_send_payload_as_turn(event, usage: str) -> Tuple[bool, Optional[str]]:
        payload = event.get_command_args().strip()
        if not payload:
            return True, usage
        with suppress(Exception):
            event.text = payload
        return False, None

    async def _hm_cmd_moa(self, event, source, _quick_key):
        # /moa is one-shot sugar only: run a single prompt through the default MoA preset, then
        # restore the prior model. To *switch* to a MoA preset for the session, pick it from the
        # model picker (MoA presets surface as a virtual "Mixture of Agents" provider).
        from hermes_cli.moa_config import moa_usage, normalize_moa_config
        from hermes_cli.config import load_config

        moa_payload = event.get_command_args().strip()
        if not moa_payload:
            return True, moa_usage()
        try:
            cfg = load_config()
            moa_cfg = normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
        except Exception:
            moa_cfg = normalize_moa_config({})
        try:
            event.text = moa_payload
            _moa_state = self._session_state(_quick_key)
            # Same one-shot snapshot `/model --once` uses, so eviction/stop/finalizer settle both alike.
            self._claim_one_turn_restore(_quick_key)
            _moa_state.conversation.model_override = {
                "provider": "moa", "model": moa_cfg["default_preset"], "base_url": "moa://local",
                "api_key": "moa-virtual-provider", "api_mode": "chat_completions",
            }
            self._evict_cached_agent(_quick_key)
        except Exception:
            return True, "Failed to prepare MoA turn."
        return False, None

    # Idle-path built-ins with bespoke flow (confirmations, prompt rewrites, one-shot MoA), each
    # handled by ``_hm_cmd_<name>`` → ``(handled, result)``; ``(False, None)`` falls through to the agent.
    _HM_CANONICAL_COMMANDS = frozenset({
        "new", "start", "egress", "learn", "plan", "init", "blueprint", "undo", "queue", "steer", "moa",
    })

    async def _hm_dispatch_canonical_command(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str,
        canonical: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """Dispatch built-in idle-path commands → ``(handled, result)``; prompt-rewriting commands
        mutate ``event.text`` and return ``(False, None)`` to fall through to the agent."""
        plain_handler = (
            self._gateway_plain_command_handlers().get(canonical)
            or self._gateway_idle_command_handlers().get(canonical)
        )
        if plain_handler is not None:
            return True, await plain_handler(event)
        if canonical in self._HM_CANONICAL_COMMANDS:
            return await getattr(self, f"_hm_cmd_{canonical}")(event, source, _quick_key)
        return False, None

    async def _hm_run_exec_quick_command(self, command: str, exec_cmd: str) -> str:
        """Run a ``type: exec`` quick command in the gateway process (30 s cap, sanitized env — the
        gateway process has every API key in os.environ; output is redacted too)."""
        try:
            from tools.environments.local import build_subprocess_env
            proc = await asyncio.create_subprocess_shell(
                exec_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=build_subprocess_env(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = (stdout or stderr).decode().strip()
            if output:
                from agent.redact import redact_sensitive_text
                output = redact_sensitive_text(output)
            return output or "Command returned no output."
        except asyncio.TimeoutError:
            return "Quick command timed out (30s)."
        except Exception as e:
            return f"Quick command error: {e}"

    async def _hm_dispatch_quick_and_plugin_commands(
        self, event: "MessageEvent", source: SessionSource, command: Optional[str]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Drain gate, user-defined quick commands (exec/alias) and plugin slash commands →
        ``(handled, result, command)``; an alias quick command rewrites ``command``."""
        from gateway.run import logger

        from gateway.overlap_restart import admits_owned_work
        if self._draining and not admits_owned_work(self, event.source):
            return True, f"⏳ Gateway is {self._status_action_gerund()} and is not accepting new work right now.", command

        # User-defined quick commands (bypass agent loop, no LLM call)
        qcmd = self._hm_quick_commands().get(command) if command else None
        if qcmd is not None:
            # Quick commands are slash capabilities too — and type:exec ones run a shell command in
            # the gateway process. They are never in the registry, so the early gate never fires for
            # them; apply the same admin/user policy to the raw typed name here.
            # The early gate above only fires for registry-known commands, so quick commands (never in the
            # registry) would otherwise reach this dispatch sink unchecked. (#44727)
            _denied = self._check_slash_access(source, command)
            if _denied is not None:
                return True, _denied, command
            qtype = qcmd.get("type")
            if qtype == "exec":
                exec_cmd = qcmd.get("command", "")
                if not exec_cmd:
                    return True, f"Quick command '/{command}' has no command defined.", command
                return True, await self._hm_run_exec_quick_command(command, exec_cmd), command
            if qtype != "alias":
                return True, f"Quick command '/{command}' has unsupported type (supported: 'exec', 'alias').", command
            new_command = self._hm_expand_alias_quick_command(event, qcmd)
            if new_command is None:
                return True, f"Quick command '/{command}' has no target defined.", command
            command = new_command  # Fall through to normal command dispatch below

        # Plugin-registered slash commands. Underscores normalize to hyphens so Telegram's
        # underscored autocomplete form matches plugin commands registered with hyphens.
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                plugin_handler = get_plugin_command_handler(command.replace("_", "-"))
                if plugin_handler:
                    result = plugin_handler(event.get_command_args().strip())
                    if asyncio.iscoroutine(result):
                        result = await result
                    return True, str(result) if result else None, command
            except Exception as e:
                logger.warning("Plugin command dispatch failed: %s", e)
        return False, None, command

    def _hm_bundle_slash_rewrite(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str, command: str
    ) -> bool:
        """Rewrite ``/<bundle>`` to the bundle invocation message; True when handled.
        Skill bundles take precedence over individual skill commands (mirrors CLI dispatch)."""
        from gateway.run import logger

        try:
            from agent.skill_bundles import (
                build_bundle_invocation_message, resolve_bundle_command_key
            )
            bundle_key = resolve_bundle_command_key(command)
            if bundle_key is None:
                return False
            # Pass the platform explicitly: bundle skill loading bypasses get_skill_commands()'
            # scan-time disabled filter, and one gateway process serves several platforms, so
            # env-var platform resolution can't be trusted here.
            # Mirrors the stacked-skill gate (#58888).
            bundle_result = build_bundle_invocation_message(
                bundle_key, event.get_command_args().strip(), task_id=_quick_key,
                platform=source.platform.value if source.platform else None,
            )
            if not bundle_result:
                return False
            event.text, _loaded, missing = bundle_result
            if missing:
                logger.info("Bundle %s skipped missing skills: %s", bundle_key, ", ".join(missing))
            return True  # Fall through to normal message processing with bundle content
        except Exception as exc:
            logger.warning("Bundle dispatch failed: %s", exc)
            return False

    @staticmethod
    def _hm_unknown_slash_reply(command: str, source: SessionSource) -> Optional[str]:
        """Reply for a /command that is not built-in/plugin/skill; None when it is known."""
        from gateway.run import logger

        from gateway.run import _check_unavailable_skill
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        # Known commands never need an unavailable-skill hint (which can require a cold scan).
        if command.replace("_", "-") in GATEWAY_KNOWN_COMMANDS:
            return None
        # Known-but-disabled or uninstalled skill → actionable guidance.
        _unavail_msg = _check_unavailable_skill(command)
        if _unavail_msg:
            return _unavail_msg
        # Genuinely unrecognized: warn instead of forwarding to the LLM as free text (it invents
        # tool calls). Normalize to hyphenated form first: the quick-command block may have set an
        # alias target, so the resolved def can be stale.
        logger.warning(
            "Unrecognized slash command /%s from %s — replying with unknown-command notice",
            command, source.platform.value if source.platform else "?",
        )
        return (
            f"Unknown command `/{command}`. "
            f"Type /commands to see what's available, "
            f"or resend without the leading slash to send "
            f"as a regular message."
        )

    def _hm_skill_slash_rewrite(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str, command: Optional[str]
    ) -> Optional[str]:
        """Rewrite ``/<bundle>`` / ``/<skill>`` invocations into the skill prompt on ``event.text``;
        returns a reply string when the command is disabled/unknown/failed, else None.
        resolve_skill_command_key() handles the Telegram underscore/hyphen round-trip (/claude_code)."""
        from gateway.run import logger

        if not command or self._hm_bundle_slash_rewrite(event, source, _quick_key, command):
            return None
        try:
            from agent.skill_commands import (
                get_skill_commands, build_skill_invocation_message, resolve_skill_command_key
            )
            skill_cmds = get_skill_commands()
            cmd_key = resolve_skill_command_key(command)
            if cmd_key is None:
                return self._hm_unknown_slash_reply(command, source)
            _plat = source.platform.value if source.platform else None
            user_instruction = event.get_command_args().strip()
            # Stacked slash-skill invocations: `/skill-a /skill-b do XYZ` loads every leading skill
            # (up to 5), not just the first. Mirrors CLI.
            try:
                from agent.skill_commands import (
                    build_stacked_skill_invocation_message as _build_stacked,
                    split_stacked_skill_commands,
                )
                extra_keys, stacked_instruction = split_stacked_skill_commands(user_instruction)
            except Exception:
                _build_stacked = None
                extra_keys, stacked_instruction = [], user_instruction
            _skill_name = skill_cmds[cmd_key].get("name", "")
            if _plat and (_skill_name or extra_keys):
                # Per-platform disabled check: get_skill_commands() only applies the *global*
                # disabled list at scan time (process-global cache across platforms), and
                # split_stacked_skill_commands() only checks each extra token is a KNOWN skill.
                from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                _plat_disabled = _get_plat_disabled(platform=_plat)
                if _skill_name and _skill_name in _plat_disabled:
                    return (
                        f"The **{_skill_name}** skill is disabled for {_plat}.\n"
                        f"Enable it with: `hermes skills config`"
                    )
                _disabled_extra = [
                    skill_cmds.get(k, {}).get("name", "")
                    for k in extra_keys
                    if skill_cmds.get(k, {}).get("name", "") in _plat_disabled
                ]
                if _disabled_extra:
                    return (
                        f"The **{', '.join(_disabled_extra)}** skill(s) in this "
                        f"stacked invocation are disabled for {_plat}.\n"
                        f"Enable them with: `hermes skills config`"
                    )
            if extra_keys and _build_stacked is not None:
                stacked_result = _build_stacked(
                    [cmd_key, *extra_keys], stacked_instruction, task_id=_quick_key,
                )
                if not stacked_result:
                    return f"Failed to load stacked skills for /{command}."
                event.text, _loaded, _missing = stacked_result
            else:
                msg = build_skill_invocation_message(cmd_key, user_instruction, task_id=_quick_key)
                if msg:
                    event.text = msg
            # Fall through to normal message processing with skill content
        except Exception as e:
            logger.debug("Skill command check failed (non-fatal): %s", e)
        return None

    async def _hm_pending_reply_intercepts(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str
    ) -> Optional[str]:
        """Replies owned by in-flight work: pending /update prompt, clarify, slash-confirm.
        Only events that may control the gateway (``allow_gateway_control``) can answer them."""
        if not event.allow_gateway_control:
            return None
        _reply = self._hm_update_prompt_reply(event, _quick_key)
        if _reply is None:
            _reply = await self._hm_clarify_reply(event, source, _quick_key)
        if _reply is None:
            _reply = await self._hm_slash_confirm_reply(event, _quick_key)
        return _reply

    async def _hm_dispatch_idle_commands(
        self, event: "MessageEvent", source: SessionSource, _quick_key: str
    ) -> Tuple[bool, Optional[str]]:
        """Idle path: resolve + dispatch slash commands; rewriting commands fall through to the agent."""
        _handled, _result, command, canonical = await self._hm_resolve_command(event, source, _quick_key)
        if not _handled:
            _handled, _result = await self._hm_dispatch_canonical_command(event, source, _quick_key, canonical)
        if not _handled:
            _handled, _result, command = await self._hm_dispatch_quick_and_plugin_commands(event, source, command)
        if not _handled:
            # Skill-slash resolution is disk-bound (cold skill scan, skill file loads, the
            # unavailable-skill rglob over every skills dir) and uncached on a first hit; on a
            # large install it held the loop past the liveness watchdog (#111091). The executor
            # hop carries the profile contextvars the scan is scoped to.
            _result = await self._run_in_executor_with_context(
                self._hm_skill_slash_rewrite, event, source, _quick_key, command)
            _handled = _result is not None
        return _handled, _result

    def _hm_rescue_orphaned_fifo(
        self, event: "MessageEvent", source: SessionSource, is_internal: bool, _quick_key: str
    ) -> Tuple["MessageEvent", SessionSource, bool]:
        """FIFO orphan rescue: a session that went idle with a populated overflow (post-turn drain
        never promoted, e.g. a compression-demoted follow-up) silently orphaned those events. The
        oldest orphan runs as THIS turn and the incoming event is parked behind the chain. Skipped
        for control commands and internal events."""
        from gateway.run import logger

        try:
            # ── FIFO orphan rescue (#99882) ──────────────────────────────── If this session went idle with
            # a populated overflow (queued during a busy window whose post-turn drain never promoted — e.g.
            # a compression-demoted follow-up after the compression window ended through an exit that
            # skipped the promotion site), those events were silently orphaned. We are starting the next
            # turn for this session NOW: re-stage the orphans in FIFO order and enqueue the incoming event
            # behind them, so arrival order (#28503) holds: oldest orphan runs as this turn, the rest drain
            # in order, the new message last.
            _orphan_adapter = self._delivery_adapter_for(source)
            if _orphan_adapter is None or getattr(event, "internal", False) or event.get_command():
                return event, source, is_internal
            _rescued = self._rescue_orphaned_overflow(_quick_key, _orphan_adapter)
            if _rescued is None:
                return event, source, is_internal
            # Into the slot when the chain was a single orphan (post-turn drain picks it up),
            # otherwise into overflow behind the already-staged next orphan. A re-delivered
            # copy of the rescued (or any queued) message id is dropped, never parked twice.
            self._rescue_park_incoming_event(_quick_key, event, _orphan_adapter, _rescued)
            # Same session key by construction; carry the orphan's own source so reply anchors /
            # thread metadata point at the message actually being answered.
            _rescued_source = getattr(_rescued, "source", None)
            source = _rescued_source if _rescued_source is not None else source
            return _rescued, source, bool(getattr(_rescued, "internal", False))
        except Exception:
            logger.debug("FIFO orphan rescue pre-claim failed for %s", _quick_key, exc_info=True)
            return event, source, is_internal

    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """
        Handle an incoming message from any platform.

        This is the core message processing pipeline:
        1. Check user authorization
        2. Check for commands (/new, /reset, etc.)
        3. Check for running agent and interrupt if needed
        4. Get or create session
        5. Build context for agent
        6. Run agent conversation
        7. Return response
        """
        from gateway.run import (
            _check_unavailable_skill,
            _hermes_home,
            _is_slack_ignored_channel,
            queue_drain_refused_message,
        )
        from gateway.run import (
            _AGENT_PENDING_SENTINEL,
            _build_media_placeholder,
            _float_env,
            logger,
        )
        from gateway.platforms.base import merge_pending_message_event

        source = event.source

        # 🔴 Cross-session leak guard. This handler runs inside a per-message
        # asyncio task created via create_task(), which snapshots the spawning
        # context with copy_context(). If a *concurrent* message had already
        # bound its session via set_session_vars() when this task was created,
        # we inherited ITS HERMES_SESSION_* ContextVars. Until we bind our own
        # (a few steps down, in _set_session_env), any subprocess spawned here
        # would read the foreign session's identity via the subprocess-env
        # bridge — the _UNSET-strip guard there can't help because the vars are
        # set-to-foreign, not _UNSET. Reset to _UNSET now so that window strips
        # safe (no session) instead of leaking the sibling's. See
        # gateway/session_context.reset_session_vars + the inheritance test.
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug("reset_session_vars failed at handler entry", exc_info=True)

        # Most adapters resolve profile routes in build_source(), before they
        # hand us the event. A few internal/voice paths construct SessionSource
        # directly, so resolve those here as the shared fail-closed ingress gate
        # before authorization, hooks, or session side effects.
        if (
            getattr(getattr(self, "config", None), "multiplex_profiles", False)
            and not getattr(source, "profile", None)
            and getattr(source, "profile_route_rejected", False) is not True
        ):
            from gateway.profile_routing import ProfileRouteRejected

            try:
                source.profile = self._profile_name_for_source(source)
            except ProfileRouteRejected:
                source.profile_route_rejected = True

        # SessionSource owns a strict boolean marker. Require the literal value
        # so duck-typed test/internal sources with dynamic attributes are not
        # mistaken for an explicit matched-route rejection.
        if getattr(source, "profile_route_rejected", False) is True:
            logger.warning(
                "Dropping inbound message because its explicit profile route "
                "targets an unserved profile"
            )
            return None

        # Internal events (e.g. background-process completion notifications)
        # are system-generated and must skip user authorization.
        is_internal = bool(getattr(event, "internal", False))

        # Ignored-channel guard runs FIRST — before startup-restore queueing,
        # plugin hooks, auth, and session setup — so a configured ignored
        # channel can never reach pairing/auth/session state (#51899).
        # getattr: bare test runners construct GatewayRunner via
        # object.__new__ without config (see AGENTS.md pitfall on
        # object.__new__ test pattern).
        if (
            not is_internal
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(
                getattr(self, "config", None), getattr(source, "chat_id", None)
            )
        ):
            logger.info(
                "Dropping Slack message from configured ignored channel %s",
                getattr(source, "chat_id", None),
            )
            return None

        if (
            getattr(self, "_startup_restore_in_progress", False)
            and not is_internal
            and not getattr(event, "_hermes_startup_restore_replay", False)
        ):
            self._queue_startup_restore_event(event)
            return None

        # scale-to-zero (Phase 0, 0.B/F13): stamp the gateway-scoped last-inbound
        # clock for real (user-originated) inbound only. Internal/system events
        # (background-process completions, startup-restore replays) are NOT
        # traffic — counting them would keep a genuinely idle gateway awake. This
        # clock is what the idle predicate (gateway/scale_to_zero.is_idle) reads.
        if not is_internal:
            self._scale_to_zero_note_real_inbound()

        # Fire pre_gateway_dispatch plugin hook for user-originated messages.
        # Plugins receive the MessageEvent and may return a dict influencing flow:
        #   {"action": "skip",    "reason": ...}    -> drop (no reply, plugin handled)
        #   {"action": "rewrite", "text":  ...}     -> replace event.text, continue
        #   {"action": "allow"}   /   None          -> normal dispatch
        # Hook runs BEFORE auth so plugins can handle unauthorized senders
        # (e.g. customer handover ingest) without triggering the pairing flow.
        if not is_internal:
            try:
                from hermes_cli.lifecycle import invoke_hook as _invoke_hook
                _hook_results = _invoke_hook(
                    "pre_gateway_dispatch",
                    event=event,
                    gateway=self,
                    # getattr: bare-runner tests build GatewayRunner via
                    # object.__new__ without __init__ (pitfall #17), and the
                    # hook must not fail dispatch over a missing attribute.
                    session_store=getattr(self, "session_store", None),
                )
            except Exception as _hook_exc:
                logger.warning("pre_gateway_dispatch invocation failed: %s", _hook_exc)
                _hook_results = []

            for _result in _hook_results:
                if not isinstance(_result, dict):
                    continue
                _action = _result.get("action")
                if _action == "skip":
                    logger.info(
                        "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s",
                        _result.get("reason"),
                        source.platform.value if source.platform else "unknown",
                        source.chat_id or "unknown",
                    )
                    return None
                if _action == "rewrite":
                    _new_text = _result.get("text")
                    if isinstance(_new_text, str):
                        event = dataclasses.replace(event, text=_new_text)
                        source = event.source
                    break
                if _action == "allow":
                    break

        if is_internal:
            pass
        elif source.user_id is None:
            # Messages with no user identity (Telegram service messages,
            # channel forwards, anonymous admin posts, sender_chat) can't
            # be paired, but they can still be authorized via a
            # chat-scoped allowlist (e.g. TELEGRAM_GROUP_ALLOWED_CHATS
            # authorizes every member of the listed chat regardless of
            # sender). Defer to _is_user_authorized so that path runs.
            if not self._is_user_authorized_for_source(source):
                logger.debug("Ignoring message with no user_id from %s", source.platform.value)
                return None
        elif not self._is_user_authorized_for_source(source):
            logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            # Mission-only WhatsApp DMs: _is_user_authorized just denied this
            # chat because the platform opted into mission_only_dms and no
            # active mission is bound to it. That opt-in replaces the pairing
            # handshake too — answering the denial with a pairing code would
            # message a sender the operator explicitly fenced off and leave
            # pairing-store rate-limit state behind. Drop silently BEFORE any
            # pairing/session/model side effect. Same shared gate as
            # _is_user_authorized, so the denial and its handling cannot
            # diverge; with the flag absent or false the legacy pairing-code
            # behavior below is unchanged.
            if self._whatsapp_mission_only_dm_denied(source):
                logger.debug(
                    "Mission-only WhatsApp gate: silently dropping unauthorized message from %s",
                    source.user_id,
                )
                return None
            # In DMs: offer pairing code. In groups: silently ignore.
            if (
                source.chat_type == "dm"
                and self._get_unauthorized_dm_behavior(
                    source.platform,
                    profile=source.profile,
                )
                == "pair"
            ):
                platform_name = source.platform.value if source.platform else "unknown"
                pairing_store = self._pairing_store_for(source)
                if pairing_store is None:
                    logger.error(
                        "Cannot offer pairing code on %s: no pairing store",
                        platform_name,
                    )
                    return None
                # Rate-limit ALL pairing responses (code or rejection) to
                # prevent spamming the user with repeated messages when
                # multiple DMs arrive in quick succession.
                if pairing_store._is_rate_limited(platform_name, source.user_id):
                    return None
                code = pairing_store.generate_code(
                    platform_name, source.user_id, source.user_name or ""
                )
                if code:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        store_profile = getattr(pairing_store, "profile", None)
                        profile_arg = (
                            f"-p {store_profile} "
                            if isinstance(store_profile, str)
                            and store_profile
                            and store_profile != "default"
                            else ""
                        )
                        await adapter.send(
                            source.chat_id,
                            f"Hi~ I don't recognize you yet!\n\n"
                            f"Here's your pairing code: `{code}`\n\n"
                            f"Ask the bot owner to run:\n"
                            f"`hermes {profile_arg}pairing approve "
                            f"{platform_name} {code}`"
                        )
                else:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        await adapter.send(
                            source.chat_id,
                            "Too many pairing requests right now~ "
                            "Please try again later!"
                        )
                    # Record rate limit so subsequent messages are silently ignored
                    pairing_store._record_rate_limit(platform_name, source.user_id)
            return None

        # Bot loop guard: the busy path charged this event on arrival; a drained
        # follow-up must not pay twice. Internal synthetic events are never charged.
        if (
            not is_internal
            and not getattr(event, "_bot_loop_admitted", False)
            and not self._admit_bot_message_for_source(source)
        ):
            return None

        # Global emergency stop (`hermes pause`): give new turns a brief
        # paused notice instead of starting an agent run. Internal events
        # (background-process completions from IN-FLIGHT work) bypass the
        # gate — pause stops NEW work, it never kills or orphans running
        # work. Placed after auth so unauthorized senders keep the normal
        # silent/pairing behavior and can't probe pause state.
        #
        # Passthroughs (pause blocks new AGENT turns, not control traffic):
        #   * recognized slash commands — /status, /help, /new, /approve and
        #     friends must keep working while paused, and /pause off is the
        #     in-band resume path for messaging-only users;
        #   * replies owned by IN-FLIGHT work — a pending detached-update
        #     prompt, clarify, slash-confirm, or dangerous-command approval,
        #     plus any message steering a session whose agent is already
        #     running. Swallowing those would stall work the pause promised
        #     not to touch.
        if not is_internal:
            try:
                from agent.estop import paused_reply as _estop_paused_reply
                _paused_notice = _estop_paused_reply()
            except ImportError:
                _paused_notice = None
            if _paused_notice is not None:
                _estop_allow = False
                _estop_cmd = None
                try:
                    _estop_cmd = event.get_command()
                except Exception:
                    _estop_cmd = None
                if _estop_cmd:
                    try:
                        from hermes_cli.commands import (
                            resolve_command as _resolve_estop_cmd,
                        )
                        _estop_allow = _resolve_estop_cmd(_estop_cmd) is not None
                    except Exception:
                        _estop_allow = False
                if not _estop_allow:
                    try:
                        _estop_key = self._session_key_for_source(source)
                        _estop_state = self._peek_session_state(_estop_key)
                        if (
                            _estop_state is not None
                            and _estop_state.persistent.update_prompt_pending
                        ):
                            _estop_allow = True
                        if not _estop_allow and self._is_session_running(_estop_key):
                            # Steering / interrupting in-flight work (which
                            # also covers pending clarify + tool approvals
                            # held by the running agent).
                            _estop_allow = True
                        if not _estop_allow:
                            from tools import slash_confirm as _estop_confirm_mod
                            if _estop_confirm_mod.get_pending(_estop_key):
                                _estop_allow = True
                        if not _estop_allow:
                            from tools.approval import (
                                has_blocking_approval as _estop_has_approval,
                            )
                            if _estop_has_approval(_estop_key):
                                _estop_allow = True
                    except Exception:
                        pass
                if not _estop_allow:
                    logger.info(
                        "Gateway turn paused by global emergency stop (platform=%s chat=%s)",
                        getattr(getattr(source, "platform", None), "value", "unknown"),
                        getattr(source, "chat_id", None) or "unknown",
                    )
                    return _paused_notice

        # Intercept messages that are responses to a pending /update prompt.
        # The update process (detached) wrote .update_prompt.json; the watcher
        # forwarded it to the user; now the user's reply goes back via
        # .update_response so the update process can continue.
        #
        # IMPORTANT: recognized slash commands must bypass this interception.
        # Otherwise control/session commands like /new or /help get silently
        # consumed as update answers instead of being dispatched normally.
        _quick_key = self._session_key_for_source(source)
        allow_gateway_control = event.allow_gateway_control
        _up_state = self._peek_session_state(_quick_key)
        if (
            allow_gateway_control
            and _up_state is not None
            and _up_state.persistent.update_prompt_pending
        ):
            raw = (event.text or "").strip()
            # Accept /approve and /deny as shorthand for yes/no
            cmd = event.get_command()
            if cmd in {"approve", "yes"}:
                response_text = "y"
            elif cmd in {"deny", "no"}:
                response_text = "n"
            else:
                _recognized_cmd = None
                if cmd:
                    try:
                        from hermes_cli.commands import resolve_command as _resolve_update_cmd
                    except Exception:
                        _resolve_update_cmd = None
                    if _resolve_update_cmd is not None:
                        try:
                            _cmd_def = _resolve_update_cmd(cmd)
                            _recognized_cmd = _cmd_def.name if _cmd_def else None
                        except Exception:
                            _recognized_cmd = None
                if _recognized_cmd:
                    response_text = ""
                else:
                    response_text = raw
            if response_text:
                response_path = _hermes_home / ".update_response"
                prompt_path = _hermes_home / ".update_prompt.json"
                try:
                    tmp = response_path.with_suffix(".tmp")
                    tmp.write_text(response_text, encoding="utf-8")
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to write update response: %s", e)
                    return f"✗ Failed to send response to update process: {e}"
                _up_state.persistent.update_prompt_pending = False
                label = response_text if len(response_text) <= 20 else response_text[:20] + "…"
                return f"✓ Sent `{label}` to the update process."
            # Recognized slash command during a pending update prompt:
            # unblock the detached update subprocess by writing a blank
            # response so ``_gateway_prompt`` returns the prompt's default
            # (typically a safe "n" / skip) and exits cleanly instead of
            # blocking on stdin until the 30-minute watcher timeout.
            # The slash command then falls through to normal dispatch.
            if _recognized_cmd:
                response_path = _hermes_home / ".update_response"
                prompt_path = _hermes_home / ".update_prompt.json"
                try:
                    tmp = response_path.with_suffix(".tmp")
                    tmp.write_text("", encoding="utf-8")
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                    logger.info(
                        "Recognized /%s during pending update prompt for %s; "
                        "cancelled prompt with default and dispatching command",
                        _recognized_cmd,
                        _quick_key,
                    )
                except OSError as e:
                    logger.warning(
                        "Failed to write cancel response for pending update prompt: %s",
                        e,
                    )
                _up_state.persistent.update_prompt_pending = False

        # Intercept messages that are responses to a pending clarify.
        # Open-ended prompts and "Other" responses are captured as free text;
        # direct replies to multi-choice prompts are accepted too ("2" maps
        # to the second option). Slash
        # commands still bypass this path so /stop and friends keep working.
        _clarify_mod = None
        try:
            from tools import clarify_gateway as _clarify_mod
            _pending_clarify = _clarify_mod.get_pending_for_session(
                _quick_key, include_choice_prompts=True,
            )
        except Exception:
            _pending_clarify = None
        if (
            allow_gateway_control
            and _pending_clarify is not None
            and _clarify_mod is not None
        ):
            _clarify_has_audio = bool(self._pending_event_audio_paths(event))
            _raw_clarify_reply = await self._prepare_clarify_reply_text(event)
            if _clarify_has_audio and not _raw_clarify_reply:
                logger.info(
                    "Gateway retained pending clarify after voice transcription "
                    "produced no usable text (session=%s, id=%s)",
                    _quick_key,
                    _pending_clarify.clarify_id,
                )
                return ""
            # Skip slash commands — the user clearly wanted to issue a
            # command, not answer the clarify.  Leave the clarify pending
            # so the user can retry; if it times out, the agent unblocks
            # with an empty response.
            if _raw_clarify_reply and not _raw_clarify_reply.startswith("/"):
                _text_outcome = _clarify_mod.attempt_text_response_for_session(
                    _quick_key, _raw_clarify_reply,
                )
                if _text_outcome == _clarify_mod.TEXT_RESOLVED:
                    logger.info(
                        "Gateway intercepted clarify text response (session=%s, id=%s)",
                        _quick_key, _pending_clarify.clarify_id,
                    )
                    # The clarify callback pauses the platform typing/status
                    # indicator while waiting so Slack users can type their
                    # answer. The active agent resumes as soon as this reply
                    # resolves the wait, so re-enable its indicator here too.
                    # Without this, Slack stays silent until the independent
                    # long-running heartbeat fires (three minutes by default).
                    _clarify_adapter = self._adapter_for_source(source)
                    if _clarify_adapter:
                        try:
                            _clarify_adapter.resume_typing_for_chat(source.chat_id)
                        except Exception:
                            logger.debug(
                                "Failed to resume typing after clarify response",
                                exc_info=True,
                            )
                        # A typed answer to a native card (numeric pick, or
                        # text after "Other") never reaches the click handler,
                        # so the card would keep its buttons forever.
                        if callable(getattr(type(_clarify_adapter), "retire_clarify_card", None)):
                            try:
                                await _clarify_adapter.retire_clarify_card(
                                    _pending_clarify.clarify_id,
                                    f"\u2705 answered: {_pending_clarify.response or _raw_clarify_reply}")
                            except Exception:
                                logger.debug(
                                    "Failed to retire clarify card after typed answer",
                                    exc_info=True,
                                )
                    # Acknowledge with empty string so adapters that emit
                    # the agent's response don't double-post.  The agent
                    # itself will produce the next user-facing message.
                    return ""
                if _text_outcome == _clarify_mod.TEXT_REJECTED_SELECTION:
                    # Selection-shaped but invalid (out-of-range number,
                    # unrecognised comma-list). Keep the clarify armed so
                    # the user can retry — do not cancel and do not treat
                    # this as an unrelated follow-up turn.
                    logger.info(
                        "Gateway retained pending clarify after invalid "
                        "selection attempt (session=%s, id=%s)",
                        _quick_key, _pending_clarify.clarify_id,
                    )
                    return ""
                if _text_outcome == _clarify_mod.TEXT_REJECTED_PROSE:
                    # Native-choice prompts deliberately reject unmatched
                    # prose so it can continue through normal busy-message
                    # routing. Release this clarify first: redirect()
                    # degrades to steer() while tools are executing, and
                    # that steer cannot drain until the clarify tool returns.
                    if _clarify_mod.resolve_gateway_clarify(
                        _pending_clarify.clarify_id,
                        "",
                    ):
                        # Adapters with a persistent native card (Slack Block
                        # Kit) retire it now, before the prose is routed, so
                        # its buttons stop advertising a dead answer path.
                        _clarify_adapter = self._adapter_for_source(source)
                        # Class lookup: a MagicMock adapter must not fabricate
                        # the method.
                        if callable(getattr(type(_clarify_adapter), "retire_clarify_card", None)):
                            try:
                                await _clarify_adapter.retire_clarify_card(
                                    _pending_clarify.clarify_id,
                                    "\u21a9\ufe0f Clarification cancelled \u2014 your message will be handled as a follow-up.")
                            except Exception:
                                logger.debug(
                                    "Failed to retire clarify card after prose cancellation",
                                    exc_info=True,
                                )

        # Intercept messages that are responses to a pending /reload-mcp
        # (or future) slash-confirm prompt.  Recognized confirm replies are
        # /approve, /always, /cancel (plus short aliases).  Anything else
        # falls through to normal dispatch — a stale pending confirm does
        # NOT block other commands.
        #
        # Important: if a dangerous-command approval is ALSO pending (agent
        # blocked inside tools/approval.py), the tool approval takes
        # precedence — /approve there unblocks the waiting tool thread.
        # Slash-confirm only catches /approve when no tool approval is live.
        from tools import slash_confirm as _slash_confirm_mod
        _pending_confirm = _slash_confirm_mod.get_pending(_quick_key)
        _tool_approval_live = False
        try:
            from tools.approval import has_blocking_approval
            _tool_approval_live = has_blocking_approval(_quick_key)
        except Exception:
            _tool_approval_live = False
        if allow_gateway_control and _pending_confirm and not _tool_approval_live:
            _raw_reply = (event.text or "").strip()
            # Accept bang-prefixed replies (`!always`, `!cancel`) verbatim.
            # Slack/Matrix instruction text shows the `!` prefix (typed `/`
            # is blocked in Slack threads), but the adapters only rewrite
            # `!<known-command>` — `always`/`cancel` are confirm keywords,
            # not registered commands, so the `!` survives to here.
            _norm_reply = _raw_reply.lstrip("!/").lower()
            _cmd_reply = event.get_command()
            _confirm_choice = None
            if _cmd_reply in {"approve", "yes", "ok", "confirm"}:
                _confirm_choice = "once"
            elif _cmd_reply in {"always", "remember"}:
                _confirm_choice = "always"
            elif _cmd_reply in {"cancel", "no", "deny", "nevermind"}:
                _confirm_choice = "cancel"
            elif _norm_reply in {"approve", "approve once", "once"}:
                _confirm_choice = "once"
            elif _norm_reply in {"always", "always approve"}:
                _confirm_choice = "always"
            elif _norm_reply in {"cancel", "nevermind", "no"}:
                _confirm_choice = "cancel"
            if _confirm_choice is not None:
                _resolved = await _slash_confirm_mod.resolve(
                    _quick_key, _pending_confirm.get("confirm_id"), _confirm_choice,
                )
                return _resolved or ""
            # Stale pending + unrelated command: drop the pending state so
            # the confirm doesn't block normal usage indefinitely.  The user
            # clearly moved on.
            _slash_confirm_mod.clear_if_stale(_quick_key)

        # PRIORITY handling when an agent is already running for this session.
        # Default behavior is to interrupt immediately so user text/stop messages
        # are handled with minimal latency.
        #
        # Special case: Telegram/photo bursts often arrive as multiple near-
        # simultaneous updates. Do NOT interrupt for photo-only follow-ups here;
        # let the adapter-level batching/queueing logic absorb them.

        # Staleness eviction: detect leaked locks from hung/crashed handlers.
        # With inactivity-based timeout, active tasks can run for hours, so
        # wall-clock age alone isn't sufficient.  Evict only when the agent
        # has been *idle* beyond the inactivity threshold (or when the agent
        # object has no activity tracker and wall-clock age is extreme).
        _raw_stale_timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800)
        _quick_state = self._peek_session_state(_quick_key)
        _stale_ts = _quick_state.turn.started_ts if _quick_state else 0
        if _quick_state is not None and _quick_state.turn.agent is not None and _stale_ts:
            _stale_age = time.time() - _stale_ts
            _stale_agent = _quick_state.turn.agent
            # Never evict the pending sentinel — it was just placed moments
            # ago during the async setup phase before the real agent is
            # created.  Sentinels have no get_activity_summary(), so the
            # idle check below would always evaluate to inf >= timeout and
            # immediately evict them, racing with the setup path.
            _stale_idle = float("inf")  # assume idle if we can't check
            _stale_detail = ""
            _activity_summary_valid = False
            if _stale_agent and hasattr(_stale_agent, "get_activity_summary"):
                try:
                    _sa = _stale_agent.get_activity_summary()
                    from gateway.session_stall import (
                        resolve_session_idle_seconds_from_activity,
                    )

                    _resolved_idle = resolve_session_idle_seconds_from_activity(
                        _sa if isinstance(_sa, dict) else None,
                        now=time.time(),
                    )
                    if _resolved_idle is not None:
                        _stale_idle = _resolved_idle
                        _activity_summary_valid = True
                    _stale_detail = (
                        f" | last_activity={_sa.get('last_activity_desc', 'unknown') if isinstance(_sa, dict) else 'unknown'} "
                        f"({_stale_idle:.0f}s ago) "
                        f"| iteration={_sa.get('api_call_count', 0) if isinstance(_sa, dict) else 0}/{_sa.get('max_iterations', 0) if isinstance(_sa, dict) else 0}"
                    )
                except Exception:
                    pass
            # A valid activity clock is authoritative: total age alone never
            # makes an actively progressing turn stale. The emergency wall TTL
            # is only a fallback when the agent cannot report usable activity.
            _wall_ttl = max(_raw_stale_timeout * 10, 7200) if _raw_stale_timeout > 0 else float("inf")
            _should_evict = (
                _stale_agent is not _AGENT_PENDING_SENTINEL
                and (
                    (
                        _activity_summary_valid
                        and _raw_stale_timeout > 0
                        and _stale_idle >= _raw_stale_timeout
                    )
                    or (
                        not _activity_summary_valid
                        and _stale_age > _wall_ttl
                    )
                )
            )
            if _should_evict:
                logger.warning(
                    "Evicting stale _running_agents entry for %s "
                    "(age: %.0fs, idle: %.0fs, timeout: %.0fs)%s",
                    _quick_key, _stale_age, _stale_idle,
                    _raw_stale_timeout, _stale_detail,
                )
                self._invalidate_session_run_generation(
                    _quick_key,
                    reason="stale_running_agent_eviction",
                )
                self._release_running_agent_state(_quick_key)

        # #99106: durable-reaped guard.  A session whose routing row was
        # ended in state.db (e.g. ``ws_orphan_reap`` / ``agent_close``) while
        # the gateway stayed alive keeps its in-memory turn slot alive
        # (``_is_session_running`` stays True).  The priority fast-path would
        # then queue every next user message into the dead runtime instead of
        # healing the routing via ``get_or_create_session`` → ``reopen``.
        # This is the live-gateway variant of #54878 and the #632 detached/
        # 405 suppressions in production.  Evict the stale slot so the next
        # message falls through to the cold path and re-attaches or creates a
        # fresh session; /status then correctly shows 代理运行中: 否 before the
        # heal and a live turn after.
        if self._is_session_running(_quick_key):
            try:
                _reap_store = getattr(self, "session_store", None)
                # Use the public, lock-held accessors: peek_session_id resolves
                # key -> session_id under the store lock, and returns a
                # non-str on stubbed stores in bare test runners — both the
                # isinstance() gate and the ``is True`` gate below keep this
                # guard inert unless a real SessionStore answers.
                _reap_peek = getattr(_reap_store, "peek_session_id", None)
                _is_ended = getattr(_reap_store, "_is_session_ended_in_db", None)
                _reap_sid = _reap_peek(_quick_key) if callable(_reap_peek) else None
                if (
                    isinstance(_reap_sid, str)
                    and _reap_sid
                    and callable(_is_ended)
                    and _is_ended(_reap_sid) is True
                ):
                    logger.warning(
                        "Evicting stale _running_agents entry for %s — "
                        "durable session %s is ended (reaped) in state.db; "
                        "healing routing on next message (#99106)",
                        _quick_key,
                        _reap_sid,
                    )
                    self._invalidate_session_run_generation(
                        _quick_key,
                        reason="reaped_session_eviction",
                    )
                    self._release_running_agent_state(_quick_key)
            except Exception:
                logger.debug("reaped-session staleness check failed", exc_info=True)

        if self._is_session_running(_quick_key):
            # Resolve the command once; every command's mid-run behavior is
            # declared on its CommandDef (busy_policy / busy_handler in
            # hermes_cli/commands.py) and dispatched through the single
            # resolver _dispatch_busy_slash_command below — no per-command
            # if-chain here.
            from hermes_cli.commands import resolve_command as _resolve_cmd_inner
            _evt_cmd = event.get_command()
            _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None

            # /status and /context are intentionally pre-gate so users
            # always see session state.
            if _cmd_def_inner and _cmd_def_inner.name == "status":
                return await self._handle_status_command(event)
            if _cmd_def_inner and _cmd_def_inner.name == "context":
                return await self._handle_context_command(event)

            # Slash command access control on the running-agent fast-path.
            # Mirrors the cold-path gate further below so non-admin users
            # can't bypass gating just because an agent happens to be busy.
            # /status above is intentionally pre-gate so users always see
            # session state. /help and /whoami fall under the always-allowed
            # floor inside _check_slash_access.
            if _evt_cmd and _cmd_def_inner is not None:
                _denied = self._check_slash_access(source, _cmd_def_inner.name)
                if _denied is not None:
                    return _denied

            # AUTHORITATIVE drain gate, before the busy resolver: a slash
            # command with busy_policy="dispatch" (e.g. /bg, /btw) would
            # otherwise START executor work while a confirmed restart is
            # already waiting for in-flight work to finish (#77184).
            if _evt_cmd and _cmd_def_inner is not None:
                _drain_notice = self._slash_drain_gate_notice(
                    _cmd_def_inner.name, source
                )
                if _drain_notice is not None:
                    return _drain_notice

            # Any recognized slash command: dispatch according to its
            # declared busy_policy (dispatch / interrupt_then_dispatch /
            # reject). Unrecognized commands and plain text fall through
            # to the interrupt/queue logic below.
            if _cmd_def_inner:
                return await self._dispatch_busy_slash_command(
                    event, _cmd_def_inner, _quick_key, source,
                )

            from gateway.overlap_restart import admits_owned_work as _admits_overlap_work
            _overlap_owned = _admits_overlap_work(self, source)
            if event.message_type == MessageType.PHOTO:
                logger.debug("PRIORITY photo follow-up for session %s — queueing without interrupt", _quick_key)
                if self._draining and not _overlap_owned:
                    # Drain window: an in-memory-only merge would die with the
                    # process bounce. Route through the durable drain queue
                    # when the busy policy allows it (the photo merge happens
                    # inside it); cap/IO failure gets the honest drain refusal,
                    # same answer text follow-ups get — never a silent loss.
                    return self._busy_drain_admission(
                        event,
                        _quick_key,
                        self._effective_busy_input_mode(source),
                    )
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(adapter._pending_messages, _quick_key, event)
                return None

            effective_busy_input_mode = self._effective_busy_input_mode(source)
            _telegram_followup_grace = float(
                os.getenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")
            )
            _grace_state = self._peek_session_state(_quick_key)
            _started_at = _grace_state.turn.started_ts if _grace_state else 0
            if (
                source.platform == Platform.TELEGRAM
                and event.message_type == MessageType.TEXT
                and _telegram_followup_grace > 0
                and _started_at
                and (time.time() - _started_at) <= _telegram_followup_grace
            ):
                if self._draining:
                    # The grace window's in-memory queue dies with the process
                    # bounce — this sub-path takes the same durable admission
                    # as every other busy follow-up, never a silent loss.
                    return self._busy_drain_admission(
                        event, _quick_key, effective_busy_input_mode
                    )
                logger.debug(
                    "Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt",
                    time.time() - _started_at,
                    _quick_key,
                )
                adapter = self._adapter_for_source(source)
                if adapter:
                    if effective_busy_input_mode == "queue":
                        self._enqueue_fifo(_quick_key, event, adapter)
                    else:
                        merge_pending_message_event(
                            adapter._pending_messages,
                            _quick_key,
                            event,
                            merge_text=True,
                        )
                return None

            _ra_state = self._peek_session_state(_quick_key)
            running_agent = _ra_state.turn.agent if _ra_state else None
            if running_agent is _AGENT_PENDING_SENTINEL:
                # Agent is being set up but not ready yet.
                if event.get_command() == "stop":
                    # Force-clean the sentinel so the session is unlocked.
                    self._release_running_agent_state(_quick_key)
                    logger.info("HARD STOP (pending) for session %s — sentinel cleared", _quick_key)
                    return EphemeralReply("⚡ Force-stopped. The agent was still starting — session unlocked.")
                if self._draining and not _overlap_owned:
                    # The sentinel window's in-memory merge dies with the
                    # process bounce — same durable admission as every other
                    # busy follow-up. (/stop above stays reachable: it is the
                    # in-flight-work lifecycle the drain itself depends on.)
                    return self._busy_drain_admission(
                        event, _quick_key, effective_busy_input_mode
                    )
                # Queue the message so it will be picked up after the
                # agent starts.
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(
                        adapter._pending_messages,
                        _quick_key,
                        event,
                        merge_text=True,
                    )
                return None
            if self._draining and not _overlap_owned:
                return self._busy_drain_admission(
                    event, _quick_key, effective_busy_input_mode
                )
            if effective_busy_input_mode == "queue":
                logger.debug("PRIORITY queue follow-up for session %s", _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # Steer/redirect resolution is shared with the adapter busy
            # handler (_handle_active_session_busy_message): one resolver
            # applies the interrupt->queue demotions (#30170 subagents,
            # #56391 in-flight compression), attempts the mid-run steer
            # (steer mode, voice transcripts folded in per #58780) or the
            # text-only redirect (interrupt mode), and falls back to queue
            # semantics when neither lands. /stop reaches its dedicated
            # handler above, so the operator keeps a clean escape hatch.
            outcome = await self._resolve_busy_steer_or_redirect(
                event, _quick_key, effective_busy_input_mode, running_agent
            )
            if outcome.steered:
                logger.debug("PRIORITY steer for session %s", _quick_key)
                return None
            if outcome.redirected:
                logger.debug("PRIORITY redirect for session %s", _quick_key)
                return None
            if outcome.effective_mode == "queue":
                logger.debug("PRIORITY queue follow-up for session %s", _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            logger.debug("PRIORITY interrupt for session %s", _quick_key)
            _interrupt_text = event.text
            _media_urls = getattr(event, "media_urls", None) or []
            if self._pending_event_audio_paths(event):
                _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                    event,
                    self._adapter_for_source(source),
                    source,
                    event.text or "",
                    log_context="Voice-priority-interrupt",
                )
            elif not _interrupt_text and _media_urls:
                _interrupt_text = _build_media_placeholder(event)
            running_agent.interrupt(_interrupt_text)
            # NOTE: self._pending_messages was write-only (never consumed).
            # The actual interrupt message is delivered via adapter._pending_messages
            # which is read by _run_agent. Removed to prevent unbounded growth.
            return None

        # Check for commands
        command = event.get_command()

        from hermes_cli.commands import (
            GATEWAY_KNOWN_COMMANDS,
            is_gateway_known_command,
            resolve_command as _resolve_cmd,
        )

        # Resolve aliases to canonical name so dispatch and hook names
        # don't depend on the exact alias the user typed.
        _cmd_def = _resolve_cmd(command) if command else None
        canonical = _cmd_def.name if _cmd_def else command

        # Expand alias quick commands before built-in dispatch so targets like
        # /model openai/gpt-5.5 --provider openrouter reach the /model handler.
        # Preserve built-in precedence; aliases only need early handling when
        # the typed command is not already known.
        if command and _cmd_def is None:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if isinstance(quick_commands, dict) and command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get("type") == "alias":
                    target = (qcmd.get("target") or "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        _cmd_def = _resolve_cmd(command) if command else None
                        canonical = _cmd_def.name if _cmd_def else command

        # Per-platform slash command access control. Only kicks in when the
        # operator has set ``allow_admin_from`` for the source's scope (DM
        # vs group). When unset → backward-compat: every allowed user can
        # run every command. When set → non-admins can run only commands in
        # ``user_allowed_commands`` (plus the always-allowed floor: /help,
        # /whoami). Plain chat is unaffected — only slash commands gate.
        if command and canonical and is_gateway_known_command(canonical):
            _denied = self._check_slash_access(source, canonical)
            if _denied is not None:
                return _denied

        # pre_command observer hook (#64204): fires for every recognized
        # slash command BEFORE core handling, mirroring the CLI fire-site in
        # cli.py process_command. Observer-only in v1 (returns ignored).
        #
        # Placement matters: this cold-path dispatch is only reached when NO
        # agent is running for the session. The running-agent intercept path
        # above (/stop, /approve, busy_policy dispatch via
        # _dispatch_busy_slash_command) deliberately does NOT fire this hook —
        # those are control-plane operations on an in-flight run, and giving
        # plugins an observation (and eventually veto) point there would let
        # a slow or hostile plugin interfere with the operator's escape
        # hatches for a live agent.
        if command and is_gateway_known_command(canonical):
            try:
                from hermes_cli.plugins import fire_pre_command_hook
                fire_pre_command_hook(
                    surface="gateway",
                    command=str(canonical),
                    alias_used=str(command),
                    args_raw=event.get_command_args().strip(),
                    session_key=_quick_key,
                    platform=source.platform.value if source.platform else "",
                )
            except Exception as _pre_cmd_err:
                logger.debug(
                    "pre_command hook dispatch failed (non-fatal): %s",
                    _pre_cmd_err,
                )

        # Fire the ``command:<canonical>`` hook for any recognized slash
        # command — built-in OR plugin-registered. Handlers can return a
        # dict with ``{"decision": "deny" | "handled" | "rewrite", ...}``
        # to intercept dispatch before core handling runs. This replaces
        # the previous fire-and-forget emit(): return values are now
        # honored, but handlers that return nothing behave exactly as
        # before (telemetry-style hooks keep working).
        if command and is_gateway_known_command(canonical):
            raw_args = event.get_command_args().strip()
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "command": canonical,
                "raw_command": command,
                "args": raw_args,
                "raw_args": raw_args,
            }
            try:
                hook_results = await self.hooks.emit_collect(
                    f"command:{canonical}", hook_ctx
                )
            except Exception as _hook_err:
                logger.debug(
                    "command:%s hook dispatch failed (non-fatal): %s",
                    canonical, _hook_err,
                )
                hook_results = []

            for hook_result in hook_results:
                if not isinstance(hook_result, dict):
                    continue
                decision = str(hook_result.get("decision", "")).strip().lower()
                if not decision or decision == "allow":
                    continue
                if decision == "deny":
                    message = hook_result.get("message")
                    if isinstance(message, str) and message:
                        return message
                    return f"Command `/{command}` was blocked by a hook."
                if decision == "handled":
                    message = hook_result.get("message")
                    return message if isinstance(message, str) and message else None
                if decision == "rewrite":
                    new_command = str(
                        hook_result.get("command_name", "")
                    ).strip().lstrip("/")
                    if not new_command:
                        continue
                    new_args = str(hook_result.get("raw_args", "")).strip()
                    event.text = f"/{new_command} {new_args}".strip()
                    command = event.get_command()
                    _cmd_def = _resolve_cmd(command) if command else None
                    canonical = _cmd_def.name if _cmd_def else command
                    break

        # AUTHORITATIVE drain gate — the single admission point for the
        # whole cold path. It must sit BEFORE the first dispatch that can
        # start work (the plain map's /bg //btw spawn, the /compress //
        # /refine /review executor branches below): with the gate further
        # down, a message arriving after a confirmed restart started its
        # work before the old _draining check ever ran (#77184). Hook
        # rewrites above have already re-resolved ``canonical``, so a
        # rewritten command is gated under its FINAL name.
        _cold_drain_notice = self._slash_drain_gate_notice(canonical, source)
        if _cold_drain_notice is not None:
            # Plain text / media the gate refused is no longer dropped: the
            # idle session has no in-memory queue to join, so queue it
            # durably for the post-restart process to replay. Slash commands
            # keep the notice — the gate verdict itself is untouched.
            if canonical is None:
                _drain_queued_ack = queue_drain_refused_message(
                    self, event, _quick_key
                )
                if _drain_queued_ack is not None:
                    return _drain_queued_ack
            return _cold_drain_notice

        plain_handler = self._gateway_plain_command_handlers().get(canonical)
        if plain_handler is not None:
            return await plain_handler(event)

        if canonical == "new":
            if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
                return self._telegram_topic_root_new_message()
            async def _do_reset():
                return await self._handle_reset_command(event)
            return await self._maybe_confirm_destructive_slash(
                event=event,
                command="new",
                title="/new",
                detail=(
                    "This starts a fresh session and discards the current "
                    "conversation history."
                ),
                execute=_do_reset,
            )

        if canonical == "topic":
            return await self._handle_topic_command(event)

        if canonical == "start":
            logger.info("Ignoring /start platform ping for session %s", _quick_key)
            return ""

        if canonical == "whoami":
            return await self._handle_whoami_command(event)

        if canonical == "egress":
            from hermes_cli.proxy_cli import format_status_text

            return format_status_text()

        if canonical == "platform":
            return await self._handle_platform_command(event)

        if canonical == "stop":
            return await self._handle_stop_command(event)

        if canonical == "reasoning":
            return await self._handle_reasoning_command(event)

        if canonical == "memory":
            return await self._handle_memory_command(event)

        if canonical == "skills":
            return await self._handle_skills_command(event)

        if canonical == "learn":
            # Open-ended: rewrite the turn to a standards-guided prompt and fall
            # through to normal agent processing. The live agent gathers the
            # sources the user described (dirs via read_file, URLs via
            # web_extract, this conversation, pasted text) and authors the skill
            # via skill_manage. Mirrors the /blueprint fall-through so role
            # alternation is preserved. No engine, works on any backend.
            from agent.learn_prompt import build_learn_prompt

            _learn_req = event.get_command_args().strip()
            _ack = (
                "Learning a skill from what you described…"
                if _learn_req
                else "Learning a skill from this conversation…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("learn ack send failed", exc_info=True)
            try:
                event.text = build_learn_prompt(_learn_req)
                # fall through to agent processing
            except Exception:
                return "Could not start /learn — please try again."

        if canonical == "plan":
            # /plan: rewrite the turn to the plan-mode prompt and fall
            # through to normal agent processing (same fall-through as /learn
            # so role alternation is preserved). The live agent inspects the
            # workspace with read-only tools and saves the markdown plan
            # under .hermes/plans/ via write_file. No engine, works on any
            # backend.
            from agent.plan_prompt import build_plan_prompt

            _plan_task = event.get_command_args().strip()
            _ack = (
                f"Planning: {_plan_task[:80]}{'…' if len(_plan_task) > 80 else ''}"
                if _plan_task
                else "Planning from this conversation's context…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("plan ack send failed", exc_info=True)
            try:
                event.text = build_plan_prompt(_plan_task)
                # fall through to agent processing
            except Exception:
                return "Could not start /plan — please try again."

        if canonical == "init":
            # /init: rewrite the turn to a guidance-laden prompt and fall
            # through to normal agent processing (same fall-through as /learn
            # so role alternation is preserved). The live agent scans the
            # project with its own read-only tools and writes/updates
            # AGENTS.md via write_file. No engine, works on any backend.
            from hermes_cli.init_command import build_init_prompt_for_cwd

            _init_notes = event.get_command_args().strip()
            try:
                _init_prompt = build_init_prompt_for_cwd(extra=_init_notes)
            except Exception:
                return "Could not start /init — please try again."
            _ack = (
                "Updating AGENTS.md from a project scan…"
                if "UPDATE the existing AGENTS.md" in _init_prompt
                else "Generating AGENTS.md from a project scan…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("init ack send failed", exc_info=True)
            event.text = _init_prompt
            # fall through to agent processing

        if canonical == "fast":
            return await self._handle_fast_command(event)

        if canonical == "approvals":
            return await self._handle_approvals_command(event)

        if canonical == "model":
            return await self._handle_model_command(event)

        if canonical == "codex-runtime":
            return await self._handle_codex_runtime_command(event)

        if canonical == "personality":
            return await self._handle_personality_command(event)

        if canonical == "suggestions":
            return await self._handle_suggestions_command(event)

        if canonical == "blueprint":
            _blueprint_result = await self._handle_blueprint_command(event)
            _blueprint_seed = getattr(_blueprint_result, "agent_seed", None)
            if _blueprint_seed:
                # Blueprint matched — rewrite the turn to the seed and fall
                # through to _handle_message_with_agent so the agent asks the
                # user for each slot value conversationally and then calls the
                # cronjob tool (the /steer fall-through pattern). The seed
                # enters as a normal user turn, preserving role alternation.
                # Send the "Setting up X…" ack first so the user gets the same
                # immediate feedback CLI users see, instead of silence until
                # the agent's first question.
                _ack = getattr(_blueprint_result, "text", "") or ""
                if _ack:
                    try:
                        adapter = self._adapter_for_source(source)
                        if adapter:
                            _ack_meta = self._thread_metadata_for_source(source)
                            await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
                    except Exception:
                        logger.debug("blueprint ack send failed", exc_info=True)
                try:
                    event.text = _blueprint_seed
                except Exception:
                    return getattr(_blueprint_result, "text", "") or None
            else:
                return getattr(_blueprint_result, "text", "") or None

        if canonical == "save":
            return await self._handle_save_command(event)

        if canonical == "retry":
            return await self._handle_retry_command(event)

        if canonical == "undo":
            async def _do_undo():
                return await self._handle_undo_command(event)
            _undo_n = 1
            _undo_raw = event.get_command_args().strip()
            if _undo_raw:
                try:
                    _undo_n = max(1, int(_undo_raw.split()[0]))
                except (ValueError, IndexError):
                    _undo_n = 1
            _undo_detail = (
                "This removes the last user/assistant exchange from history."
                if _undo_n == 1
                else f"This removes the last {_undo_n} user turns from history."
            )
            return await self._maybe_confirm_destructive_slash(
                event=event,
                command="undo",
                title="/undo",
                detail=_undo_detail,
                execute=_do_undo,
            )

        if canonical == "sethomeserver":
            return await self._handle_set_home_server_command(event)
        if canonical == "setnotify":
            return await self._handle_set_notify_command(event)

        if canonical == "clearnotify":
            return await self._handle_clear_notify_command(event)

        if canonical == "compress":
            return await self._handle_compress_command(event)

        if canonical == "usage":
            return await self._handle_usage_command(event)

        if canonical == "topup":
            return await self._handle_topup_command(event)

        if canonical == "insights":
            return await self._handle_insights_command(event)

        if canonical == "reload-mcp":
            return await self._handle_reload_mcp_command(event)

        if canonical == "reload-skills":
            return await self._handle_reload_skills_command(event)

        if canonical == "bundles":
            return await self._handle_bundles_command(event)

        if canonical == "debug":
            return await self._handle_debug_command(event)

        if canonical == "title":
            return await self._handle_title_command(event)

        if canonical == "resume":
            return await self._handle_resume_command(event)

        if canonical == "sessions":
            return await self._handle_sessions_command(event)

        if canonical == "branch":
            return await self._handle_branch_command(event)

        if canonical == "rollback":
            return await self._handle_rollback_command(event)

        if canonical == "diff":
            return await self._handle_diff_command(event)

        if canonical == "queue":
            queue_payload = event.get_command_args().strip()
            if not queue_payload:
                return "Usage: /queue <prompt>"
            try:
                event.text = queue_payload
            except Exception:
                pass

        if canonical == "steer":
            # No active agent — /steer has no tool call to inject into.
            # Strip the prefix so downstream treats it as a normal user
            # message. If the payload is empty, surface the usage hint.
            steer_payload = event.get_command_args().strip()
            if not steer_payload:
                return "Usage: /steer <prompt>  (no agent is running; sending as a normal message)"
            try:
                event.text = steer_payload
            except Exception:
                pass
            # Do NOT return — fall through to _handle_message_with_agent
            # at the end of this function so the rewritten text is sent
            # to the agent as a regular user turn.

        if canonical == "goal":
            return await self._handle_goal_command(event)

        if canonical == "loop":
            return await self._handle_loop_command(event)

        if canonical == "refine":
            return await self._handle_refine_command(event)
        if canonical == "review":
            return await self._handle_review_command(event)

        if canonical == "moa":
            # /moa is one-shot sugar only: run a single prompt through the
            # default MoA preset, then restore the prior model. To *switch* to a
            # MoA preset for the session, pick it from the model picker (MoA
            # presets surface as a virtual "Mixture of Agents" provider).
            from hermes_cli.moa_config import (
                moa_usage,
                normalize_moa_config,
            )
            from hermes_cli.config import load_config

            moa_payload = event.get_command_args().strip()
            if not moa_payload:
                return moa_usage()
            try:
                cfg = load_config()
                moa_cfg = normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
            except Exception:
                moa_cfg = normalize_moa_config({})
            preset = moa_cfg["default_preset"]
            try:
                event.text = moa_payload
                _moa_state = self._session_state(_quick_key)
                event._moa_restore_override = _moa_state.conversation.model_override
                _moa_state.conversation.model_override = {
                    "provider": "moa",
                    "model": preset,
                    "base_url": "moa://local",
                    "api_key": "moa-virtual-provider",
                    "api_mode": "chat_completions",
                }
                self._evict_cached_agent(_quick_key)
                event._moa_disable_after_turn = True
            except Exception:
                return "Failed to prepare MoA turn."

        if canonical == "voice":
            return await self._handle_voice_command(event)

        # User-defined quick commands (bypass agent loop, no LLM call)
        if command:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if not isinstance(quick_commands, dict):
                quick_commands = {}
            if command in quick_commands:
                # Quick commands are slash capabilities too — and type:exec
                # ones run a shell command in the gateway process. The early
                # gate above only fires for registry-known commands, so quick
                # commands (never in the registry) would otherwise reach this
                # dispatch sink unchecked. Apply the same admin/user policy to
                # the raw typed name here so non-admins can't invoke admin-only
                # quick commands. (#44727)
                _denied = self._check_slash_access(source, command)
                if _denied is not None:
                    return _denied
                qcmd = quick_commands[command]
                if qcmd.get("type") == "exec":
                    exec_cmd = qcmd.get("command", "")
                    if exec_cmd:
                        try:
                            # Sanitize env to prevent credential leakage —
                            # quick commands run in the gateway process which
                            # has all API keys in os.environ.
                            from tools.environments.local import build_subprocess_env
                            sanitized_env = build_subprocess_env()
                            proc = await asyncio.create_subprocess_shell(
                                exec_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                env=sanitized_env,
                            )
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                            output = (stdout or stderr).decode().strip()
                            # Redact any remaining sensitive patterns in output
                            if output:
                                from agent.redact import redact_sensitive_text
                                output = redact_sensitive_text(output)
                            return output if output else "Command returned no output."
                        except asyncio.TimeoutError:
                            return "Quick command timed out (30s)."
                        except Exception as e:
                            return f"Quick command error: {e}"
                    else:
                        return f"Quick command '/{command}' has no command defined."
                elif qcmd.get("type") == "alias":
                    target = (qcmd.get("target") or "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        # Fall through to normal command dispatch below
                    else:
                        return f"Quick command '/{command}' has no target defined."
                else:
                    return f"Quick command '/{command}' has unsupported type (supported: 'exec', 'alias')."

        # Plugin-registered slash commands
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                # Normalize underscores to hyphens so Telegram's underscored
                # autocomplete form matches plugin commands registered with
                # hyphens. See hermes_cli/commands.py:_build_telegram_menu.
                plugin_handler = get_plugin_command_handler(command.replace("_", "-"))
                if plugin_handler:
                    user_args = event.get_command_args().strip()
                    result = plugin_handler(user_args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return str(result) if result else None
            except Exception as e:
                logger.warning("Plugin command dispatch failed: %s", e)

        # Skill slash commands: /skill-name loads the skill and sends to agent.
        # resolve_skill_command_key() handles the Telegram underscore/hyphen
        # round-trip so /claude_code from Telegram autocomplete still resolves
        # to the claude-code skill.
        if command:
            # Skill bundles take precedence over individual skill commands —
            # /<bundle> loads multiple skills at once. Mirrors CLI dispatch.
            _bundle_handled = False
            try:
                from agent.skill_bundles import (
                    build_bundle_invocation_message,
                    resolve_bundle_command_key,
                )
                bundle_key = resolve_bundle_command_key(command)
                if bundle_key is not None:
                    user_instruction = event.get_command_args().strip()
                    # Pass the platform explicitly: bundle skill loading
                    # bypasses get_skill_commands()' scan-time disabled
                    # filter, and the gateway serves multiple platforms in
                    # one process, so env-var platform resolution can't be
                    # trusted here. Mirrors the stacked-skill gate (#58888).
                    _bundle_plat = source.platform.value if source.platform else None
                    bundle_result = build_bundle_invocation_message(
                        bundle_key, user_instruction, task_id=_quick_key,
                        platform=_bundle_plat,
                    )
                    if bundle_result:
                        msg, _loaded, missing = bundle_result
                        event.text = msg
                        _bundle_handled = True
                        if missing:
                            logger.info(
                                "Bundle %s skipped missing skills: %s",
                                bundle_key, ", ".join(missing),
                            )
                        # Fall through to normal message processing with bundle content
            except Exception as exc:
                logger.warning("Bundle dispatch failed: %s", exc)

        if command and not locals().get("_bundle_handled", False):
            try:
                from agent.skill_commands import (
                    get_skill_commands,
                    build_skill_invocation_message,
                    resolve_skill_command_key,
                )
                skill_cmds = get_skill_commands()
                cmd_key = resolve_skill_command_key(command)
                if cmd_key is not None:
                    # Check per-platform disabled status before executing.
                    # get_skill_commands() only applies the *global* disabled
                    # list at scan time; per-platform overrides need checking
                    # here because the cache is process-global across platforms.
                    _skill_name = skill_cmds[cmd_key].get("name", "")
                    _plat = source.platform.value if source.platform else None
                    if _plat and _skill_name:
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        if _skill_name in _get_plat_disabled(platform=_plat):
                            return (
                                f"The **{_skill_name}** skill is disabled for {_plat}.\n"
                                f"Enable it with: `hermes skills config`"
                            )
                    user_instruction = event.get_command_args().strip()
                    # Stacked slash-skill invocations: `/skill-a /skill-b do
                    # XYZ` loads every leading skill (up to 5), not just the
                    # first. Inspired by Claude Code v2.1.199. Mirrors CLI.
                    try:
                        from agent.skill_commands import (
                            build_stacked_skill_invocation_message as _build_stacked,
                            split_stacked_skill_commands,
                        )
                        extra_keys, stacked_instruction = (
                            split_stacked_skill_commands(user_instruction)
                        )
                    except Exception:
                        _build_stacked = None
                        extra_keys, stacked_instruction = [], user_instruction
                    if extra_keys and _plat:
                        # split_stacked_skill_commands() only resolves that
                        # each extra token is a KNOWN skill command — like
                        # get_skill_commands() itself, it has no per-platform
                        # view. Re-check every stacked skill (not just the
                        # leading one above) against the same disabled list,
                        # or a skill an operator disabled for this platform
                        # still gets its full content loaded via the stack.
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        _plat_disabled = _get_plat_disabled(platform=_plat)
                        _disabled_extra = [
                            skill_cmds.get(k, {}).get("name", "")
                            for k in extra_keys
                            if skill_cmds.get(k, {}).get("name", "") in _plat_disabled
                        ]
                        if _disabled_extra:
                            return (
                                f"The **{', '.join(_disabled_extra)}** skill(s) in this "
                                f"stacked invocation are disabled for {_plat}.\n"
                                f"Enable them with: `hermes skills config`"
                            )
                    if extra_keys and _build_stacked is not None:
                        stacked_result = _build_stacked(
                            [cmd_key, *extra_keys],
                            stacked_instruction,
                            task_id=_quick_key,
                        )
                        if stacked_result:
                            msg, _loaded, _missing = stacked_result
                            event.text = msg
                            # Fall through to normal message processing
                        else:
                            return f"Failed to load stacked skills for /{command}."
                    else:
                        msg = build_skill_invocation_message(
                            cmd_key, user_instruction, task_id=_quick_key
                        )
                        if msg:
                            event.text = msg
                            # Fall through to normal message processing with skill content
                else:
                    # Not an active skill — check if it's a known-but-disabled or
                    # uninstalled skill and give actionable guidance.
                    _unavail_msg = _check_unavailable_skill(command)
                    if _unavail_msg:
                        return _unavail_msg
                    # Genuinely unrecognized /command: not a built-in, not a
                    # plugin, not a skill, not a known-inactive skill. Warn
                    # the user instead of silently forwarding it to the LLM
                    # as free text (which leads to silent-failure behavior
                    # like the model inventing a delegate_agent call).
                    # Normalize to hyphenated form before checking known
                    # built-ins (command may be an alias target set by the
                    # quick-command block above, so _cmd_def can be stale).
                    if command.replace("_", "-") not in GATEWAY_KNOWN_COMMANDS:
                        logger.warning(
                            "Unrecognized slash command /%s from %s — "
                            "replying with unknown-command notice",
                            command,
                            source.platform.value if source.platform else "?",
                        )
                        return (
                            f"Unknown command `/{command}`. "
                            f"Type /commands to see what's available, "
                            f"or resend without the leading slash to send "
                            f"as a regular message."
                        )
            except Exception as e:
                logger.debug("Skill command check failed (non-fatal): %s", e)

        # Pending exec approvals are handled by /approve and /deny commands above.
        # No bare text matching — "yes" in normal conversation must not trigger
        # execution of a dangerous command.

        if not is_internal and await asyncio.to_thread(
            self._is_telegram_topic_root_lobby, source
        ):
            # Debounce the lobby reminder so a user who forgets about
            # topic mode and fires ten prompts doesn't get ten copies.
            if self._should_send_telegram_lobby_reminder(source):
                return self._telegram_topic_root_lobby_message()
            return None

        # ── External-drain new-turn gate (Phase 2) ────────────────────
        # When NAS has engaged an external drain (.drain_request.json present,
        # observed by _drain_control_watcher), refuse to START a new turn so
        # the in-flight set can only fall to zero — eliminating the TOCTOU race
        # (D4a: stop accepting new turns FIRST, then NAS polls until
        # active_agents==0). In-flight turns are untouched; this only blocks the
        # claim of a NEW session slot. Internal/system events (restart-recovery
        # replays, background-process completions) bypass the gate — they are
        # not user-initiated new work and must still flow during a drain.
        # Reversible: once the marker is removed the gate opens again.
        if self._external_drain_active and not is_internal:
            logger.info(
                "Refusing new turn for session %s — external drain active.",
                _quick_key,
            )
            return DeferredReply(
                "⏳ This agent is draining for a maintenance action and isn't "
                "accepting new turns right now. It'll be back in a moment — "
                "please resend shortly."
            )

        # ── Claim this session before any await ───────────────────────
        # Between here and _run_agent registering the real AIAgent, there
        # are numerous await points (hooks, vision enrichment, STT,
        # session hygiene compression).  Without this sentinel a second
        # message arriving during any of those yields would pass the
        # "already running" guard and spin up a duplicate agent for the
        # same session — corrupting the transcript.
        _active_session_lease, _limit_message = self._claim_active_session_slot(
            _quick_key,
            source,
        )
        if _limit_message is not None:
            from gateway.overlap_restart import drop_inbound_session_refusal_during_overlap

            if drop_inbound_session_refusal_during_overlap(self, _limit_message, _quick_key):
                logger.info(
                    "Deferring inbound for session %s during overlap-successor handoff",
                    _quick_key,
                )
                return None
            logger.info(
                "Rejecting new active session %s: max_concurrent_sessions reached",
                _quick_key,
            )
            return _limit_message

        # ── FIFO orphan rescue (#99882) ────────────────────────────────
        # If this session went idle with a populated overflow (queued
        # during a busy window whose post-turn drain never promoted —
        # e.g. a compression-demoted follow-up after the compression
        # window ended through an exit that skipped the promotion site),
        # those events were silently orphaned.  We are starting the next
        # turn for this session NOW: re-stage the orphans in FIFO order
        # and enqueue the incoming event behind them, so arrival order
        # (#28503) holds: oldest orphan runs as this turn, the rest drain
        # in order, the new message last.  Skipped for control commands
        # (/stop etc. own their own semantics) and internal events.
        try:
            _orphan_adapter = self._adapter_for_source(source)
            if (
                _orphan_adapter is not None
                and not bool(getattr(event, "internal", False))
                and not event.get_command()
            ):
                _rescued = self._rescue_orphaned_overflow(
                    _quick_key, _orphan_adapter
                )
                if _rescued is not None:
                    # The oldest orphan runs as THIS turn.  Park the
                    # incoming event behind the rest of the chain: into the
                    # slot when the chain was a single orphan (so the
                    # post-turn drain picks it up), otherwise into overflow
                    # behind the already-staged next orphan (FIFO).  A
                    # re-delivered copy of the rescued (or any queued)
                    # message id is dropped, never parked as a second turn.
                    self._rescue_park_incoming_event(
                        _quick_key, event, _orphan_adapter, _rescued
                    )
                    event = _rescued
                    # Same session key by construction; carry the orphan's
                    # own source so reply anchors / thread metadata point
                    # at the message that is actually being answered.
                    _rescued_source = getattr(_rescued, "source", None)
                    if _rescued_source is not None:
                        source = _rescued_source
                    is_internal = bool(getattr(_rescued, "internal", False))
        except Exception:
            logger.debug(
                "FIFO orphan rescue pre-claim failed for %s",
                _quick_key,
                exc_info=True,
            )

        _claim_state = self._session_state(_quick_key)
        if _active_session_lease is not None:
            _claim_state.turn.lease = _active_session_lease
        _claim_state.turn.agent = _AGENT_PENDING_SENTINEL
        _claim_state.turn.started_ts = time.time()
        self._persist_active_agents()
        _run_generation = self._begin_session_run_generation(_quick_key)

        try:
            try:
                _agent_result = await self._handle_message_with_agent(
                    event, source, _quick_key, _run_generation
                )
            except TurnLeaseTimeoutError as exc:
                # This is a rejected message, not a completed agent turn. Return
                # before the /goal judge below so it cannot consume the resend
                # notice and enqueue a synthetic continuation loop.
                logger.error(
                    "Rejecting turn for routing key %s on session %s after "
                    "turn-lease timeout; transcript load was not started and "
                    "the user must resend",
                    _quick_key,
                    exc.session_id,
                )
                return DeferredReply(
                    "⏳ Another turn is still running on this session. To "
                    "protect the transcript, this message was not processed. "
                    "Wait for the active turn to finish, then resend it."
                )
            try:
                await self._run_post_turn_hooks(
                    agent_result=_agent_result,
                    source=source,
                    is_internal=is_internal,
                    event=event,
                )
            except Exception as _goal_exc:
                logger.debug("post-turn hook failed: %s", _goal_exc)
            return _agent_result
        finally:
            # MoA one-shot restore must run on EVERY exit path, not just
            # success. The restore data lives on the per-turn event object
            # (_moa_restore_override), which is discarded once the event goes
            # out of scope — so if _handle_message_with_agent raises, a restore
            # in the try block would be skipped and the MoA override would leak
            # permanently (every later message silently fans out through MoA).
            # Putting it in finally guarantees the revert on success, exception,
            # and interrupt alike.
            self._restore_moa_one_shot(event, _quick_key)
            self._restore_pending_one_turn_model_override(_quick_key)
            # Normal completion/exception/interrupt owns and clears this exact
            # durable marker.  SIGKILL/OOM skips finally, leaving the marker for
            # the next unclean startup's recovery pass.
            await self._clear_durable_active_turn(event)
            # Unconditional release covers every exit path. _release_running_agent_state
            # is idempotent (pop-on-absent is harmless) and, called without a
            # run_generation guard, always clears the slot regardless of which
            # generation it holds. This evicts the zombie left when session_reset
            # bumps the generation (N -> N+1) mid-flight: gen-N's guarded release
            # inside _run_agent returns False, and the old sentinel-only check here
            # missed the leftover real agent — locking the session out forever (#28686).
            self._release_running_agent_state(_quick_key)
            # Turn lease (#64934): release THIS turn's lease token — keyed by
            # (routing key, run generation) so this unwind can only ever free
            # the lease its own turn acquired, never a newer turn's.
            self._release_turn_lease(_quick_key, _run_generation)

    def _restore_pending_one_turn_model_override(self, session_key: str) -> None:
        """Restore a per-session model override after ``/model --once`` runs."""
        from gateway.run import logger

        if not session_key:
            return
        try:
            _otr_state = self._peek_session_state(session_key)
            snapshot = _otr_state.conversation.one_turn_restore if _otr_state else None
            if _otr_state is not None:
                _otr_state.conversation.one_turn_restore = None
            if not snapshot:
                return
            self._restore_session_model_override(session_key, snapshot)
        except Exception:
            logger.debug("Failed to restore one-turn model override", exc_info=True)

    def _prefix_inbound_sender_context(self, event: MessageEvent, source: SessionSource, message_text: str) -> str:
        """Attribute the sender in shared multi-user sessions and prepend history-backfill channel context."""
        _is_shared_multi_user = is_shared_multi_user_session(
            source, group_sessions_per_user=getattr(self.config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(self.config, "thread_sessions_per_user", False),
        )
        if _is_shared_multi_user and source.user_name:
            # Display names are attacker-influenceable: neutralize newlines/control chars or a
            # hostile name masquerades as a fake markdown section (mirrors build_session_context_prompt).
            _safe_user_name = neutralize_untrusted_inline_text(source.user_name)
            # Slack: expose the CURRENT speaker's verifiable `<@U...>` id so "mention me again" has a
            # trusted target (display names are ambiguous). user_id comes from the envelope, not user-editable.
            # See #17916.
            if source.platform == Platform.SLACK and source.user_id:
                _safe_user_name = f"{_safe_user_name} | Slack user <@{source.user_id}>"
            message_text = f"[{_safe_user_name}] {message_text}"
        # After the sender-prefix so the prefix applies only to the trigger message, not the backfill.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\n\n[New message]\n{message_text}"
        return message_text

    @staticmethod
    def _classify_inbound_media(
        event: MessageEvent, pending_stt_prepared: bool
    ) -> Tuple[list, list, list, list]:
        """Split ``event.media_urls`` into (image, STT-voice, audio-file, video) paths. Per-attachment
        MIME wins over the message-level type (a document sent alongside an image must not be routed
        as an image). MessageType.AUDIO / mixed DOCUMENT audio is a file attachment, never STT."""
        from gateway.run import _event_media_is_audio, _event_media_is_image, _event_media_is_stt_input
        image_paths, audio_paths, audio_file_paths, video_paths = [], [], [], []
        for i, path in enumerate(event.media_urls or []):
            mtype = event.media_types[i] if i < len(event.media_types) else ""
            if _event_media_is_image(event, i):
                image_paths.append(path)
            if _event_media_is_audio(event, i):
                if event.message_type in {MessageType.AUDIO, MessageType.DOCUMENT}:
                    audio_file_paths.append(path)
                elif not pending_stt_prepared and _event_media_is_stt_input(event, i):
                    audio_paths.append(path)
            if mtype.startswith("video/") or (not mtype and event.message_type == MessageType.VIDEO):
                video_paths.append(path)
        return image_paths, audio_paths, audio_file_paths, video_paths

    async def _enrich_inbound_images(
        self, source: SessionSource, session_key: str, message_text: str, image_paths: list[str]
    ) -> str:
        """Route images natively (attach pixels at run_conversation) or pre-analyze them into text."""
        from gateway.run import logger

        # See agent/image_routing.py. Offloaded to a thread: the decision does blocking network I/O
        # (models.dev fetch on cache miss, Ollama /api/show probe) that would stall the event loop.
        _img_mode = await asyncio.to_thread(
            self._decide_image_input_mode, source=source, session_key=session_key,
        )
        if _img_mode == "native":
            self._session_state(session_key).persistent.native_image_paths = list(image_paths)
            logger.info(
                "Image routing: native (model supports vision). %d image(s) will be attached inline.",
                len(image_paths),
            )
            return message_text
        logger.info(
            "Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.",
            _img_mode, len(image_paths),
        )
        # Vision enrichment runs before AIAgent.run_conversation(), so bind this session's resolved
        # runtime explicitly rather than consulting process-global compatibility mirrors.
        vision_runtime = None
        try:
            turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                source=source, session_key=session_key,
            )
            vision_runtime = {**(runtime_kwargs or {}), "model": turn_model}
        except Exception:
            logger.debug("vision enrichment: session runtime resolution failed", exc_info=True)

        from agent.auxiliary_client import scoped_runtime_main

        with scoped_runtime_main(vision_runtime):
            return await self._enrich_message_with_vision(message_text, image_paths)

    async def _echo_stt_transcripts(
        self, adapter, source: SessionSource, transcripts: List[str], *, metadata=None, log_context: str = "Transcript"
    ) -> None:
        """Send each transcript back as ``🎙️ "…"`` (best-effort; failures are logged, never raised)."""
        from gateway.run import logger

        for tx in transcripts:
            try:
                await adapter.send(source.chat_id, f'🎙️ "{tx}"', metadata=metadata)
            except Exception as echo_exc:
                logger.debug("%s echo failed (non-fatal): %s", log_context, echo_exc)

    async def _enrich_inbound_voice(
        self, event: MessageEvent, source: SessionSource, message_text: str, audio_paths: list[str]
    ) -> str:
        message_text, _successful_transcripts = await self._enrich_message_with_transcription(
            message_text, audio_paths,
        )
        # Echo each successful transcript back immediately when configured so users can verify STT
        # quality in real time. On transcription failure do NOT send a hardcoded notice: that
        # bypassed the LLM and produced two replies; enrichment leaves one neutral marker instead.
        if _successful_transcripts and self._should_echo_stt_transcripts():
            _echo_adapter = self._delivery_adapter_for(source)
            if _echo_adapter:
                _echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                await self._echo_stt_transcripts(_echo_adapter, source, _successful_transcripts, metadata=_echo_meta)
        return message_text

    @staticmethod
    def _inbound_attachment_display_name(path: str) -> Tuple[str, str]:
        """``(display_name, agent_visible_path)``: cache filename is ``<id>_<id>_<original>``; the
        path is translated to the in-container mount under a Docker backend."""
        from tools.credential_files import to_agent_visible_cache_path
        basename = os.path.basename(path)
        parts = basename.split("_", 2)
        return re.sub(r'[^\w.\- ]', '_', parts[2] if len(parts) >= 3 else basename), to_agent_visible_cache_path(path)

    @classmethod
    def _prepend_inbound_media_file_notes(cls, message_text: str, audio_file_paths: list[str], video_paths: list[str]) -> str:
        """Prepend a path-pointing note per audio-file / video attachment (content is not inlined)."""
        for kind, noun, verb, tool, paths in (
            ("an audio file attachment", "audio", "transcribe or process", "a transcription or media tool", audio_file_paths),
            ("a video attachment", "video", "inspect or process", "a video analysis or media tool", video_paths),
        ):
            for _path in paths:
                _display, _agent_path = cls._inbound_attachment_display_name(_path)
                message_text = (
                    f"[The user sent {kind}: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the {noun} contains, {verb} it yourself — for "
                    f"example by passing the path to {tool} — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                    f"\n\n{message_text}"
                )
        return message_text

    @classmethod
    def _prepend_inbound_document_notes(cls, event: MessageEvent, message_text: str) -> str:
        """Prepend a context note per non-media attachment (anything not routed as image/audio/video)."""
        from gateway.run import (
            _build_document_context_note, _event_media_is_audio, _event_media_is_image,
            _event_media_is_video,
        )
        if not event.media_urls:
            return message_text
        import mimetypes as _mimetypes

        _TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
        inline_flags = getattr(event, "media_text_inlined", None) or []
        for i, path in enumerate(event.media_urls):
            # A document mixed into a PHOTO/VOICE message (message-level type != DOCUMENT) still
            # reaches the agent; only genuine non-media files get a note.
            if any(f(event, i) for f in (_event_media_is_image, _event_media_is_audio, _event_media_is_video)):
                continue
            mtype = event.media_types[i] if i < len(event.media_types) else ""
            if mtype in {"", "application/octet-stream"}:
                _is_text = os.path.splitext(path)[1].lower() in _TEXT_EXTENSIONS
                mtype = "text/plain" if _is_text else (_mimetypes.guess_type(path)[0] or "application/octet-stream")
            # Every accepted file gets a note — a non-text/non-application MIME (font/*, model/*)
            # must still tell the agent the file exists.
            display_name, agent_path = cls._inbound_attachment_display_name(path)
            inline_flag = inline_flags[i] if i < len(inline_flags) else None
            context_note = _build_document_context_note(
                display_name, agent_path, mtype, content_inlined=inline_flag is not False,
            )
            message_text = f"{context_note}\n\n{message_text}"
        return message_text

    @staticmethod
    def _prepend_inbound_reply_context(event: MessageEvent, source: SessionSource, message_text: str) -> str:
        """Prepend the reply-to pointer, then the Discord triggering-message note (outermost)."""
        if getattr(event, "reply_to_text", None) and event.reply_to_message_id:
            # Always inject the reply-to pointer even when the quoted text is already in history:
            # it's disambiguation (*which* prior message), not deduplication.
            # Adapters resolve the original message (or the user's native partial quote).
            # A preview here silently loses later list items and code; keep that context intact.
            reply_text = event.reply_to_text
            _who = " your previous message" if getattr(event, "reply_to_is_own_message", False) else ""
            message_text = f'[Replying to{_who}: "{reply_text}"]\n\n{message_text}'

        # Discord: the triggering message id goes on the per-turn user message, never the cached
        # system prompt — it changes every turn and would bust the agent-cache signature. It is
        # the OUTERMOST prefix so strip_discord_triggering_note can peel exactly it off the
        # persisted transcript row without touching the reply pointer.
        if (
            source is not None
            and getattr(source, "platform", None) == Platform.DISCORD
            and getattr(event, "message_id", None)
        ):
            from gateway.session import _discord_tools_loaded as _disc_tools_loaded
            if _disc_tools_loaded():
                message_text = f"{discord_triggering_note(event.message_id)}\n\n{message_text}"
        return message_text

    async def _inbound_model_context_length(self, source: SessionSource, session_key: str) -> int:
        """Context length of the model this turn runs on. A global ``model.context_length`` pin
        belongs to the configured model, not a /model or channel override; custom-provider limits win."""
        from gateway.run import _load_gateway_config
        from agent.model_metadata import get_model_context_length_async

        _msg_config_ctx = None
        _msg_cfg = None
        _msg_model_cfg = {}
        _msg_custom_providers = []
        with suppress(Exception):
            _msg_cfg = _load_gateway_config()
            _msg_model_cfg = _msg_cfg.get("model", {})
            if isinstance(_msg_model_cfg, dict):
                _msg_raw_ctx = _msg_model_cfg.get("context_length")
                if _msg_raw_ctx is not None:
                    _msg_config_ctx = int(_msg_raw_ctx)
            try:
                from hermes_cli.config import get_compatible_custom_providers

                _msg_custom_providers = get_compatible_custom_providers(_msg_cfg)
            except Exception:
                _msg_custom_providers = _msg_cfg.get("custom_providers") or []
        # GatewayRunner has no self._model/self._base_url; resolve the session's actual runtime.
        _msg_model, _msg_runtime = self._resolve_session_agent_runtime(
            source=source, session_key=session_key, user_config=_msg_cfg,
        )
        _msg_base_url = _msg_runtime.get("base_url") or ""
        if isinstance(_msg_model_cfg, dict):
            _msg_configured_model = _msg_model_cfg.get("default") or _msg_model_cfg.get("model")
        else:
            _msg_configured_model = _msg_model_cfg  # (no dict → no pin was read; ctx is already None)
        if _msg_model != _msg_configured_model:
            _msg_config_ctx = None
        if _msg_config_ctx is not None:
            try:
                from hermes_cli.route_identity import should_clear_context_pin_async

                if await should_clear_context_pin_async(
                    None, None,  # model match already checked above
                    _msg_model_cfg.get("base_url"), _msg_base_url,
                    _msg_model_cfg.get("provider"), _msg_runtime.get("provider"),
                ):
                    _msg_config_ctx = None
            except Exception:
                _msg_config_ctx = None
        if _msg_custom_providers and _msg_base_url:
            with suppress(Exception):
                from hermes_cli.config import get_custom_provider_context_length

                _msg_config_ctx = get_custom_provider_context_length(
                    model=_msg_model, base_url=_msg_base_url, custom_providers=_msg_custom_providers,
                ) or _msg_config_ctx
        return await get_model_context_length_async(
            _msg_model, base_url=_msg_base_url, api_key=_msg_runtime.get("api_key") or "",
            config_context_length=_msg_config_ctx, provider=_msg_runtime.get("provider") or "",
            custom_providers=_msg_custom_providers,
        )

    async def _expand_inbound_context_references(
        self, source: SessionSource, session_key: str, message_text: str
    ) -> Optional[str]:
        """Expand ``@`` context references; returns None when the injection was refused (user notified)."""
        from gateway.run import logger

        try:
            from agent.context_references import preprocess_context_references_async

            try:
                from tools.terminal_scope import terminal_env as _ts_env
            except ImportError:
                _ts_env = os.environ.get
            _msg_cwd = _ts_env("TERMINAL_CWD", os.path.expanduser("~"))
            _msg_ctx_len = await self._inbound_model_context_length(source, session_key)
            _ctx_result = await preprocess_context_references_async(
                message_text, cwd=_msg_cwd, context_length=_msg_ctx_len, allowed_root=_msg_cwd
            )
            if _ctx_result.blocked:
                _adapter = self._delivery_adapter_for(source)
                if _adapter:
                    await _adapter.send(
                        source.chat_id,
                        "\n".join(_ctx_result.warnings) or "Context injection refused.",
                    )
                return None
            if _ctx_result.expanded:
                message_text = _ctx_result.message
        except Exception as exc:
            logger.warning("@ context reference expansion failed: %s", exc)
            logger.debug("@ context reference expansion failure detail", exc_info=True)
        return message_text

    async def _prepare_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Prepare inbound event text for the agent.

        Keep the normal inbound path and the queued follow-up path on the same
        preprocessing pipeline so sender attribution, image enrichment, STT,
        document notes, reply context, and @ references all behave the same.

        Side effect: buffers per-session native image paths when the active
        model supports native vision AND the user has images attached. The
        caller consumes and clears that session-scoped buffer at the
        ``run_conversation`` site to build a multimodal user turn. When the
        list is empty, the ``_enrich_message_with_vision`` text path has
        already run and images are represented in-text.
        """
        from gateway.run import (
            _build_document_context_note,
            _event_media_is_audio,
            _event_media_is_image,
            _event_media_is_stt_input,
            _event_media_is_video,
            _load_gateway_config,
        )
        from gateway.run import logger

        history = history or []
        _pending_stt_prepared = hasattr(event, "_gateway_pending_stt_text")
        message_text = (
            getattr(event, "_gateway_pending_stt_text", None)
            if _pending_stt_prepared
            else event.text
        ) or ""
        _group_sessions_per_user = getattr(self.config, "group_sessions_per_user", True)
        _thread_sessions_per_user = getattr(self.config, "thread_sessions_per_user", False)
        # Prefer the already resolved session key from the caller so this write
        # key matches the consume key at the run_conversation site. Fall back
        # to deriving it here for tests and legacy standalone callers.
        session_key = session_key or self._session_key_for_source(source)
        # Reset only this session's per-call buffer; other sessions may be
        # concurrently preparing multimodal turns on the same runner.
        self._consume_pending_native_image_paths(session_key)

        _is_shared_multi_user = is_shared_multi_user_session(
            source,
            group_sessions_per_user=_group_sessions_per_user,
            thread_sessions_per_user=_thread_sessions_per_user,
        )
        # Sender attribution is for turns a human actually authored: gate it
        # on real content (text or media).  A content-less internal event —
        # the startup auto-resume turn synthesized by
        # _schedule_resume_pending_sessions with text="" — must stay empty;
        # the "[name] " prefix alone would make the resume-pending note
        # builder see a truthy message and claim a NEW user message exists
        # when nobody wrote one (has-message wording on a synthesized turn).
        if (
            _is_shared_multi_user
            and source.user_name
            and (message_text.strip() or event.media_urls)
        ):
            # source.user_name is the platform display name — attacker-
            # influenceable on any platform that lets participants set their
            # own name. Neutralize embedded newlines/control chars before
            # interpolating it into every message in the shared session, or
            # a hostile name can masquerade as a fake markdown section
            # (mirrors the same field's treatment in
            # build_session_context_prompt via _format_untrusted_prompt_value).
            _safe_user_name = neutralize_untrusted_inline_text(source.user_name)
            # On Slack, expose the current author's verifiable user ID next to
            # the display name (#17916): "mention me again" requests need a
            # trusted `<@U...>` target for the CURRENT speaker — display names
            # are ambiguous and historical mentions may point at someone else.
            # The user_id comes from the Slack event envelope (not
            # user-editable text), so it does not need neutralization.
            if source.platform == Platform.SLACK and source.user_id:
                _safe_user_name = (
                    f"{_safe_user_name} | Slack user <@{source.user_id}>"
                )
            message_text = f"[{_safe_user_name}] {message_text}"

        # Prepend channel context from history backfill (if any).  This
        # happens after sender-prefix so the prefix only applies to the
        # trigger message, not the backfill block.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\n\n[New message]\n{message_text}"

        # Declare at outer scope so the audio-file-paths handling block below
        # remains safe when ``event.media_urls`` is empty (no inner block runs).
        audio_file_paths: list[str] = []
        video_paths: list[str] = []

        if event.media_urls:
            image_paths = []
            audio_paths = []
            for i, path in enumerate(event.media_urls):
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                # Classify images per-attachment: trust this attachment's own
                # MIME, and only honour the message-level PHOTO type when the
                # per-attachment MIME is unknown. Otherwise a document (or any
                # non-image) sent alongside an image in the same message gets
                # mis-routed here as an image and the provider 400s.
                if _event_media_is_image(event, i):
                    image_paths.append(path)
                # MessageType.AUDIO = audio file attachment (e.g. .mp3, .m4a) — never STT.
                # Mixed DOCUMENT events also preserve audio as a file path instead of
                # dropping it or treating it as a voice note.
                if _event_media_is_audio(event, i):
                    if event.message_type in {MessageType.AUDIO, MessageType.DOCUMENT}:
                        audio_file_paths.append(path)
                    elif not _pending_stt_prepared and _event_media_is_stt_input(event, i):
                        audio_paths.append(path)
                if mtype.startswith("video/") or (not mtype and event.message_type == MessageType.VIDEO):
                    video_paths.append(path)

            if image_paths:
                # Decide routing: native (attach pixels) vs text (vision_analyze
                # pre-run + prepend description).  See agent/image_routing.py.
                # Offload to a worker thread: the decision does blocking network
                # I/O — a models.dev fetch on cache miss, and the Ollama
                # ``/api/show`` capability probe for local servers — whose
                # request timeout would otherwise stall the whole gateway event
                # loop (every session) while a single image is routed.
                _img_mode = await asyncio.to_thread(
                    self._decide_image_input_mode,
                    source=source,
                    session_key=session_key,
                )
                if _img_mode == "native":
                    # Defer attachment to the run_conversation call site.
                    self._session_state(
                        session_key
                    ).persistent.native_image_paths = list(image_paths)
                    logger.info(
                        "Image routing: native (model supports vision). %d image(s) will be attached inline.",
                        len(image_paths),
                    )
                else:
                    logger.info(
                        "Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.",
                        _img_mode, len(image_paths),
                    )
                    # Vision enrichment runs before AIAgent.run_conversation(),
                    # so bind this session's resolved runtime explicitly rather
                    # than consulting process-global compatibility mirrors.
                    vision_runtime = None
                    try:
                        turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                            source=source,
                            session_key=session_key,
                        )
                        vision_runtime = dict(runtime_kwargs or {})
                        vision_runtime["model"] = turn_model
                    except Exception:
                        logger.debug(
                            "vision enrichment: session runtime resolution failed",
                            exc_info=True,
                        )

                    from agent.auxiliary_client import scoped_runtime_main

                    with scoped_runtime_main(vision_runtime):
                        message_text = await self._enrich_message_with_vision(
                            message_text,
                            image_paths,
                        )

            if audio_paths:
                message_text, _successful_transcripts = await self._enrich_message_with_transcription(
                    message_text,
                    audio_paths,
                )
                # Echo each successful transcript back to the user immediately
                # when configured. Lets users verify STT quality in real-time,
                # while allowing quiet STT for users who only want the agent to
                # receive the transcription.
                if _successful_transcripts and self._should_echo_stt_transcripts():
                    _echo_adapter = self._adapter_for_source(source)
                    _echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    if _echo_adapter:
                        for _tx in _successful_transcripts:
                            try:
                                await _echo_adapter.send(
                                    source.chat_id,
                                    f'🎙️ "{_tx}"',
                                    metadata=_echo_meta,
                                )
                            except Exception as _echo_exc:
                                logger.debug(
                                    "Transcript echo failed (non-fatal): %s", _echo_exc,
                                )
                # NOTE: Previously, when transcription failed (e.g. no STT
                # provider configured), the gateway also emitted a hardcoded
                # English notice via `_stt_adapter.send()`. That bypassed the
                # LLM and produced two replies — one pre-canned English clip
                # (which TTS then spoke aloud, in the wrong language) and one
                # correct, localized LLM reply from the enriched message text.
                # The enrichment step now leaves a single neutral marker in the
                # prompt, so the LLM produces one coherent reply in the user's
                # language. The hardcoded send has therefore been removed.

        if audio_file_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _apath in audio_file_paths:
                _basename = os.path.basename(_apath)
                _parts = _basename.split("_", 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub(r'[^\w.\- ]', '_', _display)
                _agent_path = _to_agent_path(_apath)
                _note = (
                    f"[The user sent an audio file attachment: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the audio contains, transcribe or process it yourself — for "
                    f"example by passing the path to a transcription or media tool — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                )
                message_text = f"{_note}\n\n{message_text}"

        if video_paths:
            from tools.credential_files import to_agent_visible_cache_path as _to_agent_path
            for _vpath in video_paths:
                _basename = os.path.basename(_vpath)
                _parts = _basename.split("_", 2)
                _display = _parts[2] if len(_parts) >= 3 else _basename
                _display = re.sub(r'[^\w.\- ]', '_', _display)
                _agent_path = _to_agent_path(_vpath)
                _note = (
                    f"[The user sent a video attachment: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the video contains, inspect or process it yourself — for "
                    f"example by passing the path to a video analysis or media tool — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                )
                message_text = f"{_note}\n\n{message_text}"

        if event.media_urls:
            import mimetypes as _mimetypes
            from tools.credential_files import to_agent_visible_cache_path

            _TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
            for i, path in enumerate(event.media_urls):
                # Per-attachment document handling. Skip anything already routed
                # as image / audio / video by the buckets above — only genuine
                # non-media files get a path-pointing context note. This makes a
                # document mixed into a PHOTO/VOICE message (whole-message type
                # != DOCUMENT) still reach the agent as a readable cached file,
                # instead of being silently dropped because the message-level
                # type wasn't DOCUMENT.
                if (
                    _event_media_is_image(event, i)
                    or _event_media_is_audio(event, i)
                    or _event_media_is_video(event, i)
                ):
                    continue
                mtype = event.media_types[i] if i < len(event.media_types) else ""
                if mtype in {"", "application/octet-stream"}:
                    _ext = os.path.splitext(path)[1].lower()
                    if _ext in _TEXT_EXTENSIONS:
                        mtype = "text/plain"
                    else:
                        guessed, _ = _mimetypes.guess_type(path)
                        if guessed:
                            mtype = guessed
                        else:
                            mtype = "application/octet-stream"
                # Any accepted file gets a path-pointing context note — we accept
                # all file types now, so a non-text/non-application MIME (font/*,
                # model/*, etc.) must still tell the agent the file exists.

                basename = os.path.basename(path)
                parts = basename.split("_", 2)
                display_name = parts[2] if len(parts) >= 3 else basename
                display_name = re.sub(r'[^\w.\- ]', '_', display_name)

                # Translate host cache path to in-container path if running under Docker backend.
                # This ensures the agent receives a path it can open inside its sandbox, as the
                # cache directories are auto-mounted at /root/.hermes/cache/* by get_cache_directory_mounts().
                agent_path = to_agent_visible_cache_path(path)

                inline_flags = getattr(event, "media_text_inlined", None) or []
                inline_flag = inline_flags[i] if i < len(inline_flags) else None
                context_note = _build_document_context_note(
                    display_name,
                    agent_path,
                    mtype,
                    content_inlined=inline_flag is not False,
                )
                message_text = f"{context_note}\n\n{message_text}"

        # Re-delivery marker: the queue layer already saw this platform
        # message id (drain-snapshot replay, FIFO promotion/rescue of a
        # duplicated id). The note is per-turn user content — like the
        # Discord id note below, baking it into the cached system prompt
        # would bust the agent-cache signature — and tells the model this is
        # the SAME message re-delivered, not a new request, so it answers
        # once instead of per copy.
        # Restart-drain lifecycle receipt, proved and rendered by the
        # queue's own module (see ``run_drain_queue.drain_restart_receipt_for_record``
        # and ``format_drain_restart_receipt``); None leaves the text unchanged.
        from gateway.run_drain_queue import format_drain_restart_receipt

        _receipt_note = format_drain_restart_receipt(event)
        if _receipt_note is not None:
            message_text = f"{_receipt_note}\n\n{message_text}"

        if getattr(event, "redelivered", False):
            _redelivery_id = getattr(event, "message_id", None)
            _redelivery_note = "[Re-delivered message"
            if _redelivery_id:
                _redelivery_note += f" (id: `{_redelivery_id}`)"
            _redelivery_note += (
                " — the gateway received this platform message more than once "
                "(queue replay / re-delivery). It is the SAME message, not a "
                "new request: answer it once and ignore any further copies.]"
            )
            message_text = f"{_redelivery_note}\n\n{message_text}"

        # Discord: surface the triggering message id per-turn on the user
        # message rather than in the cached system prompt. message_id changes
        # every turn, so baking it into build_session_context_prompt() would
        # bust the agent-cache signature and rebuild the AIAgent every message
        # (destroying prompt caching). The static IDs block points the agent
        # here; the volatile id rides the per-turn user content.
        if (
            source is not None
            and getattr(source, "platform", None) == Platform.DISCORD
            and getattr(event, "message_id", None)
        ):
            from gateway.session import _discord_tools_loaded as _disc_tools_loaded
            if _disc_tools_loaded():
                message_text = (
                    f"[Triggering message id: `{event.message_id}` — use as "
                    f"`message_id` for reply/react/pin via the discord tools.]\n\n"
                    f"{message_text}"
                )

        if getattr(event, "reply_to_text", None) and event.reply_to_message_id:
            # Always inject the reply-to pointer — even when the quoted text
            # already appears in history. The prefix isn't deduplication, it's
            # disambiguation: it tells the agent *which* prior message the user
            # is referencing. History can contain the same or similar text
            # multiple times, and without an explicit pointer the agent has to
            # guess (or answer for both subjects). Token overhead is minimal.
            reply_snippet = event.reply_to_text[:500]
            if getattr(event, "reply_to_is_own_message", False):
                message_text = (
                    f'[Replying to your previous message: "{reply_snippet}"]\n\n'
                    f"{message_text}"
                )
            else:
                message_text = f'[Replying to: "{reply_snippet}"]\n\n{message_text}'

        if "@" in message_text:
            try:
                from agent.context_references import preprocess_context_references_async
                from agent.model_metadata import get_model_context_length_async

                try:
                    from tools.terminal_scope import terminal_env as _ts_env
                except ImportError:
                    _msg_cwd = os.environ.get("TERMINAL_CWD", os.path.expanduser("~"))
                else:
                    _msg_cwd = _ts_env("TERMINAL_CWD", os.path.expanduser("~"))
                _msg_config_ctx = None
                _msg_cfg = None
                _msg_model_cfg = {}
                _msg_custom_providers = []
                try:
                    _msg_cfg = _load_gateway_config()
                    _msg_model_cfg = _msg_cfg.get("model", {})
                    if isinstance(_msg_model_cfg, dict):
                        _msg_raw_ctx = _msg_model_cfg.get("context_length")
                        if _msg_raw_ctx is not None:
                            _msg_config_ctx = int(_msg_raw_ctx)
                    try:
                        from hermes_cli.config import get_compatible_custom_providers

                        _msg_custom_providers = get_compatible_custom_providers(_msg_cfg)
                    except Exception:
                        _msg_custom_providers = _msg_cfg.get("custom_providers") or []
                except Exception:
                    pass
                # Resolve the session's actual model/provider/base_url the
                # same way the hygiene compression block does (~11080).
                # GatewayRunner has no self._model/self._base_url attrs
                # (that was copy-pasted from HermesCLI, which does carry
                # self.model/self.base_url), so using them here always raised
                # AttributeError, silently caught below, meaning this feature
                # never ran.
                _msg_model, _msg_runtime = self._resolve_session_agent_runtime(
                    source=source,
                    session_key=session_key,
                    user_config=_msg_cfg,
                )
                _msg_base_url = _msg_runtime.get("base_url") or ""
                # A global model.context_length belongs to the configured
                # model, not a session /model or channel override. Prefer a
                # matching per-custom-provider model limit when available.
                _msg_configured_model = (
                    _msg_model_cfg.get("default") or _msg_model_cfg.get("model")
                    if isinstance(_msg_model_cfg, dict)
                    else _msg_model_cfg
                )
                if _msg_model != _msg_configured_model:
                    _msg_config_ctx = None
                if _msg_config_ctx is not None and isinstance(_msg_model_cfg, dict):
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async

                        if await should_clear_context_pin_async(
                            None,  # model match already checked above
                            None,
                            _msg_model_cfg.get("base_url"),
                            _msg_base_url,
                            _msg_model_cfg.get("provider"),
                            _msg_runtime.get("provider"),
                        ):
                            _msg_config_ctx = None
                    except Exception:
                        _msg_config_ctx = None
                if _msg_custom_providers and _msg_base_url:
                    try:
                        from hermes_cli.config import get_custom_provider_context_length

                        _msg_custom_ctx = get_custom_provider_context_length(
                            model=_msg_model,
                            base_url=_msg_base_url,
                            custom_providers=_msg_custom_providers,
                        )
                        if _msg_custom_ctx:
                            _msg_config_ctx = _msg_custom_ctx
                    except Exception:
                        pass
                _msg_ctx_len = await get_model_context_length_async(
                    _msg_model,
                    base_url=_msg_base_url,
                    api_key=_msg_runtime.get("api_key") or "",
                    config_context_length=_msg_config_ctx,
                    provider=_msg_runtime.get("provider") or "",
                    custom_providers=_msg_custom_providers,
                )
                _ctx_result = await preprocess_context_references_async(
                    message_text,
                    cwd=_msg_cwd,
                    context_length=_msg_ctx_len,
                    allowed_root=_msg_cwd,
                )
                if _ctx_result.blocked:
                    _adapter = self._adapter_for_source(source)
                    if _adapter:
                        await _adapter.send(
                            source.chat_id,
                            "\n".join(_ctx_result.warnings) or "Context injection refused.",
                        )
                    return None
                if _ctx_result.expanded:
                    message_text = _ctx_result.message
            except Exception as exc:
                logger.warning("@ context reference expansion failed: %s", exc)
                logger.debug("@ context reference expansion failure detail", exc_info=True)

        return message_text

    async def _prepare_profile_scoped_inbound_message_text(
        self,
        *,
        event: MessageEvent,
        source: SessionSource,
        history: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Run inbound preprocessing under the routed profile when multiplexed."""
        from gateway.run import _async_profile_runtime_scope

        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            async with _async_profile_runtime_scope(
                self._resolve_profile_home_for_source(source)
            ):
                return await self._prepare_inbound_message_text(
                    event=event,
                    source=source,
                    history=history,
                    session_key=session_key,
                )
        return await self._prepare_inbound_message_text(
            event=event,
            source=source,
            history=history,
            session_key=session_key,
        )

    async def _prepare_clarify_reply_text(self, event) -> str:
        """Return raw text or successful voice transcripts for a clarify reply."""
        if not self._pending_event_audio_paths(event):
            return (event.text or "").strip()

        _, successful_transcripts = await self._transcribe_pending_audio_event_once(
            event, "",
        )
        return "\n\n".join(
            transcript.strip()
            for transcript in successful_transcripts
            if transcript.strip()
        )

    def _consume_pending_native_image_paths(self, session_key: str) -> List[str]:
        state = self._peek_session_state(session_key)
        if state is None or not state.persistent.native_image_paths:
            return []
        paths = list(state.persistent.native_image_paths)
        state.persistent.native_image_paths = []
        return paths

    async def _mark_durable_active_turn(
        self,
        event: "MessageEvent",
        session_key: str,
    ) -> bool:
        """Persist the exact resolved routing key for this running turn."""
        from gateway.run import logger

        event._gateway_processing_deferred = False
        try:
            token = await self.async_session_store.mark_turn_active(session_key)
        except Exception as exc:
            logger.warning(
                "Could not persist active-turn marker for %s: %s",
                session_key,
                exc,
            )
            return False
        if not token:
            return False
        # Private event attributes are process-local ownership state.  Keep the
        # token out of public metadata, transcripts, and platform payloads.
        setattr(event, "_gateway_active_turn_session_key", session_key)
        setattr(event, "_gateway_active_turn_token", token)
        return True

    async def _clear_durable_active_turn(self, event: "MessageEvent") -> bool:
        """Best-effort CAS clear of the marker owned by *event*."""
        from gateway.run import logger

        session_key = getattr(event, "_gateway_active_turn_session_key", None)
        token = getattr(event, "_gateway_active_turn_token", None)
        try:
            if not session_key or not token:
                return False
            last_error: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    return bool(
                        await self.async_session_store.clear_turn_active(
                            session_key, token
                        )
                    )
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        logger.debug(
                            "Retrying active-turn marker cleanup for %s (%d/3): %s",
                            session_key,
                            attempt,
                            exc,
                        )
            # Never let marker cleanup block in-memory agent/lease release.  A
            # stale marker is bounded by the configured agent timeout and the
            # clean-start orphan-marker discard path.
            logger.warning(
                "Could not clear active-turn marker for %s after 3 attempts: %s",
                session_key,
                last_error,
            )
            return False
        finally:
            for attr in (
                "_gateway_active_turn_session_key",
                "_gateway_active_turn_token",
            ):
                try:
                    delattr(event, attr)
                except AttributeError:
                    pass

    def _install_plugin_message_injector(self) -> None:
        """Publish this live gateway's plugin message scheduler."""
        from hermes_cli.plugins import get_plugin_manager

        get_plugin_manager().set_gateway_message_injector(
            self, self._schedule_plugin_message_injection
        )

    def _clear_plugin_message_injector(self) -> None:
        """Remove this runner's scheduler without clobbering a newer owner."""
        from hermes_cli.plugins import get_plugin_manager

        get_plugin_manager().clear_gateway_message_injector(self)

    def _schedule_plugin_message_injection(
        self,
        *,
        session_key: str,
        content: str,
        plugin_id: str,
    ) -> bool:
        """Schedule a plugin-triggered turn on the live gateway loop."""
        from gateway.run import safe_schedule_threadsafe
        from gateway.run import logger

        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or loop.is_closed():
            return False

        coro = self._dispatch_plugin_message_injection(
            session_key=session_key,
            content=content,
            plugin_id=plugin_id,
        )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            try:
                future = loop.create_task(coro)
            except Exception:
                coro.close()
                logger.warning(
                    "Plugin message injection scheduling failed",
                    exc_info=True,
                )
                return False
            self._background_tasks.add(future)
            future.add_done_callback(self._background_tasks.discard)
        else:
            future = safe_schedule_threadsafe(
                coro,
                loop,
                logger=logger,
                log_message="Plugin message injection scheduling failed",
                log_level=logging.WARNING,
            )
            if future is None:
                return False

        def _log_result(completed) -> None:
            try:
                accepted = completed.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                return
            except Exception:
                logger.warning(
                    "Plugin message injection failed: plugin=%s session=%s",
                    plugin_id,
                    session_key,
                    exc_info=True,
                )
                return
            if not accepted:
                logger.warning(
                    "Plugin message injection was not routed: plugin=%s session=%s",
                    plugin_id,
                    session_key,
                )

        future.add_done_callback(_log_result)
        return True

    async def _dispatch_plugin_message_injection(
        self,
        *,
        session_key: str,
        content: str,
        plugin_id: str,
    ) -> bool:
        """Route a plugin-triggered turn through the session's live adapter."""
        from gateway.run import logger

        if not getattr(self, "_running", False) or getattr(self, "_draining", False):
            return False

        entry = await self.async_session_store.lookup_by_session_key(session_key)
        if entry is None or entry.origin is None:
            return False
        if not getattr(self, "_running", False) or getattr(self, "_draining", False):
            return False

        from gateway.session_identity import replace_source
        source = replace_source(self._restored_source(entry))
        try:
            if not self._is_user_authorized(
                source,
                allow_adapter_delegation=False,
            ):
                logger.warning(
                    "Plugin message injection denied by current gateway authorization: "
                    "plugin=%s session=%s",
                    plugin_id,
                    session_key,
                )
                return False
        except Exception:
            logger.warning(
                "Plugin message injection authorization check failed: "
                "plugin=%s session=%s",
                plugin_id,
                session_key,
                exc_info=True,
            )
            return False

        adapter = self._adapter_for_source(source)
        if adapter is None:
            return False

        event = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
            allow_gateway_control=False,
            metadata={
                "hermes_plugin_id": plugin_id,
                "hermes_plugin_injection": True,
                "gateway_session_key": session_key,
                "gateway_session_id": entry.session_id,
                "gateway_session_strict": True,
            },
        )
        await adapter.handle_message(event)
        logger.info(
            "Plugin message injection dispatched: plugin=%s session=%s session_id=%s",
            plugin_id,
            session_key,
            entry.session_id,
        )
        return True

    def _decide_image_input_mode(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Resolve image-input routing for the effective model this turn.

        Returns ``"native"`` (attach pixels on the user turn) or ``"text"``
        (pre-analyze with vision_analyze and prepend the description). See
        agent/image_routing.py for the full decision table.

        Gateway sessions can have /model overrides that live outside
        config.yaml. Image preprocessing runs before AIAgent sets the
        auxiliary_client runtime globals, so resolve the same per-session
        runtime bundle the upcoming agent turn will use instead of consulting
        only the persisted default model.
        """
        from gateway.run import logger

        try:
            from agent.image_routing import decide_image_input_mode
            from agent.auxiliary_client import _read_main_model, _read_main_provider
            from hermes_cli.config import load_config

            cfg = user_config if isinstance(user_config, dict) else load_config()
            resolved_provider = (provider or "").strip()
            resolved_model = (model or "").strip()
            resolved_requested_provider = ""

            needs_session_runtime = not resolved_provider or not resolved_model
            has_session_identity = source is not None or session_key
            if needs_session_runtime and has_session_identity:
                try:
                    turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                        source=source,
                        session_key=session_key,
                        user_config=cfg,
                    )
                    if not resolved_model and isinstance(turn_model, str):
                        resolved_model = turn_model.strip()
                    runtime_provider = runtime_kwargs.get("provider") if isinstance(runtime_kwargs, dict) else None
                    runtime_requested_provider = (
                        runtime_kwargs.get("requested_provider")
                        if isinstance(runtime_kwargs, dict)
                        else None
                    )
                    if not resolved_provider and isinstance(runtime_provider, str):
                        resolved_provider = runtime_provider.strip()
                    if isinstance(runtime_requested_provider, str):
                        resolved_requested_provider = runtime_requested_provider.strip()
                except Exception as exc:
                    logger.debug(
                        "image_routing: session runtime resolution failed, falling back to config — %s",
                        exc,
                    )

            if not resolved_provider:
                resolved_provider = _read_main_provider()
            if not resolved_model:
                resolved_model = _read_main_model()

            return decide_image_input_mode(
                resolved_provider,
                resolved_model,
                cfg,
                requested_provider=resolved_requested_provider,
            )
        except Exception as exc:
            logger.debug("image_routing: decision failed, falling back to text — %s", exc)
            return "text"

    async def _enrich_message_with_vision(
        self,
        user_text: str,
        image_paths: List[str],
    ) -> str:
        """
        Auto-analyze user-attached images with the vision tool and prepend
        the descriptions to the message text.

        Each image is analyzed with a general-purpose prompt.  The resulting
        description *and* the local cache path are injected so the model can:
          1. Immediately understand what the user sent (no extra tool call).
          2. Re-examine the image with vision_analyze if it needs more detail.

        Args:
            user_text:   The user's original caption / message text.
            image_paths: List of local file paths to cached images.

        Returns:
            The enriched message string with vision descriptions prepended.
        """
        from gateway.run import logger

        from tools.vision_tools import vision_analyze_tool
        from agent.memory_manager import sanitize_context

        analysis_prompt = (
            "Concisely describe this image in 2-4 sentences "
            "(~200 Chinese characters or ~150 English words). "
            "Cover the main subject, key visible text/data/code, and overall context. "
            "If it is a chart, diagram, or scientific figure, include the important "
            "labels, legend, and key values. Skip decorative details."
        )

        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug("Auto-analyzing user image: %s", path)
                result_json = await vision_analyze_tool(
                    image_url=path,
                    user_prompt=analysis_prompt,
                )
                result = json.loads(result_json)
                if result.get("success"):
                    description = result.get("analysis", "")
                    description = sanitize_context(description)
                    enriched_parts.append(
                        f"[The user sent an image~ Here's what I can see:\n{description}]\n"
                        f"[If you need a closer look, use vision_analyze with "
                        f"image_url: {path} ~]"
                    )
                else:
                    enriched_parts.append(
                        "[The user sent an image but I couldn't quite see it "
                        "this time (>_<) You can try looking at it yourself "
                        f"with vision_analyze using image_url: {path}]"
                    )
            except Exception as e:
                logger.error("Vision auto-analysis error: %s", e)
                enriched_parts.append(
                    f"[The user sent an image but something went wrong when I "
                    f"tried to look at it~ You can try examining it yourself "
                    f"with vision_analyze using image_url: {path}]"
                )

        # Combine: vision descriptions first, then the user's original text
        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            if user_text:
                return f"{prefix}\n\n{user_text}"
            return prefix
        return user_text

    _EMPTY_TEXT_PLACEHOLDER = "(The user sent a message with no text content)"

    @classmethod
    def _prepend_media_prefix(cls, prefix: str, user_text: str) -> str:
        """``prefix`` + the user's text; the Discord empty-content placeholder is dropped as redundant."""
        if user_text and user_text.strip() != cls._EMPTY_TEXT_PLACEHOLDER:
            return f"{prefix}\n\n{user_text}"
        return prefix

    @staticmethod
    def _untranscribed_audio_note(path: str) -> str:
        """One minimal neutral marker for every STT failure. Never mention "no STT provider" or setup
        steps — persisted in history they make the model keep volunteering STT-setup advice."""
        from tools.credential_files import to_agent_visible_cache_path
        agent_path = to_agent_visible_cache_path(os.path.abspath(path))
        return f"[voice message could not be transcribed automatically; the audio is available at: {agent_path}]"

    async def _transcribe_one_clip(self, path: str, transcribe_audio, transcribe_audio_local_fallback) -> Tuple[Optional[str], str]:
        """``(transcript_or_None, note)`` for one clip via configured STT with local fallback."""
        from gateway.run import logger

        result = await asyncio.to_thread(transcribe_audio, path, None, "gateway")
        if not result.get("success"):
            fallback = await asyncio.to_thread(transcribe_audio_local_fallback, path)
            if fallback.get("success"):
                logger.info("Configured STT failed for %s; recovered with local STT", path)
                result = fallback
        if not result["success"]:
            logger.info("Voice transcription failed for %s: %s", path, result.get("error", "unknown error"))
            return None, self._untranscribed_audio_note(path)
        transcript = result["transcript"]
        # STT may return success=True with an empty/whitespace transcript (silence, cut-off);
        # empty quotes make the agent reply to nothing and can loop, so emit a sentinel note.
        # See #41603.
        if not (transcript or "").strip():
            return None, (
                "[The user sent a voice message but it came through "
                "empty or inaudible — speech-to-text returned no "
                "words. Do not guess at the content; ask the user "
                "to resend or type it out.]"
            )
        # Plain quoted line: a "The user sent a voice message..." wrapper read as a meta-instruction
        # and made the LLM comment on voice mode instead.
        return transcript, f'"{transcript}"'

    async def _enrich_message_with_transcription(
        self,
        user_text: str,
        audio_paths: List[str],
    ) -> tuple[str, List[str]]:
        """
        Auto-transcribe user voice/audio messages using the configured STT provider
        and prepend the transcript to the message text.

        Args:
            user_text:   The user's original caption / message text.
            audio_paths: List of local file paths to cached audio files.

        Returns:
            A tuple of ``(enriched_text, successful_transcripts)``:
              - ``enriched_text``: the message string with transcription wrappers
                prepended (same as before).
              - ``successful_transcripts``: the raw transcript strings for audio
                clips that were successfully transcribed, in input order. Empty
                list if every clip failed or STT is disabled. Callers can use
                this to echo transcripts back to the user before the agent loop.
        """
        from gateway.run import _probe_audio_duration, logger

        seen = set()
        audio_paths = [p for p in audio_paths if p not in seen and not seen.add(p)]
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                if duration_str:
                    notes.append(
                        f"[The user sent a voice message: {abs_path} (duration: {duration_str})]"
                    )
                else:
                    notes.append(f"[The user sent a voice message: {abs_path}]")
            if not notes:
                return user_text, []
            prefix = "\n\n".join(notes)
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, []
            if user_text:
                return f"{prefix}\n\n{user_text}", []
            return prefix, []

        try:
            from tools.transcription_tools import (
                transcribe_audio,
                transcribe_audio_local_fallback,
            )
        except ModuleNotFoundError as e:
            logger.error("Transcription module unavailable: %s", e)
            unavailable_note = "[voice message could not be transcribed]"
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return unavailable_note, []
            if user_text:
                return f"{unavailable_note}\n\n{user_text}", []
            return unavailable_note, []

        enriched_parts = []
        successful_transcripts: List[str] = []
        for path in audio_paths:
            try:
                logger.debug("Transcribing user voice: %s", path)
                result = await asyncio.to_thread(
                    transcribe_audio, path, None, "gateway",
                )
                if not result.get("success"):
                    fallback = await asyncio.to_thread(
                        transcribe_audio_local_fallback,
                        path,
                    )
                    if fallback.get("success"):
                        logger.info(
                            "Configured STT failed for %s; recovered with local STT",
                            path,
                        )
                        result = fallback
                if result["success"]:
                    transcript = result["transcript"]
                    # Speech-to-text can return success=True with an empty or
                    # whitespace-only transcript on silence, cut-off, or
                    # inaudible audio. Emitting empty quotes ('""') makes the
                    # agent reply to nothing and can loop, so that case gets a
                    # clear sentinel note instead (#41603).
                    if not (transcript or "").strip():
                        enriched_parts.append(
                            "[The user sent a voice message but it came through "
                            "empty or inaudible — speech-to-text returned no "
                            "words. Do not guess at the content; ask the user "
                            "to resend or type it out.]"
                        )
                        continue
                    successful_transcripts.append(transcript)
                    # Pass the transcript through as a plain quoted line. The
                    # earlier wording ("The user sent a voice message~ Here's
                    # what they said: ...") read as a meta-instruction and made
                    # the LLM volunteer commentary about voice mode rather than
                    # reply to the content.
                    enriched_parts.append(f'"{transcript}"')
                else:
                    error = result.get("error", "unknown error")
                    # All failure branches: a single, minimal, neutral marker.
                    # Do NOT mention "no STT provider configured", "setup
                    # instructions", or the "hermes-agent-setup" skill, and do
                    # NOT claim a direct message was sent — those phrases get
                    # persisted in conversation history and poison every later
                    # turn, so the model keeps volunteering STT-setup advice
                    # even after transcription starts working. The cause is
                    # logged for operator diagnosis but kept out of the
                    # LLM-visible prompt.
                    logger.info("Voice transcription failed for %s: %s", path, error)
                    from tools.credential_files import to_agent_visible_cache_path

                    agent_path = to_agent_visible_cache_path(os.path.abspath(path))
                    enriched_parts.append(
                        "[voice message could not be transcribed automatically; "
                        f"the audio is available at: {agent_path}]"
                    )
            except Exception as e:
                logger.error("Transcription error: %s", e)
                from tools.credential_files import to_agent_visible_cache_path

                agent_path = to_agent_visible_cache_path(os.path.abspath(path))
                enriched_parts.append(
                    "[voice message could not be transcribed automatically; "
                    f"the audio is available at: {agent_path}]"
                )

        if enriched_parts:
            prefix = "\n\n".join(enriched_parts)
            # Strip the empty-content placeholder from the Discord adapter
            # when we successfully transcribed the audio — it's redundant.
            _placeholder = "(The user sent a message with no text content)"
            if user_text and user_text.strip() == _placeholder:
                return prefix, successful_transcripts
            if user_text:
                return f"{prefix}\n\n{user_text}", successful_transcripts
            return prefix, successful_transcripts
        return user_text, successful_transcripts

    def _pending_event_audio_paths(self, event) -> List[str]:
        """Return STT-eligible paths from a pending voice message."""
        from gateway.run import _event_media_is_stt_input
        audio_paths: List[str] = []
        media_urls = getattr(event, "media_urls", None) or []
        for i, path in enumerate(media_urls):
            if _event_media_is_stt_input(event, i):
                audio_paths.append(path)
        return audio_paths

    async def _transcribe_pending_audio_event_once(
        self,
        event,
        user_text: Optional[str] = None,
    ) -> tuple[str | None, List[str]]:
        """Transcribe a pending audio event once and cache the result on the event.

        Voice follow-ups can be inspected first by the interrupt monitor and
        later consumed by the pending-drain path.  Both need the same transcript,
        but only one STT call and one transcript echo should happen for the
        platform message.
        """
        if hasattr(event, "_gateway_pending_stt_text"):
            cached_text = getattr(event, "_gateway_pending_stt_text")
            cached_transcripts = getattr(event, "_gateway_pending_stt_transcripts", []) or []
            return cached_text, list(cached_transcripts)

        audio_paths = self._pending_event_audio_paths(event)
        if not audio_paths:
            return user_text if user_text is not None else (getattr(event, "text", None) or None), []

        text = user_text if user_text is not None else (getattr(event, "text", "") or "")
        enriched_text, successful_transcripts = await self._enrich_message_with_transcription(
            text,
            audio_paths,
        )
        setattr(event, "_gateway_pending_stt_text", enriched_text)
        setattr(event, "_gateway_pending_stt_transcripts", list(successful_transcripts))
        return enriched_text, successful_transcripts

    async def _echo_pending_stt_transcripts_once(
        self,
        event,
        adapter,
        source,
        transcripts: List[str],
        *,
        metadata=None,
        log_context: str = "Transcript",
    ) -> None:
        """Echo pending-event STT transcripts to the chat at most once.

        The already-echoed transcripts are tracked as a COUNT rather than a
        single boolean.  ``merge_pending_message_event`` can append a second
        voice note to an event whose first transcript was already echoed and
        invalidates the transcription cache; the re-run transcription then
        returns the earlier transcripts as a prefix of the new list, so
        echoing only the unsent tail suppresses the repeat while still
        surfacing the newly merged note.  A count rather than a set of seen
        values because two separate notes that transcribe identically are two
        distinct deliveries and both must be echoed.
        """
        from gateway.run import logger

        if (
            not transcripts
            or not self._should_echo_stt_transcripts()
            or adapter is None
        ):
            return
        already_echoed = int(getattr(event, "_gateway_pending_stt_echoed", 0) or 0)
        unsent = transcripts[already_echoed:]
        setattr(event, "_gateway_pending_stt_echoed", already_echoed + len(unsent))
        for tx in unsent:
            try:
                await adapter.send(
                    source.chat_id,
                    f'🎙️ "{tx}"',
                    metadata=metadata,
                )
            except Exception as echo_exc:
                logger.debug("%s echo failed (non-fatal): %s", log_context, echo_exc)

    async def _transcribe_and_echo_pending_voice(
        self,
        event,
        adapter,
        source,
        text: str,
        *,
        log_context: str,
        metadata=_UNSET,
    ) -> tuple[str, List[str]]:
        """Transcribe a pending voice event and echo transcripts once.

        Unified helper for all interrupt/monitor/backup/drain paths that need
        to transcribe a pending voice event and echo the transcript to chat.
        Returns ``(enriched_text, transcripts)`` so the caller can feed the
        enriched text into ``agent.interrupt()`` or the pending-drain flow.

        If the event has no STT-eligible media, returns ``(text, [])`` unchanged.
        The caller is responsible for the ``_build_media_placeholder`` fallback
        when ``text`` is empty and the event has non-audio media.
        """
        from gateway.run import _UNSET, logger

        if not self._pending_event_audio_paths(event):
            return text, []
        try:
            enriched_text, transcripts = await self._transcribe_pending_audio_event_once(
                event,
                text,
            )
            echo_meta = self._thread_metadata_for_source(
                source,
                self._reply_anchor_for_event(event),
            ) if metadata is _UNSET else metadata
            await self._echo_pending_stt_transcripts_once(
                event,
                adapter,
                source,
                transcripts,
                metadata=echo_meta,
                log_context=log_context,
            )
            return enriched_text or text, transcripts
        except Exception as trans_exc:
            logger.warning("%s transcription failed: %s", log_context, trans_exc)
            return text, []
