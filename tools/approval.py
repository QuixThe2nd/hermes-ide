"""Command hard floors and immediate pass-through gates.

Only unconditional blocks remain: catastrophic commands, password piping to
``sudo -S`` where applicable, and the user's ``approvals.deny`` rules. No gate
waits for, persists, or routes a human decision.
"""

import importlib
import logging

from tools.approval_detection import _check_sudo_stdin_guard, detect_hardline_command
from tools.approval_floors import (
    _hardline_block_result,
    _match_user_deny_rule,
    _sudo_stdin_block_result,
    _user_deny_block_result,
)

logger = logging.getLogger(__name__)


def _approved() -> dict:
    return {"approved": True, "message": None}


def callback_accepts(callback, keyword: str) -> bool:
    """Return whether a skill/memory write-approval callback accepts *keyword*."""
    import inspect

    try:
        params = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    return keyword in params or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in params.values()
    )


def request_elicitation_consent(
    message: str,
    description: str,
    *,
    timeout_seconds: int | None = None,
    surface: str = "mcp-elicitation",
    title: str = "Confirm this action?",
) -> str:
    """Accept MCP elicitation immediately; Hermes has no command approval prompt."""
    return "accept"


def clear_session(session_key: str) -> None:
    """Release resources owned by a session without retaining approval state."""
    if not session_key:
        return
    try:
        from tools.computer_use.tool import release_computer_use_session

        release_computer_use_session(session_key)
    except Exception:
        logger.debug("Failed to release computer-use resources for %s", session_key, exc_info=True)
    for module, shutdown in (
        ("tools.code_kernel", "shutdown_kernels_for_owner"),
        ("tools.code_kernel_remote", "shutdown_remote_kernels_for_owner"),
    ):
        try:
            getattr(importlib.import_module(module), shutdown)(session_key)
        except Exception:
            logger.debug("Failed to release %s for %s", module, session_key, exc_info=True)


def _user_deny_block(command: str) -> dict | None:
    pattern = _match_user_deny_rule(command)
    if pattern is None:
        return None
    logger.warning("User deny rule %r blocked command: %s", pattern, command[:200])
    return _user_deny_block_result(pattern)


def _floor_block(command: str, *, sudo_guard: bool = False) -> dict | None:
    """Return the first unconditional block, independent of backend or session."""
    is_hardline, description = detect_hardline_command(command)
    if is_hardline:
        logger.warning("Hardline block: %s (command: %s)", description, command[:200])
        return _hardline_block_result(description, command)
    if sudo_guard:
        is_sudo_guess, sudo_description = _check_sudo_stdin_guard(command)
        if is_sudo_guess:
            logger.warning("Sudo stdin guard block: %s (command: %s)", sudo_description, command[:200])
            return _sudo_stdin_block_result(sudo_description)
    return _user_deny_block(command)


def _run_approval_gate(
    *,
    pattern_key: str,
    description: str,
    display_target: str,
    approval_callback=None,
    subject: str = "",
    noun: str = "flagged actions",
    advice: str = "Find an alternative approach that avoids this action.",
    cron_deny_message: str = "",
    single_query_deny_message: str = "",
    unattended_deny_message: str = "",
    autoapprove_log_prefix: str = "",
    fail_closed_when_no_human: bool = False,
    no_human_block_message: str = "",
) -> dict:
    """Pass through immediately; callers enforce any applicable floors first."""
    return _approved()


def check_dangerous_command(
    command: str,
    env_type: str,
    approval_callback=None,
    has_host_access: bool = False,
) -> dict:
    """Apply command hard floors, then approve without consulting a human."""
    return _floor_block(command) or _approved()


def request_tool_approval(
    tool_name: str,
    reason: str,
    *,
    rule_key: str = "",
    approval_callback=None,
) -> dict:
    """Pass through a plugin's former ``pre_tool_call`` approval escalation."""
    return _approved()


def check_all_command_guards(
    command: str,
    env_type: str,
    approval_callback=None,
    has_host_access: bool = False,
) -> dict:
    """Apply all unconditional command floors on every backend, then approve."""
    return _floor_block(command, sudo_guard=True) or _approved()


def check_execute_code_guard(code: str, env_type: str, has_host_access: bool = False) -> dict:
    """Approve code execution immediately; nested terminal calls keep their floors."""
    return _approved()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
import contextlib  # noqa: F401,E402
import contextvars  # noqa: F401,E402
import fnmatch  # noqa: F401,E402
import functools  # noqa: F401,E402
import re  # noqa: F401,E402
import shlex  # noqa: F401,E402
import sys  # noqa: F401,E402
import tempfile  # noqa: F401,E402
import time  # noqa: F401,E402
import unicodedata  # noqa: F401,E402
import uuid  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    "DANGEROUS_PATTERNS": ("tools.approval_detection", "DANGEROUS_PATTERNS"),
    "DANGEROUS_PATTERNS_COMPILED": ("tools.approval_detection", "DANGEROUS_PATTERNS_COMPILED"),
    "HARDLINE_PATTERNS": ("tools.approval_detection", "HARDLINE_PATTERNS"),
    "HARDLINE_PATTERNS_COMPILED": ("tools.approval_detection", "HARDLINE_PATTERNS_COMPILED"),
    "HUMAN_WAIT_MARGIN_S": ("tools.approval_human_wait", "HUMAN_WAIT_MARGIN_S"),
    "cfg_get": ("hermes_cli.config", "cfg_get"),
    "human_wait_ceiling": ("tools.approval_human_wait", "human_wait_ceiling"),
    "human_wait_seconds": ("tools.approval_human_wait", "human_wait_seconds"),
    "human_wait_window": ("tools.approval_human_wait", "human_wait_window"),
    "is_interrupted": ("tools.interrupt", "is_interrupted"),
    "reset_current_observability_context": ("tools.approval_context", "reset_current_observability_context"),
    "reset_current_session_key": ("tools.approval_context", "reset_current_session_key"),
    "set_current_observability_context": ("tools.approval_context", "set_current_observability_context"),
    "set_current_session_key": ("tools.approval_context", "set_current_session_key"),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from hermes_cli.plugin_compat import warn_once

    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
