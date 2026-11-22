"""Turn transcript receipt: the one-page HTML a finished turn leaves in chat.

Python twin of internal/turnreceipt, the canonical renderer. The runtime uses
this module; the Go package exists so tests can hold the two to the same
bytes through testdata/receipt.json. Keep the two in step: the parity test
fails on any drift, including in the stylesheet.
"""

from __future__ import annotations

import datetime as _dt
import logging
import unicodedata
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Iterable

from gateway.platforms.base import BasePlatformAdapter

logger = logging.getLogger(__name__)

_STYLESHEET = """
body{margin:0;background:#313338;color:#dbdee1;font:15px/1.4 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.meta{margin:0;font-size:12px;color:#949ba4;text-align:center}
.bubble{padding:8px 12px;border-radius:8px;background:#2b2d31;white-space:pre-wrap;overflow-wrap:anywhere}
.user{align-self:flex-end;max-width:80%;background:#5865f2;color:#fff}
.progress{font:13px/1.35 ui-monospace,monospace;background:#1e1f22;color:#b5bac1}
.reply{border-left:3px solid #5865f2}
"""

# Order matters: "&" must be escaped first or the others get double-escaped.
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;"))

_HEAD = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
    '<meta name="color-scheme" content="dark">\n<title>Turn transcript</title>\n<style>'
)
_OPEN_BODY = "</style>\n</head>\n<body>\n<main>\n<p class=\"meta\">Turn transcript</p>\n"
_CLOSE_BODY = "</main>\n</body>\n</html>\n"


def _clean(text: str) -> str:
    """Drop control characters (category Cc) except newline and tab, then escape."""
    stripped = "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )
    for old, new in _ESCAPES:
        stripped = stripped.replace(old, new)
    return stripped


def _bubble(css_class: str, text: str) -> str:
    if not text.strip():
        return ""
    return f'<div class="bubble {css_class}">{_clean(text)}</div>\n'


def render_turn_html(user_message: str, rows: Iterable[str], final_reply: str) -> str:
    """Return the standalone HTML page for one turn (byte-equal to the Go renderer)."""
    parts = [_HEAD, _STYLESHEET, _OPEN_BODY, _bubble("user", user_message or "")]
    parts.extend(_bubble("progress", row) for row in rows)
    parts.append(_bubble("reply", final_reply or ""))
    parts.append(_CLOSE_BODY)
    return "".join(parts)


def write_turn_receipt(home: Path, user_message: str, rows: Iterable[str], final_reply: str) -> Path:
    """Write the page under ``<home>/turn_receipts`` and return its path.

    The name is a UTC timestamp plus a random suffix: unique, and it never
    carries user text.
    """
    folder = home / "turn_receipts"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"turn-{stamp}-{uuid.uuid4().hex[:12]}.html"
    path.write_text(render_turn_html(user_message, rows, final_reply), encoding="utf-8")
    return path


async def deliver_turn_receipt(adapter, chat_id: str, path: Path) -> None:
    """Ship the page as a document when the adapter implements send_document.

    Otherwise, or when the upload fails, the receipt line carries the local path.
    """
    shipped = False
    if type(adapter).send_document is not BasePlatformAdapter.send_document:
        with suppress(Exception):
            result = await adapter.send_document(chat_id, str(path), file_name=path.name)
            shipped = bool(getattr(result, "success", False))
    if shipped:
        text = f"🧾 Turn transcript attached: {path.name}"
    else:
        text = f"🧾 Turn transcript saved to {path}"
    with suppress(Exception):
        await adapter.send(chat_id=chat_id, content=text)
