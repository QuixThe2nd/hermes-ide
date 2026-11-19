"""Resolve gateway ``terminal.cwd`` placeholder values to ``TERMINAL_CWD``.

When ``terminal.cwd`` is unset or a placeholder (``.``, ``auto``, ``cwd``),
the gateway must not blindly map host ``Path.home()`` into remote backends.
SSH with a placeholder cwd uses the backend default; local resolves to
``MESSAGING_CWD`` or ``home_fallback``.
"""

from __future__ import annotations

CWD_PLACEHOLDERS = frozenset({".", "auto", "cwd"})


def resolve_placeholder_terminal_cwd(
    *,
    configured_cwd: str,
    terminal_backend: str,
    messaging_cwd: str | None,
    home_fallback: str,
) -> str | None:
    """Return the ``TERMINAL_CWD`` value to set, or ``None`` to leave it unset.

    local + placeholder → ``MESSAGING_CWD`` or ``home_fallback``; any other
    backend → ``None`` (sandbox default).
    """
    if configured_cwd and configured_cwd not in CWD_PLACEHOLDERS:
        return configured_cwd
    backend = (terminal_backend or "local").strip().lower()
    messaging = (messaging_cwd or "").strip()
    if backend == "local":
        return messaging or home_fallback
    return None
