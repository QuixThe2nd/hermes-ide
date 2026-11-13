"""Local OCR fallback for billing-dead vision calls.

When the hosted vision model refuses the call because the account is out of
credits (``FailoverReason.billing``: 402 / ``insufficient_quota`` / payment
required), the image bytes are already materialized on disk — a locally
installed tesseract can still extract their text. Anything that is NOT billing
(transient rate limits, content-policy refusals, image size/format errors)
keeps the existing error text: a local OCR dump would misrepresent those.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Prefixed so the text is never presented as a scene description by the model.
OCR_MARKER = "[local OCR only]"

_OCR_TIMEOUT_SECONDS = 30


def is_billing_failure(error: Exception) -> bool:
    """True when ``error`` means the vision provider is out of credits / payment-required."""
    try:
        from agent.error_classifier import FailoverReason, classify_api_error
        return classify_api_error(error).reason is FailoverReason.billing
    except Exception:
        return False


def run_local_ocr(image_path: Any) -> Optional[str]:
    """Tesseract text for ``image_path`` prefixed with ``OCR_MARKER``, or ``None``.

    ``None`` (caller keeps its error) when tesseract is not installed, the
    subprocess fails, or it produces no text. Never raises.
    """
    binary = shutil.which("tesseract")
    if not binary:
        return None
    try:
        from tools.environments.local import build_subprocess_env
        completed = subprocess.run(
            [binary, str(image_path), "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True, text=True, timeout=_OCR_TIMEOUT_SECONDS,
            env=build_subprocess_env(), shell=False, check=False)
    except Exception as exc:
        logger.info("Local OCR fallback unavailable: %s", exc)
        return None
    text = (completed.stdout or "").strip()
    return f"{OCR_MARKER} {text}" if text else None


def billing_ocr_fallback(error: Exception, image_path: Any) -> Optional[str]:
    """``run_local_ocr`` result when ``error`` is a billing failure, else ``None``."""
    if not is_billing_failure(error):
        return None
    return run_local_ocr(Path(image_path))
