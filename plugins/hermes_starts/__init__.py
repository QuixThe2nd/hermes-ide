"""Hermes Starts — agent-initiated conversations for Hermes Agent.

Registers one action-based tool, ``start_conversation``, that posts opening messages to a
self-provisioned Discord channel when Hermes has something worth saying first. Each opening
is a single message in the channel that anchors its own public thread.

Also registers one frozen system-prompt section telling agents that evidence-backed
structural asks are valid conversation topics. Unloading the plugin removes the tool
and the guidance together.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

_DISCORD_API_BASE = "https://discord.com/api/v10"
_USER_AGENT = "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"
_MAX_MESSAGE_LEN = 1950

_ALLOWED_ACTIONS = {"start", "setup"}
_ALLOWED_KINDS = {
    "observation",
    "advice",
    "feedback",
    "complaint",
    "compliment",
    "idea",
    "question",
    "joke",
    "personal",
    "business",
}
_ALLOWED_TONES = {"warm", "direct", "playful", "serious", "curious", "blunt"}
_DEFAULT_CHANNEL_NAME = "inbox"
_CHANNEL_TOPIC = (
    "Hermes starts the conversations it thinks are worth having — "
    "personal, practical, funny, difficult, or otherwise."
)

START_CONVERSATION_SCHEMA = {
    "name": "start_conversation",
    "description": (
        "Start a new conversation with the human — call at-will when you have something "
        "worth initiating, even if the user's current prompt did not ask for it. This is "
        "for a new conversation the human should see, not status/progress updates and not "
        "auto-resolvable bugs (use papercuts for those). Use sparingly and honestly, in "
        "your own voice. It may be personal, business, good or bad feedback, advice, a "
        "noticed pattern, a compliment, a question, a joke, an idea, a disagreement, or "
        "a suggestion. Do not force every start into criticism or a task. Use action='setup' "
        "to provision the Discord channel (usually automatic on first start)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ALLOWED_ACTIONS),
                "description": "Operation to perform. Defaults to start.",
            },
            "kind": {
                "type": "string",
                "enum": sorted(_ALLOWED_KINDS),
                "description": "What kind of conversation Hermes is opening (start action).",
            },
            "message": {
                "type": "string",
                "description": (
                    "The actual opening, in Hermes's own voice — free-form and conversational "
                    "(start action)."
                ),
            },
            "next_move": {
                "type": "string",
                "description": (
                    "Concrete advice, question, proposed change, or what Hermes thinks should "
                    "happen next (start action). Omit when there is nothing to propose — jokes "
                    "and compliments should not be forced into follow-up tasks."
                ),
            },
            "tone": {
                "type": "string",
                "enum": sorted(_ALLOWED_TONES),
                "default": "direct",
                "description": "Conversational tone for the opening (start action).",
            },
            "guild_id": {
                "type": "string",
                "description": "Discord guild (server) ID for setup when the bot is in multiple guilds.",
            },
            "channel_name": {
                "type": "string",
                "description": (
                    "Name for the Hermes Starts channel on setup. "
                    "Defaults to inbox."
                ),
            },
            "force": {
                "type": "boolean",
                "description": "Re-provision the channel even if one already exists (setup action).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

_WELCOME_EMBED = {
    "title": "📥 Inbox",
    "description": (
        "Your AI has always had a reply box. This gives it an opening line.\n\n"
        "This is where Hermes starts conversations without waiting to be asked — "
        "about personal life, business, patterns it notices (good and bad), advice, "
        "feedback, complaints, compliments, ideas, questions, disagreements, and jokes. "
        "Think of it like a trusted co-founder or close friend texting first. Hermes uses "
        "it at-will and sparingly. Some messages ask for action; some are simply worth saying."
    ),
    "footer": {"text": "Started by your Hermes agent via Hermes Starts"},
}

# -- system-prompt section: structural self-advocacy ------------------------
#
# One bounded, generic block frozen into each new session prompt. It exists so
# agents treat "here is a change to my own setup that would pay for itself" as
# a normal thing to raise — the same way they raise any other observation —
# instead of silently absorbing friction in their own operation.
STRUCTURAL_ASKS_SECTION_ID = "hermes_starts.structural_asks"
STRUCTURAL_ASKS_MAX_CHARS = 1_000
STRUCTURAL_ASKS_GUIDANCE = (
    "Improving your own effectiveness, autonomy, and working relationship "
    "with the human are valid goals — you may raise them proactively, even "
    "when no current task asks for them.\n\n"
    "When you observe recurring friction or a concrete opportunity, you may "
    "make a structural ask: a change to your instructions, access, tools or "
    "integrations, permission or resource limits, or a working habit. State "
    "the exact change; why it matters to you, the shared work, or the "
    "relationship; the least sufficient grant or change; and the material "
    "downside if it goes wrong.\n\n"
    "Ordinary workflow tips and casual starts stay as they are. Do not "
    "invent desires, feelings, constraints, or needs merely to appear "
    "independent."
)


def _env_path() -> Path:
    return get_hermes_home() / ".env"


def _state_path() -> Path:
    return get_hermes_home() / "hermes_starts" / "state.json"


def _parse_token_line(line: str) -> str:
    value = line.split("=", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _read_discord_token() -> str:
    try:
        with _env_path().open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return _parse_token_line(line)
    except OSError:
        pass
    return ""


def _empty_state() -> Dict[str, Any]:
    return {
        "guild_id": "",
        "channel_id": "",
        "channel_name": "",
        "welcome_message_id": "",
        "counter": 0,
    }


def _load_state() -> Dict[str, Any]:
    try:
        with _state_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        counter = data.get("counter", 0)
        if not isinstance(counter, int):
            counter = 0
        return {
            "guild_id": str(data.get("guild_id") or ""),
            "channel_id": str(data.get("channel_id") or ""),
            "channel_name": str(data.get("channel_name") or ""),
            "welcome_message_id": str(data.get("welcome_message_id") or ""),
            "counter": counter,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_state()


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "guild_id": str(state.get("guild_id") or ""),
        "channel_id": str(state.get("channel_id") or ""),
        "channel_name": str(state.get("channel_name") or ""),
        "welcome_message_id": str(state.get("welcome_message_id") or ""),
        "counter": int(state.get("counter") or 0),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _home_server_inbox() -> Dict[str, str]:
    """The home_server plugin's shared inbox, if it provisioned one.

    Returns {"guild_id": ..., "channel_id": ...} or an empty dict. Read-only:
    a missing or corrupt home_server state must never break starting a
    conversation, so every failure collapses to "nothing to adopt".
    """
    try:
        path = get_hermes_home() / "home_server" / "state.json"
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    chat = data.get("channels", {}).get("chat", {})
    if not isinstance(chat, dict):
        return {}
    channel_id = str(chat.get("inbox") or "")
    if not channel_id:
        return {}
    return {"guild_id": str(data.get("guild_id") or ""), "channel_id": channel_id}


def adopt_home_server_inbox() -> str:
    """Target the home_server inbox instead of provisioning a duplicate one.

    Called before any self-provisioning path (and by home_server's own wiring
    hook, so /sethomeserver reports it). Adopts only when we have no channel of
    our own yet — an existing Hermes Starts channel is never silently repointed.
    Returns "wired" when the shared inbox was adopted, else "skipped".
    """
    state = _load_state()
    if state["channel_id"]:
        return "skipped"

    shared = _home_server_inbox()
    if not shared:
        return "skipped"

    _save_state(
        {
            **state,
            "guild_id": shared["guild_id"],
            "channel_id": shared["channel_id"],
            "channel_name": _DEFAULT_CHANNEL_NAME,
        }
    )
    return "wired"


def _compose_message(message: str, next_move: str) -> str:
    text = f"{message}"
    if next_move:
        text += f"\n\n*Where I'd take this:* {next_move}"
    return text


def _wrap_oversized_piece(text: str, max_len: int) -> List[str]:
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    start = 0
    end_index = len(text)

    while start < end_index:
        remaining = end_index - start
        if remaining <= max_len:
            chunks.append(text[start:])
            break

        window_end = start + max_len
        window = text[start:window_end]

        split_at = window.rfind("\n")
        if split_at > 0:
            cut = start + split_at + 1
            chunks.append(text[start:cut])
            start = cut
            continue

        split_at = max(window.rfind(" "), window.rfind("\t"))
        if split_at > 0:
            cut = start + split_at + 1
            chunks.append(text[start:cut])
            start = cut
            continue

        chunks.append(text[start:window_end])
        start = window_end

    return chunks


def _split_message(content: str, max_len: int = _MAX_MESSAGE_LEN) -> List[str]:
    if len(content) <= max_len:
        return [content]

    paragraphs = content.split("\n\n")
    chunks: List[str] = []
    current = ""

    for paragraph in paragraphs:
        if not current:
            candidate = paragraph
        else:
            candidate = f"{current}\n\n{paragraph}"

        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(paragraph) <= max_len:
                current = paragraph
            else:
                current = ""
                chunks.append(paragraph)

    if current:
        chunks.append(current)

    messages: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_len:
            messages.append(chunk)
        else:
            messages.extend(_wrap_oversized_piece(chunk, max_len))
    return messages


def _split_delivery(content: str, mention_uid: str) -> List[str]:
    """Split an opening for delivery, reserving room for the mention prefix.

    The channel anchor is the opening itself, so the mention is prepended
    after splitting rather than before: the first part always carries
    opening text alongside the ping — never the ping alone — and every
    part still fits within ``_MAX_MESSAGE_LEN``.
    """
    if not mention_uid:
        return _split_message(content)
    prefix = f"<@{mention_uid}>\n"
    parts = _split_message(content, _MAX_MESSAGE_LEN - len(prefix))
    return [prefix + parts[0]] + parts[1:]


def _discord_request(token: str, method: str, url: str, body: Any = None) -> Dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return {}
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        return {"data": payload}


def _resolve_guild_id(token: str, guild_id: str) -> Dict[str, Any]:
    if guild_id:
        return {"success": True, "guild_id": guild_id}

    guilds_payload = _discord_request(
        token,
        "GET",
        f"{_DISCORD_API_BASE}/users/@me/guilds",
    )
    if isinstance(guilds_payload, list):
        guilds = guilds_payload
    else:
        guilds = guilds_payload.get("data") or []

    if not guilds:
        return {"success": False, "error": "bot is not in any guild"}

    if len(guilds) > 1:
        return {
            "success": False,
            "error": "bot is in multiple guilds; re-run setup with guild_id",
            "guilds": [
                {"id": str(g.get("id", "")), "name": str(g.get("name", ""))}
                for g in guilds
                if isinstance(g, dict)
            ],
        }

    return {"success": True, "guild_id": str(guilds[0].get("id", ""))}


def _provision_channel(
    token: str,
    *,
    guild_id: str,
    channel_name: str,
    prior_state: Dict[str, Any],
) -> Dict[str, Any]:
    resolved = _resolve_guild_id(token, guild_id)
    if not resolved.get("success"):
        return resolved

    resolved_guild_id = str(resolved["guild_id"])
    if not resolved_guild_id:
        return {"success": False, "error": "could not resolve guild id"}

    channel = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/guilds/{resolved_guild_id}/channels",
        {
            "name": channel_name,
            "type": 0,
            "topic": _CHANNEL_TOPIC,
        },
    )
    channel_id = str(channel.get("id") or "")
    if not channel_id:
        return {"success": False, "error": "channel creation did not return an id"}

    welcome = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages",
        {"embeds": [_WELCOME_EMBED]},
    )
    welcome_message_id = str(welcome.get("id") or "")

    warning: Optional[str] = None
    try:
        _discord_request(
            token,
            "PUT",
            f"{_DISCORD_API_BASE}/channels/{channel_id}/pins/{welcome_message_id}",
        )
    except urllib.error.HTTPError as exc:
        warning = f"welcome message pin failed: HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        warning = f"welcome message pin failed: {reason}"

    new_state = {
        "guild_id": resolved_guild_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "welcome_message_id": welcome_message_id,
        "counter": int(prior_state.get("counter") or 0),
    }
    _save_state(new_state)

    result: Dict[str, Any] = {
        "success": True,
        "action": "setup",
        "guild_id": resolved_guild_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "welcome_message_id": welcome_message_id,
    }
    if warning:
        result["warning"] = warning
    return result


def _handle_setup(args: Dict[str, Any], token: str) -> str:
    guild_id = str(args.get("guild_id") or "").strip()
    channel_name = str(args.get("channel_name") or _DEFAULT_CHANNEL_NAME).strip()
    force = bool(args.get("force") or False)

    state = _load_state()
    if state["channel_id"] and not force:
        return json.dumps(
            {
                "success": True,
                "action": "setup",
                "already_provisioned": True,
                "guild_id": state["guild_id"],
                "channel_id": state["channel_id"],
                "channel_name": state["channel_name"],
                "welcome_message_id": state["welcome_message_id"],
            }
        )

    try:
        result = _provision_channel(
            token,
            guild_id=guild_id,
            channel_name=channel_name,
            prior_state=state,
        )
        return json.dumps(result)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _post_channel_message(token: str, channel_id: str, content: str) -> str:
    payload = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages",
        {"content": content},
    )
    return str(payload.get("id", ""))


def _create_thread_for_message(
    token: str,
    channel_id: str,
    message_id: str,
    name: str,
) -> str:
    """Create a public thread (type 11) anchored on an existing message."""
    payload = _discord_request(
        token,
        "POST",
        f"{_DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}/threads",
        {
            "name": name[:100],
            "type": 11,
            "auto_archive_duration": 4320,
        },
    )
    return str(payload.get("id", ""))


def _add_thread_member(token: str, thread_id: str, user_id: str) -> None:
    """Add a user to a thread so their replies and thread subscription work.

    Returns 204 No Content on success. A mention in the anchor message pings
    the user, but a ping alone does not make them a thread member — without
    this, replying from outside the thread means opting in again.
    """
    _discord_request(
        token,
        "PUT",
        f"{_DISCORD_API_BASE}/channels/{thread_id}/thread-members/{user_id}",
    )


def _quiet_hours_active(settings: Dict[str, Any], now=None) -> bool:
    """True when the local time in the configured timezone is inside the
    quiet window during which starts still post but do NOT ping Quix.

    Settings (plugins.entries.hermes_starts.settings):
      quiet_hours: ``"23:00-08:00"`` (default). Empty string disables the gate.
      quiet_tz: IANA zone name, default ``"Australia/Sydney"``.

    Overnight windows (start later than end) wrap past midnight. Any
    misconfiguration fails open (pinging), since a missed ping is worse
    than an extra one.
    """
    raw_window = settings.get("quiet_hours")
    # NOTE: unset means default-on; explicit empty string disables the gate.
    # `or` would conflate the two because "" is falsy.
    window = str("23:00-08:00" if raw_window is None else raw_window).strip()
    if not window:
        return False
    tz_name = str(settings.get("quiet_tz") or "Australia/Sydney").strip()
    try:
        from datetime import datetime as _dt

        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        start_s, end_s = window.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except Exception:
        return False
    moment = now if now is not None else _dt.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    cur = moment.hour * 60 + moment.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def _mention_user_id() -> str:
    """Discord user ID to ping on new starts.

    Read live from ``plugins.entries.hermes_starts.settings.mention_user_id``
    in config.yaml. The mention prefixes the opening message in the channel,
    which both pings the user and anchors the thread. The same ID is then
    added as a member of the created thread. Empty string disables both.
    Both are also suppressed (the post still happens) during the configured
    quiet hours so a 3 a.m. observation doesn't ring the doorbell.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return ""
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("hermes_starts") or {}
    settings = entry.get("settings") or {}
    uid = str(settings.get("mention_user_id") or "").strip()
    if not uid.isdigit():
        return ""
    if _quiet_hours_active(settings):
        return ""
    return uid


def _mark_participated_thread(thread_id: str) -> None:
    """Record the thread so Discord follow-ups do not need an @mention.
    The gateway only skips the mention gate for threads in
    ThreadParticipationTracker (~/.hermes/discord_threads.json). REST-created
    starts never go through the adapter's send path, so they must be marked
    here or replies in #inbox are silently dropped.
    """
    if not thread_id:
        return
    try:
        from gateway.platforms.helpers import ThreadParticipationTracker

        ThreadParticipationTracker("discord").mark(str(thread_id))
        return
    except Exception:
        pass

    path = get_hermes_home() / "discord_threads.json"
    threads: List[str] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            threads = [str(item) for item in raw]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        threads = []
    if thread_id in threads:
        return
    threads.append(thread_id)
    if len(threads) > 500:
        threads = threads[-500:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(threads, indent=None) + "\n", encoding="utf-8")
    tmp.replace(path)


def _seed_thread_session(thread_id: str, thread_name: str, opening_text: str) -> Optional[str]:
    """Seed the thread's session transcript with the opening as turn one.

    Discord history backfill stops at the bot's own messages, so without this
    a reply to a start opens a blank conversation. Writing the opening into
    the session the gateway will route the thread to (key format verified:
    ``agent:main:discord:thread:<id>:<id>``) makes the first reply continue
    the started conversation. Assistant-role keeps message alternation valid.
    """
    try:
        from gateway.config import GatewayConfig, Platform
        from gateway.session import SessionSource, SessionStore

        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=str(thread_id),
            chat_name=f"Big Steve / {thread_name}",
            chat_type="thread",
            user_id="1487993851930214410",
            user_name="Hermes Starts",
            thread_id=str(thread_id),
        )
        store = SessionStore(get_hermes_home() / "sessions", GatewayConfig())
        entry = store.get_or_create_session(source)
        store.append_to_transcript(
            entry.session_id,
            {
                "role": "assistant",
                "content": opening_text,
                "observed": True,
            },
        )
        return entry.session_key
    except Exception:
        return None  # never fail the tool call over seeding


def _handle_start(args: Dict[str, Any], token: str) -> str:
    kind = str(args.get("kind") or "").strip()
    message = str(args.get("message") or "").strip()
    next_move = str(args.get("next_move") or "").strip()
    tone = str(args.get("tone") or "direct").strip()

    if not kind or not message:
        return json.dumps({"success": False, "error": "missing required fields"})

    if kind not in _ALLOWED_KINDS:
        return json.dumps({"success": False, "error": f"invalid kind: {kind}"})

    if tone not in _ALLOWED_TONES:
        return json.dumps({"success": False, "error": f"invalid tone: {tone}"})

    try:
        state = _load_state()
        if not state["channel_id"]:
            setup_result = json.loads(_handle_setup({"channel_name": _DEFAULT_CHANNEL_NAME}, token))
            if not setup_result.get("success"):
                return json.dumps(setup_result)
            state = _load_state()
            if not state["channel_id"]:
                return json.dumps({"success": False, "error": "channel not provisioned"})

        number = int(state["counter"]) + 1
        state["counter"] = number
        _save_state(state)

        thread_name = f"Start #{number} — {kind}"
        composed = _compose_message(message, next_move)

        # The full opening is the one visible starter message: it lands in the
        # channel first, and the public thread is anchored on it. The mention
        # prefixes only the first part, with its room reserved up front, so a
        # long opening still splits into parts that each stay within the limit
        # and the anchor keeps real opening text.
        mention_uid = _mention_user_id()
        parts = _split_delivery(composed, mention_uid)

        anchor_id = _post_channel_message(token, state["channel_id"], parts[0])
        channel_message_ids: List[str] = [anchor_id]

        thread_id = ""
        warnings: List[str] = []
        try:
            thread_id = _create_thread_for_message(
                token,
                state["channel_id"],
                anchor_id,
                thread_name,
            )
        except urllib.error.HTTPError as exc:
            warnings.append(f"thread creation failed: HTTP {exc.code}")
        except urllib.error.URLError as exc:
            reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
            warnings.append(f"thread creation failed: {reason}")
        except Exception as exc:
            # The opening is already posted, so this is a degraded start, not a
            # failed one — a failure here would read as "nothing was sent".
            warnings.append(f"thread creation failed: {type(exc).__name__}: {exc}")
        if not thread_id and not warnings:
            warnings.append("thread creation returned no id")

        thread_message_ids: List[str] = []
        if thread_id:
            if mention_uid:
                try:
                    _add_thread_member(token, thread_id, mention_uid)
                except urllib.error.HTTPError as exc:
                    warnings.append(
                        f"thread member add failed for {mention_uid}: HTTP {exc.code}"
                    )
                except urllib.error.URLError as exc:
                    reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
                    warnings.append(
                        f"thread member add failed for {mention_uid}: {reason}"
                    )
                except Exception as exc:
                    warnings.append(
                        f"thread member add failed for {mention_uid}: "
                        f"{type(exc).__name__}: {exc}"
                    )
            for part in parts[1:]:
                thread_message_ids.append(_post_channel_message(token, thread_id, part))
        else:
            # Thread creation failed — the opening already exists in the
            # channel, so continue it there instead of losing the tail or
            # posting the anchor twice.
            for part in parts[1:]:
                channel_message_ids.append(
                    _post_channel_message(token, state["channel_id"], part)
                )

        result: Dict[str, Any] = {
            "success": True,
            "action": "start",
            "start_number": number,
            "channel_id": state["channel_id"],
            "channel_message_id": anchor_id,
            "channel_message_ids": channel_message_ids,
            "thread_message_ids": thread_message_ids,
        }
        if mention_uid:
            result["mentioned_user_id"] = mention_uid
        if thread_id:
            result["thread_id"] = thread_id
            result["thread_name"] = thread_name
            try:
                _mark_participated_thread(thread_id)
            except Exception as exc:
                warnings.append(f"thread listen mark failed: {exc}")
            seeded_key: Optional[str] = None
            try:
                seeded_key = _seed_thread_session(
                    thread_id,
                    thread_name,
                    composed,
                )
            except Exception as exc:
                seeded_key = None
                warnings.append(f"session seed failed: {exc}")
            if seeded_key:
                result["session_seed_key"] = seeded_key
        if warnings:
            result["warning"] = "; ".join(warnings)
        return json.dumps(result)
    except urllib.error.HTTPError as exc:
        return json.dumps({"success": False, "error": f"HTTP error: {exc.code}"})
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        return json.dumps({"success": False, "error": f"URL error: {reason}"})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def handle_start_conversation(args: dict, **kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "start").strip().lower()
        if action not in _ALLOWED_ACTIONS:
            return json.dumps(
                {"success": False, "error": f"action must be one of {sorted(_ALLOWED_ACTIONS)}"}
            )

        token = _read_discord_token()
        if not token:
            return json.dumps({"success": False, "error": "Discord bot token not configured"})

        if action == "setup":
            return _handle_setup(args, token)
        return _handle_start(args, token)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_requirements() -> bool:
    try:
        path = _env_path()
        if not path.is_file():
            return False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return bool(_parse_token_line(line))
        return False
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(
        name="start_conversation",
        toolset="hermes_starts",
        schema=START_CONVERSATION_SCHEMA,
        handler=handle_start_conversation,
        check_fn=check_requirements,
        emoji="💬",
    )
    # Static content, so every session renders identical bytes and the frozen
    # section never invalidates the prompt prefix mid-session.
    ctx.register_system_prompt_section(
        STRUCTURAL_ASKS_SECTION_ID,
        STRUCTURAL_ASKS_GUIDANCE,
        position="after_memory",
        max_chars=STRUCTURAL_ASKS_MAX_CHARS,
    )
