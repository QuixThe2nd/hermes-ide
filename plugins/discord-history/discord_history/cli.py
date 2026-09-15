"""Argparse and stable output contracts for ``hermes discord-history``."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


def snowflake(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or not 17 <= len(value) <= 20:
        raise argparse.ArgumentTypeError("must be a 17-20 digit Discord snowflake")
    return value


def audit_id(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive decimal audit ID")
    return value


def expected_phrase(value: str) -> str:
    if not 1 <= len(value) <= 500 or "\x00" in value:
        raise argparse.ArgumentTypeError("must be 1-500 characters without NUL")
    return value


class _Channels(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        current = list(getattr(namespace, self.dest, None) or [])
        if len(current) >= 100:
            parser.error("--channel may be repeated at most 100 times")
        current.append(values)
        setattr(namespace, self.dest, current)


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="json_output")


def setup_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="discord_history_command")
    for name in ("init", "doctor"):
        p = subs.add_parser(name)
        _json_flag(p)
    inventory = subs.add_parser("inventory")
    inventory.add_argument("--guild", required=True, type=snowflake)
    _json_flag(inventory)
    sync = subs.add_parser("sync")
    sync.add_argument("--guild", required=True, type=snowflake)
    sync.add_argument("--channel", type=snowflake, action=_Channels)
    sync.add_argument("--mode", choices=("backfill", "incremental", "reconcile"), default="incremental")
    sync.add_argument("--keep-export", action="store_true")
    _json_flag(sync)
    reconcile = subs.add_parser("reconcile")
    reconcile.add_argument("--guild", required=True, type=snowflake)
    reconcile.add_argument("--channel", type=snowflake, action=_Channels)
    reconcile.add_argument("--keep-export", action="store_true")
    _json_flag(reconcile)
    status = subs.add_parser("status")
    status.add_argument("--guild", type=snowflake)
    status.add_argument("--channel", type=snowflake)
    _json_flag(status)
    verify = subs.add_parser("verify")
    verify.add_argument("--channel", required=True, type=snowflake)
    _json_flag(verify)
    e2e = subs.add_parser("verify-e2e")
    e2e.add_argument("--guild", required=True, type=snowflake)
    e2e.add_argument("--owner-audit-id", required=True, type=audit_id)
    e2e.add_argument("--expected-message-id", required=True, type=snowflake)
    e2e.add_argument("--expected-phrase", type=expected_phrase,
                     help="optional exact phrase strengthening the message check")
    _json_flag(e2e)
    recall = subs.add_parser(
        "recall",
        help=(
            "Owner-authorized recall via the same model-tool path that runs in-chat. "
            "Binds a Discord session context (platform, user, chat, thread) and "
            "invokes the discord_history tool. Use to confirm the tool works from "
            "a fresh shell exactly as it works for the live thread."
        ),
    )
    recall.add_argument("--guild", required=True, type=snowflake,
                        help="Discord guild snowflake (17-20 digits).")
    recall.add_argument("--thread", dest="thread_id", type=snowflake,
                        help="Discord thread snowflake to bind as the current session.")
    recall.add_argument("--chat", dest="chat_id", type=snowflake,
                        help="Override the chat_id (defaults to --thread). Use the "
                        "thread's parent channel id to bind a session shaped like a "
                        "legacy Discord thread caller.")
    recall.add_argument("--user", dest="user_id", required=True, type=snowflake,
                        help="Discord user snowflake whose session is being simulated.")
    recall.add_argument("--action", choices=("search", "get", "context", "status"),
                        default="status",
                        help="discord_history action (default: status).")
    recall.add_argument("--query",
                        help="Search query (required when --action=search).")
    recall.add_argument("--message-id", dest="message_id", type=snowflake,
                        help="message snowflake for --action=get|context.")
    recall.add_argument("--channel", dest="channel_ids", type=snowflake,
                        action=_Channels,
                        help="Restrict to a specific channel (repeatable, at most 100).")
    recall.add_argument("--limit", type=int,
                        help="Bounded result limit (default per action: 10 search/get, 3/3 context).")
    recall.add_argument("--context-before", dest="context_before", type=int,
                        help="context_before for --action=context (0-20).")
    recall.add_argument("--context-after", dest="context_after", type=int,
                        help="context_after for --action=context (0-20).")
    recall.add_argument("--after",
                        help="Lower bound timestamp (ISO 8601) for --action=search.")
    recall.add_argument("--before",
                        help="Upper bound timestamp (ISO 8601) for --action=search.")
    _json_flag(recall)
    parser.set_defaults(func=cli_entry)


def _config_mapping() -> dict[str, Any]:
    from hermes_cli.config import load_config
    cfg = load_config() or {}
    return (((cfg.get("plugins") or {}).get("entries") or {}).get("discord-history") or {}).get("config") or {}


def _connection():
    from .config import load_secrets
    from .db import connect
    return connect(load_secrets().database_url)


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    from .db import apply_migrations
    conn = _connection()
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    return {"ok": True, "command": "init", "applied": applied}


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    from .doctor import run_doctor
    return {"command": "doctor", **run_doctor()}


def cmd_inventory(args: argparse.Namespace) -> dict[str, Any]:
    from .service import run_inventory
    return run_inventory(guild_id=args.guild)


def cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    # The mutating orchestrator is deliberately imported only on invocation.
    from .service import run_sync
    return run_sync(guild_id=args.guild, channel_ids=args.channel, mode=args.mode,
                    keep_export=args.keep_export)


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    from .service import archive_status
    return {"ok": True, "command": "status",
            "status": archive_status(guild_id=args.guild, channel_id=args.channel)}


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    from .service import run_channel_verification
    result = run_channel_verification(args.channel)
    return {"command": "verify", **result}


def cmd_verify_e2e(args: argparse.Namespace) -> dict[str, Any]:
    from .config import load_secrets
    from .service import (load_plugin_config, run_denial_and_retrieval_probes,
                          run_live_acceptance_probes)
    from .verify import verify_e2e
    secrets = load_secrets()
    owner_hmacs = [hmac.new(secrets.audit_hmac_key, owner.encode("ascii"),
                            hashlib.sha256).hexdigest()
                   for owner in sorted(load_plugin_config().owner_user_ids)]
    conn = _connection()
    try:
        from .db import apply_migrations
        from .service import run_channel_verification
        first_reapply = apply_migrations(conn)
        second_reapply = apply_migrations(conn)
        channel_row = conn.execute(
            "SELECT channel_id FROM discord_archive.messages "
            "WHERE guild_id=%s AND message_id=%s AND deleted_at IS NULL",
            (args.guild, args.expected_message_id),
        ).fetchone()
        channel_verification = (run_channel_verification(str(channel_row[0]))
                                if channel_row else {"ok": False, "checks": {}})
        probes = {
            **run_denial_and_retrieval_probes(),
            **run_live_acceptance_probes(
                guild_id=args.guild,
                expected_message_id=args.expected_message_id,
                expected_phrase=args.expected_phrase,
            ),
            "schema_migration_reapply_noop": not first_reapply and not second_reapply,
            "full_channel_verification": bool(channel_verification.get("ok")),
            **{f"channel_{key}": bool(value)
               for key, value in (channel_verification.get("checks") or {}).items()},
        }
        result = verify_e2e(conn, guild_id=args.guild,
                            expected_message_id=args.expected_message_id,
                            expected_phrase=args.expected_phrase,
                            owner_audit_id=args.owner_audit_id,
                            owner_principal_hmacs=owner_hmacs,
                            probes=probes)
    finally:
        conn.close()
    return {"ok": result["verdict"] == "PASS", "command": "verify-e2e", **result}



def cmd_recall(args: argparse.Namespace) -> dict[str, Any]:
    from .service import handle_discord_history

    arguments: dict[str, Any] = {"action": args.action, "guild_id": args.guild}
    if args.query is not None:
        arguments["query"] = args.query
    if args.message_id is not None:
        arguments["message_id"] = args.message_id
    if getattr(args, "channel_ids", None):
        arguments["channel_ids"] = list(args.channel_ids)
    if args.limit is not None:
        arguments["limit"] = args.limit
    if args.context_before is not None:
        arguments["context_before"] = args.context_before
    if args.context_after is not None:
        arguments["context_after"] = args.context_after
    if args.after is not None:
        arguments["after"] = args.after
    if args.before is not None:
        arguments["before"] = args.before

    chat_id = args.chat_id or args.thread_id or ""
    thread_id = args.thread_id or ""
    if args.chat_id and not args.thread_id:
        return {"ok": False, "command": "recall",
                "error": "missing_session_thread",
                "detail": "--thread is required alongside --chat for the legacy parent-shaped caller shape"}
    if not chat_id:
        return {"ok": False, "command": "recall", "error": "missing_session_chat"}

    try:
        from gateway.session_context import set_session_vars, clear_session_vars
    except Exception as exc:
        return {"ok": False, "command": "recall",
                "error": "session_context_unavailable",
                "detail": type(exc).__name__}

    # Force-disable bytecode writes for this process so that any import of
    # discord_history.* during the recall call does not regrow __pycache__/ under
    # the deployed plugin tree. This is the runtime equivalent of running the
    # CLI with PYTHONDONTWRITEBYTECODE=1.
    try:
        sys.dont_write_bytecode = True
    except Exception:
        pass

    tokens = set_session_vars(
        platform="discord",
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=args.user_id,
        user_name="cli-recall",
    )
    try:
        response = handle_discord_history(arguments)
    finally:
        try:
            clear_session_vars(tokens)
        except Exception:
            pass

    try:
        parsed = json.loads(response)
    except Exception:
        return {"ok": False, "command": "recall", "error": "invalid_response"}

    if isinstance(parsed, dict) and parsed.get("error"):
        return {"ok": False, "command": "recall", "error": parsed["error"]}

    return {"ok": True, "command": "recall",
            "session": {"platform": "discord", "guild_id": args.guild,
                        "chat_id": chat_id, "thread_id": thread_id,
                        "user_id": args.user_id},
            "action": args.action, "response": parsed}


_COMMANDS: dict[str, Callable[[argparse.Namespace], dict[str, Any]]] = {
    "init": cmd_init, "doctor": cmd_doctor, "inventory": cmd_inventory,
    "sync": cmd_sync, "reconcile": cmd_sync, "status": cmd_status,
    "verify": cmd_verify, "verify-e2e": cmd_verify_e2e,
    "recall": cmd_recall,
}


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    elif "verdict" in result:
        print(result["verdict"])
    else:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))


def handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "discord_history_command", None)
    if not command:
        print("usage: hermes discord-history {init,doctor,inventory,sync,reconcile,status,verify,verify-e2e}",
              file=sys.stderr)
        return 2
    if command == "reconcile":
        args.mode = "reconcile"
    try:
        result = _COMMANDS[command](args)
        if not isinstance(result, dict):
            raise RuntimeError("invalid_command_result")
    except Exception as exc:
        code = getattr(exc, "code", None) or "operation_failed"
        # Stable and secret-free: never include exception repr, text, or arguments.
        result = {"ok": False, "command": command, "error": str(code)[:120]}
        _emit(result, bool(getattr(args, "json_output", False)))
        return 1
    _emit(result, bool(getattr(args, "json_output", False)))
    if command == "status":
        return 0
    if command == "verify-e2e":
        return 0 if result.get("verdict") == "PASS" else 1
    return 0 if result.get("ok", False) else 1


def cli_entry(args: argparse.Namespace) -> int:
    """Process-level adapter because Hermes core intentionally ignores handler returns."""
    code = handle_cli(args)
    if code:
        raise SystemExit(code)
    return code
