"""brief-gate plugin — validate delegate briefs against the dispatch contract.

Wires one behaviour:

* ``pre_tool_call`` hook — inspects ``delegate_agent`` / ``delegate_task``
  calls and warns, via the agent log, when the brief is missing one of the
  dispatch-contract field classes:

    - ``invariant``   — the observable condition that must hold
    - ``owner``       — what existing code owns that condition today
                        ("existing mechanism" counts as a synonym)
    - ``acceptance``  — the checks that prove the condition (tests,
                        before/after evidence)

The gate NEVER blocks: the hook always returns ``None`` so the dispatch
proceeds exactly as the model asked. It warns loudly and rate-limits to one
warning per missing-field class per session, tracked in memory on the plugin
closure — nothing is written to disk and nothing is injected into the
conversation (which would break strict role alternation).

Brief extraction handles both delegate shapes: the single-task form
(``goal`` + optional ``context``) and the batch form (``tasks=[...]``, each
with its own ``goal``/``context`` checked independently). A call with neither
field — or non-dict args — is not a brief and is ignored.

Matching is case-insensitive: a class is present when any of its label
patterns appears in the concatenated brief text, either as a labeled line
(``INVARIANT: ...``, ``- owner: ...``) or as a plain word anywhere in the
text. The synonyms per class:

    invariant:  "invariant"
    owner:      "owner", "existing mechanism"
    acceptance: "acceptance", "test"/"tests"

Configuration (no environment variables):

* ``plugins.entries.brief-gate.settings.enabled`` (bool, default true) —
  the canonical plugin settings surface.
* ``plugins.entries.brief-gate.settings.required_fields`` (list of class
  names; default all three) — narrow the checked classes.
* Flat alias ``plugins.brief_gate.enabled`` / ``plugins.brief_gate
  .required_fields`` in config.yaml, consulted when the per-entry settings
  say nothing. The flat section uses the underscore spelling.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tool names this gate inspects (both spellings registered in
# tools/delegate_tool.py; ``delegate_task`` is the legacy alias).
_DELEGATE_TOOLS = frozenset({"delegate_agent", "delegate_task"})

# Contract field classes and their recognized labels/synonyms. Each class is
# "present" in a brief when any of its patterns matches (case-insensitive).
_FIELD_PATTERNS: Dict[str, Tuple[re.Pattern, ...]] = {
    "invariant": (
        re.compile(r"\binvariants?\b", re.IGNORECASE),
    ),
    "owner": (
        re.compile(r"\bowners?\b", re.IGNORECASE),
        re.compile(r"\bexisting[\s-]+mechanisms?\b", re.IGNORECASE),
    ),
    "acceptance": (
        re.compile(r"\bacceptance\b", re.IGNORECASE),
        re.compile(r"\btests?\b", re.IGNORECASE),
    ),
}

_DEFAULT_REQUIRED_FIELDS: Tuple[str, ...] = ("invariant", "owner", "acceptance")

# Human hint appended to each warning so the log line is actionable.
_FIELD_HINTS: Dict[str, str] = {
    "invariant": "an INVARIANT section naming the observable condition that must hold",
    "owner": "an OWNER / EXISTING MECHANISM section naming the code that owns the condition today",
    "acceptance": "an ACCEPTANCE / TESTS section listing the checks that prove the condition",
}

# Flat config.yaml alias section name (underscore spelling) under ``plugins``.
_FLAT_SECTION = "brief_gate"


# ---------------------------------------------------------------------------
# Brief extraction
# ---------------------------------------------------------------------------


def _brief_texts(args: Any) -> List[str]:
    """One brief text per delegate dispatch unit.

    Single shape: ``goal`` + ``context`` concatenated. Batch shape: each
    ``tasks[]`` entry contributes its own ``goal`` + ``context``. Anything
    else (non-dict args, no fields, non-string fields) yields no texts, which
    the hook treats as "no brief to judge".
    """
    if not isinstance(args, dict):
        return []
    texts: List[str] = []
    goal = args.get("goal")
    if isinstance(goal, str) and goal:
        context = args.get("context")
        texts.append(goal + "\n" + (context if isinstance(context, str) else ""))
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            t_goal = task.get("goal")
            if not isinstance(t_goal, str) or not t_goal:
                continue
            t_context = task.get("context")
            texts.append(t_goal + "\n" + (t_context if isinstance(t_context, str) else ""))
    return texts


def _missing_fields(brief: str, required: Iterable[str]) -> List[str]:
    """Required classes with no matching label anywhere in the brief."""
    missing: List[str] = []
    for field in required:
        patterns = _FIELD_PATTERNS.get(field)
        if not patterns:
            continue
        if not any(pattern.search(brief) for pattern in patterns):
            missing.append(field)
    return missing


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _flat_section() -> Dict[str, Any]:
    """The ``plugins.brief_gate`` mapping from config.yaml, else {}.

    Read through ``load_config_readonly`` so a malformed or unreadable
    config simply means "no flat overrides" — the gate stays at defaults.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return {}
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return {}
    section = plugins.get(_FLAT_SECTION)
    return section if isinstance(section, dict) else {}


def _resolve_enabled(ctx: Any) -> bool:
    """Per-entry settings win; then the flat section; default on."""
    get_config = getattr(ctx, "get_config", None) if ctx is not None else None
    if callable(get_config):
        try:
            value = get_config("enabled", None)
        except Exception:
            value = None
        if isinstance(value, bool):
            return value
    flat = _flat_section().get("enabled")
    if isinstance(flat, bool):
        return flat
    return True


def _resolve_required_fields(ctx: Any) -> List[str]:
    """The narrowed class set, intersected with the known classes."""
    raw: Any = None
    get_config = getattr(ctx, "get_config", None) if ctx is not None else None
    if callable(get_config):
        try:
            raw = get_config("required_fields", None)
        except Exception:
            raw = None
    if raw is None:
        raw = _flat_section().get("required_fields")
    if not isinstance(raw, list):
        return list(_DEFAULT_REQUIRED_FIELDS)
    return [f for f in raw if isinstance(f, str) and f in _FIELD_PATTERNS]


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Subscribe the brief validator to ``pre_tool_call``.

    Rate-limit state lives on this closure: ``warned`` maps
    ``(session_id, field_class)`` → True once that class has warned in that
    session. In-memory only — a restart resets the budget, which is the
    desired behaviour for a per-session rate limit.
    """
    warned: Dict[Tuple[str, str], bool] = {}

    def _on_pre_tool_call(
        tool_name: str = "",
        args: Any = None,
        session_id: str = "",
        **_: Any,
    ) -> None:
        """Warn on under-specified delegate briefs; never blocks.

        Returns ``None`` unconditionally — a ``block`` directive would
        contradict the gate's contract (the user explicitly asked for the
        dispatch). Any internal failure is logged at DEBUG and also yields
        ``None``: the gate must never break a dispatch.
        """
        try:
            if tool_name not in _DELEGATE_TOOLS:
                return None
            if not _resolve_enabled(ctx):
                return None
            texts = _brief_texts(args)
            if not texts:
                return None
            required = _resolve_required_fields(ctx)
            if not required:
                return None
            session_key = session_id if isinstance(session_id, str) else ""
            for brief in texts:
                for field in _missing_fields(brief, required):
                    key = (session_key, field)
                    if warned.get(key):
                        continue
                    warned[key] = True
                    logger.warning(
                        "brief-gate: delegate brief missing %s field "
                        "(session %s, tool %s). Add %s so the delegate can "
                        "verify the contract. Dispatch is NOT blocked.",
                        field,
                        session_key or "?",
                        tool_name,
                        _FIELD_HINTS.get(field, f"a {field} section"),
                    )
            return None
        except Exception:
            logger.debug("brief-gate hook failed (non-fatal)", exc_info=True)
            return None

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
