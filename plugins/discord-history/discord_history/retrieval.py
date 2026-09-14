from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .auth import AuthorizedScope

MAX_JSON_BYTES = 100_000
MAX_RECORDS = 500
MAX_SNIPPET = 320
MAX_RESOLVED_CHANNELS = 1_000
MESSAGE_SELECT = """m.message_id,m.guild_id,m.channel_id,m.author_id,m.created_at,
    m.edited_at,left(m.content,4096) AS content,m.message_type,m.flags,m.is_pinned,
    m.has_attachments,m.author_name_snapshot,m.reply_to_message_id"""


class RetrievalValidationError(ValueError):
    pass


def _snowflake(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{17,20}", value) is None:
        raise RetrievalValidationError(f"invalid_{field}")
    return value


def _ids(values: Any, field: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    if not isinstance(values, list) or not values or len(values) > 100:
        raise RetrievalValidationError(f"invalid_{field}")
    result = tuple(_snowflake(v, field) for v in values)
    if len(set(result)) != len(result):
        raise RetrievalValidationError(f"invalid_{field}")
    return result


def _names(values: Any, field: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    if not isinstance(values, list) or not values or len(values) > 50:
        raise RetrievalValidationError(f"invalid_{field}")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not (term := value.strip()) or len(term) > 100:
            raise RetrievalValidationError(f"invalid_{field}")
        result.append(term)
    return tuple(result)


def _time(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise RetrievalValidationError(f"invalid_{field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrievalValidationError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise RetrievalValidationError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def _bounded_int(value: Any, field: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RetrievalValidationError(f"invalid_{field}")
    return value


@dataclass(frozen=True)
class Request:
    action: str
    guild_id: str
    query: str | None
    message_id: str | None
    channel_ids: tuple[str, ...] | None
    channel_names: tuple[str, ...] | None
    author_ids: tuple[str, ...] | None
    author_names: tuple[str, ...] | None
    after: datetime | None
    before: datetime | None
    limit: int
    context_before: int
    context_after: int

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> "Request":
        if not isinstance(data, Mapping):
            raise RetrievalValidationError("invalid_arguments")
        action = data.get("action")
        if action not in {"search", "get", "context", "status"}:
            raise RetrievalValidationError("invalid_action")
        allowed_by_action = {
            "search": {"action", "guild_id", "query", "channel_ids", "channel_names",
                       "author_ids", "author_names", "after", "before", "limit",
                       "context_before", "context_after"},
            "get": {"action", "guild_id", "message_id"},
            "context": {"action", "guild_id", "message_id", "context_before", "context_after"},
            "status": {"action", "guild_id", "channel_ids", "channel_names"},
        }
        if set(data) - allowed_by_action[action]:
            raise RetrievalValidationError("invalid_arguments")
        guild_id = _snowflake(data.get("guild_id"), "guild_id")
        query = data.get("query")
        if query is not None:
            if not isinstance(query, str) or not (query := query.strip()) or len(query) > 500:
                raise RetrievalValidationError("invalid_query")
        message_id = data.get("message_id")
        if message_id is not None:
            message_id = _snowflake(message_id, "message_id")
        limit = _bounded_int(data.get("limit"), "limit", 10, 1, 50)
        cb = _bounded_int(data.get("context_before"), "context_before", 3, 0, 20)
        ca = _bounded_int(data.get("context_after"), "context_after", 3, 0, 20)
        after, before = _time(data.get("after"), "after"), _time(data.get("before"), "before")
        if after and before and after >= before:
            raise RetrievalValidationError("invalid_time_range")
        if action == "search" and query is None:
            raise RetrievalValidationError("query_required")
        if action in {"get", "context"} and message_id is None:
            raise RetrievalValidationError("message_id_required")

        return cls(action, guild_id, query, message_id, _ids(data.get("channel_ids"), "channel_ids"),
                   _names(data.get("channel_names"), "channel_names"), _ids(data.get("author_ids"), "author_ids"),
                   _names(data.get("author_names"), "author_names"), after, before, limit, cb, ca)


def _rows(result: Any) -> list[dict[str, Any]]:
    rows = result.fetchall() if hasattr(result, "fetchall") else result
    if rows is None:
        return []
    if rows and not isinstance(rows[0], Mapping):
        columns = [getattr(c, "name", c[0]) for c in result.description]
        return [dict(zip(columns, row)) for row in rows]
    return [dict(row) for row in rows]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _snippet(content: str, query: str | None) -> str:
    content = content or ""
    if len(content) <= MAX_SNIPPET:
        return content
    at = content.casefold().find((query or "").casefold()) if query else 0
    at = max(at, 0)
    start = max(0, min(len(content) - MAX_SNIPPET, at - MAX_SNIPPET // 2))
    return content[start:start + MAX_SNIPPET]


class RetrievalService:
    """Read-only, scope-constrained PostgreSQL retrieval. Authorization is supplied pre-DB."""

    def __init__(self, connector: Callable[[str], Any], audit_hmac_key: bytes):
        if not isinstance(audit_hmac_key, bytes) or len(audit_hmac_key) != 32:
            raise ValueError("invalid_audit_hmac_key")
        self.connector = connector
        self.audit_hmac_key = audit_hmac_key

    def run(self, dsn: str, scope: AuthorizedScope, request: Request) -> dict[str, Any]:
        conn = self.connector(dsn)
        result_ids: list[str] = []
        channel_ids: tuple[str, ...] = ()
        author_ids: tuple[str, ...] | None = None
        outcome = "error"
        try:
            try:
                channel_ids = self._resolve_channels(conn, scope, request)
                author_ids = self._resolve_authors(conn, request)
                coverage = self._scope_coverage(conn, scope, channel_ids)
                if request.action == "search":
                    payload = self._search(conn, scope, request, channel_ids, author_ids, coverage)
                elif request.action == "get":
                    payload = self._get(conn, scope, request, channel_ids, coverage)
                elif request.action == "context":
                    payload = self._context(conn, scope, request, channel_ids, coverage)
                else:
                    payload = self._status(conn, scope, request, channel_ids)
                bounded = self._bound(payload)
                result_ids = [str(row["message_id"]) for row in bounded.get("results", [])
                              if "message_id" in row]
            except Exception:
                # A failed PostgreSQL statement aborts its transaction. Roll it back
                # before recording the authorized, database-reaching failed attempt.
                if hasattr(conn, "rollback"):
                    conn.rollback()
                self._audit(conn, scope, request, result_ids, outcome,
                            channel_ids, author_ids)
                if hasattr(conn, "commit"):
                    conn.commit()
                raise
            self._audit(conn, scope, request, result_ids, "ok",
                        channel_ids, author_ids)
            if hasattr(conn, "commit"):
                conn.commit()
            return bounded
        finally:
            if hasattr(conn, "close"):
                conn.close()

    def _resolve_channels(self, conn: Any, scope: AuthorizedScope, request: Request) -> tuple[str, ...]:
        requested = set(request.channel_ids or ())
        roots = scope.root_channel_ids or scope.channel_ids
        params: list[Any] = [scope.guild_id, list(roots), list(roots)]
        clauses = ["guild_id = %s", "(channel_id = ANY(%s) OR parent_channel_id = ANY(%s))"]
        if requested:
            clauses.append("(channel_id = ANY(%s) OR parent_channel_id = ANY(%s))")
            params.extend((sorted(requested), sorted(requested)))
        if request.channel_names:
            names = [n.casefold() for n in request.channel_names]
            exact = _rows(conn.execute(
                "/* resolve_channels_exact */ SELECT channel_id FROM discord_archive.channels WHERE "
                + " AND ".join(clauses)
                + " AND lower(name) = ANY(%s) LIMIT 1001",
                [*params, names],
            ))
            if len(exact) > MAX_RESOLVED_CHANNELS:
                raise RetrievalValidationError("scope_too_large")
            if exact:
                return tuple(sorted({str(row["channel_id"]) for row in exact}))
            clauses.append(
                "EXISTS (SELECT 1 FROM unnest(%s::text[]) AS requested_name "
                "WHERE lower(name) = requested_name OR similarity(lower(name), requested_name) >= 0.4)"
            )
            params.append(names)
        result = conn.execute(
            "/* resolve_channels */ SELECT channel_id FROM discord_archive.channels WHERE "
            + " AND ".join(clauses) + " LIMIT 1001",
            params,
        )
        resolved = {str(r["channel_id"]) for r in _rows(result)}
        if len(resolved) > MAX_RESOLVED_CHANNELS:
            raise RetrievalValidationError("scope_too_large")
        if requested and not requested.issubset(scope.channel_ids):
            raise RetrievalValidationError("scope_denied")
        return tuple(sorted(resolved))

    def _resolve_authors(self, conn: Any, request: Request) -> tuple[str, ...] | None:
        requested = set(request.author_ids or ())
        if request.author_names:
            names = [n.casefold() for n in request.author_names]
            result = conn.execute("""/* resolve_authors */ SELECT user_id FROM discord_archive.users
                WHERE EXISTS (
                    SELECT 1 FROM unnest(%s::text[]) AS requested_name
                    WHERE lower(coalesce(username, '')) = requested_name
                       OR lower(coalesce(global_name, '')) = requested_name
                       OR similarity(lower(coalesce(username, '')), requested_name) >= 0.4
                       OR similarity(lower(coalesce(global_name, '')), requested_name) >= 0.4
                ) LIMIT 50""", (names,))
            requested.update(str(r["user_id"]) for r in _rows(result))
        return tuple(sorted(requested)) if requested else (None if not request.author_names else ())

    @staticmethod
    def _filters(scope: AuthorizedScope, channels: Sequence[str], request: Request, authors: Sequence[str] | None = None) -> tuple[str, list[Any]]:
        clauses = ["m.deleted_at IS NULL", "m.guild_id = %s", "m.channel_id = ANY(%s)"]
        params: list[Any] = [scope.guild_id, list(channels)]
        if authors is not None:
            clauses.append("m.author_id = ANY(%s)"); params.append(list(authors))
        if request.after:
            clauses.append("m.created_at >= %s"); params.append(request.after)
        if request.before:
            clauses.append("m.created_at < %s"); params.append(request.before)
        return " AND ".join(clauses), params

    def _scope_coverage(self, conn: Any, scope: AuthorizedScope,
                        channels: Sequence[str]) -> dict[str, Any]:
        rows = _rows(conn.execute("""/* scope_coverage */
            SELECT c.channel_id,ic.coverage_state,ic.coverage_start,ic.coverage_end
            FROM discord_archive.channels c
            LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=c.channel_id
            WHERE c.guild_id=%s AND c.channel_id=ANY(%s) ORDER BY c.channel_id LIMIT 1001""",
            (scope.guild_id, list(channels)))) if channels else []
        if len(rows) > MAX_RESOLVED_CHANNELS:
            raise RetrievalValidationError("scope_too_large")
        states: dict[str, int] = {}
        for row in rows:
            state = str(row.get("coverage_state") or "unknown")
            states[state] = states.get(state, 0) + 1
        channel_rows = self._coverage(rows)
        return {"channel_count": len(channel_rows), "state_counts": states,
                "channels": channel_rows[:50],
                "channels_truncated": len(channel_rows) > 50}

    def _search(self, conn: Any, scope: AuthorizedScope, request: Request,
                channels: Sequence[str], authors: Sequence[str] | None,
                coverage: dict[str, Any]) -> dict[str, Any]:
        if not channels or authors == ():
            rows: list[dict[str, Any]] = []
        else:
            where, params = self._filters(scope, channels, request, authors)
            rows = _rows(conn.execute(f"""/* search_fts */ SELECT {MESSAGE_SELECT}, u.username, u.global_name, c.name AS channel_name,(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name,
                c.parent_channel_id, c.is_thread, g.name AS guild_name, ic.coverage_start, ic.coverage_end, ic.coverage_state,
                CASE WHEN lower(m.content)=lower(%s) THEN 100.0
                     WHEN position(lower(%s) in lower(m.content))>0
                     THEN 10.0 + 1.0/(1+char_length(m.content))
                     ELSE ts_rank_cd(m.content_tsv, websearch_to_tsquery('simple', %s)) END AS score
                FROM discord_archive.messages m JOIN discord_archive.users u ON u.user_id=m.author_id
                JOIN discord_archive.channels c ON c.channel_id=m.channel_id JOIN discord_archive.guilds g ON g.guild_id=m.guild_id
                LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=m.channel_id
                WHERE {where} AND m.content_tsv @@ websearch_to_tsquery('simple', %s)
                ORDER BY score DESC, m.created_at DESC, m.message_id DESC LIMIT 100""",
                [request.query, request.query, request.query, *params, request.query]))
            if len(rows) < 10:
                seen = {str(r["message_id"]) for r in rows}
                trigram = _rows(conn.execute(f"""/* search_trigram */ SELECT {MESSAGE_SELECT}, u.username, u.global_name, c.name AS channel_name,(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name,
                    c.parent_channel_id, c.is_thread, g.name AS guild_name, ic.coverage_start, ic.coverage_end, ic.coverage_state,
                    CASE WHEN lower(m.content)=lower(%s) THEN 100.0
                         WHEN position(lower(%s) in lower(m.content))>0
                         THEN 10.0 + 1.0/(1+char_length(m.content))
                         ELSE similarity(m.content, %s) END AS score FROM discord_archive.messages m
                    JOIN discord_archive.users u ON u.user_id=m.author_id JOIN discord_archive.channels c ON c.channel_id=m.channel_id
                    JOIN discord_archive.guilds g ON g.guild_id=m.guild_id LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=m.channel_id
                    WHERE {where} AND similarity(m.content, %s) >= %s
                    ORDER BY score DESC, m.created_at DESC, m.message_id DESC LIMIT 50""",
                    [request.query, request.query, request.query, *params, request.query, 0.25]))
                for row in trigram:
                    if str(row["message_id"]) not in seen:
                        row["match_type"] = "trigram"; rows.append(row); seen.add(str(row["message_id"]))
            rows.sort(key=lambda r: (-float(r.get("score") or 0), -_epoch(r.get("created_at")), str(r["message_id"])), reverse=False)
            rows = rows[:request.limit]
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        records_by_id: dict[str, dict[str, Any]] = {}
        # Register every ranked hit before adding context so an overlapping hit
        # can never be mislabeled or evicted as context.
        for row in rows:
            message_id = str(row["message_id"])
            if message_id in seen:
                continue
            record = self._record(row, request.query, row.get("match_type", "fts"))
            results.append(record)
            records_by_id[message_id] = record
            seen.add(message_id)
        if request.context_before or request.context_after:
            for row in rows:
                anchor_id = str(row["message_id"])
                for neighbour in self._neighbour_rows(conn, scope, row,
                                                       request.context_before,
                                                       request.context_after):
                    neighbour_id = str(neighbour["message_id"])
                    distance = abs(
                        _epoch(neighbour.get("created_at")) - _epoch(row.get("created_at"))
                    )
                    if neighbour_id not in seen:
                        record = self._record(neighbour, None, "context")
                        record["_context_distance"] = distance
                        record["_context_anchor_id"] = anchor_id
                        results.append(record)
                        records_by_id[neighbour_id] = record
                        seen.add(neighbour_id)
                    elif records_by_id[neighbour_id].get("match_type") == "context":
                        existing = records_by_id[neighbour_id]
                        if distance < float(existing.get("_context_distance") or 0):
                            existing["_context_distance"] = distance
                            existing["_context_anchor_id"] = anchor_id
        return {"action": "search", "results": results, "coverage": coverage,
                "truncated": False, "omitted_context_count": 0,
                "omitted_result_count": 0}

    def _get(self, conn: Any, scope: AuthorizedScope, request: Request,
             channels: Sequence[str], coverage: dict[str, Any]) -> dict[str, Any]:
        rows = self._message_rows(conn, scope, channels, request.message_id)
        results: list[dict[str, Any]] = []
        if rows:
            record = self._record(rows[0], None, "exact_id")
            revisions = _rows(conn.execute("""/* revisions */
                SELECT revision_no,content_hash,left(content,4096) AS content,observed_at
                FROM discord_archive.message_revisions WHERE message_id=%s
                ORDER BY revision_no DESC LIMIT 20""", (request.message_id,)))
            record["revisions"] = [{"revision_no": int(revision["revision_no"]),
                                    "content_hash": str(revision["content_hash"]),
                                    "observed_at": _iso(revision.get("observed_at")),
                                    "snippet": _snippet(str(revision.get("content") or ""), None)}
                                   for revision in reversed(revisions)]
            results.append(record)
        return {"action": "get", "results": results, "coverage": coverage,
                "truncated": False, "omitted_context_count": 0,
                "omitted_result_count": 0}

    def _message_rows(self, conn: Any, scope: AuthorizedScope, channels: Sequence[str], message_id: str | None) -> list[dict[str, Any]]:
        return _rows(conn.execute(f"""/* get_message */ SELECT {MESSAGE_SELECT}, u.username, u.global_name, c.name AS channel_name,(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name,
            c.parent_channel_id, c.is_thread, g.name AS guild_name, ic.coverage_start, ic.coverage_end, ic.coverage_state
            FROM discord_archive.messages m JOIN discord_archive.users u ON u.user_id=m.author_id
            JOIN discord_archive.channels c ON c.channel_id=m.channel_id JOIN discord_archive.guilds g ON g.guild_id=m.guild_id
            LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=m.channel_id
            WHERE m.deleted_at IS NULL AND m.guild_id=%s AND m.channel_id=ANY(%s) AND m.message_id=%s LIMIT 1""",
            (scope.guild_id, list(channels), message_id))) if channels else []

    def _neighbour_rows(self, conn: Any, scope: AuthorizedScope,
                        row: Mapping[str, Any], before: int,
                        after: int) -> list[dict[str, Any]]:
        return _rows(conn.execute(f"""/* context */ (SELECT {MESSAGE_SELECT}, u.username, u.global_name, c.name AS channel_name,(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name,
            c.parent_channel_id, c.is_thread, g.name AS guild_name, ic.coverage_start, ic.coverage_end, ic.coverage_state
            FROM discord_archive.messages m JOIN discord_archive.users u ON u.user_id=m.author_id JOIN discord_archive.channels c ON c.channel_id=m.channel_id
            JOIN discord_archive.guilds g ON g.guild_id=m.guild_id LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=m.channel_id
            WHERE m.deleted_at IS NULL AND m.guild_id=%s AND m.channel_id=%s AND (m.created_at,m.message_id)<(%s,%s)
            ORDER BY m.created_at DESC,m.message_id DESC LIMIT %s) UNION ALL
            (SELECT {MESSAGE_SELECT}, u.username, u.global_name, c.name AS channel_name,(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name, c.parent_channel_id, c.is_thread, g.name AS guild_name,
            ic.coverage_start, ic.coverage_end, ic.coverage_state FROM discord_archive.messages m JOIN discord_archive.users u ON u.user_id=m.author_id
            JOIN discord_archive.channels c ON c.channel_id=m.channel_id JOIN discord_archive.guilds g ON g.guild_id=m.guild_id
            LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=m.channel_id
            WHERE m.deleted_at IS NULL AND m.guild_id=%s AND m.channel_id=%s AND (m.created_at,m.message_id)>(%s,%s)
            ORDER BY m.created_at,m.message_id LIMIT %s)""",
            (scope.guild_id, row["channel_id"], row["created_at"], row["message_id"], before,
             scope.guild_id, row["channel_id"], row["created_at"], row["message_id"], after)))

    def _context(self, conn: Any, scope: AuthorizedScope, request: Request,
                 channels: Sequence[str], coverage: dict[str, Any]) -> dict[str, Any]:
        hit = self._message_rows(conn, scope, channels, request.message_id)
        if not hit:
            return {"action": "context", "results": [], "coverage": coverage,
                    "truncated": False, "omitted_context_count": 0,
                    "omitted_result_count": 0}
        row = hit[0]
        neighbours = self._neighbour_rows(conn, scope, row,
                                           request.context_before,
                                           request.context_after)
        combined = sorted([*neighbours, row], key=lambda r: (_epoch(r.get("created_at")), str(r["message_id"])))
        records: list[dict[str, Any]] = []
        for item in combined:
            is_hit = str(item["message_id"]) == request.message_id
            record = self._record(item, None, "exact_id" if is_hit else "context")
            if not is_hit:
                record["_context_distance"] = abs(
                    _epoch(item.get("created_at")) - _epoch(row.get("created_at"))
                )
                record["_context_anchor_id"] = str(row["message_id"])
            records.append(record)
        return {"action": "context", "results": records, "coverage": coverage,
                "truncated": False, "omitted_context_count": 0,
                "omitted_result_count": 0}

    def _status(self, conn: Any, scope: AuthorizedScope, request: Request, channels: Sequence[str]) -> dict[str, Any]:
        rows = _rows(conn.execute("""/* status */ SELECT c.channel_id,c.parent_channel_id,c.name AS channel_name,(SELECT pc.name FROM discord_archive.channels pc WHERE pc.channel_id=c.parent_channel_id) AS parent_channel_name,ic.coverage_start,ic.coverage_end,
            ic.coverage_state,ic.last_incremental_at,ic.last_reconciled_at,
            max(m.created_at) AS newest_message_at,count(m.message_id) AS live_message_count,
            ok_run.finished_at AS last_successful_run,err_run.error_code AS last_error_code,
            greatest(0,extract(epoch FROM (now()-coalesce(ic.coverage_end,max(m.created_at),now())))::bigint) AS lag_seconds,
            (ic.coverage_end IS NULL OR ic.coverage_end < now()-interval '48 hours') AS stale
            FROM discord_archive.channels c
            LEFT JOIN discord_archive.ingest_cursors ic ON ic.channel_id=c.channel_id
            LEFT JOIN discord_archive.messages m ON m.channel_id=c.channel_id AND m.guild_id=%s AND m.deleted_at IS NULL
            LEFT JOIN LATERAL (SELECT finished_at FROM discord_archive.ingest_runs r
                WHERE r.channel_id=c.channel_id AND r.status='ok' ORDER BY r.finished_at DESC NULLS LAST LIMIT 1) ok_run ON true
            LEFT JOIN LATERAL (SELECT error_code FROM discord_archive.ingest_runs r
                WHERE r.channel_id=c.channel_id AND r.status='error' ORDER BY r.finished_at DESC NULLS LAST LIMIT 1) err_run ON true
            WHERE c.guild_id=%s AND c.channel_id=ANY(%s)
            GROUP BY c.channel_id,c.parent_channel_id,c.name,ic.coverage_start,ic.coverage_end,ic.coverage_state,
                ic.last_incremental_at,ic.last_reconciled_at,ok_run.finished_at,err_run.error_code
            ORDER BY c.channel_id LIMIT 1001""", (scope.guild_id, scope.guild_id, list(channels)))) if channels else []
        if len(rows) > MAX_RESOLVED_CHANNELS:
            raise RetrievalValidationError("scope_too_large")
        for row in rows:
            for key in ("coverage_start", "coverage_end", "last_incremental_at", "last_reconciled_at", "newest_message_at", "last_successful_run"):
                row[key] = _iso(row.get(key))
        return {"action": "status", "channels": rows, "truncated": False}

    @staticmethod
    def _coverage(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            cid = str(row.get("channel_id", ""))
            if cid and cid not in found:
                found[cid] = {"channel_id": cid, "state": row.get("coverage_state") or "unknown", "start": _iso(row.get("coverage_start")), "end": _iso(row.get("coverage_end"))}
        return [found[k] for k in sorted(found)]

    @staticmethod
    def _record(row: Mapping[str, Any], query: str | None, match_type: str) -> dict[str, Any]:
        guild, channel, message = str(row["guild_id"]), str(row["channel_id"]), str(row["message_id"])
        return {"message_id": message, "guild_id": guild, "guild_name": row.get("guild_name"), "channel_id": channel,
                "channel_name": row.get("channel_name"), "thread_id": channel if row.get("is_thread") else None,
                "parent_channel_id": row.get("parent_channel_id"),
                "parent_channel_name": row.get("parent_channel_name"),
                "author_id": str(row["author_id"]),
                "author_name": row.get("author_name_snapshot") or row.get("global_name") or row.get("username"),
                "timestamp": _iso(row.get("created_at")), "edited_at": _iso(row.get("edited_at")),
                "snippet": _snippet(str(row.get("content") or ""), query),
                "match_type": match_type, "score": float(row.get("score") or (1 if match_type == "exact_id" else 0)),
                "coverage": {"state": row.get("coverage_state") or "unknown", "start": _iso(row.get("coverage_start")), "end": _iso(row.get("coverage_end"))},
                "permalink": f"https://discord.com/channels/{guild}/{channel}/{message}"}

    def _bound(self, payload: dict[str, Any]) -> dict[str, Any]:
        results = payload.get("results")
        if isinstance(results, list) and len(results) > MAX_RECORDS:
            while len(results) > MAX_RECORDS:
                context_indexes = [i for i, row in enumerate(results)
                                   if row.get("match_type") == "context"]
                if context_indexes:
                    del results[self._farthest_context_index(results, context_indexes)]
                    payload["omitted_context_count"] = payload.get("omitted_context_count", 0) + 1
                else:
                    results.pop()
                    payload["omitted_result_count"] = payload.get("omitted_result_count", 0) + 1
            payload["truncated"] = True
        channels = payload.get("channels")
        if isinstance(channels, list) and len(channels) > MAX_RECORDS:
            payload["omitted_channel_count"] = len(channels) - MAX_RECORDS
            del channels[MAX_RECORDS:]
            payload["truncated"] = True
        while self._public_json_size(payload) > MAX_JSON_BYTES:
            context_indexes = [i for i, row in enumerate(results or []) if row.get("match_type") == "context"]
            if context_indexes and isinstance(results, list):
                del results[self._farthest_context_index(results, context_indexes)]
                payload["omitted_context_count"] = payload.get("omitted_context_count", 0) + 1
            elif results and len(results) > 1:
                results.pop(); payload["omitted_result_count"] = payload.get("omitted_result_count", 0) + 1
            elif channels:
                channels.pop()
                payload["omitted_channel_count"] = payload.get("omitted_channel_count", 0) + 1
            else:
                raise RetrievalValidationError("result_too_large")
            payload["truncated"] = True
        for row in results or []:
            row.pop("_context_distance", None)
            row.pop("_context_anchor_id", None)
        return payload

    @staticmethod
    def _farthest_context_index(results: Sequence[Mapping[str, Any]],
                                indexes: Sequence[int]) -> int:
        return max(indexes, key=lambda index: (
            float(results[index].get("_context_distance") or 0), index
        ))

    @staticmethod
    def _public_json_size(payload: Mapping[str, Any]) -> int:
        public = dict(payload)
        results = payload.get("results")
        if isinstance(results, list):
            public["results"] = [
                {key: value for key, value in row.items() if not key.startswith("_")}
                for row in results
            ]
        return len(json.dumps(public, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))

    def _audit(self, conn: Any, scope: AuthorizedScope, request: Request,
               ids: Sequence[str], outcome: str,
               effective_channels: Sequence[str],
               effective_authors: Sequence[str] | None) -> None:
        query_hash = (hmac.new(self.audit_hmac_key, request.query.encode("utf-8"),
                               hashlib.sha256).hexdigest()
                      if request.query else None)
        requested_scope = json.dumps({"guild_id": request.guild_id,
            "message_id": request.message_id,
            "channel_ids": list(request.channel_ids or []),
            "channel_names": list(request.channel_names or []),
            "author_ids": list(request.author_ids or []),
            "author_names": list(request.author_names or []),
            "after": _iso(request.after), "before": _iso(request.before),
            "limit": request.limit,
            "context_before": request.context_before,
            "context_after": request.context_after,
            "effective_channel_ids": list(effective_channels),
            "effective_author_ids": (list(effective_authors)
                                     if effective_authors is not None else None),
            "authorized_root_channel_ids": sorted(scope.root_channel_ids)}, sort_keys=True)
        principal_hmac = hmac.new(self.audit_hmac_key,
                                  scope.principal.user_id.encode("utf-8"),
                                  hashlib.sha256).hexdigest()
        conn.execute("""/* search_audit */ INSERT INTO discord_archive.search_audit
            (principal_user_hmac,platform,action,query_hash,requested_scope,result_message_ids,outcome)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)""",
            (principal_hmac, scope.principal.platform, request.action, query_hash,
             requested_scope, list(ids), outcome))


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0
