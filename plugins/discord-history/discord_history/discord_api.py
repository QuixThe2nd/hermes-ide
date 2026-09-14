"""Exact Discord REST inventory for channels and threads; no gateway client."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Callable, Iterable

MAX_PAGES = 100
MAX_THREADS_PER_ENDPOINT = 10_000
MAX_REQUEST_ATTEMPTS = 5
THREAD_CAPABLE_TYPES = frozenset({0, 5, 15, 16})


def _hermes_helpers():
    try:
        from tools.discord_tool import _discord_request, _get_bot_token
    except ImportError:
        # Source checkout compatibility without copying the transport implementation.
        from hermes_agent.tools.discord_tool import _discord_request, _get_bot_token
    return _discord_request, _get_bot_token


def _fingerprint(items: list[dict[str, Any]]) -> str:
    raw = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _ids(items: Iterable[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in items if item.get("id") is not None]


def _validated_threads(payload: Any, *, expected_parent: str | None = None,
                       require_parent: bool = False,
                       active_response: bool = False) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("threads"), list):
        raise ValueError("malformed_response")
    has_more = payload.get("has_more", False)
    if not isinstance(has_more, bool):
        raise ValueError("malformed_response")
    if active_response and has_more:
        raise ValueError("malformed_response")
    items = payload["threads"]
    if any(not isinstance(item, dict) or
           re.fullmatch(r"[0-9]{1,20}", str(item.get("id", ""))) is None
           for item in items):
        raise ValueError("malformed_response")
    for item in items:
        parent = str(item.get("parent_id") or "")
        if require_parent and re.fullmatch(r"[0-9]{1,20}", parent) is None:
            raise ValueError("malformed_response")
        if expected_parent is not None and parent != expected_parent:
            raise ValueError("malformed_response")
    return items, has_more


def _timestamp_cursor(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _empty_endpoint(endpoint: str, reason: str, *, state: str = "error") -> dict[str, Any]:
    return {"endpoint": endpoint, "state": state, "page_count": 0,
            "final_cursor": None, "endpoint_thread_ids": [],
            "global_union_ids_after_endpoint": [], "termination_reason": reason,
            "pages": []}


def _request_with_retries(request: Callable[..., Any], method: str, path: str,
                          token: str, *, params: dict[str, str] | None = None,
                          attempts: int = MAX_REQUEST_ATTEMPTS,
                          sleep: Callable[[float], None] = time.sleep) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return request(method, path, token, params=params)
        except Exception as exc:
            status = getattr(exc, "status", None)
            retryable = status is None or status == 429 or (isinstance(status, int) and status >= 500)
            if not retryable or attempt == attempts:
                raise
            delay = min(2 ** (attempt - 1), 5.0)
            if status == 429:
                try:
                    body = json.loads(getattr(exc, "body", "") or "{}")
                    delay = min(max(float(body.get("retry_after", delay)), 0.0), 10.0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            sleep(delay)


def _archived_endpoint(
    *, parent_id: str, endpoint: str, token: str, request: Callable[..., Any],
    global_ids: set[str], max_pages: int, max_threads: int,
    sleep: Callable[[float], None], request_attempts: int,
) -> dict[str, Any]:
    if endpoint == "public":
        path = f"/channels/{parent_id}/threads/archived/public"
        cursor_kind = "timestamp"
    elif endpoint == "private":
        path = f"/channels/{parent_id}/threads/archived/private"
        cursor_kind = "timestamp"
    else:
        path = f"/channels/{parent_id}/users/@me/threads/archived/private"
        cursor_kind = "snowflake"
    cursor = None
    seen_ids: set[str] = set()
    fingerprints: set[str] = set()
    pages: list[dict[str, Any]] = []
    state, reason = "running", ""
    for page_no in range(1, max_pages + 1):
        params = {"limit": "100"}
        if cursor is not None:
            params["before"] = cursor
        request_cursor = cursor
        try:
            payload = _request_with_retries(request, "GET", path, token, params=params,
                                            attempts=request_attempts, sleep=sleep)
        except Exception as exc:
            status = getattr(exc, "status", None)
            state = "inaccessible" if status == 403 else "error"
            reason = f"http_{status}" if status is not None else "transport_error"
            break
        try:
            items, has_more = _validated_threads(payload, expected_parent=parent_id,
                                                  require_parent=True)
        except ValueError:
            state, reason = "error", "malformed_response"
            break
        fp = _fingerprint(items)
        raw_ids = _ids(items)
        repeated_fingerprint = fp in fingerprints
        fingerprints.add(fp)
        response_cursor = None
        if items:
            final = items[-1]
            if cursor_kind == "timestamp":
                response_cursor = _timestamp_cursor(
                    (final.get("thread_metadata") or {}).get("archive_timestamp")
                )
            else:
                response_cursor = str(final["id"]) if final.get("id") is not None else None
        pages.append({"page_no": page_no, "request_cursor": request_cursor,
                      "response_cursor": response_cursor, "has_more": has_more,
                      "page_fingerprint": fp, "raw_thread_ids": raw_ids})
        seen_ids.update(raw_ids)
        global_ids.update(raw_ids)
        if items and response_cursor is None:
            state, reason = "error", "malformed_cursor"
            break
        if repeated_fingerprint:
            state, reason = "error", "repeated_page_fingerprint"
            break
        if len(seen_ids) > max_threads:
            state, reason = "error", "thread_cap_exceeded"
            break
        if not has_more:
            state, reason = "complete", "has_more_false"
            cursor = response_cursor
            break
        if not items:
            state, reason = "error", "empty_page_with_has_more"
            break
        if response_cursor is None:
            state, reason = "error", "missing_cursor_source"
            break
        # Discord's before cursors must move to a different raw final item.
        non_decreasing = response_cursor == cursor
        if cursor is not None and cursor_kind == "timestamp":
            current_time = datetime.fromisoformat(response_cursor.replace("Z", "+00:00"))
            previous_time = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            non_decreasing = current_time >= previous_time
        elif cursor is not None and cursor_kind == "snowflake":
            non_decreasing = int(response_cursor) >= int(cursor)
        if non_decreasing:
            state, reason = "error", "non_decreasing_cursor"
            break
        cursor = response_cursor
    else:
        state, reason = "error", "page_cap_exceeded"
    return {"endpoint": endpoint, "state": state, "page_count": len(pages),
            "final_cursor": cursor, "endpoint_thread_ids": sorted(seen_ids, key=int),
            "global_union_ids_after_endpoint": sorted(global_ids, key=int),
            "termination_reason": reason, "pages": pages}


def inventory_guild(guild_id: str, *, token: str | None = None,
                    parent_channel_ids: Iterable[str] | None = None,
                    request: Callable[..., Any] | None = None,
                    max_pages: int = MAX_PAGES,
                    max_threads: int = MAX_THREADS_PER_ENDPOINT,
                    request_attempts: int = MAX_REQUEST_ATTEMPTS,
                    sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Inventory roots plus all active/public/private/joined-private threads."""
    if request is None or token is None:
        default_request, get_token = _hermes_helpers()
        request = request or default_request
        token = token or get_token()
    if not token:
        raise RuntimeError("discord_token_missing")
    guilds = _request_with_retries(request, "GET", "/users/@me/guilds", token,
                                   attempts=request_attempts, sleep=sleep)
    if not isinstance(guilds, list) or any(not isinstance(g, dict) for g in guilds):
        raise RuntimeError("discord_guilds_malformed")
    guild = next((g for g in guilds if str(g.get("id")) == str(guild_id)), None)
    if guild is None:
        raise ValueError("configured_guild_not_accessible")
    roots = _request_with_retries(request, "GET", f"/guilds/{guild_id}/channels", token,
                                  attempts=request_attempts, sleep=sleep)
    if not isinstance(roots, list) or any(not isinstance(root, dict) for root in roots):
        raise RuntimeError("discord_channels_malformed")
    allowed = set(map(str, parent_channel_ids)) if parent_channel_ids is not None else None
    parents = [r for r in roots if int(r.get("type", -1)) in THREAD_CAPABLE_TYPES
               and (allowed is None or str(r.get("id")) in allowed)]
    try:
        active_payload = _request_with_retries(
            request, "GET", f"/guilds/{guild_id}/threads/active", token,
            attempts=request_attempts, sleep=sleep,
        )
        active_items_all, _active_has_more = _validated_threads(
            active_payload, require_parent=True, active_response=True
        )
        active_payload = {"threads": active_items_all}
        active_state, active_reason = "complete", "guild_active_response"
    except Exception as exc:
        active_payload = {"threads": []}
        active_state = "inaccessible" if getattr(exc, "status", None) == 403 else "error"
        status = getattr(exc, "status", None)
        active_reason = (f"http_{status}" if status is not None else
                         "malformed_response" if isinstance(exc, ValueError) else "transport_error")
    active_by_parent: dict[str, set[str]] = {str(p["id"]): set() for p in parents}
    active_items_by_parent: dict[str, list[dict[str, Any]]] = {str(p["id"]): [] for p in parents}
    for thread in active_payload.get("threads", []):
        parent = str(thread.get("parent_id"))
        if parent in active_by_parent and thread.get("id") is not None:
            active_by_parent[parent].add(str(thread["id"]))
            active_items_by_parent[parent].append(thread)
    parent_results: dict[str, Any] = {}
    overall = "complete"
    for parent in parents:
        parent_id = str(parent["id"])
        active = active_by_parent[parent_id]
        active_items = active_items_by_parent[parent_id]
        active_pages = ([{"page_no": 1, "request_cursor": None,
                          "response_cursor": None, "has_more": False,
                          "page_fingerprint": _fingerprint(active_items),
                          "raw_thread_ids": _ids(active_items)}]
                        if active_state == "complete" else [])
        union = set(active)
        endpoints: dict[str, Any] = {
            "active": {"endpoint": "active", "state": active_state,
                       "page_count": len(active_pages),
                       "final_cursor": None, "endpoint_thread_ids": sorted(active, key=int),
                       "global_union_ids_after_endpoint": sorted(union, key=int),
                       "termination_reason": active_reason, "pages": active_pages}
        }
        archived: set[str] = set()
        for endpoint in ("public", "private", "joined_private"):
            manifest = _archived_endpoint(parent_id=parent_id, endpoint=endpoint,
                token=token, request=request, global_ids=union, max_pages=max_pages,
                max_threads=max_threads, sleep=sleep, request_attempts=request_attempts)
            endpoints[endpoint] = manifest
            archived.update(manifest["endpoint_thread_ids"])
        states = {str(endpoint_result["state"]) for endpoint_result in endpoints.values()}
        parent_state = ("complete" if states == {"complete"} else
                        "error" if "error" in states else "inaccessible")
        if parent_state == "error":
            overall = "error"
        elif parent_state == "inaccessible" and overall == "complete":
            overall = "inaccessible"
        parent_results[parent_id] = {"parent_channel": parent, "state": parent_state,
            "active_thread_ids": sorted(active, key=int),
            "archived_thread_ids": sorted(archived, key=int),
            "all_thread_ids": sorted(union, key=int), "endpoints": endpoints,
            "termination_reason": "all_endpoints_complete" if parent_state == "complete" else "endpoint_incomplete"}
    missing = sorted((allowed or set()) - set(parent_results), key=int)
    for parent_id in missing:
        endpoints = {name: _empty_endpoint(name, "configured_parent_missing")
                     for name in ("active", "public", "private", "joined_private")}
        parent_results[parent_id] = {
            "parent_channel": {"id": parent_id, "name": "", "type": -1},
            "state": "error", "active_thread_ids": [], "archived_thread_ids": [],
            "all_thread_ids": [], "endpoints": endpoints,
            "termination_reason": "configured_parent_missing",
        }
        overall = "error"
    return {"guild": guild, "root_channels": roots, "parents": parent_results,
            "state": overall, "termination_reason": "all_parents_complete" if overall == "complete" else "parent_incomplete"}
