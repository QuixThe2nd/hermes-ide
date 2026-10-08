"""live-progress -- minimal renderer for ``display.tool_progress: plugin``.

Reference implementation for the plugin progress mode. The gateway keeps owning the message:
one bubble per turn, its edit throttle, overflow splitting, ``cleanup_progress`` policy and
mid-run restart recovery are unchanged core behaviour. This plugin only supplies the content --
a header, one step per tool call, and a footer that counts up live -- pushed as whole-body
updates:

    ctx.progress(("__body__", body))

Whole-body updates are the reason the mode exists: the footer changes on every tick and
appending lines cannot rewrite a footer.

``ctx.progress()`` returns ``False`` when no turn is running -- a CLI session, a platform
without progress support, plugin work outside a turn -- and never raises, so this plugin stays
inert everywhere it does not apply.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

_CTX: Any = None
_STATE: Dict[str, Dict[str, Any]] = {}

_STALE_SECONDS = 900.0
_MAX_STEPS_DEFAULT = 8
_MAX_TITLE = 60
_MAX_DETAIL = 48

# One glanceable glyph per tool family; anything unmapped gets the default arrow.
_GLYPHS = {
    "read_file": "▸",
    "search_files": "▸",
    "write_file": "✎",
    "patch": "✎",
    "terminal": "$",
    "execute_code": "⌘",
    "web_search": "⌕",
    "web_extract": "⌕",
    "skill_view": "◈",
    "skill_manage": "◈",
}
_DEFAULT_GLYPH = "→"


def _cfg(key: str, default: Any) -> Any:
    if _CTX is None:
        return default
    try:
        return _CTX.get_config(key, default)
    except Exception:  # plugin settings are optional, never fatal
        return default


def _state(session_id: str, turn_id: str = "") -> Dict[str, Any]:
    st = _STATE.get(session_id)
    if st is None or (turn_id and st.get("turn") and st["turn"] != turn_id):
        st = {
            "turn": turn_id,
            "title": "",
            "steps": [],
            "started": time.monotonic(),
            "done": False,
        }
        _STATE[session_id] = st
    return st


def _age_out(now: float) -> None:
    for sid in [s for s, st in _STATE.items() if now - st["started"] > _STALE_SECONDS]:
        _STATE.pop(sid, None)


def _render(st: Dict[str, Any]) -> str:
    max_steps = int(_cfg("max_steps", _MAX_STEPS_DEFAULT) or _MAX_STEPS_DEFAULT)
    header = str(_cfg("title", "🤖 Working") or "🤖 Working")
    if st["title"]:
        header = f"{header} — {st['title']}"

    steps: List[str] = st["steps"]
    shown = steps[-max_steps:] if max_steps > 0 else steps
    body = [header]
    if len(shown) < len(steps):
        body.append(f"… {len(steps) - len(shown)} earlier step(s)")
    body.extend(shown)

    elapsed = time.monotonic() - st["started"]
    footer = f"{elapsed:.0f}s · {len(steps)} step(s)"
    if st["done"]:
        footer += " · ✅"
    body.append(footer)
    return "\n".join(body)


def _push(session_id: str, st: Dict[str, Any]) -> None:
    if _CTX is None:
        return
    try:
        _CTX.progress(("__body__", _render(st)), session_id=session_id)
    except Exception:  # a broken push must never break the turn
        pass


def _detail(tool_name: str, args: Any) -> str:
    """Short, single-line hint about what the tool is touching (path, command, query)."""
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "path", "command", "query", "url", "pattern", "skill"):
        value = args.get(key)
        if value:
            text = " ".join(str(value).split())
            return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"
    return ""


def on_pre_llm_call(user_message: str = "", session_id: str = "", turn_id: str = "",
                    platform: str = "", **kwargs: Any) -> None:
    """Open the bubble for this turn; the user's ask becomes the header subtitle."""
    if not session_id:
        return
    _age_out(time.monotonic())
    st = _state(session_id, turn_id)
    if st["done"]:
        return
    text = " ".join(str(user_message or "").split())
    if text and not st["title"]:
        st["title"] = text if len(text) <= _MAX_TITLE else text[: _MAX_TITLE - 1] + "…"
    _push(session_id, st)


def on_post_tool_call(tool_name: str = "", args: Any = None, status: str = "",
                      session_id: str = "", **kwargs: Any) -> None:
    """One line per finished tool call -- the plugin's replacement for the core's own lines."""
    if not session_id:
        return
    st = _STATE.get(session_id)
    if st is None:
        st = _state(session_id)
    glyph = _GLYPHS.get(str(tool_name), _DEFAULT_GLYPH)
    detail = _detail(str(tool_name), args)
    line = f"{glyph} {tool_name}"
    if detail:
        line += f" {detail}"
    if status and str(status) != "success":
        line += f" ({status})"
    st["steps"].append(line)
    _push(session_id, st)


def on_post_llm_call(session_id: str = "", assistant_response: str = "", **kwargs: Any) -> None:
    """Final body: same layout, closed footer. The gateway posts the answer separately."""
    st = _STATE.get(session_id)
    if st is None or st["done"]:
        return
    st["done"] = True
    _push(session_id, st)


def on_session_end(session_id: str = "", **kwargs: Any) -> None:
    _STATE.pop(session_id, None)


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
