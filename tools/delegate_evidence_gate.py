"""Shape validation for opt-in coding-delegate test evidence.

This gate validates the declared artifact only. Replaying tests against the base
ref is deliberately separate from result acceptance.
"""

from __future__ import annotations

import copy
from typing import Any

from utils import is_truthy_value


_CODE_MARKERS = ("implement", "fix", "add test", "feature", "bug")
_TEST_MARKERS = ("test", "pytest", "spec")


def requires_test_evidence(task_text: str) -> bool:
    """Return whether the task describes code work accompanied by tests."""
    text = str(task_text or "").lower()
    return (
        any(marker in text for marker in _CODE_MARKERS)
        and any(marker in text for marker in _TEST_MARKERS)
    )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_test_evidence(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False

    tests_added = evidence.get("tests_added")
    if not isinstance(tests_added, list) or not all(
        _nonempty_string(path) for path in tests_added
    ):
        return False

    if not _nonempty_string(evidence.get("fails_on_base")):
        return False
    if not _nonempty_string(evidence.get("passes_on_head")):
        return False

    edge_cases = evidence.get("edge_cases")
    return (
        isinstance(edge_cases, list)
        and bool(edge_cases)
        and all(_nonempty_string(case) for case in edge_cases)
    )


def apply_evidence_gate(
    result_payload: dict,
    task_text: str,
    config: dict,
) -> dict:
    """Downgrade an unproven success, otherwise return the original object."""
    if result_payload.get("success") is not True:
        return result_payload
    if not is_truthy_value(config.get("require_test_evidence", False)):
        return result_payload
    if not requires_test_evidence(task_text):
        return result_payload
    if _valid_test_evidence(result_payload.get("test_evidence")):
        return result_payload

    original_result = copy.deepcopy(result_payload)
    gated = dict(result_payload)
    gated["success"] = False
    gated["error"] = "missing_test_evidence"
    gated["evidence_gate"] = {"original_result": original_result}
    return gated
