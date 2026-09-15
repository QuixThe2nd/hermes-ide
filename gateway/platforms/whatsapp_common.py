"""
Transport-agnostic WhatsApp behavior shared by the Baileys bridge adapter and the
Cloud API adapter: allow-list / DM / group gating, mention detection, quoted-reply-
to-bot detection, broadcast filtering, WhatsApp markdown conversion, chunk budgeting.

Mixin contract — the host adapter sets these on ``self`` before calling any mixin
method: ``config`` (PlatformConfig), ``name``, ``_dm_policy`` / ``_group_policy``
("open" | "allowlist" | "disabled"), ``_allow_from`` / ``_group_allow_from`` (set[str]),
``_mention_patterns`` (list[re.Pattern]), ``_reply_prefix`` (Optional[str]).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.platforms._shared import get_scoped_secret as _get_wsecret

from gateway.platforms.base import MessageType


logger = logging.getLogger(__name__)

_TRUTHY = {"true", "1", "yes", "on"}
_OPTIN_TRUTHY = {"true", "1", "yes"}


def _stash(pattern: str, text: str, tag: str) -> tuple[str, list[str]]:
    """Replace every ``pattern`` match with a ``\\x00<tag><n>\\x00`` placeholder."""
    saved: list[str] = []

    def keep(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{tag}{len(saved) - 1}\x00"

    return re.sub(pattern, keep, text), saved


def _header_to_bold(m: re.Match) -> str:
    """``# Header`` → ``*Header*``, stripping already-bolded ``*...*`` so ``# **Title**``
    doesn't render with literal asterisks."""
    inner = m.group(1).strip()
    while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
        inner = inner[1:-1].strip()
    return f"*{inner}*"


class WhatsAppBehaviorMixin:
    """Shared behavior for all WhatsApp adapters (Baileys + Cloud API); owns no state
    of its own — see the module docstring for the host adapter's attribute contract."""

    # Practical UX limit, not the ~65K protocol max (long messages are unreadable on mobile).
    MAX_MESSAGE_LENGTH: int = 4096
    supports_code_blocks = True  # WhatsApp renders fenced code blocks (monospace)

    DEFAULT_REPLY_PREFIX: str = "⚕ *Hermes Agent*\n────────────\n"

    _OUTBOUND_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u2060\u2063\ufeff]")
    _OUTBOUND_ODD_SPACE_RE = re.compile(r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")

    @classmethod
    def _sanitize_outbound_text(cls, content: str) -> str:
        """Strip zero-width format chars (WORD JOINER etc.) and normalize odd unicode
        spaces — WhatsApp renders them as mojibake prefixes. Emoji joiners are kept."""
        if not content:
            return content
        return cls._OUTBOUND_ODD_SPACE_RE.sub(" ", cls._OUTBOUND_INVISIBLE_CHARS_RE.sub("", content))

    @property
    def enforces_own_access_policy(self) -> bool:
        """WhatsApp gates DM/group access at intake via dm_policy/group_policy."""
        return True

    def _effective_reply_prefix(self) -> str:
        """Prefix for outgoing replies in self-chat mode (Cloud API overrides to ``""``)."""
        if (_get_wsecret("WHATSAPP_MODE", default="self-chat") or "self-chat") != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = _get_wsecret("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """Reserve room for the reply prefix; floor keeps space for pagination/fence repair."""
        return max(1024, self.MAX_MESSAGE_LENGTH - len(self._effective_reply_prefix()))

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is None:
            configured = _get_wsecret("WHATSAPP_REQUIRE_MENTION", default="false") or "false"
        if isinstance(configured, str):
            return configured.lower() in _TRUTHY
        return bool(configured)

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
        return self._coerce_allow_list(raw)

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """Parse allow_from / group_allow_from from config (list) or env var (CSV)."""
        if raw is None:
            return set()
        parts = raw if isinstance(raw, list) else str(raw).split(",")
        return {str(part).strip() for part in parts if str(part).strip()}

    def _select_dm_allowlist(self, extra: Dict[str, Any], env_keys, read_env) -> Any:
        """Pick the raw DM allowlist by key *presence*: ``allow_from``/``allowFrom`` in config (an
        explicit empty list stays authoritative), then the first truthy env carrier. Records the
        winning source in ``_dm_allowlist_source`` so live DM checks keep the same precedence."""
        for key in ("allow_from", "allowFrom"):
            if key in extra:
                self._dm_allowlist_source = "config"
                return extra.get(key)
        for env in env_keys:
            if read_env(env):
                self._dm_allowlist_source = env
                return read_env(env)
        self._dm_allowlist_source = None
        return None

    def _live_dm_allow_from(self) -> set[str]:
        """Allowlist currently enforced for DM intake / strict DM auth. Env-seeded adapters re-read
        the same key so pairing approve/revoke takes effect without restart; a removed key (sole-entry
        revoke) means empty, not the construction snapshot. Config-seeded adapters keep the in-memory
        set (pairing revoke purges it in place) — a stale env value must not broaden access."""
        source = getattr(self, "_dm_allowlist_source", None)
        if isinstance(source, str) and source != "config":
            return self._coerce_allow_list(os.environ[source]) if source in os.environ else set()
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
        """Status updates (Stories) and Channel/Newsletter broadcasts — never reply
        (answering a Story spams the status feed; Channel posts aren't addressable)."""
        cid = (chat_id or "").strip().lower()
        return cid == "status@broadcast" or cid.endswith(("@broadcast", "@newsletter"))

    # ------------------------------------------------------------------ gating
    def _open_dm_opted_in(self) -> bool:
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in _OPTIN_TRUTHY:
            return True
        return (_get_wsecret("WHATSAPP_ALLOW_ALL_USERS", default="") or "").lower() in _OPTIN_TRUTHY

    @staticmethod
    def _matches_whatsapp_allowlist(candidate: str, allow_from) -> bool:
        """Match a WhatsApp identifier against an allowlist across phone/LID forms. Inbound senders
        arrive as ``<id>@lid`` while allowlists hold phone numbers (or vice versa), so resolve both
        sides through the bridge's lid-mapping files via ``gateway.whatsapp_identity``."""
        if not allow_from:
            return False
        if candidate in allow_from:
            return True
        from gateway.whatsapp_identity import expand_whatsapp_aliases, normalize_whatsapp_identifier
        candidate_aliases = expand_whatsapp_aliases(candidate)
        if not candidate_aliases:
            return False
        return any(
            entry == "*"
            or normalize_whatsapp_identifier(entry) in candidate_aliases
            or expand_whatsapp_aliases(entry) & candidate_aliases
            for entry in allow_from
        )

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Strict DM authorization — pairing does not imply access."""
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(sender_id, self._live_dm_allow_from())
        return self._dm_policy == "open" and self._open_dm_opted_in()

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Whether a DM may reach the gateway intake (pairing handshake path)."""
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(principal, self._live_dm_allow_from())
        if self._dm_policy == "pairing":
            return True
        return self._dm_policy == "open" and self._open_dm_opted_in()

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
        return self._group_policy == "open"

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = (_get_wsecret("WHATSAPP_MENTION_PATTERNS", default="") or "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    # Plain text: one pattern per line, else comma-separated.
                    patterns = [p.strip() for p in raw.splitlines() if p.strip()]
                    patterns = patterns or [p.strip() for p in raw.split(",") if p.strip()]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning("[%s] whatsapp mention_patterns must be a list or string; got %s", self.name, type(patterns).__name__)
            return []
        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[%s] Invalid WhatsApp mention pattern %r: %s", self.name, pattern, exc)
        if compiled:
            logger.info("[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled))
        return compiled

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        return {nid for c in (data.get("botIds") or []) if (nid := self._normalize_whatsapp_id(c))}

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        return bool(quoted_participant) and quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned = {nid for c in (data.get("mentionedIds") or []) if (nid := self._normalize_whatsapp_id(c))}
        if mentioned & bot_ids:
            return True
        lower_body = str(data.get("body") or "").lower()
        return any(
            bare and (f"@{bare}" in lower_body or bare in lower_body)
            for bare in (bot_id.split("@", 1)[0].lower() for bot_id in bot_ids)
        )

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns or ())

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        if not text:
            return text
        cleaned = text
        for bot_id in self._bot_ids_from_message(data):
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned)
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        chat_id = str(data.get("chatId") or "")
        # Broadcast pseudo-chats are filtered even in self-chat mode (fromMe events).
        if self._is_broadcast_chat(chat_id):
            return False
        if not data.get("isGroup", False):
            # DMs that pass the policy gate are always processed
            return self._is_dm_intake_allowed(str(data.get("senderId") or data.get("from") or ""))
        if not self._is_group_allowed(chat_id):
            return False
        # Mission groups: every inbound message reaches the assistant —
        # the active mission is the invite, so no mention / reply-to-bot
        # requirement applies while it runs.
        if self._mission_admitted_group(chat_id):
            return True
        # Group messages: check mention / free-response settings
        if chat_id in self._whatsapp_free_response_chats() or not self._whatsapp_require_mention():
            return True
        return (
            str(data.get("body") or "").strip().startswith("/")
            or self._message_is_reply_to_bot(data)
            or self._message_mentions_bot(data)
            or self._message_matches_mention_patterns(data)
        )

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
        """Convert markdown to WhatsApp syntax (*bold*, _italic_, ~strike~); fenced and
        inline code are protected via placeholder substitution."""
        if not content:
            return content
        result, fences = _stash(r"```[\s\S]*?```", self._sanitize_outbound_text(content), "FENCE")
        result, codes = _stash(r"`[^`\n]+`", result, "CODE")
        # Italic *text* → _text_ BEFORE bold so **bold** doesn't become italic;
        # lookarounds skip list bullets and bold delimiters.
        result = re.sub(r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)", r"_\1_", result)
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        result = re.sub(r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE)
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)  # [text](url) → text (url)
        for tag, saved in (("FENCE", fences), ("CODE", codes)):
            for i, original in enumerate(saved):
                result = result.replace(f"\x00{tag}{i}\x00", original)
        return result


def resolve_whatsapp_bridge_dir() -> Path:
    """Bridge directory for CLI and adapter. A read-only install tree (e.g. Docker
    /opt/hermes) is mirrored to HERMES_HOME so npm install works."""
    import shutil
    from hermes_constants import get_hermes_home
    install_bridge = Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"
    hermes_home_bridge = get_hermes_home() / "scripts" / "whatsapp-bridge"
    try:
        (install_bridge / ".write_test").touch()
        (install_bridge / ".write_test").unlink()
        return install_bridge
    except OSError:
        pass
    if hermes_home_bridge.exists():
        return hermes_home_bridge
    try:
        hermes_home_bridge.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(install_bridge, hermes_home_bridge, dirs_exist_ok=False)
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
