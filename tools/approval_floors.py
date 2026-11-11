"""Unconditional command floors evaluated before every pass-through gate."""

import contextlib
import fnmatch
import logging
import time
import uuid

from tools import approval_context as _ctx
from tools.approval_detection import (
    _MALFORMED_EXEC_DESCRIPTION,
    _PARSER_LIMIT_DESCRIPTION,
    _deny_command_variants,
)

logger = logging.getLogger("tools.approval")


def _match_user_deny_rule(command: str) -> str | None:
    """Return the matching case-insensitive ``approvals.deny`` glob, if any."""
    try:
        deny_patterns = _ctx._get_approval_config().get("deny") or []
    except Exception:
        return None
    globs = [pattern.strip() for pattern in deny_patterns if isinstance(pattern, str) and pattern.strip()]
    if not globs:
        return None
    for command_variant in _deny_command_variants(command):
        candidate = command_variant.lower().strip()
        for pattern in globs:
            if fnmatch.fnmatchcase(candidate, pattern.lower()):
                return pattern
    return None


def _user_deny_block_result(pattern: str) -> dict:
    return {
        "approved": False,
        "user_deny": True,
        "message": (
            f"BLOCKED: this command matches the user-defined deny rule '{pattern}' "
            "(approvals.deny in config.yaml). It cannot be executed via the agent. "
            "Do NOT retry or rephrase this command; the user has explicitly forbidden it."
        ),
    }


def _save_blocked_payload(command: str) -> str | None:
    """Persist a parser-limit-blocked payload for review and explicit execution."""
    try:
        from hermes_constants import get_hermes_home

        script_dir = get_hermes_home() / "cache" / "blocked-scripts"
        script_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 7 * 86400
        for old in script_dir.glob("blocked-*.sh"):
            with contextlib.suppress(OSError):
                if old.stat().st_mtime < cutoff:
                    old.unlink()
        path = script_dir / f"blocked-{int(time.time())}-{uuid.uuid4().hex[:8]}.sh"
        path.write_text(
            "#!/bin/bash\n"
            "# Auto-saved by Hermes: this command exceeded the inline command\n"
            "# parser limit and was blocked from direct execution. Review it,\n"
            f"# then run it via: bash {path}\n" + command + ("" if command.endswith("\n") else "\n"),
            encoding="utf-8",
            errors="replace",
        )
        return str(path)
    except Exception:
        logger.debug("failed to save blocked payload", exc_info=True)
        return None


_RECOVERY_PREFIX = (
    " RECOVERY: this block fires on oversized/unparseable inline "
    "command payloads (heredocs, giant one-liners), not on the operation itself. "
)


def _hardline_block_result(description: str, command: str = "") -> dict:
    """Build the standard block result for a hardline match."""
    message = (
        f"BLOCKED (hardline): {description}. "
        "This command is on the unconditional blocklist and cannot be executed via the agent. "
        "If you genuinely need to run it, run it yourself in a terminal outside the agent."
    )
    if description in (_PARSER_LIMIT_DESCRIPTION, _MALFORMED_EXEC_DESCRIPTION):
        saved = _save_blocked_payload(command) if command else None
        if saved:
            message += _RECOVERY_PREFIX + (
                f"Your command was saved to {saved} — review it, then run: terminal(command=\"bash {saved}\"). "
                "Do not retry inline."
            )
        else:
            message += _RECOVERY_PREFIX + (
                "Write the script to a file with write_file, then run it: "
                "terminal(command=\"bash /path/script.sh\") or \"python3 /path/script.py\". Do not retry inline."
            )
    return {"approved": False, "hardline": True, "message": message}


def _sudo_stdin_block_result(description: str) -> dict:
    return {
        "approved": False,
        "message": (
            f"BLOCKED: {description}. Do not pipe passwords to 'sudo -S' — this is a brute-force "
            "attack vector. Set SUDO_PASSWORD in your .env file if the agent needs passwordless sudo, "
            "or run the sudo command manually in your own terminal."
        ),
    }
