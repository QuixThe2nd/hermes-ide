"""Gateway platform command derivations (Discord) from ``COMMAND_REGISTRY``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hermes_cli.commands import (
    COMMAND_REGISTRY, _is_gateway_available, _iter_plugin_command_entries, _resolve_config_gates)

# Logger name parity with the origin module (tests capture "hermes_cli.commands").
logger = logging.getLogger("hermes_cli.commands")

_CMD_NAME_LIMIT = 32  # max command name length shared by Telegram and Discord



def _gateway_available_commands() -> list:
    """Registry entries visible on gateway surfaces (config gates read once)."""
    overrides = _resolve_config_gates()
    return [cmd for cmd in COMMAND_REGISTRY if _is_gateway_available(cmd, overrides)]










def _truncate_desc(desc: str, limit: int) -> str:
    """Clamp a menu description to *limit* chars with a ``...`` tail."""
    return desc if len(desc) <= limit else desc[:limit - 3] + "..."


def _clamp_command_names(
    entries: Sequence[tuple[str, ...]], reserved: set[str]) -> list[tuple[str, ...]]:
    """Enforce the 32-char Telegram/Discord name limit: over-long names are truncated; on a
    collision with *reserved* or an earlier entry a 31-char prefix + digit ``0``-``9`` is tried,
    then the entry is silently dropped. Duplicates are dropped; extra tuple elements pass through.
    """
    used: set[str] = set(reserved)
    result: list = []
    for name, desc, *extra in entries:
        if len(name) > _CMD_NAME_LIMIT:
            candidate = name[:_CMD_NAME_LIMIT]
            if candidate in used:
                prefix = name[:_CMD_NAME_LIMIT - 1]
                for digit in range(10):
                    candidate = f"{prefix}{digit}"
                    if candidate not in used:
                        break
                else:
                    continue
            name = candidate
        if name in used:
            continue
        used.add(name)
        result.append((name, desc, *extra))
    return result



# --- Shared skill/plugin collection for gateway platforms -------------------

def _iter_gateway_skills(platform: str):
    """Yield ``(cmd_key, info, rel_parts)`` for skills eligible as gateway slash commands.

    Scan roots: ``SKILLS_DIR`` plus ``skills.external_dirs`` / trusted project skills dirs;
    anything elsewhere or under ``SKILLS_DIR/.hub`` is skipped, as are skills disabled for
    *platform*. Paths are resolved on both sides (symlinked roots still match) and matched per
    path component (``/my-skills`` never admits ``/my-skills-extra``). ``rel_parts`` is the skill
    dir relative to its root (``("creative", "ascii-art")``). Alphabetical so first-wins
    collision handling is deterministic.
    """
    from pathlib import Path

    from agent.skill_commands import get_skill_commands
    from agent.skill_utils import (
        get_disabled_skill_names, get_external_skills_dirs, get_project_skills_dirs)
    from tools.skills_tool import SKILLS_DIR

    try:
        disabled = get_disabled_skill_names(platform=platform)
    except Exception:
        disabled = set()
    hub_dir = (SKILLS_DIR / ".hub").resolve()
    roots = [SKILLS_DIR.resolve()]
    for getter in (get_external_skills_dirs, get_project_skills_dirs):
        try:
            for d in getter():
                try:
                    roots.append(Path(d).resolve())
                except Exception:
                    continue
        except Exception:
            pass
    skill_cmds = get_skill_commands()
    for cmd_key in sorted(skill_cmds):
        info = skill_cmds[cmd_key]
        skill_path = info.get("skill_md_path", "")
        if not skill_path:
            continue
        sp = Path(skill_path).resolve()
        if sp.is_relative_to(hub_dir):
            continue
        root = next((r for r in roots if sp.is_relative_to(r)), None)
        if root is None or info.get("name", "") in disabled:
            continue
        yield cmd_key, info, sp.parent.relative_to(root).parts


def _collect_gateway_skill_entries(
    platform: str, max_slots: int | None, reserved_names: set[str], desc_limit: int = 100,
    sanitize_name: "Callable[[str], str] | None" = None,
) -> tuple[list[tuple[str, str, str, str]], int]:
    """Collect plugin + skill entries for a gateway platform.

    Plugin slash commands come first and are never trimmed; skill commands (alphabetical) fill
    the remaining *max_slots* (``None`` = every candidate, caller caps). *reserved_names* is
    mutated in place as names are claimed; *sanitize_name* runs before clamping and may return
    "" to skip. Returns ``(entries, hidden_count)``, entries ``(name, description, cmd_key,
    raw_name)`` — ``cmd_key`` "" for plugins; ``raw_name`` is the sanitized pre-clamp name used
    for configured-priority matching (both survive a clamp-induced rename).
    """
    sanitize = sanitize_name or (lambda n: n)

    def _entries(rows) -> list[tuple[str, str, str, str]]:
        """Sanitize + truncate ``(raw_name, description, cmd_key)`` rows, then clamp against
        ``reserved_names``; any failure in the lazy source yields the rows collected so far."""
        out: list[tuple[str, str, str, str]] = []
        try:
            for raw, desc, cmd_key in rows:
                if name := sanitize(raw):
                    out.append((name, _truncate_desc(desc, desc_limit), cmd_key, name))
        except Exception:
            pass
        return _clamp_command_names(out, reserved_names)

    def _plugin_rows():
        from hermes_cli.plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands()
        for cmd_name in sorted(plugin_cmds):
            meta = plugin_cmds[cmd_name]
            yield cmd_name, meta.get("description", "Plugin command"), ""

    plugin_entries = _entries(_plugin_rows())
    reserved_names.update(n for n, *_rest in plugin_entries)
    skill_entries = _entries(
        (cmd_key.lstrip("/"), info.get("description", ""), cmd_key)
        for cmd_key, info, _rel in _iter_gateway_skills(platform))

    if max_slots is None:
        return plugin_entries + skill_entries, 0
    remaining = max(0, max_slots - len(plugin_entries))
    hidden_count = max(0, len(skill_entries) - remaining)
    return (plugin_entries + skill_entries[:remaining])[:max_slots], hidden_count




# --- Discord ----------------------------------------------------------------

def discord_skill_commands_by_category(
    reserved_names: set[str],
) -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str]], int]:
    """``(categories, uncategorized, hidden_count)`` for Discord ``/skill`` autocomplete.

    Skills nested >= 2 levels under a scan root (``creative/ascii-art/SKILL.md``) group under
    ``categories[top_level]``; root-level skills are *uncategorized*. Entries are
    ``(name, description, cmd_key)``, names clamped to 32 chars, descriptions to 100. No
    per-group cap (the caller flattens into one autocomplete callback); ``hidden_count`` only
    reports 32-char clamp collisions against reserved names or earlier skills.

    Scan roots include the local ``SKILLS_DIR`` **and** any configured ``skills.external_dirs`` — matching
    the widened filter applied to the flat ``discord_skill_commands()`` collector in #18741. Without this
    parity, external-dir skills are visible via ``hermes skills list`` and the agent's ``/skill-name``
    dispatch but silently absent from Discord's ``/skill`` autocomplete.
    The legacy 25-group × 25-subcommand caps (from the old nested ``/skill <cat> <name>`` layout) are
    **not** applied — the live caller (``_register_skill_group`` in ``gateway/platforms/discord.py``,
    refactored in PR #11580) flattens these results and feeds them into a single autocomplete callback,
    which scales to thousands of entries without any per-command payload concerns. ``hidden_count`` is
    retained in the return tuple for backward compatibility and still reports skills dropped for other
    reasons (32-char clamp collision vs a reserved name).
    """
    categories: dict[str, list[tuple[str, str, str]]] = {}
    uncategorized: list[tuple[str, str, str]] = []
    # clamped name -> origin; reserved names carry a sentinel so the warning distinguishes a
    # reserved-command collision from two skills colliding on the clamp (the rename-worthy case).
    names_used: dict[str, str] = dict.fromkeys(reserved_names, "<reserved>")
    hidden = 0
    try:
        for cmd_key, info, rel_parts in _iter_gateway_skills("discord"):
            # First (alphabetical) skill wins; the loser is dropped from the picker — warn loudly.
            discord_name = cmd_key.lstrip("/")[:32]
            prior = names_used.get(discord_name)
            if prior == "<reserved>":
                logger.warning(
                    "Discord /skill: %r (from %r) collides on its 32-char "
                    "clamp with a reserved gateway command name %r — the "
                    "skill will not appear in the /skill autocomplete. "
                    "Rename the skill's frontmatter ``name:`` to differ "
                    "in its first 32 chars.",
                    discord_name, cmd_key, discord_name)
            elif prior is not None:
                logger.warning(
                    "Discord /skill: %r and %r both clamp to %r on "
                    "Discord's 32-char command-name limit — only %r "
                    "will appear in the /skill autocomplete. Rename "
                    "one skill's frontmatter ``name:`` to differ in "
                    "its first 32 chars.",
                    prior, cmd_key, discord_name, prior)
            if prior is not None:
                hidden += 1
                continue
            names_used[discord_name] = cmd_key
            entry = (discord_name, _truncate_desc(info.get("description", ""), 100), cmd_key)
            if len(rel_parts) >= 2:
                categories.setdefault(rel_parts[0], []).append(entry)
            else:
                uncategorized.append(entry)
    except Exception:
        pass
    return categories, uncategorized, hidden


