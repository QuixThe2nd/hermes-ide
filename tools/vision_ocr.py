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
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class LocalOCRResult:
    success: bool
    text: str = ""


def run_local_ocr_checked(
    image_path: Any, *, timeout: float = _OCR_TIMEOUT_SECONDS,
    extra_env: Optional[dict[str, str]] = None,
) -> LocalOCRResult:
    """Distinguish a successful empty OCR read from unavailable/failed inspection."""
    binary = shutil.which("tesseract")
    if not binary:
        return LocalOCRResult(False)
    try:
        from tools.environments.local import build_subprocess_env
        env = {**build_subprocess_env(), **(extra_env or {})}
        completed = subprocess.run(
            [binary, str(image_path), "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True, text=True, timeout=timeout,
            env=env, shell=False, check=False)
    except Exception as exc:
        logger.info("Local OCR unavailable: %s", exc)
        return LocalOCRResult(False)
    if completed.returncode != 0:
        return LocalOCRResult(False)
    return LocalOCRResult(True, (completed.stdout or "").strip())


def run_local_ocr(image_path: Any) -> Optional[str]:
    """Tesseract text for ``image_path`` prefixed with ``OCR_MARKER``, or ``None``.

    ``None`` (caller keeps its error) when tesseract is not installed, the
    subprocess fails, or it produces no text. Never raises.
    """
    result = run_local_ocr_checked(image_path)
    return f"{OCR_MARKER} {result.text}" if result.success and result.text else None


def billing_ocr_fallback(error: Exception, image_path: Any) -> Optional[str]:
    """``run_local_ocr`` result when ``error`` is a billing failure, else ``None``."""
    if not is_billing_failure(error):
        return None
    return run_local_ocr(Path(image_path))
