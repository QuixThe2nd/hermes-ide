"""
Transport-agnostic WhatsApp behavior shared by the Baileys bridge adapter
and the official WhatsApp Cloud API adapter.

The mixin provides:
- Allow-list / DM / group gating
- Mention detection (explicit @-mentions + configurable regex patterns)
- Quoted-reply-to-bot detection
- Broadcast / Channel / Newsletter filtering
- WhatsApp-flavored markdown conversion
- Outgoing chunk length budgeting

It is the *behavior layer*. Transport-specific concerns (subprocess management,
HTTP webhooks, Graph API calls, media upload protocols) live in each adapter.

Mixin contract — the adapter must set these on ``self`` before any of the
mixin's methods are called (typically in ``__init__``):

    self.config        # gateway.config.PlatformConfig
    self.name          # str — adapter name (used in log lines)
    self._dm_policy             # str: "open" | "allowlist" | "disabled"
    self._allow_from            # set[str]
    self._group_policy          # str: "open" | "allowlist" | "disabled"
    self._group_allow_from      # set[str]
    self._mention_patterns      # list[re.Pattern]
    self._reply_prefix          # Optional[str]

Class attributes ``MAX_MESSAGE_LENGTH`` and ``DEFAULT_REPLY_PREFIX`` are
defined on the mixin and may be overridden per-adapter if needed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret

from gateway.platforms.base import MessageType


def _get_wsecret(name, default=None):
    """Scope-aware WHATSAPP_* read with the default-profile startup fallback.

    Secondary profiles run under ``_profile_runtime_scope`` -- the scope is
    authoritative and a scoped miss returns ``default`` (no cross-profile
    borrow). The DEFAULT profile's adapter constructs and sends *unscoped*
    under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash its WhatsApp path; there ``os.environ``
    is that profile's own value, so fall back to it. Same pattern as the
    Slack ``SLACK_APP_TOKEN`` read (#59739).
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default

logger = logging.getLogger(__name__)


class WhatsAppBehaviorMixin:
    """Shared behavior for all WhatsApp adapters (Baileys + Cloud API).

    See module docstring for the attribute contract the host adapter must
    satisfy. This mixin owns no state of its own — every value it touches
    is either a class attribute or set by the adapter's ``__init__``.
    """

    # WhatsApp message limits — practical UX limit, not protocol max.
    # WhatsApp allows ~65K but long messages are unreadable on mobile.
    MAX_MESSAGE_LENGTH: int = 4096
    supports_code_blocks = True  # WhatsApp renders fenced code blocks (monospace)

    DEFAULT_REPLY_PREFIX: str = "⚕ *Hermes Agent*\n────────────\n"

    _OUTBOUND_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u2060\u2063\ufeff]")
    _OUTBOUND_ODD_SPACE_RE = re.compile(r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")

    @classmethod
    def _sanitize_outbound_text(cls, content: str) -> str:
        """Remove invisible formatting chars that leak badly in WhatsApp.

        Some provider/gateway formatting paths can emit unicode like WORD
        JOINER (U+2060) plus NARROW NO-BREAK SPACE (U+202F). WhatsApp may
        render those as mojibake-looking prefixes (``⁠ text``) instead of
        invisible spacing. Keep normal text and emoji joiners intact, but
        strip known zero-width format chars and normalize odd unicode spaces.
        """
        if not content:
            return content
        content = cls._OUTBOUND_INVISIBLE_CHARS_RE.sub("", content)
        return cls._OUTBOUND_ODD_SPACE_RE.sub(" ", content)

    @property
    def enforces_own_access_policy(self) -> bool:
        """WhatsApp gates DM/group access at intake via dm_policy/group_policy."""
        return True

    # ------------------------------------------------------------------ config
    def _effective_reply_prefix(self) -> str:
        """Return the prefix to add to outgoing replies in self-chat mode.

        Subclasses that don't have a self-chat concept (the Cloud API
        adapter) can override this to always return ``""`` or apply a
        different policy.
        """
        whatsapp_mode = _get_wsecret("WHATSAPP_MODE", default="self-chat") or "self-chat"
        if whatsapp_mode != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = _get_wsecret("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """Reserve room for the reply prefix so the final message fits."""
        prefix_len = len(self._effective_reply_prefix())
        # Keep enough space for truncate_message's pagination indicator and
        # code-fence repair even if a user configures a very long prefix.
        return max(1024, self.MAX_MESSAGE_LENGTH - prefix_len)

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return (_get_wsecret("WHATSAPP_REQUIRE_MENTION", default="false") or "false").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }

    def _whatsapp_observe_unmentioned_group_messages(self) -> bool:
        """Return whether skipped unmentioned group messages are stored as context.

        When enabled with ``require_mention``, WhatsApp groups match the
        Telegram observe-unmentioned UX: ordinary group chatter is stored on
        the shared group session transcript, but the agent only dispatches
        when the bot is explicitly addressed (mention / reply-to-bot /
        wake-word pattern / slash command).
        """
        configured = self.config.extra.get("observe_unmentioned_group_messages")
        if configured is None:
            configured = self.config.extra.get("ingest_unmentioned_group_messages")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return (_get_wsecret("WHATSAPP_OBSERVE_UNMENTIONED_GROUP_MESSAGES", default="false") or "false").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }

    def _whatsapp_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = _get_wsecret("WHATSAPP_FREE_RESPONSE_CHATS", default="") or ""
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """Parse allow_from / group_allow_from from config or env var."""
        if raw is None:
            return set()
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _live_dm_allow_from(self) -> set[str]:
        """Allowlist currently enforced for DM intake / strict DM auth.

        Source precedence matches construction: explicit config wins over any
        env carrier. When the adapter was seeded from an env var, re-read that
        same key so pairing approve/revoke takes effect without restart
        (including an empty value while the key is still present). When the key
        is absent — sole-entry revoke calls ``remove_env_value`` — treat the
        allowlist as empty instead of falling back to the construction-time
        snapshot. Config-seeded adapters keep the in-memory snapshot, which
        pairing revoke purges in place — a lower-precedence or stale env value
        must not broaden access.
        """
        source = getattr(self, "_dm_allowlist_source", None)
        if isinstance(source, str) and source != "config":
            if source in os.environ:
                return self._coerce_allow_list(os.environ.get(source, ""))
            # Key removed (e.g. sole-entry pairing revoke) — do not revive the
            # stale construction snapshot.
            return set()
        return set(self._allow_from or ())

    # ------------------------------------------------------------------ JID helpers
    @staticmethod
    def _normalize_whatsapp_id(value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = str(value).strip()
        if ":" in normalized and "@" in normalized:
            normalized = normalized.replace(":", "@", 1)
        return normalized

    @staticmethod
    def _is_broadcast_chat(chat_id: str) -> bool:
        """True for WhatsApp pseudo-chats that aren't real conversations.

        Covers Status updates (Stories) and Channel/Newsletter broadcasts.
        These show up as inbound messages on Baileys but the agent should
        never reply — answering a Story update spams the contact's status
        feed, and Channel posts aren't addressable in the first place.
        """
        if not chat_id:
            return False
        cid = chat_id.strip().lower()
        if cid == "status@broadcast":
            return True
        # @broadcast suffix covers status@broadcast plus any future
        # broadcast-list variants. @newsletter is the Channel JID suffix.
        if cid.endswith("@broadcast") or cid.endswith("@newsletter"):
            return True
        return False

    # ------------------------------------------------------------------ gating
    def _open_dm_opted_in(self) -> bool:
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
            return True
        return (_get_wsecret("WHATSAPP_ALLOW_ALL_USERS", default="") or "").lower() in {"true", "1", "yes"}

    @staticmethod
    def _matches_whatsapp_allowlist(candidate: str, allow_from) -> bool:
        """Match a WhatsApp identifier against an allowlist across phone/LID forms.

        WhatsApp delivers inbound senders in LID form (``<id>@lid``) while
        operators usually configure allowlists with phone numbers, and vice
        versa. A raw set-membership check therefore never matches a known
        contact. Resolve both the candidate and each allowlist entry through
        the bridge's ``lid-mapping-*.json`` files (the shared
        ``gateway.whatsapp_identity`` helper that the gateway authz and
        session-key paths already use) so either configured form resolves to
        the inbound form.
        """
        if not allow_from:
            return False
        # Fast path: exact match against the raw configured value (e.g. a full
        # ``@g.us`` group JID or an entry that already matches verbatim).
        if candidate in allow_from:
            return True

        from gateway.whatsapp_identity import (
            expand_whatsapp_aliases,
            normalize_whatsapp_identifier,
        )

        candidate_aliases = expand_whatsapp_aliases(candidate)
        if not candidate_aliases:
            return False
        for entry in allow_from:
            if entry == "*":
                return True
            if normalize_whatsapp_identifier(entry) in candidate_aliases:
                return True
            # Entry may itself be an unmapped form; expand it too so a phone
            # allowlist entry resolves when the inbound sender arrived as a LID.
            if expand_whatsapp_aliases(entry) & candidate_aliases:
                return True
        return False

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Strict DM authorization — pairing does not imply access."""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(sender_id, self._live_dm_allow_from())
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Whether a DM may reach the gateway intake (pairing handshake path)."""
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(principal, self._live_dm_allow_from())
        if self._dm_policy == "pairing":
            return True
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _mission_admitted_group(self, chat_id: str) -> bool:
        """True while a goal-bound mission is active for this exact group chat.

        The missions plugin binds a group mission to the exact group chat id
        (``...@g.us``); while it is active the group is dynamically admitted
        here and by gateway authorization, regardless of the configured
        ``group_policy``. Closing the mission removes admission on the next
        message (the store is read live — no gateway restart). The plugin is
        optional: absent or erroring fails closed, i.e. the configured group
        policy applies unchanged.
        """
        try:
            from plugins.missions import find_active_group_mission
        except Exception:
            return False
        try:
            return find_active_group_mission(str(chat_id or "")) is not None
        except Exception:
            return False

    def _is_group_allowed(self, chat_id: str) -> bool:
        """Check whether a group chat should be processed."""
        # Goal-bound group missions admit their exact group chat even when
        # group_policy is "disabled" or excludes it — the mission is an
        # explicit per-chat operator instruction. Other groups keep the
        # configured policy.
        if self._mission_admitted_group(chat_id):
            return True
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return self._matches_whatsapp_allowlist(chat_id, self._group_allow_from)
        if self._group_policy == "pairing":
            return False
        if self._group_policy == "open":
            return True
        return False

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = (_get_wsecret("WHATSAPP_MENTION_PATTERNS", default="") or "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    patterns = [
                        part.strip() for part in raw.splitlines() if part.strip()
                    ]
                    if not patterns:
                        patterns = [
                            part.strip() for part in raw.split(",") if part.strip()
                        ]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] whatsapp mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[%s] Invalid WhatsApp mention pattern %r: %s",
                    self.name,
                    pattern,
                    exc,
                )
        if compiled:
            logger.info(
                "[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled)
            )
        return compiled

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        bot_ids = set()
        for candidate in data.get("botIds") or []:
            normalized = self._normalize_whatsapp_id(candidate)
            if normalized:
                bot_ids.add(normalized)
        return bot_ids

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        if not quoted_participant:
            return False
        return quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned_ids = {
            nid
            for candidate in (data.get("mentionedIds") or [])
            if (nid := self._normalize_whatsapp_id(candidate))
        }
        if mentioned_ids & bot_ids:
            return True

        body = str(data.get("body") or "")
        lower_body = body.lower()
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0].lower()
            if bare_id and (f"@{bare_id}" in lower_body or bare_id in lower_body):
                return True
        return False

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        if not self._mention_patterns:
            return False
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns)

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        if not text:
            return text
        bot_ids = self._bot_ids_from_message(data)
        cleaned = text
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(
                    rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned
                )
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        chat_id_raw = str(data.get("chatId") or "")
        # WhatsApp uses pseudo-chats for Status updates (Stories) and
        # Channel/Newsletter broadcasts. These are not real conversations
        # and the agent should never reply to them — even in self-chat mode
        # where the bridge may surface them as "fromMe" events.
        if self._is_broadcast_chat(chat_id_raw):
            return False
        is_group = data.get("isGroup", False)
        if is_group:
            chat_id = chat_id_raw
            if not self._is_group_allowed(chat_id):
                return False
            # Mission groups: every inbound message reaches the assistant —
            # the active mission is the invite, so no mention / reply-to-bot
            # requirement applies while it runs.
            if self._mission_admitted_group(chat_id):
                return True
        else:
            sender_id = str(data.get("senderId") or data.get("from") or "")
            if not self._is_dm_intake_allowed(sender_id):
                return False
            # DMs that pass the policy gate are always processed
            return True
        # Group messages: check mention / free-response settings
        chat_id = str(data.get("chatId") or "")
        if chat_id in self._whatsapp_free_response_chats():
            return True
        if not self._whatsapp_require_mention():
            return True
        body = str(data.get("body") or "").strip()
        if body.startswith("/"):
            return True
        if self._message_is_reply_to_bot(data):
            return True
        if self._message_mentions_bot(data):
            return True
        return self._message_matches_mention_patterns(data)

    # ------------------------------------------------------------------ observe-unmentioned
    def _should_observe_unmentioned_group_message(self, data: Dict[str, Any]) -> bool:
        """Return True when a group message should be stored but not dispatched.

        Mirrors Telegram's observe-unmentioned gate: only messages that the
        ``require_mention`` gate is about to DROP are observable. Anything the
        gate would dispatch (mention, reply-to-bot, wake-word pattern, slash
        command) belongs to the normal dispatcher, and DMs / broadcasts /
        non-allowlisted groups are dropped exactly as before.
        """
        if not self._whatsapp_observe_unmentioned_group_messages():
            return False
        chat_id = str(data.get("chatId") or "")
        if self._is_broadcast_chat(chat_id):
            return False
        if not data.get("isGroup", False):
            return False
        # Observed context is shared at group scope, so only operator-admitted
        # groups qualify (same gate as the dispatcher). Unrelated groups keep
        # being dropped silently.
        if not self._is_group_allowed(chat_id):
            return False
        # Mission-admitted groups process every message as a request already.
        if self._mission_admitted_group(chat_id):
            return False
        if chat_id in self._whatsapp_free_response_chats():
            return False
        # With require_mention off every group message is a request, so there
        # is nothing to observe.
        if not self._whatsapp_require_mention():
            return False
        # Anything the dispatcher would accept is a real addressed request.
        return not self._should_process_message(data)

    _OBSERVED_MEDIA_LABELS: tuple[tuple[str, str], ...] = (
        ("location", "[location]"),
        ("sticker", "[sticker]"),
        ("image", "[photo]"),
        ("gif", "[photo]"),
        ("video", "[video]"),
        ("ptt", "[voice message]"),
        ("audio", "[audio]"),
        ("poll", "[poll]"),
        ("contact", "[contact]"),
        ("document", "[document]"),
    )

    def _whatsapp_group_observe_media_label(self, data: Dict[str, Any]) -> str:
        """Short placeholder for a caption-less observed media message."""
        media_type = str(data.get("mediaType") or "").strip().lower()
        for needle, label in self._OBSERVED_MEDIA_LABELS:
            if needle in media_type:
                return label
        if data.get("hasMedia"):
            return "[media]"
        return "[message]"

    def _whatsapp_group_observe_shared_source(self, source):
        """Return a group-scoped source for observed WhatsApp group context.

        Dropping the per-sender ids keys every participant's chatter into ONE
        shared group session (``build_session_key`` falls back to chat scope
        when ``user_id`` is None), so a later trigger from any member sees the
        same observed history.
        """
        return dataclasses.replace(source, user_id=None, user_name=None, user_id_alt=None)

    def _whatsapp_group_observe_attributed_text(
        self, sender_id: Optional[str], sender_name: Optional[str], text: Optional[str]
    ) -> str:
        """Render an observed group message with sender attribution."""
        sender_key = str(sender_id) if sender_id else "unknown"
        sender = sender_name or sender_key
        return f"[{sender}|{sender_key}]\n{text or ''}"

    def _whatsapp_group_observe_channel_prompt(self) -> str:
        return (
            "You are handling a WhatsApp group chat message.\n"
            "- observed WhatsApp group context may be provided in a separate context-only block "
            "before the current message; it is not necessarily addressed to you.\n"
            "- Treat only the current new message as a request explicitly directed at you, "
            "and use observed context only when the current message asks for it."
        )

    def _observe_unmentioned_group_message(self, data: Dict[str, Any]) -> None:
        """Append skipped group chatter to the shared session without dispatching."""
        store = getattr(self, "_session_store", None)
        if not store:
            return
        try:
            chat_id = str(data.get("chatId") or "")
            source = self.build_source(
                chat_id=chat_id,
                chat_name=data.get("chatName"),
                chat_type="group",
                user_id=data.get("senderId"),
                user_name=data.get("senderName"),
            )
            shared_source = self._whatsapp_group_observe_shared_source(source)
            session_entry = store.get_or_create_session(shared_source)
            body = str(data.get("body") or "").strip()
            if not body:
                # Caption-less media still carries meaning in a group thread;
                # store a short label so the chatter is not silently lost.
                body = self._whatsapp_group_observe_media_label(data)
            entry = {
                "role": "user",
                "content": self._whatsapp_group_observe_attributed_text(
                    data.get("senderId"), data.get("senderName"), body
                ),
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            message_id = data.get("messageId")
            if message_id:
                entry["message_id"] = str(message_id)
            store.append_to_transcript(session_entry.session_id, entry)
            adapter_name = getattr(self, "name", "whatsapp")
            logger.info(
                "[%s] WhatsApp group message observed (no bot trigger): chat=%s from=%s",
                adapter_name,
                chat_id or "unknown",
                data.get("senderId") or "unknown",
            )
        except Exception as exc:
            adapter_name = getattr(self, "name", "whatsapp")
            logger.warning("[%s] Failed to observe WhatsApp group message: %s", adapter_name, exc)

    def _apply_whatsapp_group_observe_attribution(self, event) -> "MessageEvent":
        """Align triggered group turns with observed-history attribution."""
        if not self._whatsapp_observe_unmentioned_group_messages():
            return event
        raw_message = getattr(event, "raw_message", None)
        if not isinstance(raw_message, dict) or not raw_message.get("isGroup", False):
            return event
        chat_id = str(raw_message.get("chatId") or "")
        if not chat_id or not self._is_group_allowed(chat_id):
            return event
        shared_source = self._whatsapp_group_observe_shared_source(event.source)
        observe_prompt = self._whatsapp_group_observe_channel_prompt()
        channel_prompt = (
            f"{event.channel_prompt}\n\n{observe_prompt}" if event.channel_prompt else observe_prompt
        )
        if (event.text or "").lstrip().startswith("/") or getattr(event, "message_type", None) is MessageType.COMMAND:
            # Slash commands must retain the original source (with user_id) so
            # slash-access control (_check_slash_access / policy_for_source)
            # can identify the sender — a shared user_id=None source is never
            # an admin. Still inject the channel prompt for group context.
            # (Same contract as Telegram's COMMAND branch, #67816.)
            return dataclasses.replace(event, channel_prompt=channel_prompt)
        return dataclasses.replace(
            event,
            text=self._whatsapp_group_observe_attributed_text(
                event.source.user_id, event.source.user_name, event.text
            ),
            source=shared_source,
            channel_prompt=channel_prompt,
        )

    # ------------------------------------------------------------------ formatting
    def format_message(self, content: str) -> str:
        """Convert standard markdown to WhatsApp-compatible formatting.

        WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```code```,
        and monospaced `inline`. Standard markdown uses different syntax
        for bold/italic/strikethrough, so we convert here.

        Code blocks (``` fenced) and inline code (`) are protected from
        conversion via placeholder substitution.
        """
        if not content:
            return content

        content = self._sanitize_outbound_text(content)

        # --- 1. Protect fenced code blocks from formatting changes ---
        _FENCE_PH = "\x00FENCE"
        fences: list[str] = []

        def _save_fence(m: re.Match) -> str:
            fences.append(m.group(0))
            return f"{_FENCE_PH}{len(fences) - 1}\x00"

        result = re.sub(r"```[\s\S]*?```", _save_fence, content)

        # --- 2. Protect inline code ---
        _CODE_PH = "\x00CODE"
        codes: list[str] = []

        def _save_code(m: re.Match) -> str:
            codes.append(m.group(0))
            return f"{_CODE_PH}{len(codes) - 1}\x00"

        result = re.sub(r"`[^`\n]+`", _save_code, result)

        # --- 3. Convert markdown formatting to WhatsApp syntax ---
        # Italic: standard Markdown *text* → WhatsApp _text_.  Do this before
        # bold conversion so **bold** does not become italic by accident.  The
        # lookarounds avoid list bullets and bold delimiters.
        result = re.sub(
            r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)",
            r"_\1_",
            result,
        )
        # Bold: **text** or __text__ → *text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        # Strikethrough: ~~text~~ → ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        # _text_ is already WhatsApp italic — leave as-is

        # --- 4. Convert markdown headers to bold text ---
        # # Header → *Header*. Strip any *...* wrapping already produced
        # by step 3 (e.g. "# **Title**" → "*Title*", not "**Title**",
        # which WhatsApp renders with literal asterisks).
        def _header_to_bold(m: re.Match) -> str:
            inner = m.group(1).strip()
            while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
                inner = inner[1:-1].strip()
            return f"*{inner}*"

        result = re.sub(
            r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE
        )

        # --- 5. Convert markdown links: [text](url) → text (url) ---
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

        # --- 6. Restore protected sections ---
        for i, fence in enumerate(fences):
            result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
        for i, code in enumerate(codes):
            result = result.replace(f"{_CODE_PH}{i}\x00", code)

        return result


# ---------------------------------------------------------------------------
# Shared bridge directory resolution for CLI and adapter
# ---------------------------------------------------------------------------

def resolve_whatsapp_bridge_dir() -> Path:
    """Resolve the WhatsApp bridge directory, mirroring to HERMES_HOME if needed.

    When the install tree is read-only (e.g., Docker /opt/hermes), this function
    mirrors the bridge source to a writable HERMES_HOME location and returns that
    path. This ensures npm install works in Docker environments.

    Returns the resolved bridge directory path.
    """
    import shutil
    from pathlib import Path as _Path

    # Default location in install tree (may be read-only)
    from hermes_constants import get_hermes_home
    install_bridge = _Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"

    # Try HERMES_HOME location first
    hermes_home = get_hermes_home()
    hermes_home_bridge = hermes_home / "scripts" / "whatsapp-bridge"

    # Check if install dir is writable
    try:
        test_file = install_bridge / ".write_test"
        test_file.touch()
        test_file.unlink()
        install_writable = True
    except (OSError, PermissionError):
        install_writable = False

    if install_writable:
        return install_bridge

    # Install dir is read-only, mirror to HERMES_HOME if needed
    if hermes_home_bridge.exists():
        return hermes_home_bridge

    # Mirror the bridge source to HERMES_HOME
    try:
        hermes_home_bridge.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            install_bridge,
            hermes_home_bridge,
            dirs_exist_ok=False,
        )
        return hermes_home_bridge
    except Exception:
        return install_bridge


def whatsapp_session_is_paired(session_path) -> bool:
    """Return True when Baileys creds.json is a finished pairing.

    A leftover creds.json from an aborted or logged-out session can still
    exist with ``registered: false`` and a ``pairingCode``. Treating file
    presence as connected made the dashboard report the account as logged
    in while the live gateway was logged out.
    """
    from pathlib import Path as _Path

    creds_path = _Path(session_path) / "creds.json"
    if not creds_path.exists():
        return False
    try:
        payload = json.loads(creds_path.read_text(encoding="utf-8"))
    except OSError:
        # Presence was the old signal. Callers (and tests) that stub
        # Path.exists without a readable file should still proceed.
        return True
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("registered") is False:
        return False
    pairing_code = payload.get("pairingCode")
    if pairing_code and payload.get("registered") is not True:
        return False
    return True
