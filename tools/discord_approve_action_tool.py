"""Reaction-gated approval cards for prepared Discord actions.

``action='propose'`` posts a self-reacted ✅/❌ embed and records a pending
listener. The Discord platform adapter forwards raw reaction events to
:func:`handle_approve_reaction`; a decision executes the prepared action at
most once and a late reaction settles an expired card without executing it.

Security contract: only a reactor whose Discord user ID is literally listed in
the adapter's ``DISCORD_ALLOWED_USERS`` set may approve or decline. The ``*``
wildcard, guild/role/channel grants, pairing, and allow-all modes do not
authorize prepared actions. The handler also fails closed when called without
an explicit authorization decision. ``run_script`` is restricted to the active
profile's ``scripts`` directory, and no Discord API call in this module uses
DELETE.
"""

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import display_hermes_home, get_hermes_home
from tools.discord_tool import (
    DiscordAPIError,
    _discord_request,
    _get_bot_token,
    check_discord_tool_requirements,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_EMBED_COLOR = 0x57F287  # Discord green
_EMBED_COLOR_MUTED = 0x95A5A6  # gray

EMOJI_CONFIRM = "✅"
EMOJI_DECLINE = "❌"
_FOOTER_MARKER = "hermes action approval"

_DEFAULT_TIMEOUT_MINUTES = 7 * 24 * 60
_MAX_TIMEOUT_MINUTES = 7 * 24 * 60
_SCRIPT_TIMEOUT_SECONDS = 10 * 60
_RESULT_TEXT_LIMIT = 1500
_PENDING_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pending-listener store
# ---------------------------------------------------------------------------


def _pending_path():
    return get_hermes_home() / "discord_approve_pending.json"


def _load_pending() -> Dict[str, Any]:
    try:
        value = json.loads(_pending_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_pending(pending: Dict[str, Any]) -> None:
    path = _pending_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending, indent=1), encoding="utf-8")
    tmp.replace(path)


def _prune_expired(
    pending: Dict[str, Any], now: Optional[float] = None
) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    return {k: v for k, v in pending.items() if v.get("expires_at", 0) > now}


def _default_timeout_minutes() -> int:
    try:
        from hermes_cli.config import load_config

        value = (load_config().get("discord") or {}).get(
            "approve_action_timeout_minutes"
        )
        if value is not None:
            return max(1, min(int(value), _MAX_TIMEOUT_MINUTES))
    except Exception:
        pass
    return _DEFAULT_TIMEOUT_MINUTES


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def _required_text(payload: Dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"action_payload.{key} must be a non-empty string.")
    return value.strip()


def _validate_schedule_cron(payload: Dict[str, Any]) -> None:
    _required_text(payload, "schedule")
    _required_text(payload, "prompt")
    for key in ("name", "deliver"):
        if (
            key in payload
            and payload[key] is not None
            and not isinstance(payload[key], str)
        ):
            raise ValueError(f"action_payload.{key} must be a string when provided.")


def _validate_post_message(payload: Dict[str, Any]) -> None:
    _required_text(payload, "channel_id")
    _required_text(payload, "content")


def _script_path(payload: Dict[str, Any]) -> Path:
    raw_path = _required_text(payload, "path")
    scripts_root = (get_hermes_home() / "scripts").resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = scripts_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(scripts_root)
    except ValueError as exc:
        raise ValueError(
            f"run_script path must live under {display_hermes_home()}/scripts."
        ) from exc
    if resolved == scripts_root:
        raise ValueError(
            "run_script path must name a script, not the scripts directory."
        )
    return resolved


def _validate_run_script(payload: Dict[str, Any]) -> None:
    _script_path(payload)
    args = payload.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError(
            "action_payload.args must be a JSON array of strings when provided."
        )


def _validate_custom_note(payload: Dict[str, Any]) -> None:
    return None


_PAYLOAD_VALIDATORS = {
    "schedule_cron": _validate_schedule_cron,
    "post_message": _validate_post_message,
    "run_script": _validate_run_script,
    "custom_note": _validate_custom_note,
}


def _parse_payload(action_kind: str, raw_payload: str) -> Dict[str, Any]:
    if action_kind not in _PAYLOAD_VALIDATORS:
        choices = ", ".join(_PAYLOAD_VALIDATORS)
        raise ValueError(f"Unknown action_kind '{action_kind}'. Use one of: {choices}.")
    if not isinstance(raw_payload, str):
        raise ValueError("action_payload must be a JSON-encoded object string.")
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("action_payload must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("action_payload must decode to a JSON object.")
    _PAYLOAD_VALIDATORS[action_kind](payload)
    return payload


# ---------------------------------------------------------------------------
# Discord REST helpers
# ---------------------------------------------------------------------------


def _add_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> None:
    encoded = urllib.parse.quote(emoji, safe="")
    _discord_request(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        token,
    )


def _edit_embed(
    token: str, channel_id: str, message_id: str, embed: Dict[str, Any]
) -> None:
    _discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        token,
        body={"embeds": [embed]},
    )


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _post_embed(
    token: str,
    channel_id: str,
    summary: str,
    action_kind: str,
    timeout_minutes: int,
    require_comment: bool,
) -> Dict[str, Any]:
    comment_note = (
        "\nThe proposal also requests a follow-up comment for agent-side context."
        if require_comment
        else ""
    )
    embed = {
        "title": _truncate(f"✅ Approve: {summary}", 256),
        "description": _truncate(
            f"{summary}\n\n"
            f"React {EMOJI_CONFIRM} to execute the prepared `{action_kind}` action described above. "
            f"React {EMOJI_DECLINE} to decline without executing it.{comment_note}\n\n"
            f"_Listening for {timeout_minutes} minute{'s' if timeout_minutes != 1 else ''}, "
            "then this prompt expires._",
            4096,
        ),
        "color": _EMBED_COLOR,
        "footer": {"text": _FOOTER_MARKER},
    }
    message = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        body={"embeds": [embed]},
    )
    message_id = (message or {}).get("id")
    if message_id:
        for emoji in (EMOJI_CONFIRM, EMOJI_DECLINE):
            try:
                _add_reaction(token, channel_id, message_id, emoji)
            except DiscordAPIError as exc:
                logger.warning(
                    "approve_action: adding %s reaction failed (%s)", emoji, exc
                )
    return message


def _settle_embed_best_effort(
    token: str, channel_id: str, message_id: str, embed: Dict[str, Any]
) -> None:
    try:
        _edit_embed(token, channel_id, message_id, embed)
    except DiscordAPIError as exc:
        logger.warning("approve_action: terminal embed edit failed (%s)", exc)


# ---------------------------------------------------------------------------
# Prepared actions
# ---------------------------------------------------------------------------


def _execute_schedule_cron(_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    from cron.scheduler import (
        CronSchedulerRegistrationError,
        create_job_with_scheduler_registration,
    )

    try:
        job = create_job_with_scheduler_registration(
            schedule=_required_text(payload, "schedule"),
            prompt=_required_text(payload, "prompt"),
            name=payload.get("name"),
            deliver=payload.get("deliver"),
            paused=False,
        )
    except CronSchedulerRegistrationError as exc:
        return {
            "success": False,
            "message": (
                f"Cron job '{exc.job.get('name', exc.job['id'])}' was saved, but scheduler "
                "registration failed; do not create a duplicate."
            ),
            "job_id": exc.job["id"],
            "job_saved": True,
            "scheduler_registered": False,
        }
    return {
        "success": True,
        "message": f"Cron job '{job['name']}' created ({job['id']}).",
        "job_id": job["id"],
        "next_run_at": job.get("next_run_at"),
    }


def _execute_post_message(token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    channel_id = _required_text(payload, "channel_id")
    message = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        body={"content": _required_text(payload, "content")},
    )
    return {
        "success": True,
        "message": f"Message posted to channel {channel_id}.",
        "message_id": (message or {}).get("id"),
    }


def _execute_run_script(_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    script = _script_path(payload)
    if not script.is_file():
        return {
            "success": False,
            "message": f"Script does not exist or is not a file: {script}",
        }

    # Re-check at execution time: a previously validated path may have been replaced by a symlink.
    from tools.environments.local import served_profile_child_env

    args = payload.get("args", [])
    command = (
        [sys.executable, str(script)]
        if script.suffix.lower() == ".py"
        else [str(script)]
    )
    try:
        completed = subprocess.run(
            command + args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_SCRIPT_TIMEOUT_SECONDS,
            cwd=str(script.parent),
            env=served_profile_child_env(
                target_home=get_hermes_home(), inherit_credentials=False
            ),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": f"Script timed out after {_SCRIPT_TIMEOUT_SECONDS} seconds.",
        }

    stdout = (completed.stdout or "")[-_RESULT_TEXT_LIMIT:]
    stderr = (completed.stderr or "")[-_RESULT_TEXT_LIMIT:]
    success = completed.returncode == 0
    return {
        "success": success,
        "message": (
            f"Script completed successfully: {script.name}."
            if success
            else f"Script exited with code {completed.returncode}: {script.name}."
        ),
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _execute_custom_note(_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": True,
        "message": "Decision recorded for agent-side follow-up.",
        "note": payload,
    }


_ACTION_HANDLERS = {
    "schedule_cron": _execute_schedule_cron,
    "post_message": _execute_post_message,
    "run_script": _execute_run_script,
    "custom_note": _execute_custom_note,
}


def _execute_action(action_kind: str, raw_payload: str, token: str) -> Dict[str, Any]:
    payload = _parse_payload(action_kind, raw_payload)
    return _ACTION_HANDLERS[action_kind](token, payload)


# ---------------------------------------------------------------------------
# Reaction listener entry point (called by the Discord platform adapter)
# ---------------------------------------------------------------------------


def handle_approve_reaction(
    channel_id: str,
    message_id: str,
    emoji: str,
    token: Optional[str] = None,
    *,
    reactor_id: Optional[str] = None,
    authorized: bool = False,
) -> Dict[str, Any]:
    """Settle one pending approval card from an authorized raw reaction.

    The adapter filters its own reactions, passes its configured token
    explicitly because raw gateway events run outside the per-turn secret
    scope, and computes authorization from its literal user-ID allowlist.
    Direct callers fail closed unless they explicitly pass ``authorized=True``.
    """
    if emoji not in (EMOJI_CONFIRM, EMOJI_DECLINE):
        return {"acted": False, "reason": "unrelated_emoji"}

    pending = _load_pending()
    entry = pending.get(message_id)
    if not entry or entry.get("channel_id") != channel_id:
        return {"acted": False, "reason": "not_listening"}

    if not authorized:
        logger.warning(
            "approve_action: unauthorized reactor %s for message %s",
            reactor_id or "<unknown>",
            message_id,
        )
        return {"acted": False, "reason": "unauthorized_reactor"}

    token = (token or "").strip() or _get_bot_token()
    if not token:
        return {"acted": False, "reason": "no_token"}

    # Claim the entry atomically within the gateway process. The adapter's
    # to_thread dispatch can deliver two reactions concurrently; external
    # effects stay outside the lock, but only the claimant reaches them.
    with _PENDING_LOCK:
        pending = _load_pending()
        entry = pending.get(message_id)
        if not entry or entry.get("channel_id") != channel_id:
            return {"acted": False, "reason": "not_listening"}
        summary = entry.get("summary", "")
        expired = entry.get("expires_at", 0) <= time.time()
        pending.pop(message_id, None)
        _save_pending(pending)

    if expired:
        _settle_embed_best_effort(
            token,
            channel_id,
            message_id,
            {
                "title": "⌛ Approval expired",
                "description": _truncate(summary, 4096),
                "color": _EMBED_COLOR_MUTED,
                "footer": {"text": f"{_FOOTER_MARKER} • expired"},
            },
        )
        return {"acted": False, "reason": "timed_out"}

    if emoji == EMOJI_DECLINE:
        _settle_embed_best_effort(
            token,
            channel_id,
            message_id,
            {
                "title": "❌ Declined",
                "description": _truncate(
                    f"{summary}\n\nThe prepared action was not executed.", 4096
                ),
                "color": _EMBED_COLOR_MUTED,
                "footer": {"text": f"{_FOOTER_MARKER} • declined"},
            },
        )
        return {"acted": True, "decision": "declined"}

    try:
        result = _execute_action(
            entry.get("action_kind", ""), entry.get("action_payload", ""), token
        )
    except Exception as exc:
        logger.exception("approve_action: prepared action failed")
        result = {
            "success": False,
            "message": f"Action failed: {type(exc).__name__}: {exc}",
        }

    outcome = result.get("message", "Action completed.")
    _settle_embed_best_effort(
        token,
        channel_id,
        message_id,
        {
            "title": _truncate(f"✅ Approved — {outcome}", 256),
            "description": _truncate(f"{summary}\n\n{outcome}", 4096),
            "color": _EMBED_COLOR,
            "footer": {"text": f"{_FOOTER_MARKER} • approved"},
        },
    )
    return {
        "acted": True,
        "decision": "approved",
        "success": bool(result.get("success")),
        "action_kind": entry.get("action_kind"),
        "result": result,
    }


# ---------------------------------------------------------------------------
# Model-facing tool
# ---------------------------------------------------------------------------


def discord_approve_action(
    action: str = "propose",
    channel_id: str = "",
    summary: str = "",
    action_kind: str = "custom_note",
    action_payload: str = "{}",
    timeout_minutes: Optional[int] = None,
    require_comment: bool = False,
    task_id: Optional[str] = None,
) -> str:
    token = _get_bot_token()
    if not token:
        return tool_error("DISCORD_BOT_TOKEN is not configured.")
    if action != "propose":
        return tool_error(f"Unknown action '{action}'. Use 'propose'.")

    channel_id = (channel_id or "").strip()
    summary = (summary or "").strip()
    if not channel_id:
        return tool_error(
            "channel_id is required — pass the current Discord channel ID."
        )
    if not summary:
        return tool_error(
            "summary is required — describe exactly what approval will do."
        )
    if not isinstance(require_comment, bool):
        return tool_error("require_comment must be a boolean.")

    try:
        _parse_payload(action_kind, action_payload)
        minutes = (
            _default_timeout_minutes()
            if timeout_minutes is None
            else max(1, min(int(timeout_minutes), _MAX_TIMEOUT_MINUTES))
        )
        message = _post_embed(
            token, channel_id, summary, action_kind, minutes, require_comment
        )
    except DiscordAPIError as exc:
        return tool_error(f"Discord API error {exc.status}: {exc.body[:500]}")
    except (TypeError, ValueError) as exc:
        return tool_error(str(exc))

    message_id = (message or {}).get("id")
    if not message_id:
        return tool_error(
            "Discord did not return a message id; no approval listener was registered."
        )

    expires_at = time.time() + minutes * 60
    with _PENDING_LOCK:
        pending = _prune_expired(_load_pending())
        pending[message_id] = {
            "channel_id": channel_id,
            "summary": summary,
            "action_kind": action_kind,
            "action_payload": action_payload,
            "expires_at": expires_at,
            "require_comment": require_comment,
        }
        _save_pending(pending)
    return json.dumps({
        "success": True,
        "action": "propose",
        "channel_id": channel_id,
        "message_id": message_id,
        "action_kind": action_kind,
        "timeout_minutes": minutes,
        "require_comment": require_comment,
        "note": (
            f"Approval embed posted; listening for a non-bot ✅/❌ reaction for {minutes} minutes. "
            "The raw-reaction convention does not verify the reactor's user id."
        ),
    })


_SCHEMA: Dict[str, Any] = {
    "name": "discord_approve_action",
    "description": (
        "Post a Discord approval card for one prepared action. ✅ executes it; ❌ declines without "
        "executing it. schedule_cron creates an active cron job, post_message posts new content, "
        "run_script runs a prepared script under "
        f"{display_hermes_home()}/scripts with a 10-minute timeout, and custom_note only records "
        "the decision. No Discord resource is deleted. A successful propose call is terminal: the "
        "embed is the reply — end your turn immediately with no follow-up assistant message and no "
        "further tool calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["propose"],
                "default": "propose",
                "description": "Post an approval card and wait for a reaction.",
            },
            "channel_id": {
                "type": "string",
                "description": "Discord channel ID where the approval card will be posted.",
            },
            "summary": {
                "type": "string",
                "description": "Exact, human-readable description of what ✅ will do.",
            },
            "action_kind": {
                "type": "string",
                "enum": list(_ACTION_HANDLERS),
                "description": "The prepared action to execute after approval.",
            },
            "action_payload": {
                "type": "string",
                "description": (
                    "JSON-encoded object. schedule_cron: {schedule,prompt,name?,deliver?}; "
                    "post_message: {channel_id,content}; run_script: {path,args?}; custom_note: {} "
                    "or agent-specific note fields."
                ),
            },
            "timeout_minutes": {
                "type": "integer",
                "description": (
                    "Reaction-listening lifetime, from discord.approve_action_timeout_minutes "
                    "in config.yaml when omitted, else 10080 minutes (7 days); maximum 7 days."
                ),
            },
            "require_comment": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Record that the proposal requests a follow-up comment for agent-side context. "
                    "Raw Discord reactions do not themselves carry comments."
                ),
            },
        },
        "required": [
            "action",
            "channel_id",
            "summary",
            "action_kind",
            "action_payload",
        ],
    },
}

registry.register(
    name="discord_approve_action",
    toolset="discord",
    schema=_SCHEMA,
    handler=lambda args, **kw: discord_approve_action(
        action=args.get("action", "propose"),
        channel_id=args.get("channel_id", ""),
        summary=args.get("summary", ""),
        action_kind=args.get("action_kind", "custom_note"),
        action_payload=args.get("action_payload", "{}"),
        timeout_minutes=args.get("timeout_minutes"),
        require_comment=args.get("require_comment", False),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_discord_tool_requirements,
    requires_env=["DISCORD_BOT_TOKEN"],
)
