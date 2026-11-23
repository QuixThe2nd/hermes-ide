"""Render Discord mention tokens as readable names for model text + thread titles.

The rendering rules live ONLY in the Go module ``internal/mentionrender``,
executed as the ``cmd/mentionrender`` binary (stdin JSON → stdout JSON) —
this module contains no copy of them. It gathers the name facts Discord
already delivers with the message (``message.mentions`` with the guild
member display name preferred, then the guild member cache; no extra REST
calls), pipes one render request through the binary resolved via
``gateway/gobuild.py``, and owns the fail-open contract: any helper problem
(no binary and no Go toolchain, build failure, timeout, unreadable output)
returns None and the caller falls back to the historical mention handling —
a message is never dropped over a rendering problem. One warning per
process, then debug-level only.

Both render entry points are async: the first-use ``go build`` and every
render subprocess are blocking work and must not stall the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import Any, Dict, Optional

from gateway import gobuild

logger = logging.getLogger(__name__)

# Any Discord mention token: <@id>, <@!id>, <@&id>, <#id>. Content without
# one renders identically to the historical handling, so the helper (and its
# first-use build) is never invoked for it.
_ANY_MENTION_TOKEN = re.compile(r"<@[!&]?\d+>|<#\d+>")
_USER_MENTION_ID = re.compile(r"<@!?(\d+)>")

_HELPER_TIMEOUT_SECONDS = 10

_RESOLVER = gobuild.HelperBinary(
    module="mentionrender",
    sources=("mentionrender.go", "cmd/mentionrender/main.go"),
    build_target="./cmd/mentionrender",
    cache_dir="mentionrender",
    binary_stem="mentionrender",
    label="discord mention renderer",
    failure_hint="mentions render as raw tokens until it succeeds",
)

_warned_unavailable = False


def has_mention_token(content: Optional[str]) -> bool:
    """Whether *content* carries any Discord mention token."""
    return bool(content) and bool(_ANY_MENTION_TOKEN.search(content))


def collect_names(message: Any, content: str) -> Dict[str, str]:
    """User ID → display name for every user token in *content*.

    Sources, in order: the user objects Discord delivered with the message
    (``message.mentions``, member display name preferred), then the guild
    member cache. No REST calls; non-string junk from partial fakes is
    ignored rather than rendered.
    """
    names: Dict[str, str] = {}
    for mentioned in getattr(message, "mentions", None) or []:
        user_id = str(getattr(mentioned, "id", "") or "")
        if not user_id:
            continue
        name = getattr(mentioned, "display_name", None) or getattr(mentioned, "name", None)
        if isinstance(name, str) and name.strip():
            names[user_id] = name
    get_member = getattr(getattr(message, "guild", None), "get_member", None)
    if callable(get_member):
        for match in _USER_MENTION_ID.finditer(content or ""):
            user_id = match.group(1)
            if user_id in names:
                continue
            try:
                member = get_member(int(user_id))
            except (ValueError, TypeError):
                continue
            name = getattr(member, "display_name", None) or getattr(member, "name", None)
            if isinstance(name, str) and name.strip():
                names[user_id] = name
    return names


def bot_display_name(message: Any, client_user: Any) -> str:
    """The bot's own display name: guild member nickname when Discord cached
    it on the message's guild, else the global display/user name."""
    guild_me = getattr(getattr(message, "guild", None), "me", None)
    for candidate in (
        getattr(guild_me, "display_name", None),
        getattr(client_user, "display_name", None),
        getattr(client_user, "name", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def _render_sync(
    mode: str, content: str, *, bot_id: str, bot_name: str, names: Dict[str, str],
) -> Optional[str]:
    """One render through the helper binary; None = caller uses the legacy path.

    Never raises. Content without a mention token returns None WITHOUT
    touching the resolver: the legacy path is byte-identical for it and no
    process (or first-use build) may be spawned for token-free text.
    """
    global _warned_unavailable
    if not has_mention_token(content):
        return None
    binary = _RESOLVER.get()
    if binary is None:
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.warning(
                "discord mention renderer unavailable (no cached binary and no Go "
                "toolchain to build one); falling back to raw-mention text"
            )
        return None
    try:
        run = subprocess.run(
            [binary],
            input=json.dumps({
                "content": content,
                "bot_id": bot_id,
                "bot_name": bot_name,
                "names": names,
                "mode": mode,
            }),
            capture_output=True, text=True, timeout=_HELPER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("mention render invocation failed: %s", exc)
        return None
    if run.returncode != 0:
        logger.debug(
            "mention renderer exited %s: %s", run.returncode, run.stderr.strip(),
        )
        return None
    try:
        rendered = json.loads(run.stdout).get("text")
    except (ValueError, AttributeError) as exc:
        logger.debug("mention renderer returned unreadable output: %s", exc)
        return None
    return rendered if isinstance(rendered, str) else None


async def render_body_async(
    content: str, *, bot_id: str, bot_name: str, names: Dict[str, str],
) -> Optional[str]:
    """Render model-facing message text off the event loop."""
    return await asyncio.to_thread(
        _render_sync, "body", content, bot_id=bot_id, bot_name=bot_name, names=names,
    )


async def render_title_async(
    content: str, *, bot_id: str, bot_name: str, names: Dict[str, str],
) -> Optional[str]:
    """Render an auto-thread placeholder title off the event loop."""
    return await asyncio.to_thread(
        _render_sync, "title", content, bot_id=bot_id, bot_name=bot_name, names=names,
    )
