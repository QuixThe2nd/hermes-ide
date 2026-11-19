"""Per-turn lifecycle for adapter-owned browser-vault save-login prompts."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from gateway.session_identity import identity_of

logger = logging.getLogger(__name__)


@contextmanager
def vault_save_login_scope(adapter, *, source, session_key, loop, chat_id, metadata):
    """Bind one transport-owned save prompt on the agent worker thread.

    Unlock and OTP deliberately stay unavailable: Discord text inputs are unmasked,
    so an adapter binding created here authorizes save-login only. Tool worker threads
    inherit these thread-local callbacks through ``tools.thread_context``.
    """
    from agent.vault_backends.unlock import (
        get_code_prompt_callback,
        get_save_login_prompt_callback,
        get_unlock_prompt_callback,
        set_code_prompt_callback,
        set_save_login_prompt_callback,
        set_unlock_prompt_callback,
    )

    factory = getattr(type(adapter), "create_vault_save_login_binding", None)
    if not callable(factory):
        yield
        return
    from gateway.platforms.base import BasePlatformAdapter

    if factory is BasePlatformAdapter.create_vault_save_login_binding:
        yield
        return

    identity = identity_of(source)
    previous_save = get_save_login_prompt_callback()
    previous_unlock = get_unlock_prompt_callback()
    previous_code = get_code_prompt_callback()
    binding = None
    set_save_login_prompt_callback(None)
    set_unlock_prompt_callback(None)
    set_code_prompt_callback(None)
    try:
        if identity is not None:
            try:
                binding = factory(
                    adapter,
                    source=source,
                    identity=identity,
                    session_key=session_key,
                    loop=loop,
                    chat_id=chat_id,
                    metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "Vault save-login prompt binding unavailable: %s",
                    type(exc).__name__,
                )
        prompt = getattr(binding, "prompt", None)
        if callable(prompt):
            set_save_login_prompt_callback(prompt)
        yield
    finally:
        try:
            if binding is not None:
                binding.close()
        except Exception as exc:
            logger.warning(
                "Vault save-login prompt cleanup failed: %s", type(exc).__name__
            )
        finally:
            set_save_login_prompt_callback(previous_save)
            set_unlock_prompt_callback(previous_unlock)
            set_code_prompt_callback(previous_code)
