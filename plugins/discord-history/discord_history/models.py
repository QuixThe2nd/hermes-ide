"""Canonical records produced from DiscordChatExporter JSON."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Guild:
    guild_id: str
    name: str
    icon_url: str | None = None


@dataclass(frozen=True)
class Channel:
    channel_id: str
    guild_id: str
    name: str
    channel_type: int = 0
    parent_channel_id: str | None = None
    topic: str | None = None
    is_thread: bool = False
    archived: bool | None = None
    locked: bool | None = None


@dataclass(frozen=True)
class User:
    user_id: str
    username: str | None
    global_name: str | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class Attachment:
    attachment_id: str
    filename: str
    media_type: str | None = None
    size_bytes: int | None = None
    url: str | None = None
    proxy_url: str | None = None


@dataclass(frozen=True)
class Embed:
    embed_id: str
    ordinal: int
    embed_type: str | None
    title: str | None
    description: str | None
    url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class Mention:
    mentioned_id: str
    mention_type: str


@dataclass(frozen=True)
class MessageReference:
    message_id: str | None = None
    channel_id: str | None = None
    guild_id: str | None = None


@dataclass(frozen=True)
class Message:
    message_id: str
    guild_id: str
    channel_id: str
    author: User
    created_at: datetime
    edited_at: datetime | None
    content: str
    content_hash: str
    raw: dict[str, Any]
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    embeds: tuple[Embed, ...] = field(default_factory=tuple)
    mentions: tuple[Mention, ...] = field(default_factory=tuple)
    reference: MessageReference | None = None
    message_type: int | None = None
    flags: int | None = None
    is_pinned: bool = False
