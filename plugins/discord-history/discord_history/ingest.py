"""DCE normalization and atomic canonical archive import."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .models import Attachment, Channel, Embed, Guild, Mention, Message, MessageReference, User


def _id(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("id")
    if value is None or str(value).strip() == "":
        raise ValueError("missing Discord ID")
    return str(value)


def _time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _stable_id(prefix: str, message_id: str, ordinal: int, obj: Mapping[str, Any]) -> str:
    existing = obj.get("id")
    if existing not in (None, ""):
        return str(existing)
    seed = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{message_id}:{ordinal}:{hashlib.sha256(seed.encode()).hexdigest()[:20]}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_MESSAGE_TYPES = {
    "Default": 0,
    "RecipientAdd": 1,
    "RecipientRemove": 2,
    "Call": 3,
    "ChannelNameChange": 4,
    "ChannelIconChange": 5,
    "ChannelPinnedMessage": 6,
    "UserJoin": 7,
    "GuildBoost": 8,
    "GuildBoostTier1": 9,
    "GuildBoostTier2": 10,
    "GuildBoostTier3": 11,
    "ThreadCreated": 18,
    "Reply": 19,
    "ChatInputCommand": 20,
    "ThreadStarterMessage": 21,
    "ContextMenuCommand": 23,
    "AutoModerationAction": 24,
}


_CHANNEL_TYPES = {
    "GuildText": 0,
    "GuildNews": 5,
    "GuildNewsThread": 10,
    "GuildPublicThread": 11,
    "GuildPrivateThread": 12,
    "GuildForum": 15,
    "GuildMedia": 16,
}


def normalize_export(
    document: Mapping[str, Any], *, channel_metadata: Mapping[str, Any] | None = None
) -> tuple[Guild, Channel, tuple[Message, ...]]:
    guild_obj = document.get("guild") or document.get("server") or {}
    channel_obj = {**(document.get("channel") or {}), **(channel_metadata or {})}
    guild_id = _id(guild_obj.get("id") or document.get("guildId") or channel_obj.get("guildId") or channel_obj.get("guild_id"))
    channel_id = _id(channel_obj.get("id") or document.get("channelId"))
    guild = Guild(guild_id, str(guild_obj.get("name") or "Unknown guild"), guild_obj.get("iconUrl") or guild_obj.get("icon"))
    thread_meta = channel_obj.get("threadMetadata") or channel_obj.get("thread_metadata") or {}
    raw_type = channel_obj.get("type", 0)
    channel_type = int(_CHANNEL_TYPES.get(str(raw_type), raw_type))
    is_thread = channel_type in {10, 11, 12} or bool(channel_obj.get("isThread") or thread_meta)
    channel = Channel(
        channel_id, guild_id, str(channel_obj.get("name") or "Unknown channel"),
        channel_type,
        str(channel_obj.get("parentId") or channel_obj.get("parent_id")) if channel_obj.get("parentId") or channel_obj.get("parent_id") else None,
        channel_obj.get("topic"), is_thread,
        thread_meta.get("archived"), thread_meta.get("locked"),
    )
    messages: list[Message] = []
    for raw_value in document.get("messages", ()):
        raw = dict(raw_value)
        mid = _id(raw.get("id"))
        raw_channel = raw.get("channelId") or raw.get("channel_id")
        if raw_channel is not None and str(raw_channel) != channel_id:
            raise ValueError(f"message {mid} belongs to another channel")
        author_obj = raw.get("author") or raw.get("user") or {}
        author = User(_id(author_obj), author_obj.get("name") or author_obj.get("username"), author_obj.get("globalName") or author_obj.get("nickname") or author_obj.get("displayName"), bool(author_obj.get("isBot") or author_obj.get("bot")))
        content = str(raw.get("content") or "")
        attachments = tuple(Attachment(
            _stable_id("attachment", mid, i, a), str(a.get("fileName") or a.get("filename") or "attachment"),
            a.get("contentType") or a.get("mediaType"), int(a["size"]) if a.get("size") is not None else None,
            a.get("url"), a.get("proxyUrl") or a.get("proxy_url")) for i, a in enumerate(raw.get("attachments") or ()))
        embeds = tuple(Embed(_stable_id("embed", mid, i, e), i, e.get("type"), e.get("title"), e.get("description"), e.get("url"), dict(e)) for i, e in enumerate(raw.get("embeds") or ()))
        mentions: set[Mention] = set()
        for value in raw.get("mentions") or ():
            mentions.add(Mention(_id(value), "user"))
        for value in raw.get("mentionRoles") or raw.get("mention_roles") or ():
            mentions.add(Mention(_id(value), "role"))
        for value in raw.get("mentionChannels") or raw.get("mention_channels") or ():
            mentions.add(Mention(_id(value), "channel"))
        if raw.get("mentionEveryone") or raw.get("mention_everyone"):
            mentions.add(Mention("everyone", "everyone"))
        ref_obj = raw.get("reference") or raw.get("messageReference") or raw.get("message_reference")
        if not ref_obj and raw.get("replyTo"):
            ref_obj = raw.get("replyTo")
        reference = None
        if isinstance(ref_obj, Mapping):
            reference = MessageReference(
                str(ref_obj.get("messageId") or ref_obj.get("message_id") or ref_obj.get("id")) if ref_obj.get("messageId") or ref_obj.get("message_id") or ref_obj.get("id") else None,
                str(ref_obj.get("channelId") or ref_obj.get("channel_id")) if ref_obj.get("channelId") or ref_obj.get("channel_id") else None,
                str(ref_obj.get("guildId") or ref_obj.get("guild_id")) if ref_obj.get("guildId") or ref_obj.get("guild_id") else None)
        created = _time(raw.get("timestamp") or raw.get("createdAt") or raw.get("created_at"))
        if created is None:
            raise ValueError(f"message {mid} has no timestamp")
        messages.append(Message(mid, guild_id, channel_id, author, created,
            _time(raw.get("timestampEdited") or raw.get("editedTimestamp") or raw.get("edited_at")), content,
            _content_hash(content), raw, attachments, embeds, tuple(sorted(mentions, key=lambda x:(x.mention_type,x.mentioned_id))), reference,
            int(_MESSAGE_TYPES.get(str(raw["type"]), raw["type"])) if raw.get("type") is not None else None,
            int(raw["flags"]) if raw.get("flags") is not None else None, bool(raw.get("isPinned") or raw.get("pinned"))))
    if len({m.message_id for m in messages}) != len(messages):
        raise ValueError("duplicate message ID in export")
    return guild, channel, tuple(messages)


class _JSONStream:
    """Small incremental JSON decoder for DCE's top-level object and message array."""

    def __init__(self, handle: Any, *, chunk_size: int = 65_536):
        self.handle = handle
        self.chunk_size = chunk_size
        self.buffer = ""
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _fill(self) -> bool:
        if self.pos:
            self.buffer = self.buffer[self.pos:]
            self.pos = 0
        chunk = self.handle.read(self.chunk_size)
        if chunk:
            self.buffer += chunk
            return True
        self.eof = True
        return False

    def _space(self) -> None:
        while True:
            while self.pos < len(self.buffer) and self.buffer[self.pos].isspace():
                self.pos += 1
            if self.pos < len(self.buffer) or self.eof:
                return
            self._fill()

    def peek(self) -> str:
        self._space()
        if self.pos >= len(self.buffer):
            raise ValueError("unexpected end of JSON")
        return self.buffer[self.pos]

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise ValueError(f"expected {char!r}")
        self.pos += 1

    def value(self) -> Any:
        while True:
            self._space()
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
            except json.JSONDecodeError:
                if self.eof or not self._fill():
                    raise ValueError("invalid JSON export") from None
                continue
            self.pos = end
            return value


def _iter_export_values(path: Path, *, chunk_size: int = 65_536) -> Iterator[tuple[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        stream = _JSONStream(handle, chunk_size=chunk_size)
        stream.expect("{")
        seen: set[str] = set()
        if stream.peek() == "}":
            stream.expect("}")
            return
        while True:
            key = stream.value()
            if not isinstance(key, str) or key in seen:
                raise ValueError("invalid or duplicate top-level export key")
            seen.add(key)
            stream.expect(":")
            if key == "messages":
                stream.expect("[")
                if stream.peek() != "]":
                    while True:
                        yield key, stream.value()
                        if stream.peek() == "]":
                            break
                        stream.expect(",")
                stream.expect("]")
            else:
                yield key, stream.value()
            if stream.peek() == "}":
                stream.expect("}")
                break
            stream.expect(",")
        stream._space()
        if not stream.eof and stream.pos == len(stream.buffer):
            stream._fill()
            stream._space()
        if stream.pos != len(stream.buffer):
            raise ValueError("trailing JSON data")


def _open_export_stream(path: Path, channel_metadata: Mapping[str, Any] | None
                        ) -> tuple[Guild, Channel, Iterator[Message]]:
    values = iter(_iter_export_values(path))
    metadata: dict[str, Any] = {}
    first_message: Mapping[str, Any] | None = None
    for key, value in values:
        if key == "messages":
            if not isinstance(value, Mapping):
                raise ValueError("message entry is not an object")
            first_message = value
            break
        metadata[key] = value
    if first_message is not None and "messageCount" in metadata:
        raise ValueError("messageCount must follow messages")
    if first_message is None and "messageCount" in metadata:
        count = metadata["messageCount"]
        if isinstance(count, bool) or not isinstance(count, int) or count != 0:
            raise ValueError("messageCount does not match messages")
    guild, channel, _messages = normalize_export(
        {**metadata, "messages": []}, channel_metadata=channel_metadata
    )

    def messages() -> Iterator[Message]:
        yielded = 0
        if first_message is not None:
            yielded += 1
            yield normalize_export({**metadata, "messages": [first_message]},
                                   channel_metadata=channel_metadata)[2][0]
        for key, value in values:
            if key == "messages":
                if not isinstance(value, Mapping):
                    raise ValueError("message entry is not an object")
                yielded += 1
                yield normalize_export({**metadata, "messages": [value]},
                                       channel_metadata=channel_metadata)[2][0]
            elif key == "messageCount":
                if isinstance(value, bool) or not isinstance(value, int) or value != yielded:
                    raise ValueError("messageCount does not match messages")
            else:
                raise ValueError("metadata after messages is unsupported")
    return guild, channel, messages()


def load_export(
    source: str | Path | Mapping[str, Any], *, channel_metadata: Mapping[str, Any] | None = None
) -> tuple[Guild, Channel, tuple[Message, ...]]:
    if isinstance(source, Mapping):
        return normalize_export(source, channel_metadata=channel_metadata)
    path = Path(source)
    guild, channel, stream = _open_export_stream(path, channel_metadata)
    messages = tuple(stream)
    if len({message.message_id for message in messages}) != len(messages):
        raise ValueError("duplicate message ID in export")
    return guild, channel, messages


def record_inventory_manifests(conn: Any, run_id: UUID, parent_channel_id: str,
                               endpoints: Sequence[Mapping[str, Any]],
                               parent_union: Mapping[str, Any]) -> None:
    """Persist endpoint pages and their final parent union atomically."""
    with conn.transaction():
        for endpoint in endpoints:
            name, pages = str(endpoint["endpoint"]), list(endpoint.get("pages", ()))
            endpoint_ids = endpoint.get("endpoint_thread_ids", endpoint.get("thread_ids", ()))
            union_ids = endpoint.get("global_union_ids_after_endpoint", endpoint.get("global_union_ids", ()))
            page_count = int(endpoint.get("page_count", len(pages)))
            conn.execute("INSERT INTO discord_archive.inventory_endpoint_manifests(run_id,parent_channel_id,endpoint,state,page_count,final_cursor,endpoint_thread_ids,global_union_ids_after_endpoint,termination_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,parent_channel_id,endpoint) DO UPDATE SET state=EXCLUDED.state,page_count=EXCLUDED.page_count,final_cursor=EXCLUDED.final_cursor,endpoint_thread_ids=EXCLUDED.endpoint_thread_ids,global_union_ids_after_endpoint=EXCLUDED.global_union_ids_after_endpoint,termination_reason=EXCLUDED.termination_reason", (run_id,parent_channel_id,name,endpoint["state"],page_count,endpoint.get("final_cursor"),list(endpoint_ids),list(union_ids),endpoint["termination_reason"]))
            conn.execute("DELETE FROM discord_archive.inventory_pages WHERE run_id=%s AND parent_channel_id=%s AND endpoint=%s", (run_id,parent_channel_id,name))
            for page_no, page in enumerate(pages, 1):
                fingerprint = page.get("page_fingerprint", page.get("fingerprint"))
                raw_ids = page.get("raw_thread_ids", page.get("thread_ids", ()))
                conn.execute("INSERT INTO discord_archive.inventory_pages(run_id,parent_channel_id,endpoint,page_no,request_cursor,response_cursor,has_more,page_fingerprint,raw_thread_ids) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)", (run_id,parent_channel_id,name,page_no,page.get("request_cursor"),page.get("response_cursor"),bool(page["has_more"]),fingerprint,list(raw_ids)))
        conn.execute("INSERT INTO discord_archive.inventory_parent_unions(run_id,parent_channel_id,state,active_thread_ids,archived_thread_ids,all_thread_ids,termination_reason) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,parent_channel_id) DO UPDATE SET state=EXCLUDED.state,active_thread_ids=EXCLUDED.active_thread_ids,archived_thread_ids=EXCLUDED.archived_thread_ids,all_thread_ids=EXCLUDED.all_thread_ids,termination_reason=EXCLUDED.termination_reason", (run_id,parent_channel_id,parent_union["state"],list(parent_union.get("active_thread_ids",())),list(parent_union.get("archived_thread_ids",())),list(parent_union.get("all_thread_ids",())),parent_union["termination_reason"]))


def import_export(conn: Any, source: str | Path | Mapping[str, Any], *, mode: str = "incremental",
                  dce_version: str = "unknown", observed_at: datetime | None = None,
                  run_id: UUID | None = None, complete: bool = False,
                  source_after: datetime | None = None, source_before: datetime | None = None,
                  channel_metadata: Mapping[str, Any] | None = None,
                  inventory_run_id: UUID | str | None = None) -> dict[str, Any]:
    if mode not in {"backfill", "incremental", "reconcile"}:
        raise ValueError("invalid import mode")
    if mode == "reconcile" and complete and inventory_run_id is None:
        raise ValueError("complete_reconcile_requires_inventory")
    if mode == "reconcile" and complete and source_before is None:
        raise ValueError("complete_reconcile_requires_cutoff")
    if isinstance(source, Mapping):
        guild, channel, normalized = normalize_export(source, channel_metadata=channel_metadata)
        expected_count: int | None = len(normalized)
        message_stream: Iterable[Message] = iter(normalized)
    else:
        path = Path(source)
        guild, channel, message_stream = _open_export_stream(path, channel_metadata)
        expected_count = None
    observed = observed_at or datetime.now(timezone.utc)
    rid = run_id or uuid4()
    inserted = updated = tombstoned = exported = 0
    seen_ids: set[str] = set()
    exported_ids: list[str] = []
    newest: Message | None = None
    oldest_created_at: datetime | None = None
    try:
        with conn.transaction():
            conn.execute("INSERT INTO discord_archive.ingest_runs(run_id,channel_id,mode,dce_version,started_at,status,source_after,source_before,inventory_run_id) VALUES(%s,%s,%s,%s,%s,'running',%s,%s,%s)", (rid,channel.channel_id,mode,dce_version,observed,source_after,source_before,inventory_run_id))
            conn.execute("INSERT INTO discord_archive.guilds(guild_id,name,icon_url,last_observed_at) VALUES(%s,%s,%s,%s) ON CONFLICT(guild_id) DO UPDATE SET name=EXCLUDED.name,icon_url=EXCLUDED.icon_url,last_observed_at=EXCLUDED.last_observed_at", (guild.guild_id,guild.name,guild.icon_url,observed))
            conn.execute("INSERT INTO discord_archive.channels(channel_id,guild_id,parent_channel_id,channel_type,name,topic,is_thread,archived,locked,last_observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(channel_id) DO UPDATE SET parent_channel_id=EXCLUDED.parent_channel_id,channel_type=EXCLUDED.channel_type,name=EXCLUDED.name,topic=EXCLUDED.topic,is_thread=EXCLUDED.is_thread,archived=EXCLUDED.archived,locked=EXCLUDED.locked,last_observed_at=EXCLUDED.last_observed_at", (channel.channel_id,channel.guild_id,channel.parent_channel_id,channel.channel_type,channel.name,channel.topic,channel.is_thread,channel.archived,channel.locked,observed))
            conn.execute("INSERT INTO discord_archive.ingest_run_scope(run_id,channel_id,channel_kind,inventory_observed_at,inventory_state,export_state,export_after,export_before,exported_count) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)", (rid,channel.channel_id,"archived_thread" if channel.is_thread and channel.archived else "active_thread" if channel.is_thread else "channel",observed,"complete" if complete else "expected","pending" if expected_count is None else "empty" if expected_count == 0 else "ok",source_after,source_before,expected_count or 0))
            for message in message_stream:
                if message.message_id in seen_ids:
                    raise ValueError("duplicate message ID in export")
                seen_ids.add(message.message_id)
                exported_ids.append(message.message_id)
                exported += 1
                if newest is None or (message.created_at, message.message_id) > (newest.created_at, newest.message_id):
                    newest = message
                if oldest_created_at is None or message.created_at < oldest_created_at:
                    oldest_created_at = message.created_at
                conn.execute("INSERT INTO discord_archive.users(user_id,username,global_name,is_bot,last_observed_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,global_name=EXCLUDED.global_name,is_bot=EXCLUDED.is_bot,last_observed_at=EXCLUDED.last_observed_at", (message.author.user_id,message.author.username,message.author.global_name,message.author.is_bot,observed))
                old = conn.execute("SELECT content_hash,source_observed_at,source_priority,deleted_at,raw_json FROM discord_archive.messages WHERE message_id=%s FOR UPDATE", (message.message_id,)).fetchone()
                wins = old is None or observed > old[1] or (observed == old[1] and 10 > old[2])
                if old is not None and old[0] == message.content_hash and old[3] is None and old[4] == message.raw:
                    wins = False
                if old is None:
                    conn.execute("INSERT INTO discord_archive.messages(message_id,guild_id,channel_id,author_id,created_at,edited_at,content,reply_to_message_id,message_type,flags,is_pinned,has_attachments,author_name_snapshot,channel_name_snapshot,raw_json,content_hash,source_priority,source_observed_at,first_ingest_run_id,last_ingest_run_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,10,%s,%s,%s)", (message.message_id,message.guild_id,message.channel_id,message.author.user_id,message.created_at,message.edited_at,message.content,message.reference.message_id if message.reference else None,message.message_type,message.flags,message.is_pinned,bool(message.attachments),message.author.global_name or message.author.username,channel.name,Jsonb(message.raw),message.content_hash,observed,rid,rid))
                    conn.execute("INSERT INTO discord_archive.message_revisions(message_id,revision_no,content,content_hash,observed_at,ingest_run_id) VALUES(%s,1,%s,%s,%s,%s)", (message.message_id,message.content,message.content_hash,observed,rid)); inserted += 1
                elif wins:
                    if old[0] != message.content_hash:
                        conn.execute("INSERT INTO discord_archive.message_revisions(message_id,revision_no,content,content_hash,observed_at,ingest_run_id) SELECT %s,COALESCE(MAX(revision_no),0)+1,%s,%s,%s,%s FROM discord_archive.message_revisions WHERE message_id=%s", (message.message_id,message.content,message.content_hash,observed,rid,message.message_id))
                    conn.execute("UPDATE discord_archive.messages SET author_id=%s,created_at=%s,edited_at=%s,deleted_at=NULL,content=%s,reply_to_message_id=%s,message_type=%s,flags=%s,is_pinned=%s,has_attachments=%s,author_name_snapshot=%s,channel_name_snapshot=%s,raw_json=%s,content_hash=%s,source_priority=10,source_observed_at=%s,last_ingest_run_id=%s WHERE message_id=%s", (message.author.user_id,message.created_at,message.edited_at,message.content,message.reference.message_id if message.reference else None,message.message_type,message.flags,message.is_pinned,bool(message.attachments),message.author.global_name or message.author.username,channel.name,Jsonb(message.raw),message.content_hash,observed,rid,message.message_id)); updated += 1
                if not wins: continue
                conn.execute("DELETE FROM discord_archive.attachments WHERE message_id=%s", (message.message_id,))
                for a in message.attachments: conn.execute("INSERT INTO discord_archive.attachments(attachment_id,message_id,filename,media_type,size_bytes,url,proxy_url) VALUES(%s,%s,%s,%s,%s,%s,%s)", (a.attachment_id,message.message_id,a.filename,a.media_type,a.size_bytes,a.url,a.proxy_url))
                conn.execute("DELETE FROM discord_archive.embeds WHERE message_id=%s", (message.message_id,))
                for e in message.embeds: conn.execute("INSERT INTO discord_archive.embeds(embed_id,message_id,ordinal,embed_type,title,description,url,raw_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (e.embed_id,message.message_id,e.ordinal,e.embed_type,e.title,e.description,e.url,Jsonb(e.raw)))
                conn.execute("DELETE FROM discord_archive.message_mentions WHERE message_id=%s", (message.message_id,))
                for mention in message.mentions: conn.execute("INSERT INTO discord_archive.message_mentions(message_id,mentioned_id,mention_type) VALUES(%s,%s,%s)", (message.message_id,mention.mentioned_id,mention.mention_type))
                conn.execute("DELETE FROM discord_archive.message_references WHERE message_id=%s", (message.message_id,))
                if message.reference: conn.execute("INSERT INTO discord_archive.message_references(message_id,referenced_message_id,referenced_channel_id,referenced_guild_id) VALUES(%s,%s,%s,%s)", (message.message_id,message.reference.message_id,message.reference.channel_id,message.reference.guild_id))
            if expected_count is not None and exported != expected_count:
                raise ValueError("message count changed while reading export")
            if mode == "reconcile" and complete:
                row = conn.execute("UPDATE discord_archive.messages SET deleted_at=%s,last_ingest_run_id=%s WHERE channel_id=%s AND deleted_at IS NULL AND created_at<%s AND NOT(message_id=ANY(%s))", (observed,rid,channel.channel_id,source_before,exported_ids)).rowcount
                tombstoned = max(row,0)
            conn.execute("UPDATE discord_archive.ingest_run_scope SET export_state=%s,exported_count=%s WHERE run_id=%s AND channel_id=%s", ("empty" if exported == 0 else "ok",exported,rid,channel.channel_id))
            coverage = "complete" if complete and mode in {"backfill", "reconcile"} else "partial"
            conn.execute("INSERT INTO discord_archive.ingest_cursors(channel_id,newest_message_id,newest_created_at,last_incremental_at,last_reconciled_at,coverage_start,coverage_end,coverage_state,last_run_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(channel_id) DO UPDATE SET newest_message_id=CASE WHEN discord_archive.ingest_cursors.newest_created_at IS NULL OR EXCLUDED.newest_created_at>=discord_archive.ingest_cursors.newest_created_at THEN EXCLUDED.newest_message_id ELSE discord_archive.ingest_cursors.newest_message_id END,newest_created_at=GREATEST(discord_archive.ingest_cursors.newest_created_at,EXCLUDED.newest_created_at),last_incremental_at=EXCLUDED.last_incremental_at,last_reconciled_at=COALESCE(EXCLUDED.last_reconciled_at,discord_archive.ingest_cursors.last_reconciled_at),coverage_start=CASE WHEN EXCLUDED.coverage_state='complete' THEN EXCLUDED.coverage_start ELSE discord_archive.ingest_cursors.coverage_start END,coverage_end=EXCLUDED.coverage_end,coverage_state=CASE WHEN discord_archive.ingest_cursors.coverage_state='complete' AND EXCLUDED.coverage_state='partial' THEN 'complete' ELSE EXCLUDED.coverage_state END,last_run_id=EXCLUDED.last_run_id", (channel.channel_id,newest.message_id if newest else None,newest.created_at if newest else None,observed,observed if mode=="reconcile" and complete else None,source_after or oldest_created_at,source_before or observed,coverage,rid))
            conn.execute("UPDATE discord_archive.ingest_runs SET finished_at=%s,status='ok',exported_count=%s,inserted_count=%s,updated_count=%s,tombstoned_count=%s WHERE run_id=%s", (observed,exported,inserted,updated,tombstoned,rid))
    except Exception:
        conn.rollback(); raise
    return {"run_id":str(rid),"exported":exported,"inserted":inserted,"updated":updated,"tombstoned":tombstoned}
