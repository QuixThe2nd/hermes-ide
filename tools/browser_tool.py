#!/usr/bin/env python3
"""Camofox-backed browser automation tools.

Camofox is the only browser engine. The facade owns the model-facing schemas,
URL/evaluate safety policy, per-task activity tracking, and registry entries;
``browser_camofox`` owns the authenticated REST transport and session state.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
from typing import Any, Dict, Optional, cast

from hermes_cli.config import DEFAULT_CONFIG
from utils import is_truthy_value
from hermes_constants import hermes_home_key
from tools import browser_camofox as _camofox
from tools import browser_tool_eval_policy as _eval_policy
from tools import browser_tool_lifecycle as _lifecycle
from tools import browser_tool_snapshot as _snapshot
from tools.registry import registry, tool_error
from tools.url_safety import (
    is_always_blocked_url as _is_always_blocked_url,
    is_safe_url as _is_safe_url,
    normalize_url_for_request as _normalize_url_for_request,
)
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_THRESHOLD = 15000
MIN_SNAPSHOT_THRESHOLD = 1000
MAX_STORED_SNAPSHOT_CHARS = 2_000_000
DEFAULT_SESSION_INACTIVITY_TIMEOUT = int(cast(Any, DEFAULT_CONFIG).get("browser", {}).get("inactivity_timeout", 120))
MAX_INACTIVITY_CLEANUP_FAILURES = 3

_cached_snapshot_threshold: Optional[Dict[str, int]] = None
_snapshot_threshold_resolved = False
_last_screenshot_cleanup_by_dir: dict[str, float] = {}

# The lifecycle janitor and Camofox share task IDs. The facade entry is only
# bookkeeping; Camofox's own map holds the REST session identity and tab ID.
_active_sessions: Dict[str, Dict[str, Any]] = {}
_session_last_activity: Dict[str, float] = {}
_session_owner_homes: Dict[str, str] = {}
_cleanup_failures: Dict[str, int] = {}
_cleanup_thread = None
_cleanup_running = False
_cleanup_done = False
_cleanup_lock = threading.Lock()


def _browser_cfg(key: str, default, parse, log_label: str):
    try:
        from hermes_cli.config import read_raw_config

        browser_cfg = read_raw_config().get("browser", {})
        if isinstance(browser_cfg, dict) and key in browser_cfg:
            return parse(browser_cfg[key])
    except Exception as exc:
        logger.debug("Could not read %s: %s", log_label, exc)
    return default


def _get_session_inactivity_timeout() -> int:
    return _browser_cfg(
        "inactivity_timeout", DEFAULT_SESSION_INACTIVITY_TIMEOUT,
        lambda value: DEFAULT_SESSION_INACTIVITY_TIMEOUT if value is None else max(int(value), 30),
        "inactivity_timeout from config",
    )


BROWSER_SESSION_INACTIVITY_TIMEOUT = _get_session_inactivity_timeout()


def get_browser_snapshot_threshold() -> int:
    """Return the per-profile snapshot budget, floored at 1,000 characters."""
    global _cached_snapshot_threshold, _snapshot_threshold_resolved
    home = hermes_home_key()
    if _cached_snapshot_threshold is None:
        _cached_snapshot_threshold = {}
    if _snapshot_threshold_resolved and home in _cached_snapshot_threshold:
        return _cached_snapshot_threshold[home]
    value = _browser_cfg(
        "snapshot_threshold", DEFAULT_SNAPSHOT_THRESHOLD,
        lambda raw: DEFAULT_SNAPSHOT_THRESHOLD if raw is None else max(int(raw), MIN_SNAPSHOT_THRESHOLD),
        "snapshot_threshold from config",
    )
    _cached_snapshot_threshold[home] = value
    _snapshot_threshold_resolved = True
    return value


def _allow_private_urls() -> bool:
    return bool(_browser_cfg(
        "allow_private_urls", False,
        lambda value: is_truthy_value(value, default=False),
        "allow_private_urls from config",
    ))


def _is_camofox_mode() -> bool:
    """Compatibility predicate for callers; Camofox is now unconditional."""
    return True


def _last_session_key(task_id: Optional[str]) -> str:
    return task_id or "default"


def _track_activity(task_id: Optional[str]) -> str:
    effective = _last_session_key(task_id)
    _lifecycle._update_session_activity(effective)
    with _cleanup_lock:
        _active_sessions.setdefault(effective, {"backend": "camofox"})
    _lifecycle._start_browser_cleanup_thread()
    return effective


def _err(error: str, **extra) -> dict:
    return {"success": False, "error": error, **extra}


def _dumps(payload: Dict[str, Any], **kwargs) -> str:
    return json.dumps(payload, ensure_ascii=False, **kwargs)


def _secret_url_error(url: str) -> Optional[dict]:
    import urllib.parse

    from agent.redact import _PREFIX_RE

    if _PREFIX_RE.search(url) or _PREFIX_RE.search(urllib.parse.unquote(url)):
        return _err("Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs.")
    return None


def _url_policy_error(url: str) -> Optional[dict]:
    if _is_always_blocked_url(url):
        return _err("Blocked: URL targets a cloud metadata endpoint")
    if not _allow_private_urls() and not _is_safe_url(url):
        return _err("Blocked: URL targets a private or internal address")
    blocked = check_website_access(url)
    if blocked:
        return _err(
            blocked["message"],
            blocked_by_policy={"host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]},
        )
    return None


def evaluate_url_safety(url: str) -> Optional[dict]:
    error = _secret_url_error(url)
    if error is None:
        url = _normalize_url_for_request(url)
        error = _secret_url_error(url)
    return error or _url_policy_error(url)


def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    error = evaluate_url_safety(url)
    if error is not None:
        return _dumps(error)
    url = _normalize_url_for_request(url)
    _track_activity(task_id)
    return _camofox.camofox_navigate(url, task_id)


def browser_snapshot(full: bool = False, task_id: Optional[str] = None, user_task: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_snapshot(full, task_id, user_task)


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_click(ref, task_id)


def browser_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_type(ref, text, task_id)


def browser_scroll(direction: str, task_id: Optional[str] = None) -> str:
    if direction not in {"up", "down"}:
        return _dumps(_err(f"Invalid direction '{direction}'. Use 'up' or 'down'."))
    _track_activity(task_id)
    # Camofox scrolls a small fixed increment; retain the existing half-viewport behavior.
    result = "{}"
    for _ in range(5):
        result = _camofox.camofox_scroll(direction, task_id)
    return result


def browser_back(task_id: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_back(task_id)


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_press(key, task_id)


def browser_get_images(task_id: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_get_images(task_id)


def browser_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> str:
    _track_activity(task_id)
    return _camofox.camofox_vision(question, annotate, task_id)


def _blocked_private_page_json(blocked_url: str, why: str) -> str:
    return _dumps(_err(f"Blocked: page URL targets a private or internal address ({blocked_url}). {why}"))


_EVAL_NAVIGATED_WHY = "This may have been caused by a JavaScript navigation via browser_console."


def _parse_eval_value(raw_result: Any) -> Any:
    if isinstance(raw_result, str):
        try:
            return json.loads(raw_result)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw_result


def _eval_ok_response(parsed: Any, **extra) -> Dict[str, Any]:
    return {
        "success": True,
        "result": _snapshot._redact_browser_output(parsed),
        "result_type": type(parsed).__name__,
        **extra,
    }


def _browser_eval(expression: str, task_id: Optional[str] = None) -> str:
    effective = _track_activity(task_id)
    if _eval_policy._eval_ssrf_guard_active(effective):
        blocked_literal = _eval_policy._expression_targets_private_url(expression)
        if blocked_literal:
            return _dumps(_err(
                "Blocked: JavaScript expression targets a private or internal address "
                f"({blocked_literal}). Reading internal endpoints via browser_console is not permitted."
            ))

    try:
        tab_info = _camofox._ensure_tab(effective)
        tab_id = tab_info.get("tab_id") or tab_info.get("id")
        user_id = tab_info["user_id"]
        if not tab_id:
            return _dumps(_err("No browser session. Call browser_navigate first."))
        response = _camofox._post(
            f"/tabs/{tab_id}/evaluate", body={"expression": expression, "userId": user_id}
        )
        parsed = _parse_eval_value(response.get("result") if isinstance(response, dict) else response)
        if _eval_policy._eval_ssrf_guard_active(effective):
            blocked_url = _eval_policy._camofox_current_page_private_url(tab_id, user_id)
            if blocked_url:
                return _blocked_private_page_json(blocked_url, _EVAL_NAVIGATED_WHY)
        return _dumps(_eval_ok_response(parsed), default=str)
    except Exception as exc:
        if any(code in str(exc) for code in ("404", "405", "501")):
            return _dumps(_err(
                "JavaScript evaluation is not supported by this Camofox server. "
                "Use browser_snapshot or browser_vision to inspect page state."
            ))
        return tool_error(str(exc), success=False)


def browser_console(clear: bool = False, expression: Optional[str] = None, task_id: Optional[str] = None) -> str:
    if expression is not None:
        policy_error = _eval_policy._enforce_browser_eval_policy(expression)
        if policy_error:
            return _dumps(_err(policy_error))
        return _browser_eval(expression, task_id)
    _track_activity(task_id)
    return _camofox.camofox_console(clear, task_id)


def check_browser_requirements() -> bool:
    """Camofox tools stay advertised; a missing server URL is reported by the call."""
    return True


def check_browser_vision_requirements() -> bool:
    from tools.vision_tools import check_vision_requirements

    return check_vision_requirements()


BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer a lightweight retrieval tool when one is available (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer an available text-extraction or terminal-fetch tool; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "The URL to navigate to (e.g., 'https://example.com')"}}, "required": ["url"]},
    },
    {
        "name": "browser_snapshot",
        "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 15000 chars are truncated or LLM-summarized; when that happens the complete snapshot is saved to a file and the output includes its path so you can page through the rest with read_file. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.",
        "parameters": {"type": "object", "properties": {"full": {"type": "boolean", "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.", "default": False}}, "required": []},
    },
    {
        "name": "browser_click",
        "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {"type": "object", "properties": {"ref": {"type": "string", "description": "The element reference from the snapshot (e.g., '@e5', '@e12')"}, "description": {"type": "string", "description": "One short phrase (max ~80 chars) saying WHAT this interaction is for, e.g. 'clicking the submit button'. Start with an -ing verb. Never put secrets, passwords, or the typed text itself here."}}, "required": ["ref"]},
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {"type": "object", "properties": {"ref": {"type": "string", "description": "The element reference from the snapshot (e.g., '@e3')"}, "text": {"type": "string", "description": "The text to type into the field"}, "description": {"type": "string", "description": "One short phrase (max ~80 chars) saying WHAT this interaction is for, e.g. 'typing the search query'. Start with an -ing verb. Never put secrets, passwords, or the typed text itself here."}}, "required": ["ref", "text"]},
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"], "description": "Direction to scroll"}}, "required": ["direction"]},
    },
    {
        "name": "browser_back",
        "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string", "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"}, "description": {"type": "string", "description": "One short phrase (max ~80 chars) saying WHAT this interaction is for, e.g. 'pressing Enter to submit the form'. Start with an -ing verb. Never put secrets, passwords, or the typed text itself here."}}, "required": ["key"]},
    },
    {
        "name": "browser_get_images",
        "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_vision",
        "description": "Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; otherwise Hermes falls back to an auxiliary vision model and returns a text analysis. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "What you want to know about the page visually. Be specific about what you're looking for."}, "annotate": {"type": "boolean", "default": False, "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout."}}, "required": ["question"]},
    },
    {
        "name": "browser_console",
        "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.",
        "parameters": {"type": "object", "properties": {"clear": {"type": "boolean", "default": False, "description": "If true, clear the message buffers after reading"}, "expression": {"type": "string", "description": "JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll(\"a\").length'"}}, "required": []},
    },
]

_BROWSER_SCHEMA_MAP = {schema["name"]: schema for schema in BROWSER_TOOL_SCHEMAS}
_BROWSER_TOOL_TABLE = (
    ("browser_navigate", "🌐", check_browser_requirements, {"url": ""}),
    ("browser_snapshot", "📄", check_browser_requirements, {"full": False}),
    ("browser_click", "👆", check_browser_requirements, {"ref": ""}),
    ("browser_type", "⌨️", check_browser_requirements, {"ref": "", "text": ""}),
    ("browser_scroll", "📜", check_browser_requirements, {"direction": "down"}),
    ("browser_back", "⬅️", check_browser_requirements, {}),
    ("browser_press", "⌨️", check_browser_requirements, {"key": "Enter"}),
    ("browser_get_images", "🖼️", check_browser_requirements, {}),
    ("browser_vision", "👁️", check_browser_vision_requirements, {"question": "", "annotate": False}),
    ("browser_console", "🖥️", check_browser_requirements, {"clear": False, "expression": None}),
)


def _handler(function, defaults: Dict[str, Any]):
    def handle(args, **kwargs):
        return function(task_id=kwargs.get("task_id"), **{
            name: args.get(name, default) for name, default in defaults.items()
        })

    return handle


for _name, _emoji, _check_fn, _defaults in _BROWSER_TOOL_TABLE:
    registry.register(
        name=_name,
        toolset="browser",
        schema=_BROWSER_SCHEMA_MAP[_name],
        handler=_handler(globals()[_name], _defaults),
        check_fn=_check_fn,
        emoji=_emoji,
    )

atexit.register(_lifecycle._emergency_cleanup_all_sessions)
atexit.register(_lifecycle._stop_browser_cleanup_thread)

# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
import contextlib  # noqa: F401,E402
import functools  # noqa: F401,E402
import re  # noqa: F401,E402
import shutil  # noqa: F401,E402
import signal  # noqa: F401,E402
from datetime import datetime, timezone  # noqa: F401,E402
from pathlib import Path  # noqa: F401,E402
from typing import List, Tuple  # noqa: F401,E402

SNAPSHOT_SUMMARIZE_THRESHOLD = DEFAULT_SNAPSHOT_THRESHOLD

_PLUGIN_COMPAT_LAZY = {
    "cleanup_all_browsers": ("tools.browser_tool_lifecycle", "cleanup_all_browsers"),
    "cleanup_browser": ("tools.browser_tool_lifecycle", "cleanup_browser"),
    "get_hermes_home_override": ("hermes_constants", "get_hermes_home_override"),
    "hermes_home_key": ("hermes_constants", "hermes_home_key"),
    "is_truthy_value": ("utils", "is_truthy_value"),
    "node_tool_runnable": ("hermes_constants", "node_tool_runnable"),
    "reset_hermes_home_override": ("hermes_constants", "reset_hermes_home_override"),
    "set_hermes_home_override": ("hermes_constants", "set_hermes_home_override"),
    "windows_hide_flags": ("hermes_cli._subprocess_compat", "windows_hide_flags"),
}


def __getattr__(name):
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    from hermes_cli.plugin_compat import warn_once

    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
