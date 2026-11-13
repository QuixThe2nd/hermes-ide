"""Contextual first-touch onboarding hints.

Each hint is shown once per install the *first* time a user hits a behavior
fork, tracked in ``config.yaml`` under ``onboarding.seen.<flag>``. Kept tiny and
dependency-free so both the CLI and gateway can import it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# Flag names (stable — used as config.yaml keys under onboarding.seen)
TOOL_PROGRESS_FLAG = "tool_progress_prompt"
PROFILE_BUILD_FLAG = "profile_build_offered"


def tool_progress_hint_gateway() -> str:
    return ("💡 First-time tip — that tool took a while and I'm streaming every step. If the progress messages "
            "feel noisy, send `/verbose` to cycle modes (all → new → off). This notice won't appear again.")


def tool_progress_hint_cli() -> str:
    return ("(tip) That tool ran for a while. Use /verbose to cycle tool-progress "
            "display modes (all -> new -> off -> verbose). This tip only shows once.")


def _onboarding_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    onboarding = config.get("onboarding") if isinstance(config, Mapping) else None
    return onboarding if isinstance(onboarding, Mapping) else {}


def profile_build_mode(config: Mapping[str, Any]) -> str:
    """``config.onboarding.profile_build``: ``"off"`` never offers; anything else -> ``"ask"``.

    Only governs whether the offer is made; lookups inside the flow are
    consented to separately in conversation.
    """
    mode = _onboarding_section(config).get("profile_build")
    return "off" if isinstance(mode, str) and mode.strip().lower() == "off" else "ask"


def profile_build_directive() -> str:
    """System-note directive appended to the very first message ever.

    Short opt-in profile-build flow persisting to the user-profile memory store;
    phrased so the agent ASKS before any lookup and never silently reads
    connected accounts.
    """
    return (
        "\n\n"
        "[System note: This is the user's very first message ever. After a one-sentence introduction (mention /help "
        "shows commands), OFFER — do not assume — to build a short profile of them so you can be more useful, and "
        "explain they can decline or do it later. If and ONLY IF they accept:\n"
        "  1. Ask for whatever they're comfortable sharing (name, what they do, how they like you to work). "
        "Volunteered facts come first.\n"
        "  2. Before ANY external lookup, say what you intend to look up and get explicit consent for that step. Never "
        "read their connected accounts (email, calendar, etc.) silently — ask each time.\n"
        "  3. With consent, you may use web_search to confirm public details (e.g. employer, public profiles) from the "
        "data points they gave.\n"
        "  4. Save each confirmed, durable fact with the memory tool using target=\"user\" — keep entries compact and "
        "high-signal.\n"
        "If they decline at any point, stop immediately and continue normally. Keep the whole exchange light and "
        "conversational, not an interrogation.]"
    )


def is_seen(config: Mapping[str, Any], flag: str) -> bool:
    """True if the user has already been shown this first-touch hint."""
    seen = _onboarding_section(config).get("seen")
    return bool(seen.get(flag)) if isinstance(seen, Mapping) else False


def mark_seen(config_path: Path, flag: str) -> bool:
    """Persist ``onboarding.seen.<flag> = True`` atomically; False on any error (best-effort)."""
    try:
        from hermes_cli.config import atomic_config_write, read_user_config_raw
    except Exception as e:  # pragma: no cover — dependency issue
        logger.debug("onboarding: failed to import config helpers: %s", e)
        return False
    try:
        cfg: dict = read_user_config_raw(config_path)
        if not isinstance(cfg.get("onboarding"), dict):
            cfg["onboarding"] = {}
        seen = cfg["onboarding"].get("seen")
        if not isinstance(seen, dict):
            seen = cfg["onboarding"]["seen"] = {}
        if seen.get(flag) is not True:
            seen[flag] = True
            atomic_config_write(config_path, cfg)
        return True
    except Exception as e:
        logger.debug("onboarding: failed to mark flag %s: %s", flag, e)
        return False


__all__ = [
    "tool_progress_hint_gateway", "tool_progress_hint_cli",
    "is_seen", "mark_seen",
]
