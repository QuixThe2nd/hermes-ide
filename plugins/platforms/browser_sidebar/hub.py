"""Hub-owned Brave sidebar WebSocket and current-gateway routing."""

import asyncio
import json
import logging
from typing import Any, Optional

from gateway.platforms._shared import coerce_port as _coerce_port
from plugins.platforms.browser_sidebar.protocol import DEFAULT_PORT, LOCK_SCOPE

logger = logging.getLogger(__name__)


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

    async def attach(self, client, config: dict[str, Any], profile: str) -> dict[str, Any]:
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
        self._active = client
        logger.info("Browser sidebar gateway %s attached for profile %s", generation, profile)
        return {"connected": self._socket is not None, "generation": generation}

    def detach(self, client) -> None:
        """Detach routing only; the listener, token, and extension socket stay hub-owned."""
        if self._active is client:
            self._active = None

    async def send(self, client, frame: Any) -> dict[str, Any]:
        if self._active is not client:
            return {"sent": False, "error": "browser sidebar gateway is not current"}
        if not isinstance(frame, dict):
            raise ValueError("browser sidebar frame must be an object")
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
        elif frame_type in ("chat", "tool_result"):
            client = self._active
            if client is None:
                if frame_type == "chat":
                    await self._write_frame({"type": "error", "error": "gateway unavailable"}, websocket)
                return
            try:
                await client.send({"type": "browser_frame", "frame": frame})
            except (ConnectionError, OSError):
                if self._active is client:
                    self.detach(client)
                if frame_type == "chat":
                    await self._write_frame({"type": "error", "error": "gateway unavailable"}, websocket)
        else:
            await self._write_frame({
                "type": "error", "text": f"unknown frame type: {frame_type!r}",
            }, websocket)

    async def _write_frame(self, frame: dict[str, Any], websocket=None) -> bool:
        target = websocket or self._socket
        if target is None or target is not self._socket:
            return False
        try:
            async with self._write_lock:
                if target is not self._socket:
                    return False
                await target.send(json.dumps(frame))
            return True
        except Exception:
            return False

    async def _notify_connection(self, connected: bool) -> None:
        client = self._active
        if client is None:
            return
        try:
            await client.send({"type": "browser_connection", "connected": connected})
        except (ConnectionError, OSError):
            if self._active is client:
                self.detach(client)

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
