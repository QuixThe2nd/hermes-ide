"""Default-on, privacy-gated browser screenshot progress events.

Capture runs after successful page-changing Camofox tools. Only sanitized event
metadata crosses to the gateway; page text and OCR output never leave this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home, hermes_home_key
from utils import is_truthy_value

logger = logging.getLogger(__name__)

BROWSER_PROGRESS_CAPTION = "Browser update"
CAPTURE_AFTER_TOOLS = frozenset({
    "browser_navigate", "browser_click", "browser_back", "browser_press", "browser_scroll",
})
_NON_CHANGING_KEYS = frozenset({"Shift", "Control", "Alt", "Meta", "Tab", "CapsLock"})
_CAPTURE_TIMEOUT_SECONDS = 5
# Privacy-OCR budget. A deployed tesseract needed 8.65s for a full 1536x735
# Camofox PNG, so the old 5s ceiling suppressed every public page. The fix is
# single-threaded execution plus headroom, NOT a downscaled OCR copy: every
# downscale factor tested (1024-1408) garbled small marker text that
# full-resolution tesseract reads ("Sign in" -> "Sign ino"), silently
# weakening suppression, while OMP_THREAD_LIMIT=1 alone took a dense
# full-size page from 16-25s under load to 0.65s with byte-identical output.
# 30s keeps >3x margin over the measured 8.65s full-image run while a hung
# tesseract still fails closed and suppresses the image.
_OCR_TIMEOUT_SECONDS = 30
_OCR_ENV = {"OMP_THREAD_LIMIT": "1"}
_MIN_INTERVAL_SECONDS = 1.0
_MAX_IMAGES_PER_TURN = 6
_MAX_IMAGE_BYTES = 9 * 1024 * 1024
_MAX_IMAGE_PIXELS = 20_000_000
_STATE_TTL_SECONDS = 30 * 60
_MAX_STATE_SCOPES = 128

# Deliberately broad: a false positive costs one preview; a false negative can
# disclose credentials. Both the accessibility tree and successful local OCR
# must be free of these markers before an image can leave the machine.
_SENSITIVE_PATTERNS = (
    ("authentication", re.compile(
        r"\b(?:login|log[ -]?in|sign[ -]?in|password|passwd|passcode|current-password)\b", re.I)),
    ("payment", re.compile(
        r"\b(?:payment|checkout|billing|credit[ -]?card|debit[ -]?card|card[ -]?number|"
        r"cc-number|cvv|cvc|iban|routing[ -]?number|account[ -]?number)\b", re.I)),
    ("verification", re.compile(
        r"\b(?:otp|2fa|mfa|one-time-code|verification[ -]?code|security[ -]?code|"
        r"recovery[ -]?code|backup[ -]?code|authenticator)\b", re.I)),
    ("secrets", re.compile(
        r"\b(?:vault|private[ -]?key|secret[ -]?key|seed[ -]?phrase|recovery[ -]?phrase|"
        r"api[ -]?key|access[ -]?token|credentials?)\b", re.I)),
)

_SENSITIVE_DOM_PROBE = r"""(() => {
  const fields = Array.from(document.querySelectorAll("input, textarea, select"));
  const autocomplete = fields.map(field => (field.getAttribute("autocomplete") || "").toLowerCase());
  const has = token => autocomplete.some(value => value.split(/\s+/).includes(token));
  if (document.querySelector('input[type="password"]') || has("current-password") || has("new-password")) {
    return {category: "authentication"};
  }
  if (["cc-name", "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-type"].some(has)) {
    return {category: "payment"};
  }
  if (has("one-time-code")) return {category: "verification"};
  return {category: null};
})()"""
_SENSITIVE_CATEGORIES = frozenset({"authentication", "payment", "verification", "secrets"})


@dataclass(slots=True)
class _ProgressState:
    last_attempt: float
    touched: float
    last_digest: Optional[str] = None
    published: int = 0


_STATE_LOCK = threading.Lock()
_CAPTURE_LOCK = threading.Lock()
_STATES: OrderedDict[tuple[str, str, str], _ProgressState] = OrderedDict()


def browser_progress_enabled(config: Optional[Mapping[str, Any]] = None) -> bool:
    """Read the active profile's default-on setting without caching cross-profile state."""
    try:
        if config is None:
            from hermes_cli.config_effective import load_user_config_effective
            config = load_user_config_effective()
        browser = config.get("browser", {})
        return isinstance(browser, Mapping) and is_truthy_value(
            browser.get("progress_images"), default=True,
        )
    except Exception:
        return False


def browser_progress_subscriber_token(session_id: Optional[str]) -> Optional[str]:
    """Return a token only when this turn explicitly enables browser delivery."""
    if not session_id:
        return None
    from agent.moa_loop import current_tool_stage_subscriber_token
    return current_tool_stage_subscriber_token(session_id, browser_progress=True)


def _claim(scope: tuple[str, str, str], now: float) -> Optional[_ProgressState]:
    with _STATE_LOCK:
        stale = [key for key, value in _STATES.items() if now - value.touched > _STATE_TTL_SECONDS]
        for key in stale:
            _STATES.pop(key, None)
        while len(_STATES) >= _MAX_STATE_SCOPES and scope not in _STATES:
            _STATES.popitem(last=False)
        state = _STATES.get(scope)
        if state is None:
            state = _ProgressState(last_attempt=float("-inf"), touched=now)
            _STATES[scope] = state
        if state.published >= _MAX_IMAGES_PER_TURN or now - state.last_attempt < _MIN_INTERVAL_SECONDS:
            return None
        state.last_attempt = state.touched = now
        _STATES.move_to_end(scope)
        return state


def clear_browser_progress_state(
    session_id: Optional[str], task_id: Optional[str] = None,
) -> None:
    """Drop rate/dedupe state for the current profile's completed turn."""
    if not session_id:
        return
    home_key = hermes_home_key()
    with _STATE_LOCK:
        for key in list(_STATES):
            if key[0] == home_key and key[1] == session_id and (task_id is None or key[2] == task_id):
                _STATES.pop(key, None)


def _sensitive_category(snapshot: str, ocr_text: str) -> Optional[str]:
    # Inspect independently so a marker split across the two sources cannot be
    # concatenated into a false negative or accidentally included in an event.
    for category, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(snapshot) or pattern.search(ocr_text):
            return category
    return None

def _sensitive_dom_category(browser_camofox: Any, session: Mapping[str, Any]) -> Optional[str]:
    """Inspect field semantics without retrieving values, labels, URLs, or page content."""
    data = browser_camofox._post(
        browser_camofox._tab_path(session, "evaluate"),
        {"userId": session["user_id"], "expression": _SENSITIVE_DOM_PROBE},
        timeout=_CAPTURE_TIMEOUT_SECONDS,
    )
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict) or set(result) != {"category"}:
        raise ValueError("Invalid browser privacy probe result")
    category = result["category"]
    if category is None:
        return None
    if category not in _SENSITIVE_CATEGORIES:
        raise ValueError("Unknown browser privacy category")
    return category


def owned_browser_progress_path(
    event: Mapping[str, Any], expected_home: Optional[Path] = None,
) -> Optional[Path]:
    """Validate profile ownership and the random screenshot-cache location."""
    try:
        home = Path(expected_home if expected_home is not None else event["profile_home"]).resolve()
        if Path(event["profile_home"]).resolve() != home:
            return None
        if str(event.get("profile_key")) != hermes_home_key(home):
            return None
        path = Path(event["path"]).resolve()
        path.relative_to(home / "browser_screenshots")
        if not path.name.startswith("browser_screenshot_") or path.suffix.lower() != ".png":
            return None
        if not path.is_file() or path.stat().st_size > _MAX_IMAGE_BYTES:
            return None
        return path
    except (KeyError, OSError, TypeError, ValueError):
        return None


def discard_browser_progress_event(
    event: Mapping[str, Any], expected_home: Optional[Path] = None,
) -> None:
    """Best-effort deletion of an owned, undelivered temporary screenshot."""
    path = owned_browser_progress_path(event, expected_home)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove browser progress screenshot", exc_info=True)


def capture_browser_progress(
    tool_name: str,
    raw_result: str,
    *,
    task_id: Optional[str],
    session_id: str,
    tool_call_id: Optional[str],
    subscriber_token: str,
) -> bool:
    """Capture and publish one safe image; never raise or mutate the tool result."""
    screenshot_path: Optional[str] = None
    accepted = False
    event: Optional[dict[str, Any]] = None
    home: Optional[Path] = None
    if tool_name not in CAPTURE_AFTER_TOOLS or not _CAPTURE_LOCK.acquire(blocking=False):
        return False
    try:
        from agent.moa_loop import current_tool_stage_subscriber_token, publish_tool_stage
        from tools import browser_camofox
        from tools.vision_ocr import run_local_ocr_checked

        home = get_hermes_home()
        if current_tool_stage_subscriber_token(session_id, browser_progress=True) != subscriber_token:
            return False
        payload = json.loads(raw_result)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return False
        home_key = hermes_home_key(home)
        effective_task = task_id or "default"
        state = _claim((home_key, session_id, effective_task), time.monotonic())
        if state is None:
            return False
        with browser_camofox._sessions_lock:
            session = browser_camofox._sessions.get(
                browser_camofox._session_key(effective_task, home_key=home_key)
            )
        if not session or session.get("home_key") != home_key:
            return False
        if not session.get("tab_id") or not session.get("user_id"):
            return False

        snapshot_data = browser_camofox._snapshot_data(session, timeout=_CAPTURE_TIMEOUT_SECONDS)
        snapshot = snapshot_data.get("snapshot") if isinstance(snapshot_data, dict) else None
        if not isinstance(snapshot, str):
            return False
        category = _sensitive_dom_category(browser_camofox, session)
        if category is not None:
            logger.info("Browser progress image suppressed: %s content", category)
            return False
        response = browser_camofox._get_raw(
            browser_camofox._tab_path(session, "screenshot"),
            params=browser_camofox._user_params(session),
            timeout=_CAPTURE_TIMEOUT_SECONDS,
        )
        content = response.content
        if not isinstance(content, bytes) or not content or len(content) > _MAX_IMAGE_BYTES:
            return False
        digest = hashlib.sha256(content).hexdigest()
        if digest == state.last_digest:
            return False
        screenshot_path = browser_camofox._save_screenshot(content)

        from PIL import Image
        with Image.open(screenshot_path) as image:
            if image.format != "PNG" or image.width * image.height > _MAX_IMAGE_PIXELS:
                return False
            image.verify()
        ocr = run_local_ocr_checked(
            screenshot_path, timeout=_OCR_TIMEOUT_SECONDS, extra_env=_OCR_ENV,
        )
        if not ocr.success:
            logger.info("Browser progress image suppressed: local privacy inspection unavailable")
            return False
        category = _sensitive_category(snapshot, ocr.text)
        if category is not None:
            logger.info("Browser progress image suppressed: %s content", category)
            return False

        state.last_digest = digest
        event = {
            "type": "browser.screenshot",
            "path": screenshot_path,
            "profile_home": str(home),
            "profile_key": home_key,
            "session_id": session_id,
            "task_id": task_id,
            "tool_call_id": tool_call_id,
            "sha256": digest,
        }
        accepted = publish_tool_stage(
            session_id, event, subscriber_token=subscriber_token,
        )
        if accepted:
            state.published += 1
        return accepted
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    except Exception as exc:
        # Exception messages can contain page URLs or OCR-adjacent details.
        logger.info("Browser progress image suppressed after %s", type(exc).__name__)
        return False
    finally:
        if screenshot_path is not None and not accepted and event is not None and home is not None:
            discard_browser_progress_event(event, home)
        elif screenshot_path is not None and not accepted:
            try:
                Path(screenshot_path).unlink(missing_ok=True)
            except OSError:
                pass
        _CAPTURE_LOCK.release()
