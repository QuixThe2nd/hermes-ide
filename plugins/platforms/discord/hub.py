"""Shared Discord transport hub.

The hub is the only process that creates a discord.py client.  Hermes gateway
processes attach over a same-user Unix socket; the routing table below keeps a
busy session on its original generation while newly arriving sessions move to
the newest attached generation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from agent.async_utils import consume_detached_task_result
from hermes_constants import get_default_hermes_root
from gateway.overlap_restart import OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV

logger = logging.getLogger(__name__)

Delivery = Callable[[str, Dict[str, Any]], Awaitable[None]]


class DiscordSessionRouter:
    """Route each session to exactly one live Hermes generation.

    Ownership is claimed on first delivery, not on attach.  Attaching a new
    generation therefore changes only unowned sessions.  A disconnected owner
    is no longer live, so its next event moves to the current generation; with
    no usable generation the complete event is retained for explicit replay.
    """

    def __init__(self, *, transport: str = "Discord",
                 version_field: str = "_discord_hub_version") -> None:
        self._clients: "OrderedDict[str, Delivery]" = OrderedDict()
        self._owners: Dict[str, str] = {}
        self._versions: Dict[str, int] = {}
        self._current: Optional[str] = None
        self._undelivered: list[Dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._transport = transport
        self._version_field = version_field

    @property
    def current_generation(self) -> Optional[str]:
        return self._current

    @property
    def undelivered(self) -> list[Dict[str, Any]]:
        return list(self._undelivered)

    def owner_for(self, session_key: str) -> Optional[str]:
        return self._owners.get(session_key)

    def attach(self, generation_id: str, delivery: Delivery) -> None:
        if not generation_id or not callable(delivery):
            raise ValueError("a generation id and delivery callback are required")
        self._clients.pop(generation_id, None)
        self._clients[generation_id] = delivery
        self._current = generation_id

    def detach(self, generation_id: str) -> None:
        self._clients.pop(generation_id, None)
        if self._current == generation_id:
            self._current = next(reversed(self._clients), None) if self._clients else None

    def unregister(self, session_key: str, generation_id: str, version: Optional[int] = None) -> bool:
        if self._owners.get(session_key) != generation_id:
            return False
        if version is not None and self._versions.get(session_key) != version:
            return False
        del self._owners[session_key]
        self._versions.pop(session_key, None)
        return True

    async def route(self, session_key: str, record: Dict[str, Any]) -> Optional[str]:
        if not session_key:
            raise ValueError("session_key is required")
        async with self._lock:
            owner = self._owners.get(session_key)
            generation = owner if owner in self._clients else self._current
            if generation is None:
                self._undelivered.append(record)
                logger.warning("%s hub retained an event for %s: no Hermes client is attached", self._transport, session_key)
                return None
            try:
                version = self._versions.get(session_key, 0) + 1
                self._versions[session_key] = version
                delivered_record = dict(record)
                delivered_record[self._version_field] = version
                await self._clients[generation](session_key, delivered_record)
            except Exception:
                # Do not send a possibly-received event to a second generation.
                self._undelivered.append(record)
                logger.exception("%s hub retained an event for %s after delivery failed", self._transport, session_key)
                return None
            self._owners[session_key] = generation
            return generation

    async def replay_undelivered(self) -> int:
        """Replay retained records after an attach; leave failures queued."""
        pending, self._undelivered = self._undelivered, []
        delivered = 0
        for record in pending:
            key = str(record.get("session_key") or "")
            if not key:
                self._undelivered.append(record)
                continue
            if await self.route(key, record) is not None:
                delivered += 1
        return delivered


def default_socket_path() -> Path:
    """One installation-wide endpoint, shared by every profile and generation."""
    return get_default_hermes_root() / "gateway" / "discord-hub.sock"


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_SYNC_RPC_METHODS = frozenset({
    "get_bot_display_name",
    "resolved_allowlist_user_ids",
    "prefers_fresh_final_streaming",
    "format_tool_preview",
    "claim_startup_resume_inbound",
    "voice_mixer_active",
    "is_in_voice_channel",
    "get_voice_channel_info",
    "get_voice_channel_context",
})
_MATRIX_SYNC_RPC_METHODS = frozenset({"get_diagnostics"})
_PICKER_CALLBACK_KWARGS = {
    "send_model_picker": "on_model_selected",
    "send_choice_picker": "on_choice_selected",
}
_PROCESSING_REACTION_EMOJI = "👀"
# One gateway generation may react on many messages; cap matches the spirit of _raw_messages
# (4096) but per-generation sets are dropped wholesale on detach, so this only bounds a
# long-lived single attachment against pathological RPC volume.
_MAX_TRACKED_PROCESSING_REACTIONS = 4096
_ADAPTER_DISCONNECT_TIMEOUT_SECONDS = 5.0


def _send_result_dict(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    if result is None or isinstance(result, (bool, str, int, float)):
        return {"value": result}
    if isinstance(result, (set, frozenset)):
        return {"value": list(result)}
    if hasattr(result, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(result)
    return {"value": result}


@dataclass
class _Client:
    generation_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock
    controls: Dict[str, asyncio.Future]
    token_key: str = ""
    platform: str = "discord"
    router: Optional[DiscordSessionRouter] = None
    processing_reactions: "OrderedDict[str, None]" = field(default_factory=OrderedDict)

    async def deliver(self, session_key: str, record: Dict[str, Any]) -> None:
        await self.send({"type": "event", "session_key": session_key, "record": record})

    async def send(self, payload: Dict[str, Any]) -> None:
        async with self.write_lock:
            self.writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            await self.writer.drain()


class DiscordHubServer:
    """Host shared Discord and Matrix transports for attached gateways."""

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        from gateway.overlap_registry import OverlapSuccessorRegistry
        from plugins.platforms.matrix.hub import MatrixHub

        self.socket_path = Path(socket_path or default_socket_path())
        self.router = DiscordSessionRouter()
        self._routers: Dict[str, DiscordSessionRouter] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Dict[str, _Client] = {}
        self._adapters: Dict[str, Any] = {}
        self._adapter_tokens: Dict[str, str] = {}
        self._adapter_locks: Dict[str, asyncio.Lock] = {}
        self._adapter_profiles: Dict[str, set[str]] = {}
        self._adapter_routes: Dict[str, list] = {}
        self._raw_messages: "OrderedDict[str, Any]" = OrderedDict()
        self._prompt_owners: Dict[str, str] = {}
        self._successors = OverlapSuccessorRegistry()
        self.matrix = MatrixHub(self)
        self._stopping = False

    def _router_for(self, token_key: str) -> DiscordSessionRouter:
        router = self._routers.get(token_key)
        if router is None:
            # Keep the first router as the single-token/test convenience API.
            router = self.router if not self._routers else DiscordSessionRouter()
            self._routers[token_key] = router
        return router

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.parent.chmod(0o700)
        if self.socket_path.exists():
            # Never unlink a live hub's endpoint during a competing start.
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                self.socket_path.unlink(missing_ok=True)
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError(f"Discord hub is already listening on {self.socket_path}")
        self._server = await asyncio.start_unix_server(self._serve_client, path=str(self.socket_path))
        self.socket_path.chmod(0o600)
        logger.info("Discord hub listening on %s (pid %s)", self.socket_path, os.getpid())

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for client in list(self._clients.values()):
            client.writer.close()
        for adapter in list(self._adapters.values()):
            await adapter.disconnect()
        self._adapters.clear()
        self._adapter_tokens.clear()
        await self.matrix.stop()
        self.socket_path.unlink(missing_ok=True)

    async def _disconnect_adapter_with_deadline(self, adapter: Any, context: str) -> None:
        """Stop the event producer before detachable transport/voice cleanup."""
        # DiscordAdapter.disconnect() deliberately leaves voice before awaiting bot cancellation.
        # Recovery cannot let that earlier step strand a reconnecting bot after the deadline.
        adapter._disconnecting = True
        bot_task = getattr(adapter, "_bot_task", None)
        if bot_task is not None and not bot_task.done():
            bot_task.cancel()
        task = asyncio.ensure_future(adapter.disconnect())
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=_ADAPTER_DISCONNECT_TIMEOUT_SECONDS)
            if task in done:
                await task
            else:
                logger.warning(
                    "Discord hub %s cleanup timed out after %.1fs; continuing",
                    context, _ADAPTER_DISCONNECT_TIMEOUT_SECONDS,
                )
        finally:
            if not task.done():
                task.cancel()
                task.add_done_callback(consume_detached_task_result)
            # Scoped-lock ownership is PID-based, and all hub adapters share one PID.
            # Release and disarm the old instance before a replacement can acquire the
            # token, so detached cleanup cannot unlink that replacement's lock later.
            adapter._release_platform_lock()

    async def _attach_adapter(self, generation_id: str, config_data: Dict[str, Any], profile: str,
                              profile_routes: Optional[list] = None,
                              gate_credential: Optional[str] = None) -> None:
        from gateway.config import PlatformConfig
        from gateway.profile_routing import parse_profile_routes
        from plugins.platforms.discord.adapter import DiscordAdapter

        config = PlatformConfig.from_dict(config_data)
        if not config.token:
            raise RuntimeError("Discord hub attach has no bot token")
        key = _token_key(config.token)
        profile_name = profile or "default"
        # One token has one lifecycle: a concurrent attach waits for either the
        # live adapter or the failed attempt's bounded cleanup.
        async with self._adapter_locks.setdefault(key, asyncio.Lock()):
            if self._stopping:
                raise RuntimeError("Discord hub is stopping")
            self._adapter_profiles.setdefault(key, set()).add(profile_name)
            routes = parse_profile_routes(profile_routes)
            previous_routes = self._adapter_routes.get(key)
            if routes:
                merged = {(route.name, route.profile, route.bot_profile): route
                          for route in [*self._adapter_routes.get(key, []), *routes]}
                self._adapter_routes[key] = sorted(
                    merged.values(), key=lambda route: route.specificity, reverse=True)
            if key in self._adapters:
                return
            adapter = DiscordAdapter(config)
            adapter._owner_profile = None
            # The attaching gateway resolved the owning profile's judge credential under
            # that profile's secret scope; pin it before connect() so the hub-side
            # adapter never falls back to this hub process's (unscoped, possibly
            # default-profile) env. "" pins "no key" — fail closed, not env fallback.
            adapter._hub_gate_credential = str(gate_credential or "")
            adapter.gateway_runner = _HubProfileResolver(self, key)
            adapter.handle_message = self._make_inbound_handler(adapter, key)
            adapter._fatal_error_handler = (
                lambda failed: self._handle_adapter_fatal_error(key, failed))
            try:
                connected = await adapter.connect()
                if not connected:
                    raise RuntimeError(
                        getattr(adapter, "_fatal_error_message", None) or "Discord connection failed")
                if self._stopping:
                    raise RuntimeError("Discord hub is stopping")
                self._adapters[key] = adapter
                self._adapter_tokens[key] = config.token
            except BaseException:
                profiles = self._adapter_profiles.get(key)
                if profiles is not None:
                    profiles.discard(profile_name)
                    if not profiles:
                        self._adapter_profiles.pop(key, None)
                if previous_routes is None:
                    self._adapter_routes.pop(key, None)
                else:
                    self._adapter_routes[key] = previous_routes
                try:
                    await self._disconnect_adapter_with_deadline(adapter, "failed-attach")
                except Exception:
                    logger.exception("Discord hub failed-attach cleanup failed")
                raise

    async def _handle_adapter_fatal_error(self, token_key: str, adapter: Any) -> None:
        """Evict a failed adapter and make its gateways use their normal reconnect path."""
        if not adapter.fatal_error_retryable:
            return
        async with self._adapter_locks.setdefault(token_key, asyncio.Lock()):
            # A delayed callback belongs to the old transport, not its replacement;
            # intentional hub shutdown owns teardown and must not trigger reattachment.
            if self._stopping or self._adapters.get(token_key) is not adapter:
                return
            self._adapters.pop(token_key, None)
            self._adapter_tokens.pop(token_key, None)
            for client in list(self._clients.values()):
                if client.token_key == token_key:
                    # EOF becomes retryable discord_hub_disconnected in hub_client;
                    # its gateway queues reconnect while this token lock holds teardown.
                    client.writer.close()
            try:
                await self._disconnect_adapter_with_deadline(adapter, "fatal-adapter")
            except Exception:
                # The failed instance is already evicted, so a later attach can retry.
                logger.exception("Discord hub fatal-adapter cleanup failed")

    def _make_inbound_handler(self, adapter: Any, token_key: str):
        async def _deliver(event: Any) -> None:
            from gateway.run_drain_queue import serialize_drain_event

            # Same admission receipt as the base handle_message: True only when
            # a live gateway generation took the record (scheduled/queued).
            event._gateway_accepted = False
            key = adapter._event_session_key(event)
            raw = getattr(event, "raw_message", None)
            message_id = str(getattr(event, "message_id", "") or "")
            if raw is not None and message_id:
                raw_key = f"{token_key}:{message_id}"
                self._raw_messages.pop(raw_key, None)
                self._raw_messages[raw_key] = raw
                while len(self._raw_messages) > 4096:
                    self._raw_messages.popitem(last=False)
            record = serialize_drain_event(key, event)
            record["discord_token"] = token_key
            generation = await self._router_for(token_key).route(key, record)
            event._gateway_accepted = generation is not None
        return _deliver

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client: Optional[_Client] = None
        try:
            while not self._stopping:
                line = await reader.readline()
                if not line:
                    break
                request = json.loads(line)
                if request.get("type") == "control_result" and client is not None:
                    future = client.controls.pop(str(request.get("id") or ""), None)
                    if future is not None and not future.done():
                        if request.get("error"):
                            future.set_exception(RuntimeError(request["error"]))
                        else:
                            future.set_result(request.get("result"))
                    continue
                request_id = request.get("id")
                try:
                    method = request.get("method")
                    params = request.get("params") or {}
                    if method == "attach":
                        generation = str(params.get("generation_id") or "").strip()
                        if not generation:
                            raise ValueError("generation_id is required")
                        if generation in self._clients:
                            raise ValueError("generation_id is already attached")
                        client = _Client(generation, reader, writer, asyncio.Lock(), {})
                        config_data = params.get("config") or {}
                        platform_name = str(params.get("platform") or "discord")
                        peercred = writer.get_extra_info("peercred")
                        peer_pid = int(peercred[0]) if peercred else None
                        gateway_pid = int(params.get("gateway_pid") or 0)
                        if peer_pid is not None and gateway_pid and gateway_pid != peer_pid:
                            raise RuntimeError("attached gateway PID does not match the socket peer")
                        if platform_name == "matrix":
                            token_key, router = await self.matrix.attach(
                                config_data=config_data,
                                profile=str(params.get("profile") or "default"),
                                profile_routes=params.get("profile_routes") or [],
                                hermes_home=str(params.get("hermes_home") or ""),
                                secret_scope=params.get("secret_scope") or {},
                            )
                            client.platform = "matrix"
                        elif platform_name == "discord":
                            token_key = _token_key(str(config_data.get("token") or ""))
                            await self._attach_adapter(
                                generation, config_data, str(params.get("profile") or "default"),
                                params.get("profile_routes") or [],
                                params.get("gate_credential"))
                            router = self._router_for(token_key)
                        else:
                            raise ValueError(f"unknown hub platform: {platform_name}")
                        if params.get("overlap_predecessor_pid"):
                            self._successors.attach(
                                pid=gateway_pid,
                                start_time=int(params.get("gateway_start_time") or 0),
                                gateway_home=str(params.get("gateway_home") or ""),
                                generation_id=generation,
                                peer_pid=peer_pid,
                            )
                        client.token_key = token_key
                        client.router = router
                        self._clients[generation] = client
                        client.router.attach(generation, client.deliver)
                        await client.send({"id": request_id, "result": {
                            "generation_id": generation, "pid": os.getpid(),
                            "token_key": token_key,
                        }})
                        await client.router.replay_undelivered()
                        continue
                    if client is None and method not in {"sync_rpc", "overlap_successors"}:
                        raise RuntimeError("attach is required before hub RPC")
                    result = await self._dispatch(client, str(request.get("method") or ""), params)
                    if client is None:
                        await self._write_response(writer, {"id": request_id, "result": result})
                    else:
                        await client.send({"id": request_id, "result": result})
                except Exception as exc:
                    logger.exception("Discord hub RPC failed")
                    await self._write_response(writer, {"id": request_id, "error": str(exc)})
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            logger.exception("Discord hub client connection failed")
        finally:
            if client is not None:
                if client.platform == "matrix":
                    self.matrix.detach(client)
                else:
                    await self._reap_client_processing_reactions(client)
                self._clients.pop(client.generation_id, None)
                (client.router or self.router).detach(client.generation_id)
                for future in client.controls.values():
                    if not future.done():
                        future.cancel()
                client.controls.clear()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _write_response(writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()

    async def _dispatch(self, client: _Client, method: str, params: Dict[str, Any]) -> Any:
        if method == "overlap_successors":
            return {"successors": self._successors.snapshot()}
        if method == "register_matrix_prompt":
            if client is None or client.platform != "matrix":
                raise ValueError("an attached Matrix generation is required")
            return self.matrix.register_prompt(client, str(params.get("event_id") or ""))
        if method == "unregister":
            version = params.get("version")
            return (client.router or self.router).unregister(
                str(params["session_key"]), client.generation_id,
                int(version) if version is not None else None)
        if method == "ping":
            return {
                "pid": os.getpid(),
                "current_generation": (client.router or self.router).current_generation,
            }
        if method == "spawn_gateway":
            return await self._spawn_gateway(params)
        if method == "rpc":
            if client is not None and client.platform == "matrix":
                return await self.matrix.rpc(client, params)
            return await self._adapter_rpc(client, params)
        if method == "sync_rpc":
            requested = str(params.get("method") or "")
            if params.get("platform") == "matrix":
                if requested not in _MATRIX_SYNC_RPC_METHODS:
                    raise ValueError(f"Matrix adapter method is not synchronous: {requested}")
                return await self.matrix.rpc(client, params)
            if requested not in _SYNC_RPC_METHODS:
                raise ValueError(f"Discord adapter method is not synchronous: {requested}")
            return await self._adapter_rpc(client, params)
        raise ValueError(f"unknown hub method: {method}")

    async def _reap_client_processing_reactions(self, client: _Client) -> None:
        """Remove processing 👀 reactions owned by a detaching gateway generation."""
        if not client.processing_reactions:
            return
        adapter = self._adapters.get(client.token_key)
        hub_user = getattr(getattr(adapter, "_client", None), "user", None) if adapter else None
        raw_keys = list(client.processing_reactions)
        client.processing_reactions.clear()
        for raw_key in raw_keys:
            message = self._raw_messages.get(raw_key)
            if message is None or hub_user is None:
                continue
            with contextlib.suppress(Exception):
                await message.remove_reaction(_PROCESSING_REACTION_EMOJI, hub_user)

    async def _adapter_rpc(self, client: _Client, params: Dict[str, Any]) -> Any:
        token_key = str(params.get("token_key") or "")
        adapter = self._adapters.get(token_key)
        if adapter is None:
            raise RuntimeError("Discord hub connection is unavailable")
        method = str(params.get("method") or "")
        if method in {"add_reaction", "remove_reaction"}:
            message_id = str(params.get("message_id") or "")
            raw_key = f"{token_key}:{message_id}"
            message = self._raw_messages.get(raw_key)
            if message is None:
                return False
            emoji = params.get("emoji")
            fn = getattr(message, method)
            if method == "remove_reaction":
                result = await fn(emoji, adapter._client.user)
                if emoji == _PROCESSING_REACTION_EMOJI and result is not False:
                    client.processing_reactions.pop(raw_key, None)
            else:
                result = await fn(emoji)
                if emoji == _PROCESSING_REACTION_EMOJI and result is not False:
                    client.processing_reactions.pop(raw_key, None)
                    client.processing_reactions[raw_key] = None
                    while len(client.processing_reactions) > _MAX_TRACKED_PROCESSING_REACTIONS:
                        client.processing_reactions.popitem(last=False)
            return True if result is None else result
        if method.startswith("_") or method in {"connect", "disconnect"}:
            raise ValueError("Discord RPC method is not public")
        fn = getattr(adapter, method, None)
        if not callable(fn):
            raise ValueError(f"Discord adapter has no RPC method {method}")
        args = list(params.get("args") or [])
        kwargs = dict(params.get("kwargs") or {})
        if method == "format_tool_preview" and args and isinstance(args[0], dict):
            from agent.display import ToolPreview
            args[0] = ToolPreview(**args[0])
        elif method == "claim_startup_resume_inbound" and len(args) > 1 and isinstance(args[1], dict):
            from gateway.session import SessionSource
            args[1] = SessionSource.from_dict(args[1])
        elif method == "on_processing_complete" and len(args) == 2 and isinstance(args[0], dict):
            from gateway.platforms.event import ProcessingOutcome
            from gateway.run_drain_queue import deserialize_drain_event

            record = args[0]
            _session_key, event = deserialize_drain_event(record)
            event._gateway_replay_message_ids = [
                str(message_id) for message_id in record.get("_gateway_replay_message_ids", ())
                if str(message_id)
            ]
            event._drain_snapshot_owned = bool(record.get("_drain_snapshot_owned"))
            event._gateway_processing_deferred = bool(record.get("_gateway_processing_deferred"))
            raw_message = self._raw_messages.get(f"{token_key}:{event.message_id}")
            if raw_message is not None:
                event.raw_message = raw_message
            args[0] = event
            args[1] = ProcessingOutcome(args[1])
        elif method == "join_voice_channel" and args and isinstance(args[0], dict):
            channel_id = int(args[0]["id"])
            channel = adapter._client.get_channel(channel_id)
            if channel is None:
                channel = await adapter._client.fetch_channel(channel_id)
            if channel is None:
                raise RuntimeError(f"Discord voice channel {channel_id} is unavailable")
            args[0] = channel
        callback_keyword = _PICKER_CALLBACK_KWARGS.get(method)
        if callback_keyword is not None:
            if client is None:
                raise ValueError("Discord pickers require an attached gateway")
            marker = kwargs.get(callback_keyword) or {}
            picker_id = str(marker.get("picker_id") or "")
            if not picker_id:
                raise ValueError("Discord picker callback id is required")

            async def invoke_picker(*callback_args: Any) -> Any:
                return await self._request_generation_control(
                    client.generation_id, "invoke_picker_callback",
                    {"picker_id": picker_id, "args": list(callback_args)}, timeout=120)

            kwargs[callback_keyword] = invoke_picker
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if method == "get_user_voice_channel":
            if result is None:
                return {"value": None}
            return {"value": {
                "id": result.id, "name": result.name, "guild_id": result.guild.id,
            }}
        if method == "send_restart_wind_down_offer" and result:
            self._prompt_owners[f"{token_key}:{result}"] = client.generation_id
        elif method == "finalize_restart_wind_down_offer":
            self._prompt_owners.pop(f"{token_key}:{params.get('kwargs', {}).get('message_id')}", None)
        return _send_result_dict(result)

    async def _request_generation_control(self, generation_id: str, method: str,
                                          params: Dict[str, Any], *, timeout: float = 15) -> Any:
        client = self._clients.get(generation_id)
        if client is None:
            raise RuntimeError("Discord picker owner detached")
        return await self._request_client_control(client, method, params, timeout=timeout)

    async def _request_client_control(self, client: _Client, method: str,
                                      params: Dict[str, Any], *, timeout: float = 15) -> Any:
        control_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        client.controls[control_id] = future
        try:
            await client.send({"type": "control", "id": control_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            client.controls.pop(control_id, None)

    async def request_control(self, token_key: str, message_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        owner = self._prompt_owners.pop(f"{token_key}:{message_id}", None)
        client = self._clients.get(owner or "")
        if client is None:
            return {"accepted": False, "reason": "owner_detached"}
        return await self._request_client_control(client, "accept_restart_wind_down_opt_in", params)

    async def _spawn_gateway(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from gateway.restart import detached_restart_spawn_blocked, spawn_replacement_gateway

        blocked = detached_restart_spawn_blocked()
        if blocked:
            raise RuntimeError("test isolation forbids a detached gateway spawn")
        project_root = str(Path(__file__).resolve().parents[3])
        home = str(params.get("hermes_home") or "")
        if not home:
            raise ValueError("hermes_home is required")
        try:
            predecessor_pid = int(params.get("predecessor_pid") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("predecessor_pid must be a positive integer") from exc
        if predecessor_pid <= 0:
            raise ValueError("predecessor_pid must be a positive integer")

        def _popen(argv: list[str], **kwargs: Any):
            import subprocess
            from tools.environments.local import served_profile_child_env

            # The checkout venv the replacement was resolved to (spawn_replacement_gateway);
            # pinning the child to it prevents a hub running under another interpreter from
            # leaking a foreign VIRTUAL_ENV/PYTHONPATH into mixed site-packages (cffi incident).
            venv_bin_dir = kwargs.pop("checkout_venv_bin_dir", None)
            env = served_profile_child_env(target_home=home, inherit_credentials=True)
            if venv_bin_dir:
                env["VIRTUAL_ENV"] = str(Path(venv_bin_dir).parent)
                env["PATH"] = venv_bin_dir + os.pathsep + env.get("PATH", "")
            # Exactly the checkout: foreign inherited entries poison the child's sys.path.
            env["PYTHONPATH"] = project_root
            for marker in ("_HERMES_GATEWAY", "HERMES_GATEWAY", "HERMES_GATEWAY_MODE"):
                env.pop(marker, None)
            env[OVERLAP_SUCCESSOR_ENV] = "1"
            env[OVERLAP_PREDECESSOR_PID_ENV] = str(predecessor_pid)
            kwargs["env"] = env
            return subprocess.Popen(argv, **kwargs)

        process = await asyncio.to_thread(spawn_replacement_gateway, project_root, popen=_popen)
        self._successors.record_spawn(process.pid, home, predecessor_pid)
        return {"pid": process.pid}

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()


class _HubProfileResolver:
    """Minimal profile-route seam used by BasePlatformAdapter.build_source()."""

    def __init__(self, server: DiscordHubServer, token_key: str) -> None:
        self.server = server
        self.token_key = token_key

    async def accept_restart_wind_down_opt_in(self, **params: Any) -> Dict[str, Any]:
        return await self.server.request_control(
            self.token_key, str(params.get("message_id") or ""), params)

    def _profile_name_for_source(self, source: Any, adapter_profile: Optional[str] = None) -> Optional[str]:
        profiles = self.server._adapter_profiles.get(self.token_key) or {"default"}
        # A dedicated bot has one unambiguous runtime profile. Shared bots use
        # the same conjunctive profile routes as an in-process gateway.
        if len(profiles) == 1:
            return next(iter(profiles))
        from gateway.profile_routing import ProfileRouteRejected, match_profile_route
        transport_profile = None if "default" in profiles else adapter_profile
        route = match_profile_route(
            self.server._adapter_routes.get(self.token_key, []), "discord",
            guild_id=getattr(source, "guild_id", None), chat_id=getattr(source, "chat_id", None),
            thread_id=getattr(source, "thread_id", None), parent_chat_id=getattr(source, "parent_chat_id", None),
            adapter_profile=transport_profile, user_id=getattr(source, "user_id", None),
        )
        if route is not None:
            if route.profile not in profiles:
                raise ProfileRouteRejected(route.profile)
            return route.profile
        return "default" if "default" in profiles else sorted(profiles)[0]


def main() -> None:
    import argparse
    from hermes_logging import setup_logging

    parser = argparse.ArgumentParser(description="Own shared platform transports for attached Hermes gateways")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    args = parser.parse_args()
    setup_logging()
    hub = DiscordHubServer(args.socket)

    async def _run() -> None:
        await hub.start()
        try:
            await hub.serve_forever()
        finally:
            await hub.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
