"""Fallback watch plugin — alert a Discord channel when the primary model falls back."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Service-only plugin: the watcher runs from ``run.py`` under systemd.

    No model tools and no gateway hooks — registration is intentionally
    empty so plugin discovery has something to call. All logic lives in
    ``plugins.fallback_watch.core``.
    """
    return None
