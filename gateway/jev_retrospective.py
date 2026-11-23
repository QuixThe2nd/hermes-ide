"""Profile-scoped Python boundary for the Go retrospective Jev classifier.

Classification, recovery policy, and notice formatting live in
``internal/jevnotices``. This module only transports one exchange over stdin,
validates the structured result, and fails closed without an LLM fallback.
When the Go process itself cannot return a result, a fixed local failure notice
keeps the required status visible without attempting classification.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)

_LABELS = {"chat", "task", "approval", "chase"}
_COMPARISONS = {"changed", "consistent", "unavailable"}
_ERROR_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,48}")


def _bridge_failure(error: str) -> Dict[str, Any]:
    return {
        "label": "unknown",
        "confidence": 0.0,
        "comparison": "unavailable",
        "recover": False,
        "notice": f"```\n🔍 Retrospective Intent: failed ({error})\n```",
        "error": error,
    }


def _valid_result(
    result: Any, *, initial_label: str, initial_confidence: float
) -> Dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    label = result.get("label")
    confidence = result.get("confidence")
    comparison = result.get("comparison")
    recover = result.get("recover")
    notice = result.get("notice")
    error = result.get("error", "")
    if label not in _LABELS | {"unknown"} or isinstance(confidence, bool):
        return None
    if (
        not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        return None
    if comparison not in _COMPARISONS or type(recover) is not bool:
        return None
    if not isinstance(notice, str) or not isinstance(error, str):
        return None
    if error and _ERROR_TOKEN.fullmatch(error) is None:
        return None
    if error and (label != "unknown" or recover):
        return None
    if not error and (label == "unknown" or not notice):
        return None
    if recover and not (
        initial_label in {"chat", "unknown"}
        and label == "task"
        and confidence >= 0.6
        and initial_confidence >= 0
    ):
        return None
    return {
        "label": label,
        "confidence": float(confidence),
        "comparison": comparison,
        "recover": recover,
        "notice": notice,
        "error": error,
    }


def classify_retrospective(
    message_text: str,
    final_response: str,
    *,
    initial_label: str = "unknown",
    initial_confidence: float = 0.0,
) -> Dict[str, Any]:
    """Classify one completed exchange through the real Go binary; never raises."""
    try:
        from gateway.jev_notice import go_formatter_binary
        from hermes_cli.goal_intent import _litellm_credential

        binary = go_formatter_binary()
        if binary is None:
            return _bridge_failure("bridge_unavailable")
        request = {
            "op": "classify_retrospective",
            "message": str(message_text or ""),
            "final_response": str(final_response or ""),
            "initial_label": str(initial_label or "unknown"),
            "initial_confidence": float(initial_confidence),
            # The scoped key crosses only over stdin, never argv or logs.
            "credential": _litellm_credential(),
        }
        completed = subprocess.run(
            [binary],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=40,
        )
        if completed.returncode != 0:
            logger.debug("retrospective classifier exited %s", completed.returncode)
            return _bridge_failure("bridge_exit")
        decoded = json.loads(completed.stdout)
    except Exception as exc:
        logger.debug("retrospective classifier bridge failed: %s", type(exc).__name__)
        return _bridge_failure("bridge_error")
    result = _valid_result(
        decoded,
        initial_label=str(initial_label or "unknown").strip().lower(),
        initial_confidence=float(initial_confidence),
    )
    if result is None:
        logger.debug("retrospective classifier returned an invalid result")
        return _bridge_failure("invalid_bridge_result")
    return result
