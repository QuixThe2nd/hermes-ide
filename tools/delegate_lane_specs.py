"""Shared lane specifications and ``delegation.lanes`` resolution.

The four bundled specs are generic capability defaults. A non-empty
``delegation.lanes`` mapping opts into the lane surface; entries override
individual defaults without discarding the rest of a lane's configuration.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home
from tools.delegate_tool_config import _cfg

HARNESSES = frozenset({"omp", "claude", "cursor", "subagent"})
RECEIPT_FIELDS = ["lane", "harness", "model", "reviewer", "verdict"]
LANE_NAMES = ["ui_dev", "client_dev", "backend_dev", "reverse_engineer"]
LANE_TOOL_NAMES = [f"delegate_{name}" for name in LANE_NAMES]

_DEFAULT_MODELS = ["openai/gpt-5.2", "anthropic/claude-sonnet-5-5"]
_CODING_TOOLS = ["read", "write", "edit", "glob", "grep", "bash", "lsp"]


def _lane(soul: str, tools: Iterable[str], *, goal: bool = False) -> Dict[str, Any]:
    return {
        "harness": "omp",
        "models": list(_DEFAULT_MODELS),
        "soul": f"skills/dev-lanes/souls/{soul}.md",
        "skills": [],
        "mcp": None,
        "tools_allowlist": list(tools),
        "reviewer": "requesting agent",
        "receipt_fields": list(RECEIPT_FIELDS),
        # goal=True drives OMP RPC goal continuation (the lane keeps working
        # until the goal completes); False stays a one-shot print-mode turn.
        "goal": goal,
    }


DEFAULT_LANE_SPECS: Dict[str, Dict[str, Any]] = {
    "ui_dev": _lane("ui", [*_CODING_TOOLS, "browser"]),
    "client_dev": _lane("client", _CODING_TOOLS),
    "backend_dev": _lane("backend", _CODING_TOOLS),
    "reverse_engineer": _lane(
        "reverse_engineer", ["read", "glob", "grep"], goal=True
    ),
}


def canonical_lane_name(name: str) -> str:
    """Accept both config keys (``ui_dev``) and tool names (``delegate_ui_dev``)."""
    value = str(name or "").strip()
    return value[len("delegate_"):] if value.startswith("delegate_") else value


def lanes_configured() -> bool:
    """Lane schemas are opt-in: an empty or absent mapping exposes nothing."""
    return bool(_cfg().get("lanes"))


def _override_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        if "name" in raw:
            return [raw]
        result = []
        for key, value in raw.items():
            if isinstance(value, dict):
                result.append({"name": key, **value})
        return result
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    return []


def _find_override(name: str, raw: Any) -> Dict[str, Any]:
    for entry in _override_list(raw):
        if canonical_lane_name(entry.get("name", "")) == name:
            return entry
    return {}


def _string_list(value: Any, field: str, *, nonempty: bool = False) -> List[str]:
    if value is None:
        values: List[Any] = []
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError(f"lane {field} must be an ordered list of strings")
    result = [str(item).strip() for item in values if str(item).strip()]
    if nonempty and not result:
        raise ValueError("lane models must contain at least one model")
    if len(result) != len(values):
        raise ValueError(f"lane {field} entries must be non-empty strings")
    return result


def resolve_lane_spec(name: str, lanes: Any = None) -> Dict[str, Any]:
    """Return one validated spec, with operator values winning field by field.

    ``lanes`` is injectable for tests and embedders; when omitted it is read
    from the active profile at call time, never cached at module import.
    """
    lane_name = canonical_lane_name(name)
    raw = _cfg().get("lanes") if lanes is None else lanes
    override = _find_override(lane_name, raw)
    if lane_name not in DEFAULT_LANE_SPECS and not override:
        raise ValueError(f"unknown delegation lane: {name}")
    spec = copy.deepcopy(DEFAULT_LANE_SPECS.get(lane_name, {}))
    spec.update(copy.deepcopy(override))
    spec["name"] = lane_name
    spec["harness"] = str(spec.get("harness") or "").strip().lower()
    if spec["harness"] not in HARNESSES:
        raise ValueError(
            f"lane {lane_name} has unsupported harness {spec['harness']!r}; "
            f"choose one of {sorted(HARNESSES)}"
        )
    spec["models"] = _string_list(spec.get("models"), "models", nonempty=True)
    spec["skills"] = _string_list(spec.get("skills"), "skills")
    spec["tools_allowlist"] = _string_list(
        spec.get("tools_allowlist"), "tools_allowlist"
    )
    spec["receipt_fields"] = _string_list(
        spec.get("receipt_fields") or RECEIPT_FIELDS, "receipt_fields"
    )
    spec["soul"] = str(spec.get("soul") or "")
    # Operator overlays merge field by field, so an overlay that omits ``goal``
    # keeps the lane default; an explicit ``goal: false`` stays one-shot.
    spec["goal"] = bool(spec.get("goal"))
    raw_reviewer = spec.get("reviewer")
    # Dict reviewers ({provider, model, reasoning_effort}) stay structured for
    # the reviewer pass; everything else stays the display string.
    spec["reviewer"] = (
        raw_reviewer if isinstance(raw_reviewer, dict)
        else str(raw_reviewer or "requesting agent")
    )
    return spec


def resolve_lane_path(value: str) -> Path:
    """Resolve an operator path without embedding a deployment-specific root.

    Existing repo-relative paths (bundled souls and skills) win. Other
    relative paths belong to the active profile, which keeps multiplexed
    callers from reading the launch profile's files.
    """
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    bundled = Path(__file__).resolve().parent.parent / path
    if bundled.exists():
        return bundled
    return get_hermes_home() / path


def _looks_like_path(value: str) -> bool:
    return "\n" not in value and (
        value.startswith((".", "~", "/", "\\"))
        or "/" in value
        or "\\" in value
        or value.lower().endswith((".md", ".txt"))
    )


def soul_path(soul: str) -> Optional[Path]:
    """Return a soul's fragment file, or None when ``soul`` is literal text."""
    if not soul:
        return None
    path = resolve_lane_path(soul)
    if path.is_file():
        return path
    if _looks_like_path(soul):
        raise FileNotFoundError(f"lane soul file does not exist: {path}")
    return None


def read_soul(soul: str) -> str:
    """Load a path-backed soul or return its literal text unchanged."""
    path = soul_path(soul)
    return path.read_text(encoding="utf-8") if path is not None else soul
