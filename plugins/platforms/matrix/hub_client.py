"""Gateway-generation proxy for the hub-owned Matrix transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.overlap_restart import overlap_attachment
from gateway.platforms.base import SendResult
from hermes_constants import get_hermes_home
from plugins.platforms.discord.hub import default_socket_path
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter
from plugins.platforms.matrix.adapter import MatrixAdapter

logger = logging.getLogger(__name__)

_OVERLAP_SUCCESSOR_ATTACH_TIMEOUT_SECONDS = 300.0
_OVERLAP_SUCCESSOR_ATTACH_POLL_SECONDS = 0.5
_MATRIX_SCOPE_KEYS = (
    "MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID", "MATRIX_PASSWORD",
    "MATRIX_DEVICE_ID", "MATRIX_E2EE_MODE", "MATRIX_ENCRYPTION",
    "MATRIX_RECOVERY_KEY", "MATRIX_RECOVERY_KEY_OUTPUT_FILE", "MATRIX_PROXY",
    "MATRIX_MAX_MESSAGE_LENGTH", "MATRIX_ROOM_IDENTITY_TTL_SECONDS",
    "MATRIX_ALLOW_ROOM_MENTIONS", "MATRIX_AUTO_THREAD", "MATRIX_DM_AUTO_THREAD",
    "MATRIX_DM_MENTION_THREADS", "MATRIX_SESSION_SCOPE", "MATRIX_PROCESS_NOTICES",
    "MATRIX_REACTIONS", "MATRIX_ALLOWED_USERS", "MATRIX_ALLOWED_ROOMS",
    "MATRIX_FREE_RESPONSE_ROOMS", "MATRIX_IGNORE_USER_PATTERNS",
    "MATRIX_REQUIRE_MENTION", "MATRIX_THREAD_REQUIRE_MENTION",
    "MATRIX_MAX_MEDIA_BYTES", "MATRIX_APPROVAL_REQUIRE_SENDER",
    "MATRIX_APPROVAL_TIMEOUT_SECONDS", "MATRIX_ALLOW_PUBLIC_ROOMS",
    "MATRIX_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS",
)


class MatrixHubClientAdapter(MatrixAdapter):
    """Keep prompt/session state locally; never open Matrix sync or crypto locally."""

    supports_overlap_restart = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
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

    async def _open_socket(self) -> None:
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            if not await self._start_hub():
                raise
            self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))

    async def _start_hub(self) -> bool:
        # The existing hub process and its supervisor own both transports.
        return await DiscordHubClientAdapter._start_hub(self)

    @staticmethod
    def _attach_secret_scope() -> Dict[str, str]:
        from agent.secret_scope import build_profile_secret_scope, get_secret

        secrets = build_profile_secret_scope(get_hermes_home())
        # Explicit empty values are meaningful: they prevent a hub-side scoped
        # miss from borrowing the hub process's default-profile environment.
        for name in _MATRIX_SCOPE_KEYS:
            secrets[name] = str(get_secret(name, "") or "")
        return secrets

    def _resolved_hub_config(self) -> Dict[str, Any]:
        data = self.config.to_dict()
        extra = dict(data.get("extra") or {})
        extra.update({
            "homeserver": self._homeserver,
            "user_id": self._user_id,
            "password": self._password,
            "device_id": self._device_id,
            "e2ee_mode": self._e2ee_mode,
        })
        data["extra"] = extra
        data["token"] = self._access_token
        return data

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._overlap_successor_ready = False
        if self._writer is not None:
            await self._close_socket()
        try:
            await self._open_socket()
            self._read_task = asyncio.create_task(self._read_loop())
            from hermes_cli.config_effective import load_user_config_effective

            profile_routes = (load_user_config_effective().get("gateway") or {}).get("profile_routes") or []
            result = await self._call("attach", {
                "platform": "matrix",
                "generation_id": self._generation_id,
                "profile": self._owner_profile or "default",
                "profile_routes": profile_routes,
                "config": self._resolved_hub_config(),
                "hermes_home": str(get_hermes_home()),
                "secret_scope": self._attach_secret_scope(),
                **overlap_attachment(),
            })
            self.hub_pid = int(result["pid"])
            self._token_key = str(result.get("token_key") or "")
            prompt_event_ids = set(self._approval_prompts_by_event)
            prompt_event_ids.update(self._model_picker_prompts_by_event)
            prompt_event_ids.update(self._choice_picker_prompts_by_event)
            for event_id in prompt_event_ids:
                await self._call("register_matrix_prompt", {"event_id": event_id})
            if self._read_task is None or self._read_task.done():
                raise ConnectionError("Matrix hub connection closed during attach")
            self._client = object()  # public prompt methods require a connected sentinel
            self._closing = False
            self._mark_connected()
            logger.info(
                "[%s] Attached generation %s to Matrix hub pid %s",
                self.name, self._generation_id, self.hub_pid,
            )
            return True
        except Exception as exc:
            self._client = None
            self._set_fatal_error(
                "matrix_hub_unavailable",
                f"Matrix hub unavailable at {self._socket_path}: {exc}",
                retryable=True,
            )
            logger.error("[%s] Matrix hub attach failed: %s", self.name, exc)
            await self._close_socket()
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._overlap_successor_ready = False
        if self._release_tasks:
            await asyncio.gather(*list(self._release_tasks), return_exceptions=True)
        await self._close_socket()
        await MatrixAdapter.disconnect(self)
        logger.info("[%s] Detached from Matrix hub", self.name)

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
                future.set_exception(ConnectionError("Matrix hub connection closed"))
        self._requests.clear()

    async def _call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._writer is None:
            raise ConnectionError("Matrix hub is not attached")
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
                logger.error("[%s] Matrix hub receive loop failed: %s", self.name, exc)
        finally:
            unexpected = self._running
            self._running = False
            self._overlap_successor_ready = False
            self._client = None
            if unexpected:
                self._set_fatal_error(
                    "matrix_hub_disconnected", "Matrix hub connection closed", retryable=True)
                try:
                    await self._notify_fatal_error()
                except Exception:
                    logger.exception("[%s] Could not report Matrix hub disconnect", self.name)

    async def _receive_event(self, message: Dict[str, Any]) -> None:
        from gateway.run_drain_queue import deserialize_drain_event

        try:
            record = message["record"]
            session_key = str(message.get("session_key") or record.get("session_key") or "")
            version = record.get("_matrix_hub_version")
            if session_key and version is not None:
                self._session_versions[session_key] = int(version)
            _session_key, event = deserialize_drain_event(record)
            await self.handle_message(event)
        except Exception:
            logger.exception("[%s] Could not admit Matrix hub event", self.name)

    async def _receive_control(self, message: Dict[str, Any]) -> None:
        try:
            method = message.get("method")
            params = message.get("params") or {}
            if method != "matrix_reaction":
                raise ValueError("unknown Matrix hub control method")
            event = SimpleNamespace(**(params.get("event") or {}))
            await MatrixAdapter._on_reaction(self, event)
            content = getattr(event, "content", {})
            relates_to = (content.get("m.relates_to", {}) if isinstance(content, dict)
                          else getattr(content, "relates_to", {}))
            prompt_event_id = (relates_to.get("event_id", "") if isinstance(relates_to, dict)
                               else str(getattr(relates_to, "event_id", "") or ""))
            registries = (
                self._approval_prompts_by_event,
                self._model_picker_prompts_by_event,
                self._choice_picker_prompts_by_event,
            )
            response = {
                "type": "control_result", "id": message.get("id"),
                "result": {"prompt_pending": any(
                    prompt_event_id in registry for registry in registries)},
            }
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
            logger.debug("[%s] Matrix ownership release failed: %s", self.name, exc)

    def mark_overlap_successor_ready(self) -> None:
        self._overlap_successor_ready = True

    def admits_owned_work_while_draining(self, source: Any = None) -> bool:
        return self._running and self._overlap_successor_ready

    async def wait_for_overlap_successor(self) -> None:
        deadline = time.monotonic() + _OVERLAP_SUCCESSOR_ATTACH_TIMEOUT_SECONDS
        while True:
            status = await self._call("ping")
            current = status.get("current_generation")
            if current and current != self._generation_id:
                self.mark_overlap_successor_ready()
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Matrix hub successor did not attach within "
                    f"{_OVERLAP_SUCCESSOR_ATTACH_TIMEOUT_SECONDS:g} seconds"
                )
            await asyncio.sleep(min(_OVERLAP_SUCCESSOR_ATTACH_POLL_SECONDS, remaining))

    async def spawn_overlap_successor(
        self, *, via_service: bool = False, predecessor_pid: Optional[int] = None
    ) -> Optional[int]:
        if via_service and not os.environ.get("INVOCATION_ID"):
            return None
        result = await self._call("spawn_gateway", {
            "hermes_home": str(get_hermes_home()),
            "predecessor_pid": predecessor_pid or os.getpid(),
        })
        pid = int(result["pid"])
        await self.wait_for_overlap_successor()
        return pid

    async def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call("rpc", {
            "token_key": self._token_key,
            "method": method,
            "args": list(args),
            "kwargs": kwargs,
        })

    @staticmethod
    def _value(data: Any) -> Any:
        return data.get("value") if isinstance(data, dict) and set(data) == {"value"} else data

    @staticmethod
    def _send_result(data: Dict[str, Any]) -> SendResult:
        values = dict(data)
        if "continuation_message_ids" in values:
            values["continuation_message_ids"] = tuple(values["continuation_message_ids"] or ())
        return SendResult(**values)

    def get_diagnostics(self) -> Dict[str, Any]:
        if self._writer is None or not self._running:
            return MatrixAdapter.get_diagnostics(self)
        value = DiscordHubClientAdapter._sync_rpc(self, "get_diagnostics", platform="matrix")
        return dict(self._value(value) or {})

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        try:
            return self._send_result(await self._rpc(
                "send", chat_id, content, reply_to=reply_to, metadata=metadata))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def create_handoff_thread(self, parent_chat_id: str, name: str) -> Optional[str]:
        return self._value(await self._rpc("create_handoff_thread", parent_chat_id, name))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return dict(self._value(await self._rpc("get_chat_info", chat_id)) or {})

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        await self._rpc("send_typing", chat_id, metadata=metadata)

    async def stop_typing(self, chat_id: str) -> None:
        await self._rpc("stop_typing", chat_id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        return self._send_result(await self._rpc(
            "edit_message", chat_id, message_id, content, finalize=finalize))

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_image", chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_image_file", chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_multiple_images(self, chat_id: str, images: list, metadata: Optional[dict] = None,
                                   human_delay: float = 0.0) -> SendResult:
        return self._send_result(await self._rpc(
            "send_multiple_images", chat_id, images, metadata=metadata, human_delay=human_delay))

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_document", chat_id, file_path, caption=caption, file_name=file_name,
            reply_to=reply_to, metadata=metadata))

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None, **kwargs) -> SendResult:
        return self._send_result(await self._rpc(
            "send_voice", chat_id, audio_path, caption=caption,
            reply_to=reply_to, metadata=metadata, **kwargs))

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None) -> SendResult:
        return self._send_result(await self._rpc(
            "send_video", chat_id, video_path, caption=caption, reply_to=reply_to, metadata=metadata))

    async def send_read_receipt(self, room_id: str, event_id: str) -> bool:
        return bool(self._value(await self._rpc("send_read_receipt", room_id, event_id)))

    async def redact_message(self, room_id: str, event_id: str, reason: str = "") -> bool:
        return bool(self._value(await self._rpc("redact_message", room_id, event_id, reason)))

    async def create_room(self, name: str = "", topic: str = "", invite: Optional[list] = None,
                          is_direct: bool = False, preset: str = "private_chat",
                          initial_state: Optional[list[dict]] = None) -> Optional[str]:
        return self._value(await self._rpc(
            "create_room", name=name, topic=topic, invite=invite, is_direct=is_direct,
            preset=preset, initial_state=initial_state))

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        return bool(self._value(await self._rpc("invite_user", room_id, user_id)))

    async def set_presence(self, state: str = "online", status_msg: str = "") -> bool:
        return bool(self._value(await self._rpc("set_presence", state, status_msg)))

    async def _send_reaction(self, room_id: str, event_id: str, emoji: str) -> Optional[str]:
        registries = (
            self._approval_prompts_by_event,
            self._model_picker_prompts_by_event,
            self._choice_picker_prompts_by_event,
        )
        if any(event_id in registry for registry in registries):
            await self._call("register_matrix_prompt", {"event_id": event_id})
        return self._value(await self._rpc("_send_reaction", room_id, event_id, emoji))
