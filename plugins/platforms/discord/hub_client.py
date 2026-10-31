"""Hermes-side adapter for the out-of-process Discord transport hub."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from hermes_constants import get_default_hermes_root, get_hermes_home
from plugins.platforms.discord.hub import default_socket_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordHubVoiceChannel:
    """JSON-safe voice channel handle returned by the hub."""

    id: int
    name: str
    guild_id: int


class DiscordHubClientAdapter(BasePlatformAdapter):
    """Attach to Discord's shared hub; never open a token websocket locally."""

    MAX_MESSAGE_LENGTH = 2000
    _SPLIT_THRESHOLD = 1900
    MAX_SPLIT_MESSAGES = 8
    supports_code_blocks = True
    splits_long_messages = True
    _delegation_typing_enabled = True
    supports_overlap_restart = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._requests: Dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._generation_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._socket_path = default_socket_path()
        self._token_key = ""
        self.hub_pid: Optional[int] = None
        self._release_tasks: set[asyncio.Task] = set()
        self._inbound_tasks: set[asyncio.Task] = set()
        self._overlap_successor_ready = False
        self._session_versions: Dict[str, int] = {}
        self._picker_callbacks: Dict[str, Any] = {}

    def format_message(self, content: str) -> str:
        from gateway.platforms.helpers import convert_table_to_bullets
        return convert_table_to_bullets(content) if content else content

    async def _open_socket(self) -> None:
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            if not await self._start_hub():
                raise
            self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))

    async def _start_hub(self) -> bool:
        """Start the installation hub when no service manager has done so.

        Test isolation forbids detached processes just as it does for gateway
        restart helpers.  Startup failure is returned to the gateway; there is
        deliberately no in-process Discord fallback.
        """
        from gateway.restart import detached_restart_spawn_blocked

        if detached_restart_spawn_blocked():
            logger.error("Discord hub is unavailable and test isolation forbids detached startup")
            return False
        if os.environ.get("INVOCATION_ID"):
            logger.error("Discord hub is unavailable; start hermes-discord-hub.service")
            return False
        project_root = Path(__file__).resolve().parents[3]
        from tools.environments.local import served_profile_child_env

        env = served_profile_child_env(target_home=get_default_hermes_root())
        env["PYTHONPATH"] = str(project_root) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        for marker in ("_HERMES_GATEWAY", "HERMES_GATEWAY", "HERMES_GATEWAY_MODE"):
            env.pop(marker, None)
        kwargs: Dict[str, Any] = {
            "cwd": str(project_root), "env": env,
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            return False
        kwargs["start_new_session"] = True
        try:
            subprocess.Popen(
                [sys.executable, "-m", "plugins.platforms.discord.hub"], **kwargs)
        except OSError:
            logger.exception("Could not start Discord hub")
            return False
        for _ in range(50):
            if self._socket_path.exists():
                try:
                    reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
                except OSError:
                    pass
                else:
                    writer.close()
                    await writer.wait_closed()
                    return True
            await asyncio.sleep(0.1)
        logger.error("Discord hub did not create %s", self._socket_path)
        return False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._overlap_successor_ready = False
        try:
            await self._open_socket()
            self._read_task = asyncio.create_task(self._read_loop())
            from hermes_cli.config_effective import load_user_config_effective
            from gateway.platforms._shared import platform_gate_env
            profile_routes = (load_user_config_effective().get("gateway") or {}).get("profile_routes") or []
            # connect() runs inside the owning profile's runtime scope, so this is
            # the one place THIS profile's judge key can be resolved. The hub-side
            # adapter has no profile scope (and must not trust the hub process's
            # env), so the resolved value travels in the attach payload — over the
            # same-user socket only, never logged or persisted.
            gate_credential = platform_gate_env("OPENROUTER_API_KEY") or None
            result = await self._call("attach", {
                "generation_id": self._generation_id,
                "profile": self._owner_profile or "default",
                "profile_routes": profile_routes,
                "config": self.config.to_dict(),
                "gate_credential": gate_credential,
            })
            self.hub_pid = int(result["pid"])
            self._token_key = str(result.get("token_key") or "")
            self._mark_connected()
            logger.info(
                "[%s] Attached generation %s to Discord hub pid %s",
                self.name, self._generation_id, self.hub_pid,
            )
            return True
        except Exception as exc:
            self._set_fatal_error(
                "discord_hub_unavailable",
                f"Discord hub unavailable at {self._socket_path}: {exc}",
                retryable=True,
            )
            logger.error("[%s] Discord hub attach failed: %s", self.name, exc)
            await self._close_socket()
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._overlap_successor_ready = False
        if self._release_tasks:
            await asyncio.gather(*list(self._release_tasks), return_exceptions=True)
        await self._close_socket()
        logger.info("[%s] Detached from Discord hub", self.name)

    async def _close_socket(self) -> None:
        task, self._read_task = self._read_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        for future in self._requests.values():
            if not future.done():
                future.set_exception(ConnectionError("Discord hub connection closed"))
        self._requests.clear()

    async def _call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._writer is None:
            raise ConnectionError("Discord hub is not attached")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._requests[request_id] = future
        payload = {"id": request_id, "method": method, "params": params or {}}
        try:
            async with self._write_lock:
                self._writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
                await self._writer.drain()
            return await future
        finally:
            self._requests.pop(request_id, None)

    async def _read_loop(self) -> None:
        try:
            while self._reader is not None:
                line = await self._reader.readline()
                if not line:
                    break
                message = json.loads(line)
                request_id = message.get("id")
                if request_id in self._requests:
                    future = self._requests[request_id]
                    if not future.done():
                        if message.get("error"):
                            future.set_exception(RuntimeError(message["error"]))
                        else:
                            future.set_result(message.get("result"))
                elif message.get("type") == "event":
                    task = asyncio.create_task(self._receive_event(message))
                    self._inbound_tasks.add(task)
                    task.add_done_callback(self._inbound_tasks.discard)
                elif message.get("type") == "control":
                    task = asyncio.create_task(self._receive_control(message))
                    self._inbound_tasks.add(task)
                    task.add_done_callback(self._inbound_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._running:
                logger.error("[%s] Discord hub receive loop failed: %s", self.name, exc)
        finally:
            unexpected = self._running
            self._running = False
            self._overlap_successor_ready = False
            self._picker_callbacks.clear()
            if unexpected:
                self._set_fatal_error(
                    "discord_hub_disconnected", "Discord hub connection closed", retryable=True)
                try:
                    await self._notify_fatal_error()
                except Exception:
                    logger.exception("[%s] Could not report Discord hub disconnect", self.name)

    async def _receive_event(self, message: Dict[str, Any]) -> None:
        from gateway.run_drain_queue import deserialize_drain_event

        try:
            record = message["record"]
            session_key = str(message.get("session_key") or record.get("session_key") or "")
            version = record.get("_discord_hub_version")
            if session_key and version is not None:
                self._session_versions[session_key] = int(version)
            session_key, event = deserialize_drain_event(record)
            event.raw_message = {
                "discord_hub_message_id": event.message_id,
                "discord_hub_token": self._token_key,
            }
            await self.handle_message(event)
        except Exception:
            logger.exception("[%s] Could not admit Discord hub event", self.name)

    async def _receive_control(self, message: Dict[str, Any]) -> None:
        try:
            method = message.get("method")
            params = message.get("params") or {}
            if method == "accept_restart_wind_down_opt_in":
                accept = getattr(self.gateway_runner, "accept_restart_wind_down_opt_in", None)
                if not callable(accept):
                    raise RuntimeError("gateway runner is not ready for Discord control events")
                result = await accept(**params)
            elif method == "invoke_picker_callback":
                picker_id = str(params.get("picker_id") or "")
                callback = self._picker_callbacks.pop(picker_id, None)
                if not callable(callback):
                    raise RuntimeError("Discord picker is no longer available")
                result = callback(*(params.get("args") or []))
                if inspect.isawaitable(result):
                    result = await result
            else:
                raise ValueError("unknown Discord hub control method")
            response = {"type": "control_result", "id": message.get("id"), "result": result}
        except Exception as exc:
            response = {"type": "control_result", "id": message.get("id"), "error": str(exc)}
        if self._writer is not None:
            async with self._write_lock:
                self._writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
                await self._writer.drain()

    def _cleanup_finished_session_task(self, session_key: str, interrupt_event) -> None:
        super()._cleanup_finished_session_task(session_key, interrupt_event)
        if (
            session_key not in self._active_sessions
            and session_key not in self._pending_messages
            and session_key not in self._session_tasks
        ):
            task = asyncio.create_task(self._release_ownership(session_key))
            self._release_tasks.add(task)
            task.add_done_callback(self._release_tasks.discard)

    async def _release_ownership(self, session_key: str) -> None:
        try:
            released = await self._call("unregister", {
                "session_key": session_key,
                "version": self._session_versions.get(session_key, 0),
            })
            if released:
                self._session_versions.pop(session_key, None)
        except (ConnectionError, RuntimeError) as exc:
            logger.debug("[%s] Discord ownership release failed: %s", self.name, exc)

    def mark_overlap_successor_ready(self) -> None:
        self._overlap_successor_ready = True

    def admits_owned_work_while_draining(self, source: Any = None) -> bool:
        # The hub has already selected this generation for the event. Once the
        # successor is attached, new keys go there and only owned follow-ups
        # arrive here. Before that attach, the ordinary drain gate stays shut.
        return self._running and self._overlap_successor_ready

    async def wait_for_overlap_successor(self) -> None:
        for _ in range(300):
            status = await self._call("ping")
            current = status.get("current_generation")
            if current and current != self._generation_id:
                self.mark_overlap_successor_ready()
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Discord hub successor did not attach within 30 seconds")

    async def spawn_overlap_successor(self, *, via_service: bool = False) -> Optional[int]:
        # systemd is taught not to respawn the predecessor after a successful
        # overlap handoff. Other supervisors retain their legacy restart path.
        if via_service and not os.environ.get("INVOCATION_ID"):
            return None
        result = await self._call("spawn_gateway", {"hermes_home": str(get_hermes_home())})
        pid = int(result["pid"])
        await self.wait_for_overlap_successor()
        return pid

    async def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call("rpc", {
            "token_key": self._token_key,
            "method": method, "args": list(args), "kwargs": kwargs,
        })

    @staticmethod
    def _value(data: Any) -> Any:
        return data.get("value") if isinstance(data, dict) and set(data) == {"value"} else data

    def _sync_rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Answer a synchronous adapter query without driving the attached event loop reentrantly."""
        payload = {
            "id": uuid.uuid4().hex,
            "method": "sync_rpc",
            "params": {
                "token_key": self._token_key,
                "method": method,
                "args": list(args),
                "kwargs": kwargs,
            },
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5.0)
            connection.connect(str(self._socket_path))
            connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            with connection.makefile("rb") as reader:
                line = reader.readline()
        if not line:
            raise ConnectionError("Discord hub closed the synchronous RPC connection")
        response = json.loads(line)
        if response.get("error"):
            raise RuntimeError(response["error"])
        return response.get("result")

    def get_bot_display_name(self, *, chat_id: Optional[str] = None,
                             guild_id: Optional[str] = None) -> Optional[str]:
        try:
            return self._value(self._sync_rpc(
                "get_bot_display_name", chat_id=chat_id, guild_id=guild_id))
        except Exception:
            # BasePlatformAdapter's identity contract is deliberately non-raising.
            return None

    def resolved_allowlist_user_ids(self) -> set:
        return set(self._value(self._sync_rpc("resolved_allowlist_user_ids")) or ())

    def prefers_fresh_final_streaming(self, content: str,
                                      metadata: Optional[Dict[str, Any]] = None) -> bool:
        return bool(self._value(self._sync_rpc(
            "prefers_fresh_final_streaming", content, metadata=metadata)))

    def format_tool_preview(self, preview) -> str:
        value = self._value(self._sync_rpc("format_tool_preview", {
            "text": preview.text, "truncated": preview.truncated, "url": preview.url,
        }))
        return str(value) if value is not None else ""

    def claim_startup_resume_inbound(self, message_id: str, source) -> None:
        source_data = source.to_dict() if hasattr(source, "to_dict") else source
        self._sync_rpc("claim_startup_resume_inbound", str(message_id), source_data)

    def voice_mixer_active(self, guild_id: int) -> bool:
        return bool(self._value(self._sync_rpc("voice_mixer_active", guild_id)))

    def is_in_voice_channel(self, guild_id: int) -> bool:
        return bool(self._value(self._sync_rpc("is_in_voice_channel", guild_id)))

    def get_voice_channel_info(self, guild_id: int) -> Optional[Dict[str, Any]]:
        return self._value(self._sync_rpc("get_voice_channel_info", guild_id))

    def get_voice_channel_context(self, guild_id: int) -> str:
        return self._value(self._sync_rpc("get_voice_channel_context", guild_id)) or ""

    @staticmethod
    def _send_result(data: Dict[str, Any]) -> SendResult:
        values = dict(data)
        if "continuation_message_ids" in values:
            values["continuation_message_ids"] = tuple(values["continuation_message_ids"] or ())
        return SendResult(**values)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        try:
            return self._send_result(await self._rpc(
                "send", chat_id, content, reply_to=reply_to, metadata=metadata))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        try:
            return self._send_result(await self._rpc(
                "edit_message", chat_id, message_id, content, finalize=finalize, metadata=metadata))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return bool(self._value(await self._rpc("delete_message", chat_id, message_id)))

    async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
        await self._rpc("send_typing", chat_id, metadata=metadata)

    async def stop_typing(self, chat_id: str) -> None:
        await self._rpc("stop_typing", chat_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return dict(await self._rpc("get_chat_info", chat_id))

    async def rename_thread(self, thread_id: str, name: str, *,
                            only_if_current_name: Optional[str] = None) -> bool:
        return bool(self._value(await self._rpc(
            "rename_thread", thread_id, name, only_if_current_name=only_if_current_name)))

    async def create_handoff_thread(self, parent_chat_id: str, name: str) -> Optional[str]:
        return self._value(await self._rpc("create_handoff_thread", parent_chat_id, name))

    async def send_restart_confirmation(self, chat_id: str, prompt: str, requester_user_id: str = "",
                                        metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_restart_confirmation", chat_id, prompt,
            requester_user_id=requester_user_id, metadata=metadata))

    async def settle_restart_confirmation(self, chat_id: str, message_id: str, *, shared: bool = False,
                                          thread_id: Optional[str] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "settle_restart_confirmation", chat_id, message_id, shared=shared, thread_id=thread_id))

    async def send_restart_wind_down_offer(self, *, channel_id: str, requester_user_id: str,
                                           generation: int, nonce: Optional[str],
                                           spec: Optional[dict] = None) -> Optional[str]:
        return self._value(await self._rpc(
            "send_restart_wind_down_offer", channel_id=channel_id,
            requester_user_id=requester_user_id, generation=generation, nonce=nonce, spec=spec))

    async def finalize_restart_wind_down_offer(self, *, message_id: str, channel_id: str,
                                               spec: dict) -> bool:
        return bool(self._value(await self._rpc(
            "finalize_restart_wind_down_offer", message_id=message_id,
            channel_id=channel_id, spec=spec)))

    async def capture_restart_pending_thread_title(self, thread_id: str):
        value = self._value(await self._rpc("capture_restart_pending_thread_title", thread_id))
        if value is None:
            return None
        from plugins.platforms.discord.adapter import RestartPendingThreadTitle
        if isinstance(value, dict):
            return RestartPendingThreadTitle(**value)
        return RestartPendingThreadTitle(*value)

    async def begin_restart_pending_thread_title(self, restore) -> None:
        await self._rpc("begin_restart_pending_thread_title", restore._asdict() if restore is not None else None)

    async def end_restart_pending_thread_title(self, restore) -> None:
        await self._rpc("end_restart_pending_thread_title", restore._asdict() if restore is not None else None)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_clarify", chat_id, question, choices, clarify_id, session_key, metadata=metadata))

    async def send_tool_stage_embed(self, chat_id: str, stage: Dict[str, Any],
                                    metadata: Optional[Dict[str, Any]] = None,
                                    reply_to: Optional[str] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_tool_stage_embed", chat_id, stage, metadata=metadata, reply_to=reply_to))

    async def edit_tool_stage_embed(self, chat_id: str, message_id: str, stage: Dict[str, Any],
                                    metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "edit_tool_stage_embed", chat_id, message_id, stage, metadata=metadata))

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_slash_confirm", chat_id, title, message, session_key, confirm_id, metadata=metadata))

    async def send_update_prompt(self, chat_id: str, prompt: str, default: str = "", session_key: str = "",
                                 metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_update_prompt", chat_id, prompt, default=default,
            session_key=session_key, metadata=metadata))

    async def refresh_skill_group(self) -> tuple[int, int]:
        return tuple(self._value(await self._rpc("refresh_skill_group")) or (0, 0))

    async def cancel_background_tasks(self) -> None:
        await self._rpc("cancel_background_tasks")

    async def _send_picker(self, method: str, callback_keyword: str, callback,
                           *args: Any, **kwargs: Any) -> SendResult:
        picker_id = uuid.uuid4().hex
        self._picker_callbacks[picker_id] = callback
        kwargs[callback_keyword] = {"picker_id": picker_id}
        try:
            result = self._send_result(await self._rpc(method, *args, **kwargs))
        except Exception:
            self._picker_callbacks.pop(picker_id, None)
            raise
        if not result.success:
            self._picker_callbacks.pop(picker_id, None)
        return result

    async def send_model_picker(self, chat_id: str, providers: list, current_model: str,
                                current_provider: str, session_key: str, on_model_selected,
                                metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._send_picker(
            "send_model_picker", "on_model_selected", on_model_selected,
            chat_id, providers, current_model, current_provider, session_key, metadata=metadata)

    async def send_choice_picker(self, chat_id: str, title: str, choices: list, session_key: str,
                                 on_choice_selected, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._send_picker(
            "send_choice_picker", "on_choice_selected", on_choice_selected,
            chat_id, title, choices, session_key, metadata=metadata)

    async def play_tts(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        return self._send_result(await self._rpc("play_tts", chat_id, audio_path, **kwargs))

    async def play_ack_in_voice(self, guild_id: int, phrase: Optional[str] = None) -> bool:
        return bool(self._value(await self._rpc("play_ack_in_voice", guild_id, phrase=phrase)))

    async def get_user_voice_channel(self, guild_id: int, user_id: str) -> Optional[DiscordHubVoiceChannel]:
        value = self._value(await self._rpc("get_user_voice_channel", guild_id, user_id))
        return DiscordHubVoiceChannel(**value) if value else None

    async def join_voice_channel(self, channel, *, text_channel_id: int = None,
                                 source: dict = None) -> bool:
        if isinstance(channel, dict):
            channel_data = channel
        else:
            guild_id = getattr(channel, "guild_id", None)
            if guild_id is None:
                guild_id = channel.guild.id
            channel_data = {"id": channel.id, "name": channel.name, "guild_id": guild_id}
        return bool(self._value(await self._rpc(
            "join_voice_channel", channel_data, text_channel_id=text_channel_id, source=source)))

    async def leave_voice_channel(self, guild_id: int) -> None:
        await self._rpc("leave_voice_channel", guild_id)

    async def play_in_voice_channel(self, guild_id: int, audio_path: str) -> bool:
        return bool(self._value(await self._rpc("play_in_voice_channel", guild_id, audio_path)))

    async def send_multiple_images(self, chat_id: str, images: list, metadata: Optional[dict] = None,
                                   human_delay: float = 0.0, reply_to: Optional[str] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_multiple_images", chat_id, images,
            metadata=metadata, human_delay=human_delay, reply_to=reply_to))

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None, **kwargs) -> SendResult:
        return self._send_result(await self._rpc(
            "send_voice", chat_id, audio_path, caption=caption,
            reply_to=reply_to, metadata=metadata, **kwargs))

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_image_file", chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_image", chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_animation(self, chat_id: str, animation_url: str, caption: Optional[str] = None,
                             reply_to: Optional[str] = None, metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_animation", chat_id, animation_url, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_video", chat_id, video_path, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_document", chat_id, file_path, caption=caption, file_name=file_name,
            reply_to=reply_to, metadata=metadata))

    def _reactions_enabled(self) -> bool:
        value = self.config.extra.get("reactions", True)
        return value is not False and str(value).strip().lower() not in {"0", "false", "off", "no"}

    async def on_processing_start(self, event: MessageEvent) -> None:
        if self._reactions_enabled() and event.message_id:
            with contextlib.suppress(Exception):
                await self._call("rpc", {
                    "token_key": self._token_key, "method": "add_reaction",
                    "message_id": event.message_id, "emoji": "👀",
                })

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if not self._reactions_enabled() or not event.message_id:
            return
        with contextlib.suppress(Exception):
            await self._call("rpc", {
                "token_key": self._token_key, "method": "remove_reaction",
                "message_id": event.message_id, "emoji": "👀",
            })
            emoji = {ProcessingOutcome.SUCCESS: "✅", ProcessingOutcome.FAILURE: "❌"}.get(outcome)
            if emoji:
                await self._call("rpc", {
                    "token_key": self._token_key, "method": "add_reaction",
                    "message_id": event.message_id, "emoji": emoji,
                })
