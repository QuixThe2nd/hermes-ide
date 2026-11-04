"""Matrix transport ownership inside the existing multi-generation hub.

This is not a second broker: ``DiscordHubServer`` owns one ``MatrixHub`` and
offers its operations on the same Unix socket used for overlap restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from plugins.platforms.discord.hub import DiscordSessionRouter

logger = logging.getLogger(__name__)


def _matrix_key(config: Any) -> str:
    extra = config.extra or {}
    identity = json.dumps({
        "homeserver": str(extra.get("homeserver") or "").rstrip("/"),
        "token": config.token or "",
        "user_id": str(extra.get("user_id") or ""),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _result_dict(result: Any) -> Dict[str, Any]:
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
class _Attachment:
    key: str
    home: Path
    secrets: Dict[str, str]
    adapter: Any = None
    profiles: set[str] = field(default_factory=set)
    routes: list = field(default_factory=list)


class _ProfileResolver:
    """Profile-route seam used by the hub-side real MatrixAdapter."""

    def __init__(self, attachment: _Attachment) -> None:
        self.attachment = attachment

    def _profile_name_for_source(self, source: Any, adapter_profile: Optional[str] = None) -> Optional[str]:
        profiles = self.attachment.profiles or {"default"}
        if len(profiles) == 1:
            return next(iter(profiles))
        from gateway.profile_routing import ProfileRouteRejected, match_profile_route

        route = match_profile_route(
            self.attachment.routes,
            "matrix",
            chat_id=getattr(source, "chat_id", None),
            thread_id=getattr(source, "thread_id", None),
            parent_chat_id=getattr(source, "parent_chat_id", None),
            adapter_profile=None if "default" in profiles else adapter_profile,
            user_id=getattr(source, "user_id", None),
        )
        if route is not None:
            if route.profile not in profiles:
                raise ProfileRouteRejected(route.profile)
            return route.profile
        return "default" if "default" in profiles else sorted(profiles)[0]


class MatrixHub:
    """Own each shared Matrix sync/crypto adapter and route its events."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.router = DiscordSessionRouter(transport="Matrix", version_field="_matrix_hub_version")
        self._routers: Dict[str, DiscordSessionRouter] = {}
        self._attachments: Dict[str, _Attachment] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._prompt_owners: Dict[str, str] = {}
        self._stopping = False

    @staticmethod
    @contextmanager
    def _scope(attachment: _Attachment):
        # The real adapter resolves its per-profile store, credentials, authz,
        # recovery key, and plugin handlers under this canonical scope.  Sync
        # and background tasks created inside retain the same contextvars.
        from gateway.run import _profile_runtime_scope

        with _profile_runtime_scope(attachment.home, attachment.secrets):
            yield

    def router_for(self, key: str) -> DiscordSessionRouter:
        router = self._routers.get(key)
        if router is None:
            router = self.router if not self._routers else DiscordSessionRouter(
                transport="Matrix", version_field="_matrix_hub_version")
            self._routers[key] = router
        return router

    async def attach(self, *, config_data: Dict[str, Any], profile: str,
                     profile_routes: list, hermes_home: str,
                     secret_scope: Dict[str, str]) -> tuple[str, DiscordSessionRouter]:
        from gateway.config import PlatformConfig
        from gateway.profile_routing import parse_profile_routes
        from plugins.platforms.matrix.adapter import MatrixAdapter

        config = PlatformConfig.from_dict(config_data)
        extra = config.extra or {}
        if not extra.get("homeserver") or not (
                config.token or (extra.get("user_id") and extra.get("password"))):
            raise RuntimeError("Matrix hub attach has no resolved homeserver/credential")
        key = _matrix_key(config)
        profile_name = profile or "default"
        async with self._locks.setdefault(key, asyncio.Lock()):
            if self._stopping:
                raise RuntimeError("Matrix hub is stopping")
            attachment = self._attachments.get(key)
            if attachment is None:
                attachment = _Attachment(
                    key=key,
                    home=Path(hermes_home),
                    secrets=dict(secret_scope),
                    profiles={profile_name},
                    routes=parse_profile_routes(profile_routes),
                )
                with self._scope(attachment):
                    adapter = MatrixAdapter(config)
                    attachment.adapter = adapter
                    adapter._owner_profile = None
                    adapter.gateway_runner = _ProfileResolver(attachment)
                    adapter.handle_message = self._make_inbound_handler(attachment)
                    adapter._on_reaction = self._make_reaction_handler(attachment)
                    try:
                        connected = await adapter.connect()
                        if not connected:
                            raise RuntimeError("Matrix connection failed")
                        if self._stopping:
                            raise RuntimeError("Matrix hub is stopping")
                    except BaseException:
                        try:
                            await adapter.disconnect()
                        except Exception:
                            logger.exception("Matrix hub failed-attach cleanup failed")
                        raise
                self._attachments[key] = attachment
            else:
                attachment.profiles.add(profile_name)
                routes = parse_profile_routes(profile_routes)
                if routes:
                    merged = {(route.name, route.profile, route.bot_profile): route
                              for route in [*attachment.routes, *routes]}
                    attachment.routes = sorted(
                        merged.values(), key=lambda route: route.specificity, reverse=True)
        return key, self.router_for(key)

    def _make_inbound_handler(self, attachment: _Attachment):
        async def _deliver(event: Any) -> None:
            from gateway.run_drain_queue import serialize_drain_event

            with self._scope(attachment):
                key = attachment.adapter._event_session_key(event)
                record = serialize_drain_event(key, event)
                record["matrix_transport_key"] = attachment.key
            await self.router_for(attachment.key).route(key, record)
        return _deliver

    @staticmethod
    def _event_data(event: Any) -> Dict[str, Any]:
        content = getattr(event, "content", None)
        if content is not None and not isinstance(content, dict) and hasattr(content, "serialize"):
            content = content.serialize()
        return {
            "event_id": str(getattr(event, "event_id", "") or ""),
            "room_id": str(getattr(event, "room_id", "") or ""),
            "sender": str(getattr(event, "sender", "") or ""),
            "content": content or {},
        }

    def _make_reaction_handler(self, attachment: _Attachment):
        async def _deliver(event: Any) -> None:
            data = self._event_data(event)
            with self._scope(attachment):
                if attachment.adapter._is_self_sender(data["sender"]):
                    return
                if attachment.adapter._is_duplicate_event(data["event_id"]):
                    return
            content = data["content"]
            relates_to = (content.get("m.relates_to", {}) if isinstance(content, dict)
                          else getattr(content, "relates_to", {}))
            reacts_to = (relates_to.get("event_id", "") if isinstance(relates_to, dict)
                         else str(getattr(relates_to, "event_id", "") or ""))
            owner = self._prompt_owners.get(f"{attachment.key}:{reacts_to}")
            if not owner:
                return
            result = await self.server._request_generation_control(
                owner, "matrix_reaction", {"event": data}, timeout=120)
            if isinstance(result, dict) and result.get("prompt_pending") is False:
                self._prompt_owners.pop(f"{attachment.key}:{reacts_to}", None)
        return _deliver

    def register_prompt(self, client: Any, event_id: str) -> bool:
        if client is None or not event_id:
            raise ValueError("an attached Matrix prompt owner and event id are required")
        self._prompt_owners[f"{client.token_key}:{event_id}"] = client.generation_id
        return True

    def detach(self, client: Any) -> None:
        for key, owner in list(self._prompt_owners.items()):
            if owner == client.generation_id:
                self._prompt_owners.pop(key, None)

    async def rpc(self, client: Any, params: Dict[str, Any]) -> Any:
        key = str(params.get("token_key") or "")
        attachment = self._attachments.get(key)
        if attachment is None:
            raise RuntimeError("Matrix hub connection is unavailable")
        method = str(params.get("method") or "")
        if (method.startswith("_") and method != "_send_reaction") or method in {"connect", "disconnect"}:
            raise ValueError("Matrix RPC method is not public")
        fn = getattr(attachment.adapter, method, None)
        if not callable(fn):
            raise ValueError(f"Matrix adapter has no RPC method {method}")
        args = list(params.get("args") or [])
        kwargs = dict(params.get("kwargs") or {})
        with self._scope(attachment):
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        if method == "redact_message" and result:
            event_id = kwargs.get("event_id") or (args[1] if len(args) > 1 else "")
            self._prompt_owners.pop(f"{key}:{event_id}", None)
        return _result_dict(result)

    async def stop(self) -> None:
        self._stopping = True
        for attachment in list(self._attachments.values()):
            with self._scope(attachment):
                await attachment.adapter.disconnect()
        self._attachments.clear()
        self._prompt_owners.clear()
