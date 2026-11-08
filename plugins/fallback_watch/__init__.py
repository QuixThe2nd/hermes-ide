"""Fallback watch plugin — alert a Discord channel when the primary model falls back."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Service/library plugins register no model tools or gateway hooks.

    The uniform entry point keeps discovery simple while each plugin's logic
    stays in its own modules.
    """
    return None
