"""WhatsApp media delivery for send_message (#19105).

Covers two layers:

* ``_bridge_media_type`` — extension/force_document -> bridge mediaType.
* ``_standalone_send`` — text-first then per-file ``/send-media`` uploads,
  media-only (skip ``/send``), and missing-file errors. The bridge HTTP calls
  are mocked at the ``aiohttp.ClientSession`` boundary.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.whatsapp.adapter import _bridge_media_type, _standalone_send


# ---------------------------------------------------------------------------
# _bridge_media_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,force_document,expected",
    [
        ("a.png", False, "image"),
        ("a.JPG", False, "image"),
        ("a.jpeg", False, "image"),
        ("a.webp", False, "image"),
        ("a.gif", False, "image"),
        ("a.mp4", False, "video"),
        ("a.mov", False, "video"),
        ("a.webm", False, "video"),
        ("a.ogg", False, "audio"),
        ("a.opus", False, "audio"),
        ("a.mp3", False, "audio"),
        ("a.wav", False, "audio"),
        ("a.pdf", False, "document"),
        ("a.zip", False, "document"),
        # force_document overrides everything
        ("a.png", True, "document"),
        ("a.mp4", True, "document"),
    ],
)
def test_bridge_media_type(path, force_document, expected):
    assert _bridge_media_type(path, force_document) == expected


# ---------------------------------------------------------------------------
# _standalone_send — bridge HTTP mocked
# ---------------------------------------------------------------------------


def _resp(status, json_data=None, text_data=None):
    r = AsyncMock()
    r.status = status
    r.json = AsyncMock(return_value=json_data or {})
    r.text = AsyncMock(return_value=text_data or "")
    return r


def _session_with(responses):
    """Build a mocked aiohttp.ClientSession that returns *responses* in order
    and records every POST (url, json_payload)."""
    calls = []
    idx = [0]

    def _post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        r = responses[idx[0]] if idx[0] < len(responses) else responses[-1]
        idx[0] += 1
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=r)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx, calls


def _pconfig():
    return SimpleNamespace(token="", extra={"bridge_port": 3000})


def _tmpfile(suffix):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"x")
    f.close()
    return f.name


def test_text_plus_mixed_media_routes_native_types():
    img = _tmpfile(".png")
    vid = _tmpfile(".mp4")
    voice = _tmpfile(".ogg")
    try:
        session_ctx, calls = _session_with(
            [
                _resp(200, {"messageId": "t1"}),
                _resp(200, {"messageId": "m1"}),
                _resp(200, {"messageId": "m2"}),
                _resp(200, {"messageId": "m3"}),
            ]
        )
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            res = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "12345",
                    "hello",
                    media_files=[img, vid, voice],
                )
            )
        assert res["success"] is True
        # text first, then three media uploads in order
        assert calls[0][0].endswith("/send")
        assert calls[0][1]["message"] == "hello"
        media_types = [c[1]["mediaType"] for c in calls if c[0].endswith("/send-media")]
        assert media_types == ["image", "video", "audio"]
        # chat id normalized to a WhatsApp JID
        assert "@" in calls[0][1]["chatId"]
    finally:
        for p in (img, vid, voice):
            os.unlink(p)


def test_missing_captioned_file_falls_back_to_text():
    """If the single captioned file is missing, the caption is delivered as a
    plain /send message rather than being silently lost (W1)."""
    session_ctx, calls = _session_with([_resp(200, {"messageId": "t1"})])
    with patch("aiohttp.ClientSession", return_value=session_ctx):
        res = asyncio.run(
            _standalone_send(
                _pconfig(),
                "12345",
                "",
                media_files=["/no/such/file.png"],
                caption="floor plan",
            )
        )
    # The send still surfaces the missing-file error...
    assert "error" in res
    assert "not found" in res["error"]
    # ...but the caption text was delivered on its own first.
    assert len(calls) == 1
    assert calls[0][0].endswith("/send")
    assert calls[0][1]["message"] == "floor plan"
