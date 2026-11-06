"""Voice-channel / auto-TTS methods for GatewayRunner (split out of ``gateway/run.py``; bound via
the MRO). ``gateway.run`` internals are imported lazily inside method bodies (import cycle), so
``patch("gateway.run.X")`` keeps intercepting them at call time."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import sys
import time
import weakref
from contextlib import suppress
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, cast

from gateway.config import Platform
from gateway.platforms.base import build_auto_tts_output_path
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")  # log-record parity with the origin module

# Adapter-side per-chat auto-TTS override sets (``/voice off`` vs explicit ``/voice on``/``tts``).
_OFF_SET, _ON_SET = "_auto_tts_disabled_chats", "_auto_tts_enabled_chats"
_VOICE_MODES = {"off", "voice_only", "all"}


class GatewayVoiceMixin:
    def _voice_key(
        self, platform: Platform, chat_id: str, profile: Optional[str] = None
    ) -> str:
        """Return a platform-namespaced key for voice mode state.

        Under multiplexing the key is additionally namespaced by the profile
        whose bot speaks in the chat (``<profile>:<platform>:<chat_id>``); the
        default profile keeps the historical ``<platform>:<chat_id>`` shape so
        persisted state stays valid. Two bots in one Discord channel otherwise
        share a key and one profile's ``/voice`` flips the other's (#75198).
        """
        base = f"{platform.value}:{chat_id}"
        profile = profile.strip() if isinstance(profile, str) else ""
        if not profile or profile == "default":
            return base
        return f"{profile}:{base}"

    def _voice_key_for_source(self, source: SessionSource) -> str:
        """Voice-state key for an inbound source, namespaced by its transport owner.

        Voice mode belongs to the (bot, chat) pair, so the namespace is the
        profile that OWNS the receiving adapter (``_adapter_profile_for_source``)
        — the same profile ``_sync_voice_mode_state_to_adapter`` uses on
        reconnect — not the routed runtime profile.
        """
        return self._voice_key(
            source.platform,
            source.chat_id,
            profile=self._adapter_profile_for_source(source),
        )

    def _bind_voice_input_callback(self, adapter) -> None:
        """Route voice transcripts back through the adapter that captured them."""
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = functools.partial(
                self._handle_voice_channel_input, adapter=adapter)

    def _load_voice_modes(self) -> Dict[str, str]:
        try:
            data = json.loads(self._VOICE_MODE_PATH.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        if not isinstance(data, dict):
            return {}

        valid_modes = {"off", "voice_only", "all"}
        result = {}
        for chat_id, mode in data.items():
            if mode not in valid_modes:
                continue
            key = str(chat_id)
            # Skip legacy unprefixed keys (warn and skip)
            if ":" not in key:
                logger.warning(
                    "Skipping legacy unprefixed voice mode key %r during migration. "
                    "Re-enable voice mode on that chat to rebuild the prefixed key.",
                    key,
                )
                continue
            result[key] = mode
        return result

    def _save_voice_modes(self) -> None:
        try:
            self._VOICE_MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._VOICE_MODE_PATH.write_text(
                json.dumps(self._voice_mode, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("Failed to save voice modes: %s", e)

    @staticmethod
    def _toggle_adapter_auto_tts_set(adapter, chat_id: str, on: bool, *, enable: bool) -> None:
        """Add/discard ``chat_id`` in the adapter's enabled (``enable=True``) or disabled set;
        adding also clears the other set (``/voice off`` and ``/voice on``/``tts`` override)."""
        add_to, clear_from = (_ON_SET, _OFF_SET) if enable else (_OFF_SET, _ON_SET)
        if not isinstance(target := getattr(adapter, add_to, None), set):
            return
        if not on:
            target.discard(chat_id)
            return
        target.add(chat_id)
        if isinstance(other := getattr(adapter, clear_from, None), set):
            other.discard(chat_id)

    def _set_adapter_auto_tts_disabled(self, adapter, chat_id: str, disabled: bool) -> None:
        """Update an adapter's in-memory auto-TTS suppression set if present."""
        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        if not isinstance(disabled_chats, set):
            return
        if disabled:
            disabled_chats.add(chat_id)
            # ``/voice off`` also clears any explicit enable — it's a hard override.
            enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
            if isinstance(enabled_chats, set):
                enabled_chats.discard(chat_id)
        else:
            disabled_chats.discard(chat_id)

    def _set_adapter_auto_tts_enabled(self, adapter, chat_id: str, enabled: bool) -> None:
        """Update an adapter's per-chat auto-TTS opt-in set if present.

        Used for ``/voice on``/``/voice tts`` where the user explicitly wants
        auto-TTS even when ``voice.auto_tts`` is False globally.
        """
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(enabled_chats, set):
            return
        if enabled:
            enabled_chats.add(chat_id)
            # An explicit opt-in clears any stale /voice off for this chat.
            disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
            if isinstance(disabled_chats, set):
                disabled_chats.discard(chat_id)
        else:
            enabled_chats.discard(chat_id)

    def _apply_voice_mode(self, adapter, voice_key: str, chat_id: str, mode: str) -> None:
        """Record+persist ``mode``; mirror into adapter sets (``off`` -> disabled, else enabled)."""
        self._voice_mode[voice_key] = mode
        self._save_voice_modes()
        self._toggle_adapter_auto_tts_set(adapter, chat_id, True, enable=mode != "off")

    def _sync_voice_mode_state_to_adapter(self, adapter) -> None:
        """Restore persisted /voice state into a live platform adapter.

        Populates three fields from config + ``self._voice_mode``:
          - ``_auto_tts_default``: global default from ``voice.auto_tts``
          - ``_auto_tts_enabled_chats``: chats with mode ``voice_only``/``all``
          - ``_auto_tts_disabled_chats``: chats with mode ``off``
        """
        platform = getattr(adapter, "platform", None)
        if not isinstance(platform, Platform):
            return

        disabled_chats = getattr(adapter, "_auto_tts_disabled_chats", None)
        enabled_chats = getattr(adapter, "_auto_tts_enabled_chats", None)
        if not isinstance(disabled_chats, set) and not isinstance(enabled_chats, set):
            return

        # Push the global voice.auto_tts default (config.yaml) onto the adapter.
        # Lazy import to avoid adding a module-level dep from gateway → hermes_cli.
        try:
            from hermes_cli.config import load_config as _load_full_config
            _full_cfg = _load_full_config()
            _auto_tts_default = bool(
                (_full_cfg.get("voice") or {}).get("auto_tts", False)
            )
        except Exception:
            _auto_tts_default = False
        if hasattr(adapter, "_auto_tts_default"):
            adapter._auto_tts_default = _auto_tts_default

        prefix = self._voice_key(platform, "", profile=getattr(adapter, "_owner_profile", None))
        if isinstance(disabled_chats, set):
            disabled_chats.clear()
            disabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode == "off" and key.startswith(prefix)
            )
        if isinstance(enabled_chats, set):
            enabled_chats.clear()
            enabled_chats.update(
                key[len(prefix):] for key, mode in self._voice_mode.items()
                if mode in {"voice_only", "all"} and key.startswith(prefix)
            )

    @staticmethod
    def _get_guild_id(event: MessageEvent) -> Optional[int]:
        """Extract Discord guild_id from the raw message object."""
        raw = getattr(event, "raw_message", None)
        if raw is None:
            return None
        # Slash command interaction
        if hasattr(raw, "guild_id") and raw.guild_id:
            return int(raw.guild_id)
        # Regular message
        if hasattr(raw, "guild") and raw.guild:
            return raw.guild.id
        return None

    async def _handle_voice_channel_join(self, event: MessageEvent) -> str:
        """Join the user's current Discord voice channel."""
        adapter = self._adapter_for_source(event.source)
        if not hasattr(adapter, "join_voice_channel"):
            return "Voice channels are not supported on this platform."

        guild_id = self._get_guild_id(event)
        if not guild_id:
            return "This command only works in a Discord server."

        voice_channel = await adapter.get_user_voice_channel(
            guild_id, event.source.user_id
        )
        if not voice_channel:
            return "You need to be in a voice channel first."

        # Wire callbacks BEFORE join so voice input arriving immediately
        # after connection is not lost.
        self._bind_voice_input_callback(adapter)
        voice_profile = self._adapter_profile_for_source(event.source)
        if hasattr(adapter, "_on_voice_disconnect"):
            adapter._on_voice_disconnect = functools.partial(
                self._handle_voice_timeout_cleanup, adapter=adapter
            )
        # Let the adapter's inactivity timer see the live voice-reply mode so it
        # doesn't disconnect a deliberately text-only (/voice off) session.
        if hasattr(adapter, "_voice_mode_getter"):
            adapter._voice_mode_getter = lambda chat_id: self._voice_mode.get(
                self._voice_key(Platform.DISCORD, str(chat_id), profile=voice_profile),
                "off",
            )

        try:
            success = await adapter.join_voice_channel(voice_channel)
        except Exception as e:
            logger.warning("Failed to join voice channel: %s", e)
            adapter._voice_input_callback = None
            err_lower = str(e).lower()
            if "pynacl" in err_lower or "nacl" in err_lower or "davey" in err_lower:
                return (
                    "Voice dependencies are missing (PyNaCl / davey). "
                    f"Install with: `{sys.executable} -m pip install PyNaCl`"
                )
            return f"Failed to join voice channel: {e}"

        if success:
            adapter._voice_text_channels[guild_id] = int(event.source.chat_id)
            if hasattr(adapter, "_voice_sources"):
                adapter._voice_sources[guild_id] = event.source.to_dict()
            self._voice_mode[self._voice_key_for_source(event.source)] = "all"
            self._save_voice_modes()
            self._set_adapter_auto_tts_enabled(adapter, event.source.chat_id, enabled=True)
            return (
                f"Joined voice channel **{voice_channel.name}**.\n"
                f"I'll speak my replies and listen to you. Use /voice leave to disconnect."
            )
        # Join failed — clear callback
        adapter._voice_input_callback = None
        return "Failed to join voice channel. Check bot permissions (Connect + Speak)."

    async def _handle_voice_channel_leave(self, event: MessageEvent) -> str:
        """Leave the Discord voice channel."""
        adapter = self._adapter_for_source(event.source)
        guild_id = self._get_guild_id(event)

        if not guild_id or not hasattr(adapter, "leave_voice_channel"):
            return "Not in a voice channel."

        if not hasattr(adapter, "is_in_voice_channel") or not adapter.is_in_voice_channel(guild_id):
            return "Not in a voice channel."

        try:
            await adapter.leave_voice_channel(guild_id)
        except Exception as e:
            logger.warning("Error leaving voice channel: %s", e)
        # Always clean up state even if leave raised an exception
        self._voice_mode[self._voice_key_for_source(event.source)] = "off"
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(adapter, event.source.chat_id, disabled=True)
        if hasattr(adapter, "_voice_input_callback"):
            adapter._voice_input_callback = None
        return "Left voice channel."

    def _handle_voice_timeout_cleanup(self, chat_id: str, *, adapter=None) -> None:
        """Called by the adapter when a voice channel times out.

        Cleans up runner-side voice_mode state that the adapter cannot reach.
        ``adapter`` is the Discord adapter that timed out (bound at join time);
        under multiplexing that is a specific profile's bot, not necessarily
        ``self.adapters[DISCORD]``.
        """
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        profile = getattr(adapter, "_owner_profile", None)
        self._voice_mode[self._voice_key(Platform.DISCORD, chat_id, profile=profile)] = "off"
        self._save_voice_modes()
        self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)

    def _is_duplicate_voice_transcript(self, guild_id: int, user_id: int, transcript: str) -> bool:
        """Suppress repeated STT outputs for the same recent utterance.

        Voice capture can occasionally emit the same utterance twice a few
        seconds apart, which creates a second queued agent run and overlapping
        spoken replies. Dedup exact and near-exact repeats per guild/user over a
        short window while allowing genuinely new turns through.
        """
        from difflib import SequenceMatcher

        normalized = re.sub(r"\s+", " ", transcript).strip().lower()
        normalized = re.sub(r"[^\w\s]", "", normalized)
        if not normalized:
            return False

        now = time.monotonic()
        window_seconds = 12.0
        key = (guild_id, user_id)
        recent_store = getattr(self, "_recent_voice_transcripts", None)
        if not isinstance(recent_store, dict):
            recent_store = {}
            self._recent_voice_transcripts = recent_store
        recent = [
            (ts, txt)
            for ts, txt in recent_store.get(key, [])
            if now - ts <= window_seconds
        ]

        for _, prior in recent:
            if prior == normalized:
                recent_store[key] = recent
                return True
            if len(prior) >= 16 and len(normalized) >= 16:
                if SequenceMatcher(None, prior, normalized).ratio() >= 0.95:
                    recent_store[key] = recent
                    return True

        recent.append((now, normalized))
        recent_store[key] = recent[-5:]
        return False

    @staticmethod
    def _voice_input_source(adapter, guild_id: int, user_id: int, text_ch_id) -> SessionSource:
        """Bound text channel's own source when available (voice shares the text conversation's
        session), else a synthetic one."""
        if source_data := getattr(adapter, "_voice_sources", {}).get(guild_id):
            source = SessionSource.from_dict(source_data)
            source.user_id = source.user_name = str(user_id)
        else:
            source = SessionSource(
                platform=Platform.DISCORD, chat_id=str(text_ch_id), user_id=str(user_id),
                user_name=str(user_id), chat_type="channel",
                profile=getattr(adapter, "_owner_profile", None))
        # Serialization drops transport provenance; auth must still follow the receiving bot.
        source._transport_adapter_ref = weakref.ref(adapter)
        return source

    async def _handle_voice_channel_input(
        self, guild_id: int, user_id: int, transcript: str, *, adapter=None
    ):
        """Handle transcribed voice from a user in a voice channel.

        Creates a synthetic MessageEvent and processes it through the
        adapter's full message pipeline (session, typing, agent, TTS reply).
        ``adapter`` is the Discord adapter that captured the audio (bound via
        ``_bind_voice_input_callback``); under multiplexing each profile's bot
        must dispatch through its own adapter, never the default profile's.
        """
        if adapter is None:
            adapter = self.adapters.get(Platform.DISCORD)
        if not adapter:
            return

        text_ch_id = adapter._voice_text_channels.get(guild_id)
        if not text_ch_id:
            return

        # Build source — reuse the linked text channel's metadata when available
        # so voice input shares the same session as the bound text conversation.
        source_data = getattr(adapter, "_voice_sources", {}).get(guild_id)
        if source_data:
            source = SessionSource.from_dict(source_data)
            source.user_id = str(user_id)
            source.user_name = str(user_id)
        else:
            source = SessionSource(
                platform=Platform.DISCORD,
                chat_id=str(text_ch_id),
                user_id=str(user_id),
                user_name=str(user_id),
                chat_type="channel",
                profile=getattr(adapter, "_owner_profile", None),
            )

        # Serialization drops transport provenance; auth must still follow the receiving bot.
        source._transport_adapter_ref = weakref.ref(adapter)

        # The cached source still carries the previous speaker's identity (per-sender routes,
        # #106019): drop the pin so the seam re-resolves for THIS speaker.
        from gateway.session_identity import clear_identity
        clear_identity(source)
        if self._canonicalize(source, transport_profile=getattr(adapter, "_owner_profile", None)) is None:
            logger.warning("Dropping voice input: its profile route targets an unserved profile")
            return

        # Check authorization before processing voice input
        if not self._is_user_authorized(source):
            logger.debug("Unauthorized voice input from user %d, ignoring", user_id)
            return

        if self._is_duplicate_voice_transcript(guild_id, user_id, transcript):
            logger.info(
                "Suppressing duplicate voice transcript for guild=%s user=%s: %s",
                guild_id,
                user_id,
                transcript[:100],
            )
            return

        # Show transcript in text channel (after auth, with mention sanitization)
        try:
            channel = adapter._client.get_channel(text_ch_id)
            if channel:
                safe_text = transcript[:2000].replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
                await channel.send(f"**[Voice]** <@{user_id}>: {safe_text}")
        except Exception:
            pass

        # Build a synthetic MessageEvent and feed through the normal pipeline
        # Use SimpleNamespace as raw_message so _get_guild_id() can extract
        # guild_id and _send_voice_reply() plays audio in the voice channel.
        from types import SimpleNamespace
        # Resolve the bound text channel's channel_prompt so voice input gets
        # the same per-channel context as typed messages (#50149).
        channel_prompt: Optional[str] = None
        resolver = getattr(adapter, "_resolve_channel_prompt", None)
        if callable(resolver):
            try:
                resolved = resolver(str(text_ch_id))
                channel_prompt = resolved if isinstance(resolved, str) else None
            except Exception:
                channel_prompt = None
        event = MessageEvent(
            source=source,
            text=transcript,
            message_type=MessageType.VOICE,
            raw_message=SimpleNamespace(guild_id=guild_id, guild=None),
            channel_prompt=channel_prompt,
        )

        await adapter.handle_message(event)

    def _should_send_voice_reply(
        self,
        event: MessageEvent,
        response: str,
        agent_messages: list,
        already_sent: bool = False,
    ) -> bool:
        """Decide whether the runner should send a TTS voice reply.

        Returns False when:
        - voice_mode is off for this chat
        - response is empty or an error
        - agent already called text_to_speech tool (dedup)
        - voice input and base adapter auto-TTS already handled it (skip_double)
          UNLESS streaming already consumed the response (already_sent=True),
          in which case the base adapter won't have text for auto-TTS so the
          runner must handle it.
        """
        if not response or response.startswith("Error:"):
            return False

        chat_id = event.source.chat_id
        voice_key = self._voice_key_for_source(event.source)
        voice_mode = self._voice_mode.get(voice_key)
        is_voice_input = (event.message_type == MessageType.VOICE)

        adapter = self._adapter_for_source(event.source)
        adapter_auto_tts = False
        if adapter and hasattr(adapter, "_should_auto_tts_for_chat"):
            try:
                adapter_auto_tts = bool(adapter._should_auto_tts_for_chat(chat_id))
            except Exception:
                adapter_auto_tts = False

        should = (
            (voice_mode == "all")
            or (voice_mode == "voice_only" and is_voice_input)
            # ``voice.auto_tts`` is synced into the adapter on gateway startup.
            # It is the fallback only when the chat has no explicit mode;
            # otherwise the chat-level all/voice_only/off choice takes precedence.
            or (voice_mode is None and adapter_auto_tts)
        )
        if not should:
            logger.debug(
                "Auto voice reply skipped: mode=%s adapter_auto_tts=%s chat=%s platform=%s",
                voice_mode, adapter_auto_tts, chat_id, event.source.platform.value,
            )
            return False

        # Dedup: agent already called TTS tool in THIS turn only
        last_user_idx = None
        for i, msg in enumerate(reversed(agent_messages)):
            if msg.get("role") == "user":
                last_user_idx = len(agent_messages) - 1 - i; break
        turn_messages = agent_messages[last_user_idx:] if last_user_idx is not None else agent_messages
        has_agent_tts = any(
            msg.get("role") == "assistant"
            and any(
                (tc.get("function") or {}).get("name") == "text_to_speech"
                for tc in (msg.get("tool_calls") or [])
            )
            for msg in turn_messages
        )
        if has_agent_tts:
            return False

        # Dedup: base adapter auto-TTS already handles voice input
        # (play_tts plays in VC when connected, so runner can skip).
        # When streaming already delivered the text (already_sent=True),
        # the base adapter will receive None and can't run auto-TTS,
        # so the runner must take over.
        if is_voice_input and not already_sent:
            return False

        return True

    def _should_echo_stt_transcripts(self) -> bool:
        return bool(getattr(self.config, "stt_echo_transcripts", True))

    async def _send_voice_reply(self, event: MessageEvent, text: str) -> None:
        """Generate TTS audio and send as a voice message before the text reply."""
        audio_path = None
        actual_paths: List[str] = []
        try:
            from tools.tts_tool import text_to_speech_tool, _strip_markdown_for_tts

            tts_text = _strip_markdown_for_tts(text)
            if not tts_text:
                return

            # Platform-aware output path: platforms whose native voice
            # bubbles require Ogg/Opus (OPUS_VOICE_PLATFORMS — Telegram,
            # Matrix, Feishu, WhatsApp, Signal) get an explicit .ogg path;
            # the TTS tool's central container repair guarantees real
            # Ogg/Opus bytes for every provider. Others keep MP3.
            audio_path = build_auto_tts_output_path(event.source.platform)

            result_json = await asyncio.to_thread(
                text_to_speech_tool, text=tts_text, output_path=audio_path
            )
            try:
                result = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Auto voice reply TTS returned invalid JSON: %s", result_json[:200] if result_json else result_json)
                return

            # Final delivery may be one combined file or multiple separately
            # valid files when combination is unavailable or would exceed a
            # platform limit. Preserve legacy single-file results.
            actual_paths = result.get("file_paths") or [
                result.get("file_path", audio_path)
            ]
            actual_paths = [
                str(path) for path in actual_paths
                if path and os.path.isfile(path)
            ]
            if not result.get("success") or not actual_paths:
                logger.warning("Auto voice reply TTS failed: %s", result.get("error"))
                return

            adapter = self._adapter_for_source(event.source)

            # If connected to a voice channel, play there instead of sending a file
            guild_id = self._get_guild_id(event)
            play_in_voice_channel = getattr(adapter, "play_in_voice_channel", None)
            is_in_voice_channel = getattr(adapter, "is_in_voice_channel", None)
            send_voice = getattr(adapter, "send_voice", None)
            in_voice_channel = bool(
                guild_id
                and callable(play_in_voice_channel)
                and callable(is_in_voice_channel)
                and is_in_voice_channel(guild_id)
            )
            reply_anchor = self._reply_anchor_for_event(event)
            thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
            if not in_voice_channel and callable(send_voice):
                # Mark the auto voice reply as notify-worthy.  Mirrors the
                # final-text path in gateway/platforms/base.py which sets
                # ``notify=True`` so platform adapters that gate push
                # notifications (Telegram "important" mode) deliver the
                # final voice reply as a normal notification instead of a
                # silent message.  Clone first so we don't mutate metadata
                # shared with concurrent typing-indicator state.
                if thread_meta is not None:
                    thread_meta = dict(thread_meta)
                    thread_meta["notify"] = True
                else:
                    thread_meta = {"notify": True}
            for actual_path in actual_paths:
                if in_voice_channel:
                    play_voice = cast(Callable[..., Awaitable[Any]], play_in_voice_channel)
                    await play_voice(guild_id, actual_path)
                elif callable(send_voice):
                    send_voice_call = cast(Callable[..., Awaitable[Any]], send_voice)
                    send_kwargs: Dict[str, Any] = {
                        "chat_id": event.source.chat_id,
                        "audio_path": actual_path,
                        "reply_to": reply_anchor,
                        "metadata": thread_meta,
                    }
                    await send_voice_call(**send_kwargs)
        except Exception as e:
            logger.warning("Auto voice reply failed: %s", e, exc_info=True)
        finally:
            for p in ({audio_path, *actual_paths} - {None}):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    async def _deliver_voice_reply(self, event: MessageEvent, audio_paths: List[str]) -> None:
        """Play the files in the connected voice channel, else send them as voice messages."""
        adapter = self._delivery_adapter_for(event.source)
        guild_id = self._get_guild_id(event)
        play = getattr(adapter, "play_in_voice_channel", None)
        is_in_vc = getattr(adapter, "is_in_voice_channel", None)
        if guild_id and callable(play) and callable(is_in_vc) and is_in_vc(guild_id):
            for path in audio_paths:
                await play(guild_id, path)
            return
        if not callable(send_voice := getattr(adapter, "send_voice", None)):
            return
        reply_anchor = self._reply_anchor_for_event(event)
        # notify=True mirrors the final-text path in platforms/base.py so notification-gating
        # adapters (Telegram "important" mode) deliver it. Clone: shared w/ typing-indicator state.
        thread_meta = dict(self._thread_metadata_for_source(event.source, reply_anchor) or {})
        thread_meta["notify"] = True
        for path in audio_paths:
            await send_voice(chat_id=event.source.chat_id, audio_path=path, reply_to=reply_anchor,
                             metadata=thread_meta)
