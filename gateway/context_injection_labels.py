"""Kind taxonomy and heading labels for context-injection progress cards.

Producers tag each injected segment with a *kind*; the ``context.injected``
payload carries the ordered list as ``kinds`` and the gateway renders one
sentence-cased heading line per kind. ``kinds`` is additive display metadata:
it never changes the API-bound bytes, and a payload without it (legacy
producers, or a segment nobody classifies) renders the generic heading —
unclassifiable bytes are never given a specific label they may not deserve.

Tagged turn notes are ``(kind, text)`` pairs staged through the sidecar-notes
channel; legacy plain-string entries keep working (kind ``None``). The staged
notes persist through session state, where a JSON round-trip hands the pairs
back as plain 2-lists, so both shapes classify.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

# Injection kinds the display layer knows a specific heading for. Anything
# else (or ``None``) renders the generic heading.
CONTEXT_INJECTION_KIND_LABELS = {
    "message_id": "📎 Injecting message id",
    "restart": "🔁 Injecting restart notice",
    "session_reset": "🆕 Injecting session reset",
    "intro": "👋 Injecting intro",
    "surface_switch": "🔀 Injecting surface switch",
    "plugin": "🧩 Injecting plugin context",
    "process": "🖥️ Injecting process info",
    "session": "🗺️ Injecting session context",
    "goal": "🎯 Injecting standing goal",
    "skill": "📚 Injecting skill",
    "compaction": "🗜️ Injecting conversation summary",
    "steer": "🧭 Injecting steering update",
    "subdirectory": "🗂️ Injecting directory context",
    "spillover": "📦 Injecting persisted output",
}

CONTEXT_INJECTION_GENERIC_LABEL = "🧠 Injecting context"

# Memory is special-cased: it keeps its dedicated heading (with an optional
# discrete recall count) rather than a table label.
KIND_MEMORY = "memory"


def _memory_heading(recalled_count: Any) -> str:
    if (
        isinstance(recalled_count, int)
        and not isinstance(recalled_count, bool)
        and recalled_count > 0
    ):
        return (
            f"🧠 Injecting {recalled_count} memory"
            if recalled_count == 1
            else f"🧠 Injecting {recalled_count} memories"
        )
    return "🧠 Injecting memory"


def _format_heading(label: str, detail: Any) -> str:
    """One table label with its optional meta detail: ``label (detail)``.

    Blank detail renders the bare label, so a producer without a name never
    renders empty parentheses. Mirrors ``FormatHeading`` in the Go twin.
    """
    return f"{label} ({detail})" if isinstance(detail, str) and detail else label


def context_injection_heading_lines(
    kinds: Optional[Sequence[Optional[str]]],
    *,
    memory_sourced: bool,
    recalled_count: Any = None,
    meta: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Ordered heading lines for an injection payload's kinds.

    ``kinds`` empty/None is the legacy shape: derive from ``memory_sourced``
    exactly as the pre-kinds formatter did. Unknown or ``None`` kinds render
    the generic heading; duplicate OUTPUT lines render once.

    ``meta`` optionally maps a kind to the detail naming WHAT it carried (the
    skill's name); the kind's label renders suffixed ``(detail)``. Memory
    ignores meta (its recall count already speaks), and meta on an
    unclassifiable kind drops with the label.
    """
    if not kinds:
        return [_memory_heading(recalled_count)] if memory_sourced else [
            CONTEXT_INJECTION_GENERIC_LABEL
        ]
    meta = meta if isinstance(meta, Mapping) else {}
    lines: List[str] = []
    for kind in kinds:
        if kind == KIND_MEMORY:
            line = _memory_heading(recalled_count)
        elif isinstance(kind, str) and kind:
            label = CONTEXT_INJECTION_KIND_LABELS.get(kind)
            if label is not None:
                line = _format_heading(label, meta.get(kind))
            else:
                # Unknown kind: unclassifiable bytes never get a specific
                # label they may not deserve, and its meta drops with it.
                line = CONTEXT_INJECTION_GENERIC_LABEL
        else:
            line = CONTEXT_INJECTION_GENERIC_LABEL
        if line not in lines:
            lines.append(line)
    return lines


# Anchors for classify_user_message_injection — the exact bytes the goal loop
# and the skill-command builder emit (hermes_cli/goals.py CONTINUATION_PROMPT_*
# templates; agent/skill_commands.py build_skill_invocation_message).
# Prefix/substring anchors, not a parser: the scaffolding bytes are stable by
# contract. Mirrors ClassifyUserMessage in the Go twin.
_GOAL_CONTINUATION_PREFIX = "[Continuing toward your standing goal"
_SKILL_INVOCATION_OPEN = '[IMPORTANT: The user has invoked the "'


def classify_user_message_injection(text: Any) -> Tuple[Optional[str], str]:
    """Anchor a clean user message to the subsystem that assembled it.

    Returns ``("goal", "")`` for standing-goal continuations,
    ``("skill", "<skill name>")`` for skill slash-command bodies, and
    ``(None, "")`` for a message the user typed themselves. Goal beats
    skill: the goal-continuation templates never embed the skill note, so
    the prefix check is unambiguous.
    """
    if not isinstance(text, str) or not text:
        return (None, "")
    if text.startswith(_GOAL_CONTINUATION_PREFIX):
        return ("goal", "")
    open_at = text.find(_SKILL_INVOCATION_OPEN)
    if open_at >= 0:
        rest = text[open_at + len(_SKILL_INVOCATION_OPEN):]
        close_at = rest.find('"')
        if close_at > 0:
            return ("skill", rest[:close_at])
    return (None, "")


def triggering_message_id_kind(content: Any, message_id: Any) -> Optional[str]:
    """Classify the Discord triggering-id note no producer tag can reach.

    The note is composed into the inbound message text at intake time
    (``run_inbound.discord_triggering_note``), long before any agent or
    sidecar-notes channel exists — tagging it would mean re-injecting its
    bytes. So the display layer recognizes the note by anchored equality with
    the exact bytes reconstructed from the event's known message id: a
    mismatched id (or any other leading text) never matches. This is the one
    sanctioned classification of rendered bytes, and it is narrower than any
    prefix heuristic.
    """
    if not message_id or not isinstance(content, str):
        return None
    try:
        from gateway.run_inbound import discord_triggering_note
    except Exception:
        return None
    return "message_id" if content.startswith(discord_triggering_note(message_id)) else None


# ---------------------------------------------------------------------------
# Tagged turn notes
# ---------------------------------------------------------------------------


def tag_turn_note(kind: Optional[str], text: str) -> Tuple[Optional[str], str]:
    """Stage a sidecar note with its injection kind for the progress card."""
    return (kind, text)


def _tagged_entry(entry: Any) -> Optional[Tuple[Any, Any]]:
    """A staged ``(kind, text)`` entry; tuple or the list a JSON round-trip
    left behind. Anything else is a legacy plain string."""
    if isinstance(entry, tuple) and len(entry) == 2:
        return entry
    if isinstance(entry, list) and len(entry) == 2:
        return (entry[0], entry[1])
    return None


def turn_note_text(entry: Any) -> str:
    """Note bytes of a staged entry; ``""`` for junk shapes."""
    if isinstance(entry, str):
        return entry
    pair = _tagged_entry(entry)
    if pair is not None and isinstance(pair[1], str):
        return pair[1]
    return ""


def turn_note_kind(entry: Any) -> Optional[str]:
    """Injection kind of a staged entry; ``None`` for legacy strings."""
    pair = _tagged_entry(entry)
    if pair is not None:
        kind = pair[0]
        return kind if isinstance(kind, str) and kind else None
    return None


def join_turn_notes(entries: Iterable[Any]) -> Tuple[str, List[Optional[str]]]:
    """Join staged notes to ``(joined_text, kinds)`` — kinds align 1:1 with the
    entries, in staging order. The text is exactly the legacy
    ``"\\n\\n".join`` over the note texts (empty texts included), so the
    model-facing bytes are byte-identical with and without tagging."""
    materialized = list(entries)
    texts = [turn_note_text(entry) for entry in materialized]
    kinds = [turn_note_kind(entry) for entry in materialized]
    return "\n\n".join(texts), kinds
