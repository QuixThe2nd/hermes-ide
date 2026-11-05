"""Opt-in Matrix inbox rooms: one private room per top-level human message.

The adapter owns event normalization and authorization. This module owns only the
root-event → child-room lifecycle and its durable, content-free routing metadata.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

from hermes_constants import get_hermes_home, hermes_home_key

logger = logging.getLogger(__name__)

_ACCOUNT_DATA_TYPE = "io.hermes.matrix.inbox_chats.v1"
_NOTICES = {
    "create": "Hermes could not create a private conversation for this inbox message.",
    "encryption": "Hermes could not verify encryption for this inbox message, so no room or agent turn was started.",
    "interrupted": "Hermes could not safely resume an interrupted room creation for this inbox message.",
    "dispatch": "Hermes created the private conversation but could not start its agent turn.",
    "state": "Hermes could not durably record this inbox conversation, so no agent turn was started.",
}


def parse_inbox_rooms(value: Any) -> dict[str, Optional[str]]:
    """Validate ``matrix.inbox_rooms`` without enabling partial malformed entries."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        logger.warning("Matrix: matrix.inbox_rooms must be a mapping; inbox routing is disabled")
        return {}
    parsed: dict[str, Optional[str]] = {}
    for raw_room, raw_space in value.items():
        room = str(raw_room or "").strip()
        space = None if raw_space is None else str(raw_space).strip() or None
        if not _is_room_id(room) or (space is not None and not _is_room_id(space)):
            logger.warning("Matrix: ignoring an invalid matrix.inbox_rooms entry (internal room IDs are required)")
            continue
        parsed[room] = space
    return parsed


def _is_room_id(value: Any) -> bool:
    # Room IDs are server-preserved opaque identifiers; v12 IDs need no server suffix.
    return isinstance(value, str) and value.startswith("!") and len(value) > 1


def _server_name_suffix(identifier: str) -> Optional[str]:
    if ":" not in identifier:
        return None
    return identifier.rsplit(":", 1)[1].strip() or None


def _matrix_link(room_id: str, event_id: Optional[str] = None) -> str:
    suffix = f"/{quote(event_id, safe='')}" if event_id else ""
    return f"https://matrix.to/#/{quote(room_id, safe='')}{suffix}"


def _room_title(msgtype: str) -> str:
    return {
        "m.image": "Image conversation",
        "m.audio": "Audio conversation",
        "m.video": "Video conversation",
        "m.file": "File conversation",
    }.get(msgtype, "New conversation")


def _is_not_found(exc: BaseException) -> bool:
    return getattr(exc, "errcode", None) == "M_NOT_FOUND" or getattr(exc, "status_code", None) == 404


@dataclass(frozen=True)
class InboxRoute:
    root_key: str
    inbox_room_id: str
    root_event_id: str
    sender: str
    encrypted: bool
    parent_space_id: Optional[str] = None
    child_room_id: Optional[str] = None
    state: str = "creating"
    origin_link_event_id: Optional[str] = None
    acknowledgement_event_id: Optional[str] = None
    dispatch_accepted: bool = False
    space_link_state: str = "pending"
    failure_notice_sent: bool = False
    last_error: Optional[str] = None

    def account_data(self) -> dict[str, Any]:
        return {
            "root_key": self.root_key,
            "inbox_room_id": self.inbox_room_id,
            "root_event_id": self.root_event_id,
            "sender": self.sender,
            "child_room_id": self.child_room_id,
            "encrypted": self.encrypted,
            "parent_space_id": self.parent_space_id,
            "origin_link_event_id": self.origin_link_event_id,
            "acknowledgement_event_id": self.acknowledgement_event_id,
            "dispatch_accepted": self.dispatch_accepted,
            "space_link_state": self.space_link_state,
            "failure_notice_sent": self.failure_notice_sent,
        }


class _InboxStore:
    """Small synchronous SQLite store; async callers offload every operation."""

    def __init__(self, path: Path):
        self.path = path
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        if not self._schema_ready:
            with self._schema_lock:
                if not self._schema_ready:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS matrix_inbox_routes (
                            root_key TEXT PRIMARY KEY,
                            inbox_room_id TEXT NOT NULL,
                            root_event_id TEXT NOT NULL,
                            sender TEXT NOT NULL,
                            encrypted INTEGER NOT NULL,
                            parent_space_id TEXT,
                            child_room_id TEXT,
                            state TEXT NOT NULL,
                            origin_link_event_id TEXT,
                            acknowledgement_event_id TEXT,
                            dispatch_accepted INTEGER NOT NULL DEFAULT 0,
                            space_link_state TEXT NOT NULL DEFAULT 'pending',
                            failure_notice_sent INTEGER NOT NULL DEFAULT 0,
                            last_error TEXT,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS matrix_inbox_child_room
                            ON matrix_inbox_routes(child_room_id)
                            WHERE child_room_id IS NOT NULL;
                        """
                    )
                    self._schema_ready = True
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _route(row: sqlite3.Row) -> InboxRoute:
        return InboxRoute(
            root_key=row["root_key"], inbox_room_id=row["inbox_room_id"],
            root_event_id=row["root_event_id"], sender=row["sender"],
            encrypted=bool(row["encrypted"]), parent_space_id=row["parent_space_id"],
            child_room_id=row["child_room_id"], state=row["state"],
            origin_link_event_id=row["origin_link_event_id"],
            acknowledgement_event_id=row["acknowledgement_event_id"],
            dispatch_accepted=bool(row["dispatch_accepted"]),
            space_link_state=row["space_link_state"],
            failure_notice_sent=bool(row["failure_notice_sent"]), last_error=row["last_error"])

    def lookup(self, root_key: str) -> Optional[InboxRoute]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM matrix_inbox_routes WHERE root_key = ?", (root_key,)).fetchone()
        return self._route(row) if row is not None else None

    def begin(self, route: InboxRoute) -> tuple[InboxRoute, bool]:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO matrix_inbox_routes
                   (root_key, inbox_room_id, root_event_id, sender, encrypted, parent_space_id,
                    state, space_link_state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'creating', ?, ?, ?)""",
                (route.root_key, route.inbox_room_id, route.root_event_id, route.sender,
                 int(route.encrypted), route.parent_space_id,
                 "pending" if route.parent_space_id else "skipped", now, now))
            row = conn.execute(
                "SELECT * FROM matrix_inbox_routes WHERE root_key = ?", (route.root_key,)).fetchone()
        return self._route(row), cursor.rowcount == 1

    def _update(self, root_key: str, assignments: str, values: tuple) -> InboxRoute:
        with self._connection() as conn:
            conn.execute(
                f"UPDATE matrix_inbox_routes SET {assignments}, updated_at = ? WHERE root_key = ?",
                (*values, time.time(), root_key))
            row = conn.execute("SELECT * FROM matrix_inbox_routes WHERE root_key = ?", (root_key,)).fetchone()
        if row is None:
            raise RuntimeError("Matrix inbox route disappeared during update")
        return self._route(row)

    def mark_ready(self, root_key: str, child_room_id: str, encrypted: bool) -> InboxRoute:
        return self._update(
            root_key, "child_room_id = ?, encrypted = ?, state = 'ready', last_error = NULL",
            (child_room_id, int(encrypted)))

    def mark_failed(self, root_key: str, error: str) -> InboxRoute:
        return self._update(root_key, "state = 'failed', last_error = ?", (error,))

    def mark_origin_linked(self, root_key: str, event_id: str) -> InboxRoute:
        return self._update(root_key, "origin_link_event_id = ?", (event_id,))

    def mark_acknowledged(self, root_key: str, event_id: Optional[str]) -> InboxRoute:
        return self._update(root_key, "acknowledgement_event_id = ?", (event_id or "",))

    def mark_dispatched(self, root_key: str) -> InboxRoute:
        return self._update(root_key, "dispatch_accepted = 1", ())

    def mark_space_link(self, root_key: str, state: str) -> InboxRoute:
        return self._update(root_key, "space_link_state = ?", (state,))

    def mark_failure_noticed(self, root_key: str) -> InboxRoute:
        return self._update(root_key, "failure_notice_sent = 1", ())

    def ready_routes(self) -> list[InboxRoute]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM matrix_inbox_routes WHERE state = 'ready'").fetchall()
        return [self._route(row) for row in rows]

    def restore(self, route: InboxRoute) -> None:
        now = time.time()
        data = route.account_data()
        with self._connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO matrix_inbox_routes
                   (root_key, inbox_room_id, root_event_id, sender, encrypted, parent_space_id,
                    child_room_id, state, origin_link_event_id, acknowledgement_event_id,
                    dispatch_accepted, space_link_state, failure_notice_sent, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?)""",
                (data["root_key"], data["inbox_room_id"], data["root_event_id"], data["sender"],
                 int(data["encrypted"]), data["parent_space_id"], data["child_room_id"],
                 data["origin_link_event_id"], data["acknowledgement_event_id"],
                 int(data["dispatch_accepted"]), data["space_link_state"],
                 int(data["failure_notice_sent"]), now, now))


class MatrixInboxRouter:
    """Create and recover private child rooms for explicitly configured inboxes."""

    def __init__(self, adapter: Any, inbox_rooms: dict[str, Optional[str]]):
        self.adapter = adapter
        self.inbox_rooms = inbox_rooms
        self._locks: dict[str, asyncio.Lock] = {}
        self._children: dict[str, InboxRoute] = {}
        self._account_data_lock = asyncio.Lock()
        self._remote_recovered = False
        self._mirror_warning_logged = False
        self._profile_key = hashlib.sha256(hermes_home_key().encode("utf-8")).hexdigest()
        self._store = _InboxStore(
            get_hermes_home() / "platforms" / "matrix" / "inbox-routes.db") if inbox_rooms else None
        if self._store is not None:
            try:
                self._children = {route.child_room_id: route for route in self._store.ready_routes()}
            except Exception as exc:
                logger.warning("Matrix: could not load inbox routing state: %s", type(exc).__name__)

    def is_child_room(self, room_id: str) -> bool:
        return room_id in self._children

    def child_reply_anchor(self, room_id: str, event_id: Optional[str]) -> Optional[str]:
        """Anchor an inbox root's answer to the link event that exists in its child."""
        route = self._children.get(room_id)
        if route and event_id == route.root_event_id and route.origin_link_event_id:
            return route.origin_link_event_id
        return event_id

    def _lock_for(self, root_key: str) -> asyncio.Lock:
        return self._locks.setdefault(root_key, asyncio.Lock())

    @staticmethod
    def _root_key(inbox_room_id: str, event_id: str) -> str:
        return hashlib.sha256(f"{inbox_room_id}\0{event_id}".encode("utf-8")).hexdigest()

    async def route_inbound(
        self, room_id: str, sender: str, event_id: str, event_ts: float,
        source_content: dict, relates_to: dict, msgtype: str, *, is_command: bool,
    ) -> bool:
        """Return True when an opted-in inbox event was consumed (including safe failure)."""
        if self.is_child_room(room_id) or room_id not in self.inbox_rooms:
            return False
        if msgtype == "m.notice" or is_command:
            return False
        if relates_to.get("rel_type") == "m.thread" or relates_to.get("m.in_reply_to"):
            return False
        if not event_id:
            logger.warning("Matrix: refusing inbox routing for an event without an event ID")
            return True
        # A room is a credentialed side effect: unknown authorization is not approval.
        # Since the hub transport migration the sync loop runs in the hub process, where
        # no runner authorization callback is registered, so the gate falls back to the
        # adapter's own intake allowlist (fail-closed when that admits no one either).
        authorized = self.adapter._is_sender_authorized(sender, "group", room_id)
        if authorized is None:
            if self.adapter._inbox_sender_authorized(sender) is not True:
                logger.info("Matrix: inbox message did not pass the existing sender authorization gate")
                return True
            logger.info("Matrix: inbox sender admitted via adapter allowlist (no runner authorization callback on this side)")
        elif authorized is not True:
            logger.info("Matrix: inbox message did not pass the existing sender authorization gate")
            return True
        root_key = self._root_key(room_id, event_id)
        async with self._lock_for(root_key):
            await self._route_locked(
                root_key, room_id, sender, event_id, event_ts, source_content, relates_to, msgtype)
        return True

    async def _route_locked(
        self, root_key: str, room_id: str, sender: str, event_id: str, event_ts: float,
        source_content: dict, relates_to: dict, msgtype: str,
    ) -> None:
        try:
            route = await asyncio.to_thread(self._store.lookup, root_key)
            if route is None and not self._remote_recovered:
                await self.recover_account_data()
                route = await asyncio.to_thread(self._store.lookup, root_key)
        except Exception as exc:
            logger.warning("Matrix: inbox state lookup failed: %s", type(exc).__name__)
            await self._send_untracked_failure(room_id, event_id, "state")
            return
        if route is not None and route.sender != sender:
            logger.warning("Matrix: refusing an inbox root replay with a different sender")
            return
        if route is not None and route.state != "ready":
            if route.state == "creating":
                try:
                    route = await asyncio.to_thread(self._store.mark_failed, root_key, "interrupted")
                except Exception:
                    pass
            await self._notify_failure_once(route, "interrupted" if route.last_error == "interrupted" else "create")
            return
        if route is None:
            route = InboxRoute(
                root_key=root_key, inbox_room_id=room_id, root_event_id=event_id, sender=sender,
                encrypted=False, parent_space_id=self.inbox_rooms[room_id],
                space_link_state="pending" if self.inbox_rooms[room_id] else "skipped")
            try:
                route, created = await asyncio.to_thread(self._store.begin, route)
            except Exception as exc:
                logger.warning("Matrix: could not begin durable inbox route: %s", type(exc).__name__)
                await self._send_untracked_failure(room_id, event_id, "state")
                return
            if not created and route.state != "ready":
                await self._notify_failure_once(route, "create")
                return
            if created:
                try:
                    algorithm = await self._source_encryption_algorithm(room_id)
                    route = replace(route, encrypted=bool(algorithm))
                    if algorithm and not (
                            self.adapter._encryption and getattr(self.adapter._client, "crypto", None)):
                        raise RuntimeError("encrypted input requires an active E2EE client")
                    initial_state = ([{
                        "type": "m.room.encryption", "state_key": "",
                        "content": {"algorithm": algorithm},
                    }] if algorithm else None)
                    child_room_id = await self.adapter.create_room(
                        name=_room_title(msgtype), invite=[sender], is_direct=False,
                        preset="private_chat", initial_state=initial_state)
                    if not child_room_id:
                        raise RuntimeError("room creation failed")
                except Exception as exc:
                    reason = "encryption" if "encrypt" in str(exc).lower() else "create"
                    logger.warning("Matrix: inbox room creation failed (%s): %s", reason, type(exc).__name__)
                    route = await self._mark_failed(route, reason)
                    await self._notify_failure_once(route, reason)
                    return
                try:
                    route = await asyncio.to_thread(self._store.mark_ready, root_key, child_room_id, route.encrypted)
                except Exception as exc:
                    logger.warning("Matrix: could not persist the new inbox room: %s", type(exc).__name__)
                    await self._notify_failure_once(route, "state")
                    return
                self._children[child_room_id] = route
                self.adapter._dm_rooms[child_room_id] = True
                self.adapter._invalidate_room_identities(child_room_id)
                await self._mirror_best_effort(route)
        self._children[route.child_room_id] = route
        self.adapter._dm_rooms[route.child_room_id] = True
        if not route.origin_link_event_id:
            result = await self.adapter.send(
                route.child_room_id,
                f"Started from [the original inbox message]({_matrix_link(room_id, event_id)}).")
            if not result.success or not result.message_id:
                await self._notify_failure_once(route, "dispatch")
                return
            try:
                route = await asyncio.to_thread(
                    self._store.mark_origin_linked, root_key, str(result.message_id))
                self._children[route.child_room_id] = route
            except Exception as exc:
                logger.warning("Matrix: could not checkpoint the origin link: %s", type(exc).__name__)
                await self._notify_failure_once(route, "state")
                return
            await self._mirror_best_effort(route)
        route = await self._link_parent_space(route)
        if not route.acknowledgement_event_id:
            acknowledgement = f"Opened a private conversation: {_matrix_link(route.child_room_id)}"
            if route.space_link_state == "failed":
                acknowledgement += "\nThe room was created, but Hermes could not add it to the configured space; check space permissions."
            result = await self.adapter.send(room_id, acknowledgement)
            if result.success:
                try:
                    route = await asyncio.to_thread(
                        self._store.mark_acknowledged, root_key, result.message_id)
                    await self._mirror_best_effort(route)
                except Exception as exc:
                    logger.warning("Matrix: could not checkpoint the inbox acknowledgement: %s", type(exc).__name__)
                    await self._notify_failure_once(route, "state")
                    return
            else:
                logger.warning("Matrix: inbox acknowledgement could not be sent; the private room remains available")
        if route.dispatch_accepted:
            return
        if msgtype in ("m.image", "m.audio", "m.video", "m.file"):
            event = await self.adapter._handle_media_message(
                route.child_room_id, sender, event_id, event_ts, source_content, relates_to, msgtype)
        else:
            event = await self.adapter._handle_text_message(
                route.child_room_id, sender, event_id, event_ts, source_content, relates_to, batch=False)
        if event is None or not getattr(event, "_gateway_accepted", False):
            await self._notify_failure_once(route, "dispatch")
            return
        try:
            route = await asyncio.to_thread(self._store.mark_dispatched, root_key)
        except Exception as exc:
            logger.warning("Matrix: initial turn was accepted but its checkpoint failed: %s", type(exc).__name__)
            return
        self._children[route.child_room_id] = route
        await self._mirror_best_effort(route)

    async def _source_encryption_algorithm(self, room_id: str) -> Optional[str]:
        client = self.adapter._client
        if client is None:
            raise RuntimeError("Matrix client is disconnected")
        from mautrix.types import (
            EncryptionAlgorithm, EventType, RoomEncryptionStateEventContent, RoomID, StateEvent)
        try:
            event = await client.get_state_event(
                RoomID(room_id), EventType.ROOM_ENCRYPTION, format="event")
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise RuntimeError("encryption state could not be verified") from exc
        if not isinstance(event, StateEvent) or event.type != EventType.ROOM_ENCRYPTION:
            raise RuntimeError("encryption state response was malformed")
        content = event.content
        if not isinstance(content, RoomEncryptionStateEventContent):
            raise RuntimeError("encryption state content was malformed")
        if content.algorithm != EncryptionAlgorithm.MEGOLM_V1:
            raise RuntimeError("encryption algorithm was missing or unsupported")
        return str(content.algorithm)

    async def _link_parent_space(self, route: InboxRoute) -> InboxRoute:
        if not route.parent_space_id or route.space_link_state != "pending":
            return route
        server = _server_name_suffix(route.child_room_id)
        if server is None:
            # The bot is a joined member of the child it created, so its MXID is known via context.
            own_account = str(self.adapter._user_id or getattr(self.adapter._client, "mxid", "") or "")
            server = _server_name_suffix(own_account)
        try:
            if server is None:
                raise RuntimeError("no known server name for the space-child via list")
            await self.adapter._client.send_state_event(
                route.parent_space_id, "m.space.child", {"via": [server]},
                state_key=route.child_room_id)
            state = "linked"
        except Exception as exc:
            logger.warning(
                "Matrix: could not link an inbox child to its configured space (%s)", type(exc).__name__)
            state = "failed"
        try:
            return await asyncio.to_thread(self._store.mark_space_link, route.root_key, state)
        except Exception as exc:
            logger.warning("Matrix: could not checkpoint optional space linking: %s", type(exc).__name__)
            return replace(route, space_link_state=state)

    async def _mark_failed(self, route: InboxRoute, reason: str) -> InboxRoute:
        try:
            return await asyncio.to_thread(self._store.mark_failed, route.root_key, reason)
        except Exception:
            return replace(route, state="failed", last_error=reason)

    async def _notify_failure_once(self, route: InboxRoute, reason: str) -> None:
        if route.failure_notice_sent:
            return
        target = route.child_room_id or route.inbox_room_id
        child = f" {_matrix_link(route.child_room_id)}" if route.child_room_id else ""
        original = _matrix_link(route.inbox_room_id, route.root_event_id)
        text = f"{_NOTICES.get(reason, _NOTICES['create'])}{child}\nOriginal message: {original}\nNo fallback thread was created; send a new top-level message after checking the gateway logs."
        result = await self.adapter.send(target, text)
        if not result.success and target != route.inbox_room_id:
            result = await self.adapter.send(route.inbox_room_id, text)
        if not result.success:
            logger.warning("Matrix: could not deliver an inbox routing failure notice")
            return
        try:
            await asyncio.to_thread(self._store.mark_failure_noticed, route.root_key)
        except Exception:
            pass

    async def _send_untracked_failure(self, room_id: str, event_id: str, reason: str) -> None:
        text = f"{_NOTICES[reason]}\nOriginal message: {_matrix_link(room_id, event_id)}\nNo fallback thread was created; send a new top-level message after checking the gateway logs."
        result = await self.adapter.send(room_id, text)
        if not result.success:
            logger.warning("Matrix: could not deliver an inbox state failure notice")

    def _account_data_profile(self, raw: Any) -> tuple[dict, dict, dict]:
        if not isinstance(raw, dict):
            raise ValueError("invalid account data")
        profiles = raw.get("profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("invalid account-data profiles")
        mine = profiles.get(self._profile_key, {})
        if not isinstance(mine, dict):
            raise ValueError("invalid account-data profile")
        return raw, profiles, mine

    async def _read_remote_routes(self) -> list[InboxRoute]:
        client = self.adapter._client
        if client is None:
            return []
        try:
            raw = await client.get_account_data(_ACCOUNT_DATA_TYPE)
        except Exception as exc:
            if _is_not_found(exc):
                return []
            raise
        _, _, mine = self._account_data_profile(raw)
        account = str(mine.get("account") or "")
        own_account = str(self.adapter._user_id or getattr(client, "mxid", "") or "")
        if not mine:
            return []
        if not account or not own_account or account.casefold() != own_account.casefold():
            logger.warning("Matrix: ignoring inbox account data for a different Matrix account")
            return []
        chats = mine.get("chats", [])
        if not isinstance(chats, list):
            raise ValueError("invalid inbox account-data chats")
        routes = []
        for item in chats:
            try:
                if not isinstance(item, dict):
                    continue
                route = InboxRoute(
                    root_key=str(item["root_key"]), inbox_room_id=str(item["inbox_room_id"]),
                    root_event_id=str(item["root_event_id"]), sender=str(item["sender"]),
                    child_room_id=str(item["child_room_id"]), encrypted=bool(item["encrypted"]),
                    parent_space_id=item.get("parent_space_id"),
                    origin_link_event_id=item.get("origin_link_event_id"),
                    acknowledgement_event_id=item.get("acknowledgement_event_id"),
                    dispatch_accepted=bool(item.get("dispatch_accepted")),
                    space_link_state=str(item.get("space_link_state") or "skipped"),
                    failure_notice_sent=bool(item.get("failure_notice_sent")), state="ready")
                if (len(route.root_key) == 64
                        and route.root_key == self._root_key(route.inbox_room_id, route.root_event_id)
                        and _is_room_id(route.inbox_room_id) and _is_room_id(route.child_room_id)
                        and route.root_event_id.startswith("$") and route.sender.startswith("@")
                        and ":" in route.sender
                        and (route.parent_space_id is None or _is_room_id(route.parent_space_id))
                        and (route.origin_link_event_id is None or isinstance(route.origin_link_event_id, str))
                        and (route.acknowledgement_event_id is None
                             or isinstance(route.acknowledgement_event_id, str))
                        and route.space_link_state in {"pending", "linked", "skipped", "failed"}):
                    routes.append(route)
            except (KeyError, TypeError, ValueError):
                continue
        return routes

    async def recover_account_data(self) -> None:
        """Restore only this profile/account's bot-owned account-data records."""
        if self._store is None or self._remote_recovered or self.adapter._client is None:
            return
        async with self._account_data_lock:
            if self._remote_recovered:
                return
            try:
                routes = await self._read_remote_routes()
                for route in routes:
                    await asyncio.to_thread(self._store.restore, route)
                    stored = await asyncio.to_thread(self._store.lookup, route.root_key)
                    if stored is not None and stored.state == "ready":
                        self._children[stored.child_room_id] = stored
                        self.adapter._dm_rooms[stored.child_room_id] = True
                self._remote_recovered = True
                if routes:
                    self.adapter._invalidate_room_identities()
            except Exception as exc:
                logger.warning("Matrix: inbox account-data recovery failed: %s", type(exc).__name__)

    async def _mirror_best_effort(self, route: InboxRoute) -> None:
        client = self.adapter._client
        if client is None:
            return
        async with self._account_data_lock:
            try:
                try:
                    raw = await client.get_account_data(_ACCOUNT_DATA_TYPE)
                except Exception as exc:
                    if not _is_not_found(exc):
                        raise
                    raw = {}
                if not raw:
                    raw = {"version": 1, "profiles": {}}
                raw, profiles, mine = self._account_data_profile(raw)
                account = str(self.adapter._user_id or getattr(client, "mxid", "") or "")
                existing_account = str(mine.get("account") or "")
                if existing_account and existing_account.casefold() != account.casefold():
                    raise ValueError("account-data owner mismatch")
                chats = mine.get("chats", [])
                if not isinstance(chats, list):
                    raise ValueError("invalid account-data chats")
                chats = [item for item in chats if not (
                    isinstance(item, dict) and item.get("root_key") == route.root_key)]
                chats.append(route.account_data())
                mine.update({"account": account, "chats": chats})
                profiles[self._profile_key] = mine
                raw["version"] = 1
                raw["profiles"] = profiles
                await client.set_account_data(_ACCOUNT_DATA_TYPE, raw)
            except Exception as exc:
                if not self._mirror_warning_logged:
                    logger.warning(
                        "Matrix: could not mirror inbox routing to account data; local recovery remains active (%s)",
                        type(exc).__name__)
                    self._mirror_warning_logged = True
