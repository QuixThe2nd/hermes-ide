"""Voice transcode arc (#97873): [[audio_as_voice]] works for every audio format.

Fix: shared transcode_to_ogg_opus in base.py and should_send_media_as_audio
honoring explicit is_voice for any audio ext on telegram; is_voice threaded
through the dispatch call sites.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.platforms.base import should_send_media_as_audio, transcode_to_ogg_opus


class TestVoiceRouting(unittest.TestCase):
    def test_telegram_explicit_voice_any_audio_ext(self):
        """[[audio_as_voice]] intent routes EVERY audio format to send_voice."""
        for ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"):
            self.assertTrue(
                should_send_media_as_audio("telegram", ext, is_voice=True),
                f"{ext} with is_voice=True must route to the voice sender",
            )

    def test_telegram_without_intent_unchanged(self):
        """No directive: .mp3/.m4a -> sendAudio; .ogg NOT voice; .wav -> document."""
        self.assertTrue(should_send_media_as_audio("telegram", ".mp3", is_voice=False))
        self.assertTrue(should_send_media_as_audio("telegram", ".m4a", is_voice=False))
        self.assertFalse(should_send_media_as_audio("telegram", ".ogg", is_voice=False))
        self.assertFalse(should_send_media_as_audio("telegram", ".wav", is_voice=False))

    def test_other_platforms_unchanged(self):
        self.assertTrue(should_send_media_as_audio("feishu", ".mp3", is_voice=False))
        self.assertFalse(should_send_media_as_audio("feishu", ".qzx7", is_voice=False))


class TestTranscodeEngine(unittest.TestCase):
    def test_missing_ffmpeg_returns_none(self):
        with patch("shutil.which", return_value=None):
            self.assertIsNone(transcode_to_ogg_opus("/tmp/x.mp3"))

    def test_transcode_failure_cleans_up(self):
        """A failing ffmpeg leaves no orphan temp file behind."""
        import subprocess

        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"boom")
        with patch("shutil.which", return_value="ffmpeg"), \
             patch("subprocess.run", return_value=fake):
            before = set(os.listdir(tempfile.gettempdir()))
            self.assertIsNone(transcode_to_ogg_opus("/tmp/x.mp3"))
            leaked = [f for f in os.listdir(tempfile.gettempdir())
                      if f.startswith("voice_transcode_") and f not in before]
            self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
