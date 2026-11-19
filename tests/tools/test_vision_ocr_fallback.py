"""Local-OCR fallback when the hosted vision model is a billing failure.

When ``vision_analyze`` or the Camofox browser-vision path has already materialized
the image (temp file / saved screenshot) and the vision API call fails with
``FailoverReason.billing`` (402 / ``insufficient_quota`` / payment required), a locally
installed tesseract may still extract the text — returned with a ``[local OCR only]``
marker so it is never presented as a scene description. Non-billing failures
(rate limits, content policy, image problems) keep today's error text unchanged.
"""

import json
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from tools import browser_camofox
from tools.vision_tools import vision_analyze_tool

OCR_TEXT = "UGG OUTLET"
OCR_MARKER = "[local OCR only]"


def _png_bytes(width: int = 320, height: int = 200) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def _fake_tesseract(bin_dir: Path) -> Path:
    """Executable ``tesseract`` shim on PATH: records invocations, prints fixed OCR text."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = bin_dir / "invoked.txt"
    script = bin_dir / "tesseract"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{marker}'\necho '{OCR_TEXT}'\n")
    script.chmod(0o755)
    return marker


def _tesseract_on_path(bin_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + (os.environ.get("PATH") or ""))


class TestVisionAnalyzeLocalOcrFallback:
    @pytest.mark.asyncio
    async def test_billing_failure_runs_local_ocr(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        marker = _fake_tesseract(bin_dir)
        _tesseract_on_path(bin_dir, monkeypatch)
        img = tmp_path / "photo.png"
        img.write_bytes(_png_bytes())

        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   side_effect=Exception("429 insufficient_quota")):
            result = json.loads(
                await vision_analyze_tool(str(img), "describe", "test/model", task_id="ocr-billing"))

        assert result["success"] is True
        assert result["analysis"].startswith(OCR_MARKER)
        assert OCR_TEXT in result["analysis"]
        assert marker.exists()

    @pytest.mark.asyncio
    async def test_billing_failure_without_tesseract_keeps_billing_error(self, tmp_path, monkeypatch):
        marker = _fake_tesseract(tmp_path / "bin")
        empty_path = tmp_path / "nowhere"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        img = tmp_path / "photo.png"
        img.write_bytes(_png_bytes())

        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   side_effect=Exception("429 insufficient_quota")):
            result = json.loads(
                await vision_analyze_tool(str(img), "describe", "test/model", task_id="ocr-no-tess"))

        assert result["success"] is False
        assert "Insufficient credits" in result["analysis"]
        assert "insufficient_quota" in result["analysis"]
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_content_policy_failure_never_runs_ocr(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        marker = _fake_tesseract(bin_dir)
        _tesseract_on_path(bin_dir, monkeypatch)
        img = tmp_path / "photo.png"
        img.write_bytes(_png_bytes())

        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   side_effect=Exception("content_policy violation")):
            result = json.loads(
                await vision_analyze_tool(str(img), "describe", "test/model", task_id="ocr-policy"))

        assert result["success"] is False
        assert OCR_TEXT not in result["analysis"]
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_transient_rate_limit_never_runs_ocr(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        marker = _fake_tesseract(bin_dir)
        _tesseract_on_path(bin_dir, monkeypatch)
        img = tmp_path / "photo.png"
        img.write_bytes(_png_bytes())

        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock,
                   side_effect=Exception("429 rate limit exceeded, try again in 20s")):
            result = json.loads(
                await vision_analyze_tool(str(img), "describe", "test/model", task_id="ocr-ratelimit"))

        assert result["success"] is False
        assert OCR_TEXT not in result["analysis"]
        assert not marker.exists()


class TestCamofoxVisionLocalOcrFallback:
    def test_billing_failure_runs_local_ocr_on_screenshot(self, tmp_path, monkeypatch):
        task_id = "ocr-cam-billing"
        bin_dir = tmp_path / "bin"
        marker = _fake_tesseract(bin_dir)
        _tesseract_on_path(bin_dir, monkeypatch)
        monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
        monkeypatch.setattr(browser_camofox, "get_vnc_url", lambda: None)
        monkeypatch.setattr(
            browser_camofox, "_post",
            MagicMock(return_value={"tabId": "tab-ocr", "url": "https://example.com"}))
        monkeypatch.setattr(
            browser_camofox, "_get",
            MagicMock(return_value={"snapshot": "page snapshot", "refsCount": 1}))
        screenshot_resp = MagicMock()
        screenshot_resp.content = _png_bytes(640, 400)
        monkeypatch.setattr(browser_camofox, "_get_raw", MagicMock(return_value=screenshot_resp))

        def _billing(**kwargs):
            raise Exception("429 insufficient_quota")

        monkeypatch.setattr("agent.auxiliary_client.call_llm", _billing)

        try:
            nav = json.loads(browser_camofox.camofox_navigate("https://example.com", task_id=task_id))
            assert nav["success"] is True

            result = json.loads(browser_camofox.camofox_vision("what is on this page", task_id=task_id))

            assert result["success"] is True
            assert result["analysis"].startswith(OCR_MARKER)
            assert OCR_TEXT in result["analysis"]
            assert result["screenshot_path"]
            assert Path(result["screenshot_path"]).exists()
            assert marker.exists()
        finally:
            browser_camofox._drop_session(task_id)
