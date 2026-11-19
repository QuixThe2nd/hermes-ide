"""Propagate agent-turn context and interactive callbacks into tool workers.

Call :func:`propagate_context_to_thread` on the parent thread. The wrapper
snapshots context variables plus sudo, memory-review, and vault callbacks,
installs them for the worker's lifetime, and clears the callbacks on exit.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """(getter, setter) pairs for thread-local sudo, memory-review, and vault prompts."""
    from agent.vault_backends import unlock as vault_unlock
    from tools import terminal_tool as tt
    from tools import write_approval

    return ((tt._get_sudo_password_callback, tt.set_sudo_password_callback),
            (write_approval.get_inline_prompt_callback, write_approval.set_inline_prompt_callback),
            (vault_unlock.get_unlock_prompt_callback, vault_unlock.set_unlock_prompt_callback),
            (vault_unlock.get_save_login_prompt_callback, vault_unlock.set_save_login_prompt_callback),
            (vault_unlock.get_code_prompt_callback, vault_unlock.set_code_prompt_callback))


def propagate_context_to_thread(target: Callable) -> Callable:
    """Wrap *target* with the current context and per-thread prompt callbacks."""
    ctx = contextvars.copy_context()
    installs = None
    try:
        installs = tuple((setter, getter()) for getter, setter in _callback_api())
    except Exception:
        logger.debug("Could not capture parent prompt callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            if installs is None:
                return target(*args, **kwargs)
            try:
                for setter, cb in installs:
                    if cb is not None:
                        setter(cb)
            except Exception:
                logger.debug("Failed to install propagated prompt callbacks", exc_info=True)
            try:
                return target(*args, **kwargs)
            finally:
                try:
                    for setter, _cb in installs:
                        setter(None)
                except Exception:
                    logger.debug("Failed to clear propagated prompt callbacks", exc_info=True)

        return ctx.run(_inner)

    return _runner
