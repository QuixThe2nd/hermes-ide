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

from typing import Any, Iterable, List, Optional, Sequence, Tuple

# Injection kinds the display layer knows a specific heading for. Anything
# else (or ``None``) renders the generic heading.
CONTEXT_INJECTION_KIND_LABELS = {
    "message_id": "📎 Injecting message id",
    "restart": "🔁 Injecting restart notice",
    "voice": "🎙️ Injecting voice status",
    "session_reset": "🆕 Injecting session reset",
    "intro": "👋 Injecting intro",
    "surface_switch": "🔀 Injecting surface switch",
    "plugin": "🧩 Injecting plugin context",
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


def context_injection_heading_lines(
    kinds: Optional[Sequence[Optional[str]]],
    *,
    memory_sourced: bool,
    recalled_count: Any = None,
) -> List[str]:
    """Ordered heading lines for an injection payload's kinds.

    ``kinds`` empty/None is the legacy shape: derive from ``memory_sourced``
    exactly as the pre-kinds formatter did. Unknown or ``None`` kinds render
    the generic heading; duplicate OUTPUT lines render once.
    """
    if not kinds:
        return [_memory_heading(recalled_count)] if memory_sourced else [
            CONTEXT_INJECTION_GENERIC_LABEL
        ]
    lines: List[str] = []
    for kind in kinds:
        if kind == KIND_MEMORY:
            line = _memory_heading(recalled_count)
        elif isinstance(kind, str) and kind:
            line = CONTEXT_INJECTION_KIND_LABELS.get(kind, CONTEXT_INJECTION_GENERIC_LABEL)
        else:
            line = CONTEXT_INJECTION_GENERIC_LABEL
        if line not in lines:
            lines.append(line)
    return lines


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
