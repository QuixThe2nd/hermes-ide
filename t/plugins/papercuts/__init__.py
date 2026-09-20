"""Private, structured papercut capture for Hermes Agent.

The plugin registers one agent-facing tool, ``papercuts``, with actions for
logging, listing, resolving, ignoring, and summarising workflow friction. The
canonical store is an append-only JSONL journal under ``$HERMES_HOME/papercuts``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from hermes_constants import get_hermes_home

try:  # POSIX
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:  # Windows
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore


_ALLOWED_ACTIONS = {"log", "list", "resolve", "ignore", "stats"}
_ALLOWED_SEVERITIES = {"minor", "major"}
_ALLOWED_CATEGORIES = {
    "tool",
    "docs",
    "workflow",
    "platform",
    "missing_capability",
    "repo",
}
_MAX_TEXT = {
    "summary": 500,
    "observed": 2_000,
    "workaround": 2_000,
    "suggested_fix": 2_000,
    "component": 120,
    "note": 1_000,
}


PAPERCUTS_SCHEMA = {
    "name": "papercuts",
    "description": (
        "Record and manage small but reusable pieces of workflow friction. "
        "Use action='log' after you have pushed through a dead-end tool call, "
        "misleading documentation, missing helper, platform limitation, or "
        "avoidable multi-step workaround and can explain what should change. "
        "Keep working after logging. Do not log normal searching/thinking, a "
        "transient error that immediately succeeded on retry, expected approval "
        "boundaries, user mistakes, or speculative complaints. At most one new "
        "papercut is accepted per user turn. Use list/stats for review and "
        "resolve/ignore only when the item has actually been addressed or triaged."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALLOWED_ACTIONS),
                "description": "Operation to perform.",
            },
            "summary": {
                "type": "string",
                "description": "Concise statement of the friction and its impact.",
            },
            "observed": {
                "type": "string",
                "description": "What happened, including a short safe error symptom when useful.",
            },
            "workaround": {
                "type": "string",
                "description": "How the task was unblocked. Leave empty if still unresolved.",
            },
            "suggested_fix": {
                "type": "string",
                "description": "Concrete change that would prevent the friction next time.",
            },
            "severity": {
                "type": "string",
                "enum": sorted(_ALLOWED_SEVERITIES),
                "description": "minor for annoyance, major for a substantial time sink or hard wall.",
            },
            "category": {
                "type": "string",
                "enum": sorted(_ALLOWED_CATEGORIES),
                "description": "Primary source of the friction.",
            },
            "component": {
                "type": "string",
                "description": "Tool, command, service, package, or subsystem involved.",
            },
            "id": {
                "type": "string",
                "description": "Full papercut ID or unique prefix for resolve/ignore.",
            },
            "note": {
                "type": "string",
                "description": "Resolution or triage note for resolve/ignore.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "resolved", "ignored", "all"],
                "description": "Status filter for list. Defaults to open.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum list results. Defaults to 50.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _store_dir() -> Path:
    override = os.environ.get("HERMES_PAPERCUTS_DIR", "").strip()
    return Path(override).expanduser() if override else get_hermes_home() / "papercuts"


def _events_path() -> Path:
    return _store_dir() / "events.jsonl"


def _ensure_store() -> None:
    root = _store_dir()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    events = _events_path()
    events.touch(exist_ok=True)
    try:
        events.chmod(0o600)
    except OSError:
        pass


@contextmanager
def _store_lock() -> Iterator[None]:
    """Cross-process exclusive lock for the small append-only journal."""
    _ensure_store()
    lock_path = _store_dir() / ".events.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            lock_fh.seek(0)
            if lock_fh.read(1) == "":
                lock_fh.seek(0)
                lock_fh.write("0")
                lock_fh.flush()
            lock_fh.seek(0)
            msvcrt.locking(lock_fh.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                lock_fh.seek(0)
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)


def _fallback_redact(value: str) -> str:
    value = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
        r"\s*[:=]\s*([^\s,;]+)",
        lambda m: f"{m.group(1)}=[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    return value


def _clean_text(name: str, value: Any, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} is required")
    limit = _MAX_TEXT[name]
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    # The shared redactor focuses on credential-shaped values. Papercuts also
    # accept prose such as "api_key=...", so always run the narrow label/Bearer
    # pass as a final guard before anything reaches the durable journal.
    return _fallback_redact(text)


def _now() -> Tuple[float, str]:
    epoch = time.time()
    return epoch, datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _normalize(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", value)
    value = re.sub(r"\b\d+\b", "<n>", value)
    value = re.sub(r"\s+", " ", value)
    return value


def _fingerprint(summary: str, category: str, component: str) -> str:
    basis = "\x1f".join((_normalize(summary), category, _normalize(component)))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _metadata(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    parent = kwargs.get("parent_agent")
    session_id = str(kwargs.get("session_id") or getattr(parent, "session_id", "") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    task_id = str(kwargs.get("task_id") or "")
    tool_call_id = str(kwargs.get("tool_call_id") or "")
    platform = str(getattr(parent, "platform", "") or "")
    model = str(getattr(parent, "model", "") or "")
    provider = str(getattr(parent, "provider", "") or "")
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "platform": platform,
        "model": model,
        "provider": provider,
        "cwd": os.getcwd(),
    }


def _read_events_unlocked() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with _events_path().open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line cannot poison the entire complaint box.
                continue
            if isinstance(event, dict) and event.get("kind"):
                event["_line"] = line_no
                events.append(event)
    return events


def _append_event_unlocked(event: Dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _events_path().open("a", encoding="utf-8") as fh:
        fh.write(payload + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    try:
        _events_path().chmod(0o600)
    except OSError:
        pass


def _fold(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    for event in events:
        kind = event.get("kind")
        item_id = str(event.get("id") or "")
        if not item_id:
            continue
        if kind == "cut":
            item = dict(event)
            item.pop("kind", None)
            item.pop("_line", None)
            item["status"] = "open"
            item["occurrences"] = 1
            item["first_seen"] = event.get("ts")
            item["last_seen"] = event.get("ts")
            items[item_id] = item
            continue
        item = items.get(item_id)
        if item is None:
            continue
        if kind in {"occurrence", "reopen"}:
            item["occurrences"] = int(item.get("occurrences") or 1) + 1
            item["last_seen"] = event.get("ts") or item.get("last_seen")
            if kind == "reopen":
                item["status"] = "open"
                item.pop("closed_at", None)
                item.pop("closing_note", None)
        elif kind in {"resolve", "ignore"}:
            item["status"] = "resolved" if kind == "resolve" else "ignored"
            item["closed_at"] = event.get("ts")
            item["closing_note"] = event.get("note", "")
    return items


def _resolve_id(items: Dict[str, Dict[str, Any]], prefix: str) -> Tuple[Optional[str], Optional[str]]:
    prefix = prefix.strip()
    if not prefix:
        return None, "id is required"
    matches = [item_id for item_id in items if item_id == prefix or item_id.startswith(prefix)]
    if not matches:
        return None, f"no papercut matches '{prefix}'"
    if len(matches) > 1:
        return None, f"papercut prefix '{prefix}' is ambiguous"
    return matches[0], None


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(code: str, message: str) -> str:
    return _json({"success": False, "error": {"code": code, "message": message}})


def _handle_log(args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    try:
        summary = _clean_text("summary", args.get("summary"), required=True)
        observed = _clean_text("observed", args.get("observed"), required=True)
        workaround = _clean_text("workaround", args.get("workaround"))
        suggested_fix = _clean_text("suggested_fix", args.get("suggested_fix"), required=True)
        component = _clean_text("component", args.get("component"))
    except ValueError as exc:
        return _error("invalid_input", str(exc))

    severity = str(args.get("severity") or "minor").strip().lower()
    category = str(args.get("category") or "workflow").strip().lower()
    if severity not in _ALLOWED_SEVERITIES:
        return _error("invalid_input", f"severity must be one of {sorted(_ALLOWED_SEVERITIES)}")
    if category not in _ALLOWED_CATEGORIES:
        return _error("invalid_input", f"category must be one of {sorted(_ALLOWED_CATEGORIES)}")

    meta = _metadata(kwargs)
    fingerprint = _fingerprint(summary, category, component)
    item_id = "pc_" + fingerprint[:12]
    epoch, iso = _now()

    with _store_lock():
        events = _read_events_unlocked()
        items = _fold(events)

        # Lovable's production vent loop once emitted 43 self-retractions. One
        # accepted report per user turn prevents that failure mode.
        if meta["session_id"] and meta["turn_id"]:
            for event in events:
                if (
                    event.get("kind") in {"cut", "reopen"}
                    and event.get("session_id") == meta["session_id"]
                    and event.get("turn_id") == meta["turn_id"]
                ):
                    return _error(
                        "turn_limit",
                        "one papercut has already been logged this turn; keep working",
                    )

        existing = next(
            (item for item in items.values() if item.get("fingerprint") == fingerprint),
            None,
        )
        if existing is not None:
            item_id = str(existing["id"])
            kind = "occurrence" if existing.get("status") == "open" else "reopen"
            event = {
                "kind": kind,
                "id": item_id,
                "ts": iso,
                "ts_epoch": epoch,
                **meta,
            }
            _append_event_unlocked(event)
            folded = _fold([*events, event])[item_id]
            return _json(
                {
                    "success": True,
                    "action": "log",
                    "deduplicated": True,
                    "reopened": kind == "reopen",
                    "item": folded,
                    "store": str(_events_path()),
                }
            )

        event = {
            "kind": "cut",
            "id": item_id,
            "fingerprint": fingerprint,
            "summary": summary,
            "observed": observed,
            "workaround": workaround,
            "suggested_fix": suggested_fix,
            "severity": severity,
            "category": category,
            "component": component,
            "ts": iso,
            "ts_epoch": epoch,
            **meta,
        }
        _append_event_unlocked(event)
        folded = _fold([*events, event])[item_id]
        return _json(
            {
                "success": True,
                "action": "log",
                "deduplicated": False,
                "item": folded,
                "store": str(_events_path()),
            }
        )


def _handle_list(args: Dict[str, Any]) -> str:
    status = str(args.get("status") or "open").strip().lower()
    if status not in {"open", "resolved", "ignored", "all"}:
        return _error("invalid_input", "status must be open, resolved, ignored, or all")
    try:
        limit = max(1, min(100, int(args.get("limit") or 50)))
    except (TypeError, ValueError):
        return _error("invalid_input", "limit must be an integer")

    with _store_lock():
        items = list(_fold(_read_events_unlocked()).values())
    if status != "all":
        items = [item for item in items if item.get("status") == status]
    severity_rank = {"major": 0, "minor": 1}
    items.sort(
        key=lambda item: (
            severity_rank.get(str(item.get("severity")), 9),
            -int(item.get("occurrences") or 1),
            -float(item.get("ts_epoch") or 0),
        )
    )
    items = items[:limit]
    return _json(
        {
            "success": True,
            "action": "list",
            "status": status,
            "count": len(items),
            "items": items,
            "store": str(_events_path()),
        }
    )


def _handle_close(args: Dict[str, Any], action: str, kwargs: Dict[str, Any]) -> str:
    try:
        note = _clean_text("note", args.get("note"))
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    with _store_lock():
        events = _read_events_unlocked()
        items = _fold(events)
        item_id, err = _resolve_id(items, str(args.get("id") or ""))
        if err:
            return _error("not_found", err)
        assert item_id is not None
        desired = "resolved" if action == "resolve" else "ignored"
        if items[item_id].get("status") == desired:
            return _json(
                {
                    "success": True,
                    "action": action,
                    "changed": False,
                    "item": items[item_id],
                    "store": str(_events_path()),
                }
            )
        epoch, iso = _now()
        event = {
            "kind": action,
            "id": item_id,
            "note": note,
            "ts": iso,
            "ts_epoch": epoch,
            **_metadata(kwargs),
        }
        _append_event_unlocked(event)
        folded = _fold([*events, event])[item_id]
    return _json(
        {
            "success": True,
            "action": action,
            "changed": True,
            "item": folded,
            "store": str(_events_path()),
        }
    )


def _handle_stats() -> str:
    with _store_lock():
        items = list(_fold(_read_events_unlocked()).values())
    by_status = {status: sum(1 for item in items if item.get("status") == status) for status in ("open", "resolved", "ignored")}
    by_category = {
        category: sum(1 for item in items if item.get("category") == category)
        for category in sorted(_ALLOWED_CATEGORIES)
    }
    return _json(
        {
            "success": True,
            "action": "stats",
            "total": len(items),
            "by_status": by_status,
            "by_category": {key: value for key, value in by_category.items() if value},
            "occurrences": sum(int(item.get("occurrences") or 1) for item in items),
            "store": str(_events_path()),
        }
    )


def handle_papercuts(args: Dict[str, Any], **kwargs: Any) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        return _error("invalid_input", f"action must be one of {sorted(_ALLOWED_ACTIONS)}")
    try:
        if action == "log":
            return _handle_log(args, kwargs)
        if action == "list":
            return _handle_list(args)
        if action in {"resolve", "ignore"}:
            return _handle_close(args, action, kwargs)
        return _handle_stats()
    except OSError as exc:
        return _error("io_error", f"papercuts store failure: {exc}")


def check_requirements() -> bool:
    try:
        _ensure_store()
        return True
    except OSError:
        return False


def register(ctx) -> None:
    from plugins.papercuts.cli import papercuts_command as _papercuts_command
    from plugins.papercuts.cli import register_cli as _register_papercuts_cli

    ctx.register_tool(
        name="papercuts",
        toolset="papercuts",
        schema=PAPERCUTS_SCHEMA,
        handler=handle_papercuts,
        check_fn=check_requirements,
        emoji="🩹",
    )

    ctx.register_cli_command(
        name="papercuts",
        help="Papercuts journal and daily autofix installer",
        setup_fn=_register_papercuts_cli,
        handler_fn=_papercuts_command,
        description="Manage workflow-friction papercuts and the optional daily autofix cron job.",
    )
