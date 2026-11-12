"""Discord thread glue for papercuts: per-papercut #inbox threads.

All Discord access goes through the public helpers exported from
``hermes_starts`` — this module never talks REST itself. Every function is
total: with threads disabled, ``hermes_starts`` missing, or Discord failing,
the journal in ``lifecycle``/``__init__`` is still the source of truth and
nothing here raises into a caller.
"""

from __future__ import annotations

from typing import Dict, List, Optional

TITLE_MAX = 100


def thread_title(item: Dict[str, object]) -> str:
    shortid = str(item.get("id") or "").removeprefix("pc_")
    summary = str(item.get("summary") or "").strip()
    title = f"pc-{shortid} · {summary}".strip()
    return title[:TITLE_MAX]


def thread_body(item: Dict[str, object]) -> str:
    summary = " ".join(str(item.get("summary") or "").split())
    if not summary:
        summary = "A recurring workflow problem needs attention."
    elif summary[-1] not in ".!?":
        summary += "."
    try:
        occurrences = max(1, int(item.get("occurrences") or 1))
    except (TypeError, ValueError):
        occurrences = 1
    if occurrences == 1:
        impact = "It adds an avoidable detour to work that should be straightforward."
    else:
        impact = (
            f"It has happened {occurrences} times, so the same detour keeps "
            "interrupting work."
        )

    lines = [
        f"{summary} {impact}",
        "",
        f"Component: `{item.get('component') or 'unknown'}` · Severity: {item.get('severity') or 'minor'}"
        f" · Times seen: {occurrences}",
    ]
    observed = str(item.get("observed") or "").strip()
    if observed:
        lines.extend(["", f"What happens: {observed}"])
    suggested_fix = str(item.get("suggested_fix") or "").strip()
    if suggested_fix:
        lines.extend(["", f"Suggested next step: {suggested_fix}"])
    session_id = str(item.get("session_id") or "")
    if session_id:
        lines.extend(["", f"First seen in session `{session_id}`"])
    return "\n".join(lines)


def _enabled() -> bool:
    from plugins.papercuts.lifecycle import threads_enabled

    return threads_enabled()


def open_thread(item: Dict[str, object]) -> Optional[str]:
    """Open the papercut's inbox thread; returns thread_id or None."""
    if not _enabled():
        return None
    try:
        from plugins.hermes_starts.threads import open_inbox_thread
    except Exception:
        return None
    try:
        result = open_inbox_thread(thread_title(item), thread_body(item))
    except Exception:
        return None  # the journal stays authoritative; the tick retries
    return str(result["thread_id"]) if result else None


def post(thread_id: str, text: str) -> bool:
    if not thread_id or not _enabled():
        return False
    try:
        from plugins.hermes_starts.threads import post_to_thread
    except Exception:
        return False
    try:
        return bool(post_to_thread(thread_id, text))
    except Exception:
        return False


def flag_needs_human(thread_id: str, item: Dict[str, object]) -> bool:
    """Rename the thread with the 🙋 prefix so it stands out in the channel."""
    if not thread_id or not _enabled():
        return False
    try:
        from plugins.hermes_starts.threads import update_inbox_thread
    except Exception:
        return False
    try:
        return update_inbox_thread(thread_id, name=f"🙋 {thread_title(item)}")
    except Exception:
        return False


def steer_ready(thread_id: str, item: Dict[str, object]) -> bool:
    """Drop the 🙋 prefix and unarchive: the human has steered again."""
    if not thread_id or not _enabled():
        return False
    try:
        from plugins.hermes_starts.threads import update_inbox_thread
    except Exception:
        return False
    try:
        return update_inbox_thread(thread_id, name=thread_title(item), archived=False)
    except Exception:
        return False


def reopen(thread_id: str) -> bool:
    if not thread_id or not _enabled():
        return False
    try:
        from plugins.hermes_starts.threads import update_inbox_thread
    except Exception:
        return False
    try:
        return update_inbox_thread(thread_id, archived=False)
    except Exception:
        return False


def archive(thread_id: str, evidence: str) -> bool:
    """Post the closing evidence, then archive. Never deletes the thread."""
    posted = post(thread_id, evidence)
    if not thread_id or not _enabled():
        return posted
    try:
        from plugins.hermes_starts.threads import update_inbox_thread
    except Exception:
        return posted
    try:
        return update_inbox_thread(thread_id, archived=True) or posted
    except Exception:
        return posted


def format_sighting(item: Dict[str, object], observed: str) -> str:
    occurrences = int(item.get("occurrences") or 1)
    text = f"seen again ×{occurrences}"
    if observed:
        text += f"\n> {observed}"
    return text


def steer_notes_block(notes: Optional[List[Dict[str, object]]]) -> str:
    notes = notes or []
    if not notes:
        return "(none yet)"
    return "\n".join(f"- {note.get('message', '')}" for note in notes)
