"""Hub-owned Brave sidebar WebSocket and per-session gateway routing.

The hub keeps ONE authenticated extension socket for the whole installation
and routes each sidebar session to exactly one live gateway generation via
``DiscordSessionRouter`` — the same instant-overlap machinery Discord uses.
A successor generation attaches while the predecessor is still draining:
owned in-flight sessions stay with their running owner, new sessions go to
the successor immediately, and a pending ``run_tab_js`` result returns to
the generation that issued the request.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from gateway.platforms._shared import coerce_port as _coerce_port
from plugins.platforms.browser_sidebar.protocol import DEFAULT_PORT, LOCK_SCOPE
from plugins.platforms.discord.hub import DiscordSessionRouter

logger = logging.getLogger(__name__)

# Cap on remembered tool-request ownership: one entry per in-flight
# run_tab_js request; the cap only bounds memory against a pathological
# client that never answers.
_MAX_TOOL_OWNERS = 4096


class BrowserSidebarHub:
    """Own one authenticated extension socket independently of gateway lifetimes."""

    def __init__(self):
        self._listener = None
        self._socket = None
        self._write_lock = asyncio.Lock()
        self._token: Optional[str] = None
        self._locked_tokens: set[str] = set()
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._active = None
        self._retired_generations: set[str] = set()
        # Attached sidebar generations (newest last) and per-profile routers.
        self._profiles: Dict[str, str] = {}
        self._session_keys: Dict[str, str] = {}
        self._clients: "OrderedDict[str, Any]" = OrderedDict()
        self._routers: Dict[str, DiscordSessionRouter] = {}
        self._session_keys: Dict[str, str] = {}
        self._tool_owners: "OrderedDict[str, str]" = OrderedDict()
        self._gateway_pids: Dict[str, int] = {}
        self._owner_profile: Optional[str] = None
        self._owner_session_key: str = ""

    async def attach(
        self, client, config: dict[str, Any], profile: str, *,
        store_dir: Optional[Path] = None, session_key: str = "",
    ) -> Tuple[dict[str, Any], Optional[DiscordSessionRouter]]:
        generation = client.generation_id
        if generation in self._retired_generations:
            raise RuntimeError("browser sidebar gateway generation was superseded")
        if not isinstance(config, dict):
            raise ValueError("browser sidebar attach config must be an object")
        extra = config.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        token = extra.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("browser_sidebar token is required")
        host = str(extra.get("host") or "127.0.0.1")
        port = _coerce_port(extra.get("port"), DEFAULT_PORT)
        if self._listener is not None and (host, port) != (self._host, self._port):
            raise RuntimeError("browser sidebar host/port cannot change while the hub listener is running")

        token_changed = self._listener is not None and token != self._token
        if self._listener is None or token_changed:
            await self._acquire_token_lock(token)
            self._token = token
        try:
            if self._listener is None:
                from websockets.asyncio.server import serve

                self._listener = await serve(
                    self._handle_connection, host, port,
                    select_subprotocol=self._select_subprotocol,
                    ping_interval=20, ping_timeout=20,
                )
                self._host, self._port = host, port
                logger.info("Browser sidebar hub listening on ws://%s:%s/ws", host, port)

            if token_changed:
                await self._release_token_lock(except_token=token)
                if self._socket is not None:
                    await self._socket.close(code=1008, reason="authentication changed")
        except Exception:
            if self._listener is None:
                await self._release_token_lock()
            raise

        previous = self._active
        if previous is not None and previous is not client:
            # Retirement survives detachment: an old process must never reclaim its successor.
            self._retired_generations.add(previous.generation_id)
        self._clients.pop(generation, None)
        self._clients[generation] = client
        self._active = client
        self._owner_profile = profile
        if session_key:
            self._session_keys[generation] = str(session_key)
            self._owner_session_key = str(session_key)
        self._profiles[generation] = profile
        self._gateway_pids[generation] = int(getattr(client, "gateway_pid", 0) or 0)

        router = self._routers.get(profile)
        if router is None:
            router = DiscordSessionRouter(
                transport="BrowserSidebar", version_field="_sidebar_hub_version")
            if store_dir is not None:
                digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
                router.bind_store(Path(store_dir) / f"hub-undelivered-browser-sidebar-{digest}.json")
            self._routers[profile] = router
        logger.info("Browser sidebar gateway %s attached for profile %s", generation, profile)
        return {"connected": self._socket is not None, "generation": generation}, router

    def detach(self, client) -> None:
        """Detach routing only; the listener, token, and extension socket stay hub-owned."""
        generation = getattr(client, "generation_id", None)
        if generation is not None:
            self._clients.pop(generation, None)
            self._profiles.pop(generation, None)
            self._gateway_pids.pop(generation, None)
            self._session_keys.pop(generation, None)
            for request_id, owner in list(self._tool_owners.items()):
                if owner == generation:
                    self._tool_owners.pop(request_id, None)
        if self._active is client:
            self._active = next(reversed(self._clients.values()), None) if self._clients else None

    def status(self) -> Dict[str, Any]:
        """Nonsecret routing state for diagnostics."""
        generations = [{
            "generation_id": generation,
            "profile": self._profiles.get(generation, ""),
            "gateway_pid": self._gateway_pids.get(generation, 0),
            "retired": generation in self._retired_generations,
        } for generation in self._clients]
        return {
            "hub_pid": os.getpid(),
            "browser_connected": self._socket is not None,
            "current_generation": getattr(self._active, "generation_id", None),
            "generations": generations,
            "owner_profile": self._owner_profile,
            "session_owners": {
                profile: router.owners_snapshot()
                for profile, router in self._routers.items()
            },
            "pending_tool_requests": len(self._tool_owners),
        }

    async def send(self, client, frame: Any) -> dict[str, Any]:
        # "Still attached", not "is active": a draining predecessor's mid-turn
        # drafts and finals must flow after a successor attached. Retirement
        # still blocks re-attach, so a detached generation cannot steal back.
        generation = str(getattr(client, "generation_id", "") or "")
        if not generation or self._clients.get(generation) is not client:
            return {"sent": False, "error": "browser sidebar gateway is not attached"}
        if not isinstance(frame, dict):
            raise ValueError("browser sidebar frame must be an object")
        request_id = frame.get("request_id")
        if frame.get("type") == "tool" and request_id:
            # Route the tool_result back to the generation that issued it.
            self._tool_owners[str(request_id)] = generation
            while len(self._tool_owners) > _MAX_TOOL_OWNERS:
                self._tool_owners.popitem(last=False)
        sent = await self._write_frame(frame)
        return {"sent": sent, **({} if sent else {"error": "browser sidebar is not connected"})}

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
            await self._listener.wait_closed()
            self._listener = None
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
        self._active = None
        self._clients.clear()
        self._profiles.clear()
        self._session_keys.clear()
        self._gateway_pids.clear()
        self._tool_owners.clear()
        await self._release_token_lock()

    def _select_subprotocol(self, _ws, offered):
        from websockets.exceptions import NegotiationError
        from websockets.typing import Subprotocol

        if list(offered) != [self._token]:
            raise NegotiationError("unauthorized")
        return Subprotocol(str(self._token))

    async def _handle_connection(self, websocket) -> None:
        if websocket.subprotocol != self._token:
            await websocket.close(code=1008, reason="unauthorized")
            return
        previous = self._socket
        self._socket = websocket
        if previous is not None and previous is not websocket:
            await previous.close(code=1012, reason="reconnected")
        await self._notify_connection(True)
        try:
            async for raw in websocket:
                if self._socket is not websocket:
                    break
                await self._handle_frame(websocket, raw)
        except Exception:
            logger.exception("Browser sidebar connection handler failed")
        finally:
            if self._socket is websocket:
                self._socket = None
                await self._notify_connection(False)

    async def _handle_frame(self, websocket, raw: str) -> None:
        try:
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                raise ValueError("frame must be a JSON object")
        except (ValueError, TypeError) as exc:
            await self._write_frame({"type": "error", "text": f"malformed frame: {exc}"}, websocket)
            return
        frame_type = str(frame.get("type") or "")
        if frame_type == "hello":
            await self._write_frame({"type": "ready"}, websocket)
        elif frame_type == "ready":
            pass
        elif frame_type == "chat":
            await self._route_chat_frame(websocket, frame)
        elif frame_type == "tool_result":
            await self._route_tool_result(frame)
        else:
            await self._write_frame({
                "type": "error", "text": f"unknown frame type: {frame_type!r}",
            }, websocket)

    async def _route_chat_frame(self, websocket, frame: dict[str, Any]) -> None:
        """Route a chat frame through the session router, Discord-style.

        Owned sessions stay with their (possibly draining) owner generation;
        new sessions go to the newest attached generation. With no router at
        all — no gateway has ever attached — the extension still gets the
        explicit gateway-unavailable error frame.
        """
        router = self._routers.get(self._owner_profile) if self._owner_profile else None
        session_key = self._owner_session_key
        if router is None or not session_key:
            # No gateway has ever attached: nothing can own or retain the
            # event, so the explicit error frame is the honest answer.
            await self._write_frame({"type": "error", "error": "gateway unavailable"}, websocket)
            return
        from gateway.run_drain_queue import serialize_drain_event
        from plugins.platforms.browser_sidebar.protocol import (
            chat_frame_to_event,
            chat_id_for_frame,
            session_key_for_chat_id,
        )

        # One session per browser tab: the attached key is the legacy base
        # (chat id "brave"); the frame's tab id selects the actual session.
        # A frame without a usable tab id keeps the base key itself.
        session_key = session_key_for_chat_id(session_key, chat_id_for_frame(frame))
        record = serialize_drain_event(session_key, chat_frame_to_event(frame))
        delivered = await router.route(session_key, record)
        if delivered is None:
            # The router retained the event for the next live generation —
            # nothing further to tell the extension.
            logger.info("Browser sidebar hub retained a chat frame for %s", session_key)

    async def _route_tool_result(self, frame: dict[str, Any]) -> None:
        """Return a tool_result to the generation that issued the request."""
        request_id = str(frame.get("request_id") or "")
        owner = self._tool_owners.pop(request_id, None)
        client = self._clients.get(owner) if owner else None
        if client is None:
            client = self._active
            if owner is not None:
                logger.warning(
                    "Browser sidebar tool result for %s arrived after its generation detached; "
                    "falling back to the current generation", request_id,
                )
        if client is None:
            return
        try:
            await client.send({"type": "browser_frame", "frame": frame})
        except (ConnectionError, OSError):
            logger.warning("Browser sidebar tool result for %s could not be delivered", request_id)

    async def _write_frame(self, frame: dict[str, Any], websocket=None) -> bool:
        target = websocket or self._socket
        if target is None:
            return False
        try:
            async with self._write_lock:
                await target.send(json.dumps(frame, separators=(",", ":")))
            return True
        except (ConnectionError, OSError):
            return False

    async def _notify_connection(self, connected: bool) -> None:
        # Every attached generation tracks browser liveness (pending tools
        # must fail on disconnect on the generation that awaits them).
        for client in list(self._clients.values()):
            try:
                await client.send({"type": "browser_connection", "connected": connected})
            except (ConnectionError, OSError):
                pass

    async def _acquire_token_lock(self, token: str) -> None:
        from gateway.status import acquire_scoped_lock

        acquired, _holder = acquire_scoped_lock(LOCK_SCOPE, token)
        if not acquired:
            raise RuntimeError("browser_sidebar token already in use by another gateway or hub")
        self._locked_tokens.add(token)

    async def _release_token_lock(self, except_token: Optional[str] = None) -> None:
        from gateway.status import release_scoped_lock

        tokens = self._locked_tokens
        self._locked_tokens = {except_token} if except_token in tokens else set()
        for token in tokens:
            if token != except_token:
                release_scoped_lock(LOCK_SCOPE, token)
